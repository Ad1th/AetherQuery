def compare_results(
    exact_rows,
    approx_rows,
    group_key_columns=1,
):
    if not exact_rows or not approx_rows:
        return None

    exact_map = {
        tuple(row[:group_key_columns]): row[group_key_columns:]
        for row in exact_rows
    }

    approx_map = {
        tuple(row[:group_key_columns]): row[group_key_columns:]
        for row in approx_rows
    }

    common_keys = set(exact_map) & set(approx_map)

    if not common_keys:
        return None

    errors = []

    for key in common_keys:
        exact_values = exact_map[key]
        approx_values = approx_map[key]

        for exact_value, approx_value in zip(
            exact_values,
            approx_values,
        ):
            if exact_value is None or approx_value is None:
                continue

            if exact_value == 0:
                if approx_value == 0:
                    errors.append(0.0)
                continue

            error = abs(
                float(approx_value) - float(exact_value)
            ) / abs(float(exact_value))

            errors.append(error)

    if not errors:
        return None

    return sum(errors) / len(errors)
