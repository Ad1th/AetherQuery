import time
import hashlib
import uuid
from typing import Any
from collections import deque

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.core.cache import query_cache
from backend.core.progress import execution_progress
from backend.core.router import route_query
from backend.core.sql_syntax import auto_correct_query, suggest_functions
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
    request_id = req.request_id or uuid.uuid4().hex
    corrected_query = auto_correct_query(req.query)
    function_suggestions = suggest_functions(corrected_query)
    cache_key = hashlib.sha256(
        f"{req.source}|{req.mode}|{corrected_query}".encode("utf-8")
    ).hexdigest()

    cached = query_cache.get(cache_key)

    # ✅ Handle cached response
    if cached is not None:
        cached_response = {
            **cached,
            "original_query": req.query,
            "corrected_query": corrected_query,
            "function_suggestions": function_suggestions,
            "cached": True,
        }
        execution_progress.start(
            request_id,
            query=req.query,
            source=req.source,
            mode=req.mode,
        )
        execution_progress.finish(request_id, result=cached_response)

        append_history({
            "query": req.query,
            "corrected_query": corrected_query,
            "mode": req.mode,
            "source": req.source,
            "result": cached_response.get("result"),
            "time": cached_response.get("time"),
            "timestamp": time.time(),
            "cached": True,
            "cache_key": cache_key
        })

        return {**cached_response, "request_id": request_id}

    # ✅ Execute fresh query
    execution_progress.start(
        request_id,
        query=req.query,
        source=req.source,
        mode=req.mode,
    )

    def publish_progress(update: dict[str, Any]) -> None:
        latest_iteration = update.pop("latest_iteration", None)
        execution_progress.update(request_id, **update)
        if latest_iteration is not None:
            execution_progress.append_iteration(request_id, latest_iteration)

    try:
        payload = await run_in_threadpool(
            route_query,
            corrected_query,
            req.mode,
            req.source,
            publish_progress,
        )
    except Exception as exc:
        execution_progress.fail(request_id, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.get("benchmark"):
        benchmark_response = {
            **payload,
            "original_query": req.query,
            "corrected_query": corrected_query,
            "function_suggestions": function_suggestions,
            "cached": False,
        }
        query_cache.set(cache_key, benchmark_response)
        execution_progress.finish(request_id, result=payload.get("approx"))
        append_history({
            "query": req.query,
            "corrected_query": corrected_query,
            "mode": req.mode,
            "source": req.source,
            "result": benchmark_response.get("approx", {}).get("result"),
            "time": benchmark_response.get("approx", {}).get("time"),
            "timestamp": time.time(),
            "cached": False,
            "cache_key": cache_key
        })
        return {**benchmark_response, "request_id": request_id}

    response = {
        "result": payload.get("result"),
        "rows": payload.get("rows", payload.get("result")),
        "columns": payload.get("columns", []),
        "time": payload.get("time", 0.0),
        "approx": payload.get("approx", False),
        "sample_rate": payload.get("sample_rate"),
        "source": payload.get("source", req.source),
        "rewritten_query": payload.get("rewritten_query"),
        "original_query": req.query,
        "corrected_query": corrected_query,
        "function_suggestions": function_suggestions,
        "iterations": payload.get("iterations", []),
        "mode_profile": payload.get("mode_profile"),
        "convergence_error": payload.get("convergence_error"),
        "convergence_threshold": payload.get("convergence_threshold"),
        "stop_reason": payload.get("stop_reason"),
        "request_id": request_id,
        "cached": False,
    }

    query_cache.set(cache_key, response)
    execution_progress.finish(request_id, result=payload)

    append_history({
        "query": req.query,
        "corrected_query": corrected_query,
        "mode": req.mode,
        "source": req.source,
        "result": response["result"],
        "time": response["time"],
        "timestamp": time.time(),
        "cached": False,
        "cache_key": cache_key
    })

    return response


@router.get("/execute/progress/{request_id}")
@router.get("/sql/execute/progress/{request_id}")
def get_execute_progress(request_id: str) -> dict[str, Any]:
    progress = execution_progress.get(request_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="Progress entry not found")
    return progress

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
