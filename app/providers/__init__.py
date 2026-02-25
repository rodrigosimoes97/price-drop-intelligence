from app.providers.base import PriceProvider
from app.providers.generic_html import GenericHTMLProvider


def get_provider(store: str) -> PriceProvider:
    # Future: map store-specific providers (amazon, mercadolivre, etc.)
    return GenericHTMLProvider()
