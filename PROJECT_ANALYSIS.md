# Argus - Complete Project Analysis

## Executive Summary

**Project Name:** Argus (formerly AetherQuery)  
**Project Type:** SQL Query Optimization & Execution Platform  
**Tech Stack:**

- Backend: Python FastAPI + DuckDB/PostgreSQL/MySQL
- Frontend: React 19 + TypeScript + Vite  
  **Status:** ~40-50% Complete (MVP partially implemented, core features working)

---

## Project Vision & Objectives

**Primary Goal:** A unified platform for executing SQL queries in both **exact** and **approximate** modes, with automatic query plan parsing, visualization, and structural comparison.

**Problems It Solves:**

1. **Query Optimization Visualization** - Helps developers understand how their queries are being executed by parsing and visualizing query plans
2. **Approximate Query Processing** - For COUNT/SUM/AVG operations, offers faster approximate results using sampling when exact results aren't needed
3. **Multi-Database Support** - Single interface to query across DuckDB, PostgreSQL, and MySQL
4. **Plan Comparison** - Structural similarity matching for query plans

---

## Architecture Overview

### Backend (FastAPI - /backend)

**Core Structure:**

```
backend/
├── main.py                 # FastAPI app setup + CORS middleware
├── requirements.txt        # Dependencies
├── api/                    # API routers
│   ├── execute.py         # Execute queries (exact/approx)
│   ├── plan.py            # Parse & analyze query plans
│   ├── upload.py          # CSV file upload
│   └── optimize.py        # Query rewriting for approximation
├── core/                  # Business logic
│   ├── router.py          # Route to exact/approx execution
│   ├── exact_engine.py    # Exact query execution
│   ├── approx_engine.py   # Approximate sampling logic
│   ├── plan_parser.py     # Parse EXPLAIN output into tree structure
│   ├── matcher.py         # Plan similarity scoring
│   └── cache.py           # In-memory query result caching
├── db/                    # Database adapters
│   ├── duckdb.py         # DuckDB queries + CSV loading
│   ├── postgres.py       # PostgreSQL queries
│   └── mysql.py          # MySQL queries
└── models/
    └── query.py          # Pydantic request/response models
```

**Key Components:**

1. **API Layer** (`/api`)
   - `POST /api/execute` - Execute query in exact or approx mode
   - `POST /api/sql/execute` - Execute alias
   - `POST /api/plan` - Get query plan
   - `POST /api/sql/parse-plan` - Get query plan alias
   - `POST /api/upload` - Upload CSV file
   - `POST /api/optimize` - Rewrite query for approximation

2. **Execution Engine** (`core/router.py` + engines)
   - Routes queries to exact or approximate execution path
   - Handles mode parameter: "exact" or "approx"
   - Measures execution time and returns structured responses

3. **Database Adapters** (`/db`)
   - DuckDB: Default, in-memory/persistent, CSV auto-loading with UUID filenames
   - PostgreSQL: Requires env vars (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD)
   - MySQL: Requires env vars (MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE)
   - Each adapter provides `execute_query()` and `explain_query()` methods

4. **Query Plan Parser** (`core/plan_parser.py`)
   - Handles multiple EXPLAIN formats: DuckDB text, PostgreSQL JSON, MySQL explain
   - Parses into normalized tree structure:
     ```python
     {
       "type": "OPERATOR_NAME",      # e.g., "SEQ_SCAN", "PROJECTION", "UNGROUPED_AGGREGATE"
       "columns": [...],
       "aggregates": [...],
       "rows": estimated_rows,
       "children": [...]             # Child operators
     }
     ```
   - Generates human-readable explanations

5. **Approximate Query Engine** (`core/approx_engine.py`)
   - **Currently supports:** Simple COUNT(\*), SUM(column), AVG(column) queries
   - Uses sampling: 10% sample rate (TABLESAMPLE for most DBs, USING SAMPLE for DuckDB)
   - Rewrites query to sample data, then scales results (e.g., COUNT(\*) / 0.1)
   - Returns both scaled result and rewritten query for transparency

6. **Caching Layer** (`core/cache.py`)
   - In-memory thread-safe cache with 30-second TTL
   - Cache key = SHA256(source|mode|query)
   - Returns cached results with `"cached": true` flag

---

### Frontend (React + TypeScript + Vite - /frontend)

**Structure:**

```
frontend/
├── src/
│   ├── App.tsx                    # Main app component (split-panel design)
│   ├── main.tsx                   # Entry point
│   ├── index.css                  # Global dark theme styling
│   ├── components/
│   │   └── PlanGraph.tsx          # ReactFlow visualization of query plan tree
│   ├── pages/
│   │   └── QueryPlan.tsx          # Planned but not fully implemented
│   └── utils/
│       └── planToFlow.ts          # Convert plan tree to ReactFlow nodes/edges
├── package.json
├── vite.config.ts
├── tsconfig.json
└── eslint.config.js
```

**UI Layout:**

- **Header**: "Query Executor Workspace" title + subtitle
- **Upload Bar**: File input for CSV upload with visual confirmation
- **CSV Mode Banner**: Shows when CSV is loaded (locks source to DuckDB)
- **Suggested Queries**: Auto-generated for uploaded CSVs (SELECT \*, COUNT, AVG)
- **Two-Column Panel Layout**:
  - **Left Panel (Exact Mode)**:
    - Source selector (DuckDB/PostgreSQL/MySQL)
    - Query editor textarea
    - "Analyze Query" button (parses plan)
    - "Run Query" button (executes)
    - Results table/output
    - Plan tree visualization with ReactFlow
  - **Right Panel (Approx Mode)**:
    - (Same layout as exact panel)
    - Executes with approximate sampling
    - Shows sample rate and rewritten query

**Design:**

- Dark theme (#0f0f0f background, #1a1a1a panels)
- Modern typography: Syne font (headers), JetBrains Mono (code)
- Blue accent color (#5aaaf5) for interactive elements
- Full responsive two-column layout that works on wide screens

**Key Features Implemented:**

1. CSV upload with DuckDB auto-loading
2. Real-time query editing in both panels
3. Side-by-side execution comparison (exact vs approx)
4. Query plan parsing and tree visualization
5. Results displayed in formatted tables
6. Error handling with user-friendly messages
7. Caching indication (`"cached": true`)
8. Query execution timing
9. Rewritten query display for approximate mode

---

## API Contracts

### 1. Execute Query

```http
POST /api/execute
Content-Type: application/json

{
  "query": "SELECT * FROM table LIMIT 10",
  "mode": "exact",           // or "approx"
  "source": "duckdb"        // or "postgres", "mysql"
}

Response:
{
  "result": [[...], [...]], // Rows
  "rows": [[...], [...]],
  "columns": ["col1", "col2"],
  "time": 0.123456,         // Seconds
  "approx": false,          // true if approx mode
  "sample_rate": 0.1,       // Only for approx
  "rewritten_query": "...", // Only for approx
  "source": "duckdb",
  "cached": false           // true if from cache
}
```

### 2. Get Query Plan

```http
POST /api/plan
Content-Type: application/json

{
  "query": "SELECT * FROM table",
  "source": "duckdb"        // or "postgres", "mysql"
}

Response:
{
  "success": true,
  "source": "duckdb",
  "raw_plan": [...],        // Raw EXPLAIN output
  "parsed_plan": {...},     // Parsed structure
  "plan_tree": {            // Tree structure for visualization
    "type": "PROJECTION",
    "columns": [...],
    "aggregates": [...],
    "rows": null,
    "children": [...]
  },
  "explanation": "Selects columns: ..."
}
```

### 3. Upload CSV

```http
POST /api/upload
Content-Type: multipart/form-data

file: <binary CSV file>

Response:
{
  "table_name": "table_a1b2c3d4",
  "path": "/Users/.../datasets/a1b2c3d4.csv"
}
```

### 4. Optimize (Rewrite for Approx)

```http
POST /api/optimize
Content-Type: application/json

{
  "query": "SELECT COUNT(*) FROM table",
  "source": "duckdb"
}

Response:
{
  "success": true,
  "mode": "approx",
  "rewritten_query": "SELECT COUNT(*) / 0.1 AS approx_value FROM (SELECT * FROM table USING SAMPLE 10 PERCENT) t"
}
```

---

## Current Features & Completion Status

### ✅ **IMPLEMENTED (100%)**

1. **Exact Query Execution**
   - Execute arbitrary SQL against DuckDB, PostgreSQL, MySQL
   - Timing measurement
   - Result formatting (columns + rows)
   - Error handling with detailed messages

2. **CSV Upload & DuckDB Integration**
   - Upload CSV files via UI
   - Auto-create DuckDB views with safe identifiers
   - UUID-based file naming in `/datasets/` directory
   - Auto-generate suggested queries

3. **Query Plan Parsing**
   - Handle DuckDB EXPLAIN text format
   - Handle PostgreSQL EXPLAIN JSON format
   - Handle MySQL EXPLAIN format
   - Parse into normalized tree structure
   - Generate human-readable explanations

4. **Basic Approximate Query Processing**
   - Simple COUNT(\*), SUM(column), AVG(column) detection via regex
   - Automatic query rewriting with 10% sampling
   - Scaling results (e.g., COUNT / 0.1)
   - Display rewritten query to user

5. **Query Caching**
   - SHA256-based cache keys (source|mode|query)
   - 30-second TTL
   - Returns with "cached" flag

6. **Plan Visualization (Partial)**
   - Convert tree to ReactFlow nodes/edges
   - Basic graph rendering with layout
   - Shows node types

7. **UI/UX**
   - Dark theme with proper styling
   - Two-column side-by-side comparison
   - CSV mode detection and locking
   - Error messages and status indicators

### 🔄 **PARTIALLY IMPLEMENTED (60-70%)**

1. **Plan Visualization**
   - Tree structure created ✅
   - ReactFlow rendering ✅
   - **Missing:** Proper automatic layout (currently random positioning)
   - **Missing:** Detailed node information on hover
   - **Missing:** Dagre layout algorithm integration (package installed but not used)

2. **Approximate Query Processing**
   - Basic rewriting for simple queries ✅
   - **Missing:** Support for WHERE clauses
   - **Missing:** Support for GROUP BY queries
   - **Missing:** Multi-table queries
   - **Missing:** Complex aggregates (MIN, MAX, MEDIAN, PERCENTILES)

### ❌ **NOT IMPLEMENTED (0%)**

1. **Plan Comparison Feature**
   - `match_plans()` function exists in `matcher.py`
   - **Missing:** API endpoint to compare two plans
   - **Missing:** UI panel to visualize similarity score
   - **Missing:** History of compared plans

2. **Optimization Recommendations**
   - No analysis of slow queries
   - No index suggestions
   - No rewrite suggestions beyond basic approximation

3. **Query History & Saved Queries**
   - No persistent storage
   - No query history tracking
   - No favorites/bookmarks

4. **Advanced Approx Features**
   - No dynamic sampling rate adjustment
   - No sampling strategy options
   - No confidence intervals
   - No materialized sample tables

5. **Advanced UI Features**
   - **Missing:** QueryPlan page (created but empty)
   - **Missing:** Dark/light theme toggle
   - **Missing:** Query result export (CSV, JSON)
   - **Missing:** Plan comparison view
   - **Missing:** Query builder/autocomplete
   - **Missing:** Database schema browser

6. **Performance Features**
   - No query result pagination
   - No large result streaming
   - No incremental result display

7. **Data Management**
   - No table management interface
   - No sample dataset management
   - No dataset metadata display

---

## Project Data

### Datasets (/datasets)

- **aetherquery.duckdb** - Default local DuckDB file
- **aetherquery.duckdb.wal** - DuckDB write-ahead log
- Multiple CSV files (UUID-named, uploaded by users)

### Sample Data Files (in /oldcodes/experiments)

- PostgreSQL TPCH benchmark results (Q1, Q3, etc.)
- Metrics and plans JSON from test runs

---

## MVP (Minimum Viable Product)

**Current Status: ~70% MVP Complete**

### MVP Goals:

1. ✅ Execute queries against multiple databases (DuckDB, PostgreSQL, MySQL)
2. ✅ Parse and visualize query execution plans
3. ✅ Support approximate query processing for simple aggregates
4. ✅ Upload CSV and query it through DuckDB
5. ✅ Split-screen comparison of exact vs approximate execution
6. 🔄 (5/10) Detailed plan comparison and similarity scoring

### Post-MVP Features (For Full Release):

1. Query history and saved queries
2. Advanced approximate processing (GROUP BY, WHERE clauses, etc.)
3. Optimization recommendations and hints
4. Plan comparison dashboard
5. Dataset management and schema browser
6. Query result export
7. Performance analytics and insights
8. Multi-user collaboration features

---

## Project Roadmap & Direction

### Phase 1: MVP Completion (Current - 70% Done)

- ✅ Core execution engines
- ✅ Basic plan parsing
- ✅ CSV upload
- 🔄 Plan visualization with proper layout
- 🔄 UI polish and edge case handling

### Phase 2: Plan Intelligence (Next)

- Implement full plan comparison API endpoint
- Add similarity scoring UI display
- Build plan history tracking
- Create plan recommendation engine (based on patterns)

### Phase 3: Advanced Approximation

- Support WHERE clauses in approximate mode
- Add GROUP BY support with confidence intervals
- Implement dynamic sampling strategies
- Add multi-table approximate joins

### Phase 4: Optimization & Analytics

- Query performance profiling
- Automatic index recommendations
- Query rewrite suggestions
- Historical performance trends

### Phase 5: Full Platform

- User authentication and multi-tenancy
- Collaborative workspace features
- Advanced dataset management
- Mobile-friendly interface

---

## Development Setup

### Requirements

- Python 3.9+
- Node.js 16+
- Virtual environment

### Starting the Backend

```bash
cd /Users/nehadamani/Argus
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload --port 8093
```

Backend runs at: `http://127.0.0.1:8093`  
API Docs: `http://127.0.0.1:8093/docs`

### Starting the Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at: `http://localhost:5173`

### Environment Variables

**For PostgreSQL:**

```
PGHOST=localhost
PGPORT=5432
PGDATABASE=tpch
PGUSER=postgres
PGPASSWORD=password
```

**For MySQL:**

```
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=password
MYSQL_DATABASE=mysql
```

**For DuckDB:**

```
AETHERQUERY_DUCKDB_PATH=/path/to/aetherquery.duckdb  # Optional
```

---

## Codebase Quality & Architecture

### Strengths

1. **Clean Separation of Concerns**
   - API layer (routers)
   - Business logic (core engines)
   - Data access (adapters)
   - Models (Pydantic schemas)

2. **Database Abstraction**
   - Each DB has consistent interface
   - Easy to add new databases

3. **Modern Tech Stack**
   - FastAPI for async support and auto-documentation
   - React 19 with TypeScript for type safety
   - Pydantic for schema validation

4. **Error Handling**
   - Specific HTTP error codes
   - User-friendly error messages
   - Detailed error context

### Areas for Improvement

1. **Logging & Monitoring**
   - Basic logging exists
   - Missing performance metrics
   - No trace-level debugging

2. **Testing**
   - No unit tests visible
   - No integration tests
   - No E2E tests

3. **Configuration Management**
   - Hardcoded defaults in code
   - Should use `.env` files more consistently

4. **Code Documentation**
   - Minimal docstrings
   - Complex logic could use explanation

5. **Frontend State Management**
   - Using only React useState
   - Could benefit from state management library for complex interactions

---

## Completion Percentage Breakdown

| Component            | Completion | Status                       |
| -------------------- | ---------- | ---------------------------- |
| Backend Core Engines | 100%       | ✅ Complete                  |
| Database Adapters    | 100%       | ✅ Complete                  |
| API Endpoints        | 80%        | 🔄 Missing plan comparison   |
| Query Caching        | 100%       | ✅ Complete                  |
| Plan Parsing         | 90%        | 🔄 Layout algorithm not used |
| Approximate Engine   | 60%        | 🔄 Only simple queries       |
| Frontend UI          | 85%        | 🔄 Some pages incomplete     |
| Plan Visualization   | 70%        | 🔄 Needs better layout       |
| CSV Upload           | 100%       | ✅ Complete                  |
| Dark Theme           | 100%       | ✅ Complete                  |
| Query History        | 0%         | ❌ Not started               |
| Optimization Hints   | 0%         | ❌ Not started               |
| **Overall**          | **~65%**   | **🔄 In Active Development** |

---

## How to Use (User Guide)

### Basic Query Execution

1. Open frontend at `http://localhost:5173`
2. Select database source (DuckDB, PostgreSQL, or MySQL)
3. Write SQL query in either panel (exact/approx)
4. Click "Run Query" to execute
5. Results appear below in table format

### Upload & Query CSV

1. Click "Upload CSV" and select a file
2. CSV is loaded into DuckDB as a temporary view
3. Use suggested queries or write custom SQL
4. Both panels are locked to DuckDB (CSV mode)
5. Query the uploaded data like any other table

### Analyze Query Plans

1. Write SQL query
2. Click "Analyze Query"
3. Plan tree appears below
4. Visual graph shows operator flow
5. Tree structure shows estimated rows and operations

### Compare Exact vs Approximate

1. Write same query in both panels
2. Click "Run Query" on both
3. Left shows exact results with full precision
4. Right shows approximate results (10% sample)
5. Compare execution time and accuracy
6. View rewritten approximate query

---

## Next Steps for Development

### Immediate (Sprint 1)

1. Implement plan comparison API endpoint
2. Fix plan visualization layout using Dagre
3. Add plan comparison to UI
4. Add unit tests for core engines
5. Document all API endpoints

### Short Term (Sprint 2-3)

1. Extend approximate engine to support WHERE clauses
2. Add GROUP BY support with confidence intervals
3. Implement query history storage (local storage or DB)
4. Create dataset schema browser
5. Add result export functionality

### Medium Term (Sprint 4-6)

1. Multi-table approximate joins
2. Advanced sampling strategies
3. Performance profiling and analytics
4. Optimization recommendations
5. QueryPlan page implementation

---

## Conclusion

**Argus** is a well-architected SQL execution and analysis platform at the ~65% completion stage. The core infrastructure is solid and MVP-ready, with most critical features working. The main gaps are in advanced approximation support, plan visualization refinement, and feature expansion (history, recommendations, etc.).

The project is positioned to become a powerful tool for:

- Database developers optimizing queries
- Data analysts understanding query performance
- Organizations seeking faster approximate results for exploratory analytics
- Educational purposes (understanding query execution)

With the roadmap outlined above, reaching a production-ready 1.0 release would require 2-3 more months of focused development.
