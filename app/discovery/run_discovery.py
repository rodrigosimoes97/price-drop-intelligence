from __future__ import annotations

import argparse
import json
import logging
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from app.affiliate.shopee import to_affiliate_url
from app.db import Database
from app.discovery.shopee import DiscoveryCategory, ShopeeDiscoveryConfig, ShopeeDiscoveryProvider
from app.reporting import generate_json_report
from app.utils import load_yaml, setup_logging, utc_now

LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run product discovery")
    p.add_argument("--store", default="shopee")
    p.add_argument("--country", default="BR")
    p.add_argument("--max-active", type=int, default=100)
    p.add_argument("--ttl-hours", type=int, default=72)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--only-category", default=None)
    return p.parse_args()


def load_discovery_config(path: str = "discovery.yaml", args: argparse.Namespace | None = None) -> ShopeeDiscoveryConfig:
    cfg = load_yaml(path).get("shopee", {})
    categories = [DiscoveryCategory(**row) for row in cfg.get("categories", [])]
    return ShopeeDiscoveryConfig(
        enabled=bool(cfg.get("enabled", True)),
        country=(args.country if args else cfg.get("country", "BR")),
        currency=cfg.get("currency", "BRL"),
        max_active_products=(args.max_active if args else int(cfg.get("max_active_products", 100))),
        ttl_hours=(args.ttl_hours if args else int(cfg.get("ttl_hours", 72))),
        categories=categories,
        filters=cfg.get("filters", {}),
        http=cfg.get("http", {}),
        affiliate=cfg.get("affiliate", {}),
    )


def _write_md(path: str, payload: dict) -> None:
    lines = [
        "# Discovery Report",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Store: `{payload['store']}`",
        f"- Country: `{payload['country']}`",
        f"- Dry run: `{payload['dry_run']}`",
        f"- Collected: **{payload['collected']}**",
        f"- Saved: **{payload['saved']}**",
        f"- Inserted: **{payload['inserted']}**",
        f"- Updated: **{payload['updated']}**",
        f"- Expired: **{payload['expired']}**",
        "",
        "## Top 20",
        "",
        "| id | title | score | sold | rating | url |",
        "|---|---|---:|---:|---:|---|",
    ]
    for item in payload.get("top_items", [])[:20]:
        lines.append(f"| {item['id']} | {item['title'][:60]} | {item['score']:.2f} | {item.get('sold')} | {item.get('rating')} | {item['url']} |")

    lines.extend(["", "## Strategy stats", ""])
    for k, v in payload.get("strategy_stats", {}).items():
        lines.append(f"- `{k}`: {v}")

    lines.extend(["", "## Errors", ""])
    errs = payload.get("errors", [])
    if errs:
        for err in errs:
            lines.append(f"- {err}")
    else:
        lines.append("- none")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    log_path = setup_logging(debug=args.debug, prefix="discovery")
    run_id = str(uuid4())

    if args.store != "shopee":
        raise ValueError("only shopee is supported in this MVP")

    cfg = load_discovery_config(args=args)
    provider = ShopeeDiscoveryProvider(cfg, only_category=args.only_category)

    products, strategy_stats, errors = provider.discover()
    now = utc_now()
    for p in products:
        p.url = to_affiliate_url(p.canonical_url, cfg.affiliate)
        p.expires_at_utc = now + timedelta(hours=cfg.ttl_hours)

    inserted = updated = expired = trimmed = 0
    db = Database()
    db.init()

    if not args.dry:
        inserted, updated = db.upsert_products(products)
        keep_ids = {p.id for p in products}
        expired = db.expire_products(store="shopee", now_utc=now, keep_ids=keep_ids, ttl_hours=cfg.ttl_hours)
        trimmed = db.trim_active_products(store="shopee", country=cfg.country, max_active=cfg.max_active_products)

    payload = {
        "run_id": run_id,
        "store": "shopee",
        "country": cfg.country,
        "dry_run": args.dry,
        "categories": [c.name for c in (cfg.categories or []) if (not args.only_category or c.name == args.only_category)],
        "collected": len(products),
        "saved": 0 if args.dry else inserted + updated,
        "inserted": inserted,
        "updated": updated,
        "expired": expired + trimmed,
        "strategy_stats": strategy_stats,
        "errors": errors,
        "log_file": str(log_path),
        "top_items": [
            {
                "id": p.id,
                "title": p.title,
                "score": p.score,
                "sold": p.sold,
                "rating": p.rating,
                "url": p.url,
            }
            for p in products[:20]
        ],
    }

    Path("reports").mkdir(exist_ok=True)
    generate_json_report("reports/discovery_latest.json", payload)
    _write_md("reports/discovery_latest.md", payload)

    db.close()
    LOGGER.info("discovery finished collected=%s inserted=%s updated=%s expired=%s", len(products), inserted, updated, expired + trimmed)


if __name__ == "__main__":
    main()
