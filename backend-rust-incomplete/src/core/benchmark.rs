use serde_json::{json, Map, Value};
use crate::core::exact_engine::{run_exact, ExactQueryResult};
use crate::core::parser::{
    parse_analytical_query,
    ParsedQuery,
};
use crate::core::runtime_sampling::run_runtime_sampling;

fn normalize_exact_result(
    parsed: &ParsedQuery,
    payload: &ExactQueryResult,
) -> Value {
    let rows = match serde_json::to_value(&payload.result) {
        Ok(Value::Array(arr)) => arr,
        _ => vec![],
    };

    if parsed.group_by.is_empty() {
        if rows.is_empty() {
            return json!({});
        }

        let first_row = rows[0]
            .as_array()
            .cloned()
            .unwrap_or_default();

        let mut map = Map::new();

        for (idx, aggregate) in
            parsed.aggregates.iter().enumerate()
        {
            map.insert(
                aggregate.alias.clone(),
                first_row
                    .get(idx)
                    .cloned()
                    .unwrap_or(Value::Null),
            );
        }

        return Value::Object(map);
    }

    let mut result = Map::new();

    for row in rows {
        let row_arr = row
            .as_array()
            .cloned()
            .unwrap_or_default();

        let key = if parsed.group_by.len() == 1 {
            row_arr
                .first()
                .map(|v| v.to_string())
                .unwrap_or_default()
        } else {
            format!(
                "{:?}",
                &row_arr[..parsed.group_by.len()]
            )
        };

        let mut agg_map = Map::new();

        for (idx, aggregate) in
            parsed.aggregates.iter().enumerate()
        {
            agg_map.insert(
                aggregate.alias.clone(),
                row_arr
                    .get(
                        parsed.group_by.len() + idx
                    )
                    .cloned()
                    .unwrap_or(Value::Null),
            );
        }

        result.insert(
            key,
            Value::Object(agg_map),
        );
    }

    Value::Object(result)
}

fn mean_relative_error(
    exact: &Value,
    approx: &Value,
) -> Option<f64> {
    let exact_obj = exact.as_object()?;
    let approx_obj = approx.as_object()?;

    let mut values = Vec::<f64>::new();

    for (key, exact_value) in exact_obj {
        let Some(approx_value) =
            approx_obj.get(key)
        else {
            continue;
        };

        if let (
            Some(exact_nested),
            Some(approx_nested),
        ) = (
            exact_value.as_object(),
            approx_value.as_object(),
        ) {
            for (
                nested_key,
                exact_number,
            ) in exact_nested
            {
                let Some(candidate) =
                    approx_nested.get(nested_key)
                else {
                    continue;
                };

                let Some(exact_f) =
                    exact_number.as_f64()
                else {
                    continue;
                };

                let Some(candidate_f) =
                    candidate.as_f64()
                else {
                    continue;
                };

                if exact_f == 0.0 {
                    continue;
                }

                values.push(
                    (candidate_f - exact_f)
                        .abs()
                        / exact_f.abs(),
                );
            }
        }
    }

    if values.is_empty() {
        None
    } else {
        Some(
            values.iter().sum::<f64>()
                / values.len() as f64,
        )
    }
}

pub fn run_benchmark(
    query: &str,
    source: &str,
    approx_mode: &str,
    accuracy_target: Option<f64>,
) -> Result<Value, String> {
    let parsed =
        parse_analytical_query(query)?;

    let exact_payload =
        run_exact(query, source)?;

    let approx_payload =
        run_runtime_sampling(
            &parsed,
            source,
            approx_mode,
            accuracy_target,
        );

    let approx_result_map =
        serde_json::to_value(&approx_payload.payload.result_map)
            .unwrap_or(json!({}));

    let exact_result_map =
        normalize_exact_result(
            &parsed,
            &exact_payload,
        );

    let error_ratio =
        mean_relative_error(
            &exact_result_map,
            &approx_result_map,
        );

    let exact_time = exact_payload.time;

    let approx_time = approx_payload.total_time;

    Ok(json!({
        "benchmark": true,
        "source": source,
        "approx_mode": approx_mode,
        "accuracy_target": accuracy_target,

        "exact": {
            "result": exact_payload.result,
            "columns": exact_payload.columns,
            "time": exact_time
        },

        "approx": {
            "result": approx_payload.payload.result,
            "rows": approx_payload.payload.rows,
            "columns": approx_payload.payload.columns,
            "time": approx_time,
            "sample_rate": approx_payload.sample_rate,
            "accuracy_target": approx_payload.accuracy_target,
            "iterations": approx_payload.iterations,
            "stop_reason": approx_payload.stop_reason
        },

        "speedup":
            if approx_time > 0.0 {
                Some(exact_time / approx_time)
            } else {
                None::<f64>
            },

        "error_percent":
            error_ratio.map(|v| v * 100.0)
    }))
}