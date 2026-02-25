from datetime import timedelta

from app.discovery.shopee import (
    normalize_shopee_url,
    passes_filters,
    product_id_from,
    score_product,
    shopee_price_to_float,
)
from app.models import DiscoveredProduct
from app.utils import utc_now


def _product(**kwargs) -> DiscoveredProduct:
    now = utc_now()
    base = DiscoveredProduct(
        id="shopee:BR:1:2",
        store="shopee",
        country="BR",
        currency="BRL",
        canonical_url="https://shopee.com.br/p-i.1.2",
        url="https://shopee.com.br/p-i.1.2",
        title="Produto Bom",
        tags=["electronics", "bestseller"],
        source="bestsellers:electronics",
        discovered_at_utc=now,
        expires_at_utc=now + timedelta(hours=72),
        score=0.0,
        price=199.9,
        sold=200,
        rating=4.8,
        rating_count=150,
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def test_normalize_and_ids():
    url = "https://shopee.com.br/Produto-Legal-i.12345.67890?utm_source=x&smtt=abc"
    canonical, shop_id, item_id = normalize_shopee_url(url)
    assert canonical == "https://shopee.com.br/Produto-Legal-i.12345.67890"
    assert shop_id == "12345"
    assert item_id == "67890"
    assert product_id_from("BR", shop_id, item_id, canonical) == "shopee:BR:12345:67890"


def test_build_product_id_from_shop_item():
    assert product_id_from("BR", "555", "777", "https://shopee.com.br/product/555/777") == "shopee:BR:555:777"


def test_filters_exclude_keyword_and_min_price():
    filters = {"min_price": 25.0, "exclude_keywords": ["capa"]}
    assert passes_filters(_product(title="Capa iPhone Premium"), filters) is False
    assert passes_filters(_product(price=10.0), filters) is False
    assert passes_filters(_product(), filters) is True


def test_score_deterministic():
    p = _product(sold=1000, rating=4.9, rating_count=800)
    assert score_product(p) == score_product(p)
    assert score_product(p) > 0


def test_price_divisor_conversion():
    assert shopee_price_to_float(25990000) == 259.9


def test_extract_keyword():
    from app.discovery.shopee import ShopeeDiscoveryConfig, ShopeeDiscoveryProvider

    provider = ShopeeDiscoveryProvider(ShopeeDiscoveryConfig())
    kw = provider._extract_keyword("https://shopee.com.br/search?keyword=balan%C3%A7a%20digital&sortBy=sales")
    assert kw == "balança digital"
