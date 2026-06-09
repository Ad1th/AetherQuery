use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use regex::Regex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AggregateSpec {
    pub func: String,
    pub expression: String,
    pub alias: String,
}

impl AggregateSpec {
    pub fn is_count_star(&self) -> bool {
        self.func.eq_ignore_ascii_case("count") && self.expression == "*"
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBySpec {
    pub key: String,
    pub descending: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParsedQuery {
    pub raw_sql: String,
    pub table: String,
    pub select_items: Vec<String>,
    pub aggregates: Vec<AggregateSpec>,
    pub group_by: Vec<String>,
    pub where_clause: Option<String>,
    pub order_by: Vec<OrderBySpec>,
    pub limit: Option<usize>,
}

impl ParsedQuery {
    pub fn projection_columns(&self) -> Vec<String> {
        let mut columns = self.group_by.clone();

        let mut expression_sources = self.group_by.clone();

        if let Some(where_clause) = &self.where_clause {
            expression_sources.push(where_clause.clone());
        }

        for aggregate in &self.aggregates {
            if aggregate.expression != "*" {
                expression_sources.push(aggregate.expression.clone());
            }
        }

        for expression in expression_sources {
            for identifier in extract_identifiers(&expression) {
                if !columns.contains(&identifier) {
                    columns.push(identifier);
                }
            }
        }

        columns
    }
}

pub const SUPPORTED_AGGREGATES: &[&str] = &[
    "count",
    "sum",
    "avg",
];

pub const SQL_KEYWORDS: &[&str] = &[
    "and",
    "as",
    "asc",
    "avg",
    "between",
    "by",
    "coalesce",
    "count",
    "date",
    "desc",
    "from",
    "group",
    "in",
    "is",
    "limit",
    "like",
    "not",
    "null",
    "or",
    "order",
    "select",
    "sum",
    "where",
];

pub fn split_top_level_csv(text: &str) -> Vec<String> {
    let mut items = Vec::new();
    let mut depth = 0;
    let mut current = String::new();

    for ch in text.chars() {
        match ch {
            '(' => {
                depth += 1;
                current.push(ch);
            }
            ')' => {
                depth -= 1;
                current.push(ch);
            }
            ',' if depth == 0 => {
                items.push(current.trim().to_string());
                current.clear();
            }
            _ => current.push(ch),
        }
    }

    let tail = current.trim();

    if !tail.is_empty() {
        items.push(tail.to_string());
    }

    items
}

pub fn normalize_alias(func: &str, expression: &str) -> String {
    let target = if expression == "*" {
        "all".to_string()
    } else {
        expression
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect::<String>()
            .trim_matches('_')
            .to_lowercase()
    };

    format!("{}_{}", func.to_lowercase(), target)
}

pub fn extract_identifiers(expression: &str) -> Vec<String> {
    let string_re =
        Regex::new(r"'(?:''|[^'])*'").unwrap();

    let ident_re =
        Regex::new(r"[a-zA-Z_][a-zA-Z0-9_]*").unwrap();

    let cleaned =
        string_re.replace_all(expression, " ");

    let mut seen = HashSet::new();
    let mut identifiers = Vec::new();

    for capture in ident_re.find_iter(&cleaned) {
        let token = capture.as_str();

        if SQL_KEYWORDS
            .iter()
            .any(|kw| kw.eq_ignore_ascii_case(token))
        {
            continue;
        }

        if seen.insert(token.to_string()) {
            identifiers.push(token.to_string());
        }
    }

    identifiers
}

pub fn parse_aggregate(item: &str) -> Result<AggregateSpec, String> {
    let aggregate_start =
        Regex::new(r"(?i)^(count|sum|avg)\s*\(")
            .unwrap();

    let captures = aggregate_start
        .captures(item.trim())
        .ok_or_else(|| {
            format!(
                "Unsupported select expression: {}",
                item
            )
        })?;

    let func = captures
        .get(1)
        .unwrap()
        .as_str()
        .to_lowercase();

    if !SUPPORTED_AGGREGATES.contains(&func.as_str()) {
        return Err(format!(
            "Unsupported aggregate function: {}",
            func
        ));
    }

    let whole_match = captures.get(0).unwrap();

    let remainder =
        &item.trim()[whole_match.end()..];

    let mut depth = 1;
    let mut closing_index = None;

    for (idx, ch) in remainder.char_indices() {
        match ch {
            '(' => depth += 1,
            ')' => {
                depth -= 1;

                if depth == 0 {
                    closing_index = Some(idx);
                    break;
                }
            }
            _ => {}
        }
    }

    let closing_index =
        closing_index.ok_or_else(|| {
            format!(
                "Malformed aggregate expression: {}",
                item
            )
        })?;

    let expression =
        remainder[..closing_index].trim();

    let tail =
        remainder[closing_index + 1..].trim();

    let alias_re = Regex::new(
        r"(?i)^(?:as\s+)?([a-zA-Z_][a-zA-Z0-9_]*)?$",
    )
    .unwrap();

    let alias_capture = alias_re
        .captures(tail)
        .ok_or_else(|| {
            format!(
                "Unsupported select expression: {}",
                item
            )
        })?;

    let alias = alias_capture
        .get(1)
        .map(|m| m.as_str().to_string())
        .unwrap_or_else(|| {
            normalize_alias(
                &func,
                expression,
            )
        });

    Ok(AggregateSpec {
        func,
        expression: expression.to_string(),
        alias,
    })
}


pub fn parse_order_by(
    order_by_clause: Option<&str>,
    parsed: Option<&ParsedQuery>,
) -> Result<Vec<OrderBySpec>, String> {
    let Some(clause) = order_by_clause else {
        return Ok(vec![]);
    };

    let mut specs = Vec::new();

    let re = Regex::new(
        r"(?i)^(.+?)(?:\s+(asc|desc))?$"
    )
    .unwrap();

    for item in split_top_level_csv(clause) {
        let captures = re
            .captures(item.trim())
            .ok_or_else(|| {
                format!(
                    "Unsupported ORDER BY expression: {}",
                    item
                )
            })?;

        let mut key = captures
            .get(1)
            .unwrap()
            .as_str()
            .trim()
            .to_string();

        if let Some(parsed) = parsed {
            for aggregate in &parsed.aggregates {
                let signature = format!(
                    "{}({})",
                    aggregate.func,
                    aggregate.expression
                );

                if key
                    .replace(" ", "")
                    .eq_ignore_ascii_case(
                        &signature.replace(" ", "")
                    )
                {
                    key = aggregate.alias.clone();
                    break;
                }
            }
        }

        let descending = captures
            .get(2)
            .map(|m| {
                m.as_str()
                    .eq_ignore_ascii_case("desc")
            })
            .unwrap_or(false);

        specs.push(OrderBySpec {
            key,
            descending,
        });
    }

    Ok(specs)
}

pub fn parse_analytical_query(
    query: &str,
) -> Result<ParsedQuery, String> {
    let normalized = query
        .trim()
        .trim_end_matches(';')
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");

    let query_re = Regex::new(
        r"(?i)^select\s+(?P<select>.+)\s+from\s+(?P<table>[a-zA-Z_][a-zA-Z0-9_]*)(?:\s+where\s+(?P<where>.*?))?(?:\s+group\s+by\s+(?P<group_by>.*?))?(?:\s+order\s+by\s+(?P<order_by>.*?))?(?:\s+limit\s+(?P<limit>\d+))?$"
    ).unwrap();

    let captures = query_re
        .captures(&normalized)
        .ok_or_else(|| {
            "Approx mode supports SELECT aggregate queries on one table with optional WHERE/GROUP BY/ORDER BY/LIMIT"
                .to_string()
        })?;

    let select_items = split_top_level_csv(
        captures.name("select").unwrap().as_str()
    );

    let mut aggregates = Vec::new();
    let mut plain_columns = Vec::new();

    let aggregate_start = Regex::new(r"(?i)^(count|sum|avg)\s*\(").unwrap();

    for item in &select_items {
        if aggregate_start.is_match(item.trim()) {
            aggregates.push(parse_aggregate(item)?);
        } else {
            plain_columns.push(item.trim().to_string());
        }
    }

    if aggregates.is_empty() {
        return Err(
            "Approx mode requires at least one aggregate expression".to_string(),
        );
    }

    let group_by = captures
        .name("group_by")
        .map(|m| split_top_level_csv(m.as_str()))
        .unwrap_or_default();

    let normalized_plain: Vec<String> = plain_columns
        .iter()
        .map(|s| s.to_lowercase())
        .collect();

    let normalized_group: Vec<String> = group_by
        .iter()
        .map(|s| s.to_lowercase())
        .collect();

    if normalized_plain != normalized_group {
        return Err(
            "Non-aggregate SELECT columns must match GROUP BY columns in order".to_string(),
        );
    }

    let mut parsed = ParsedQuery {
        raw_sql: normalized.clone(),
        table: captures
            .name("table")
            .unwrap()
            .as_str()
            .to_string(),
        select_items,
        aggregates,
        group_by,
        where_clause: captures
            .name("where")
            .map(|m| m.as_str().trim().to_string()),
        order_by: vec![],
        limit: captures
            .name("limit")
            .map(|m| m.as_str().parse::<usize>().unwrap()),
    };

    parsed.order_by = parse_order_by(
        captures.name("order_by").map(|m| m.as_str()),
        Some(&parsed),
    )?;

    Ok(parsed)
}