import os
import re
import time
import math
import uuid
import shutil
import datetime
import decimal
import json
import logging
from typing import Dict, Any, List, Optional, Union

import numpy as np
import pandas as pd
import duckdb
import duckrun
from deltalake import DeltaTable
import asyncio
from fastapi import FastAPI, Request, Response, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from web.auth import (
    get_current_user, require_role, create_access_token, verify_password,
    get_user_by_username, list_users, create_user, update_user, reset_user_password,
    delete_user, record_user_login, COOKIE_NAME, get_db_connection, init_auth_db
)
from web.permissions import (
    can_user_access_catalog, can_user_manage_catalog, can_user_delete_catalog,
    delete_all_catalog_permissions, filter_catalogs_for_user,
    list_catalog_permissions, grant_catalog_permission, revoke_catalog_permission,
    enforce_sql_permissions, get_catalog_owner
)
from web.audit import log_query, get_query_history, get_query_by_id, clear_query_history, save_query_profile
from web.profiler import execute_profiled_query, parse_profile_json
from web.dashboards import (
    DEFAULT_DASHBOARDS, load_dashboards_store, save_dashboards_store,
    execute_widget_query, resolve_query_parameters, get_dashboard_filter_options
)
from web.workflow import (
    load_jobs, get_job, create_or_update_job, delete_job,
    run_pipeline, get_job_runs, get_run_detail, cron_scheduler_loop
)
from web.genie import (
    get_available_providers, extract_schema_context,
    load_chats, get_chat, create_chat, delete_chat, ask_genie
)
from web.warehouses import (
    CLUSTER_SIZES,
    load_sql_warehouses,
    save_sql_warehouses,
    get_sql_warehouse,
    create_sql_warehouse,
    update_sql_warehouse,
    start_sql_warehouse,
    stop_sql_warehouse,
    delete_sql_warehouse,
    apply_warehouse_compute,
    get_compute_nodes_status,
    load_catalogs,
    save_catalogs,
    get_catalog,
    create_catalog,
    delete_catalog,
    create_catalog_schema,
    sync_catalogs_with_duckrun,
    scan_all_catalogs_and_tables
)

logger = logging.getLogger("databricks_studio")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Databricks Local Studio", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cron_scheduler_loop())
    init_auth_db()
    from web.alerts import alerts_scheduler_loop, init_alerts_db
    init_alerts_db()
    asyncio.create_task(alerts_scheduler_loop())
    from web.experiments import init_experiments_db
    init_experiments_db()
    from web.playground import init_playground_db
    init_playground_db()
    try:
        from web.lineage import init_lineage_db, scan_and_sync_all_assets
        init_lineage_db()
        asyncio.create_task(asyncio.to_thread(scan_and_sync_all_assets))
    except Exception as e_lin:
        logger.warning(f"Failed to auto-scan lineage on startup: {e_lin}")
    # Initialize scheduled exports
    from web.scheduled_exports import init_scheduler
    init_scheduler()

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    from web.scheduled_exports import shutdown_scheduler
    shutdown_scheduler()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOADS_DIR = "/tmp/uploads"

os.makedirs(UPLOADS_DIR, exist_ok=True)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
NOTEBOOKS_DIR = os.getenv("NOTEBOOKS_DIR", "/workspace/notebooks")
JUPYTER_PORT = os.getenv("JUPYTER_PORT", "8890")
JUPYTER_TOKEN = os.getenv("JUPYTER_TOKEN", "databricks")

os.makedirs(WAREHOUSE_DIR, exist_ok=True)
os.makedirs(NOTEBOOKS_DIR, exist_ok=True)

# Shared Duckrun connection
_duckrun_conn = None

def get_duckrun_conn():
    global _duckrun_conn
    if _duckrun_conn is None:
        _duckrun_conn = duckrun.connect(WAREHOUSE_DIR, read_only=False)
        sync_catalogs_with_duckrun(_duckrun_conn)
    else:
        sync_catalogs_with_duckrun(_duckrun_conn)
    return _duckrun_conn

def clean_json_value(v: Any) -> Any:
    """Sanitizes individual values for RFC 7159/8259 compliant JSON serialization, replacing NaNs/Infs/NAs with None."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass

    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    elif isinstance(v, (np.floating,)):
        if np.isnan(v) or np.isinf(v):
            return None
        return float(v)
    elif isinstance(v, (np.integer,)):
        return int(v)
    elif isinstance(v, (np.bool_,)):
        return bool(v)
    elif isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    elif isinstance(v, decimal.Decimal):
        if v.is_nan() or v.is_infinite():
            return None
        return float(v)
    elif isinstance(v, bytes):
        return v.hex()
    elif isinstance(v, (list, tuple, set)):
        return [clean_json_value(x) for x in v]
    elif isinstance(v, dict):
        return {str(k): clean_json_value(sub_v) for k, sub_v in v.items()}
    return v

def json_serializable_row(row_dict: Any) -> Any:
    """Recursively converts datetimes, timestamps, decimals, NaN/Infs, numpy types, and bytes to JSON serializable objects."""
    if not isinstance(row_dict, dict):
        return clean_json_value(row_dict)
    return {str(k): clean_json_value(v) for k, v in row_dict.items()}

def scan_delta_tables() -> List[Dict[str, Any]]:
    """Walks the warehouse directory and returns metadata for all Delta Lake tables."""
    tables = []
    if not os.path.exists(WAREHOUSE_DIR):
        return tables

    for root, dirs, files in os.walk(WAREHOUSE_DIR):
        if "_delta_log" in dirs:
            rel = os.path.relpath(root, WAREHOUSE_DIR)
            parts = rel.split(os.sep)
            if len(parts) >= 2:
                schema_name = parts[0]
                table_name = parts[1]
            else:
                schema_name = "dbo"
                table_name = parts[0]

            total_size = sum(os.path.getsize(os.path.join(root, f)) for f in files)
            version = 0
            num_files = 0
            try:
                dt = DeltaTable(root)
                fields = [f.name for f in dt.schema().fields]
                if fields == ["__duckrun_deleted__"] or "__duckrun_deleted__" in fields:
                    continue
                version = dt.version()
                num_files = len(dt.file_uris())
            except Exception as e:
                logger.warning(f"Error loading DeltaTable at {root}: {e}")
                continue

            tables.append({
                "schema": schema_name,
                "name": table_name,
                "full_name": f"{schema_name}.{table_name}",
                "path": root,
                "version": version,
                "num_files": num_files,
                "size_bytes": total_size
            })
    return tables

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    resp = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "jupyter_port": JUPYTER_PORT,
            "jupyter_token": JUPYTER_TOKEN,
            "warehouse_dir": WAREHOUSE_DIR
        }
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.get("/api/status")
async def get_status():
    conn = get_duckrun_conn()
    tables = scan_delta_tables()
    return {
        "engine": "DuckDB + duckrun",
        "duckdb_version": duckdb.__version__,
        "duckrun_version": getattr(duckrun, "__version__", "0.4.68"),
        "warehouse_dir": WAREHOUSE_DIR,
        "table_count": len(tables),
        "status": "RUNNING",
        "jupyter_url": f"http://localhost:{JUPYTER_PORT}/lab?token={JUPYTER_TOKEN}"
    }

# ==============================================================================
# MULTI-USER RBAC & AUTHENTICATION ENDPOINTS
# ==============================================================================

class LoginRequest(BaseModel):
    username: str
    password: str

class UserCreateRequest(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: str = "user"

class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None

class PasswordResetRequest(BaseModel):
    new_password: str

class CatalogPermissionRequest(BaseModel):
    user_id: Optional[str] = None
    username: Optional[str] = None
    permission: str = "READ"  # READ, WRITE, ADMIN


@app.post("/api/auth/login")
async def login_endpoint(payload: LoginRequest):
    u = get_user_by_username(payload.username, include_password_hash=True)
    if not u or not verify_password(payload.password, u["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if u.get("is_active", 1) != 1:
        raise HTTPException(status_code=403, detail="Account is deactivated. Contact an administrator.")

    record_user_login(u["id"])
    token = create_access_token(u)
    safe_user = {
        "id": u["id"],
        "username": u["username"],
        "display_name": u["display_name"],
        "full_name": u.get("display_name") or u["username"],
        "email": f"{u['username']}@localspark.lakehouse",
        "role": u["role"],
        "created_at": u["created_at"],
        "last_login_at": u["last_login_at"]
    }
    resp = JSONResponse(content={"success": True, "token": token, "user": safe_user})
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=86400,
        httponly=True,
        samesite="lax",
        secure=False
    )
    return resp


@app.post("/api/auth/logout")
async def logout_endpoint():
    resp = JSONResponse(content={"success": True, "message": "Successfully logged out"})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/auth/me")
async def get_current_user_profile(request: Request):
    try:
        current_user = await get_current_user(request)
        if current_user:
            if "full_name" not in current_user or not current_user["full_name"]:
                current_user["full_name"] = current_user.get("display_name") or current_user.get("username", "admin")
            if "email" not in current_user or not current_user["email"]:
                current_user["email"] = f"{current_user.get('username', 'admin')}@localspark.lakehouse"
            return {
                "authenticated": True,
                "user": current_user,
                "is_admin": current_user.get("role") == "admin",
                "is_power_user": current_user.get("role") in ("admin", "power_user")
            }
    except Exception:
        pass
    return {
        "authenticated": False,
        "user": None,
        "is_admin": False,
        "is_power_user": False
    }


# ==============================================================================
# USER MANAGEMENT & IAM (ADMIN ONLY)
# ==============================================================================

@app.get("/api/users")
async def get_users_endpoint(current_user: Dict[str, Any] = Depends(require_role(["admin"]))):
    return {"users": list_users()}


@app.post("/api/users")
async def create_user_endpoint(
    payload: UserCreateRequest,
    current_user: Dict[str, Any] = Depends(require_role(["admin"]))
):
    try:
        name = payload.display_name or payload.full_name or payload.username
        new_u = create_user(
            username=payload.username,
            password=payload.password,
            display_name=name,
            role=payload.role
        )
        new_u["full_name"] = new_u.get("display_name") or new_u["username"]
        new_u["email"] = payload.email or f"{new_u['username']}@localspark.lakehouse"
        return {"success": True, "user": new_u}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/users/{user_id}")
async def update_user_endpoint(
    user_id: str,
    payload: UserUpdateRequest,
    current_user: Dict[str, Any] = Depends(require_role(["admin"]))
):
    try:
        name = payload.display_name or payload.full_name
        updated = update_user(
            user_id,
            display_name=name,
            role=payload.role,
            is_active=payload.is_active
        )
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")
        updated["full_name"] = updated.get("display_name") or updated["username"]
        updated["email"] = payload.email or f"{updated['username']}@localspark.lakehouse"
        return {"success": True, "user": updated}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/users/{user_id}/reset-password")
async def reset_password_endpoint(
    user_id: str,
    payload: PasswordResetRequest,
    current_user: Dict[str, Any] = Depends(require_role(["admin"]))
):
    try:
        ok = reset_user_password(user_id, payload.new_password)
        if not ok:
            raise HTTPException(status_code=404, detail="User not found")
        return {"success": True, "message": "Password successfully reset"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/users/{user_id}")
async def delete_user_endpoint(
    user_id: str,
    current_user: Dict[str, Any] = Depends(require_role(["admin"]))
):
    try:
        ok = delete_user(user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="User not found")
        return {"success": True, "message": "User deactivated successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==============================================================================
# APP SETTINGS ENDPOINTS (ADMIN ONLY)
# ==============================================================================

@app.get("/api/settings")
async def get_settings_endpoint(current_user: Dict[str, Any] = Depends(require_role(["admin"]))):
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT key, value_json, updated_by, updated_at FROM app_settings").fetchall()
        settings = {}
        for r in rows:
            try:
                settings[r["key"]] = json.loads(r["value_json"])
            except Exception:
                settings[r["key"]] = r["value_json"]
        return {"settings": settings}
    finally:
        conn.close()


@app.post("/api/settings")
async def update_settings_endpoint(
    payload: Dict[str, Any],
    current_user: Dict[str, Any] = Depends(require_role(["admin"]))
):
    conn = get_db_connection()
    try:
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with conn:
            for k, v in payload.items():
                val_json = json.dumps(v)
                conn.execute("""
                INSERT INTO app_settings (key, value_json, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """, (k, val_json, current_user.get("username", "admin"), now_str))
        return {"success": True, "message": "Settings updated successfully"}
    finally:
        conn.close()


# ==============================================================================
# CATALOG GOVERNANCE & PERMISSIONS
# ==============================================================================

@app.get("/api/catalogs")
async def get_catalogs(request: Request):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    conn = get_duckrun_conn()
    sync_catalogs_with_duckrun(conn)
    all_cats = scan_all_catalogs_and_tables(conn)
    filtered = filter_catalogs_for_user(all_cats.get("catalogs", []), current_user)
    return {
        "catalogs": filtered,
        "active_catalog": "warehouse",
        "current_user_role": current_user.get("role", "user")
    }


@app.post("/api/catalogs")
async def create_catalog_endpoint(
    payload: Dict[str, Any],
    request: Request
):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    if current_user.get("role") not in ("admin", "power_user"):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: role '{current_user.get('role')}' cannot create catalogs. Requires admin or power_user."
        )

    name = payload.get("name")
    cat_id = payload.get("id")
    if not name or not cat_id:
        raise HTTPException(status_code=400, detail="Catalog name and ID are required")
    desc = payload.get("description", "")
    try:
        new_cat = create_catalog(
            name=name,
            cat_id=cat_id,
            description=desc,
            owner=current_user.get("username", "admin")
        )
        conn = get_duckrun_conn()
        sync_catalogs_with_duckrun(conn)
        return new_cat
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/catalogs/{cat_id}")
async def delete_catalog_endpoint(cat_id: str, request: Request):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    if cat_id == "warehouse":
        raise HTTPException(
            status_code=400,
            detail="The default primary catalog 'warehouse' cannot be deleted."
        )

    if not can_user_delete_catalog(current_user, cat_id):
        role = current_user.get("role", "user")
        if role == "power_user":
            detail = f"Access denied: power users can only delete catalogs where they are the owner."
        elif role == "user":
            detail = f"Access denied: regular users cannot delete catalogs."
        else:
            detail = f"Access denied: only administrators or the catalog owner can delete catalog '{cat_id}'."
        raise HTTPException(status_code=403, detail=detail)

    ok = delete_catalog(cat_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot delete default catalog or catalog not found")

    try:
        delete_all_catalog_permissions(cat_id)
    except Exception as e:
        logger.warning(f"Failed cleaning permissions for deleted catalog {cat_id}: {e}")

    try:
        conn = get_duckrun_conn()
        sync_catalogs_with_duckrun(conn)
    except Exception as e:
        logger.warning(f"Failed syncing duckrun after catalog deletion: {e}")

    return {"success": True, "deleted_id": cat_id}


@app.get("/api/catalogs/{cat_id}/permissions")
async def get_catalog_permissions_endpoint(cat_id: str, request: Request):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    if not can_user_manage_catalog(current_user, cat_id):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: you do not have permission to inspect access lists for catalog '{cat_id}'."
        )
    return list_catalog_permissions(cat_id)


@app.post("/api/catalogs/{cat_id}/permissions")
async def grant_catalog_permission_endpoint(
    cat_id: str,
    payload: CatalogPermissionRequest,
    request: Request
):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    target = payload.user_id or payload.username
    if not target:
        raise HTTPException(status_code=400, detail="Target user_id or username is required")

    return grant_catalog_permission(
        catalog_id=cat_id,
        target_user_id=target,
        permission=payload.permission,
        granted_by_user=current_user
    )


@app.delete("/api/catalogs/{cat_id}/permissions/{target_user_id}")
async def revoke_catalog_permission_endpoint(
    cat_id: str,
    target_user_id: str,
    request: Request
):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    ok = revoke_catalog_permission(
        catalog_id=cat_id,
        target_user_id=target_user_id,
        revoked_by_user=current_user
    )
    return {"success": ok}

@app.post("/api/catalogs/{cat_id}/schemas")
async def create_catalog_schema_endpoint(cat_id: str, payload: Dict[str, Any]):
    schema_name = payload.get("schema_name")
    if not schema_name:
        raise HTTPException(status_code=400, detail="Schema name is required")
    try:
        path = create_catalog_schema(cat_id, schema_name)
        return {"success": True, "catalog": cat_id, "schema": schema_name, "path": path}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ==================== SQL WAREHOUSES (COMPUTE) APIS ====================

@app.get("/api/cluster/nodes")
async def list_cluster_nodes():
    nodes = get_compute_nodes_status()
    online_cnt = sum(1 for n in nodes if n.get("status") == "ONLINE")
    return {
        "success": True,
        "nodes": nodes,
        "total_nodes": len(nodes),
        "online_nodes": online_cnt
    }

@app.get("/api/sql-warehouses")
async def list_sql_warehouses():
    return {
        "warehouses": load_sql_warehouses(),
        "cluster_sizes": CLUSTER_SIZES
    }

@app.post("/api/sql-warehouses")
async def create_sql_warehouse_endpoint(payload: Dict[str, Any]):
    name = payload.get("name")
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="Warehouse name is required")
    cs = payload.get("cluster_size", "Small")
    threads = payload.get("threads")
    max_memory = payload.get("max_memory")
    auto_stop = payload.get("auto_stop_mins", 10)
    is_def = payload.get("is_default", False)
    endpoint = payload.get("endpoint")
    
    wh = create_sql_warehouse(
        name=name,
        cluster_size=cs,
        threads=threads,
        max_memory=max_memory,
        auto_stop_mins=auto_stop,
        is_default=is_def,
        endpoint=endpoint
    )
    return wh

@app.get("/api/sql-warehouses/{wh_id}")
async def get_sql_warehouse_endpoint(wh_id: str):
    wh = get_sql_warehouse(wh_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Warehouse not found")
    return wh

@app.put("/api/sql-warehouses/{wh_id}")
async def update_sql_warehouse_endpoint(wh_id: str, payload: Dict[str, Any]):
    wh = update_sql_warehouse(wh_id, payload)
    if not wh:
        raise HTTPException(status_code=404, detail="Warehouse not found")
    return wh

@app.post("/api/sql-warehouses/{wh_id}/start")
async def start_sql_warehouse_endpoint(wh_id: str):
    wh = start_sql_warehouse(wh_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Warehouse not found")
    return {"success": True, "warehouse": wh}

@app.post("/api/sql-warehouses/{wh_id}/stop")
async def stop_sql_warehouse_endpoint(wh_id: str):
    wh = stop_sql_warehouse(wh_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Warehouse not found")
    return {"success": True, "warehouse": wh}

@app.delete("/api/sql-warehouses/{wh_id}")
async def delete_sql_warehouse_endpoint(wh_id: str):
    ok = delete_sql_warehouse(wh_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot delete default warehouse or warehouse not found")
    return {"success": True, "deleted_id": wh_id}

@app.delete("/api/table/{schema_name}/{table_name}")
async def drop_table_api(schema_name: str, table_name: str, catalog: Optional[str] = "warehouse"):
    schema_clean = sanitize_identifier(schema_name)
    table_clean = sanitize_identifier(table_name)
    target_catalog = catalog or "warehouse"

    conn = get_duckrun_conn()
    if target_catalog != "warehouse":
        cat = get_catalog(target_catalog)
        if not cat:
            raise HTTPException(status_code=404, detail=f"Catalog '{target_catalog}' not found")
        table_ref = f"{cat['id']}.{schema_clean}.{table_clean}"
        dt_path = os.path.join(cat["path"], schema_clean, table_clean)
    else:
        table_ref = f"{schema_clean}.{table_clean}"
        dt_path = os.path.join(WAREHOUSE_DIR, schema_clean, table_clean)

    try:
        conn.sql(f"DROP TABLE IF EXISTS {table_ref}")
        conn.refresh()
    except Exception as e:
        logger.warning(f"Error executing DROP TABLE {table_ref}: {e}")

    try:
        if os.path.exists(dt_path):
            shutil.rmtree(dt_path)
            logger.info(f"Purged dropped table directory: {dt_path}")
    except Exception as e:
        logger.warning(f"Error removing dropped table directory {dt_path}: {e}")

    return {
        "success": True,
        "message": f"Successfully dropped table {table_ref}"
    }

@app.get("/api/table/{schema_name}/{table_name}")
async def get_table_details(schema_name: str, table_name: str, catalog: Optional[str] = None, request: Request = None):
    cat_id = catalog or "warehouse"
    if request:
        try:
            current_user = await get_current_user(request)
        except Exception:
            current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}
        if not can_user_access_catalog(current_user, cat_id, action="READ"):
            raise HTTPException(status_code=403, detail=f"Access denied: User '{current_user.get('username')}' cannot view catalog '{cat_id}'.")
    # Check if catalog is an external storage mount
    from web.mounts import load_mounts
    mounts = {m["catalog_name"]: m for m in load_mounts() if m.get("enabled", True)}
    if catalog in mounts:
        m = mounts[catalog]
        conn = get_duckrun_conn()
        sync_catalogs_with_duckrun(conn)
        raw_conn = getattr(conn, "con", conn)
        try:
            is_delta = False
            target_s3_path = None
            history = []
            version = 1

            if m["type"] == "s3":
                bucket = m["config"].get("bucket", "localspark")
                s3_uri_schema = f"s3://{bucket}/{schema_name}/{table_name}"
                s3_uri_flat = f"s3://{bucket}/{table_name}"

                try:
                    delta_check = raw_conn.execute(f"SELECT file FROM glob('{s3_uri_schema}/_delta_log/0*.json') LIMIT 1").fetchall()
                    if delta_check:
                        is_delta = True
                        target_s3_path = s3_uri_schema
                    else:
                        delta_check_flat = raw_conn.execute(f"SELECT file FROM glob('{s3_uri_flat}/_delta_log/0*.json') LIMIT 1").fetchall()
                        if delta_check_flat:
                            is_delta = True
                            target_s3_path = s3_uri_flat
                except Exception:
                    pass

                if is_delta and target_s3_path:
                    query_target = f"delta_scan('{target_s3_path}')"
                    location = target_s3_path
                    try:
                        from web.mounts import get_s3_storage_options
                        storage_options = get_s3_storage_options(m["config"])
                        dt_s3 = DeltaTable(target_s3_path, storage_options=storage_options)
                        version = dt_s3.version()
                        for h in dt_s3.history():
                            ts = h.get("timestamp", 0)
                            iso_ts = datetime.datetime.fromtimestamp(ts / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else ""
                            history.append({
                                "version": h.get("version", 0),
                                "timestamp": ts,
                                "timestamp_iso": iso_ts,
                                "operation": h.get("operation", "UNKNOWN"),
                                "operationParameters": h.get("operationParameters", {}),
                                "operationMetrics": h.get("operationMetrics", {}),
                                "clientVersion": h.get("clientVersion", ""),
                                "engineInfo": h.get("engineInfo", "")
                            })
                    except Exception as e:
                        logger.debug(f"Could not load Delta history for S3 table: {e}")
                else:
                    query_target = f"read_parquet('s3://{bucket}/{table_name}')"
                    location = f"s3://{bucket}/{table_name}"
            else:
                query_target = f"{catalog}.{schema_name}.{table_name}"
                location = f"{catalog}.{schema_name}.{table_name}"

            desc_rows = raw_conn.execute(f"DESCRIBE SELECT * FROM {query_target} LIMIT 0").fetchall()
            columns = [{"name": r[0], "type": str(r[1]).upper(), "nullable": True, "metadata": {}} for r in desc_rows]

            row_count = 0
            try:
                row_count = raw_conn.execute(f"SELECT COUNT(*) FROM {query_target}").fetchone()[0]
            except Exception:
                pass

            return {
                "catalog": catalog,
                "schema_name": schema_name,
                "table_name": table_name,
                "full_name": f"{catalog}.{schema_name}.{table_name}",
                "location": location,
                "format": "delta" if is_delta else ("parquet" if m["type"] == "s3" else m["type"]),
                "is_delta": is_delta,
                "columns": columns,
                "column_count": len(columns),
                "version": version,
                "history": history,
                "num_files": len(history) if is_delta else (1 if m["type"] == "s3" else 0),
                "size_bytes": 0,
                "is_federated": True,
                "mount_type": m["type"],
                "mount_name": m["name"],
                "row_count": row_count,
                "properties": {
                    "Federation Type": f"Zero-Copy {m['type'].upper()} Mount",
                    "Mount Name": m["name"],
                    "Storage Location": location,
                    "Format": "Delta Lake (ACID)" if is_delta else ("Parquet" if m["type"] == "s3" else m["type"].upper()),
                    "Attached Catalog": catalog
                }
            }
        except Exception as e:
            logger.error(f"Error inspecting federated table: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to inspect federated table: {str(e)}")

    target_path = None
    if catalog == "warehouse":
        cand = os.path.join(WAREHOUSE_DIR, schema_name, table_name)
        if os.path.exists(cand):
            target_path = cand
        else:
            alt = os.path.join(WAREHOUSE_DIR, table_name)
            if os.path.exists(alt):
                target_path = alt
    elif catalog:
        cat = get_catalog(catalog)
        if cat:
            cand = os.path.join(cat["path"], schema_name, table_name)
            if os.path.exists(cand):
                target_path = cand
            else:
                alt = os.path.join(cat["path"], table_name)
                if os.path.exists(alt):
                    target_path = alt
    else:
        cand = os.path.join(WAREHOUSE_DIR, schema_name, table_name)
        if os.path.exists(cand):
            target_path = cand
            catalog = "warehouse"
        else:
            alt = os.path.join(WAREHOUSE_DIR, table_name)
            if os.path.exists(alt):
                target_path = alt
                catalog = "warehouse"
            else:
                for c in load_catalogs():
                    p = os.path.join(c["path"], schema_name, table_name)
                    if os.path.exists(p):
                        target_path = p
                        catalog = c["id"]
                        break

    if not target_path or not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail=f"Table {schema_name}.{table_name} not found in catalog '{catalog or 'warehouse'}'")

    try:
        dt = DeltaTable(target_path)
        fields = [f.name for f in dt.schema().fields]
        if fields == ["__duckrun_deleted__"] or "__duckrun_deleted__" in fields:
            raise HTTPException(status_code=404, detail=f"Table {schema_name}.{table_name} has been dropped")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to open Delta table: {str(e)}")

    columns = []
    try:
        dt_schema = dt.schema()
        for field in dt_schema.fields:
            type_str = str(field.type)
            columns.append({
                "name": field.name,
                "type": type_str,
                "nullable": field.nullable,
                "metadata": field.metadata
            })
    except Exception as e:
        logger.warning(f"Error reading schema: {e}")

    history = []
    try:
        raw_history = dt.history()
        for h in raw_history:
            ts = h.get("timestamp", 0)
            iso_ts = datetime.datetime.fromtimestamp(ts / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else ""
            history.append({
                "version": h.get("version", 0),
                "timestamp": ts,
                "timestamp_iso": iso_ts,
                "operation": h.get("operation", "UNKNOWN"),
                "operationParameters": h.get("operationParameters", {}),
                "operationMetrics": h.get("operationMetrics", {}),
                "clientVersion": h.get("clientVersion", ""),
                "engineInfo": h.get("engineInfo", "")
            })
    except Exception as e:
        logger.warning(f"Error reading history: {e}")

    files = dt.file_uris()
    total_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
    full_name = f"{catalog}.{schema_name}.{table_name}" if catalog and catalog != "warehouse" else f"{schema_name}.{table_name}"

    return {
        "catalog": catalog or "warehouse",
        "schema_name": schema_name,
        "table_name": table_name,
        "full_name": full_name,
        "location": target_path,
        "format": "delta",
        "version": dt.version(),
        "columns": columns,
        "history": history,
        "details": {
            "catalog": catalog or "warehouse",
            "num_files": len(files),
            "size_bytes": total_size,
            "version": dt.version(),
            "location": target_path,
            "format": "Delta Lake (Parquet + ACID Log)"
        }
    }

@app.get("/api/table/{schema_name}/{table_name}/preview")
async def preview_table(schema_name: str, table_name: str, limit: int = 50, version: Optional[int] = None, catalog: Optional[str] = None, request: Request = None):
    cat_id = catalog or "warehouse"
    if request:
        try:
            current_user = await get_current_user(request)
        except Exception:
            current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}
        if not can_user_access_catalog(current_user, cat_id, action="READ"):
            raise HTTPException(status_code=403, detail=f"Access denied: User '{current_user.get('username')}' cannot view catalog '{cat_id}'.")
    conn = get_duckrun_conn()
    sync_catalogs_with_duckrun(conn)

    # Check if catalog is an external storage mount
    from web.mounts import load_mounts
    mounts = {m["catalog_name"]: m for m in load_mounts() if m.get("enabled", True)}
    if catalog in mounts:
        m = mounts[catalog]
        try:
            if m["type"] == "s3":
                bucket = m["config"].get("bucket", "localspark")
                s3_uri_schema = f"s3://{bucket}/{schema_name}/{table_name}"
                s3_uri_flat = f"s3://{bucket}/{table_name}"
                is_delta = False
                target_s3_path = None
                raw_conn = getattr(conn, "con", conn)
                try:
                    delta_check = raw_conn.execute(f"SELECT file FROM glob('{s3_uri_schema}/_delta_log/0*.json') LIMIT 1").fetchall()
                    if delta_check:
                        is_delta = True
                        target_s3_path = s3_uri_schema
                    else:
                        delta_check_flat = raw_conn.execute(f"SELECT file FROM glob('{s3_uri_flat}/_delta_log/0*.json') LIMIT 1").fetchall()
                        if delta_check_flat:
                            is_delta = True
                            target_s3_path = s3_uri_flat
                except Exception:
                    pass

                if is_delta and target_s3_path:
                    query_target = f"delta_scan('{target_s3_path}')"
                else:
                    query_target = f"read_parquet('s3://{bucket}/{table_name}')"
            else:
                query_target = f"{catalog}.{schema_name}.{table_name}"

            df = conn.sql(f"SELECT * FROM {query_target} LIMIT {int(limit)}").df()
            df_clean = df.replace({np.nan: None, np.inf: None, -np.inf: None})
            rows = [
                {col: clean_json_value(val) for col, val in row.items()}
                for row in df_clean.to_dict(orient="records")
            ]
            columns = [{"name": str(col), "type": str(df[col].dtype).upper()} for col in df.columns]

            return {
                "columns": columns,
                "rows": rows,
                "total_rows": len(rows),
                "limit": limit,
                "is_federated": True,
                "mount_type": m["type"]
            }
        except Exception as e:
            logger.error(f"Error previewing federated table: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to preview federated table: {str(e)}")

    target_path = None
    if catalog == "warehouse":
        cand = os.path.join(WAREHOUSE_DIR, schema_name, table_name)
        if os.path.exists(cand):
            target_path = cand
        else:
            alt = os.path.join(WAREHOUSE_DIR, table_name)
            if os.path.exists(alt):
                target_path = alt
    elif catalog:
        cat = get_catalog(catalog)
        if cat:
            cand = os.path.join(cat["path"], schema_name, table_name)
            if os.path.exists(cand):
                target_path = cand
            else:
                alt = os.path.join(cat["path"], table_name)
                if os.path.exists(alt):
                    target_path = alt
    else:
        cand = os.path.join(WAREHOUSE_DIR, schema_name, table_name)
        if os.path.exists(cand):
            target_path = cand
            catalog = "warehouse"
        else:
            alt = os.path.join(WAREHOUSE_DIR, table_name)
            if os.path.exists(alt):
                target_path = alt
                catalog = "warehouse"
            else:
                for c in load_catalogs():
                    p = os.path.join(c["path"], schema_name, table_name)
                    if os.path.exists(p):
                        target_path = p
                        catalog = c["id"]
                        break

    if not target_path or not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail=f"Table {schema_name}.{table_name} not found in catalog '{catalog or 'warehouse'}'")

    try:
        dt = DeltaTable(target_path)
        fields = [f.name for f in dt.schema().fields]
        if fields == ["__duckrun_deleted__"] or "__duckrun_deleted__" in fields:
            raise HTTPException(status_code=404, detail=f"Table {schema_name}.{table_name} has been dropped")
    except HTTPException:
        raise
    except Exception:
        pass

    try:
        if version is not None:
            query = f"SELECT * FROM delta_scan('{target_path}', version => {version}) LIMIT {limit}"
        else:
            query = f"SELECT * FROM delta_scan('{target_path}') LIMIT {limit}"

        res = conn.sql(query)
        df = res.df()
        columns = [{"name": col, "type": str(df[col].dtype)} for col in df.columns]
        rows = [json_serializable_row(row) for row in df.to_dict(orient="records")]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class QueryRequest(BaseModel):
    query: str
    warehouse_id: Optional[str] = None
    catalog: Optional[str] = None
    saved_query_id: Optional[str] = None

@app.post("/api/sql/execute")
async def execute_sql(payload: QueryRequest, request: Request):
    query = payload.query.strip()
    if not query:
        return {"success": False, "error": "Empty query"}

    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    # Enforce zero-trust catalog permissions
    try:
        enforce_sql_permissions(query, current_user, action="READ")
    except HTTPException as e_perm:
        return {
            "success": False,
            "error": e_perm.detail,
            "elapsed_ms": 0,
            "warehouse_id": payload.warehouse_id or "wh_starter"
        }

    conn = get_duckrun_conn()
    wh = apply_warehouse_compute(conn, payload.warehouse_id)
    start_time = time.perf_counter()

    if payload.saved_query_id:
        try:
            from web.saved_queries import record_saved_query_run
            record_saved_query_run(payload.saved_query_id)
        except Exception as e_rec:
            logger.warning(f"Could not record run for saved query {payload.saved_query_id}: {e_rec}")

    # 1. Attempt Clustered Worker Dispatch (if warehouse defines an endpoint)
    remote_executed = False
    remote_data = None
    if wh and wh.get("endpoint"):
        try:
            import httpx
            endpoints_to_try = [wh["endpoint"]]
            if "compute-node-01" in wh["endpoint"]:
                endpoints_to_try.append("http://localhost:8001")
            elif "compute-node-02" in wh["endpoint"]:
                endpoints_to_try.append("http://localhost:8002")
            elif "compute-node-03" in wh["endpoint"]:
                endpoints_to_try.append("http://localhost:8003")

            for ep in endpoints_to_try:
                try:
                    with httpx.Client(timeout=45.0) as client:
                        resp = client.post(
                            f"{ep}/api/compute/execute",
                            json={
                                "query": query,
                                "warehouse_id": wh["id"],
                                "catalog": payload.catalog or "warehouse"
                            }
                        )
                    if resp.status_code == 200:
                        remote_data = resp.json()
                        remote_executed = True
                        break
                except Exception:
                    continue
        except Exception as e_rem:
            logger.warning(f"Remote compute node dispatch failed for {wh.get('endpoint')}: {e_rem}")

    if remote_executed and remote_data is not None:
        elapsed_ms = remote_data.get("elapsed_ms", round((time.perf_counter() - start_time) * 1000, 2))
        node_id = remote_data.get("executed_by", "compute-worker")
        if remote_data.get("success"):
            try:
                from web.lineage import record_query_lineage
                record_query_lineage(query, client="SQL_EDITOR")
            except Exception:
                pass
            qid = log_query(
                query_text=query,
                duration_ms=elapsed_ms,
                rows_produced=remote_data.get("row_count", 0),
                status="SUCCESS",
                client="SQL_EDITOR",
                is_mutation=remote_data.get("is_mutation", False),
                warehouse_id=wh["id"],
                catalog=payload.catalog or "warehouse",
                user=current_user.get("username", "admin"),
                executed_by=node_id
            )
            remote_data["query_id"] = qid
            remote_data["warehouse_name"] = wh["name"]
            remote_data["cluster_size"] = wh.get("cluster_size", "Small")
            return remote_data
        else:
            qid = log_query(
                query_text=query,
                duration_ms=elapsed_ms,
                rows_produced=0,
                status="FAILED",
                error_message=remote_data.get("error", "Worker execution error"),
                client="SQL_EDITOR",
                warehouse_id=wh["id"],
                catalog=payload.catalog or "warehouse",
                user=current_user.get("username", "admin"),
                executed_by=node_id
            )
            remote_data["query_id"] = qid
            return remote_data

    # 2. Local In-Process DuckDB Execution (Fallback or default)
    fallback_note = "local-studio (worker offline)" if (wh and wh.get("endpoint")) else "local-studio"
    try:
        res = conn.sql(query)
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        try:
            from web.lineage import record_query_lineage
            record_query_lineage(query, client="SQL_EDITOR")
        except Exception:
            pass

        if res is not None and hasattr(res, "df"):
            df = res.df()
            columns = [{"name": col, "type": str(df[col].dtype)} for col in df.columns]
            rows = [json_serializable_row(row) for row in df.to_dict(orient="records")]
            qid = log_query(
                query_text=query,
                duration_ms=elapsed_ms,
                rows_produced=len(rows),
                status="SUCCESS",
                client="SQL_EDITOR",
                is_mutation=False,
                warehouse_id=wh["id"],
                catalog=payload.catalog or "warehouse",
                user=current_user.get("username", "admin"),
                executed_by=fallback_note
            )
            return {
                "success": True,
                "query_id": qid,
                "is_mutation": False,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "elapsed_ms": elapsed_ms,
                "warehouse_id": wh["id"],
                "warehouse_name": wh["name"],
                "cluster_size": wh.get("cluster_size", "Small"),
                "executed_by": fallback_note
            }
        else:
            try:
                conn.refresh()
            except Exception:
                pass
            qid = log_query(
                query_text=query,
                duration_ms=elapsed_ms,
                rows_produced=0,
                status="SUCCESS",
                client="SQL_EDITOR",
                is_mutation=True,
                warehouse_id=wh["id"],
                catalog=payload.catalog or "warehouse",
                user=current_user.get("username", "admin"),
                executed_by=fallback_note
            )
            return {
                "success": True,
                "query_id": qid,
                "is_mutation": True,
                "message": "Statement executed and committed successfully.",
                "elapsed_ms": elapsed_ms,
                "row_count": 0,
                "warehouse_id": wh["id"],
                "warehouse_name": wh["name"],
                "cluster_size": wh.get("cluster_size", "Small"),
                "executed_by": fallback_note
            }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        qid = log_query(
            query_text=query,
            duration_ms=elapsed_ms,
            rows_produced=0,
            status="FAILED",
            error_message=str(e),
            client="SQL_EDITOR",
            warehouse_id=wh["id"] if 'wh' in locals() and wh else "wh_starter",
            catalog=payload.catalog or "warehouse",
            user=current_user.get("username", "admin"),
            executed_by=fallback_note
        )
        return {
            "success": False,
            "query_id": qid,
            "error": str(e),
            "elapsed_ms": elapsed_ms,
            "warehouse_id": wh["id"] if 'wh' in locals() and wh else "wh_starter",
            "executed_by": fallback_note
        }

@app.post("/api/sql/profile")
async def profile_sql(payload: QueryRequest, request: Request):
    query = payload.query.strip()
    if not query:
        return {"success": False, "error": "Empty query"}

    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    # Enforce zero-trust catalog permissions
    try:
        enforce_sql_permissions(query, current_user, action="READ")
    except HTTPException as e_perm:
        return {
            "success": False,
            "error": e_perm.detail,
            "elapsed_ms": 0,
            "warehouse_id": payload.warehouse_id or "wh_starter"
        }

    conn = get_duckrun_conn()
    wh = apply_warehouse_compute(conn, payload.warehouse_id)
    start_time = time.perf_counter()

    try:
        res = execute_profiled_query(conn, query)
        elapsed_ms = res["elapsed_ms"]
        profile_obj = res.get("profile")
        profile_json_str = json.dumps(profile_obj) if profile_obj else None

        if res["success"]:
            try:
                from web.lineage import record_query_lineage
                record_query_lineage(query, client="SQL_EDITOR")
            except Exception:
                pass

            qid = log_query(
                query_text=query,
                duration_ms=elapsed_ms,
                rows_produced=res.get("row_count", 0),
                status="SUCCESS",
                client="SQL_EDITOR",
                is_mutation=False,
                warehouse_id=wh["id"],
                catalog=payload.catalog or "warehouse",
                profile_json=profile_json_str,
                user=current_user.get("username", "admin")
            )
            return {
                "success": True,
                "query_id": qid,
                "columns": res.get("columns", []),
                "rows": res.get("rows", []),
                "row_count": res.get("row_count", 0),
                "elapsed_ms": elapsed_ms,
                "profile": profile_obj,
                "warehouse_id": wh["id"],
                "warehouse_name": wh["name"],
                "cluster_size": wh.get("cluster_size", "Small")
            }
        else:
            qid = log_query(
                query_text=query,
                duration_ms=elapsed_ms,
                rows_produced=0,
                status="FAILED",
                error_message=res.get("error", "Execution failed"),
                client="SQL_EDITOR",
                warehouse_id=wh["id"],
                catalog=payload.catalog or "warehouse"
            )
            return {
                "success": False,
                "query_id": qid,
                "error": res.get("error", "Execution failed"),
                "elapsed_ms": elapsed_ms,
                "profile": None
            }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {"success": False, "error": str(e), "elapsed_ms": elapsed_ms, "profile": None}


# ==================== MONACO AUTOCOMPLETE & INLINE COPILOT ====================

class CopilotGenerateRequest(BaseModel):
    prompt: str
    current_query: Optional[str] = None
    selection: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None

@app.get("/api/sql/autocomplete-metadata")
async def get_autocomplete_metadata_api():
    from web.copilot import get_autocomplete_metadata
    conn = get_duckrun_conn()
    return get_autocomplete_metadata(conn)

@app.post("/api/sql/copilot/generate")
async def generate_copilot_sql_api(payload: CopilotGenerateRequest):
    prompt = payload.prompt.strip()
    if not prompt:
        return {"success": False, "error": "Prompt cannot be empty"}
    from web.copilot import generate_copilot_sql
    conn = get_duckrun_conn()
    return generate_copilot_sql(
        prompt=prompt,
        current_query=payload.current_query,
        selection=payload.selection,
        provider=payload.provider,
        model=payload.model,
        conn=conn
    )

@app.get("/api/sql/copilot/providers")
async def get_copilot_providers_api():
    from web.genie import get_available_providers
    return get_available_providers()


@app.get("/api/workspace/files")
async def get_workspace_files():
    files = []
    if os.path.exists(NOTEBOOKS_DIR):
        for name in sorted(os.listdir(NOTEBOOKS_DIR)):
            full = os.path.join(NOTEBOOKS_DIR, name)
            if os.path.isfile(full):
                files.append({
                    "name": name,
                    "size_bytes": os.path.getsize(full),
                    "modified": datetime.datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M"),
                    "is_notebook": name.endswith(".ipynb"),
                    "url": f"http://localhost:{JUPYTER_PORT}/lab/tree/notebooks/{name}?token={JUPYTER_TOKEN}"
                })
    return {"files": files}

# ==================== DATA INGESTION APIS ====================

def get_source_sql_for_file(filepath: str, ext: str) -> str:
    ext_clean = ext.lower().lstrip(".")
    if ext_clean in ["csv", "tsv"]:
        return f"read_csv_auto('{filepath}', header=true)"
    elif ext_clean in ["parquet", "pq", "parq"]:
        return f"read_parquet('{filepath}')"
    elif ext_clean in ["json", "jsonl", "ndjson"]:
        return f"read_json_auto('{filepath}')"
    else:
        raise ValueError(f"Unsupported file format '.{ext_clean}'. Supported formats: CSV, TSV, Parquet (.parquet, .pq, .parq), JSON (.json, .jsonl, .ndjson).")

def normalize_uploaded_file(temp_path: str, ext: str, conn: Any) -> None:
    """
    Detects if an uploaded Parquet file erroneously contains a single string column
    whose column name and data rows are actually delimited (CSV/TSV/etc.) lines.
    If detected, automatically unpacks it into a proper multi-column Parquet file.
    """
    ext_clean = ext.lower().lstrip(".")
    if ext_clean not in ["parquet", "pq", "parq"]:
        return

    try:
        df_sample = conn.sql(f"SELECT * FROM read_parquet('{temp_path}') LIMIT 1").df()
        if len(df_sample.columns) != 1:
            return

        col_name = str(df_sample.columns[0])
        first_val = str(df_sample.iloc[0, 0]) if len(df_sample) > 0 else ""

        delim_found = None
        for d in [",", ";", "\t", "|"]:
            if col_name.count(d) >= 1 and first_val.count(d) >= 1:
                delim_found = d
                break

        if not delim_found:
            return

        logger.info(f"Auto-detect: Single-column Parquet '{temp_path}' contains delimited data (delim={repr(delim_found)}). Unpacking into multi-column dataset...")
        csv_temp = temp_path + ".unpack.csv"
        body_temp = temp_path + ".unpack.body"
        fixed_parquet = temp_path + ".unpacked.parquet"

        try:
            conn.sql(f"""
                COPY (SELECT * FROM read_parquet('{temp_path}'))
                TO '{body_temp}' (HEADER FALSE, DELIMITER '\n', QUOTE '', ESCAPE '')
            """)
            with open(csv_temp, "w", encoding="utf-8", errors="replace") as out_f:
                out_f.write(col_name + "\n")
                with open(body_temp, "r", encoding="utf-8", errors="replace") as in_f:
                    shutil.copyfileobj(in_f, out_f)

            csv_desc = conn.sql(f"DESCRIBE SELECT * FROM read_csv_auto('{csv_temp}')").df()
            time_cols = csv_desc[csv_desc['column_type'].str.upper().str.contains('TIME') & ~csv_desc['column_type'].str.upper().str.contains('TIMESTAMP')]['column_name'].tolist()
            if time_cols:
                escaped = [f'"{c}"::VARCHAR AS "{c}"' for c in time_cols]
                select_expr = f"SELECT * REPLACE ({', '.join(escaped)}) FROM read_csv_auto('{csv_temp}')"
            else:
                select_expr = f"SELECT * FROM read_csv_auto('{csv_temp}')"

            conn.sql(f"""
                COPY ({select_expr})
                TO '{fixed_parquet}' (FORMAT PARQUET)
            """)

            if os.path.exists(fixed_parquet):
                os.replace(fixed_parquet, temp_path)
                logger.info(f"Successfully unpacked '{temp_path}' into multi-column Parquet file.")
        finally:
            for p in [csv_temp, body_temp, fixed_parquet]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"Could not auto-unpack single-column Parquet: {e}")

def get_delta_compatible_source_sql(conn: Any, source_sql: str) -> str:
    """
    Checks if any columns in source_sql are of type TIME (which Delta Lake protocol rejects).
    Casts them to VARCHAR so delta-rs can serialize them cleanly into Lakehouse tables.
    """
    try:
        desc_df = conn.sql(f"DESCRIBE SELECT * FROM {source_sql}").df()
        time_cols = desc_df[desc_df['column_type'].str.upper().str.contains('TIME') & ~desc_df['column_type'].str.upper().str.contains('TIMESTAMP')]['column_name'].tolist()
        if time_cols:
            escaped = [f'"{c}"::VARCHAR AS "{c}"' for c in time_cols]
            return f"(SELECT * REPLACE ({', '.join(escaped)}) FROM {source_sql})"
    except Exception as e:
        logger.warning(f"Could not inspect/cast TIME columns in source SQL: {e}")
    return source_sql

def sanitize_identifier(name: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    s = re.sub(r'_+', '_', s).strip('_')
    if not s or s[0].isdigit():
        s = 'table_' + s
    return s

@app.post("/api/ingest/preview")
async def ingest_preview(file: UploadFile = File(...)):
    filename = file.filename or "uploaded_data.csv"
    _, ext = os.path.splitext(filename)
    if not ext:
        ext = ".csv"

    file_id = f"upload_{uuid.uuid4().hex[:12]}{ext}"
    temp_path = os.path.join(UPLOADS_DIR, file_id)

    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store uploaded file: {str(e)}")
    finally:
        await file.close()

    # Use DuckDB to inspect schema and sample rows
    conn = get_duckrun_conn()
    try:
        normalize_uploaded_file(temp_path, ext, conn)
        file_size = int(os.path.getsize(temp_path))
        source_sql = get_source_sql_for_file(temp_path, ext)
        source_sql = get_delta_compatible_source_sql(conn, source_sql)
        res = conn.sql(f"SELECT * FROM {source_sql} LIMIT 25")
        df = res.df()

        # Get total row count estimate
        count_res = conn.sql(f"SELECT COUNT(*) FROM {source_sql}").fetchone()
        total_rows = int(count_res[0]) if count_res else int(len(df))

        columns = [{"name": str(col), "type": str(df[col].dtype)} for col in df.columns]
        sample_rows = [json_serializable_row(row) for row in df.to_dict(orient="records")]

        suggested_name = sanitize_identifier(os.path.splitext(filename)[0])

        return {
            "file_id": file_id,
            "filename": filename,
            "extension": ext.lower().lstrip("."),
            "file_size_bytes": file_size,
            "suggested_table_name": suggested_name,
            "total_rows": total_rows,
            "columns": columns,
            "sample_rows": sample_rows
        }
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.exception("Failed to parse uploaded file for preview")
        raise HTTPException(status_code=400, detail=f"Failed to parse data: {str(e)}")

class IngestCommitRequest(BaseModel):
    file_id: str
    catalog: Optional[str] = "warehouse"
    schema_name: str = "dbo"
    table_name: str
    mode: str = "overwrite" # overwrite | append

@app.post("/api/ingest/create")
async def ingest_create(payload: IngestCommitRequest, request: Request):
    temp_path = os.path.join(UPLOADS_DIR, payload.file_id)
    if not os.path.exists(temp_path):
        raise HTTPException(status_code=404, detail="Uploaded file session expired or not found. Please upload again.")

    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    target_catalog = payload.catalog or "warehouse"
    if not can_user_access_catalog(current_user, target_catalog, action="WRITE"):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: User '{current_user.get('username')}' does not have write/ingest permissions on catalog '{target_catalog}'."
        )

    _, ext = os.path.splitext(payload.file_id)
    conn = get_duckrun_conn()
    normalize_uploaded_file(temp_path, ext, conn)
    try:
        source_sql = get_source_sql_for_file(temp_path, ext)
        source_sql = get_delta_compatible_source_sql(conn, source_sql)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    schema_clean = sanitize_identifier(payload.schema_name or "dbo")
    table_clean = sanitize_identifier(payload.table_name)
    target_catalog = payload.catalog or "warehouse"
    start_time = time.perf_counter()

    if target_catalog != "warehouse":
        cat = get_catalog(target_catalog)
        if not cat:
            raise HTTPException(status_code=404, detail=f"Catalog '{target_catalog}' not found.")

        if cat.get("is_mounted"):
            if cat.get("read_only", False):
                raise HTTPException(status_code=400, detail=f"Mounted catalog '{cat['name']}' is configured as read-only. Ingestion is not permitted.")

            m_type = cat.get("type")
            if m_type == "s3":
                from web.mounts import get_s3_storage_options, attach_mount_to_duckdb
                from deltalake import write_deltalake, DeltaTable

                cfg = cat.get("config", {})
                bucket = cfg.get("bucket", "localspark")
                storage_options = get_s3_storage_options(cfg)

                s3_table_uri = f"s3://{bucket}/{schema_clean}/{table_clean}"
                target_rel = f"{cat['id']}.{schema_clean}.{table_clean}"

                # Materialize from DuckDB query to Arrow table
                arrow_table = conn.sql(f"SELECT * FROM {source_sql}").arrow().read_all()
                row_count = arrow_table.num_rows

                mode = "append" if payload.mode.lower() == "append" else "overwrite"
                schema_mode = "merge" if mode == "append" else "overwrite"
                write_deltalake(s3_table_uri, arrow_table, storage_options=storage_options, mode=mode, schema_mode=schema_mode)

                dt = DeltaTable(s3_table_uri, storage_options=storage_options)
                version = dt.version()
                elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

                # Remove temp uploaded file
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass

                # Ensure mount secret attached to DuckDB and create a convenient view
                attach_mount_to_duckdb(conn, cat)
                clean_view = f"{target_catalog}_{schema_clean}_{table_clean}".replace("-", "_").replace(".", "_")
                try:
                    conn.sql(f"CREATE OR REPLACE VIEW {clean_view} AS SELECT * FROM delta_scan('{s3_table_uri}')")
                except Exception as e:
                    logger.debug(f"Notice registering S3 delta view: {e}")

                query = f"-- Materialized Delta Lake Table in S3: {s3_table_uri}\nCREATE OR REPLACE TABLE {target_rel} (Delta v{version}, {row_count} rows)"
                log_query(
                    query_text=query,
                    duration_ms=elapsed_ms,
                    rows_produced=row_count,
                    status="SUCCESS",
                    client="INGESTION",
                    is_mutation=True,
                    catalog=target_catalog
                )

                try:
                    from web.lineage import upsert_node, upsert_edge, make_table_id
                    f_name = os.path.basename(payload.file_id)
                    f_id = f"file:{f_name}"
                    upsert_node(f_id, f_name, "FILE", layer="RAW_FILE")
                    t_id = make_table_id(target_catalog, schema_clean, table_clean)
                    upsert_node(t_id, table_clean, "TABLE", catalog=target_catalog, schema_name=schema_clean)
                    upsert_edge(f_id, t_id, edge_type="INGESTS_TO")
                except Exception:
                    pass

                return {
                    "success": True,
                    "catalog": target_catalog,
                    "schema_name": schema_clean,
                    "table_name": table_clean,
                    "full_name": target_rel,
                    "location": s3_table_uri,
                    "version": version,
                    "rows_ingested": row_count,
                    "elapsed_ms": elapsed_ms,
                    "message": f"Successfully created Delta table {target_rel} on S3 bucket '{bucket}' with {row_count} rows (Version {version})"
                }
            else:
                raise HTTPException(status_code=400, detail=f"Ingestion into external mount type '{m_type}' is not supported for Delta table creation.")

        target_rel = f"{cat['id']}.{schema_clean}.{table_clean}"
        dt_path = os.path.join(cat["path"], schema_clean, table_clean)
        os.makedirs(os.path.join(cat["path"], schema_clean), exist_ok=True)
    else:
        target_rel = f"{schema_clean}.{table_clean}"
        dt_path = os.path.join(WAREHOUSE_DIR, schema_clean, table_clean)

    conn = get_duckrun_conn()
    start_time = time.perf_counter()

    try:
        if payload.mode.lower() == "append":
            query = f"INSERT INTO {target_rel} SELECT * FROM {source_sql}"
        else:
            query = f"CREATE OR REPLACE TABLE {target_rel} AS SELECT * FROM {source_sql}"

        conn.sql(query)
        conn.refresh()

        # Verify created table
        if not os.path.exists(dt_path):
            if target_catalog == "warehouse":
                dt_path = os.path.join(WAREHOUSE_DIR, table_clean)

        dt = DeltaTable(dt_path)
        version = dt.version()
        row_count = conn.sql(f"SELECT COUNT(*) FROM delta_scan('{dt_path}')").fetchone()[0]
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # Remove temp uploaded file
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        log_query(
            query_text=query,
            duration_ms=elapsed_ms,
            rows_produced=row_count,
            status="SUCCESS",
            client="INGESTION",
            is_mutation=True,
            catalog=target_catalog
        )

        try:
            from web.lineage import upsert_node, upsert_edge, make_table_id
            f_name = os.path.basename(payload.file_id)
            f_id = f"file:{f_name}"
            upsert_node(f_id, f_name, "FILE", layer="RAW_FILE")
            t_id = make_table_id(target_catalog, schema_clean, table_clean)
            upsert_node(t_id, table_clean, "TABLE", catalog=target_catalog, schema_name=schema_clean)
            upsert_edge(f_id, t_id, edge_type="INGESTS_TO")
        except Exception:
            pass

        return {
            "success": True,
            "catalog": target_catalog,
            "schema_name": schema_clean,
            "table_name": table_clean,
            "full_name": target_rel,
            "version": version,
            "rows_ingested": row_count,
            "elapsed_ms": elapsed_ms,
            "message": f"Successfully created Delta table {target_rel} in catalog '{target_catalog}' with {row_count} rows (Version {version})"
        }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        log_query(
            query_text=query if 'query' in locals() else f"Ingest {payload.file_id}",
            duration_ms=elapsed_ms,
            rows_produced=0,
            status="FAILED",
            error_message=str(e),
            client="INGESTION",
            is_mutation=True,
            catalog=target_catalog
        )
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(e)}")

# ==================== LAKEVIEW DASHBOARDS APIS ====================

# Dashboards logic and store managed via web.dashboards

@app.get("/api/dashboards")
async def list_dashboards():
    dashboards = load_dashboards_store()
    return {
        "dashboards": [
            {
                "id": d["id"],
                "name": d["name"],
                "description": d.get("description", ""),
                "created_at": d.get("created_at", ""),
                "widget_count": len(d.get("widgets", []))
            }
            for d in dashboards
        ]
    }

@app.get("/api/dashboards/{dashboard_id}")
async def get_dashboard(dashboard_id: str, params: Optional[str] = None):
    dashboards = load_dashboards_store()
    target = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not target:
        if dashboards:
            target = dashboards[0]
        else:
            raise HTTPException(status_code=404, detail="Dashboard not found")

    parsed_params = {}
    if params:
        try:
            parsed_params = json.loads(params)
        except Exception:
            pass

    conn = get_duckrun_conn()
    filter_data = get_dashboard_filter_options(conn, target["id"])
    hydrated_widgets = []
    for w in target.get("widgets", []):
        w_copy = dict(w)
        exec_res = execute_widget_query(conn, w["query"], parsed_params)
        w_copy["result"] = exec_res
        hydrated_widgets.append(w_copy)

    return {
        "id": target["id"],
        "name": target["name"],
        "description": target.get("description", ""),
        "created_at": target.get("created_at", ""),
        "filters": filter_data.get("filters", []),
        "filter_options": filter_data.get("options", {}),
        "widgets": hydrated_widgets
    }

class DashboardQueryParams(BaseModel):
    parameters: Optional[Dict[str, Any]] = {}

@app.post("/api/dashboards/{dashboard_id}/query")
async def query_dashboard(dashboard_id: str, payload: DashboardQueryParams):
    dashboards = load_dashboards_store()
    target = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    conn = get_duckrun_conn()
    hydrated_widgets = []
    for w in target.get("widgets", []):
        w_copy = dict(w)
        exec_res = execute_widget_query(conn, w["query"], payload.parameters or {})
        w_copy["result"] = exec_res
        hydrated_widgets.append(w_copy)

    return {
        "id": target["id"],
        "widgets": hydrated_widgets
    }

@app.get("/api/dashboards/{dashboard_id}/filters")
async def get_dashboard_filters(dashboard_id: str):
    conn = get_duckrun_conn()
    return get_dashboard_filter_options(conn, dashboard_id)

class CreateDashboardRequest(BaseModel):
    name: str
    description: Optional[str] = ""

@app.post("/api/dashboards")
async def create_dashboard(payload: CreateDashboardRequest):
    dashboards = load_dashboards_store()
    new_id = f"dash_{uuid.uuid4().hex[:10]}"
    new_dash = {
        "id": new_id,
        "name": payload.name.strip() or "Untitled Dashboard",
        "description": payload.description or "",
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "widgets": []
    }
    dashboards.append(new_dash)
    save_dashboards_store(dashboards)
    return new_dash

@app.delete("/api/dashboards/{dashboard_id}")
async def delete_dashboard(dashboard_id: str):
    dashboards = load_dashboards_store()
    dashboards = [d for d in dashboards if d["id"] != dashboard_id]
    save_dashboards_store(dashboards)
    return {"success": True, "deleted_id": dashboard_id}

class WidgetPayload(BaseModel):
    title: str
    type: str  # kpi | big_number_trendline | bar | line | area | pie | scatter | bubble | mixed | waterfall | funnel | gauge | heatmap | treemap | sunburst | boxplot | radar | gantt | graph | tree | table | pivot_table | world_map
    query: str
    x_col: Optional[str] = None
    y_col: Optional[str] = None
    z_col: Optional[str] = None
    unit: Optional[str] = ""
    width: Optional[str] = "col-span-2"
    query_source: Optional[str] = "custom"
    saved_query_id: Optional[str] = None
    history_query_id: Optional[str] = None

@app.post("/api/dashboards/{dashboard_id}/widgets")
async def add_widget(dashboard_id: str, payload: WidgetPayload):
    dashboards = load_dashboards_store()
    target = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    widget_id = f"w_{uuid.uuid4().hex[:8]}"
    default_w = "col-span-1" if payload.type in ["kpi", "big_number_trendline"] else ("col-span-4" if payload.type in ["table", "pivot_table", "world_map"] else "col-span-2")
    new_widget = {
        "id": widget_id,
        "title": payload.title.strip() or "New Metric",
        "type": payload.type,
        "query": payload.query.strip(),
        "x_col": payload.x_col,
        "y_col": payload.y_col,
        "z_col": payload.z_col,
        "unit": payload.unit or "",
        "width": payload.width or default_w,
        "query_source": payload.query_source or "custom",
        "saved_query_id": payload.saved_query_id,
        "history_query_id": payload.history_query_id
    }

    if "widgets" not in target:
        target["widgets"] = []
    target["widgets"].append(new_widget)
    save_dashboards_store(dashboards)

    conn = get_duckrun_conn()
    new_widget_copy = dict(new_widget)
    new_widget_copy["result"] = execute_widget_query(conn, new_widget["query"])
    return new_widget_copy

@app.delete("/api/dashboards/{dashboard_id}/widgets/{widget_id}")
async def delete_widget(dashboard_id: str, widget_id: str):
    dashboards = load_dashboards_store()
    target = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    target["widgets"] = [w for w in target.get("widgets", []) if w["id"] != widget_id]
    save_dashboards_store(dashboards)
    return {"success": True, "deleted_widget_id": widget_id}

@app.get("/api/dashboards/{dashboard_id}/widgets/{widget_id}/export")
async def export_widget(dashboard_id: str, widget_id: str, format: str = "csv", params: Optional[str] = None):
    """
    Export widget data in CSV or Parquet format.
    Params:
      - format: 'csv' or 'parquet'
      - params: JSON string of filter parameters
    """
    from fastapi.responses import StreamingResponse
    import io

    dashboards = load_dashboards_store()
    target_dashboard = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not target_dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    target_widget = next((w for w in target_dashboard.get("widgets", []) if w["id"] == widget_id), None)
    if not target_widget:
        raise HTTPException(status_code=404, detail="Widget not found")

    # Parse filter parameters
    filter_params = {}
    if params:
        try:
            filter_params = json.loads(params)
        except:
            filter_params = {}

    # Execute the query
    conn = get_duckrun_conn()
    query = target_widget.get("query", "")
    result = execute_widget_query(conn, query, filter_params)

    if not result.get("success"):
        raise HTTPException(status_code=500, detail=f"Query failed: {result.get('error')}")

    # Convert to DataFrame
    rows = result.get("rows", [])
    if not rows:
        raise HTTPException(status_code=404, detail="No data to export")

    df = pd.DataFrame(rows)

    # Export based on format
    if format.lower() == "csv":
        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)

        filename = f"{widget_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode('utf-8')),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    elif format.lower() == "parquet":
        output = io.BytesIO()
        df.to_parquet(output, index=False, engine='pyarrow')
        output.seek(0)

        filename = f"{widget_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
        return StreamingResponse(
            output,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {format}. Use 'csv' or 'parquet'")

@app.post("/api/dashboards/cache/clear")
async def clear_dashboard_cache(current_user: dict = Depends(get_current_user)):
    """Clear all cached dashboard query results."""
    from web.dashboards import clear_query_cache, QUERY_CACHE
    cache_size = len(QUERY_CACHE)
    clear_query_cache()
    return {"success": True, "message": f"Cleared {cache_size} cached queries"}

@app.get("/api/dashboards/cache/stats")
async def get_cache_stats(current_user: dict = Depends(get_current_user)):
    """Get query cache statistics."""
    from web.dashboards import QUERY_CACHE, CACHE_TTL_SECONDS, MAX_CACHE_SIZE
    return {
        "cached_queries": len(QUERY_CACHE),
        "max_cache_size": MAX_CACHE_SIZE,
        "default_ttl_seconds": CACHE_TTL_SECONDS
    }

@app.post("/api/dashboards/{dashboard_id}/share")
async def create_share_link(dashboard_id: str, expires_in_days: int = 30, current_user: dict = Depends(get_current_user)):
    """Create a shareable link for a dashboard."""
    from web.dashboards import create_dashboard_share, load_dashboards_store

    # Verify dashboard exists
    dashboards = load_dashboards_store()
    dashboard = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    share_token = create_dashboard_share(dashboard_id, current_user["username"], expires_in_days)

    return {
        "success": True,
        "share_token": share_token,
        "share_url": f"/share/{share_token}",
        "expires_in_days": expires_in_days
    }

@app.get("/api/dashboards/shares")
async def list_shares(dashboard_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    """List all dashboard shares."""
    from web.dashboards import list_dashboard_shares
    shares = list_dashboard_shares(dashboard_id)
    return {"success": True, "shares": shares}

@app.delete("/api/dashboards/share/{share_token}")
async def revoke_share(share_token: str, current_user: dict = Depends(get_current_user)):
    """Revoke a dashboard share."""
    from web.dashboards import revoke_dashboard_share
    success = revoke_dashboard_share(share_token)
    if not success:
        raise HTTPException(status_code=404, detail="Share token not found")
    return {"success": True, "message": "Share revoked"}

@app.get("/share/{share_token}")
async def view_shared_dashboard(share_token: str):
    """View a shared dashboard (no auth required)."""
    from web.dashboards import get_dashboard_by_share_token
    dashboard = get_dashboard_by_share_token(share_token)

    if not dashboard:
        raise HTTPException(status_code=404, detail="Share link not found or expired")

    # Return the main index page with share token in URL
    # The frontend will detect the share token and load the dashboard
    with open("/workspace/web/templates/index.html", "r") as f:
        html_content = f.read()

    return HTMLResponse(content=html_content)

@app.get("/api/dashboards/templates")
async def list_dashboard_templates(current_user: dict = Depends(get_current_user)):
    """List all available dashboard templates."""
    from web.dashboard_templates import get_all_templates
    templates = get_all_templates()
    return {"success": True, "templates": templates}

@app.get("/api/dashboards/templates/{template_id}")
async def get_dashboard_template(template_id: str, current_user: dict = Depends(get_current_user)):
    """Get a specific dashboard template."""
    from web.dashboard_templates import get_template_by_id
    template = get_template_by_id(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"success": True, "template": template}

@app.post("/api/dashboards/from-template/{template_id}")
async def create_dashboard_from_template(
    template_id: str,
    dashboard_name: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Create a new dashboard from a template."""
    from web.dashboard_templates import instantiate_template
    from web.dashboards import load_dashboards_store, save_dashboards_store

    dashboard = instantiate_template(template_id, dashboard_name)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Template not found")

    # Add to dashboards store
    dashboards = load_dashboards_store()
    dashboards.append(dashboard)
    save_dashboards_store(dashboards)

    return {
        "success": True,
        "dashboard_id": dashboard["id"],
        "message": f"Dashboard created from template: {dashboard['name']}"
    }

@app.post("/api/dashboards/{dashboard_id}/version")
async def create_dashboard_version(
    dashboard_id: str,
    comment: str = "",
    current_user: dict = Depends(get_current_user)
):
    """Create a version snapshot of a dashboard."""
    from web.dashboards import load_dashboards_store
    from web.dashboard_versions import create_version

    dashboards = load_dashboards_store()
    dashboard = next((d for d in dashboards if d["id"] == dashboard_id), None)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    version_id = create_version(dashboard_id, dashboard, current_user["username"], comment)
    return {"success": True, "version_id": version_id}

@app.get("/api/dashboards/{dashboard_id}/versions")
async def list_dashboard_versions(dashboard_id: str, current_user: dict = Depends(get_current_user)):
    """List all versions of a dashboard."""
    from web.dashboard_versions import list_versions
    versions = list_versions(dashboard_id)
    return {"success": True, "versions": versions}

@app.post("/api/dashboards/{dashboard_id}/restore/{version_id}")
async def restore_dashboard_version(
    dashboard_id: str,
    version_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Restore a dashboard to a specific version."""
    from web.dashboard_versions import restore_version
    from web.dashboards import load_dashboards_store, save_dashboards_store

    dashboard_config = restore_version(dashboard_id, version_id)
    if not dashboard_config:
        raise HTTPException(status_code=404, detail="Version not found")

    dashboards = load_dashboards_store()
    for i, d in enumerate(dashboards):
        if d["id"] == dashboard_id:
            dashboards[i] = dashboard_config
            break

    save_dashboards_store(dashboards)
    return {"success": True, "message": "Dashboard restored"}

@app.get("/api/themes")
async def list_themes():
    """List all available themes."""
    from web.dashboard_themes import get_all_themes
    themes = get_all_themes()
    return {"success": True, "themes": themes}

@app.get("/api/themes/{theme_id}")
async def get_theme(theme_id: str):
    """Get a specific theme."""
    from web.dashboard_themes import get_theme as get_theme_data
    theme = get_theme_data(theme_id)
    if not theme:
        raise HTTPException(status_code=404, detail="Theme not found")
    return {"success": True, "theme": theme}

@app.get("/api/folders")
async def list_folders(parent_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    """List dashboard folders."""
    from web.dashboard_folders import list_folders as list_folders_func
    folders = list_folders_func(parent_id)
    return {"success": True, "folders": folders}

@app.get("/api/folders/tree")
async def get_folder_tree(current_user: dict = Depends(get_current_user)):
    """Get folder hierarchy tree."""
    from web.dashboard_folders import get_folder_tree
    tree = get_folder_tree()
    return {"success": True, "tree": tree}

@app.post("/api/folders")
async def create_folder(name: str, parent_id: str = "folder_root", icon: str = "folder", current_user: dict = Depends(get_current_user)):
    """Create a new folder."""
    from web.dashboard_folders import create_folder as create_folder_func
    folder_id = create_folder_func(name, parent_id, icon, current_user["username"])
    return {"success": True, "folder_id": folder_id}

@app.post("/api/dashboards/{dashboard_id}/move")
async def move_dashboard(dashboard_id: str, folder_id: str, current_user: dict = Depends(get_current_user)):
    """Move dashboard to a folder."""
    from web.dashboard_folders import move_dashboard_to_folder
    success = move_dashboard_to_folder(dashboard_id, folder_id)
    if not success:
        raise HTTPException(status_code=404, detail="Folder not found")
    return {"success": True}

@app.get("/api/widgets/{widget_id}/comments")
async def list_widget_comments(widget_id: str, current_user: dict = Depends(get_current_user)):
    """List comments on a widget."""
    from web.widget_comments import list_comments
    comments = list_comments(widget_id)
    return {"success": True, "comments": comments}

@app.post("/api/widgets/{widget_id}/comments")
async def add_widget_comment(widget_id: str, text: str, current_user: dict = Depends(get_current_user)):
    """Add a comment to a widget."""
    from web.widget_comments import add_comment
    comment_id = add_comment(widget_id, text, current_user["username"])
    return {"success": True, "comment_id": comment_id}

@app.put("/api/widgets/{widget_id}/comments/{comment_id}")
async def update_widget_comment(widget_id: str, comment_id: str, text: str, current_user: dict = Depends(get_current_user)):
    """Update a comment."""
    from web.widget_comments import update_comment
    success = update_comment(widget_id, comment_id, text, current_user["username"])
    if not success:
        raise HTTPException(status_code=404, detail="Comment not found")
    return {"success": True}

@app.delete("/api/widgets/{widget_id}/comments/{comment_id}")
async def delete_widget_comment(widget_id: str, comment_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a comment."""
    from web.widget_comments import delete_comment
    success = delete_comment(widget_id, comment_id)
    if not success:
        raise HTTPException(status_code=404, detail="Comment not found")
    return {"success": True}

# ==================== SCHEDULED EXPORTS APIS ====================

@app.get("/api/schedules")
async def list_schedules(dashboard_id: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    """List all scheduled exports."""
    from web.scheduled_exports import list_schedules as list_schedules_func
    schedules = list_schedules_func(dashboard_id)
    return {"success": True, "schedules": schedules}

@app.get("/api/schedules/{schedule_id}")
async def get_schedule(schedule_id: str, current_user: dict = Depends(get_current_user)):
    """Get a specific scheduled export."""
    from web.scheduled_exports import get_schedule as get_schedule_func
    schedule = get_schedule_func(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"success": True, "schedule": schedule}

@app.post("/api/schedules")
async def create_schedule(
    dashboard_id: str,
    name: str,
    frequency: str,
    format: str = "csv",
    widget_ids: Optional[List[str]] = None,
    hour: int = 0,
    minute: int = 0,
    day_of_week: int = 0,
    day_of_month: int = 1,
    cron_expression: Optional[str] = None,
    enabled: bool = True,
    email_enabled: bool = False,
    email_recipients: Optional[List[str]] = None,
    email_cc: Optional[List[str]] = None,
    email_message: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Create a new scheduled export."""
    from web.scheduled_exports import create_schedule as create_schedule_func
    schedule_id = create_schedule_func(
        dashboard_id=dashboard_id,
        name=name,
        frequency=frequency,
        format=format,
        widget_ids=widget_ids,
        created_by=current_user["username"],
        hour=hour,
        minute=minute,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        cron_expression=cron_expression,
        enabled=enabled,
        email_enabled=email_enabled,
        email_recipients=email_recipients,
        email_cc=email_cc,
        email_message=email_message
    )
    return {"success": True, "schedule_id": schedule_id}

@app.put("/api/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    name: Optional[str] = None,
    frequency: Optional[str] = None,
    format: Optional[str] = None,
    widget_ids: Optional[List[str]] = None,
    enabled: Optional[bool] = None,
    hour: Optional[int] = None,
    minute: Optional[int] = None,
    day_of_week: Optional[int] = None,
    day_of_month: Optional[int] = None,
    cron_expression: Optional[str] = None,
    email_enabled: Optional[bool] = None,
    email_recipients: Optional[List[str]] = None,
    email_cc: Optional[List[str]] = None,
    email_message: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Update a scheduled export."""
    from web.scheduled_exports import update_schedule as update_schedule_func
    success = update_schedule_func(
        schedule_id=schedule_id,
        name=name,
        frequency=frequency,
        format=format,
        widget_ids=widget_ids,
        enabled=enabled,
        hour=hour,
        minute=minute,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        cron_expression=cron_expression,
        email_enabled=email_enabled,
        email_recipients=email_recipients,
        email_cc=email_cc,
        email_message=email_message
    )
    if not success:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"success": True}

@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a scheduled export."""
    from web.scheduled_exports import delete_schedule as delete_schedule_func
    success = delete_schedule_func(schedule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"success": True}

@app.post("/api/schedules/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: str, current_user: dict = Depends(get_current_user)):
    """Manually trigger a scheduled export."""
    from web.scheduled_exports import trigger_schedule_now
    success = trigger_schedule_now(schedule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"success": True, "message": "Export triggered"}

@app.get("/api/schedules/{schedule_id}/history")
async def get_schedule_history(schedule_id: str, limit: int = 10, current_user: dict = Depends(get_current_user)):
    """Get export history for a schedule."""
    from web.scheduled_exports import get_export_history
    history = get_export_history(schedule_id, limit)
    return {"success": True, "history": history}

# ==================== EMAIL REPORTS APIS ====================

@app.get("/api/email/config")
async def get_email_config(current_user: dict = Depends(require_role("admin"))):
    """Get email configuration (admin only)."""
    from web.email_reports import get_email_config
    config = get_email_config()
    return {"success": True, "config": config}

@app.post("/api/email/config")
async def update_email_config(
    smtp_server: Optional[str] = None,
    smtp_port: Optional[int] = None,
    use_tls: Optional[bool] = None,
    sender_email: Optional[str] = None,
    sender_password: Optional[str] = None,
    sender_name: Optional[str] = None,
    enabled: Optional[bool] = None,
    current_user: dict = Depends(require_role("admin"))
):
    """Update email configuration (admin only)."""
    from web.email_reports import update_email_config
    success = update_email_config(
        smtp_server=smtp_server,
        smtp_port=smtp_port,
        use_tls=use_tls,
        sender_email=sender_email,
        sender_password=sender_password,
        sender_name=sender_name,
        enabled=enabled
    )
    return {"success": success, "message": "Email configuration updated"}

@app.post("/api/email/test")
async def test_email_connection(current_user: dict = Depends(require_role("admin"))):
    """Test email connection (admin only)."""
    from web.email_reports import test_email_connection
    result = test_email_connection()
    return result

@app.post("/api/email/send")
async def send_email_report(
    recipients: List[str],
    subject: str,
    body: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    current_user: dict = Depends(get_current_user)
):
    """Send an email."""
    from web.email_reports import send_email
    result = send_email(
        recipients=recipients,
        subject=subject,
        body_html=body,
        cc=cc,
        bcc=bcc
    )
    return result

@app.post("/api/dashboards/{dashboard_id}/email")
async def email_dashboard_report(
    dashboard_id: str,
    recipients: List[str],
    include_attachments: bool = True,
    attachment_format: str = "csv",
    custom_message: Optional[str] = None,
    cc: Optional[List[str]] = None,
    current_user: dict = Depends(get_current_user)
):
    """Email a dashboard report."""
    from web.email_reports import send_dashboard_report
    from web.dashboards import load_dashboards_store

    # Get dashboard name
    dashboards = load_dashboards_store()
    dashboard = None
    for d in dashboards:
        if d["id"] == dashboard_id:
            dashboard = d
            break

    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    result = send_dashboard_report(
        dashboard_id=dashboard_id,
        dashboard_name=dashboard.get("name", "Dashboard"),
        recipients=recipients,
        include_attachments=include_attachments,
        attachment_format=attachment_format,
        custom_message=custom_message,
        cc=cc
    )
    return result

@app.get("/api/email/history")
async def get_email_history(limit: int = 50, current_user: dict = Depends(get_current_user)):
    """Get email send history."""
    from web.email_reports import get_email_history
    history = get_email_history(limit)
    return {"success": True, "history": history}

class PreviewWidgetRequest(BaseModel):
    query: str

@app.post("/api/dashboards/preview-widget")
async def preview_widget(payload: PreviewWidgetRequest):
    conn = get_duckrun_conn()
    exec_res = execute_widget_query(conn, payload.query.strip())
    return exec_res


# ==================== SAVED QUERIES APIS ====================

class SavedQueryCreateRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    query_text: str
    warehouse_id: Optional[str] = "wh_starter"
    catalog: Optional[str] = "warehouse"
    schema_name: Optional[str] = "dbo"
    tags: Optional[List[str]] = []

class SavedQueryUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    query_text: Optional[str] = None
    warehouse_id: Optional[str] = None
    catalog: Optional[str] = None
    schema_name: Optional[str] = None
    tags: Optional[List[str]] = None

@app.get("/api/queries")
async def list_saved_queries(q: Optional[str] = None, tag: Optional[str] = None):
    from web.saved_queries import get_saved_queries
    try:
        queries = get_saved_queries(q=q, tag=tag)
        return {"queries": queries}
    except Exception as e:
        logger.error(f"Failed to list saved queries: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/queries")
async def create_new_saved_query(payload: SavedQueryCreateRequest):
    from web.saved_queries import create_saved_query
    try:
        new_q = create_saved_query(payload.dict())
        return new_q
    except Exception as e:
        logger.error(f"Failed to create saved query: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/queries/{query_id}")
async def get_single_saved_query(query_id: str):
    from web.saved_queries import get_saved_query
    q = get_saved_query(query_id)
    if not q:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return q

@app.put("/api/queries/{query_id}")
async def update_existing_saved_query(query_id: str, payload: SavedQueryUpdateRequest):
    from web.saved_queries import update_saved_query
    data_dict = {k: v for k, v in payload.dict().items() if v is not None}
    updated = update_saved_query(query_id, data_dict)
    if not updated:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return updated

@app.delete("/api/queries/{query_id}")
async def delete_existing_saved_query(query_id: str):
    from web.saved_queries import delete_saved_query
    deleted = delete_saved_query(query_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return {"success": True, "deleted_query_id": query_id}

@app.post("/api/queries/{query_id}/duplicate")
async def duplicate_existing_saved_query(query_id: str):
    from web.saved_queries import duplicate_saved_query
    cloned = duplicate_saved_query(query_id)
    if not cloned:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return cloned


# ==================== QUERY HISTORY & AUDIT LOGGING APIS ====================

@app.get("/api/history")
async def list_query_history(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    client: Optional[str] = None,
    search: Optional[str] = None,
    min_duration_ms: Optional[float] = None
):
    return get_query_history(
        limit=limit,
        offset=offset,
        status=status,
        client=client,
        search=search,
        min_duration_ms=min_duration_ms
    )

@app.get("/api/history/{query_id}")
async def get_single_query_history(query_id: str):
    record = get_query_by_id(query_id)
    if not record:
        raise HTTPException(status_code=404, detail="Query audit record not found")
    return record

@app.get("/api/history/{query_id}/profile")
async def get_history_query_profile(query_id: str):
    record = get_query_by_id(query_id)
    if not record:
        raise HTTPException(status_code=404, detail="Query audit record not found")

    profile_json = record.get("profile_json")
    if profile_json:
        try:
            return {"success": True, "query_id": query_id, "profile": json.loads(profile_json)}
        except Exception:
            pass

    # If not previously profiled, execute profiled query on-demand
    conn = get_duckrun_conn()
    res = execute_profiled_query(conn, record["query_text"])
    if res.get("profile"):
        save_query_profile(query_id, json.dumps(res["profile"]))
        return {"success": True, "query_id": query_id, "profile": res["profile"]}
    else:
        return {"success": False, "error": res.get("error", "Profiling unavailable for this query")}

@app.delete("/api/history")
async def clear_history():
    clear_query_history()
    return {"success": True, "message": "Query history cleared"}

class ManualLogPayload(BaseModel):
    query_text: str
    duration_ms: float
    rows_produced: Optional[int] = 0
    status: Optional[str] = "SUCCESS"
    error_message: Optional[str] = None
    client: Optional[str] = "NOTEBOOK"
    is_mutation: Optional[bool] = False
    user: Optional[str] = "martin"

@app.post("/api/history/log")
async def manual_log_query(payload: ManualLogPayload):
    qid = log_query(
        query_text=payload.query_text,
        duration_ms=payload.duration_ms,
        rows_produced=payload.rows_produced or 0,
        status=payload.status or "SUCCESS",
        error_message=payload.error_message,
        client=payload.client or "NOTEBOOK",
        is_mutation=payload.is_mutation or False,
        user=payload.user or "martin"
    )
    return {"success": True, "query_id": qid}

# ==================== JOBS & PIPELINES (WORKFLOWS) APIS ====================

@app.get("/api/jobs")
async def list_jobs_endpoint():
    jobs = load_jobs()
    enriched = []
    for j in jobs:
        j_copy = dict(j)
        runs = get_job_runs(job_id=j["id"], limit=1)
        j_copy["last_run"] = runs[0] if runs else None
        enriched.append(j_copy)
    return {"jobs": enriched}

@app.post("/api/jobs")
async def save_job_endpoint(payload: Dict[str, Any]):
    saved = create_or_update_job(payload)
    return saved

@app.get("/api/jobs/{job_id}")
async def get_job_endpoint(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    runs = get_job_runs(job_id=job_id, limit=20)
    return {"job": job, "runs": runs}

@app.delete("/api/jobs/{job_id}")
async def delete_job_endpoint(job_id: str):
    ok = delete_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "deleted_id": job_id}

@app.post("/api/jobs/{job_id}/run")
async def trigger_job_run_endpoint(job_id: str):
    try:
        res = run_pipeline(job_id, trigger="MANUAL")
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/jobs/{job_id}/runs")
async def list_job_runs_endpoint(job_id: str, limit: int = 50):
    return {"runs": get_job_runs(job_id=job_id, limit=limit)}

@app.get("/api/jobs/runs/{run_id}")
async def get_single_run_endpoint(run_id: str):
    detail = get_run_detail(run_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Run not found")
    return detail

# ==================== DBT CORE / TRANSFORMATIONS APIS ====================

class DbtRunRequest(BaseModel):
    action: str = "run"
    select: Optional[str] = None
    full_refresh: bool = False
    target: str = "dev"

@app.get("/api/dbt/status")
async def get_dbt_status_endpoint():
    from web.dbt_service import get_dbt_status
    return get_dbt_status()

@app.get("/api/dbt/models")
async def list_dbt_models_endpoint():
    from web.dbt_service import list_dbt_models
    return list_dbt_models()

@app.get("/api/dbt/models/{model_name}")
async def get_dbt_model_endpoint(model_name: str):
    from web.dbt_service import get_dbt_model_detail
    detail = get_dbt_model_detail(model_name)
    if not detail:
        raise HTTPException(status_code=404, detail="Model not found")
    return detail

@app.post("/api/dbt/run")
async def run_dbt_endpoint(payload: DbtRunRequest):
    from web.dbt_service import run_dbt_cli
    res = run_dbt_cli(
        action=payload.action,
        select=payload.select,
        full_refresh=payload.full_refresh,
        target=payload.target
    )
    return res

@app.get("/api/dbt/runs")
async def list_dbt_runs_endpoint():
    from web.dbt_service import _load_runs_history
    return {"runs": _load_runs_history()}

@app.get("/api/dbt/runs/{run_id}")
async def get_dbt_run_endpoint(run_id: str):
    from web.dbt_service import _load_runs_history
    runs = _load_runs_history()
    matched = next((r for r in runs if r["run_id"] == run_id), None)
    if not matched:
        raise HTTPException(status_code=404, detail="Run record not found")
    return matched

@app.get("/api/dbt/preview/{model_name}")
async def preview_dbt_model_endpoint(model_name: str, limit: int = 50):
    from web.dbt_service import preview_dbt_model_data
    return preview_dbt_model_data(model_name, limit=limit)

@app.get("/api/dbt/cte-preview/{model_name}/{cte_name}")
async def preview_dbt_cte_endpoint(model_name: str, cte_name: str, limit: int = 50):
    from web.dbt_service import preview_cte_step
    return preview_cte_step(model_name, cte_name, limit=limit)

class DbtAddTestRequest(BaseModel):
    model_name: str
    column_name: Optional[str] = None
    test_type: str = "not_null"
    parameters: Optional[Dict[str, Any]] = None
    sql_text: Optional[str] = None
    test_name: Optional[str] = None

class DbtDeleteTestRequest(BaseModel):
    model_name: str
    column_name: Optional[str] = None
    test_type: Optional[str] = None
    test_name: Optional[str] = None

@app.post("/api/dbt/tests")
async def add_dbt_test_endpoint(payload: DbtAddTestRequest):
    from web.dbt_service import add_dbt_test
    try:
        res = add_dbt_test(
            model_name=payload.model_name,
            column_name=payload.column_name,
            test_type=payload.test_type,
            parameters=payload.parameters,
            sql_text=payload.sql_text,
            test_name=payload.test_name
        )
        return res
    except Exception as e:
        logger.error(f"Error adding dbt test: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/dbt/tests")
async def delete_dbt_test_endpoint(payload: DbtDeleteTestRequest):
    from web.dbt_service import delete_dbt_test
    try:
        res = delete_dbt_test(
            model_name=payload.model_name,
            column_name=payload.column_name,
            test_type=payload.test_type,
            test_name=payload.test_name
        )
        return res
    except Exception as e:
        logger.error(f"Error deleting dbt test: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class DbtSourceAddRequest(BaseModel):
    source_name: str = "lakehouse"
    table_name: str
    description: Optional[str] = None
    external_location: Optional[str] = None

class DbtSourceUpdateRequest(BaseModel):
    source_name: str
    table_name: str
    new_table_name: Optional[str] = None
    description: Optional[str] = None

class DbtSourceDeleteRequest(BaseModel):
    source_name: str
    table_name: str

class DbtModelCreateRequest(BaseModel):
    name: str
    layer: str = "staging"
    materialization: str = "view"
    sql_content: Optional[str] = None
    description: Optional[str] = ""

class DbtModelUpdateRequest(BaseModel):
    sql_content: str
    description: Optional[str] = None

@app.get("/api/dbt/available-delta-tables")
async def list_available_delta_tables_endpoint():
    from web.dbt_service import list_available_delta_tables
    return {"tables": list_available_delta_tables()}

@app.post("/api/dbt/sources")
async def add_dbt_source_endpoint(payload: DbtSourceAddRequest):
    from web.dbt_service import add_dbt_source
    try:
        res = add_dbt_source(
            source_name=payload.source_name,
            table_name=payload.table_name,
            description=payload.description,
            external_location=payload.external_location
        )
        return res
    except Exception as e:
        logger.error(f"Error adding dbt source: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/api/dbt/sources")
async def update_dbt_source_endpoint(payload: DbtSourceUpdateRequest):
    from web.dbt_service import update_dbt_source
    try:
        res = update_dbt_source(
            source_name=payload.source_name,
            table_name=payload.table_name,
            new_table_name=payload.new_table_name,
            description=payload.description
        )
        return res
    except Exception as e:
        logger.error(f"Error updating dbt source: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/dbt/sources")
async def delete_dbt_source_endpoint(payload: DbtSourceDeleteRequest):
    from web.dbt_service import delete_dbt_source
    try:
        res = delete_dbt_source(
            source_name=payload.source_name,
            table_name=payload.table_name
        )
        return res
    except Exception as e:
        logger.error(f"Error deleting dbt source: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/dbt/sources/{source_name}/{table_name}/preview")
async def preview_dbt_source_endpoint(source_name: str, table_name: str, limit: int = 50):
    from web.dbt_service import preview_dbt_source
    return preview_dbt_source(source_name=source_name, table_name=table_name, limit=limit)

@app.post("/api/dbt/models")
async def create_dbt_model_endpoint(payload: DbtModelCreateRequest):
    from web.dbt_service import create_dbt_model
    try:
        res = create_dbt_model(
            name=payload.name,
            layer=payload.layer,
            materialization=payload.materialization,
            sql_content=payload.sql_content,
            description=payload.description
        )
        return res
    except Exception as e:
        logger.error(f"Error creating dbt model: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/api/dbt/models/{model_name}")
async def update_dbt_model_code_endpoint(model_name: str, payload: DbtModelUpdateRequest):
    from web.dbt_service import update_dbt_model_code
    try:
        res = update_dbt_model_code(
            model_name=model_name,
            sql_content=payload.sql_content,
            description=payload.description
        )
        return res
    except Exception as e:
        logger.error(f"Error updating dbt model: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/dbt/models/{model_name}")
async def delete_dbt_model_endpoint(model_name: str):
    from web.dbt_service import delete_dbt_model
    try:
        res = delete_dbt_model(model_name=model_name)
        return res
    except Exception as e:
        logger.error(f"Error deleting dbt model: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ==================== DATABRICKS GENIE (TEXT-TO-SQL) APIS ====================

class GenieAskPayload(BaseModel):
    prompt: str
    provider: Optional[str] = None
    model: Optional[str] = None
    chat_id: Optional[str] = None

class CreateChatPayload(BaseModel):
    title: Optional[str] = "New Exploration"

@app.get("/api/genie/config")
async def genie_config_endpoint():
    config = get_available_providers()
    schema_info = extract_schema_context()
    config["tables"] = [
        {
            "name": t["table_name"],
            "columns": len(t["columns"]),
            "column_names": [c["name"] for c in t["columns"]]
        }
        for t in schema_info["tables"]
    ]
    return config

@app.get("/api/genie/chats")
async def list_genie_chats_endpoint():
    chats = load_chats()
    return {"chats": chats}

@app.post("/api/genie/chats")
async def create_genie_chat_endpoint(payload: Optional[CreateChatPayload] = None):
    title = payload.title if payload and payload.title else "New Exploration"
    new_chat = create_chat(title=title)
    return new_chat

@app.get("/api/genie/chats/{chat_id}")
async def get_genie_chat_endpoint(chat_id: str):
    chat = get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat

@app.delete("/api/genie/chats/{chat_id}")
async def delete_genie_chat_endpoint(chat_id: str):
    ok = delete_chat(chat_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"success": True, "deleted_id": chat_id}

@app.post("/api/genie/chats/{chat_id}/ask")
async def ask_genie_in_chat_endpoint(chat_id: str, payload: GenieAskPayload):
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    try:
        res = ask_genie(chat_id, payload.prompt, payload.provider, payload.model)
        return res
    except Exception as e:
        logger.error(f"Genie ask failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/genie/ask")
async def quick_ask_genie_endpoint(payload: GenieAskPayload):
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    chat_id = payload.chat_id
    if not chat_id:
        new_chat = create_chat(title=payload.prompt[:35] + ("..." if len(payload.prompt) > 35 else ""))
        chat_id = new_chat["id"]
    try:
        res = ask_genie(chat_id, payload.prompt, payload.provider, payload.model)
        return res
    except Exception as e:
        logger.error(f"Genie ask failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== UNIVERSAL SEARCH API (CTRL+P) ====================

@app.get("/api/search")
async def search_endpoint(
    q: str = "",
    category: str = "ALL",
    limit: int = 25
):
    """
    Executes a high-speed unified fuzzy search across Delta tables, schemas, columns,
    notebooks, queries, dashboards, workflows, and SQL warehouses.
    """
    from web.search import universal_search
    try:
        return universal_search(query=q, category=category, limit=min(max(limit, 1), 100))
    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== WORKSPACE BROWSER APIS ====================

@app.get("/api/workspace/tree")
async def get_workspace_tree_endpoint():
    from web.workspace import get_workspace_tree
    try:
        return {"tree": get_workspace_tree()}
    except Exception as e:
        logger.error(f"Failed to fetch workspace tree: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/workspace/file")
async def get_workspace_file_endpoint(path: str):
    from web.workspace import get_file_details
    try:
        return get_file_details(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except Exception as e:
        logger.error(f"Failed to get workspace file details: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class WorkspaceCreatePayload(BaseModel):
    target_dir: Optional[str] = ""
    name: str
    type: Optional[str] = "notebook"
    notebook_template: Optional[str] = "pyspark"

@app.post("/api/workspace/item")
async def create_workspace_item_endpoint(payload: WorkspaceCreatePayload):
    from web.workspace import create_workspace_item
    try:
        return create_workspace_item(
            payload.target_dir,
            payload.name,
            payload.type,
            payload.notebook_template or "pyspark"
        )
    except Exception as e:
        logger.error(f"Failed to create workspace item: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class WorkspaceRenamePayload(BaseModel):
    old_rel_path: str
    new_name: str

@app.put("/api/workspace/item/rename")
async def rename_workspace_item_endpoint(payload: WorkspaceRenamePayload):
    from web.workspace import rename_workspace_item
    try:
        return rename_workspace_item(payload.old_rel_path, payload.new_name)
    except Exception as e:
        logger.error(f"Failed to rename workspace item: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/workspace/item")
async def delete_workspace_item_endpoint(path: str):
    from web.workspace import delete_workspace_item
    try:
        return delete_workspace_item(path)
    except Exception as e:
        logger.error(f"Failed to delete workspace item: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/workspace/upload")
async def upload_workspace_file_endpoint(file: UploadFile = File(...), target_dir: str = Form("")):
    from web.workspace import get_safe_path, NOTEBOOKS_DIR
    try:
        dir_full = get_safe_path(target_dir)
        os.makedirs(dir_full, exist_ok=True)
        dest_file = os.path.join(dir_full, file.filename)
        with open(dest_file, "wb") as f:
            shutil.copyfileobj(file.file, f)
        rel = os.path.relpath(dest_file, NOTEBOOKS_DIR).replace("\\", "/")
        return {"success": True, "rel_path": rel, "filename": file.filename}
    except Exception as e:
        logger.error(f"Failed to upload file to workspace: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ==================== NOTEBOOK EXECUTION APIS (NATIVE RUNNER) ====================

class NotebookCellRunPayload(BaseModel):
    path: str
    cell_index: int
    source: Optional[str] = None

@app.post("/api/workspace/notebook/cell/run")
async def run_notebook_cell_endpoint(payload: NotebookCellRunPayload):
    from web.notebook_runner import execute_single_cell
    try:
        return execute_single_cell(payload.path, payload.cell_index, payload.source)
    except Exception as e:
        logger.error(f"Error executing notebook cell: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class NotebookRunAllPayload(BaseModel):
    path: str

@app.post("/api/workspace/notebook/run_all")
async def run_all_notebook_cells_endpoint(payload: NotebookRunAllPayload):
    from web.notebook_runner import execute_all_cells
    try:
        return execute_all_cells(payload.path)
    except Exception as e:
        logger.error(f"Error running all notebook cells: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class NotebookKernelPayload(BaseModel):
    path: str

@app.post("/api/workspace/notebook/kernel/restart")
async def restart_notebook_kernel_endpoint(payload: NotebookKernelPayload):
    from web.notebook_runner import restart_notebook_kernel
    try:
        return restart_notebook_kernel(payload.path)
    except Exception as e:
        logger.error(f"Error restarting notebook kernel: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/workspace/notebook/kernel/status")
async def get_notebook_kernel_status_endpoint(path: str):
    from web.notebook_runner import get_kernel_status
    try:
        return get_kernel_status(path)
    except Exception as e:
        logger.error(f"Error checking notebook kernel status: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class NotebookCellSavePayload(BaseModel):
    path: str
    cell_index: int
    source: str

@app.post("/api/workspace/notebook/cell/save")
async def save_notebook_cell_endpoint(payload: NotebookCellSavePayload):
    from web.notebook_runner import save_cell_source
    try:
        return save_cell_source(payload.path, payload.cell_index, payload.source)
    except Exception as e:
        logger.error(f"Error saving notebook cell: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class NotebookCellAddPayload(BaseModel):
    path: str
    after_index: int = 0
    type: str = "code"

@app.post("/api/workspace/notebook/cell/add")
async def add_notebook_cell_endpoint(payload: NotebookCellAddPayload):
    from web.notebook_runner import add_new_cell
    try:
        return add_new_cell(payload.path, payload.after_index, payload.type)
    except Exception as e:
        logger.error(f"Error adding notebook cell: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/workspace/notebook/cell")
async def delete_notebook_cell_endpoint(path: str, cell_index: int):
    from web.notebook_runner import delete_cell
    try:
        return delete_cell(path, cell_index)
    except Exception as e:
        logger.error(f"Error deleting notebook cell: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/workspace/notebook/clear_outputs")
async def clear_notebook_outputs_endpoint(payload: NotebookKernelPayload):
    from web.notebook_runner import clear_notebook_outputs
    try:
        return clear_notebook_outputs(payload.path)
    except Exception as e:
        logger.error(f"Error clearing notebook outputs: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ==================== RECENTS APIS (MULTI-USER TRACKING) ====================

class RecentRecordPayload(BaseModel):
    item_type: str
    item_id: str
    title: str
    subtitle: Optional[str] = ""
    metadata: Optional[Dict[str, Any]] = None
    user: Optional[str] = "martin"

@app.post("/api/recents")
async def record_recent_endpoint(payload: RecentRecordPayload, request: Request):
    from web.recents import record_recent
    user = request.headers.get("X-User") or payload.user or "martin"
    try:
        return record_recent(
            item_type=payload.item_type,
            item_id=payload.item_id,
            title=payload.title,
            subtitle=payload.subtitle or "",
            metadata=payload.metadata,
            user_id=user
        )
    except Exception as e:
        logger.error(f"Failed to record recent item: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/recents")
async def get_recents_endpoint(
    request: Request,
    type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    user: Optional[str] = "martin"
):
    from web.recents import get_recents
    user_id = request.headers.get("X-User") or user or "martin"
    try:
        return get_recents(user_id=user_id, item_type=type, search=search, limit=limit)
    except Exception as e:
        logger.error(f"Failed to get recents: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class RecentPinPayload(BaseModel):
    item_type: str
    item_id: str
    user: Optional[str] = "martin"

@app.post("/api/recents/pin")
async def toggle_pin_recent_endpoint(payload: RecentPinPayload, request: Request):
    from web.recents import toggle_pin_recent
    user = request.headers.get("X-User") or payload.user or "martin"
    try:
        return toggle_pin_recent(item_type=payload.item_type, item_id=payload.item_id, user_id=user)
    except Exception as e:
        logger.error(f"Failed to toggle pin for recent item: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/recents")
async def delete_recent_endpoint(
    request: Request,
    item_type: str,
    item_id: str,
    user: Optional[str] = "martin"
):
    from web.recents import delete_recent
    user_id = request.headers.get("X-User") or user or "martin"
    try:
        success = delete_recent(item_type=item_type, item_id=item_id, user_id=user_id)
        return {"success": success, "item_type": item_type, "item_id": item_id}
    except Exception as e:
        logger.error(f"Failed to delete recent item: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/recents/clear")
async def clear_recents_endpoint(
    request: Request,
    type: Optional[str] = None,
    include_pinned: bool = False,
    user: Optional[str] = "martin"
):
    from web.recents import clear_recents
    user_id = request.headers.get("X-User") or user or "martin"
    try:
        count = clear_recents(user_id=user_id, item_type=type, include_pinned=include_pinned)
        return {"success": True, "deleted_count": count}
    except Exception as e:
        logger.error(f"Failed to clear recents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# SQL Alerts & Lakehouse Monitoring Endpoints
# ==============================================================================

class AlertCreatePayload(BaseModel):
    name: str
    description: Optional[str] = ""
    query_id: Optional[str] = None
    custom_query: Optional[str] = None
    warehouse_id: Optional[str] = "wh_starter"
    catalog: Optional[str] = "warehouse"
    schema_name: Optional[str] = "dbo"
    target_column: Optional[str] = "count"
    operator: Optional[str] = ">"
    threshold_value: Optional[Union[str, float, int]] = "0"
    schedule_interval: Optional[str] = "5m"
    notify_on_state_change_only: Optional[bool] = True
    is_enabled: Optional[bool] = True
    is_muted: Optional[bool] = False
    is_shared: Optional[bool] = True
    user: Optional[str] = "martin"


class AlertUpdatePayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    query_id: Optional[str] = None
    custom_query: Optional[str] = None
    target_column: Optional[str] = None
    operator: Optional[str] = None
    threshold_value: Optional[Union[str, float, int]] = None
    schedule_interval: Optional[str] = None
    notify_on_state_change_only: Optional[bool] = None
    is_enabled: Optional[bool] = None
    is_muted: Optional[bool] = None
    is_shared: Optional[bool] = None
    user: Optional[str] = "martin"


@app.get("/api/alerts/summary")
async def get_alerts_summary_endpoint(request: Request, user: Optional[str] = "martin"):
    from web.alerts import get_alerts_summary
    user_id = request.headers.get("X-User") or user or "martin"
    try:
        return get_alerts_summary(user_id=user_id)
    except Exception as e:
        logger.error(f"Failed to get alerts summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/alerts")
async def get_alerts_endpoint(
    request: Request,
    state: Optional[str] = None,
    search: Optional[str] = None,
    user: Optional[str] = "martin"
):
    from web.alerts import get_alerts
    user_id = request.headers.get("X-User") or user or "martin"
    try:
        return get_alerts(user_id=user_id, state_filter=state, search=search)
    except Exception as e:
        logger.error(f"Failed to get alerts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/alerts")
async def create_alert_endpoint(payload: AlertCreatePayload, request: Request):
    from web.alerts import create_alert
    from web.recents import record_recent
    user_id = request.headers.get("X-User") or payload.user or "martin"
    try:
        alert = create_alert(payload.dict(), user_id=user_id)
        try:
            record_recent(
                item_type="alert",
                item_id=alert["id"],
                title=alert.get("name", "SQL Alert"),
                subtitle=f"{alert.get('target_column', '')} {alert.get('operator', '')} {alert.get('threshold_value', '')}",
                metadata={"state": alert.get("state"), "schedule": alert.get("schedule_interval")},
                user_id=user_id
            )
        except Exception:
            pass
        return alert
    except Exception as e:
        logger.error(f"Failed to create alert: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/alerts/{alert_id}")
async def get_alert_endpoint(alert_id: str, request: Request, user: Optional[str] = "martin"):
    from web.alerts import get_alert
    from web.recents import record_recent
    user_id = request.headers.get("X-User") or user or "martin"
    alert = get_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    try:
        record_recent(
            item_type="alert",
            item_id=alert_id,
            title=alert.get("name", "SQL Alert"),
            subtitle=f"{alert.get('target_column', '')} {alert.get('operator', '')} {alert.get('threshold_value', '')}",
            metadata={"state": alert.get("state"), "schedule": alert.get("schedule_interval")},
            user_id=user_id
        )
    except Exception:
        pass
    return alert


@app.put("/api/alerts/{alert_id}")
async def update_alert_endpoint(alert_id: str, payload: AlertUpdatePayload, request: Request):
    from web.alerts import update_alert
    user_id = request.headers.get("X-User") or payload.user or "martin"
    data = payload.dict(exclude_unset=True)
    alert = update_alert(alert_id, data, user_id=user_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


@app.delete("/api/alerts/{alert_id}")
async def delete_alert_endpoint(alert_id: str, request: Request, user: Optional[str] = "martin"):
    from web.alerts import delete_alert
    from web.recents import delete_recent
    user_id = request.headers.get("X-User") or user or "martin"
    success = delete_alert(alert_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found or already deleted")
    try:
        delete_recent("alert", alert_id, user_id=user_id)
    except Exception:
        pass
    return {"success": True, "alert_id": alert_id}


@app.post("/api/alerts/{alert_id}/run")
async def run_alert_check_endpoint(alert_id: str, request: Request, user: Optional[str] = "martin"):
    from web.alerts import execute_alert_check
    user_id = request.headers.get("X-User") or user or "martin"
    try:
        result = execute_alert_check(alert_id, triggered_by="manual", user_id=user_id)
        return result
    except Exception as e:
        logger.error(f"Failed to run alert check for {alert_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/alerts/{alert_id}/mute")
async def toggle_mute_alert_endpoint(alert_id: str, request: Request, user: Optional[str] = "martin"):
    from web.alerts import toggle_mute_alert
    user_id = request.headers.get("X-User") or user or "martin"
    alert = toggle_mute_alert(alert_id, user_id=user_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


@app.post("/api/alerts/{alert_id}/toggle")
async def toggle_enable_alert_endpoint(alert_id: str, request: Request, user: Optional[str] = "martin"):
    from web.alerts import toggle_enable_alert
    user_id = request.headers.get("X-User") or user or "martin"
    alert = toggle_enable_alert(alert_id, user_id=user_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


@app.get("/api/alerts/{alert_id}/history")
async def get_alert_history_endpoint(alert_id: str, limit: int = 50):
    from web.alerts import get_alert_evaluations
    return get_alert_evaluations(alert_id, limit=limit)


# =========================================================================
# MLflow 2.0 REST API & Studio Experiments Endpoints
# =========================================================================

@app.post("/api/2.0/mlflow/experiments/create")
async def mlflow_create_experiment_api(request: Request):
    from web.experiments import mlflow_create_experiment
    body = await request.json()
    user_id = request.headers.get("X-User", "martin")
    try:
        res = mlflow_create_experiment(body.get("name", ""), body.get("artifact_location"), user_id=user_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/2.0/mlflow/experiments/list")
async def mlflow_list_experiments_api(view_type: str = "ACTIVE_ONLY"):
    from web.experiments import mlflow_list_experiments
    return {"experiments": mlflow_list_experiments(view_type)}

@app.get("/api/2.0/mlflow/experiments/search")
@app.post("/api/2.0/mlflow/experiments/search")
async def mlflow_search_experiments_api(view_type: str = "ACTIVE_ONLY"):
    from web.experiments import mlflow_list_experiments
    return {"experiments": mlflow_list_experiments(view_type)}

@app.get("/api/2.0/mlflow/experiments/get")
async def mlflow_get_experiment_api(experiment_id: str):
    from web.experiments import mlflow_get_experiment
    exp = mlflow_get_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return {"experiment": exp}

@app.get("/api/2.0/mlflow/experiments/get-by-name")
@app.post("/api/2.0/mlflow/experiments/get-by-name")
async def mlflow_get_experiment_by_name_api(request: Request, experiment_name: Optional[str] = None):
    from web.experiments import mlflow_get_experiment_by_name
    name = experiment_name
    if not name and request.method == "POST":
        try:
            body = await request.json()
            name = body.get("experiment_name")
        except Exception:
            pass
    if not name:
        raise HTTPException(status_code=400, detail="Missing experiment_name")
    exp = mlflow_get_experiment_by_name(name)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return {"experiment": exp}

@app.post("/api/2.0/mlflow/experiments/delete")
async def mlflow_delete_experiment_api(request: Request):
    from web.experiments import mlflow_delete_experiment
    body = await request.json()
    mlflow_delete_experiment(body.get("experiment_id", ""))
    return {}

@app.post("/api/2.0/mlflow/runs/create")
async def mlflow_create_run_api(request: Request):
    from web.experiments import mlflow_create_run
    body = await request.json()
    user_id = request.headers.get("X-User", "martin")
    try:
        run_data = mlflow_create_run(
            experiment_id=body.get("experiment_id", "0"),
            run_name=body.get("run_name"),
            start_time=body.get("start_time"),
            user_id=user_id,
            tags=body.get("tags")
        )
        return {"run": run_data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/2.0/mlflow/runs/get")
async def mlflow_get_run_api(run_id: str):
    from web.experiments import mlflow_get_run
    run_data = mlflow_get_run(run_id)
    if not run_data:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run": run_data}

@app.post("/api/2.0/mlflow/runs/update")
async def mlflow_update_run_api(request: Request):
    from web.experiments import mlflow_update_run
    body = await request.json()
    try:
        run_data = mlflow_update_run(
            run_id=body.get("run_id"),
            status=body.get("status", "FINISHED"),
            end_time=body.get("end_time")
        )
        return {"run_info": run_data.get("info", {})}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/2.0/mlflow/runs/delete")
async def mlflow_delete_run_api(request: Request):
    from web.experiments import mlflow_delete_run
    body = await request.json()
    mlflow_delete_run(body.get("run_id", ""))
    return {}

@app.post("/api/2.0/mlflow/runs/search")
async def mlflow_search_runs_api(request: Request):
    from web.experiments import mlflow_search_runs
    body = await request.json()
    runs = mlflow_search_runs(
        experiment_ids=body.get("experiment_ids", ["0"]),
        filter_string=body.get("filter"),
        order_by=body.get("order_by"),
        max_results=body.get("max_results", 100)
    )
    return {"runs": runs}

@app.post("/api/2.0/mlflow/runs/log-parameter")
async def mlflow_log_param_api(request: Request):
    from web.experiments import mlflow_log_param
    body = await request.json()
    try:
        mlflow_log_param(body["run_id"], body["key"], body["value"])
        return {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/2.0/mlflow/runs/log-metric")
async def mlflow_log_metric_api(request: Request):
    from web.experiments import mlflow_log_metric
    body = await request.json()
    try:
        mlflow_log_metric(body["run_id"], body["key"], body["value"], body.get("timestamp"), body.get("step", 0))
        return {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/2.0/mlflow/runs/log-batch")
async def mlflow_log_batch_api(request: Request):
    from web.experiments import mlflow_log_batch
    body = await request.json()
    try:
        mlflow_log_batch(body["run_id"], body.get("metrics"), body.get("params"), body.get("tags"))
        return {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/2.0/mlflow/runs/set-tag")
async def mlflow_set_tag_api(request: Request):
    from web.experiments import mlflow_set_tag
    body = await request.json()
    try:
        mlflow_set_tag(body["run_id"], body["key"], body["value"])
        return {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/2.0/mlflow/metrics/get-history")
async def mlflow_get_metric_history_api(run_id: str, metric_key: str):
    from web.experiments import mlflow_get_metric_history
    history = mlflow_get_metric_history(run_id, metric_key)
    return {"metrics": history}

# -------------------------------------------------------------------------
# Studio UI Convenience Endpoints
# -------------------------------------------------------------------------

@app.get("/api/experiments/summary")
async def get_experiments_summary_endpoint():
    from web.experiments import get_experiments_summary
    return get_experiments_summary()

@app.get("/api/experiments")
async def list_studio_experiments():
    from web.experiments import mlflow_list_experiments
    return {"experiments": mlflow_list_experiments()}

@app.post("/api/experiments")
async def create_studio_experiment(request: Request):
    from web.experiments import mlflow_create_experiment
    body = await request.json()
    user_id = request.headers.get("X-User", "martin")
    try:
        res = mlflow_create_experiment(body.get("name", ""), body.get("artifact_location"), user_id=user_id)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/experiments/{experiment_id}")
async def get_studio_experiment(experiment_id: str):
    from web.experiments import mlflow_get_experiment, mlflow_search_runs
    exp = mlflow_get_experiment(experiment_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    runs = mlflow_search_runs([experiment_id], max_results=200)
    return {"experiment": exp, "runs": runs}

@app.delete("/api/experiments/{experiment_id}")
async def delete_studio_experiment(experiment_id: str):
    from web.experiments import mlflow_delete_experiment
    mlflow_delete_experiment(experiment_id)
    return {"status": "SUCCESS"}

@app.get("/api/experiments/runs/compare")
async def compare_studio_runs(run_ids: str):
    from web.experiments import compare_runs
    ids = [r.strip() for r in run_ids.split(",") if r.strip()]
    return compare_runs(ids)

@app.get("/api/experiments/runs/{run_id}")
async def get_studio_run(run_id: str):
    from web.experiments import mlflow_get_run
    r = mlflow_get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    return r

@app.delete("/api/experiments/runs/{run_id}")
async def delete_studio_run(run_id: str):
    from web.experiments import mlflow_delete_run
    mlflow_delete_run(run_id)
    return {"status": "SUCCESS"}

@app.get("/api/experiments/runs/{run_id}/metrics/{metric_key}")
async def get_studio_run_metric_history(run_id: str, metric_key: str):
    from web.experiments import mlflow_get_metric_history
    history = mlflow_get_metric_history(run_id, metric_key)
    return {"run_id": run_id, "metric_key": metric_key, "history": history}

@app.post("/api/experiments/seed_demo")
async def seed_demo_experiments_endpoint():
    from web.experiments import seed_demo_experiments
    return seed_demo_experiments()


# ==============================================================================
# AI PROMPT PLAYGROUND ROUTES
# ==============================================================================

class PlaygroundSingleRunRequest(BaseModel):
    model: str
    provider: str
    host: Optional[str] = None
    prompt: str
    raw_prompt: Optional[str] = None
    system_prompt: Optional[str] = ""
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    max_tokens: Optional[int] = 1024
    variables: Optional[Dict[str, str]] = None

class PlaygroundCompareRunRequest(BaseModel):
    config_a: Dict[str, Any]
    config_b: Dict[str, Any]
    prompt: str
    raw_prompt: Optional[str] = None
    system_prompt: Optional[str] = ""
    variables: Optional[Dict[str, str]] = None

class PlaygroundTemplateRequest(BaseModel):
    id: Optional[str] = None
    title: str
    description: Optional[str] = ""
    category: Optional[str] = "Custom"
    system_prompt: Optional[str] = ""
    user_prompt: str
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    max_tokens: Optional[int] = 1024
    variables: Optional[List[str]] = None
    is_builtin: Optional[int] = 0

@app.get("/api/playground/models")
async def api_playground_models():
    from web.playground import get_available_models
    return await get_available_models()

@app.post("/api/playground/run")
async def api_playground_run(req: PlaygroundSingleRunRequest):
    from web.playground import run_and_record_single
    raw = req.raw_prompt if req.raw_prompt is not None else req.prompt
    config = {
        "model": req.model,
        "provider": req.provider,
        "host": req.host,
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_tokens": req.max_tokens
    }
    return await run_and_record_single(
        config=config,
        rendered_prompt=req.prompt,
        raw_prompt=raw,
        system_prompt=req.system_prompt or "",
        variables=req.variables
    )

@app.post("/api/playground/compare")
async def api_playground_compare(req: PlaygroundCompareRunRequest):
    from web.playground import run_comparison_prompts
    raw = req.raw_prompt if req.raw_prompt is not None else req.prompt
    return await run_comparison_prompts(
        config_a=req.config_a,
        config_b=req.config_b,
        rendered_prompt=req.prompt,
        raw_prompt=raw,
        system_prompt=req.system_prompt or "",
        variables=req.variables
    )

@app.get("/api/playground/templates")
async def api_playground_get_templates(category: Optional[str] = None):
    from web.playground import get_templates
    return get_templates(category=category)

@app.post("/api/playground/templates")
async def api_playground_save_template(req: PlaygroundTemplateRequest):
    from web.playground import save_template
    return save_template(req.dict())

@app.delete("/api/playground/templates/{template_id}")
async def api_playground_delete_template(template_id: str):
    from web.playground import delete_template
    ok = delete_template(template_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot delete template (might be built-in or not found)")
    return {"status": "SUCCESS", "id": template_id}

@app.get("/api/playground/history")
async def api_playground_get_history(limit: int = 50):
    from web.playground import get_history
    return get_history(limit=limit)

@app.delete("/api/playground/history/{hist_id}")
async def api_playground_delete_history(hist_id: str):
    from web.playground import delete_history_item
    ok = delete_history_item(hist_id)
    return {"status": "SUCCESS" if ok else "NOT_FOUND"}

@app.delete("/api/playground/history")
async def api_playground_clear_history():
    from web.playground import clear_history
    clear_history()
    return {"status": "SUCCESS"}

@app.get("/api/playground/schema-tables")
async def api_playground_schema_tables():
    from web.playground import get_schema_tables_summary
    return get_schema_tables_summary()


# ==============================================================================
# DELTA TIME-TRAVEL RESTORE & VISUAL DIFF ENDPOINTS
# ==============================================================================

class TableRestorePayload(BaseModel):
    target_version: int
    catalog: Optional[str] = "warehouse"


@app.get("/api/table/{schema_name}/{table_name}/diff")
async def get_table_diff_endpoint(
    schema_name: str,
    table_name: str,
    v1: int,
    v2: int,
    catalog: Optional[str] = "warehouse",
    limit: int = 50
):
    from web.time_travel import resolve_table_path, compare_table_versions
    path, cat_id = resolve_table_path(schema_name, table_name, catalog)
    if not (path.startswith("s3://") or os.path.exists(path)):
        raise HTTPException(status_code=404, detail=f"Table {schema_name}.{table_name} not found")
    try:
        return compare_table_versions(path, v1, v2, sample_limit=limit)
    except Exception as e:
        logger.error(f"Error diffing table versions: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/table/{schema_name}/{table_name}/restore")
async def restore_table_endpoint(
    schema_name: str,
    table_name: str,
    payload: TableRestorePayload,
    request: Request
):
    from web.time_travel import resolve_table_path, restore_table_to_version
    user = request.headers.get("X-User") or "martin"
    path, cat_id = resolve_table_path(schema_name, table_name, payload.catalog)
    if not (path.startswith("s3://") or os.path.exists(path)):
        raise HTTPException(status_code=404, detail=f"Table {schema_name}.{table_name} not found")
    try:
        res = restore_table_to_version(path, payload.target_version, user=user)
        try:
            from web.lineage import record_query_lineage
            record_query_lineage(f"RESTORE TABLE {schema_name}.{table_name} TO VERSION AS OF {payload.target_version};")
        except Exception:
            pass
        return res
    except Exception as e:
        logger.error(f"Error restoring table: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/table/{schema_name}/{table_name}/version/{version}/preview")
async def preview_table_version_endpoint(
    schema_name: str,
    table_name: str,
    version: int,
    catalog: Optional[str] = "warehouse",
    limit: int = 50
):
    from web.time_travel import resolve_table_path, get_version_preview
    path, cat_id = resolve_table_path(schema_name, table_name, catalog)
    if not (path.startswith("s3://") or os.path.exists(path)):
        raise HTTPException(status_code=404, detail=f"Table {schema_name}.{table_name} not found")
    try:
        return get_version_preview(path, version, limit=limit)
    except Exception as e:
        logger.error(f"Error previewing version: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ==============================================================================
# AUTOMATED DATA LINEAGE ENDPOINTS
# ==============================================================================

@app.get("/api/lineage/global")
async def get_global_lineage_endpoint(
    layer: Optional[str] = None,
    schema: Optional[str] = None,
    search: Optional[str] = None
):
    from web.lineage import get_global_lineage
    try:
        return get_global_lineage(layer=layer, schema=schema, search=search)
    except Exception as e:
        logger.error(f"Error getting global lineage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/lineage/table/{schema_name}/{table_name}")
async def get_table_lineage_endpoint(
    schema_name: str,
    table_name: str,
    depth: int = 2
):
    from web.lineage import get_table_lineage
    try:
        return get_table_lineage(schema_name, table_name, depth=depth)
    except Exception as e:
        logger.error(f"Error getting table lineage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/lineage/node/{node_id:path}/impact")
async def get_node_impact_endpoint(node_id: str):
    from web.lineage import get_node_impact_analysis
    try:
        return get_node_impact_analysis(node_id)
    except Exception as e:
        logger.error(f"Error analyzing node impact: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/lineage/refresh")
async def refresh_lineage_endpoint():
    from web.lineage import scan_and_sync_all_assets, get_global_lineage
    try:
        scan_and_sync_all_assets()
        return {"status": "SUCCESS", "graph": get_global_lineage()}
    except Exception as e:
        logger.error(f"Error refreshing lineage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# STORAGE MOUNTS & ZERO-COPY FEDERATION APIS
# ==============================================================================

@app.get("/api/mounts")
async def list_mounts_endpoint():
    from web.mounts import load_mounts
    try:
        return {"mounts": load_mounts()}
    except Exception as e:
        logger.error(f"Error listing mounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mounts")
async def create_or_update_mount_endpoint(payload: Dict[str, Any], request: Request):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    if current_user.get("role") not in ("admin", "power_user"):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: role '{current_user.get('role')}' cannot mount storage. Requires admin or power_user."
        )

    from web.mounts import create_or_update_mount
    try:
        payload_copy = dict(payload)
        payload_copy["owner"] = current_user.get("username", "admin")
        res = create_or_update_mount(payload_copy)
        # Re-sync active duckrun connection
        conn = get_duckrun_conn()
        sync_catalogs_with_duckrun(conn)
        return {"success": True, "mount": res}
    except Exception as e:
        logger.error(f"Error creating/updating mount: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/mounts/{mount_id}")
async def delete_mount_endpoint(mount_id: str, request: Request):
    try:
        current_user = await get_current_user(request)
    except Exception:
        current_user = {"role": "admin", "username": "admin", "id": "u_admin_01"}

    from web.mounts import load_mounts, delete_mount
    target = next((m for m in load_mounts() if m["id"] == mount_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Mount not found")

    if current_user.get("role") != "admin" and target.get("owner") != current_user.get("username"):
        raise HTTPException(status_code=403, detail="Access denied: only administrators or the mount owner can delete this mount.")

    try:
        ok = delete_mount(mount_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Mount not found")
        return {"success": True, "deleted_id": mount_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting mount {mount_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mounts/test")
async def test_mount_endpoint(payload: Dict[str, Any]):
    from web.mounts import test_mount_connection
    try:
        res = test_mount_connection(payload)
        return res
    except Exception as e:
        logger.error(f"Error testing mount: {e}")
        return {"success": False, "error": str(e)}


# ==============================================================================
# MLFLOW MODEL REGISTRY & LOCAL HTTP SERVING APIS
# ==============================================================================

@app.get("/api/2.0/mlflow/registered-models")
async def list_registered_models_endpoint():
    from web.serving import list_registered_models
    try:
        models = list_registered_models()
        return {"registered_models": models}
    except Exception as e:
        logger.error(f"Error listing registered models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/2.0/mlflow/registered-models/{name}")
async def get_registered_model_endpoint(name: str):
    from web.serving import get_registered_model
    try:
        model = get_registered_model(name)
        if not model:
            raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
        return {"registered_model": model}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting registered model {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/2.0/mlflow/registered-models")
async def create_registered_model_endpoint(payload: Dict[str, Any]):
    from web.serving import create_registered_model
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Model name is required")
    try:
        m = create_registered_model(name=name, description=payload.get("description", ""), tags=payload.get("tags"))
        return {"registered_model": m}
    except Exception as e:
        logger.error(f"Error creating registered model: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/2.0/mlflow/registered-models/{name}")
async def delete_registered_model_endpoint(name: str):
    from web.serving import delete_registered_model
    try:
        ok = delete_registered_model(name)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
        return {"success": True, "deleted_name": name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting registered model {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/2.0/mlflow/model-versions")
async def create_model_version_endpoint(payload: Dict[str, Any]):
    from web.serving import create_model_version
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Model name is required")
    try:
        v = create_model_version(
            name=name,
            run_id=payload.get("run_id"),
            stage=payload.get("stage", "None"),
            algorithm=payload.get("algorithm", "custom"),
            metrics=payload.get("metrics"),
            signature=payload.get("signature"),
            description=payload.get("description", ""),
            source=payload.get("source", "")
        )
        return {"model_version": v}
    except Exception as e:
        logger.error(f"Error creating model version: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/2.0/mlflow/model-versions/transition-stage")
async def transition_stage_endpoint(payload: Dict[str, Any]):
    from web.serving import transition_model_version_stage
    name = payload.get("name")
    version = payload.get("version")
    stage = payload.get("stage")
    if not name or version is None or not stage:
        raise HTTPException(status_code=400, detail="Name, version, and stage are required")
    try:
        v = transition_model_version_stage(
            name=name,
            version=int(version),
            stage=stage,
            archive_existing_versions=payload.get("archive_existing_versions", True)
        )
        return {"model_version": v}
    except Exception as e:
        logger.error(f"Error transitioning stage: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/2.0/mlflow/model-versions/{name}/{version}")
async def delete_model_version_endpoint(name: str, version: int):
    from web.serving import delete_model_version
    try:
        ok = delete_model_version(name, int(version))
        if not ok:
            raise HTTPException(status_code=404, detail=f"Model version {name} v{version} not found")
        return {"success": True, "name": name, "version": version}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting model version: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/models/endpoints")
async def list_serving_endpoints_api():
    from web.serving import list_serving_endpoints
    try:
        return {"endpoints": list_serving_endpoints()}
    except Exception as e:
        logger.error(f"Error listing serving endpoints: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/models/{name}/serving")
async def configure_model_serving_endpoint(name: str, payload: Dict[str, Any]):
    from web.serving import create_or_update_serving_endpoint
    try:
        stage = payload.get("stage", "Production")
        version = payload.get("version")
        state = payload.get("state", "READY")
        ep = create_or_update_serving_endpoint(
            model_name=name,
            stage=stage,
            version=int(version) if version is not None else None,
            state=state,
            endpoint_name=payload.get("endpoint_name")
        )
        return {"endpoint": ep}
    except Exception as e:
        logger.error(f"Error configuring model serving: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/models/{name}/score")
async def score_model_endpoint(name: str, payload: Dict[str, Any], version: Optional[int] = None):
    from web.serving import score_model
    try:
        res = score_model(model_name=name, payload=payload, version=version)
        return res
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        logger.error(f"Error scoring model {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))






