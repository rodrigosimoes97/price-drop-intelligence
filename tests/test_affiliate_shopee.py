from app.affiliate.shopee import to_affiliate_url


def test_affiliate_template_mode():
    canonical = "https://shopee.com.br/produto-i.1.2"
    cfg = {"mode": "template", "template": "https://redirect.local/?u={url}"}
    out = to_affiliate_url(canonical, cfg)
    assert out.startswith("https://redirect.local/?u=")


def test_affiliate_template_fallback():
    canonical = "https://shopee.com.br/produto-i.1.2"
    cfg = {"mode": "template"}
    assert to_affiliate_url(canonical, cfg) == canonical
