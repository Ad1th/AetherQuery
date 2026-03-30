from __future__ import annotations

import threading
import time
from typing import Any


class ExecutionProgressStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def start(self, request_id: str, *, query: str, source: str, mode: str) -> None:
        now = time.time()
        with self._lock:
            self._items[request_id] = {
                "request_id": request_id,
                "query": query,
                "source": source,
                "mode": mode,
                "status": "running",
                "phase": "executing",
                "message": "Execution started & is running",
                "started_at": now,
                "updated_at": now,
                "iterations": [],
            }

    def update(self, request_id: str, **fields: Any) -> None:
        now = time.time()
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return
            item.update(fields)
            item["updated_at"] = now

    def append_iteration(self, request_id: str, iteration: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return
            item.setdefault("iterations", []).append(iteration)
            item["updated_at"] = now

    def finish(self, request_id: str, *, result: dict[str, Any] | None = None) -> None:
        now = time.time()
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return
            item["status"] = "completed"
            item["phase"] = "completed"
            item["message"] = "Execution completed"
            item["finished_at"] = now
            item["updated_at"] = now
            if result is not None:
                item["result_preview"] = {
                    "time": result.get("time"),
                    "sample_rate": result.get("sample_rate"),
                    "stop_reason": result.get("stop_reason"),
                }

    def fail(self, request_id: str, error: str) -> None:
        now = time.time()
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return
            item["status"] = "error"
            item["phase"] = "failed"
            item["message"] = error
            item["finished_at"] = now
            item["updated_at"] = now

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return None
            return {
                **item,
                "iterations": list(item.get("iterations", [])),
            }


execution_progress = ExecutionProgressStore()
