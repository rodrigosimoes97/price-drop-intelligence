from pathlib import Path

from app.db import Database
from app.detector import DetectorConfig, PriceDropDetector
from app.models import Product


def build_product() -> Product:
    return Product(
        id="p1",
        country="BR",
        store="generic",
        url="https://example.com",
        currency="BRL",
        tags=["x"],
    )


def test_detector_alert_and_cooldown(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    db.init()
    p = build_product()
    db.upsert_product(p)

    # seed past reference price
    from app.models import PriceSnapshot
    from app.utils import utc_now

    db.insert_snapshot(
        PriceSnapshot(
            product_id="p1",
            timestamp_utc=utc_now(),
            price=200,
            currency="BRL",
            in_stock=True,
            title="x",
            source="generic",
        )
    )

    det = PriceDropDetector(db, DetectorConfig(drop_percent_min=10, drop_amount_min_by_currency={"BRL": 20}, cooldown_hours=24))
    d1 = det.evaluate(p, current_price=150, run_id="r1")
    assert d1.should_alert is True

    d2 = det.evaluate(p, current_price=149, run_id="r1")
    assert d2.should_alert is False
    assert d2.reason == "cooldown"

    db.close()


def test_detector_outlier(tmp_path: Path):
    db = Database(tmp_path / "t2.db")
    db.init()
    p = build_product()
    db.upsert_product(p)

    from app.models import PriceSnapshot
    from app.utils import utc_now

    db.insert_snapshot(
        PriceSnapshot(
            product_id="p1",
            timestamp_utc=utc_now(),
            price=300,
            currency="BRL",
            in_stock=True,
            title="x",
            source="generic",
        )
    )

    det = PriceDropDetector(db, DetectorConfig(min_price_threshold=100))
    d = det.evaluate(p, current_price=20, run_id="r2")
    assert d.should_alert is False
    assert d.reason == "outlier_below_min"
    db.close()
