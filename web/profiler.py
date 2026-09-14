import os
import json
import time
import uuid
import tempfile
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("localspark.profiler")

def get_operator_meta(op_name: str, op_type: str) -> Dict[str, str]:
    """Returns icon and category color class for a DuckDB physical operator."""
    name_upper = (op_name or "").upper()
    type_upper = (op_type or "").upper()

    if "SCAN" in name_upper or "SCAN" in type_upper:
        if "DELTA" in name_upper:
            return {"category": "DELTA_SCAN", "color": "emerald", "icon": "ph-database", "label": "Delta Table Scan"}
        return {"category": "SCAN", "color": "teal", "icon": "ph-table", "label": "Table Scan"}
    elif "AGGREGATE" in name_upper or "GROUP_BY" in name_upper:
        return {"category": "AGGREGATE", "color": "sky", "icon": "ph-sigma", "label": "Aggregate / Group"}
    elif "JOIN" in name_upper:
        return {"category": "JOIN", "color": "purple", "icon": "ph-git-fork", "label": "Hash / Nested Join"}
    elif "ORDER" in name_upper or "SORT" in name_upper:
        return {"category": "SORT", "color": "amber", "icon": "ph-sort-ascending", "label": "Order / Sort"}
    elif "FILTER" in name_upper:
        return {"category": "FILTER", "color": "rose", "icon": "ph-funnel", "label": "Filter"}
    elif "PROJECTION" in name_upper:
        return {"category": "PROJECTION", "color": "blue", "icon": "ph-brackets-curly", "label": "Projection"}
    elif "LIMIT" in name_upper or "TOP" in name_upper:
        return {"category": "LIMIT", "color": "indigo", "icon": "ph-arrows-down-up", "label": "Limit / Top N"}
    else:
        return {"category": "OPERATOR", "color": "slate", "icon": "ph-gear", "label": op_name or "Operator"}

def format_duration(seconds: float) -> str:
    """Formats seconds into human-readable ms or µs."""
    if seconds is None:
        return "0 ms"
    ms = seconds * 1000.0
    if ms >= 1000.0:
        return f"{ms / 1000.0:.2f} s"
    elif ms >= 1.0:
        return f"{ms:.2f} ms"
    elif ms >= 0.001:
        return f"{ms * 1000.0:.1f} µs"
    elif ms > 0:
        return f"{ms * 1000.0:.2f} µs"
    return "0 ms"

def format_bytes(b: int) -> str:
    """Formats bytes to KB / MB."""
    if not b:
        return "0 B"
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    elif b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"

def enrich_plan_node(node: Dict[str, Any], total_latency: float, all_operators: List[Dict[str, Any]], depth: int = 0) -> Dict[str, Any]:
    """Recursively parses and enriches a DuckDB physical operator node."""
    op_name = node.get("operator_name") or node.get("name") or "OPERATOR"
    op_type = node.get("operator_type") or op_name
    timing = float(node.get("operator_timing") or node.get("cpu_time") or 0.0)
    cardinality = int(node.get("operator_cardinality") or 0)
    rows_scanned = int(node.get("operator_rows_scanned") or 0)
    extra_info = node.get("extra_info") or {}

    pct = round((timing / total_latency * 100.0), 1) if total_latency > 0 else 0.0
    meta = get_operator_meta(op_name, op_type)

    # Extract Delta Lake specifics
    delta_files_scanning = extra_info.get("Scanning Files")
    total_files_read = extra_info.get("Total Files Read")
    filters = extra_info.get("Filters") or extra_info.get("File Filters")
    projections = extra_info.get("Projections")
    table_name = extra_info.get("Table")
    groups = extra_info.get("Groups")
    aggregates = extra_info.get("Aggregates")

    node_id = f"op_{len(all_operators)}_{uuid.uuid4().hex[:4]}"

    enriched = {
        "id": node_id,
        "name": op_name,
        "type": op_type,
        "label": meta["label"],
        "category": meta["category"],
        "color": meta["color"],
        "icon": meta["icon"],
        "depth": depth,
        "timing_sec": timing,
        "timing_ms": round(timing * 1000.0, 3),
        "timing_formatted": format_duration(timing),
        "percentage": pct,
        "cardinality": cardinality,
        "rows_scanned": rows_scanned,
        "is_bottleneck": False,
        "details": {
            "table": table_name,
            "filters": str(filters) if filters else None,
            "projections": str(projections) if projections else None,
            "groups": str(groups) if groups else None,
            "aggregates": str(aggregates) if aggregates else None,
            "delta_files_scanning": delta_files_scanning,
            "total_files_read": total_files_read,
            "raw_extra": extra_info
        },
        "children": []
    }

    all_operators.append(enriched)

    for child in node.get("children", []):
        child_enriched = enrich_plan_node(child, total_latency, all_operators, depth=depth + 1)
        enriched["children"].append(child_enriched)

    return enriched

def parse_profile_json(raw_json_str: str) -> Dict[str, Any]:
    """Parses DuckDB JSON profiling output into a structured visualization payload."""
    try:
        data = json.loads(raw_json_str)
    except Exception as e:
        logger.error(f"Error decoding profiling JSON: {e}")
        return {"error": f"Invalid profile JSON: {e}"}

    total_latency = float(data.get("latency") or data.get("cpu_time") or 0.0)
    cpu_time = float(data.get("cpu_time") or 0.0)
    rows_returned = int(data.get("rows_returned") or 0)
    cumulative_scanned = int(data.get("cumulative_rows_scanned") or 0)
    peak_memory = int(data.get("system_peak_buffer_memory") or 0)
    query_name = data.get("query_name", "")

    all_operators: List[Dict[str, Any]] = []
    children_trees = []

    for child in data.get("children", []):
        enriched_child = enrich_plan_node(child, total_latency, all_operators, depth=0)
        children_trees.append(enriched_child)

    # Identify bottleneck: operator with max timing
    bottleneck_node = None
    if all_operators:
        all_operators.sort(key=lambda x: x["timing_sec"], reverse=True)
        max_op = all_operators[0]
        if max_op["percentage"] >= 20.0 or max_op["timing_sec"] > 0.0001:
            max_op["is_bottleneck"] = True
            bottleneck_node = max_op

    bottleneck_summary = None
    if bottleneck_node:
        bottleneck_summary = {
            "operator_name": bottleneck_node["name"],
            "operator_label": bottleneck_node["label"],
            "percentage": bottleneck_node["percentage"],
            "duration": bottleneck_node["timing_formatted"],
            "message": f"{bottleneck_node['label']} ({bottleneck_node['name']}) consumed {bottleneck_node['percentage']}% ({bottleneck_node['timing_formatted']}) of total execution time."
        }

    # Filter efficiency / selectivity
    selectivity_pct = 100.0
    if cumulative_scanned > 0:
        selectivity_pct = round((rows_returned / cumulative_scanned * 100.0), 1)

    return {
        "success": True,
        "query_name": query_name,
        "total_latency_sec": total_latency,
        "total_latency_ms": round(total_latency * 1000.0, 2),
        "total_latency_formatted": format_duration(total_latency),
        "cpu_time_formatted": format_duration(cpu_time),
        "rows_returned": rows_returned,
        "rows_scanned": cumulative_scanned,
        "selectivity_pct": selectivity_pct,
        "peak_memory_bytes": peak_memory,
        "peak_memory_formatted": format_bytes(peak_memory),
        "bottleneck": bottleneck_summary,
        "plan_tree": children_trees,
        "all_operators_count": len(all_operators)
    }

def execute_profiled_query(conn, query: str) -> Dict[str, Any]:
    """
    Executes a query against DuckDB with JSON profiling enabled.
    Returns standard execution results (rows, columns, elapsed_ms) + structured profile tree.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="duckdb_profile_")
    os.close(fd)

    start_perf = time.perf_counter()
    raw_profile_json = None
    query_error = None
    df = None

    try:
        # Enable DuckDB profiling output to temp JSON file
        conn.sql("PRAGMA enable_profiling = 'json';")
        conn.sql(f"PRAGMA profiling_output = '{tmp_path}';")

        res = conn.sql(query)
        if res is not None and hasattr(res, "df"):
            df = res.df()

        conn.sql("PRAGMA disable_profiling;")

        # Read captured profile JSON
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            with open(tmp_path, "r") as f:
                raw_profile_json = f.read()

    except Exception as e:
        query_error = str(e)
        try:
            conn.sql("PRAGMA disable_profiling;")
        except Exception:
            pass
    finally:
        # Clean up temporary profiling file
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    elapsed_ms = round((time.perf_counter() - start_perf) * 1000.0, 2)

    if query_error:
        return {
            "success": False,
            "error": query_error,
            "elapsed_ms": elapsed_ms,
            "profile": None
        }

    # Format result rows and columns
    columns = []
    rows = []
    if df is not None:
        columns = [{"name": col, "type": str(df[col].dtype)} for col in df.columns]
        for record in df.to_dict(orient="records"):
            clean_rec = {}
            for k, v in record.items():
                if v is None or (isinstance(v, float) and (v != v)):
                    clean_rec[k] = None
                elif hasattr(v, "isoformat"):
                    clean_rec[k] = v.isoformat()
                else:
                    clean_rec[k] = v
            rows.append(clean_rec)

    # Enrich profile tree
    profile_data = None
    if raw_profile_json:
        profile_data = parse_profile_json(raw_profile_json)

    return {
        "success": True,
        "elapsed_ms": elapsed_ms,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "profile": profile_data,
        "raw_profile_json": raw_profile_json
    }
