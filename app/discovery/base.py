from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import DiscoveredProduct


class DiscoveryProvider(ABC):
    name = "base"

    @abstractmethod
    def discover(self) -> tuple[list[DiscoveredProduct], dict[str, int], list[str]]:
        raise NotImplementedError
