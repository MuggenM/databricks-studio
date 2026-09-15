"""
Dashboard Versioning - Save and restore different dashboard versions.

Allows users to create snapshots of dashboards and rollback to previous versions.
"""

import os
import json
import time
import hashlib
from typing import Dict, Any, List, Optional

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
VERSIONS_FILE = os.path.join(METADATA_DIR, "dashboard_versions.json")

# {dashboard_id: [{version_id, dashboard_config, created_at, created_by, comment}]}
DASHBOARD_VERSIONS = {}


def load_versions():
    """Load dashboard versions from file."""
    global DASHBOARD_VERSIONS
    if os.path.exists(VERSIONS_FILE):
        try:
            with open(VERSIONS_FILE, "r") as f:
                DASHBOARD_VERSIONS = json.load(f)
        except Exception as e:
            DASHBOARD_VERSIONS = {}
    return DASHBOARD_VERSIONS


def save_versions():
    """Save dashboard versions to file."""
    try:
        with open(VERSIONS_FILE, "w") as f:
            json.dump(DASHBOARD_VERSIONS, f, indent=2)
    except Exception as e:
        print(f"Error saving versions: {e}")


def create_version(dashboard_id: str, dashboard_config: Dict[str, Any], created_by: str, comment: str = "") -> str:
    """Create a new version of a dashboard."""
    load_versions()

    version_id = f"v_{int(time.time())}_{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}"

    version_data = {
        "version_id": version_id,
        "dashboard_config": dashboard_config,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_by": created_by,
        "comment": comment or "Version snapshot"
    }

    if dashboard_id not in DASHBOARD_VERSIONS:
        DASHBOARD_VERSIONS[dashboard_id] = []

    DASHBOARD_VERSIONS[dashboard_id].append(version_data)

    # Keep only last 50 versions per dashboard
    if len(DASHBOARD_VERSIONS[dashboard_id]) > 50:
        DASHBOARD_VERSIONS[dashboard_id] = DASHBOARD_VERSIONS[dashboard_id][-50:]

    save_versions()
    return version_id


def list_versions(dashboard_id: str) -> List[Dict[str, Any]]:
    """List all versions of a dashboard."""
    load_versions()
    versions = DASHBOARD_VERSIONS.get(dashboard_id, [])
    # Return without dashboard_config to reduce size
    return [{k: v for k, v in ver.items() if k != "dashboard_config"} for ver in versions]


def get_version(dashboard_id: str, version_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific version of a dashboard."""
    load_versions()
    versions = DASHBOARD_VERSIONS.get(dashboard_id, [])
    return next((v for v in versions if v["version_id"] == version_id), None)


def restore_version(dashboard_id: str, version_id: str) -> Optional[Dict[str, Any]]:
    """Restore a dashboard to a specific version."""
    version = get_version(dashboard_id, version_id)
    if version:
        return version["dashboard_config"]
    return None


def delete_version(dashboard_id: str, version_id: str) -> bool:
    """Delete a specific version."""
    load_versions()
    if dashboard_id in DASHBOARD_VERSIONS:
        DASHBOARD_VERSIONS[dashboard_id] = [
            v for v in DASHBOARD_VERSIONS[dashboard_id]
            if v["version_id"] != version_id
        ]
        save_versions()
        return True
    return False
