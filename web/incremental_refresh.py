"""
Incremental Refresh - Load only new/changed data for improved performance.

Supports time-based incremental refresh with watermark tracking and multiple merge strategies.
"""

import os
import json
import time
import re
from typing import Dict, Any, List, Optional
from datetime import datetime

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
WATERMARKS_FILE = os.path.join(METADATA_DIR, "incremental_watermarks.json")

# {widget_id: {last_refresh, watermark_column, watermark_value, strategy, row_count}}
WATERMARKS = {}


def load_watermarks():
    """Load watermark data from file."""
    global WATERMARKS
    if os.path.exists(WATERMARKS_FILE):
        try:
            with open(WATERMARKS_FILE, "r") as f:
                WATERMARKS = json.load(f)
        except Exception as e:
            print(f"Error loading watermarks: {e}")
            WATERMARKS = {}
    return WATERMARKS


def save_watermarks():
    """Save watermark data to file."""
    try:
        with open(WATERMARKS_FILE, "w") as f:
            json.dump(WATERMARKS, f, indent=2)
    except Exception as e:
        print(f"Error saving watermarks: {e}")


def detect_timestamp_columns(rows: List[Dict[str, Any]]) -> List[str]:
    """Detect potential timestamp/date columns from query results."""
    if not rows:
        return []

    timestamp_columns = []
    sample_row = rows[0]

    # Common timestamp column patterns
    timestamp_patterns = [
        r'.*_at$',          # created_at, updated_at
        r'.*_time$',        # event_time, insert_time
        r'.*_date$',        # order_date, sale_date
        r'.*timestamp.*',   # any timestamp
        r'^date$',          # just "date"
        r'^time$',          # just "time"
        r'^ts$',            # common abbreviation
        r'.*_ts$',          # event_ts, record_ts
    ]

    for col_name in sample_row.keys():
        # Check column name patterns
        col_lower = col_name.lower()
        if any(re.match(pattern, col_lower) for pattern in timestamp_patterns):
            timestamp_columns.append(col_name)
            continue

        # Check value type
        value = sample_row[col_name]
        if value is not None:
            # Check if it looks like a timestamp
            if isinstance(value, str):
                # Try common timestamp formats
                timestamp_formats = [
                    '%Y-%m-%d %H:%M:%S',
                    '%Y-%m-%d',
                    '%Y-%m-%dT%H:%M:%S',
                    '%Y-%m-%d %H:%M:%S.%f',
                ]
                for fmt in timestamp_formats:
                    try:
                        datetime.strptime(value, fmt)
                        timestamp_columns.append(col_name)
                        break
                    except ValueError:
                        continue

    return timestamp_columns


def get_watermark(widget_id: str) -> Optional[Dict[str, Any]]:
    """Get watermark data for a widget."""
    load_watermarks()
    return WATERMARKS.get(widget_id)


def update_watermark(
    widget_id: str,
    watermark_column: str,
    watermark_value: Any,
    strategy: str = "append",
    row_count: int = 0
):
    """Update watermark for a widget."""
    load_watermarks()

    WATERMARKS[widget_id] = {
        "last_refresh": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "watermark_column": watermark_column,
        "watermark_value": str(watermark_value),
        "strategy": strategy,
        "row_count": row_count
    }

    save_watermarks()


def clear_watermark(widget_id: str) -> bool:
    """Clear watermark for a widget (force full refresh)."""
    load_watermarks()

    if widget_id in WATERMARKS:
        del WATERMARKS[widget_id]
        save_watermarks()
        return True

    return False


def build_incremental_query(
    original_query: str,
    watermark_column: str,
    watermark_value: str,
    strategy: str = "append"
) -> str:
    """Build an incremental query with watermark filter."""

    # Clean up the query
    query = original_query.strip()

    # Remove trailing semicolon if present
    if query.endswith(';'):
        query = query[:-1].strip()

    # Build the watermark filter
    watermark_filter = f"{watermark_column} > '{watermark_value}'"

    # Check if query already has WHERE clause
    query_upper = query.upper()

    if ' WHERE ' in query_upper:
        # Find the WHERE clause position
        where_pos = query_upper.find(' WHERE ')

        # Check if there's an ORDER BY, LIMIT, or GROUP BY after WHERE
        order_pos = query_upper.find(' ORDER BY ', where_pos)
        limit_pos = query_upper.find(' LIMIT ', where_pos)
        group_pos = query_upper.find(' GROUP BY ', where_pos)

        # Find the earliest clause after WHERE
        clause_positions = [p for p in [order_pos, limit_pos, group_pos] if p > where_pos]
        insert_pos = min(clause_positions) if clause_positions else len(query)

        # Insert the watermark filter
        incremental_query = (
            query[:insert_pos].rstrip() +
            f" AND {watermark_filter} " +
            query[insert_pos:]
        )
    else:
        # Check for ORDER BY, LIMIT, or GROUP BY at the end
        order_pos = query_upper.find(' ORDER BY ')
        limit_pos = query_upper.find(' LIMIT ')
        group_pos = query_upper.find(' GROUP BY ')

        # Find the earliest clause
        clause_positions = [p for p in [order_pos, limit_pos, group_pos] if p != -1]

        if clause_positions:
            insert_pos = min(clause_positions)
            incremental_query = (
                query[:insert_pos].rstrip() +
                f" WHERE {watermark_filter} " +
                query[insert_pos:]
            )
        else:
            # No WHERE, ORDER BY, LIMIT, or GROUP BY
            incremental_query = f"{query} WHERE {watermark_filter}"

    return incremental_query


def merge_results(
    existing_rows: List[Dict[str, Any]],
    new_rows: List[Dict[str, Any]],
    strategy: str = "append",
    key_column: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Merge new rows with existing rows based on strategy."""

    if not existing_rows:
        return new_rows

    if not new_rows:
        return existing_rows

    if strategy == "replace":
        # Replace entire dataset
        return new_rows

    elif strategy == "append":
        # Simply append new rows
        return existing_rows + new_rows

    elif strategy == "merge" and key_column:
        # Merge based on key column (update existing, add new)
        merged = {row[key_column]: row for row in existing_rows}

        for row in new_rows:
            key_value = row.get(key_column)
            if key_value is not None:
                merged[key_value] = row

        return list(merged.values())

    else:
        # Default to append if strategy not recognized
        return existing_rows + new_rows


def get_max_value(rows: List[Dict[str, Any]], column: str) -> Optional[Any]:
    """Get maximum value from a column in results."""
    if not rows or column not in rows[0]:
        return None

    values = [row[column] for row in rows if row.get(column) is not None]
    if not values:
        return None

    return max(values)


def configure_incremental_refresh(
    widget_id: str,
    watermark_column: str,
    strategy: str = "append",
    key_column: Optional[str] = None,
    enabled: bool = True
) -> Dict[str, Any]:
    """Configure incremental refresh for a widget."""
    config = {
        "widget_id": widget_id,
        "enabled": enabled,
        "watermark_column": watermark_column,
        "strategy": strategy,
        "key_column": key_column,
        "configured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    return config


def execute_incremental_query(
    conn,
    original_query: str,
    widget_id: str,
    watermark_column: str,
    strategy: str = "append",
    key_column: Optional[str] = None,
    cached_rows: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Execute an incremental query and merge results."""

    watermark = get_watermark(widget_id)

    # Determine if this is the first run or a full refresh
    is_first_run = watermark is None

    if is_first_run:
        # First run - execute full query
        try:
            result = conn.execute(original_query).fetchdf()
            rows = result.to_dict('records') if not result.empty else []

            # Detect and store watermark
            if rows and watermark_column in rows[0]:
                max_value = get_max_value(rows, watermark_column)
                if max_value:
                    update_watermark(
                        widget_id=widget_id,
                        watermark_column=watermark_column,
                        watermark_value=max_value,
                        strategy=strategy,
                        row_count=len(rows)
                    )

            return {
                "success": True,
                "rows": rows,
                "row_count": len(rows),
                "incremental": False,
                "full_refresh": True,
                "watermark_updated": True
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "incremental": False
            }

    else:
        # Incremental refresh
        try:
            # Build incremental query
            incremental_query = build_incremental_query(
                original_query=original_query,
                watermark_column=watermark_column,
                watermark_value=watermark["watermark_value"],
                strategy=strategy
            )

            # Execute incremental query
            result = conn.execute(incremental_query).fetchdf()
            new_rows = result.to_dict('records') if not result.empty else []

            # Merge with cached results if available
            if cached_rows and strategy in ["append", "merge"]:
                merged_rows = merge_results(
                    existing_rows=cached_rows,
                    new_rows=new_rows,
                    strategy=strategy,
                    key_column=key_column
                )
            else:
                merged_rows = new_rows

            # Update watermark if we got new data
            if new_rows and watermark_column in new_rows[0]:
                max_value = get_max_value(new_rows, watermark_column)
                if max_value:
                    update_watermark(
                        widget_id=widget_id,
                        watermark_column=watermark_column,
                        watermark_value=max_value,
                        strategy=strategy,
                        row_count=len(merged_rows)
                    )

            return {
                "success": True,
                "rows": merged_rows,
                "row_count": len(merged_rows),
                "new_rows": len(new_rows),
                "incremental": True,
                "full_refresh": False,
                "watermark_updated": len(new_rows) > 0,
                "incremental_query": incremental_query
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "incremental": True
            }


def get_watermark_stats() -> Dict[str, Any]:
    """Get statistics about incremental refresh usage."""
    load_watermarks()

    total_widgets = len(WATERMARKS)
    total_rows = sum(w.get("row_count", 0) for w in WATERMARKS.values())

    strategies = {}
    for watermark in WATERMARKS.values():
        strategy = watermark.get("strategy", "append")
        strategies[strategy] = strategies.get(strategy, 0) + 1

    return {
        "total_widgets": total_widgets,
        "total_rows": total_rows,
        "strategies": strategies,
        "watermarks": list(WATERMARKS.values())
    }
