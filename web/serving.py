import os
import time
import math
import uuid
import json
import sqlite3
import datetime
import logging
from typing import Dict, Any, List, Optional, Union

logger = logging.getLogger("localspark.serving")

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
DB_PATH = os.path.join(METADATA_DIR, "experiments.db")


def get_db() -> sqlite3.Connection:
    os.makedirs(METADATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_serving_db():
    """Initializes tables for MLflow registered models, model versions, and serving endpoints."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS registered_models (
                name TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                user_id TEXT DEFAULT 'martin',
                tags TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_versions (
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                run_id TEXT,
                current_stage TEXT NOT NULL DEFAULT 'None',
                source TEXT DEFAULT '',
                status TEXT DEFAULT 'READY',
                flavor TEXT DEFAULT 'python_function',
                algorithm TEXT DEFAULT 'xgboost',
                description TEXT DEFAULT '',
                metrics TEXT DEFAULT '{}',
                signature TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (name, version),
                FOREIGN KEY (name) REFERENCES registered_models(name) ON DELETE CASCADE
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mv_stage ON model_versions(name, current_stage);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS serving_endpoints (
                endpoint_id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                model_name TEXT NOT NULL,
                version INTEGER DEFAULT NULL,
                stage TEXT DEFAULT 'Production',
                state TEXT DEFAULT 'READY',
                request_count INTEGER DEFAULT 0,
                last_served_at TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (model_name) REFERENCES registered_models(name) ON DELETE CASCADE
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ep_model ON serving_endpoints(model_name);")

        # Check if seed models exist; if not, seed them
        cursor = conn.execute("SELECT COUNT(*) FROM registered_models;")
        if cursor.fetchone()[0] == 0:
            seed_default_models(conn)


def seed_default_models(conn: sqlite3.Connection):
    """Seeds production-grade demonstration models and endpoints into MLflow registry."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. employee_turnover_predictor
    conn.execute(
        "INSERT INTO registered_models (name, description, user_id, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?);",
        (
            "employee_turnover_predictor",
            "Predicts probability of employee attrition and voluntary turnover risk based on tenure, salary, and satisfaction telemetry.",
            "martin",
            json.dumps({"domain": "people_analytics", "framework": "xgboost", "task": "binary_classification"}),
            now_str,
            now_str
        )
    )

    turnover_sig = json.dumps({
        "inputs": [
            {"name": "tenure_years", "type": "double", "example": 2.5},
            {"name": "salary", "type": "long", "example": 92000},
            {"name": "satisfaction_score", "type": "double", "example": 0.42},
            {"name": "overtime_hours", "type": "double", "example": 14.5}
        ],
        "outputs": [
            {"name": "turnover_probability", "type": "double"},
            {"name": "risk_tier", "type": "string"},
            {"name": "recommendation", "type": "string"}
        ]
    })

    conn.execute("""
        INSERT INTO model_versions (name, version, run_id, current_stage, source, status, flavor, algorithm, description, metrics, signature, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        "employee_turnover_predictor",
        1,
        "aca979a9b4d140948cac6dd6400ca781",
        "Staging",
        "/workspace/warehouse/mlflow/artifacts/98763edf/aca979a9b4d140948cac6dd6400ca781/artifacts/model",
        "READY",
        "python_function",
        "RandomForestClassifier",
        "Initial tree ensemble benchmark on historical personnel records",
        json.dumps({"accuracy": 0.884, "auc_roc": 0.912, "f1_score": 0.865}),
        turnover_sig,
        now_str,
        now_str
    ))

    conn.execute("""
        INSERT INTO model_versions (name, version, run_id, current_stage, source, status, flavor, algorithm, description, metrics, signature, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        "employee_turnover_predictor",
        2,
        "eae654bf2d284fe19a6489d86f775b4f",
        "Production",
        "/workspace/warehouse/mlflow/artifacts/98763edf/eae654bf2d284fe19a6489d86f775b4f/artifacts/model",
        "READY",
        "python_function",
        "XGBClassifier",
        "Fine-tuned gradient boosting model with early stopping and balanced class weighting",
        json.dumps({"accuracy": 0.946, "auc_roc": 0.968, "f1_score": 0.938}),
        turnover_sig,
        now_str,
        now_str
    ))

    conn.execute("""
        INSERT INTO serving_endpoints (endpoint_id, name, model_name, version, stage, state, request_count, last_served_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        "ep_turnover_prod",
        "employee-turnover-predictor-prod",
        "employee_turnover_predictor",
        2,
        "Production",
        "READY",
        148,
        now_str,
        now_str,
        now_str
    ))

    # 2. equipment_failure_forecaster
    conn.execute(
        "INSERT INTO registered_models (name, description, user_id, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?);",
        (
            "equipment_failure_forecaster",
            "Real-time industrial IoT telemetry failure forecasting and predictive maintenance time-to-failure scoring.",
            "martin",
            json.dumps({"domain": "iot_manufacturing", "framework": "lightgbm", "task": "anomaly_detection"}),
            now_str,
            now_str
        )
    )

    sensor_sig = json.dumps({
        "inputs": [
            {"name": "vibration_rms", "type": "double", "example": 4.1},
            {"name": "temperature_c", "type": "double", "example": 82.3},
            {"name": "pressure_psi", "type": "double", "example": 128.5},
            {"name": "operating_hours", "type": "long", "example": 4200}
        ],
        "outputs": [
            {"name": "failure_probability", "type": "double"},
            {"name": "alert_level", "type": "string"},
            {"name": "hours_to_failure", "type": "double"}
        ]
    })

    conn.execute("""
        INSERT INTO model_versions (name, version, run_id, current_stage, source, status, flavor, algorithm, description, metrics, signature, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        "equipment_failure_forecaster",
        1,
        "22b243763b6546deb1c564c01abed94d",
        "Production",
        "/workspace/warehouse/mlflow/artifacts/13c30f05/22b243763b6546deb1c564c01abed94d/artifacts/model",
        "READY",
        "python_function",
        "LGBMClassifier",
        "Anomaly detection classifier on vibration and temperature sensors",
        json.dumps({"accuracy": 0.971, "auc_roc": 0.985, "precision": 0.962}),
        sensor_sig,
        now_str,
        now_str
    ))

    conn.execute("""
        INSERT INTO serving_endpoints (endpoint_id, name, model_name, version, stage, state, request_count, last_served_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        "ep_equipment_prod",
        "equipment-failure-forecaster-prod",
        "equipment_failure_forecaster",
        1,
        "Production",
        "READY",
        892,
        now_str,
        now_str,
        now_str
    ))


def list_registered_models() -> List[Dict[str, Any]]:
    init_serving_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM registered_models ORDER BY updated_at DESC;").fetchall()
        result = []
        for r in rows:
            m = dict(r)
            m["tags"] = json.loads(m["tags"]) if m["tags"] else {}
            
            # Fetch versions
            v_rows = conn.execute(
                "SELECT * FROM model_versions WHERE name = ? ORDER BY version DESC;",
                (m["name"],)
            ).fetchall()
            versions = []
            latest_stage_version = {}
            for vr in v_rows:
                v_dict = dict(vr)
                v_dict["metrics"] = json.loads(v_dict["metrics"]) if v_dict["metrics"] else {}
                v_dict["signature"] = json.loads(v_dict["signature"]) if v_dict["signature"] else {}
                versions.append(v_dict)
                st = v_dict["current_stage"]
                if st not in latest_stage_version:
                    latest_stage_version[st] = v_dict["version"]

            m["latest_versions"] = versions
            m["version_count"] = len(versions)
            m["production_version"] = latest_stage_version.get("Production")
            m["staging_version"] = latest_stage_version.get("Staging")
            
            # Fetch serving endpoint
            ep_row = conn.execute(
                "SELECT * FROM serving_endpoints WHERE model_name = ? LIMIT 1;",
                (m["name"],)
            ).fetchone()
            m["serving_endpoint"] = dict(ep_row) if ep_row else None
            
            result.append(m)
        return result


def get_registered_model(name: str) -> Optional[Dict[str, Any]]:
    init_serving_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM registered_models WHERE name = ?;", (name,)).fetchone()
        if not row:
            return None
        m = dict(row)
        m["tags"] = json.loads(m["tags"]) if m["tags"] else {}
        
        v_rows = conn.execute(
            "SELECT * FROM model_versions WHERE name = ? ORDER BY version DESC;",
            (m["name"],)
        ).fetchall()
        versions = []
        latest_stage_version = {}
        for vr in v_rows:
            v_dict = dict(vr)
            v_dict["metrics"] = json.loads(v_dict["metrics"]) if v_dict["metrics"] else {}
            v_dict["signature"] = json.loads(v_dict["signature"]) if v_dict["signature"] else {}
            versions.append(v_dict)
            st = v_dict["current_stage"]
            if st not in latest_stage_version:
                latest_stage_version[st] = v_dict["version"]

        m["latest_versions"] = versions
        m["version_count"] = len(versions)
        m["production_version"] = latest_stage_version.get("Production")
        m["staging_version"] = latest_stage_version.get("Staging")
        
        ep_row = conn.execute("SELECT * FROM serving_endpoints WHERE model_name = ?;", (m["name"],)).fetchone()
        m["serving_endpoint"] = dict(ep_row) if ep_row else None
        return m


def create_registered_model(name: str, description: str = "", tags: Optional[Dict] = None) -> Dict[str, Any]:
    init_serving_db()
    clean_name = name.strip().replace(" ", "_")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO registered_models (name, description, user_id, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (
            clean_name,
            description.strip(),
            "martin",
            json.dumps(tags or {}),
            now_str,
            now_str
        ))
    return get_registered_model(clean_name)


def delete_registered_model(name: str) -> bool:
    init_serving_db()
    with get_db() as conn:
        c = conn.execute("DELETE FROM registered_models WHERE name = ?;", (name,))
        return c.rowcount > 0


def create_model_version(
    name: str,
    run_id: Optional[str] = None,
    stage: str = "None",
    algorithm: str = "custom",
    metrics: Optional[Dict] = None,
    signature: Optional[Dict] = None,
    description: str = "",
    source: str = ""
) -> Dict[str, Any]:
    init_serving_db()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        # Determine next version
        curr_max = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM model_versions WHERE name = ?;",
            (name,)
        ).fetchone()[0]
        new_version = curr_max + 1

        conn.execute("""
            INSERT INTO model_versions (
                name, version, run_id, current_stage, source, status,
                flavor, algorithm, description, metrics, signature, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            name,
            new_version,
            run_id,
            stage,
            source or f"/workspace/warehouse/mlflow/models/{name}/v{new_version}",
            "READY",
            "python_function",
            algorithm,
            description,
            json.dumps(metrics or {}),
            json.dumps(signature or {}),
            now_str,
            now_str
        ))

        # If stage is Production or Staging, update endpoint if configured
        conn.execute("UPDATE registered_models SET updated_at = ? WHERE name = ?;", (now_str, name))

    return get_model_version(name, new_version)


def get_model_version(name: str, version: int) -> Optional[Dict[str, Any]]:
    init_serving_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM model_versions WHERE name = ? AND version = ?;",
            (name, version)
        ).fetchone()
        if not row:
            return None
        v = dict(row)
        v["metrics"] = json.loads(v["metrics"]) if v["metrics"] else {}
        v["signature"] = json.loads(v["signature"]) if v["signature"] else {}
        return v


def transition_model_version_stage(
    name: str,
    version: int,
    stage: str,
    archive_existing_versions: bool = True
) -> Dict[str, Any]:
    """Promotes or transitions a model version to a stage ('Production', 'Staging', 'Archived', 'None')."""
    init_serving_db()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        if archive_existing_versions and stage in ["Production", "Staging"]:
            conn.execute(
                "UPDATE model_versions SET current_stage = 'Archived', updated_at = ? WHERE name = ? AND current_stage = ? AND version != ?;",
                (now_str, name, stage, version)
            )

        conn.execute(
            "UPDATE model_versions SET current_stage = ?, updated_at = ? WHERE name = ? AND version = ?;",
            (stage, now_str, name, version)
        )
        conn.execute("UPDATE registered_models SET updated_at = ? WHERE name = ?;", (now_str, name))

        # Also update any serving endpoint attached to this model and stage
        if stage == "Production":
            conn.execute(
                "UPDATE serving_endpoints SET version = ?, updated_at = ? WHERE model_name = ? AND stage = 'Production';",
                (version, now_str, name)
            )

    return get_model_version(name, version)


def delete_model_version(name: str, version: int) -> bool:
    init_serving_db()
    with get_db() as conn:
        c = conn.execute("DELETE FROM model_versions WHERE name = ? AND version = ?;", (name, version))
        return c.rowcount > 0


def list_serving_endpoints() -> List[Dict[str, Any]]:
    init_serving_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM serving_endpoints ORDER BY updated_at DESC;").fetchall()
        return [dict(r) for r in rows]


def get_serving_endpoint(endpoint_id_or_name: str) -> Optional[Dict[str, Any]]:
    init_serving_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM serving_endpoints WHERE endpoint_id = ? OR name = ? OR model_name = ?;",
            (endpoint_id_or_name, endpoint_id_or_name, endpoint_id_or_name)
        ).fetchone()
        return dict(row) if row else None


def create_or_update_serving_endpoint(
    model_name: str,
    stage: str = "Production",
    version: Optional[int] = None,
    state: str = "READY",
    endpoint_name: Optional[str] = None
) -> Dict[str, Any]:
    init_serving_db()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ep_name = endpoint_name or f"{model_name.replace('_', '-')}-service"
    ep_id = f"ep_{uuid.uuid4().hex[:8]}"

    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM serving_endpoints WHERE model_name = ?;",
            (model_name,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE serving_endpoints
                SET stage = ?, version = ?, state = ?, updated_at = ?
                WHERE endpoint_id = ?;
            """, (stage, version, state, now_str, existing["endpoint_id"]))
            ep_id = existing["endpoint_id"]
        else:
            conn.execute("""
                INSERT INTO serving_endpoints (
                    endpoint_id, name, model_name, version, stage, state, request_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?);
            """, (ep_id, ep_name, model_name, version, stage, state, now_str, now_str))

    return get_serving_endpoint(ep_id)


def toggle_serving_endpoint_state(endpoint_id: str, new_state: str) -> Optional[Dict[str, Any]]:
    init_serving_db()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "UPDATE serving_endpoints SET state = ?, updated_at = ? WHERE endpoint_id = ?;",
            (new_state, now_str, endpoint_id)
        )
    return get_serving_endpoint(endpoint_id)


def score_model(
    model_name: str,
    payload: Dict[str, Any],
    version: Optional[int] = None
) -> Dict[str, Any]:
    """In-process local prediction scoring endpoint supporting MLflow DataFrame split or records format."""
    start_time = time.perf_counter()
    init_serving_db()

    model = get_registered_model(model_name)
    if not model:
        raise ValueError(f"Model '{model_name}' not found in registry.")

    # Determine version to score against
    selected_version = None
    if version is not None:
        selected_version = get_model_version(model_name, version)
    else:
        # Default to Production stage version if available, else latest
        prod_v = model.get("production_version")
        if prod_v:
            selected_version = get_model_version(model_name, prod_v)
        elif model.get("latest_versions"):
            selected_version = model["latest_versions"][0]

    if not selected_version:
        raise ValueError(f"No active version found for model '{model_name}'.")

    # Parse inputs from payload
    records: List[Dict[str, Any]] = []
    if "dataframe_split" in payload:
        ds = payload["dataframe_split"]
        cols = ds.get("columns", [])
        data = ds.get("data", [])
        for row in data:
            records.append(dict(zip(cols, row)))
    elif "dataframe_records" in payload:
        records = payload["dataframe_records"]
    elif "inputs" in payload:
        inputs = payload["inputs"]
        if isinstance(inputs, list):
            if inputs and isinstance(inputs[0], dict):
                records = inputs
            else:
                records = [{"input": x} for x in inputs]
        elif isinstance(inputs, dict):
            records = [inputs]
    elif "records" in payload:
        records = payload["records"]
    else:
        # Check if top-level dict is a single record
        if any(k in payload for k in ["tenure_years", "vibration_rms", "vibration", "salary", "satisfaction_score"]):
            records = [payload]
        else:
            records = [payload]

    predictions: List[Dict[str, Any]] = []
    m_name_lower = model_name.lower()

    for idx, rec in enumerate(records):
        if "turnover" in m_name_lower or "churn" in m_name_lower:
            # Employee turnover prediction model
            tenure = float(rec.get("tenure_years", rec.get("tenure", 2.0)))
            salary = float(rec.get("salary", 75000.0))
            satisfaction = float(rec.get("satisfaction_score", rec.get("satisfaction", 0.5)))
            overtime = float(rec.get("overtime_hours", rec.get("overtime", 0.0)))
            
            # Feature logic
            logit = 0.8 + (overtime * 0.09) - (satisfaction * 3.2) - (salary / 100000.0 * 0.6) + (0.5 if tenure < 2.0 else -0.3)
            prob = 1.0 / (1.0 + math.exp(-max(-10.0, min(10.0, logit))))
            tier = "HIGH" if prob >= 0.60 else ("MEDIUM" if prob >= 0.35 else "LOW")
            rec_action = (
                "Immediate retention intervention: schedule 1-on-1 and review compensation"
                if tier == "HIGH"
                else ("Monitor engagement and project allocation" if tier == "MEDIUM" else "Satisfied; standard retention tracking")
            )

            predictions.append({
                "record_index": idx,
                "turnover_probability": round(prob, 4),
                "risk_tier": tier,
                "confidence": round(abs(prob - 0.5) * 2, 4),
                "recommendation": rec_action,
                "input_features": rec
            })

        elif "equipment" in m_name_lower or "failure" in m_name_lower or "sensor" in m_name_lower or "anomaly" in m_name_lower:
            # Industrial equipment predictive maintenance model
            vib = float(rec.get("vibration_rms", rec.get("vibration", 2.0)))
            temp = float(rec.get("temperature_c", rec.get("temperature", 65.0)))
            psi = float(rec.get("pressure_psi", rec.get("pressure", 100.0)))
            hours = float(rec.get("operating_hours", rec.get("hours", 2000.0)))

            stress = (max(0.0, vib - 2.0) * 0.55) + (max(0.0, temp - 70.0) * 0.04) + (max(0.0, psi - 105.0) * 0.03) + (hours / 15000.0 * 0.4)
            prob = min(0.999, max(0.001, 1.0 / (1.0 + math.exp(-stress + 1.2))))
            alert = "CRITICAL" if prob >= 0.70 else ("WARNING" if prob >= 0.35 else "NORMAL")
            time_to_fail = round(max(2.0, (1.0 - prob) * 1400.0), 1)

            predictions.append({
                "record_index": idx,
                "failure_probability": round(prob, 4),
                "alert_level": alert,
                "hours_to_failure": time_to_fail,
                "input_features": rec
            })

        else:
            # Generic model prediction
            numeric_sum = sum(float(v) for v in rec.values() if isinstance(v, (int, float)))
            prob = 1.0 / (1.0 + math.exp(-numeric_sum / 100.0))
            predictions.append({
                "record_index": idx,
                "score": round(prob, 4),
                "class": 1 if prob >= 0.5 else 0,
                "input_features": rec
            })

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Record telemetry on serving endpoint
    try:
        with get_db() as conn:
            conn.execute("""
                UPDATE serving_endpoints
                SET request_count = request_count + ?, last_served_at = ?
                WHERE model_name = ?;
            """, (len(records), now_str, model_name))
    except Exception as e:
        logger.debug(f"Telemetry update notice: {e}")

    return {
        "predictions": predictions,
        "model_name": model_name,
        "model_version": selected_version["version"],
        "stage": selected_version["current_stage"],
        "algorithm": selected_version.get("algorithm", "xgboost"),
        "latency_ms": elapsed_ms,
        "served_at": now_str,
        "record_count": len(predictions)
    }
