from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import Database
from app.models import AlertDecision, Product


@dataclass
class DetectorConfig:
    drop_percent_min: float = 10.0
    drop_amount_min_by_currency: dict[str, float] | None = None
    lookback_days: int = 30
    reference_mode: str = "rolling_max"
    cooldown_hours: int = 24
    min_price_threshold: float | None = None
    max_price_threshold: float | None = None
    realert_extra_drop_percent: float = 5.0

    def amount_min_for(self, currency: str) -> float:
        mapping = self.drop_amount_min_by_currency or {}
        return float(mapping.get(currency.upper(), 0.0))


class PriceDropDetector:
    def __init__(self, db: Database, config: DetectorConfig):
        self.db = db
        self.config = config

    def evaluate(self, product: Product, current_price: float, run_id: str) -> AlertDecision:
        min_threshold = product.rules.min_price_threshold if product.rules.min_price_threshold is not None else self.config.min_price_threshold
        max_threshold = product.rules.max_price_threshold if product.rules.max_price_threshold is not None else self.config.max_price_threshold
        if min_threshold is not None and current_price < min_threshold:
            return AlertDecision(False, "outlier_below_min", current_price=current_price)
        if max_threshold is not None and current_price > max_threshold:
            return AlertDecision(False, "outlier_above_max", current_price=current_price)

        mode = self.config.reference_mode
        ref_price = self.db.get_reference_price(product.id, self.config.lookback_days, mode)
        if ref_price is None:
            return AlertDecision(False, "no_reference", current_price=current_price)

        drop_amount = max(0.0, ref_price - current_price)
        drop_percent = (drop_amount / ref_price * 100) if ref_price > 0 else 0.0

        percent_min = product.rules.drop_percent_min if product.rules.drop_percent_min is not None else self.config.drop_percent_min
        amount_min = product.rules.drop_amount_min if product.rules.drop_amount_min is not None else self.config.amount_min_for(product.currency)

        threshold_hit = drop_percent >= percent_min or drop_amount >= amount_min
        if not threshold_hit:
            return AlertDecision(False, "threshold_not_met", drop_percent=drop_percent, drop_amount=drop_amount, reference_price=ref_price, current_price=current_price)

        cooldown_hours = product.rules.cooldown_hours if product.rules.cooldown_hours is not None else self.config.cooldown_hours
        last_alert = self.db.get_last_alert(product.id)
        bypassed = False
        if last_alert:
            last_time = datetime.fromisoformat(last_alert["timestamp_utc"])
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            delta = datetime.now(tz=timezone.utc) - last_time
            if delta < timedelta(hours=cooldown_hours):
                prev_drop = float(last_alert["drop_percent"] or 0.0)
                if drop_percent < prev_drop + self.config.realert_extra_drop_percent:
                    return AlertDecision(False, "cooldown", drop_percent=drop_percent, drop_amount=drop_amount, reference_price=ref_price, current_price=current_price)
                bypassed = True

        lowest = self.db.get_lowest_price(product.id, self.config.lookback_days)
        self.db.insert_alert(product.id, current_price, ref_price, drop_percent, drop_amount, mode, run_id)
        return AlertDecision(
            True,
            "alert",
            drop_percent=drop_percent,
            drop_amount=drop_amount,
            reference_price=ref_price,
            reference_label=mode,
            current_price=current_price,
            lowest_lookback=lowest,
            bypassed_cooldown=bypassed,
        )
