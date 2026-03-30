"""
Query history tracking module.
Stores recent executed queries with metadata.
"""

import threading
from datetime import datetime
from typing import Any, Optional


class QueryHistoryStore:
    """Thread-safe in-memory query history storage."""

    def __init__(self, max_items: int = 100):
        self.max_items = max_items
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def add(
        self,
        query: str,
        source: str,
        mode: str,
        result_rows: int = 0,
        execution_time: float = 0.0,
    ) -> None:
        """Add query to history."""
        with self._lock:
            entry = {
                "query": query,
                "source": source,
                "mode": mode,
                "result_rows": result_rows,
                "execution_time": round(execution_time, 6),
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._items.insert(0, entry)  # Most recent first
            if len(self._items) > self.max_items:
                self._items = self._items[:self.max_items]

    def get_all(self) -> list[dict[str, Any]]:
        """Get all history items (most recent first)."""
        with self._lock:
            return list(self._items)

    def get_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get most recent N queries."""
        with self._lock:
            return list(self._items[:limit])

    def clear(self) -> None:
        """Clear all history."""
        with self._lock:
            self._items.clear()


# Global history instance
query_history = QueryHistoryStore(max_items=100)
