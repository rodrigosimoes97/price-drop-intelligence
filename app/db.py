from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from app.models import DiscoveredProduct, PriceSnapshot, Product

DB_PATH = Path("data/prices.db")


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                country TEXT NOT NULL,
                store TEXT NOT NULL,
                url TEXT NOT NULL,
                currency TEXT NOT NULL,
                title_hint TEXT,
                tags TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                price REAL,
                currency TEXT NOT NULL,
                in_stock INTEGER,
                title TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                raw_json TEXT,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                current_price REAL NOT NULL,
                reference_price REAL,
                drop_percent REAL,
                drop_amount REAL,
                reason TEXT,
                run_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                total_products INTEGER NOT NULL,
                snapshots_ok INTEGER NOT NULL,
                snapshots_error INTEGER NOT NULL,
                alerts_sent INTEGER NOT NULL,
                errors_json TEXT
            );
            """
        )
        self._ensure_product_columns()
        self.conn.commit()

    def _ensure_product_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(products)").fetchall()}
        additions = {
            "canonical_url": "TEXT",
            "image_url": "TEXT",
            "shop_id": "TEXT",
            "item_id": "TEXT",
            "sold": "INTEGER",
            "rating": "REAL",
            "rating_count": "INTEGER",
            "source": "TEXT",
            "discovered_at_utc": "TEXT",
            "expires_at_utc": "TEXT",
            "score": "REAL",
        }
        for col, ctype in additions.items():
            if col not in columns:
                self.conn.execute(f"ALTER TABLE products ADD COLUMN {col} {ctype}")

    def upsert_product(self, p: Product) -> None:
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO products (id, country, store, url, canonical_url, currency, title_hint, tags, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                country=excluded.country,
                store=excluded.store,
                url=excluded.url,
                canonical_url=excluded.canonical_url,
                currency=excluded.currency,
                title_hint=excluded.title_hint,
                tags=excluded.tags,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (p.id, p.country, p.store, p.url, p.url, p.currency, p.title_hint, ",".join(p.tags), "watchlist", now_iso),
        )
        self.conn.commit()

    def upsert_products(self, products: list[DiscoveredProduct]) -> tuple[int, int]:
        inserted = 0
        updated = 0
        for p in products:
            existing = self.conn.execute("SELECT id FROM products WHERE id=?", (p.id,)).fetchone()
            self.conn.execute(
                """
                INSERT INTO products (
                    id, country, store, url, canonical_url, currency, title_hint, tags, source,
                    image_url, shop_id, item_id, sold, rating, rating_count,
                    discovered_at_utc, expires_at_utc, score, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    country=excluded.country,
                    store=excluded.store,
                    url=excluded.url,
                    canonical_url=excluded.canonical_url,
                    currency=excluded.currency,
                    title_hint=excluded.title_hint,
                    tags=excluded.tags,
                    source=excluded.source,
                    image_url=excluded.image_url,
                    shop_id=excluded.shop_id,
                    item_id=excluded.item_id,
                    sold=excluded.sold,
                    rating=excluded.rating,
                    rating_count=excluded.rating_count,
                    discovered_at_utc=excluded.discovered_at_utc,
                    expires_at_utc=excluded.expires_at_utc,
                    score=excluded.score,
                    updated_at=excluded.updated_at
                """,
                (
                    p.id,
                    p.country,
                    p.store,
                    p.url,
                    p.canonical_url,
                    p.currency,
                    p.title,
                    ",".join(p.tags),
                    p.source,
                    p.image_url,
                    p.shop_id,
                    p.item_id,
                    p.sold,
                    p.rating,
                    p.rating_count,
                    p.discovered_at_utc.isoformat(),
                    p.expires_at_utc.isoformat(),
                    p.score,
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )
            if existing:
                updated += 1
            else:
                inserted += 1

            if p.price is not None:
                self.insert_snapshot(
                    PriceSnapshot(
                        product_id=p.id,
                        timestamp_utc=p.discovered_at_utc,
                        price=p.price,
                        currency=p.currency,
                        in_stock=True,
                        title=p.title,
                        source="discovery",
                        raw={"source": p.source},
                    )
                )
        self.conn.commit()
        return inserted, updated

    def expire_products(self, store: str, now_utc: datetime, keep_ids: set[str], ttl_hours: int) -> int:
        threshold = (now_utc - timedelta(hours=ttl_hours)).isoformat()
        sql = """
        UPDATE products
        SET expires_at_utc=?
        WHERE store=?
          AND id NOT IN ({placeholders})
          AND (expires_at_utc IS NULL OR expires_at_utc > ?)
          AND (discovered_at_utc IS NULL OR discovered_at_utc <= ?)
        """
        ids = list(keep_ids) or ["__none__"]
        formatted = sql.format(placeholders=",".join("?" for _ in ids))
        params = [now_utc.isoformat(), store, *ids, now_utc.isoformat(), threshold]
        cur = self.conn.execute(formatted, params)
        self.conn.commit()
        return cur.rowcount

    def get_active_products(self, store: str | None, country: str | None, now_utc: datetime) -> list[Product]:
        clauses = ["(expires_at_utc IS NULL OR expires_at_utc > ?)"]
        params: list[str] = [now_utc.isoformat()]
        if store:
            clauses.append("store=?")
            params.append(store)
        if country:
            clauses.append("country=?")
            params.append(country)

        rows = self.conn.execute(
            f"""
            SELECT id, country, store, url, currency, title_hint, tags
            FROM products
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(score,0) DESC, COALESCE(discovered_at_utc, updated_at) DESC
            """,
            params,
        ).fetchall()
        return [
            Product(
                id=r["id"],
                country=r["country"],
                store=r["store"],
                url=r["url"],
                currency=r["currency"],
                title_hint=r["title_hint"],
                tags=(r["tags"].split(",") if r["tags"] else []),
            )
            for r in rows
        ]

    def trim_active_products(self, store: str, country: str, max_active: int) -> int:
        rows = self.conn.execute(
            """
            SELECT id FROM products
            WHERE store=? AND country=? AND (expires_at_utc IS NULL OR expires_at_utc > ?)
            ORDER BY COALESCE(score,0) DESC, COALESCE(discovered_at_utc, updated_at) DESC
            """,
            (store, country, datetime.now(tz=timezone.utc).isoformat()),
        ).fetchall()
        if len(rows) <= max_active:
            return 0
        keep = {r["id"] for r in rows[:max_active]}
        return self.expire_products(store=store, now_utc=datetime.now(tz=timezone.utc), keep_ids=keep, ttl_hours=0)

    def insert_snapshot(self, s: PriceSnapshot) -> None:
        self.conn.execute(
            """
            INSERT INTO snapshots (
                product_id, timestamp_utc, price, currency, in_stock, title, source, status, error, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s.product_id,
                s.timestamp_utc.isoformat(),
                s.price,
                s.currency,
                None if s.in_stock is None else int(s.in_stock),
                s.title,
                s.source,
                s.status,
                s.error,
                json.dumps(s.raw, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def get_recent_valid_prices(self, product_id: str, lookback_days: int) -> list[float]:
        since = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
        rows = self.conn.execute(
            """
            SELECT price FROM snapshots
            WHERE product_id=? AND status='ok' AND price IS NOT NULL AND timestamp_utc >= ?
            ORDER BY timestamp_utc DESC
            """,
            (product_id, since.isoformat()),
        ).fetchall()
        return [float(r["price"]) for r in rows if r["price"] is not None]

    def get_reference_price(self, product_id: str, lookback_days: int, mode: str) -> float | None:
        prices = self.get_recent_valid_prices(product_id, lookback_days)
        if not prices:
            return None
        if mode == "last_price":
            return prices[0]
        if mode == "rolling_max":
            return max(prices)
        if mode == "rolling_median":
            return float(median(prices))
        return prices[0]

    def get_lowest_price(self, product_id: str, lookback_days: int) -> float | None:
        prices = self.get_recent_valid_prices(product_id, lookback_days)
        return min(prices) if prices else None

    def get_last_alert(self, product_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE product_id=? ORDER BY timestamp_utc DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    def insert_alert(self, product_id: str, current_price: float, reference_price: float | None, drop_percent: float, drop_amount: float, reason: str, run_id: str) -> None:
        self.conn.execute(
            """
            INSERT INTO alerts (product_id, timestamp_utc, current_price, reference_price, drop_percent, drop_amount, reason, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                datetime.now(tz=timezone.utc).isoformat(),
                current_price,
                reference_price,
                drop_percent,
                drop_amount,
                reason,
                run_id,
            ),
        )
        self.conn.commit()

    def insert_run_start(self, run_id: str, started_at: str, total_products: int) -> None:
        self.conn.execute(
            """
            INSERT INTO runs (run_id, started_at, total_products, snapshots_ok, snapshots_error, alerts_sent, errors_json)
            VALUES (?, ?, ?, 0, 0, 0, '{}')
            """,
            (run_id, started_at, total_products),
        )
        self.conn.commit()

    def update_run_end(self, run_id: str, finished_at: str, snapshots_ok: int, snapshots_error: int, alerts_sent: int, errors_json: str) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at=?, snapshots_ok=?, snapshots_error=?, alerts_sent=?, errors_json=?
            WHERE run_id=?
            """,
            (finished_at, snapshots_ok, snapshots_error, alerts_sent, errors_json, run_id),
        )
        self.conn.commit()
