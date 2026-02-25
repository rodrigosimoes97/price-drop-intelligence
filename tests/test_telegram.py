from app.models import AlertDecision, Product
from app.telegram import TelegramNotifier, render_alert_line


def test_render_alert_line():
    p = Product(id="p1", country="BR", store="x", url="https://x", currency="BRL", title_hint="Produto")
    d = AlertDecision(
        should_alert=True,
        reason="alert",
        drop_percent=15,
        drop_amount=30,
        reference_price=200,
        reference_label="rolling_max",
        current_price=170,
    )
    text = render_alert_line(p, d)
    assert "Produto" in text
    assert "15.0%" in text


def test_batch_messages_limit():
    p = Product(id="p1", country="BR", store="x", url="https://x", currency="BRL", title_hint="Produto")
    d = AlertDecision(True, "alert", 15, 30, 200, "rolling_max", 170)
    notifier = TelegramNotifier(dry_run=True)
    msgs = notifier._batch_messages([(p, d) for _ in range(12)])
    assert len(msgs) >= 2
