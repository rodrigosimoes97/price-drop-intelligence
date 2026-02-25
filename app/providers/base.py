from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import PriceSnapshot, Product


class PriceProvider(ABC):
    name = "base"

    @abstractmethod
    def fetch_product(self, product: Product) -> PriceSnapshot:
        raise NotImplementedError
