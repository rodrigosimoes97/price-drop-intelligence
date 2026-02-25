from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import asdict
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from app.db import Database
from app.detector import DetectorConfig, PriceDropDetector
from app.models import AlertDecision, Product, RunStats
from app.providers import get_provider
from app.reporting import generate_json_report, generate_markdown_report, generate_site
from app.telegram import TelegramNotifier
from app.utils import filter_products, load_watchlist, load_yaml, setup_logging, utc_now

LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Price drop monitoring")
    p.add_argument("--mode", choices=["live", "dry"], default="dry")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--only-store", default=None)
    p.add_argument("--only-country", default=None)
    p.add_argument("--only-tags", default=None, help="comma separated tags")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def load_detector_config(path: str = "config.yaml") -> DetectorConfig:
    cfg = load_yaml(path)
    drop_amount = cfg.get("drop_amount_min", {})
    return DetectorConfig(
        drop_percent_min=float(cfg.get("drop_percent_min", 10)),
        drop_amount_min_by_currency={k.upper(): float(v) for k, v in drop_amount.items()},
        lookback_days=int(cfg.get("lookback_days", 30)),
        reference_mode=str(cfg.get("reference_mode", "rolling_max")),
        cooldown_hours=int(cfg.get("cooldown_hours", 24)),
        min_price_threshold=cfg.get("min_price_threshold"),
        max_price_threshold=cfg.get("max_price_threshold"),
        realert_extra_drop_percent=float(cfg.get("realert_extra_drop_percent", 5)),
    )


def main() -> None:
    args = parse_args()
    log_path = setup_logging(debug=args.debug)
    LOGGER.info("starting run")

    run_id = str(uuid4())
    products = load_watchlist("watchlist.yaml")
    tags = set((args.only_tags or "").split(",")) if args.only_tags else None
    products = filter_products(products, args.only_store, args.only_country, tags, args.limit)

    db = Database()
    db.init()

    stats = RunStats(run_id=run_id, total_products=len(products), started_at=utc_now())
    db.insert_run_start(run_id, stats.started_at.isoformat(), stats.total_products)

    detector = PriceDropDetector(db, load_detector_config())
    notifier = TelegramNotifier(dry_run=args.mode == "dry")

    alerts: list[tuple[Product, AlertDecision]] = []
    errors = Counter()
    top_drops: list[dict] = []

    for product in products:
        db.upsert_product(product)
        provider = get_provider(product.store)
        snapshot = provider.fetch_product(product)
        db.insert_snapshot(snapshot)

        if snapshot.status != "ok" or snapshot.price is None:
            stats.snapshots_error += 1
            errors[provider.name] += 1
            continue

        stats.snapshots_ok += 1
        decision = detector.evaluate(product, snapshot.price, run_id)
        if decision.should_alert:
            alerts.append((product, decision))
            top_drops.append(
                {
                    "product_id": product.id,
                    "currency": product.currency,
                    "current_price": decision.current_price,
                    "reference_price": decision.reference_price,
                    "drop_percent": decision.drop_percent,
                    "drop_amount": decision.drop_amount,
                    "url": product.url,
                }
            )

    top_drops = sorted(top_drops, key=lambda x: x["drop_percent"], reverse=True)
    sent_count = notifier.send_alerts(alerts)
    stats.alerts_sent = sent_count
    stats.finished_at = utc_now()

    run_payload = {
        "run_id": run_id,
        "started_at": stats.started_at.isoformat(),
        "finished_at": stats.finished_at.isoformat(),
        "mode": args.mode,
        "total_products": stats.total_products,
        "snapshots_ok": stats.snapshots_ok,
        "snapshots_error": stats.snapshots_error,
        "alerts_sent": stats.alerts_sent,
        "log_file": str(log_path),
    }

    json_payload = {
        "run": run_payload,
        "alerts": top_drops,
        "errors_by_provider": dict(errors),
    }
    generate_json_report("reports/latest.json", json_payload)
    generate_markdown_report("reports/latest.md", run_payload, top_drops, dict(errors))

    if load_yaml("config.yaml").get("site_enabled", False):
        generate_site("reports/latest.json", "site")

    db.update_run_end(
        run_id,
        stats.finished_at.isoformat(),
        stats.snapshots_ok,
        stats.snapshots_error,
        stats.alerts_sent,
        json.dumps(dict(errors)),
    )
    db.close()
    LOGGER.info("run completed")


if __name__ == "__main__":
    main()
