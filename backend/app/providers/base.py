"""Abstract base for all providers."""
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any


class BaseProvider(ABC):
    """All providers inherit from this for consistent interface."""

    @abstractmethod
    def initialize(self) -> None:
        """Set up storage, connections, indexes."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if provider is operational."""
        pass
