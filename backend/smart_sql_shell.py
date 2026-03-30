from __future__ import annotations

import argparse
import os
import re
import time
from difflib import get_close_matches
from typing import Any

import psycopg

SQL_KEYWORDS = [
    "SELECT", "FROM", "WHERE", "AS", "JOIN", "ON", "GROUP", "BY", "ORDER",
    "LIMIT", "COUNT", "SUM", "AVG", "MAX", "MIN", "DISTINCT",
    "AND", "OR", "NOT", "LIKE", "IN",
]

SQL_FUNCTIONS = ["SUM", "AVG", "COUNT", "MAX", "MIN"]

COMMON_SQL_TYPOS = {
    "SELEC": "SELECT",
    "FORM": "FROM",
    "WHER": "WHERE",
    "ODER": "ORDER",
    "GROPU": "GROUP",
    "LMIIT": "LIMIT",
    "JION": "JOIN",
}


class SmartSQLShell:
    def __init__(self, db_config: dict[str, Any], auto_correct: bool = True):
        self.conn: psycopg.Connection | None = None
        self.cursor: psycopg.Cursor | None = None
        self.schema: dict[str, list[str]] = {}
        self.auto_correct = auto_correct
        self.connect_postgres(db_config)
        if self.cursor:
            self.schema = self.load_schema()
            print("Loaded schema:", self.schema)

    def connect_postgres(self, db_config: dict[str, Any]) -> None:
        try:
            self.conn = psycopg.connect(**db_config)
            self.cursor = self.conn.cursor()
            print("✅ Connected to PostgreSQL")
        except Exception as exc:
            print(f"✗ PostgreSQL connection failed: {exc}")
            self.conn = None
            self.cursor = None

    def load_schema(self) -> dict[str, list[str]]:
        if not self.cursor:
            return {}

        tables: dict[str, list[str]] = {}
        try:
            self.cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
            for (table,) in self.cursor.fetchall():
                self.cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (table,),
                )
                tables[table] = [col for (col,) in self.cursor.fetchall()]
        except Exception:
            return {}
        return tables

    def correct_keywords(self, query: str) -> str:
        tokens = re.split(r"(\W+)", query)
        corrected: list[str] = []
        for token in tokens:
            if token.upper() in SQL_KEYWORDS or not token.isalpha():
                corrected.append(token)
            elif token.upper() in COMMON_SQL_TYPOS:
                corrected.append(COMMON_SQL_TYPOS[token.upper()])
            else:
                match = get_close_matches(token.upper(), SQL_KEYWORDS, n=1, cutoff=0.75)
                corrected.append(match[0] if match else token)
        return "".join(corrected)

    def correct_schema_names(self, query: str) -> str:
        # flatten punctuation for matching
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query.replace(".", " "))
        for table in self.schema:
            table_match = get_close_matches(table.upper(), [w.upper() for w in words], n=1, cutoff=0.6)
            if table_match:
                query = re.sub(rf"\b{re.escape(table_match[0])}\b", table, query, flags=re.IGNORECASE)
            for col in self.schema[table]:
                col_match = get_close_matches(col.upper(), [w.upper() for w in words], n=1, cutoff=0.6)
                if col_match:
                    query = re.sub(rf"\b{re.escape(col_match[0])}\b", col, query, flags=re.IGNORECASE)
        return query

    def auto_correct_query(self, query: str) -> str:
        query = self.correct_keywords(query)
        query = self.correct_schema_names(query)
        query = self.correct_syntax(query)
        return query

    def is_syntax_error(self, exc: Exception) -> bool:
        sqlstate = getattr(exc, "sqlstate", None)
        return sqlstate == "42601"

    def validate_syntax(self, query: str) -> bool:
        """Returns True when SQL parses, False only for syntax errors."""
        if not self.cursor:
            return True

        try:
            self.cursor.execute(f"EXPLAIN {query}")
            return True
        except Exception as exc:
            return not self.is_syntax_error(exc)
        finally:
            if self.conn:
                self.conn.rollback()

    def apply_syntax_rules(self, query: str) -> str:
        corrected = query

        replacements = [
            (r"\bGROUPBY\b", "GROUP BY"),
            (r"\bORDERBY\b", "ORDER BY"),
            (r"\bINNERJOIN\b", "INNER JOIN"),
            (r"\bLEFTJOIN\b", "LEFT JOIN"),
            (r"\bRIGHTJOIN\b", "RIGHT JOIN"),
            (r"\bFULLJOIN\b", "FULL JOIN"),
            (r"\bUNIONALL\b", "UNION ALL"),
            (r"\bGROUP\s+BY\s+BY\b", "GROUP BY"),
            (r"\bORDER\s+BY\s+BY\b", "ORDER BY"),
            (r"=>", ">="),
            (r"=<", "<="),
            (r"==", "="),
            (r",\s*,+", ", "),
        ]

        for pattern, repl in replacements:
            corrected = re.sub(pattern, repl, corrected, flags=re.IGNORECASE)

        corrected = re.sub(r"\(\s+", "(", corrected)
        corrected = re.sub(r"\s+\)", ")", corrected)
        corrected = re.sub(r"\s+,", ",", corrected)
        corrected = re.sub(r",\s*", ", ", corrected)
        corrected = re.sub(r"\s+", " ", corrected).strip()
        return corrected

    def correct_syntax(self, query: str) -> str:
        """
        Apply syntax-oriented fixes and keep only parseable variants.
        """
        candidates: list[str] = [query]

        rule_fixed = self.apply_syntax_rules(query)
        if rule_fixed != query:
            candidates.append(rule_fixed)

        # Balance parentheses if likely missing closing parens.
        open_parens = query.count("(")
        close_parens = query.count(")")
        if open_parens > close_parens:
            candidates.append(query + (")" * (open_parens - close_parens)))

        # Close dangling quote if odd count.
        if query.count("'") % 2 == 1:
            candidates.append(query + "'")

        for candidate in candidates:
            if self.validate_syntax(candidate):
                return candidate

        # Fall back to the rule-based variant if nothing validates.
        return rule_fixed

    def suggest_functions(self, query: str) -> dict[str, str]:
        func_calls = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", query)
        suggestions: dict[str, str] = {}
        for fn in func_calls:
            if fn.upper() in SQL_FUNCTIONS:
                continue
            match = get_close_matches(fn.upper(), SQL_FUNCTIONS, n=1, cutoff=0.5)
            if match:
                suggestions[fn] = match[0]
        return suggestions

    def suggest_fix_from_error(self, query: str, exc: Exception) -> tuple[str, str, str] | None:
        """
        Returns (wrong_token, suggested_token, kind) for common SQL errors.
        kind is one of: table, column, function.
        """
        msg = str(exc)

        table_match = re.search(r'relation "([^"]+)" does not exist', msg, flags=re.IGNORECASE)
        if table_match:
            wrong = table_match.group(1)
            table_suggestion = get_close_matches(wrong, list(self.schema.keys()), n=1, cutoff=0.5)
            if table_suggestion:
                return wrong, table_suggestion[0], "table"

        col_match = re.search(r'column "([^"]+)" does not exist', msg, flags=re.IGNORECASE)
        if col_match:
            wrong = col_match.group(1)
            all_cols = [c for cols in self.schema.values() for c in cols]
            col_suggestion = get_close_matches(wrong, all_cols, n=1, cutoff=0.5)
            if col_suggestion:
                return wrong, col_suggestion[0], "column"

        fn_match = re.search(r"function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", msg, flags=re.IGNORECASE)
        if fn_match:
            wrong = fn_match.group(1)
            fn_suggestion = get_close_matches(wrong.upper(), SQL_FUNCTIONS, n=1, cutoff=0.5)
            if fn_suggestion:
                return wrong, fn_suggestion[0], "function"

        # Fallback: inspect query function calls even when error text is not parseable.
        for wrong, right in self.suggest_functions(query).items():
            return wrong, right, "function"

        return None

    def execute_query(self, query: str, _retry_depth: int = 0) -> None:
        if not self.cursor:
            print("No active database connection.")
            return

        corrected = query
        if self.auto_correct:
            corrected = self.auto_correct_query(query)

            func_suggestions = self.suggest_functions(corrected)
            for wrong, right in func_suggestions.items():
                print(f"💡 Function '{wrong}' not found. Did you mean '{right}'?")
                answer = input(f"Replace '{wrong}' with '{right}'? (y/n): ").strip().lower()
                if answer == "y":
                    corrected = re.sub(rf"\b{re.escape(wrong)}\b", right, corrected, flags=re.IGNORECASE)

            if corrected != query:
                print(f"⚡ Corrected query:\n{corrected}")
                answer = input("Execute corrected query? (y/n): ").strip().lower()
                if answer != "y":
                    corrected = query

        start = time.time()
        try:
            self.cursor.execute(corrected)
            if self.cursor.description:
                rows = self.cursor.fetchall()
                for row in rows[:20]:
                    print(row)
                if len(rows) > 20:
                    print(f"... ({len(rows) - 20} more rows)")
            else:
                if self.conn:
                    self.conn.commit()
                print("✓ Query executed successfully")
        except Exception as exc:
            print(f"✗ Execution error: {exc}")
            if self.conn:
                self.conn.rollback()

            if self.auto_correct and _retry_depth < 1:
                fix = self.suggest_fix_from_error(corrected, exc)
                if fix:
                    wrong, right, kind = fix
                    print(
                        f"💡 Detected possible {kind} typo: '{wrong}' -> '{right}'"
                    )
                    answer = input(
                        f"Apply this fix and retry query? (y/n): "
                    ).strip().lower()
                    if answer == "y":
                        retried_query = re.sub(
                            rf"\b{re.escape(wrong)}\b",
                            right,
                            corrected,
                            flags=re.IGNORECASE,
                        )
                        print(f"⚡ Retrying with:\n{retried_query}")
                        self.execute_query(retried_query, _retry_depth=_retry_depth + 1)
                        return

        print(f"⏱ Time: {time.time() - start:.4f} seconds\n")

    def interactive_shell(self) -> None:
        print("🔥 Smart SQL Shell - type 'help' for commands, 'exit' to quit")
        while True:
            try:
                query = input("TPC-H>> ").strip()
                if not query:
                    continue
                cmd = query.lower()
                if cmd in ("exit", "quit"):
                    print("👋 Goodbye!")
                    break
                if cmd == "help":
                    print("Type SQL directly. The shell suggests fixes before execution.")
                    continue
                self.execute_query(query)
            except (KeyboardInterrupt, EOFError):
                print("\n👋 Goodbye!")
                break


def default_db_config() -> dict[str, Any]:
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "tpch"),
        "user": os.getenv("PGUSER", "tpch_user"),
        "password": os.getenv("PGPASSWORD", "abcd@1234"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive SQL shell with typo correction")
    parser.add_argument("--host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--database", default=os.getenv("PGDATABASE", "tpch"))
    parser.add_argument("--user", default=os.getenv("PGUSER", "tpch_user"))
    parser.add_argument("--password", default=os.getenv("PGPASSWORD", "abcd@1234"))
    parser.add_argument(
        "--no-auto-correct",
        action="store_true",
        help="Disable keyword/schema/function correction",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = default_db_config()
    config.update(
        {
            "host": args.host,
            "port": args.port,
            "dbname": args.database,
            "user": args.user,
            "password": args.password,
        }
    )
    shell = SmartSQLShell(config, auto_correct=not args.no_auto_correct)
    shell.interactive_shell()
