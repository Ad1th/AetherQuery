from __future__ import annotations

import re
from difflib import get_close_matches


SQL_KEYWORDS = [
    "SELECT",
    "FROM",
    "WHERE",
    "AS",
    "JOIN",
    "ON",
    "GROUP",
    "BY",
    "ORDER",
    "LIMIT",
    "COUNT",
    "SUM",
    "AVG",
    "MAX",
    "MIN",
    "DISTINCT",
    "AND",
    "OR",
    "NOT",
    "LIKE",
    "IN",
]

SQL_FUNCTIONS = ["SUM", "AVG", "COUNT", "MAX", "MIN", "COALESCE"]

COMMON_SQL_TYPOS = {
    "SELEC": "SELECT",
    "FORM": "FROM",
    "WHER": "WHERE",
    "ODER": "ORDER",
    "GROPU": "GROUP",
    "LMIIT": "LIMIT",
    "JION": "JOIN",
}


def correct_keywords(query: str) -> str:
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


def apply_syntax_rules(query: str) -> str:
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
    for pattern, replacement in replacements:
        corrected = re.sub(pattern, replacement, corrected, flags=re.IGNORECASE)

    corrected = re.sub(r"\(\s+", "(", corrected)
    corrected = re.sub(r"\s+\)", ")", corrected)
    corrected = re.sub(r"\s+,", ",", corrected)
    corrected = re.sub(r",\s*", ", ", corrected)
    corrected = re.sub(r"\s+", " ", corrected).strip()
    return corrected


def auto_correct_query(query: str) -> str:
    corrected = correct_keywords(query)
    corrected = apply_syntax_rules(corrected)
    if corrected.count("(") > corrected.count(")"):
        corrected += ")" * (corrected.count("(") - corrected.count(")"))
    if corrected.count("'") % 2 == 1:
        corrected += "'"
    return corrected


def suggest_functions(query: str) -> dict[str, str]:
    func_calls = re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", query)
    suggestions: dict[str, str] = {}
    for fn in func_calls:
        if fn.upper() in SQL_FUNCTIONS:
            continue
        match = get_close_matches(fn.upper(), SQL_FUNCTIONS, n=1, cutoff=0.5)
        if match:
            suggestions[fn] = match[0]
    return suggestions
