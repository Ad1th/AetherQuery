mod core;
mod db;

use core::parser::parse_analytical_query;

fn main() {
    let query = "
        SELECT department, AVG(salary)
        FROM employees
        GROUP BY department
        ORDER BY AVG(salary) DESC
        LIMIT 10
    ";

    match parse_analytical_query(query) {
        Ok(parsed) => println!("{:#?}", parsed),
        Err(err) => println!("ERROR: {}", err),
    }
}