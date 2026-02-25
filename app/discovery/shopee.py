from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote_plus, urlencode, urlparse, urlunparse

from app.discovery.base import DiscoveryProvider
from app.models import DiscoveredProduct
from app.utils import DomainCircuitBreaker, DomainRateLimiter, parse_price, retry_with_backoff, utc_now

LOGGER = logging.getLogger(__name__)

try:
    import requests  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    requests = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    BeautifulSoup = None

BLOCK_MARKERS = ["captcha", "access denied", "robot", "verify you are human", "unusual traffic"]
SHOPEE_SEARCH_API = "https://shopee.com.br/api/v4/search/search_items"


class ShopeeApiError(RuntimeError):
    pass


@dataclass(slots=True)
class DiscoveryCategory:
    name: str
    url: str
    tags: list[str]
    take: int


@dataclass(slots=True)
class ShopeeDiscoveryConfig:
    enabled: bool = True
    country: str = "BR"
    currency: str = "BRL"
    max_active_products: int = 100
    ttl_hours: int = 72
    categories: list[DiscoveryCategory] | None = None
    filters: dict[str, Any] | None = None
    http: dict[str, Any] | None = None
    affiliate: dict[str, Any] | None = None


def normalize_shopee_url(url: str) -> tuple[str, str | None, str | None]:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().replace("www.", "")
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "smtt", "sp_atk", "xptdk"}
    ]
    clean_query = urlencode(query)

    shop_id = None
    item_id = None
    path = parsed.path
    match = re.search(r"(?:-i\.|/product/)(\d+)[\./](\d+)", path)
    if match:
        shop_id, item_id = match.group(1), match.group(2)
    else:
        qs = dict(parse_qsl(parsed.query))
        shop_id = qs.get("shopId") or qs.get("shopid")
        item_id = qs.get("itemId") or qs.get("itemid")

    canonical = urlunparse((parsed.scheme or "https", netloc, path, "", clean_query, ""))
    return canonical, shop_id, item_id


def product_id_from(country: str, shop_id: str | None, item_id: str | None, canonical_url: str) -> str:
    if shop_id and item_id:
        return f"shopee:{country.upper()}:{shop_id}:{item_id}"
    digest = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:16]
    return f"shopee:{country.upper()}:url:{digest}"


def score_product(p: DiscoveredProduct) -> float:
    sold = max(0, int(p.sold or 0))
    rating = float(p.rating or 4.0)
    rating_count = max(0, int(p.rating_count or 0))
    source_bonus = 1.2 if "bestsellers" in p.source else 0.0
    return round(math.log1p(sold) * 2 + rating * 1.5 + math.log1p(rating_count) + source_bonus, 4)


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    s = re.sub(r"[^\d]", "", str(value))
    return int(s) if s else None


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _parse_sold(value: Any) -> int | None:
    if value is None:
        return None
    txt = str(value).strip().lower().replace("+", "")
    m = re.search(r"(\d+[\.,]?\d*)\s*k", txt)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1000)
    return _parse_int(txt)


def shopee_price_to_float(raw_price: Any) -> float | None:
    if raw_price is None:
        return None
    try:
        val = int(raw_price)
    except (TypeError, ValueError):
        return parse_price(str(raw_price))
    return round(val / 100000.0, 2)


def passes_filters(item: DiscoveredProduct, filters: dict[str, Any]) -> bool:
    title = item.title.lower()
    excludes = [k.lower() for k in filters.get("exclude_keywords", [])]
    if any(kw in title for kw in excludes):
        return False

    if item.price is not None:
        if item.price < float(filters.get("min_price", 0)):
            return False
        max_price = filters.get("max_price")
        if max_price is not None and item.price > float(max_price):
            return False

    min_sold = filters.get("min_sold")
    if min_sold is not None and (item.sold or 0) < int(min_sold):
        return False

    min_rating = filters.get("min_rating")
    min_rating_count = filters.get("min_rating_count")
    if min_rating is not None and min_rating_count is not None:
        if (item.rating or 0) < float(min_rating) and (item.rating_count or 0) >= int(min_rating_count):
            return False

    return True


class ShopeeDiscoveryProvider(DiscoveryProvider):
    name = "shopee"

    def __init__(self, config: ShopeeDiscoveryConfig, only_category: str | None = None):
        self.config = config
        self.only_category = only_category
        self.rate_limiter = DomainRateLimiter(min_interval_seconds=float((config.http or {}).get("rate_limit_per_domain_seconds", 2.0)))
        self.breaker = DomainCircuitBreaker(threshold=3, cooloff_seconds=600)
        self.timeout = int((config.http or {}).get("timeout_seconds", 15))
        self.retries = int((config.http or {}).get("retries", 3))
        self.backoff = float((config.http or {}).get("backoff_base_seconds", 1.2))
        self.ua = str(
            (config.http or {}).get(
                "user_agent",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            )
        )
        self.session = requests.Session() if requests else None
        if self.session:
            self.session.headers.update(
                {
                    "User-Agent": self.ua,
                    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
            )

    def discover(self) -> tuple[list[DiscoveredProduct], dict[str, int], list[str]]:
        discovered: list[DiscoveredProduct] = []
        strategy_stats = {"api_search": 0, "json_state": 0, "json_ld": 0, "html": 0}
        errors: list[str] = []

        for cat in self.config.categories or []:
            if self.only_category and cat.name != self.only_category:
                continue
            try:
                cat_items, cat_stats, cat_errors = self._discover_category(cat)
                for k, v in cat_stats.items():
                    strategy_stats[k] = strategy_stats.get(k, 0) + v
                errors.extend(cat_errors)
                discovered.extend(cat_items[: cat.take])
                LOGGER.info("category=%s collected=%s kept=%s", cat.name, len(cat_items), min(len(cat_items), cat.take))
            except Exception as exc:  # noqa: BLE001
                msg = f"category={cat.name} network_error:{type(exc).__name__}"
                LOGGER.exception(msg)
                errors.append(msg)

        deduped = self._dedupe(discovered)
        filtered = [p for p in deduped if passes_filters(p, self.config.filters or {})]
        for p in filtered:
            p.score = score_product(p)
        filtered.sort(key=lambda x: x.score, reverse=True)
        return filtered[: self.config.max_active_products], strategy_stats, errors

    def _extract_keyword(self, url: str) -> str | None:
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        keyword = (q.get("keyword") or q.get("q") or [None])[0]
        if not keyword:
            return None
        return keyword.strip() or None

    def _fetch_html(self, url: str) -> tuple[str, str]:
        domain = urlparse(url).netloc or "shopee.com.br"
        if self.breaker.is_open(domain):
            raise RuntimeError("circuit_open")
        self.rate_limiter.wait(domain)

        def _do() -> tuple[str, str]:
            if self.session:
                res = self.session.get(url, timeout=self.timeout)
                body = res.text or ""
                if res.status_code != 200:
                    raise RuntimeError(f"html_status:{res.status_code}")
                return body, f"status={res.status_code}"
            from urllib.request import Request, urlopen

            req = Request(url, headers={"User-Agent": self.ua, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                return body, f"status={resp.status}"

        try:
            html, meta = retry_with_backoff(_do, retries=self.retries, base_delay=self.backoff)
            self.breaker.record_success(domain)
            LOGGER.info("fetch_html url=%s %s bytes=%s", url, meta, len(html.encode("utf-8", errors="ignore")))
            return html, meta
        except Exception:
            self.breaker.record_failure(domain)
            raise

    def _fetch_search_api(self, keyword: str, limit: int, newest: int = 0) -> dict[str, Any]:
        if self.session is None:
            raise ShopeeApiError("requests_missing")

        domain = "shopee.com.br"
        if self.breaker.is_open(domain):
            raise ShopeeApiError("circuit_open")
        self.rate_limiter.wait(domain)

        params = {
            "by": "sales",
            "keyword": keyword,
            "limit": str(limit),
            "newest": str(newest),
            "order": "desc",
            "page_type": "search",
            "scenario": "PAGE_GLOBAL_SEARCH",
            "version": "2",
        }

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Referer": f"https://shopee.com.br/search?keyword={quote_plus(keyword)}",
            "User-Agent": self.ua,
        }

        def _do() -> Any:
            return self.session.get(SHOPEE_SEARCH_API, params=params, timeout=self.timeout, headers=headers)

        try:
            res = retry_with_backoff(_do, retries=self.retries, base_delay=self.backoff)
            LOGGER.info("api_search keyword=%s status=%s", keyword, res.status_code)
            if res.status_code != 200:
                snippet = (res.text or "")[:300].replace("\n", " ")
                raise ShopeeApiError(f"api_error:{res.status_code} body={snippet}")
            self.breaker.record_success(domain)
            return res.json() if res.text else {}
        except ShopeeApiError:
            self.breaker.record_failure(domain)
            raise
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure(domain)
            raise ShopeeApiError(f"api_error:{type(exc).__name__}") from exc

    def _parse_search_api_items(self, data: dict[str, Any], category: DiscoveryCategory, now: datetime, expires: datetime) -> list[DiscoveredProduct]:
        out: list[DiscoveredProduct] = []
        for row in data.get("items") or []:
            basic = row.get("item_basic") or {}
            shop_id = str(basic.get("shopid") or "") or None
            item_id = str(basic.get("itemid") or "") or None
            if not shop_id or not item_id:
                continue
            title = str(basic.get("name") or "").strip()
            if not title:
                continue
            canonical_seed = f"https://shopee.com.br/product/{shop_id}/{item_id}"
            canonical_url, norm_shop, norm_item = normalize_shopee_url(canonical_seed)
            pid = product_id_from(self.config.country, norm_shop or shop_id, norm_item or item_id, canonical_url)

            rating_obj = basic.get("item_rating") or {}
            rating_count = rating_obj.get("rating_count")
            rating_count_value = None
            if isinstance(rating_count, list):
                rating_count_value = int(sum(int(x or 0) for x in rating_count))
            else:
                rating_count_value = _parse_int(rating_count)

            out.append(
                DiscoveredProduct(
                    id=pid,
                    store="shopee",
                    country=self.config.country,
                    currency=self.config.currency,
                    canonical_url=canonical_url,
                    url=canonical_url,
                    title=title[:180],
                    tags=list(dict.fromkeys([*category.tags, "bestseller", category.name])),
                    source=f"bestsellers:{category.name}",
                    discovered_at_utc=now,
                    expires_at_utc=expires,
                    score=0.0,
                    price=shopee_price_to_float(basic.get("price")),
                    image_url=(f"https://cf.shopee.com.br/file/{basic.get('image')}" if basic.get("image") else None),
                    shop_id=shop_id,
                    item_id=item_id,
                    sold=_parse_int(basic.get("historical_sold") or basic.get("sold")),
                    rating=_parse_float(rating_obj.get("rating_star")),
                    rating_count=rating_count_value,
                )
            )
        LOGGER.info("api_search parsed_items=%s", len(out))
        return out

    def _discover_category(self, category: DiscoveryCategory) -> tuple[list[DiscoveredProduct], dict[str, int], list[str]]:
        now = utc_now()
        expires = now + timedelta(hours=self.config.ttl_hours)
        stats = {"api_search": 0, "json_state": 0, "json_ld": 0, "html": 0}
        errors: list[str] = []

        keyword = self._extract_keyword(category.url)
        if keyword:
            try:
                data = self._fetch_search_api(keyword=keyword, limit=max(60, category.take))
                api_items = self._parse_search_api_items(data, category, now, expires)
                stats["api_search"] = len(api_items)
                if api_items:
                    return api_items, stats, errors
                errors.append(f"category={category.name} api_empty")
            except ShopeeApiError as exc:
                errors.append(f"category={category.name} {exc}")

        html = ""
        try:
            html, _meta = self._fetch_html(category.url)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"category={category.name} network_error:{type(exc).__name__}")
            return [], stats, errors

        lower = html[:8000].lower()
        if any(marker in lower for marker in BLOCK_MARKERS):
            errors.append(f"category={category.name} blocked_detected")
            return [], stats, errors

        state_items = self._parse_json_state(html, category, now, expires)
        stats["json_state"] = len(state_items)
        if state_items:
            return state_items, stats, errors

        ld_items = self._parse_json_ld(html, category, now, expires)
        stats["json_ld"] = len(ld_items)
        if ld_items:
            return ld_items, stats, errors

        html_items = self._parse_html_cards(html, category, now, expires)
        stats["html"] = len(html_items)
        if html_items:
            return html_items, stats, errors

        errors.append(f"category={category.name} html_empty")
        LOGGER.warning(
            "discovery_empty url=%s keyword=%s html_bytes=%s strategy_counts=%s hint=%s",
            category.url,
            keyword,
            len(html.encode("utf-8", errors="ignore")),
            stats,
            "HTML likely JS-rendered; enable API search or check blocking",
        )
        return [], stats, errors

    def _parse_json_state(self, html: str, category: DiscoveryCategory, now: datetime, expires: datetime) -> list[DiscoveredProduct]:
        out: list[DiscoveredProduct] = []
        candidates = re.findall(r"<script[^>]*>(\{.*?\})</script>", html, flags=re.DOTALL)
        for raw in candidates:
            if "itemid" not in raw.lower() and "shopid" not in raw.lower() and "price" not in raw.lower():
                continue
            try:
                payload = json.loads(unescape(raw))
            except json.JSONDecodeError:
                continue
            for item in self._walk_dicts(payload):
                prod = self._build_from_mapping(item, category, now, expires)
                if prod:
                    out.append(prod)
        return out

    def _parse_json_ld(self, html: str, category: DiscoveryCategory, now: datetime, expires: datetime) -> list[DiscoveredProduct]:
        out: list[DiscoveredProduct] = []
        for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, flags=re.DOTALL | re.IGNORECASE):
            try:
                payload = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            for d in self._walk_dicts(payload):
                if str(d.get("@type", "")).lower() not in {"product", "offer"} and "offers" not in d:
                    continue
                prod = self._build_from_mapping(d, category, now, expires)
                if prod:
                    out.append(prod)
        return out

    def _parse_html_cards(self, html: str, category: DiscoveryCategory, now: datetime, expires: datetime) -> list[DiscoveredProduct]:
        out: list[DiscoveredProduct] = []
        if BeautifulSoup is None:
            for link, title in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL):
                if "shopee" not in link:
                    continue
                clean_title = re.sub(r"<[^>]+>", "", title).strip()
                if not clean_title:
                    continue
                canonical, shop_id, item_id = normalize_shopee_url(link)
                out.append(
                    DiscoveredProduct(
                        id=product_id_from(self.config.country, shop_id, item_id, canonical),
                        store="shopee",
                        country=self.config.country,
                        currency=self.config.currency,
                        canonical_url=canonical,
                        url=canonical,
                        title=clean_title[:180],
                        tags=list(dict.fromkeys([*category.tags, "bestseller", category.name])),
                        source=f"bestsellers:{category.name}",
                        discovered_at_utc=now,
                        expires_at_utc=expires,
                        score=0.0,
                        shop_id=shop_id,
                        item_id=item_id,
                    )
                )
            return out

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href*='shopee.com.br'], a[href*='shopee.com']"):
            href = a.get("href")
            if not href:
                continue
            title = (a.get_text(" ", strip=True) or a.get("title") or "").strip()
            if len(title) < 6:
                continue
            card_text = a.parent.get_text(" ", strip=True) if a.parent else title
            canonical, shop_id, item_id = normalize_shopee_url(href)
            out.append(
                DiscoveredProduct(
                    id=product_id_from(self.config.country, shop_id, item_id, canonical),
                    store="shopee",
                    country=self.config.country,
                    currency=self.config.currency,
                    canonical_url=canonical,
                    url=canonical,
                    title=title[:180],
                    tags=list(dict.fromkeys([*category.tags, "bestseller", category.name])),
                    source=f"bestsellers:{category.name}",
                    discovered_at_utc=now,
                    expires_at_utc=expires,
                    score=0.0,
                    price=parse_price(card_text),
                    shop_id=shop_id,
                    item_id=item_id,
                )
            )
        return out

    def _build_from_mapping(self, item: dict[str, Any], category: DiscoveryCategory, now: datetime, expires: datetime) -> DiscoveredProduct | None:
        title = str(item.get("name") or item.get("title") or item.get("item_name") or "").strip()
        raw_url = str(item.get("url") or item.get("item_url") or item.get("product_url") or "").strip()
        if not title or not raw_url:
            return None
        canonical, shop_id, item_id = normalize_shopee_url(raw_url)
        pid = product_id_from(self.config.country, shop_id or (str(item.get("shopid")) if item.get("shopid") else None), item_id or (str(item.get("itemid")) if item.get("itemid") else None), canonical)

        sold = _parse_sold(item.get("sold") or item.get("historical_sold") or item.get("sold_count"))
        rating = _parse_float(item.get("rating") or item.get("item_rating") or item.get("rating_star"))
        rating_count = _parse_int(item.get("rating_count") or item.get("cmt_count") or item.get("review_count"))

        return DiscoveredProduct(
            id=pid,
            store="shopee",
            country=self.config.country,
            currency=self.config.currency,
            canonical_url=canonical,
            url=canonical,
            title=title[:180],
            tags=list(dict.fromkeys([*category.tags, "bestseller", category.name])),
            source=f"bestsellers:{category.name}",
            discovered_at_utc=now,
            expires_at_utc=expires,
            score=0.0,
            price=parse_price(str(item.get("price") or item.get("price_min") or item.get("display_price") or "")),
            image_url=str(item.get("image") or item.get("image_url") or "") or None,
            shop_id=shop_id,
            item_id=item_id,
            sold=sold,
            rating=rating,
            rating_count=rating_count,
        )

    def _walk_dicts(self, payload: Any):
        if isinstance(payload, dict):
            yield payload
            for value in payload.values():
                yield from self._walk_dicts(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from self._walk_dicts(item)

    @staticmethod
    def _dedupe(items: list[DiscoveredProduct]) -> list[DiscoveredProduct]:
        by_id: dict[str, DiscoveredProduct] = {}
        for item in items:
            existing = by_id.get(item.id)
            if existing is None or (item.sold or 0) > (existing.sold or 0):
                by_id[item.id] = item
        return list(by_id.values())
