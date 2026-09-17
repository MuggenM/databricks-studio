"""
Dashboard Folders - Organize dashboards into categories and folders.

Allows users to create folder hierarchies and organize dashboards by team, project, or category.
"""

import os
import json
from typing import Dict, Any, List, Optional

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
FOLDERS_FILE = os.path.join(METADATA_DIR, "dashboard_folders.json")

# Default folder structure
DEFAULT_FOLDERS = [
    {
        "id": "folder_root",
        "name": "My Dashboards",
        "parent_id": None,
        "icon": "folder",
        "created_at": "2026-09-15 00:00:00"
    },
    {
        "id": "folder_sales",
        "name": "Sales & Revenue",
        "parent_id": "folder_root",
        "icon": "chart-line-up",
        "created_at": "2026-09-15 00:00:00"
    },
    {
        "id": "folder_operations",
        "name": "Operations",
        "parent_id": "folder_root",
        "icon": "gear",
        "created_at": "2026-09-15 00:00:00"
    },
    {
        "id": "folder_hr",
        "name": "Human Resources",
        "parent_id": "folder_root",
        "icon": "users",
        "created_at": "2026-09-15 00:00:00"
    },
    {
        "id": "folder_finance",
        "name": "Finance",
        "parent_id": "folder_root",
        "icon": "currency-dollar",
        "created_at": "2026-09-15 00:00:00"
    }
]

# {folder_id: {name, parent_id, icon, created_at, created_by}}
FOLDERS = {}

# {dashboard_id: folder_id}
DASHBOARD_FOLDER_MAP = {}


def load_folders():
    """Load folders from file."""
    global FOLDERS, DASHBOARD_FOLDER_MAP
    if os.path.exists(FOLDERS_FILE):
        try:
            with open(FOLDERS_FILE, "r") as f:
                data = json.load(f)
                FOLDERS = data.get("folders", {})
                DASHBOARD_FOLDER_MAP = data.get("dashboard_folder_map", {})
        except Exception:
            FOLDERS = {f["id"]: f for f in DEFAULT_FOLDERS}
            DASHBOARD_FOLDER_MAP = {}
    else:
        FOLDERS = {f["id"]: f for f in DEFAULT_FOLDERS}
        DASHBOARD_FOLDER_MAP = {}

    return FOLDERS, DASHBOARD_FOLDER_MAP


def save_folders():
    """Save folders to file."""
    try:
        with open(FOLDERS_FILE, "w") as f:
            json.dump({
                "folders": FOLDERS,
                "dashboard_folder_map": DASHBOARD_FOLDER_MAP
            }, f, indent=2)
    except Exception as e:
        print(f"Error saving folders: {e}")


def create_folder(name: str, parent_id: str = "folder_root", icon: str = "folder", created_by: str = "admin") -> str:
    """Create a new folder."""
    load_folders()

    import uuid
    import datetime

    folder_id = f"folder_{uuid.uuid4().hex[:12]}"

    FOLDERS[folder_id] = {
        "id": folder_id,
        "name": name,
        "parent_id": parent_id,
        "icon": icon,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "created_by": created_by
    }

    save_folders()
    return folder_id


def list_folders(parent_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all folders, optionally filtered by parent."""
    load_folders()

    if parent_id is None:
        return list(FOLDERS.values())

    return [f for f in FOLDERS.values() if f["parent_id"] == parent_id]


def get_folder(folder_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific folder."""
    load_folders()
    return FOLDERS.get(folder_id)


def update_folder(folder_id: str, name: Optional[str] = None, parent_id: Optional[str] = None, icon: Optional[str] = None) -> bool:
    """Update folder properties."""
    load_folders()

    if folder_id not in FOLDERS:
        return False

    if name:
        FOLDERS[folder_id]["name"] = name
    if parent_id is not None:
        FOLDERS[folder_id]["parent_id"] = parent_id
    if icon:
        FOLDERS[folder_id]["icon"] = icon

    save_folders()
    return True


def delete_folder(folder_id: str, move_to_root: bool = True) -> bool:
    """Delete a folder."""
    load_folders()

    if folder_id not in FOLDERS:
        return False

    # Move dashboards to root or remove mapping
    if move_to_root:
        for dash_id, fid in list(DASHBOARD_FOLDER_MAP.items()):
            if fid == folder_id:
                DASHBOARD_FOLDER_MAP[dash_id] = "folder_root"
    else:
        DASHBOARD_FOLDER_MAP = {k: v for k, v in DASHBOARD_FOLDER_MAP.items() if v != folder_id}

    # Move child folders to parent
    parent_id = FOLDERS[folder_id].get("parent_id", "folder_root")
    for fid, folder in FOLDERS.items():
        if folder.get("parent_id") == folder_id:
            FOLDERS[fid]["parent_id"] = parent_id

    del FOLDERS[folder_id]
    save_folders()
    return True


def move_dashboard_to_folder(dashboard_id: str, folder_id: str) -> bool:
    """Move a dashboard to a folder."""
    load_folders()

    if folder_id not in FOLDERS:
        return False

    DASHBOARD_FOLDER_MAP[dashboard_id] = folder_id
    save_folders()
    return True


def get_dashboard_folder(dashboard_id: str) -> Optional[str]:
    """Get the folder ID for a dashboard."""
    load_folders()
    return DASHBOARD_FOLDER_MAP.get(dashboard_id, "folder_root")


def get_dashboards_in_folder(folder_id: str) -> List[str]:
    """Get all dashboard IDs in a folder."""
    load_folders()
    return [dash_id for dash_id, fid in DASHBOARD_FOLDER_MAP.items() if fid == folder_id]


def get_folder_tree() -> List[Dict[str, Any]]:
    """Get folder hierarchy as a tree structure."""
    load_folders()

    def build_tree(parent_id):
        children = []
        for folder in FOLDERS.values():
            if folder.get("parent_id") == parent_id:
                folder_data = folder.copy()
                folder_data["children"] = build_tree(folder["id"])
                folder_data["dashboard_count"] = len(get_dashboards_in_folder(folder["id"]))
                children.append(folder_data)
        return sorted(children, key=lambda x: x["name"])

    # Start from root
    root_folders = build_tree(None)
    return root_folders
