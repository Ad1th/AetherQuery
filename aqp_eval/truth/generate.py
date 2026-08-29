import argparse
import json
from pathlib import Path

import duckdb


def load_queries(path):
    text = Path(path).read_text()

    queries = []

    for block in text.split(";"):
        block = block.strip()

        if not block:
            continue

        lines = [
            line
            for line in block.splitlines()
            if not line.strip().startswith("--")
        ]

        sql = "\n".join(lines).strip()

        if sql:
            queries.append(sql)

    return queries


def generate(database, query_file, output):
    con = duckdb.connect(database, read_only=True)

    queries = load_queries(query_file)

    results = []

    for number, sql in enumerate(queries, start=1):
        print(f"Running Q{number:02d}")

        result = con.execute(sql)

        columns = [
            description[0]
            for description in result.description
        ]

        rows = result.fetchall()

        results.append({
            "query_id": f"Q{number:02d}",
            "sql": sql,
            "columns": columns,
            "rows": [list(row) for row in rows],
        })

        print(f"  rows: {len(rows)}")

    con.close()

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"\nGround truth written to: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--database", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--output", required=True)

    args = parser.parse_args()

    generate(
        args.database,
        args.queries,
        args.output,
    )
