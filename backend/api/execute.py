import time
import hashlib
from typing import Any
from collections import deque

from fastapi import APIRouter, HTTPException

from backend.core.cache import query_cache
from backend.core.router import route_query
from backend.models.query import ExecuteRequest

router = APIRouter()

# ✅ Query history storage (thread-safe, max 50 entries)
query_history: deque[dict] = deque(maxlen=50)

# ✅ Helper to append to history
def append_history(entry: dict):
    query_history.append(entry)

@router.post("/execute")
@router.post("/sql/execute")
async def execute(req: ExecuteRequest) -> dict[str, Any]:
    cache_key = hashlib.sha256(
        f"{req.source}|{req.mode}|{req.query}".encode("utf-8")
    ).hexdigest()

    cached = query_cache.get(cache_key)

    # ✅ Handle cached response
    if cached is not None:
        cached_response = {**cached, "cached": True}

        append_history({
            "query": req.query,
            "mode": req.mode,
            "source": req.source,
            "result": cached_response.get("result"),
            "time": cached_response.get("time"),
            "timestamp": time.time(),
            "cached": True,
            "cache_key": cache_key
        })

        return cached_response

    # ✅ Execute fresh query
    try:
        payload = route_query(req.query, req.mode, req.source)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = {
        "result": payload.get("result"),
        "rows": payload.get("rows", payload.get("result")),
        "columns": payload.get("columns", []),
        "time": payload.get("time", 0.0),
        "approx": payload.get("approx", False),
        "sample_rate": payload.get("sample_rate"),
        "source": payload.get("source", req.source),
        "rewritten_query": payload.get("rewritten_query"),
        "cached": False,
    }

    query_cache.set(cache_key, response)

    append_history({
        "query": req.query,
        "mode": req.mode,
        "source": req.source,
        "result": response["result"],
        "time": response["time"],
        "timestamp": time.time(),
        "cached": False,
        "cache_key": cache_key
    })

    return response

# ✅ API to fetch history (latest first)
@router.get("/history")
def get_history():
    return list(query_history)[::-1]


@router.post("/cache/clear")
def clear_cache(clear_history: bool = True) -> dict[str, Any]:
    history_items_before = len(query_history)
    query_cache.clear()
    if clear_history:
        query_history.clear()

    return {
        "ok": True,
        "cache_cleared": True,
        "history_cleared": clear_history,
        "history_items_before": history_items_before,
        "history_items_after": len(query_history),
    }
