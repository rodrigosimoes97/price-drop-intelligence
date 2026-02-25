from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from app.models import PriceSnapshot, Product

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
        self.conn.commit()

    def upsert_product(self, p: Product) -> None:
        self.conn.execute(
            """
            INSERT INTO products (id, country, store, url, currency, title_hint, tags, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                country=excluded.country,
                store=excluded.store,
                url=excluded.url,
                currency=excluded.currency,
                title_hint=excluded.title_hint,
                tags=excluded.tags,
                updated_at=excluded.updated_at
            """,
            (
                p.id,
                p.country,
                p.store,
                p.url,
                p.currency,
                p.title_hint,
                ",".join(p.tags),
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

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
                str(s.raw),
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

    def insert_alert(
        self,
        product_id: str,
        current_price: float,
        reference_price: float | None,
        drop_percent: float,
        drop_amount: float,
        reason: str,
        run_id: str,
    ) -> None:
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

    def update_run_end(
        self,
        run_id: str,
        finished_at: str,
        snapshots_ok: int,
        snapshots_error: int,
        alerts_sent: int,
        errors_json: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at=?, snapshots_ok=?, snapshots_error=?, alerts_sent=?, errors_json=?
            WHERE run_id=?
            """,
            (finished_at, snapshots_ok, snapshots_error, alerts_sent, errors_json, run_id),
        )
        self.conn.commit()
