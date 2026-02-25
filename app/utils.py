from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


from app.models import ItemRules, Product

LOGGER = logging.getLogger(__name__)
PRICE_CLEAN_RE = re.compile(r"[^\d,\.\-]")


class DomainRateLimiter:
    def __init__(self, min_interval_seconds: float = 2.0):
        self.min_interval_seconds = min_interval_seconds
        self._last_call_by_domain: dict[str, float] = {}

    def wait(self, domain: str) -> None:
        now = time.time()
        last = self._last_call_by_domain.get(domain)
        if last is None:
            self._last_call_by_domain[domain] = now
            return
        elapsed = now - last
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_call_by_domain[domain] = time.time()


class DomainCircuitBreaker:
    def __init__(self, threshold: int = 3, cooloff_seconds: int = 600):
        self.threshold = threshold
        self.cooloff_seconds = cooloff_seconds
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def record_failure(self, domain: str) -> None:
        q = self.failures[domain]
        q.append(time.time())
        while len(q) > self.threshold:
            q.popleft()

    def record_success(self, domain: str) -> None:
        self.failures.pop(domain, None)

    def is_open(self, domain: str) -> bool:
        q = self.failures.get(domain)
        if not q or len(q) < self.threshold:
            return False
        return time.time() - q[-1] < self.cooloff_seconds


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def setup_logging(debug: bool = False) -> Path:
    ts = utc_now().strftime("%Y%m%d_%H%M%S")
    log_path = Path("logs") / f"run_{ts}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if debug else logging.INFO
    handlers = [logging.StreamHandler(), logging.FileHandler(log_path)]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path


def parse_price(price_text: str) -> float | None:
    if not price_text:
        return None
    clean = PRICE_CLEAN_RE.sub("", price_text.strip())
    if not clean:
        return None

    comma_count = clean.count(",")
    dot_count = clean.count(".")

    if comma_count and dot_count:
        if clean.rfind(",") > clean.rfind("."):
            clean = clean.replace(".", "").replace(",", ".")
        else:
            clean = clean.replace(",", "")
    elif comma_count > 1 and dot_count == 0:
        clean = clean.replace(",", "")
    elif dot_count > 1 and comma_count == 0:
        clean = clean.replace(".", "")
    elif comma_count == 1 and dot_count == 0:
        clean = clean.replace(",", ".")

    try:
        return round(float(clean), 2)
    except ValueError:
        return None


def retry_with_backoff(
    fn: Callable[[], Any],
    retries: int = 3,
    base_delay: float = 0.7,
    allowed_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Any:
    for i in range(retries):
        try:
            return fn()
        except allowed_exceptions as exc:
            if i == retries - 1:
                raise
            sleep_s = base_delay * (2**i)
            LOGGER.warning("retrying after error: %s", exc)
            time.sleep(sleep_s)


def load_yaml(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(raw) or {}
    except ModuleNotFoundError:
        return json.loads(raw)


def load_watchlist(path: str | Path) -> list[Product]:
    data = load_yaml(path)
    items = data.get("products", [])
    products: list[Product] = []

    for row in items:
        rules = row.get("rules") or {}
        product = Product(
            id=str(row["id"]),
            country=str(row["country"]).upper(),
            store=str(row["store"]).lower(),
            url=str(row["url"]),
            currency=str(row["currency"]).upper(),
            title_hint=row.get("title_hint"),
            tags=list(row.get("tags") or []),
            rules=ItemRules(**rules),
        )
        products.append(product)
    return products


def dump_json(path: str | Path, payload: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def filter_products(
    products: Iterable[Product],
    only_store: str | None = None,
    only_country: str | None = None,
    only_tags: set[str] | None = None,
    limit: int | None = None,
) -> list[Product]:
    selected: list[Product] = []
    for p in products:
        if only_store and p.store != only_store.lower():
            continue
        if only_country and p.country != only_country.upper():
            continue
        if only_tags and not (only_tags.intersection(set(p.tags))):
            continue
        selected.append(p)
    if limit is not None:
        return selected[:limit]
    return selected
