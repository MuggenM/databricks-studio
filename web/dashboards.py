import os
import re
import json
import time
import uuid
import logging
from typing import Dict, Any, List, Optional
from web.audit import log_query

logger = logging.getLogger("localspark.dashboards")

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
if not os.path.exists(WAREHOUSE_DIR):
    local_alt = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "warehouse"))
    if os.path.exists(local_alt):
        WAREHOUSE_DIR = local_alt
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
DASHBOARDS_FILE = os.path.join(METADATA_DIR, "dashboards.json")
os.makedirs(METADATA_DIR, exist_ok=True)

DEFAULT_DASHBOARDS = [
    {
        "id": "dash_executive_lakehouse",
        "name": "Executive Lakehouse Overview",
        "description": "Real-time KPI metrics and visual analytics across workforce, market equity, products, and customer revenue with interactive parameters.",
        "created_at": "2026-09-12 02:30:00",
        "filters": [
            {
                "key": "department",
                "label": "Department",
                "type": "select",
                "dimension": "department",
                "table": "silver_employees",
                "default": "ALL"
            },
            {
                "key": "sector",
                "label": "Market Sector",
                "type": "select",
                "dimension": "sector",
                "table": "nyse_tickers",
                "default": "ALL"
            },
            {
                "key": "category",
                "label": "Product Category",
                "type": "select",
                "dimension": "category",
                "table": "dim_products",
                "default": "ALL"
            }
        ],
        "widgets": [
            {
                "id": "w_kpi_1",
                "title": "Total Workforce",
                "type": "kpi",
                "query": "SELECT COUNT(*) as value FROM silver_employees WHERE (:department IS NULL OR department = :department)",
                "unit": "employees",
                "width": "col-span-1"
            },
            {
                "id": "w_kpi_2",
                "title": "Annual Payroll",
                "type": "kpi",
                "query": "SELECT SUM(salary) as value FROM silver_employees WHERE (:department IS NULL OR department = :department)",
                "unit": "$",
                "width": "col-span-1"
            },
            {
                "id": "w_kpi_3",
                "title": "Total Market Cap (NYSE)",
                "type": "kpi",
                "query": "SELECT ROUND(SUM(market_cap_b), 1) as value FROM nyse_tickers WHERE (:sector IS NULL OR sector = :sector)",
                "unit": "$B",
                "width": "col-span-1"
            },
            {
                "id": "w_kpi_4",
                "title": "Total Inventory Value",
                "type": "kpi",
                "query": "SELECT ROUND(SUM(price * stock_qty), 2) as value FROM dim_products WHERE (:category IS NULL OR category = :category)",
                "unit": "$",
                "width": "col-span-1"
            },
            {
                "id": "w_chart_1",
                "title": "Average Salary by Department",
                "type": "bar",
                "filter_dimension": "department",
                "query": "SELECT department, ROUND(AVG(salary), 2) as avg_salary FROM silver_employees WHERE (:department IS NULL OR department = :department) GROUP BY department ORDER BY avg_salary DESC",
                "x_col": "department",
                "y_col": "avg_salary",
                "unit": "$",
                "width": "col-span-2"
            },
            {
                "id": "w_chart_2",
                "title": "Market Cap Distribution by Sector",
                "type": "pie",
                "filter_dimension": "sector",
                "query": "SELECT sector, ROUND(SUM(market_cap_b), 1) as total_mcap FROM nyse_tickers WHERE (:sector IS NULL OR sector = :sector) GROUP BY sector",
                "x_col": "sector",
                "y_col": "total_mcap",
                "unit": "$B",
                "width": "col-span-2"
            },
            {
                "id": "w_chart_3",
                "title": "Inventory Valuation by Category",
                "type": "bar",
                "filter_dimension": "category",
                "query": "SELECT category, ROUND(SUM(price * stock_qty), 2) as total_val FROM dim_products WHERE (:category IS NULL OR category = :category) GROUP BY category ORDER BY total_val DESC",
                "x_col": "category",
                "y_col": "total_val",
                "unit": "$",
                "width": "col-span-2"
            },
            {
                "id": "w_table_1",
                "title": "Top Enterprise & Mid-Market Accounts",
                "type": "table",
                "query": "SELECT full_name, segment, annual_spend, join_date FROM dim_customers ORDER BY annual_spend DESC LIMIT 10",
                "width": "col-span-2"
            },
            {
                "id": "w_trend_1",
                "title": "Compensation Momentum",
                "type": "big_number_trendline",
                "query": "SELECT hire_date, ROUND(AVG(salary), 0) as avg_comp FROM silver_employees GROUP BY hire_date ORDER BY hire_date",
                "x_col": "hire_date",
                "y_col": "avg_comp",
                "unit": "$",
                "width": "col-span-1"
            },
            {
                "id": "w_gauge_1",
                "title": "Customer Quota Target",
                "type": "gauge",
                "query": "SELECT ROUND(AVG(annual_spend) / 1000, 1) as quota_pct FROM dim_customers",
                "x_col": "",
                "y_col": "quota_pct",
                "unit": "%",
                "width": "col-span-1"
            },
            {
                "id": "w_area_1",
                "title": "Workforce Comp Trajectory",
                "type": "area",
                "query": "SELECT hire_date, ROUND(SUM(salary), 0) as total_comp FROM silver_employees GROUP BY hire_date ORDER BY hire_date",
                "x_col": "hire_date",
                "y_col": "total_comp",
                "unit": "$",
                "width": "col-span-2"
            },
            {
                "id": "w_treemap_1",
                "title": "Market Cap Treemap",
                "type": "treemap",
                "query": "SELECT company, ROUND(market_cap_b, 1) as market_cap FROM nyse_tickers ORDER BY market_cap DESC LIMIT 15",
                "x_col": "company",
                "y_col": "market_cap",
                "unit": "$B",
                "width": "col-span-2"
            },
            {
                "id": "w_radar_1",
                "title": "Department Comp Radar",
                "type": "radar",
                "query": "SELECT department, ROUND(AVG(salary)/1000, 1) as comp_k FROM silver_employees GROUP BY department",
                "x_col": "department",
                "y_col": "comp_k",
                "unit": "k",
                "width": "col-span-2"
            },
            {
                "id": "w_funnel_1",
                "title": "Account Segment Funnel",
                "type": "funnel",
                "query": "SELECT segment, COUNT(*) as accounts FROM dim_customers GROUP BY segment ORDER BY accounts DESC",
                "x_col": "segment",
                "y_col": "accounts",
                "width": "col-span-2"
            },
            {
                "id": "w_pivot_1",
                "title": "Workforce Headcount Pivot Table",
                "type": "pivot_table",
                "query": "SELECT department, rank, COUNT(*) as headcount FROM silver_employees GROUP BY department, rank ORDER BY department, rank",
                "x_col": "department",
                "y_col": "headcount",
                "z_col": "rank",
                "width": "col-span-2"
            },
            {
                "id": "w_scatter_1",
                "title": "Salary vs Bonus Analysis",
                "type": "scatter",
                "query": "SELECT salary, bonus_estimate FROM silver_employees WHERE salary IS NOT NULL AND bonus_estimate IS NOT NULL",
                "x_col": "salary",
                "y_col": "bonus_estimate",
                "unit": "$",
                "width": "col-span-2"
            },
            {
                "id": "w_scatter_2",
                "title": "Market Cap vs P/E Ratio",
                "type": "scatter",
                "query": "SELECT market_cap_b, pe_ratio FROM nyse_tickers WHERE market_cap_b IS NOT NULL AND pe_ratio IS NOT NULL",
                "x_col": "market_cap_b",
                "y_col": "pe_ratio",
                "unit": "",
                "width": "col-span-2"
            },
            {
                "id": "w_scatter_3",
                "title": "Employee Rank vs Compensation",
                "type": "scatter",
                "query": "SELECT rank, salary FROM silver_employees WHERE rank IS NOT NULL AND salary IS NOT NULL",
                "x_col": "rank",
                "y_col": "salary",
                "unit": "$",
                "width": "col-span-2"
            },
            {
                "id": "w_heatmap_1",
                "title": "Department vs Rank Headcount Matrix",
                "type": "heatmap",
                "query": "SELECT department, CAST(rank AS VARCHAR) as rank_level, COUNT(*) as headcount FROM silver_employees GROUP BY department, rank ORDER BY department, rank",
                "x_col": "department",
                "y_col": "headcount",
                "z_col": "rank_level",
                "unit": "employees",
                "width": "col-span-2"
            },
            {
                "id": "w_heatmap_2",
                "title": "Product Category Inventory Heatmap",
                "type": "heatmap",
                "query": "SELECT category, CASE WHEN stock_qty < 50 THEN 'Low' WHEN stock_qty < 150 THEN 'Medium' ELSE 'High' END as stock_level, SUM(price * stock_qty) as total_value FROM dim_products GROUP BY category, stock_level ORDER BY category, stock_level",
                "x_col": "category",
                "y_col": "total_value",
                "z_col": "stock_level",
                "unit": "$",
                "width": "col-span-2"
            },
            {
                "id": "w_heatmap_3",
                "title": "Customer Segment vs Spending Tier",
                "type": "heatmap",
                "query": "SELECT segment, CASE WHEN annual_spend < 50000 THEN 'Tier 1' WHEN annual_spend < 80000 THEN 'Tier 2' ELSE 'Tier 3' END as spend_tier, COUNT(*) as customer_count FROM dim_customers GROUP BY segment, spend_tier ORDER BY segment, spend_tier",
                "x_col": "segment",
                "y_col": "customer_count",
                "z_col": "spend_tier",
                "unit": "customers",
                "width": "col-span-2"
            }
        ]
    }
]

def load_dashboards_store() -> List[Dict[str, Any]]:
    if not os.path.exists(DASHBOARDS_FILE):
        save_dashboards_store(DEFAULT_DASHBOARDS)
        return DEFAULT_DASHBOARDS
    try:
        with open(DASHBOARDS_FILE, "r") as f:
            data = json.load(f)
            dashboards = data.get("dashboards", DEFAULT_DASHBOARDS)
            # Ensure filters list is present
            for d in dashboards:
                if "filters" not in d:
                    matching_def = next((x for x in DEFAULT_DASHBOARDS if x["id"] == d["id"]), None)
                    d["filters"] = matching_def.get("filters", []) if matching_def else []
            return dashboards
    except Exception as e:
        logger.warning(f"Error loading dashboards file: {e}")
        return DEFAULT_DASHBOARDS

def save_dashboards_store(dashboards: List[Dict[str, Any]]):
    with open(DASHBOARDS_FILE, "w") as f:
        json.dump({"dashboards": dashboards}, f, indent=2)

def resolve_query_parameters(query: str, params: Optional[Dict[str, Any]] = None) -> str:
    """
    Substitutes named :param and {{param}} placeholders in query strings.
    If param is missing, None, or 'ALL', it is replaced with NULL.
    """
    if not query:
        return ""
    if params is None:
        params = {}

    resolved = query

    # Find all :identifier parameters
    named_params = set(re.findall(r':([a-zA-Z_][a-zA-Z0-9_]*)', resolved))
    # Find all {{identifier}} parameters
    mustache_params = set(re.findall(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}', resolved))
    all_param_keys = named_params.union(mustache_params)

    for key in all_param_keys:
        val = params.get(key)
        if val is None or val == "" or str(val).upper() == "ALL":
            replacement = "NULL"
        else:
            escaped = str(val).replace("'", "''")
            replacement = f"'{escaped}'"

        # Replace :key (word boundary)
        resolved = re.sub(rf':{key}\b', replacement, resolved)
        # Replace {{key}}
        resolved = re.sub(rf'\{{\{{\s*{key}\s*\}}\}}', replacement, resolved)

    return resolved

def get_dashboard_filter_options(conn, dashboard_id: str) -> Dict[str, Any]:
    """
    Returns available filter configurations and dynamic dropdown options
    queried from the active Lakehouse tables.
    """
    dashboards = load_dashboards_store()
    target = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not target and dashboards:
        target = dashboards[0]

    filter_defs = target.get("filters", []) if target else []
    options: Dict[str, List[str]] = {}

    for f in filter_defs:
        key = f["key"]
        table = f.get("table")
        dimension = f.get("dimension")
        if table and dimension:
            try:
                res = conn.sql(f"SELECT DISTINCT {dimension} FROM {table} WHERE {dimension} IS NOT NULL ORDER BY 1 LIMIT 50").df()
                vals = ["ALL"] + [str(v) for v in res[dimension].tolist() if v is not None]
                options[key] = vals
            except Exception as e:
                logger.warning(f"Could not load options for {key} from {table}: {e}")
                options[key] = ["ALL"]
        else:
            options[key] = ["ALL"]

    return {
        "filters": filter_defs,
        "options": options
    }

def json_serializable_row(row: Dict[str, Any]) -> Dict[str, Any]:
    new_row = {}
    for k, v in row.items():
        if v is None:
            new_row[k] = None
        elif isinstance(v, float) and (v != v):  # NaN
            new_row[k] = None
        elif hasattr(v, "isoformat"):
            new_row[k] = v.isoformat()
        else:
            new_row[k] = v
    return new_row

def execute_widget_query(conn, query: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    start = time.perf_counter()
    resolved_query = resolve_query_parameters(query, params)
    try:
        res = conn.sql(resolved_query)
        df = res.df()
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        columns = [{"name": col, "type": str(df[col].dtype)} for col in df.columns]
        rows = [json_serializable_row(r) for r in df.to_dict(orient="records")]
        qid = log_query(query_text=resolved_query, duration_ms=elapsed_ms, rows_produced=len(rows), status="SUCCESS", client="DASHBOARD")
        return {
            "success": True,
            "query_id": qid,
            "query_executed": resolved_query,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "elapsed_ms": elapsed_ms,
            "error": None
        }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        qid = log_query(query_text=resolved_query, duration_ms=elapsed_ms, rows_produced=0, status="FAILED", error_message=str(e), client="DASHBOARD")
        return {
            "success": False,
            "query_id": qid,
            "query_executed": resolved_query,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "elapsed_ms": elapsed_ms,
            "error": str(e)
        }
