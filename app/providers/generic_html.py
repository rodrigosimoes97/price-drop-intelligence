from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timezone
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.models import PriceSnapshot, Product
from app.providers.base import PriceProvider
from app.utils import DomainCircuitBreaker, DomainRateLimiter, parse_price, retry_with_backoff, utc_now

LOGGER = logging.getLogger(__name__)
BLOCK_MARKERS = ["captcha", "access denied", "robot", "unusual traffic", "verify you are human"]


@dataclass
class FetchConfig:
    timeout_seconds: int = 15
    user_agent: str = "price-drop-intelligence/1.0 (+https://github.com/)"


class GenericHTMLProvider(PriceProvider):
    name = "generic_html"

    def __init__(self, config: FetchConfig | None = None):
        self.config = config or FetchConfig()
        self.rate_limiter = DomainRateLimiter(min_interval_seconds=2)
        self.breaker = DomainCircuitBreaker(threshold=3, cooloff_seconds=600)

    def fetch_product(self, product: Product) -> PriceSnapshot:
        domain = urlparse(product.url).netloc
        now = utc_now().astimezone(timezone.utc)
        if self.breaker.is_open(domain):
            return PriceSnapshot(product.id, now, None, product.currency, None, product.title_hint, self.name, status="error", error="circuit_open", raw={"domain": domain})

        self.rate_limiter.wait(domain)

        def do_request() -> tuple[int, str]:
            req = Request(product.url, headers={"User-Agent": self.config.user_agent, "Accept-Language": "en-US,en;q=0.9"})
            with urlopen(req, timeout=self.config.timeout_seconds) as resp:
                return resp.status, resp.read().decode("utf-8", errors="ignore")

        try:
            status_code, html = retry_with_backoff(do_request, retries=3, base_delay=0.6)
            lower = html[:8000].lower()
            if any(marker in lower for marker in BLOCK_MARKERS):
                self.breaker.record_failure(domain)
                return PriceSnapshot(product.id, now, None, product.currency, None, product.title_hint, self.name, status="error", error="blocked", raw={"status_code": status_code})
            snapshot = self._parse_html(product, html, status_code)
            (self.breaker.record_success if snapshot.status == "ok" else self.breaker.record_failure)(domain)
            return snapshot
        except Exception as exc:
            self.breaker.record_failure(domain)
            return PriceSnapshot(product.id, now, None, product.currency, None, product.title_hint, self.name, status="error", error=f"request_error:{type(exc).__name__}", raw={"domain": domain})

    def _parse_html(self, product: Product, html: str, status_code: int) -> PriceSnapshot:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = (title_match.group(1).strip()[:180] if title_match else product.title_hint)
        currency = self._extract_currency(html) or product.currency
        price = self._extract_price(html)
        in_stock = self._extract_stock(html)
        now = utc_now()
        if price is None:
            return PriceSnapshot(product.id, now, None, currency, in_stock, title, self.name, status="error", error="price_not_found", raw={"status_code": status_code})
        return PriceSnapshot(product.id, now, price, currency, in_stock, title, self.name, raw={"status_code": status_code})

    @staticmethod
    def _extract_currency(html: str) -> str | None:
        for pat in [r'priceCurrency"\s*:\s*"([A-Z]{3})"', r'product:price:currency" content="([A-Z]{3})"']:
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _extract_stock(html: str) -> bool | None:
        text = html.lower()
        if "out of stock" in text or "esgotado" in text:
            return False
        if "in stock" in text or "disponível" in text:
            return True
        return None

    def _extract_price(self, html: str) -> float | None:
        for pat in [
            r'product:price:amount" content="([\d\.,]+)"',
            r'itemprop="price" content="([\d\.,]+)"',
            r'"price"\s*:\s*"([\d\.,]+)"',
            r'R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}',
            r'\$\s?\d{1,3}(?:,\d{3})*\.\d{2}',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                val = m.group(1) if m.groups() else m.group(0)
                p = parse_price(val)
                if p is not None:
                    return p

        for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            p = self._extract_price_from_jsonld(payload)
            if p is not None:
                return p
        return None

    def _extract_price_from_jsonld(self, payload: Any) -> float | None:
        if isinstance(payload, list):
            for item in payload:
                found = self._extract_price_from_jsonld(item)
                if found is not None:
                    return found
        elif isinstance(payload, dict):
            for key in ("price", "lowPrice", "highPrice"):
                if key in payload:
                    p = parse_price(str(payload[key]))
                    if p is not None:
                        return p
            if "offers" in payload:
                return self._extract_price_from_jsonld(payload["offers"])
        return None
