"""
Widget Comments - Add annotations and notes to dashboard widgets.

Allows users to add comments, notes, and annotations to widgets for documentation and collaboration.
"""

import os
import json
import time
from typing import Dict, Any, List, Optional

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
COMMENTS_FILE = os.path.join(METADATA_DIR, "widget_comments.json")

# {widget_id: [{comment_id, text, created_at, created_by, edited_at, edited_by}]}
WIDGET_COMMENTS = {}


def load_comments():
    """Load widget comments from file."""
    global WIDGET_COMMENTS
    if os.path.exists(COMMENTS_FILE):
        try:
            with open(COMMENTS_FILE, "r") as f:
                WIDGET_COMMENTS = json.load(f)
        except Exception:
            WIDGET_COMMENTS = {}
    return WIDGET_COMMENTS


def save_comments():
    """Save widget comments to file."""
    try:
        with open(COMMENTS_FILE, "w") as f:
            json.dump(WIDGET_COMMENTS, f, indent=2)
    except Exception as e:
        print(f"Error saving comments: {e}")


def add_comment(widget_id: str, text: str, created_by: str) -> str:
    """Add a comment to a widget."""
    load_comments()

    import uuid

    comment_id = f"comment_{uuid.uuid4().hex[:12]}"

    comment = {
        "comment_id": comment_id,
        "text": text,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_by": created_by,
        "edited_at": None,
        "edited_by": None
    }

    if widget_id not in WIDGET_COMMENTS:
        WIDGET_COMMENTS[widget_id] = []

    WIDGET_COMMENTS[widget_id].append(comment)
    save_comments()

    return comment_id


def list_comments(widget_id: str) -> List[Dict[str, Any]]:
    """List all comments for a widget."""
    load_comments()
    return WIDGET_COMMENTS.get(widget_id, [])


def get_comment(widget_id: str, comment_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific comment."""
    load_comments()
    comments = WIDGET_COMMENTS.get(widget_id, [])
    return next((c for c in comments if c["comment_id"] == comment_id), None)


def update_comment(widget_id: str, comment_id: str, text: str, edited_by: str) -> bool:
    """Update a comment."""
    load_comments()

    if widget_id not in WIDGET_COMMENTS:
        return False

    for comment in WIDGET_COMMENTS[widget_id]:
        if comment["comment_id"] == comment_id:
            comment["text"] = text
            comment["edited_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            comment["edited_by"] = edited_by
            save_comments()
            return True

    return False


def delete_comment(widget_id: str, comment_id: str) -> bool:
    """Delete a comment."""
    load_comments()

    if widget_id not in WIDGET_COMMENTS:
        return False

    original_count = len(WIDGET_COMMENTS[widget_id])
    WIDGET_COMMENTS[widget_id] = [
        c for c in WIDGET_COMMENTS[widget_id]
        if c["comment_id"] != comment_id
    ]

    if len(WIDGET_COMMENTS[widget_id]) < original_count:
        save_comments()
        return True

    return False


def get_all_comments_for_dashboard(dashboard_id: str, widget_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Get all comments for all widgets in a dashboard."""
    load_comments()

    result = {}
    for widget_id in widget_ids:
        comments = WIDGET_COMMENTS.get(widget_id, [])
        if comments:
            result[widget_id] = comments

    return result


def get_comment_count(widget_id: str) -> int:
    """Get the number of comments on a widget."""
    load_comments()
    return len(WIDGET_COMMENTS.get(widget_id, []))
