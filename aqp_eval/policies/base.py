from abc import ABC, abstractmethod
from typing import Any


class Policy(ABC):
    name: str

    @abstractmethod
    def run(
        self,
        query: str,
        database: str,
        target: float | None = None,
        seed: int = 42,
    ) -> dict[str, Any]:
        pass
