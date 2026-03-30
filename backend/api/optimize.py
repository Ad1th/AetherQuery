from fastapi import APIRouter, HTTPException

from backend.core.approx_engine import _rewrite_agg_query
from backend.core.parser import parse_analytical_query
from backend.models.query import ExecuteRequest

router = APIRouter()


@router.post("/optimize")
async def optimize_query(req: ExecuteRequest):
    try:
        parsed = parse_analytical_query(req.query)
        mode = req.mode.lower().strip()
        approx_mode = mode if mode in {"fast", "balanced", "accurate"} else "balanced"
        rewritten = _rewrite_agg_query(req.query, req.source.lower().strip(), mode=approx_mode)
        return {
            "success": True,
            "mode": "approx",
            "approx_mode": approx_mode,
            "rewritten_query": rewritten,
            "table": parsed.table,
            "group_by": parsed.group_by,
            "aggregates": [
                {"func": aggregate.func, "expression": aggregate.expression, "alias": aggregate.alias}
                for aggregate in parsed.aggregates
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
