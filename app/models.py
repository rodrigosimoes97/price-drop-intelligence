from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ItemRules:
    drop_percent_min: float | None = None
    drop_amount_min: float | None = None
    cooldown_hours: int | None = None
    min_price_threshold: float | None = None
    max_price_threshold: float | None = None


@dataclass(slots=True)
class Product:
    id: str
    country: str
    store: str
    url: str
    currency: str
    tags: list[str] = field(default_factory=list)
    title_hint: str | None = None
    rules: ItemRules = field(default_factory=ItemRules)


@dataclass(slots=True)
class DiscoveredProduct:
    id: str
    store: str
    country: str
    currency: str
    canonical_url: str
    url: str
    title: str
    tags: list[str]
    source: str
    discovered_at_utc: datetime
    expires_at_utc: datetime
    score: float
    price: float | None = None
    image_url: str | None = None
    shop_id: str | None = None
    item_id: str | None = None
    sold: int | None = None
    rating: float | None = None
    rating_count: int | None = None


@dataclass(slots=True)
class PriceSnapshot:
    product_id: str
    timestamp_utc: datetime
    price: float | None
    currency: str
    in_stock: bool | None
    title: str | None
    source: str
    raw: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    error: str | None = None


@dataclass(slots=True)
class AlertDecision:
    should_alert: bool
    reason: str
    drop_percent: float = 0.0
    drop_amount: float = 0.0
    reference_price: float | None = None
    reference_label: str = ""
    current_price: float | None = None
    lowest_lookback: float | None = None
    bypassed_cooldown: bool = False


@dataclass(slots=True)
class RunStats:
    run_id: str
    total_products: int = 0
    snapshots_ok: int = 0
    snapshots_error: int = 0
    alerts_sent: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
