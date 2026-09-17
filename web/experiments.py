import os
import time
import uuid
import sqlite3
import datetime
import logging
from typing import Optional, Dict, Any, List, Union

logger = logging.getLogger("localspark.experiments")

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
if not os.path.exists(WAREHOUSE_DIR):
    local_alt = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "warehouse"))
    if os.path.exists(local_alt):
        WAREHOUSE_DIR = local_alt

METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
DB_PATH = os.path.join(METADATA_DIR, "experiments.db")
ARTIFACTS_BASE_DIR = os.path.join(WAREHOUSE_DIR, "mlflow", "artifacts")


def get_exp_db() -> sqlite3.Connection:
    """Returns a SQLite connection to experiments.db with WAL mode enabled."""
    os.makedirs(METADATA_DIR, exist_ok=True)
    os.makedirs(ARTIFACTS_BASE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_experiments_db():
    """Initializes tables for experiments, runs, params, metrics, tags, and artifacts."""
    try:
        with get_exp_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    artifact_location TEXT NOT NULL,
                    lifecycle_stage TEXT NOT NULL DEFAULT 'active',
                    user_id TEXT NOT NULL DEFAULT 'martin',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_user ON experiments(user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_name ON experiments(name);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    run_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'RUNNING',
                    start_time INTEGER NOT NULL,
                    end_time INTEGER DEFAULT NULL,
                    duration_ms REAL DEFAULT 0.0,
                    user_id TEXT NOT NULL DEFAULT 'martin',
                    source_type TEXT NOT NULL DEFAULT 'NOTEBOOK',
                    source_name TEXT DEFAULT '',
                    lifecycle_stage TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_exp ON runs(experiment_id, start_time DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_params (
                    run_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (run_id, key),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_params_key ON run_params(key);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_metrics (
                    run_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value REAL NOT NULL,
                    timestamp INTEGER NOT NULL,
                    step INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (run_id, key, step, timestamp),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON run_metrics(run_id, key, step);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_tags (
                    run_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (run_id, key),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    is_dir INTEGER NOT NULL DEFAULT 0,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    file_type TEXT NOT NULL DEFAULT 'file',
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_run ON run_artifacts(run_id);")

            # Create default experiment if no experiments exist
            cur = conn.execute("SELECT COUNT(*) FROM experiments")
            if cur.fetchone()[0] == 0:
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                def_exp_id = "0"
                def_artifact = os.path.join(ARTIFACTS_BASE_DIR, def_exp_id)
                os.makedirs(def_artifact, exist_ok=True)
                conn.execute(
                    "INSERT INTO experiments (experiment_id, name, artifact_location, lifecycle_stage, user_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'active', 'martin', ?, ?)",
                    (def_exp_id, "Default", def_artifact, now_str, now_str)
                )
                logger.info("Initialized default experiment '0'")
    except Exception as e:
        logger.error(f"Failed to initialize experiments database: {e}", exc_info=True)


# =========================================================================
# MLflow 2.0 REST API Service Implementation
# =========================================================================

def mlflow_create_experiment(name: str, artifact_location: Optional[str] = None, user_id: str = "martin") -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Experiment name cannot be empty")

    with get_exp_db() as conn:
        row = conn.execute("SELECT experiment_id FROM experiments WHERE name = ?", (name,)).fetchone()
        if row:
            raise ValueError(f"Experiment with name '{name}' already exists")

        exp_id = str(uuid.uuid4().hex[:8])
        if not artifact_location:
            artifact_location = os.path.join(ARTIFACTS_BASE_DIR, exp_id)
        os.makedirs(artifact_location, exist_ok=True)

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO experiments (experiment_id, name, artifact_location, lifecycle_stage, user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (exp_id, name, artifact_location, user_id, now_str, now_str)
        )
        return {"experiment_id": exp_id}


def mlflow_get_experiment(experiment_id: str) -> Optional[Dict[str, Any]]:
    with get_exp_db() as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ? AND lifecycle_stage != 'deleted'",
            (str(experiment_id),)
        ).fetchone()
        if not row:
            return None
        return {
            "experiment_id": row["experiment_id"],
            "name": row["name"],
            "artifact_location": row["artifact_location"],
            "lifecycle_stage": row["lifecycle_stage"],
            "user_id": row["user_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }


def mlflow_get_experiment_by_name(name: str) -> Optional[Dict[str, Any]]:
    with get_exp_db() as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE name = ? AND lifecycle_stage != 'deleted'",
            (name.strip(),)
        ).fetchone()
        if not row:
            return None
        return {
            "experiment_id": row["experiment_id"],
            "name": row["name"],
            "artifact_location": row["artifact_location"],
            "lifecycle_stage": row["lifecycle_stage"],
            "user_id": row["user_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }


def mlflow_list_experiments(view_type: str = "ACTIVE_ONLY") -> List[Dict[str, Any]]:
    stage_filter = "lifecycle_stage = 'active'" if view_type != "ALL" else "1=1"
    with get_exp_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM experiments WHERE {stage_filter} ORDER BY created_at ASC"
        ).fetchall()
        result = []
        for r in rows:
            # count runs
            c_runs = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE experiment_id = ? AND lifecycle_stage != 'deleted'",
                (r["experiment_id"],)
            ).fetchone()[0]
            result.append({
                "experiment_id": r["experiment_id"],
                "name": r["name"],
                "artifact_location": r["artifact_location"],
                "lifecycle_stage": r["lifecycle_stage"],
                "user_id": r["user_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "runs_count": c_runs
            })
        return result


def mlflow_delete_experiment(experiment_id: str):
    with get_exp_db() as conn:
        conn.execute(
            "UPDATE experiments SET lifecycle_stage = 'deleted' WHERE experiment_id = ?",
            (str(experiment_id),)
        )
        conn.execute(
            "UPDATE runs SET lifecycle_stage = 'deleted' WHERE experiment_id = ?",
            (str(experiment_id),)
        )


def mlflow_create_run(
    experiment_id: str,
    run_name: Optional[str] = None,
    start_time: Optional[int] = None,
    user_id: str = "martin",
    tags: Optional[List[Dict[str, str]]] = None,
    source_type: str = "NOTEBOOK",
    source_name: str = ""
) -> Dict[str, Any]:
    exp = mlflow_get_experiment(experiment_id)
    if not exp:
        raise ValueError(f"Experiment '{experiment_id}' does not exist")

    run_id = uuid.uuid4().hex
    if not run_name:
        run_name = f"run_{run_id[:7]}"
    if start_time is None:
        start_time = int(time.time() * 1000)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_exp_db() as conn:
        conn.execute("""
            INSERT INTO runs (run_id, experiment_id, run_name, status, start_time, duration_ms, user_id, source_type, source_name, lifecycle_stage, created_at)
            VALUES (?, ?, ?, 'RUNNING', ?, 0.0, ?, ?, ?, 'active', ?)
        """, (run_id, experiment_id, run_name, start_time, user_id, source_type, source_name, now_str))

        if tags:
            for t in tags:
                k = (t.get("key") or "").strip()
                v = str(t.get("value") or "")
                if k:
                    conn.execute(
                        "INSERT OR REPLACE INTO run_tags (run_id, key, value) VALUES (?, ?, ?)",
                        (run_id, k, v)
                    )

    return mlflow_get_run(run_id)


def mlflow_update_run(
    run_id: str,
    status: str = "FINISHED",
    end_time: Optional[int] = None
) -> Dict[str, Any]:
    if end_time is None:
        end_time = int(time.time() * 1000)

    with get_exp_db() as conn:
        r = conn.execute("SELECT start_time FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not r:
            raise ValueError(f"Run '{run_id}' not found")

        start_time = r["start_time"]
        duration_ms = max(0.0, float(end_time - start_time))

        conn.execute("""
            UPDATE runs SET status = ?, end_time = ?, duration_ms = ? WHERE run_id = ?
        """, (status.upper(), end_time, duration_ms, run_id))

    return mlflow_get_run(run_id)


def mlflow_delete_run(run_id: str):
    with get_exp_db() as conn:
        conn.execute("UPDATE runs SET lifecycle_stage = 'deleted' WHERE run_id = ?", (run_id,))


def mlflow_log_param(run_id: str, key: str, value: Any):
    key = str(key).strip()
    val_str = str(value)
    if not key:
        raise ValueError("Param key cannot be empty")

    with get_exp_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO run_params (run_id, key, value) VALUES (?, ?, ?)
        """, (run_id, key, val_str))


def mlflow_log_metric(run_id: str, key: str, value: float, timestamp: Optional[int] = None, step: int = 0):
    key = str(key).strip()
    if not key:
        raise ValueError("Metric key cannot be empty")
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    with get_exp_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO run_metrics (run_id, key, value, timestamp, step) VALUES (?, ?, ?, ?, ?)
        """, (run_id, key, float(value), timestamp, int(step)))


def mlflow_set_tag(run_id: str, key: str, value: str):
    key = str(key).strip()
    if not key:
        raise ValueError("Tag key cannot be empty")

    with get_exp_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO run_tags (run_id, key, value) VALUES (?, ?, ?)
        """, (run_id, key, str(value)))


def mlflow_log_batch(
    run_id: str,
    metrics: Optional[List[Dict[str, Any]]] = None,
    params: Optional[List[Dict[str, Any]]] = None,
    tags: Optional[List[Dict[str, Any]]] = None
):
    with get_exp_db() as conn:
        if params:
            for p in params:
                k = (p.get("key") or "").strip()
                if k:
                    conn.execute(
                        "INSERT OR REPLACE INTO run_params (run_id, key, value) VALUES (?, ?, ?)",
                        (run_id, k, str(p.get("value", "")))
                    )
        if metrics:
            now_ms = int(time.time() * 1000)
            for m in metrics:
                k = (m.get("key") or "").strip()
                if k:
                    val = float(m.get("value", 0.0))
                    ts = m.get("timestamp", now_ms)
                    step = int(m.get("step", 0))
                    conn.execute(
                        "INSERT OR REPLACE INTO run_metrics (run_id, key, value, timestamp, step) VALUES (?, ?, ?, ?, ?)",
                        (run_id, k, val, ts, step)
                    )
        if tags:
            for t in tags:
                k = (t.get("key") or "").strip()
                if k:
                    conn.execute(
                        "INSERT OR REPLACE INTO run_tags (run_id, key, value) VALUES (?, ?, ?)",
                        (run_id, k, str(t.get("value", "")))
                    )


def mlflow_get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with get_exp_db() as conn:
        r = conn.execute(
            "SELECT * FROM runs WHERE run_id = ? AND lifecycle_stage != 'deleted'",
            (run_id,)
        ).fetchone()
        if not r:
            return None

        # Fetch params
        params_rows = conn.execute(
            "SELECT key, value FROM run_params WHERE run_id = ? ORDER BY key ASC",
            (run_id,)
        ).fetchall()
        params_dict = {p["key"]: p["value"] for p in params_rows}
        params_list = [{"key": p["key"], "value": p["value"]} for p in params_rows]

        # Fetch latest metrics (group by key, highest step/timestamp)
        metrics_rows = conn.execute("""
            SELECT m.key, m.value, m.step, m.timestamp
            FROM run_metrics m
            INNER JOIN (
                SELECT key, MAX(step) as max_step, MAX(timestamp) as max_ts
                FROM run_metrics
                WHERE run_id = ?
                GROUP BY key
            ) latest ON m.key = latest.key AND m.step = latest.max_step
            WHERE m.run_id = ?
            ORDER BY m.key ASC
        """, (run_id, run_id)).fetchall()

        latest_metrics_dict = {m["key"]: round(float(m["value"]), 4) for m in metrics_rows}
        metrics_list = [{
            "key": m["key"],
            "value": float(m["value"]),
            "step": m["step"],
            "timestamp": m["timestamp"]
        } for m in metrics_rows]

        # Fetch tags
        tags_rows = conn.execute(
            "SELECT key, value FROM run_tags WHERE run_id = ? ORDER BY key ASC",
            (run_id,)
        ).fetchall()
        tags_dict = {t["key"]: t["value"] for t in tags_rows}
        tags_list = [{"key": t["key"], "value": t["value"]} for t in tags_rows]

        # Artifacts
        artifacts_rows = conn.execute(
            "SELECT path, file_size, file_type FROM run_artifacts WHERE run_id = ?",
            (run_id,)
        ).fetchall()
        artifacts_list = [{
            "path": a["path"],
            "file_size": a["file_size"],
            "file_type": a["file_type"]
        } for a in artifacts_rows]

        exp = mlflow_get_experiment(r["experiment_id"])
        exp_name = exp["name"] if exp else r["experiment_id"]

        run_info = {
            "run_id": r["run_id"],
            "run_uuid": r["run_id"],
            "run_name": r["run_name"],
            "experiment_id": r["experiment_id"],
            "experiment_name": exp_name,
            "status": r["status"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "duration_ms": r["duration_ms"],
            "duration_sec": round((r["duration_ms"] or 0) / 1000.0, 2),
            "user_id": r["user_id"],
            "source_type": r["source_type"],
            "source_name": r["source_name"],
            "lifecycle_stage": r["lifecycle_stage"],
            "created_at": r["created_at"],
            "artifact_uri": os.path.join(ARTIFACTS_BASE_DIR, r["experiment_id"], r["run_id"])
        }

        return {
            "info": run_info,
            "data": {
                "params": params_list,
                "metrics": metrics_list,
                "tags": tags_list
            },
            # Convenience maps for Studio UI:
            "params_dict": params_dict,
            "metrics_dict": latest_metrics_dict,
            "tags_dict": tags_dict,
            "artifacts": artifacts_list
        }


def mlflow_search_runs(
    experiment_ids: List[str],
    filter_string: Optional[str] = None,
    order_by: Optional[List[str]] = None,
    max_results: int = 100
) -> List[Dict[str, Any]]:
    if not experiment_ids:
        return []

    placeholders = ",".join(["?"] * len(experiment_ids))
    with get_exp_db() as conn:
        query = f"""
            SELECT run_id FROM runs
            WHERE experiment_id IN ({placeholders}) AND lifecycle_stage != 'deleted'
            ORDER BY start_time DESC
            LIMIT ?
        """
        params = list(map(str, experiment_ids)) + [max_results]
        rows = conn.execute(query, params).fetchall()

        results = []
        for r in rows:
            run_data = mlflow_get_run(r["run_id"])
            if run_data:
                results.append(run_data)
        return results


def mlflow_get_metric_history(run_id: str, metric_key: str) -> List[Dict[str, Any]]:
    with get_exp_db() as conn:
        rows = conn.execute("""
            SELECT key, value, timestamp, step
            FROM run_metrics
            WHERE run_id = ? AND key = ?
            ORDER BY step ASC, timestamp ASC
        """, (run_id, metric_key)).fetchall()

        return [{
            "key": r["key"],
            "value": float(r["value"]),
            "timestamp": r["timestamp"],
            "step": r["step"]
        } for r in rows]


# =========================================================================
# Studio UI Convenience & Comparison Methods
# =========================================================================

def get_experiments_summary() -> Dict[str, Any]:
    """Returns high-level statistics for Studio UI."""
    with get_exp_db() as conn:
        total_exp = conn.execute("SELECT COUNT(*) FROM experiments WHERE lifecycle_stage != 'deleted'").fetchone()[0]
        total_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE lifecycle_stage != 'deleted'").fetchone()[0]
        active_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'RUNNING' AND lifecycle_stage != 'deleted'").fetchone()[0]
        finished_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'FINISHED' AND lifecycle_stage != 'deleted'").fetchone()[0]

        # Top metric keys across all runs
        top_metrics = conn.execute("""
            SELECT key, COUNT(DISTINCT run_id) as c
            FROM run_metrics
            GROUP BY key
            ORDER BY c DESC LIMIT 6
        """).fetchall()

        recent_runs_rows = conn.execute("""
            SELECT r.run_id, r.run_name, r.experiment_id, r.status, r.duration_ms, r.created_at, e.name as experiment_name
            FROM runs r
            JOIN experiments e ON r.experiment_id = e.experiment_id
            WHERE r.lifecycle_stage != 'deleted'
            ORDER BY r.start_time DESC LIMIT 5
        """).fetchall()

        recent_runs = []
        for r in recent_runs_rows:
            recent_runs.append({
                "run_id": r["run_id"],
                "run_name": r["run_name"],
                "experiment_id": r["experiment_id"],
                "experiment_name": r["experiment_name"],
                "status": r["status"],
                "duration_sec": round((r["duration_ms"] or 0) / 1000.0, 2),
                "created_at": r["created_at"]
            })

        return {
            "total_experiments": total_exp,
            "total_runs": total_runs,
            "active_runs": active_runs,
            "finished_runs": finished_runs,
            "popular_metrics": [m["key"] for m in top_metrics],
            "recent_runs": recent_runs
        }


def compare_runs(run_ids: List[str]) -> Dict[str, Any]:
    """Compares multiple runs, computing parameter diffs and metric variations."""
    if not run_ids:
        return {"runs": [], "common_params": {}, "diff_params": {}, "metrics_comparison": {}, "step_metrics": {}}

    runs_data = []
    all_param_keys = set()
    all_metric_keys = set()

    for rid in run_ids:
        r = mlflow_get_run(rid)
        if r:
            runs_data.append(r)
            all_param_keys.update(r["params_dict"].keys())
            all_metric_keys.update(r["metrics_dict"].keys())

    if not runs_data:
        return {"runs": [], "common_params": {}, "diff_params": {}, "metrics_comparison": {}, "step_metrics": {}}

    common_params = {}
    diff_params = {}

    for k in sorted(all_param_keys):
        vals = [r["params_dict"].get(k, "-") for r in runs_data]
        if all(v == vals[0] for v in vals):
            common_params[k] = vals[0]
        else:
            diff_params[k] = vals

    # Compare metrics: compute values, best, and delta percentage
    metrics_comparison = {}
    for k in sorted(all_metric_keys):
        vals = [r["metrics_dict"].get(k, None) for r in runs_data]
        valid_vals = [v for v in vals if v is not None]
        max_val = max(valid_vals) if valid_vals else None
        min_val = min(valid_vals) if valid_vals else None
        metrics_comparison[k] = {
            "values": vals,
            "max": max_val,
            "min": min_val
        }

    # Fetch time-series for popular loss/eval curves (up to 3 metrics)
    step_metrics = {}
    candidate_curve_keys = [k for k in all_metric_keys if any(term in k.lower() for term in ["loss", "accuracy", "error", "auc", "f1", "score"])]
    for mk in candidate_curve_keys[:4]:
        curves = {}
        for r in runs_data:
            rid = r["info"]["run_id"]
            history = mlflow_get_metric_history(rid, mk)
            if history:
                curves[rid] = {
                    "run_name": r["info"]["run_name"],
                    "points": [{"step": pt["step"], "value": pt["value"]} for pt in history]
                }
        if curves:
            step_metrics[mk] = curves

    return {
        "runs": [{
            "run_id": r["info"]["run_id"],
            "run_name": r["info"]["run_name"],
            "status": r["info"]["status"],
            "duration_sec": r["info"]["duration_sec"],
            "experiment_name": r["info"]["experiment_name"],
            "created_at": r["info"]["created_at"]
        } for r in runs_data],
        "common_params": common_params,
        "diff_params": diff_params,
        "metrics_comparison": metrics_comparison,
        "step_metrics": step_metrics
    }


def seed_demo_experiments() -> Dict[str, Any]:
    """Generates realistic demo MLflow experiments and training runs."""
    init_experiments_db()

    # 1. Experiment: Customer Churn Classification
    exp_churn_name = "customer_churn_prediction"
    exp_churn = mlflow_get_experiment_by_name(exp_churn_name)
    if not exp_churn:
        exp_res = mlflow_create_experiment(exp_churn_name)
        churn_id = exp_res["experiment_id"]
    else:
        churn_id = exp_churn["experiment_id"]

    # Run 1: XGBoost Classifier (Champion model)
    now_ms = int(time.time() * 1000)
    r1 = mlflow_create_run(churn_id, run_name="xgboost_churn_baseline", start_time=now_ms - 3600000, source_type="NOTEBOOK", source_name="notebooks/customer_churn_ml.ipynb")
    r1_id = r1["info"]["run_id"]
    mlflow_log_batch(
        r1_id,
        params=[
            {"key": "model_type", "value": "xgboost"},
            {"key": "max_depth", "value": "6"},
            {"key": "learning_rate", "value": "0.05"},
            {"key": "n_estimators", "value": "150"},
            {"key": "subsample", "value": "0.8"},
            {"key": "delta_source", "value": "dbo.silver_telemetry"}
        ],
        metrics=[
            {"key": "train_loss", "value": 0.65, "step": 1, "timestamp": now_ms - 3590000},
            {"key": "train_loss", "value": 0.48, "step": 2, "timestamp": now_ms - 3580000},
            {"key": "train_loss", "value": 0.35, "step": 3, "timestamp": now_ms - 3570000},
            {"key": "train_loss", "value": 0.24, "step": 4, "timestamp": now_ms - 3560000},
            {"key": "train_loss", "value": 0.16, "step": 5, "timestamp": now_ms - 3550000},
            {"key": "val_loss", "value": 0.68, "step": 1, "timestamp": now_ms - 3590000},
            {"key": "val_loss", "value": 0.51, "step": 2, "timestamp": now_ms - 3580000},
            {"key": "val_loss", "value": 0.38, "step": 3, "timestamp": now_ms - 3570000},
            {"key": "val_loss", "value": 0.28, "step": 4, "timestamp": now_ms - 3560000},
            {"key": "val_loss", "value": 0.19, "step": 5, "timestamp": now_ms - 3550000},
            {"key": "accuracy", "value": 0.942, "step": 5, "timestamp": now_ms - 3550000},
            {"key": "f1_score", "value": 0.915, "step": 5, "timestamp": now_ms - 3550000},
            {"key": "roc_auc", "value": 0.963, "step": 5, "timestamp": now_ms - 3550000},
            {"key": "latency_ms", "value": 4.8, "step": 5, "timestamp": now_ms - 3550000}
        ],
        tags=[
            {"key": "framework", "value": "xgboost"},
            {"key": "mlflow.user", "value": "martin"},
            {"key": "candidate_stage", "value": "Production"}
        ]
    )
    mlflow_update_run(r1_id, status="FINISHED", end_time=now_ms - 3548000)

    # Run 2: Random Forest
    r2 = mlflow_create_run(churn_id, run_name="random_forest_v1", start_time=now_ms - 7200000, source_type="NOTEBOOK", source_name="notebooks/customer_churn_ml.ipynb")
    r2_id = r2["info"]["run_id"]
    mlflow_log_batch(
        r2_id,
        params=[
            {"key": "model_type", "value": "random_forest"},
            {"key": "max_depth", "value": "8"},
            {"key": "n_estimators", "value": "200"},
            {"key": "criterion", "value": "gini"},
            {"key": "delta_source", "value": "dbo.silver_telemetry"}
        ],
        metrics=[
            {"key": "train_loss", "value": 0.72, "step": 1, "timestamp": now_ms - 7190000},
            {"key": "train_loss", "value": 0.55, "step": 2, "timestamp": now_ms - 7180000},
            {"key": "train_loss", "value": 0.42, "step": 3, "timestamp": now_ms - 7170000},
            {"key": "train_loss", "value": 0.31, "step": 4, "timestamp": now_ms - 7160000},
            {"key": "train_loss", "value": 0.22, "step": 5, "timestamp": now_ms - 7150000},
            {"key": "val_loss", "value": 0.75, "step": 1, "timestamp": now_ms - 7190000},
            {"key": "val_loss", "value": 0.58, "step": 2, "timestamp": now_ms - 7180000},
            {"key": "val_loss", "value": 0.46, "step": 3, "timestamp": now_ms - 7170000},
            {"key": "val_loss", "value": 0.36, "step": 4, "timestamp": now_ms - 7160000},
            {"key": "val_loss", "value": 0.27, "step": 5, "timestamp": now_ms - 7150000},
            {"key": "accuracy", "value": 0.921, "step": 5, "timestamp": now_ms - 7150000},
            {"key": "f1_score", "value": 0.887, "step": 5, "timestamp": now_ms - 7150000},
            {"key": "roc_auc", "value": 0.938, "step": 5, "timestamp": now_ms - 7150000},
            {"key": "latency_ms", "value": 8.2, "step": 5, "timestamp": now_ms - 7150000}
        ],
        tags=[
            {"key": "framework", "value": "scikit-learn"},
            {"key": "mlflow.user", "value": "martin"},
            {"key": "candidate_stage", "value": "Staging"}
        ]
    )
    mlflow_update_run(r2_id, status="FINISHED", end_time=now_ms - 7149000)

    # Run 3: Logistic Regression
    r3 = mlflow_create_run(churn_id, run_name="logistic_regression_baseline", start_time=now_ms - 10800000, source_type="NOTEBOOK", source_name="notebooks/customer_churn_ml.ipynb")
    r3_id = r3["info"]["run_id"]
    mlflow_log_batch(
        r3_id,
        params=[
            {"key": "model_type", "value": "logistic_regression"},
            {"key": "C", "value": "1.0"},
            {"key": "penalty", "value": "l2"},
            {"key": "solver", "value": "lbfgs"},
            {"key": "delta_source", "value": "dbo.silver_telemetry"}
        ],
        metrics=[
            {"key": "train_loss", "value": 0.82, "step": 1, "timestamp": now_ms - 10790000},
            {"key": "train_loss", "value": 0.69, "step": 2, "timestamp": now_ms - 10780000},
            {"key": "train_loss", "value": 0.58, "step": 3, "timestamp": now_ms - 10770000},
            {"key": "train_loss", "value": 0.51, "step": 4, "timestamp": now_ms - 10760000},
            {"key": "train_loss", "value": 0.46, "step": 5, "timestamp": now_ms - 10750000},
            {"key": "val_loss", "value": 0.85, "step": 1, "timestamp": now_ms - 10790000},
            {"key": "val_loss", "value": 0.72, "step": 2, "timestamp": now_ms - 10780000},
            {"key": "val_loss", "value": 0.61, "step": 3, "timestamp": now_ms - 10770000},
            {"key": "val_loss", "value": 0.54, "step": 4, "timestamp": now_ms - 10760000},
            {"key": "val_loss", "value": 0.49, "step": 5, "timestamp": now_ms - 10750000},
            {"key": "accuracy", "value": 0.845, "step": 5, "timestamp": now_ms - 10750000},
            {"key": "f1_score", "value": 0.792, "step": 5, "timestamp": now_ms - 10750000},
            {"key": "roc_auc", "value": 0.860, "step": 5, "timestamp": now_ms - 10750000},
            {"key": "latency_ms", "value": 1.2, "step": 5, "timestamp": now_ms - 10750000}
        ],
        tags=[
            {"key": "framework", "value": "scikit-learn"},
            {"key": "mlflow.user", "value": "martin"},
            {"key": "candidate_stage", "value": "Archived"}
        ]
    )
    mlflow_update_run(r3_id, status="FINISHED", end_time=now_ms - 10749000)

    # 2. Experiment: Sensor Telemetry Anomaly Detection
    exp_anomaly_name = "sensor_telemetry_anomaly_detection"
    exp_anomaly = mlflow_get_experiment_by_name(exp_anomaly_name)
    if not exp_anomaly:
        exp_res2 = mlflow_create_experiment(exp_anomaly_name)
        anom_id = exp_res2["experiment_id"]
    else:
        anom_id = exp_anomaly["experiment_id"]

    r_anom = mlflow_create_run(anom_id, run_name="isolation_forest_prod", start_time=now_ms - 1800000, source_type="JOB", source_name="job_medallion_pipeline")
    r_anom_id = r_anom["info"]["run_id"]
    mlflow_log_batch(
        r_anom_id,
        params=[
            {"key": "algorithm", "value": "IsolationForest"},
            {"key": "contamination", "value": "0.03"},
            {"key": "n_estimators", "value": "100"},
            {"key": "target_table", "value": "dbo.bronze_telemetry"}
        ],
        metrics=[
            {"key": "anomalies_detected", "value": 14.0, "step": 1, "timestamp": now_ms - 1790000},
            {"key": "anomaly_pct", "value": 2.8, "step": 1, "timestamp": now_ms - 1790000},
            {"key": "silhouette_score", "value": 0.724, "step": 1, "timestamp": now_ms - 1790000},
            {"key": "latency_ms", "value": 6.5, "step": 1, "timestamp": now_ms - 1790000}
        ],
        tags=[
            {"key": "domain", "value": "IoT Telemetry"},
            {"key": "pipeline", "value": "Medallion Automated"}
        ]
    )
    mlflow_update_run(r_anom_id, status="FINISHED", end_time=now_ms - 1785000)

    return {"status": "SUCCESS", "message": "Seeded demo experiments: customer_churn_prediction (3 runs) and sensor_telemetry_anomaly_detection (1 run)"}
