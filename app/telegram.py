from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from urllib import request

from app.models import AlertDecision, Product

LOGGER = logging.getLogger(__name__)
TELEGRAM_MAX = 4000


def render_alert_line(product: Product, decision: AlertDecision) -> str:
    emoji = "📉"
    title = product.title_hint or product.id
    return (
        f"{emoji} *{title}*\n"
        f"Atual: {decision.current_price:.2f} {product.currency} | "
        f"Queda: {decision.drop_percent:.1f}% ({decision.drop_amount:.2f} {product.currency})\n"
        f"Ref: {decision.reference_label} ({decision.reference_price:.2f} {product.currency})\n"
        f"{product.url}"
    )


class TelegramNotifier:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    def send_alerts(self, payloads: list[tuple[Product, AlertDecision]]) -> int:
        if not payloads:
            return 0
        messages = self._batch_messages(payloads)
        sent = 0
        for msg in messages:
            if self.dry_run:
                LOGGER.info("[DRY] telegram: %s", msg)
                sent += 1
                continue
            if not self.token or not self.chat_id:
                LOGGER.warning("telegram vars missing; skipping")
                return sent
            self._send(msg)
            sent += 1
        return sent

    def _batch_messages(self, payloads: list[tuple[Product, AlertDecision]]) -> list[str]:
        chunks: list[str] = []
        current = ""
        count = 0
        for product, decision in payloads:
            block = render_alert_line(product, decision)
            block += f"\nUTC: {datetime.now(tz=timezone.utc).isoformat()}"
            candidate = f"{current}\n\n{block}" if current else block
            if len(candidate) > TELEGRAM_MAX or count >= 10:
                if current:
                    chunks.append(current)
                current = block
                count = 1
            else:
                current = candidate
                count += 1
        if current:
            chunks.append(current)
        return chunks

    def _send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        req = request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with request.urlopen(req, timeout=12) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"telegram error {resp.status}")
