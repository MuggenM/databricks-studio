"""
Widget Export - Export individual widgets as CSV, Parquet, or image formats.

Provides on-demand export for dashboard widgets with format-specific optimization.
"""

import os
import csv
import json
import base64
from typing import Dict, Any, List, Optional
from datetime import datetime
import tempfile

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
EXPORTS_DIR = os.path.join(WAREHOUSE_DIR, "widget_exports")

# Ensure exports directory exists
os.makedirs(EXPORTS_DIR, exist_ok=True)


def export_widget_csv(
    widget_id: str,
    widget_title: str,
    rows: List[Dict[str, Any]],
    filename: Optional[str] = None
) -> Dict[str, Any]:
    """Export widget data to CSV format."""

    if not rows:
        return {
            "success": False,
            "error": "No data to export"
        }

    try:
        # Generate filename
        if not filename:
            safe_title = "".join(c for c in widget_title if c.isalnum() or c in (' ', '-', '_')).strip()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_{timestamp}.csv"

        filepath = os.path.join(EXPORTS_DIR, filename)

        # Write CSV
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)

        file_size = os.path.getsize(filepath)

        return {
            "success": True,
            "format": "csv",
            "filename": filename,
            "filepath": filepath,
            "file_size": file_size,
            "row_count": len(rows)
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def export_widget_parquet(
    widget_id: str,
    widget_title: str,
    rows: List[Dict[str, Any]],
    filename: Optional[str] = None
) -> Dict[str, Any]:
    """Export widget data to Parquet format."""

    if not rows:
        return {
            "success": False,
            "error": "No data to export"
        }

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        # Generate filename
        if not filename:
            safe_title = "".join(c for c in widget_title if c.isalnum() or c in (' ', '-', '_')).strip()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_{timestamp}.parquet"

        filepath = os.path.join(EXPORTS_DIR, filename)

        # Convert to Arrow table and write
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, filepath, compression='snappy')

        file_size = os.path.getsize(filepath)

        return {
            "success": True,
            "format": "parquet",
            "filename": filename,
            "filepath": filepath,
            "file_size": file_size,
            "row_count": len(rows)
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def export_widget_json(
    widget_id: str,
    widget_title: str,
    rows: List[Dict[str, Any]],
    filename: Optional[str] = None
) -> Dict[str, Any]:
    """Export widget data to JSON format."""

    if not rows:
        return {
            "success": False,
            "error": "No data to export"
        }

    try:
        # Generate filename
        if not filename:
            safe_title = "".join(c for c in widget_title if c.isalnum() or c in (' ', '-', '_')).strip()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_{timestamp}.json"

        filepath = os.path.join(EXPORTS_DIR, filename)

        # Write JSON
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({
                "widget_id": widget_id,
                "widget_title": widget_title,
                "exported_at": datetime.now().isoformat(),
                "row_count": len(rows),
                "data": rows
            }, f, indent=2)

        file_size = os.path.getsize(filepath)

        return {
            "success": True,
            "format": "json",
            "filename": filename,
            "filepath": filepath,
            "file_size": file_size,
            "row_count": len(rows)
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def export_widget_excel(
    widget_id: str,
    widget_title: str,
    rows: List[Dict[str, Any]],
    filename: Optional[str] = None
) -> Dict[str, Any]:
    """Export widget data to Excel format."""

    if not rows:
        return {
            "success": False,
            "error": "No data to export"
        }

    try:
        import pandas as pd

        # Generate filename
        if not filename:
            safe_title = "".join(c for c in widget_title if c.isalnum() or c in (' ', '-', '_')).strip()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_{timestamp}.xlsx"

        filepath = os.path.join(EXPORTS_DIR, filename)

        # Convert to DataFrame and write Excel
        df = pd.DataFrame(rows)
        df.to_excel(filepath, index=False, sheet_name=widget_title[:31])  # Excel sheet name max 31 chars

        file_size = os.path.getsize(filepath)

        return {
            "success": True,
            "format": "excel",
            "filename": filename,
            "filepath": filepath,
            "file_size": file_size,
            "row_count": len(rows)
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def export_chart_svg(
    widget_id: str,
    widget_title: str,
    svg_content: str,
    filename: Optional[str] = None
) -> Dict[str, Any]:
    """Export chart as SVG."""

    if not svg_content:
        return {
            "success": False,
            "error": "No SVG content provided"
        }

    try:
        # Generate filename
        if not filename:
            safe_title = "".join(c for c in widget_title if c.isalnum() or c in (' ', '-', '_')).strip()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_{timestamp}.svg"

        filepath = os.path.join(EXPORTS_DIR, filename)

        # Write SVG
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(svg_content)

        file_size = os.path.getsize(filepath)

        return {
            "success": True,
            "format": "svg",
            "filename": filename,
            "filepath": filepath,
            "file_size": file_size
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def export_chart_png(
    widget_id: str,
    widget_title: str,
    image_data: str,
    filename: Optional[str] = None
) -> Dict[str, Any]:
    """Export chart as PNG from base64 data."""

    if not image_data:
        return {
            "success": False,
            "error": "No image data provided"
        }

    try:
        # Generate filename
        if not filename:
            safe_title = "".join(c for c in widget_title if c.isalnum() or c in (' ', '-', '_')).strip()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_title}_{timestamp}.png"

        filepath = os.path.join(EXPORTS_DIR, filename)

        # Decode base64 and write PNG
        # Remove data URL prefix if present
        if ',' in image_data:
            image_data = image_data.split(',', 1)[1]

        image_bytes = base64.b64decode(image_data)

        with open(filepath, 'wb') as f:
            f.write(image_bytes)

        file_size = os.path.getsize(filepath)

        return {
            "success": True,
            "format": "png",
            "filename": filename,
            "filepath": filepath,
            "file_size": file_size
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_export_history(limit: int = 20) -> List[Dict[str, Any]]:
    """Get recent export history."""

    if not os.path.exists(EXPORTS_DIR):
        return []

    try:
        files = []
        for filename in os.listdir(EXPORTS_DIR):
            filepath = os.path.join(EXPORTS_DIR, filename)
            if os.path.isfile(filepath):
                stat = os.stat(filepath)
                files.append({
                    "filename": filename,
                    "filepath": filepath,
                    "file_size": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
                    "format": filename.rsplit('.', 1)[-1] if '.' in filename else 'unknown'
                })

        # Sort by creation time (newest first)
        files.sort(key=lambda x: x["created_at"], reverse=True)

        return files[:limit]

    except Exception as e:
        print(f"Error getting export history: {e}")
        return []


def cleanup_old_exports(days: int = 7) -> Dict[str, Any]:
    """Cleanup export files older than specified days."""

    if not os.path.exists(EXPORTS_DIR):
        return {"success": True, "deleted_count": 0}

    try:
        import time

        deleted_count = 0
        cutoff_time = time.time() - (days * 24 * 60 * 60)

        for filename in os.listdir(EXPORTS_DIR):
            filepath = os.path.join(EXPORTS_DIR, filename)
            if os.path.isfile(filepath):
                if os.path.getctime(filepath) < cutoff_time:
                    os.remove(filepath)
                    deleted_count += 1

        return {
            "success": True,
            "deleted_count": deleted_count
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "deleted_count": 0
        }
