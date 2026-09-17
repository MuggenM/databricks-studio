import os
import sys
import time
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, List, Union

_active_run_stack = []
_current_experiment_id = "0"
_tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:8000")


def set_tracking_uri(uri: str):
    global _tracking_uri
    _tracking_uri = uri.rstrip("/")


def get_tracking_uri() -> str:
    return _tracking_uri


def _call_api(path: str, data: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
    """Helper to communicate with Localspark MLflow API or in-process web.experiments."""
    # First attempt: in-process direct call if web.experiments is available
    try:
        from web import experiments as exp_mod
        if path == "/api/2.0/mlflow/experiments/get-by-name":
            exp = exp_mod.mlflow_get_experiment_by_name(data.get("experiment_name", ""))
            return {"experiment": exp} if exp else {}
        elif path == "/api/2.0/mlflow/experiments/create":
            return exp_mod.mlflow_create_experiment(data.get("name", ""), data.get("artifact_location"))
        elif path == "/api/2.0/mlflow/runs/create":
            r = exp_mod.mlflow_create_run(
                experiment_id=data.get("experiment_id", "0"),
                run_name=data.get("run_name"),
                start_time=data.get("start_time"),
                user_id=data.get("user_id", "martin"),
                tags=data.get("tags")
            )
            return {"run": r}
        elif path == "/api/2.0/mlflow/runs/update":
            r = exp_mod.mlflow_update_run(
                run_id=data.get("run_id"),
                status=data.get("status", "FINISHED"),
                end_time=data.get("end_time")
            )
            return {"run_info": r.get("info", {})}
        elif path == "/api/2.0/mlflow/runs/log-parameter":
            exp_mod.mlflow_log_param(data["run_id"], data["key"], data["value"])
            return {}
        elif path == "/api/2.0/mlflow/runs/log-metric":
            exp_mod.mlflow_log_metric(data["run_id"], data["key"], data["value"], data.get("timestamp"), data.get("step", 0))
            return {}
        elif path == "/api/2.0/mlflow/runs/set-tag":
            exp_mod.mlflow_set_tag(data["run_id"], data["key"], data["value"])
            return {}
        elif path == "/api/2.0/mlflow/runs/log-batch":
            exp_mod.mlflow_log_batch(data["run_id"], data.get("metrics"), data.get("params"), data.get("tags"))
            return {}
    except Exception:
        pass

    # Fallback: HTTP request to _tracking_uri
    url = f"{_tracking_uri}{path}"
    headers = {"Content-Type": "application/json"}
    body_bytes = json.dumps(data or {}).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # Silently log if local server is starting
        return {}


class ActiveRun:
    def __init__(self, run_data: Dict[str, Any]):
        self.data = run_data
        self.info = run_data.get("info", {})

    @property
    def run_id(self) -> str:
        return self.info.get("run_id", "")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        status = "FAILED" if exc_type else "FINISHED"
        end_run(status=status)
        return False


def set_experiment(experiment_name: str) -> str:
    """Sets or creates an experiment by name, returning experiment_id."""
    global _current_experiment_id
    experiment_name = experiment_name.strip()
    res = _call_api("/api/2.0/mlflow/experiments/get-by-name", {"experiment_name": experiment_name}, method="POST")
    exp = res.get("experiment")
    if exp and exp.get("experiment_id"):
        _current_experiment_id = exp["experiment_id"]
    else:
        create_res = _call_api("/api/2.0/mlflow/experiments/create", {"name": experiment_name})
        _current_experiment_id = create_res.get("experiment_id", "0")
    return _current_experiment_id


def start_run(
    run_id: Optional[str] = None,
    experiment_id: Optional[str] = None,
    run_name: Optional[str] = None,
    nested: bool = False,
    tags: Optional[Dict[str, str]] = None,
    description: Optional[str] = None
) -> ActiveRun:
    """Starts a new tracking run and returns an ActiveRun context manager."""
    global _active_run_stack
    target_exp = experiment_id or _current_experiment_id
    tags_list = [{"key": k, "value": str(v)} for k, v in (tags or {}).items()]
    if description:
        tags_list.append({"key": "mlflow.note.content", "value": description})

    req_data = {
        "experiment_id": target_exp,
        "run_name": run_name,
        "start_time": int(time.time() * 1000),
        "tags": tags_list
    }
    resp = _call_api("/api/2.0/mlflow/runs/create", req_data)
    run_data = resp.get("run", {"info": {"run_id": f"run_{int(time.time()*1000)}"}})
    run_obj = ActiveRun(run_data)
    _active_run_stack.append(run_obj)
    return run_obj


def end_run(status: str = "FINISHED"):
    """Ends the currently active run."""
    global _active_run_stack
    if not _active_run_stack:
        return
    run_obj = _active_run_stack.pop()
    rid = run_obj.run_id
    _call_api("/api/2.0/mlflow/runs/update", {
        "run_id": rid,
        "status": status.upper(),
        "end_time": int(time.time() * 1000)
    })


def active_run() -> Optional[ActiveRun]:
    return _active_run_stack[-1] if _active_run_stack else None


def log_param(key: str, value: Any):
    cur = active_run()
    if not cur:
        start_run()
        cur = active_run()
    _call_api("/api/2.0/mlflow/runs/log-parameter", {
        "run_id": cur.run_id,
        "key": str(key),
        "value": str(value)
    })


def log_params(params: Dict[str, Any]):
    for k, v in (params or {}).items():
        log_param(k, v)


def log_metric(key: str, value: float, step: Optional[int] = None):
    cur = active_run()
    if not cur:
        start_run()
        cur = active_run()
    _call_api("/api/2.0/mlflow/runs/log-metric", {
        "run_id": cur.run_id,
        "key": str(key),
        "value": float(value),
        "step": int(step or 0),
        "timestamp": int(time.time() * 1000)
    })


def log_metrics(metrics: Dict[str, float], step: Optional[int] = None):
    for k, v in (metrics or {}).items():
        log_metric(k, v, step=step)


def set_tag(key: str, value: Any):
    cur = active_run()
    if not cur:
        start_run()
        cur = active_run()
    _call_api("/api/2.0/mlflow/runs/set-tag", {
        "run_id": cur.run_id,
        "key": str(key),
        "value": str(value)
    })


def set_tags(tags: Dict[str, Any]):
    for k, v in (tags or {}).items():
        set_tag(k, v)
