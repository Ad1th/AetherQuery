"""
Static-sampling baseline with confidence intervals.

The point of an *adaptive* AQP engine is that it draws just enough sample to
meet the error target, and no more. The honest thing to compare against is the
same estimator machinery run once at a fixed fraction: a pre-materialised
uniform sample, answered with a real backend.stats confidence interval, with no
per-query adaptation. If AetherQuery cannot beat this on (sample rate / latency)
at equal accuracy, the adaptivity is not paying for itself.

This is BlinkDB-shaped -- offline sample, closed-form error -- without BlinkDB's
stratification or workload-tuned profile.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from aqp_eval.policies.base import Policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_MATERIALISED: set[str] = set()
_STATIC_FRACTION = 0.05


def _ensure_sample(con, database: str) -> None:
    if database in _MATERIALISED:
        return
    pct = _STATIC_FRACTION * 100.0
    con.execute(
        f"CREATE OR REPLACE TABLE __static_lineitem AS "
        f"SELECT * FROM lineitem TABLESAMPLE SYSTEM ({pct:.4f} PERCENT)"
    )
    _MATERIALISED.add(database)


class StaticCIPolicy(Policy):
    name = "static_ci"

    def run(self, query, database, target=None, seed=42):
        os.environ["AETHERQUERY_DUCKDB_PATH"] = str(Path(database).resolve())
        from backend.db import duckdb as ddb
        from backend.core.parser import parse_analytical_query
        from backend.stats import (
            Aggregate, AggregateCell, Correction, Method, SampleStats, estimate_query,
        )

        con = ddb.get_connection()
        parsed = parse_analytical_query(query)
        if parsed.has_joins:
            # Static single-table sampling has nothing to say about joins.
            return {
                "policy": self.name, "rows": [], "columns": [],
                "latency_ms": 0.0, "iterations": 0, "error": None,
                "stop_reason": "skipped_join", "seed": seed, "target": target,
                "sample_rate": None,
            }

        _ensure_sample(con, database)
        agg = {"count": Aggregate.COUNT, "sum": Aggregate.SUM, "avg": Aggregate.AVG}
        # Use the *realized* fraction of the one fixed offline sample, not the
        # nominal 5%: a single SYSTEM draw is not exactly 5% and a static sample
        # is stuck with whatever it got.
        n_static = con.execute("SELECT COUNT(*) FROM __static_lineitem").fetchone()[0]
        n_full = con.execute("SELECT COUNT(*) FROM lineitem").fetchone()[0]
        f = max(1e-6, n_static / max(1, n_full))

        where = parsed.where_clause
        filt = f" FILTER (WHERE {where})" if where else ""
        sel = [f"{g} AS __g{i}" for i, g in enumerate(parsed.group_by)]
        sel.append("COUNT(*) AS __n_bucket")
        sel.append(f"COUNT(*){filt} AS __n_domain")
        for a in parsed.aggregates:
            if a.is_count_star:
                continue
            c = f"CAST(({a.expression}) AS DOUBLE)"
            sel += [
                f"SUM({c}){filt} AS __s_{a.alias}",
                f"SUM({c}*{c}){filt} AS __ss_{a.alias}",
                f"VAR_SAMP({c}){filt} AS __v_{a.alias}",
                f"MIN({c}){filt} AS __mn_{a.alias}",
                f"MAX({c}){filt} AS __mx_{a.alias}",
            ]
        sql = f"SELECT {', '.join(sel)} FROM __static_lineitem"
        if parsed.group_by:
            sql += " GROUP BY " + ", ".join(f"__g{i}" for i in range(len(parsed.group_by)))

        start = time.perf_counter()
        res = con.execute(sql)
        cols = [d[0] for d in res.description]
        raw = [dict(zip(cols, r)) for r in res.fetchall()]
        elapsed = time.perf_counter() - start

        n_sample = max(1, sum(int(r.get("__n_bucket") or 0) for r in raw))
        cells, out_rows = [], []
        for r in raw:
            gk = tuple(r.get(f"__g{i}") for i in range(len(parsed.group_by)))
            n_dom = int(r.get("__n_domain") or 0)
            row_out = list(gk)
            for a in parsed.aggregates:
                if a.is_count_star:
                    st = SampleStats(n_sample=n_sample, n_domain=n_dom, nominal_fraction=f)
                    row_out.append(n_dom / f)
                else:
                    sx = float(r.get(f"__s_{a.alias}") or 0.0)
                    st = SampleStats(
                        n_sample=n_sample, n_domain=n_dom, sum_x=sx,
                        sum_xx=float(r.get(f"__ss_{a.alias}") or 0.0),
                        min_x=r.get(f"__mn_{a.alias}"), max_x=r.get(f"__mx_{a.alias}"),
                        variance_direct=r.get(f"__v_{a.alias}"), nominal_fraction=f,
                    )
                    row_out.append(sx / f if a.func.lower() == "sum" else (sx / n_dom if n_dom else None))
                cells.append(AggregateCell(
                    alias=a.alias, aggregate=agg[a.func.lower()], stats=st,
                    group_key=gk if parsed.group_by else None,
                ))
            out_rows.append(row_out)

        es = estimate_query(
            cells, coverage_level=0.95, method=Method.CLT,
            correction=Correction.BONFERRONI if len(cells) > 1 else Correction.NONE,
        )

        return {
            "policy": self.name,
            "rows": out_rows,
            "columns": [*parsed.group_by, *[a.alias for a in parsed.aggregates]],
            "latency_ms": elapsed * 1000,
            "iterations": 1,
            "error": None,
            "stop_reason": "static_5pct",
            "seed": seed,
            "target": target,
            "sample_rate": f,
            "reported_max_rel_half_width": es.max_relative_half_width,
        }
