from app.utils import parse_price


def test_parse_brl_price():
    assert parse_price("R$ 1.234,56") == 1234.56


def test_parse_usd_price():
    assert parse_price("$1,234.56") == 1234.56


def test_parse_invalid_price():
    assert parse_price("sem preço") is None
