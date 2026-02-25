from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

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

from urllib.parse import parse_qs

_SHOPEE_PRICE_DIVISOR = 100000  # Shopee costuma retornar preço nesse formato

def _extract_keyword(url: str) -> str | None:
    try:
        qs = parse_qs(urlparse(url).query)
        kw = qs.get("keyword", [None])[0]
        return kw.strip() if kw and str(kw).strip() else None
    except Exception:
        return None

def _safe_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        return 0

def _safe_float(x: Any) -> float | None:
    try:
        return float(x)
    except Exception:
        return None


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
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "smtt", "sp_atk", "xptdk"}]
    clean_query = urlencode(query)

    shop_id = None
    item_id = None
    path = parsed.path
    match = re.search(r"-i\.(\d+)\.(\d+)", path)
    if match:
        shop_id, item_id = match.group(1), match.group(2)
        path = re.sub(r"\?.*$", "", path)
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
        self.ua = str((config.http or {}).get("user_agent", "price-drop-intelligence-bot/1.0"))

    def discover(self) -> tuple[list[DiscoveredProduct], dict[str, int], list[str]]:
        discovered: list[DiscoveredProduct] = []
        strategy_stats = {"json_state": 0, "json_ld": 0, "html": 0, "api_search": 0}
        errors: list[str] = []

        categories = self.config.categories or []
        for cat in categories:
            if self.only_category and cat.name != self.only_category:
                continue
            try:
                cat_items, cat_stats = self._discover_category(cat)
                for k, v in cat_stats.items():
                    strategy_stats[k] += v
                discovered.extend(cat_items[: cat.take])
                LOGGER.info("category=%s collected=%s kept=%s", cat.name, len(cat_items), len(cat_items[: cat.take]))
            except Exception as exc:  # noqa: BLE001
                msg = f"category={cat.name} error={type(exc).__name__}"
                LOGGER.exception(msg)
                errors.append(msg)

        deduped = self._dedupe(discovered)
        filtered = [p for p in deduped if passes_filters(p, self.config.filters or {})]
        for p in filtered:
            p.score = score_product(p)
        filtered.sort(key=lambda x: x.score, reverse=True)
        limited = filtered[: self.config.max_active_products]
        return limited, strategy_stats, errors

    def _fetch(self, url: str) -> str:
        domain = urlparse(url).netloc
        if self.breaker.is_open(domain):
            raise RuntimeError("circuit_open")
        self.rate_limiter.wait(domain)

        def do_req() -> str:
            if requests is not None:
                res = requests.get(url, timeout=self.timeout, headers={"User-Agent": self.ua})
                res.raise_for_status()
                return res.text
            req = Request(url, headers={"User-Agent": self.ua})
            with urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")

        try:
            body = retry_with_backoff(do_req, retries=self.retries, base_delay=self.backoff)
            self.breaker.record_success(domain)
            return body
        except Exception:
            self.breaker.record_failure(domain)
            raise

        def _fetch_search_api(self, keyword: str, limit: int = 60, newest: int = 0) -> dict[str, Any]:
            """
            Busca itens via endpoint JSON usado pelo front do Shopee Search.
            """
            api = "https://shopee.com.br/api/v4/search/search_items"
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
                "Referer": f"https://shopee.com.br/search?keyword={keyword}",
                # use UA "real" aqui — Shopee bloqueia UA muito “bot”
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
            }
    
            # Reusa seu rate limit + breaker
            domain = "shopee.com.br"
            if self.breaker.is_open(domain):
                raise RuntimeError("circuit_open")
            self.rate_limiter.wait(domain)
    
            def do_req() -> dict[str, Any]:
                if requests is None:
                    raise RuntimeError("requests_required_for_api")
                res = requests.get(api, params=params, timeout=self.timeout, headers=headers)
                res.raise_for_status()
                return res.json()
    
            try:
                data = retry_with_backoff(do_req, retries=self.retries, base_delay=self.backoff)
                self.breaker.record_success(domain)
                return data
            except Exception:
                self.breaker.record_failure(domain)
                raise

    def _parse_search_api_items(self, data: dict[str, Any], category: DiscoveryCategory, now, expires) -> list[DiscoveredProduct]:
        out: list[DiscoveredProduct] = []
        items = data.get("items") or []
        for it in items:
            basic = (it or {}).get("item_basic") or {}
            if not basic:
                continue

            shop_id = str(basic.get("shopid") or "") or None
            item_id = str(basic.get("itemid") or "") or None
            title = str(basic.get("name") or "").strip()
            if not title:
                continue

            # preço
            raw_price = basic.get("price") or basic.get("price_min") or basic.get("price_max")
            price = None
            if isinstance(raw_price, (int, float)) and raw_price > 0:
                price = float(raw_price) / _SHOPEE_PRICE_DIVISOR

            # métricas
            sold = _safe_int(basic.get("historical_sold") or basic.get("sold") or 0)
            rating_block = basic.get("item_rating") or {}
            rating = _safe_float(rating_block.get("rating_star")) if isinstance(rating_block, dict) else None
            rating_count = _safe_int(rating_block.get("rating_count") or 0) if isinstance(rating_block, dict) else 0

            # url canônica (forma estável)
            canonical = None
            if shop_id and item_id:
                canonical = f"https://shopee.com.br/product/{shop_id}/{item_id}"
            else:
                canonical, shop_id2, item_id2 = normalize_shopee_url(category.url)
                shop_id = shop_id or shop_id2
                item_id = item_id or item_id2

            canonical, shop_idn, item_idn = normalize_shopee_url(canonical) if canonical else (category.url, shop_id, item_id)
            pid = product_id_from(self.config.country, shop_idn, item_idn, canonical)

            out.append(
                DiscoveredProduct(
                    id=pid,
                    store="shopee",
                    country=self.config.country,
                    currency=self.config.currency,
                    canonical_url=canonical,
                    url=canonical,
                    title=title[:180],
                    tags=list(dict.fromkeys([*category.tags, "bestseller", category.name])),
                    source=f"api_search:{category.name}",
                    discovered_at_utc=now,
                    expires_at_utc=expires,
                    score=0.0,
                    price=price,
                    shop_id=shop_idn,
                    item_id=item_idn,
                    sold=sold,
                    rating=rating,
                    rating_count=rating_count,
                )
            )
        return out

    def _discover_category(self, category: DiscoveryCategory) -> tuple[list[DiscoveredProduct], dict[str, int]]:
        html = self._fetch(category.url)
        now = utc_now()
        expires = now + timedelta(hours=self.config.ttl_hours)

        items, stats = self._parse_html_cards(html, category, now, expires)
        if items:
            return items, stats

        # ✅ Fallback para /search?keyword=... via API (JS-rendered pages)
        keyword = _extract_keyword(category.url)
        if keyword:
            data = self._fetch_search_api(keyword=keyword, limit=max(60, category.take))
            api_items = self._parse_search_api_items(data, category, now, expires)
            return api_items, {"json_state": 0, "json_ld": 0, "html": 0, "api_search": len(api_items)}

        return [], {"json_state": 0, "json_ld": 0, "html": 0}

    def _parse_json_state(self, html: str, category: DiscoveryCategory, now, expires) -> tuple[list[DiscoveredProduct], dict[str, int]]:
        candidates = re.findall(r"<script[^>]*>(\{.*?\})</script>", html, flags=re.DOTALL)
        out: list[DiscoveredProduct] = []
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
        return out, {"json_state": len(out), "json_ld": 0, "html": 0}

    def _parse_json_ld(self, html: str, category: DiscoveryCategory, now, expires) -> tuple[list[DiscoveredProduct], dict[str, int]]:
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
        return out, {"json_state": 0, "json_ld": len(out), "html": 0}

    def _parse_html_cards(self, html: str, category: DiscoveryCategory, now, expires) -> tuple[list[DiscoveredProduct], dict[str, int]]:
        out: list[DiscoveredProduct] = []
        if BeautifulSoup is None:
            for link, title in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL):
                if "shopee" not in link:
                    continue
                clean_title = re.sub(r"<[^>]+>", "", title).strip()
                if not clean_title:
                    continue
                canonical, shop_id, item_id = normalize_shopee_url(link)
                pid = product_id_from(self.config.country, shop_id, item_id, canonical)
                out.append(
                    DiscoveredProduct(
                        id=pid,
                        store="shopee",
                        country=self.config.country,
                        currency=self.config.currency,
                        canonical_url=canonical,
                        url=canonical,
                        title=clean_title,
                        tags=list(dict.fromkeys([*category.tags, "bestseller", category.name])),
                        source=f"bestsellers:{category.name}",
                        discovered_at_utc=now,
                        expires_at_utc=expires,
                        score=0.0,
                        shop_id=shop_id,
                        item_id=item_id,
                    )
                )
            return out, {"json_state": 0, "json_ld": 0, "html": len(out)}

        soup = BeautifulSoup(html, "html.parser")
        nodes = soup.select("a[href*='shopee.com.br'], a[href*='shopee.com']")
        for a in nodes:
            href = a.get("href")
            if not href:
                continue
            title = (a.get_text(" ", strip=True) or a.get("title") or "").strip()
            if len(title) < 6:
                continue
            card_text = a.parent.get_text(" ", strip=True) if a.parent else title
            price = parse_price(card_text)
            canonical, shop_id, item_id = normalize_shopee_url(href)
            pid = product_id_from(self.config.country, shop_id, item_id, canonical)
            out.append(
                DiscoveredProduct(
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
                    price=price,
                    shop_id=shop_id,
                    item_id=item_id,
                )
            )
        return out, {"json_state": 0, "json_ld": 0, "html": len(out)}

    def _build_from_mapping(self, item: dict[str, Any], category: DiscoveryCategory, now, expires) -> DiscoveredProduct | None:
        title = str(item.get("name") or item.get("title") or item.get("item_name") or "").strip()
        raw_url = str(item.get("url") or item.get("item_url") or item.get("product_url") or "").strip()
        if not title or not raw_url:
            return None
        canonical, shop_id, item_id = normalize_shopee_url(raw_url)
        pid = product_id_from(self.config.country, shop_id or str(item.get("shopid") or "") or None, item_id or str(item.get("itemid") or "") or None, canonical)

        sold = _parse_sold(item.get("sold") or item.get("historical_sold") or item.get("sold_count"))
        rating = _parse_float(item.get("rating") or item.get("item_rating") or item.get("rating_star"))
        rating_count = _parse_int(item.get("rating_count") or item.get("cmt_count") or item.get("review_count"))

        price = parse_price(str(item.get("price") or item.get("price_min") or item.get("display_price") or ""))
        image_url = item.get("image") or item.get("image_url")
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
            price=price,
            image_url=str(image_url) if image_url else None,
            shop_id=shop_id or (str(item.get("shopid")) if item.get("shopid") else None),
            item_id=item_id or (str(item.get("itemid")) if item.get("itemid") else None),
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
