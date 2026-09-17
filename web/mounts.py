import os
import json
import logging
import datetime
import uuid
from typing import Dict, Any, List, Optional
import duckdb

logger = logging.getLogger("localspark.mounts")

WAREHOUSE_DIR = os.getenv("WAREHOUSE_DIR", "/workspace/warehouse")
METADATA_DIR = os.path.join(WAREHOUSE_DIR, ".metadata")
MOUNTS_FILE = os.path.join(METADATA_DIR, "storage_mounts.json")


def get_default_mounts() -> List[Dict[str, Any]]:
    return []


def load_mounts() -> List[Dict[str, Any]]:
    os.makedirs(METADATA_DIR, exist_ok=True)
    if not os.path.exists(MOUNTS_FILE):
        defaults = get_default_mounts()
        save_mounts(defaults)
        return defaults
    try:
        with open(MOUNTS_FILE, "r") as f:
            data = json.load(f)
            return data.get("mounts", [])
    except Exception as e:
        logger.error(f"Failed to load storage_mounts.json: {e}")
        return get_default_mounts()


def save_mounts(mounts: List[Dict[str, Any]]):
    os.makedirs(METADATA_DIR, exist_ok=True)
    try:
        with open(MOUNTS_FILE, "w") as f:
            json.dump({"mounts": mounts, "updated_at": datetime.datetime.now().isoformat()}, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save storage_mounts.json: {e}")


def get_mount(mount_id: str) -> Optional[Dict[str, Any]]:
    mounts = load_mounts()
    return next((m for m in mounts if m["id"] == mount_id), None)


def create_or_update_mount(mount_data: Dict[str, Any]) -> Dict[str, Any]:
    mounts = load_mounts()
    mount_id = mount_data.get("id") or f"mount_{uuid.uuid4().hex[:8]}"
    catalog_name = mount_data.get("catalog_name", "").strip().lower().replace("-", "_").replace(" ", "_")
    if not catalog_name:
        catalog_name = f"mount_{uuid.uuid4().hex[:6]}"

    existing_idx = next((i for i, m in enumerate(mounts) if m["id"] == mount_id), -1)

    mount_record = {
        "id": mount_id,
        "name": mount_data.get("name", "External Storage Mount").strip(),
        "type": mount_data.get("type", "postgres").lower().strip(),
        "catalog_name": catalog_name,
        "read_only": bool(mount_data.get("read_only", True)),
        "enabled": bool(mount_data.get("enabled", True)),
        "description": mount_data.get("description", "").strip(),
        "config": mount_data.get("config", {}),
        "status": mount_data.get("status", "ACTIVE"),
        "created_at": mount_data.get("created_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_synced_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    if existing_idx >= 0:
        mounts[existing_idx] = mount_record
    else:
        mounts.append(mount_record)

    save_mounts(mounts)
    return mount_record


def delete_mount(mount_id: str) -> bool:
    mounts = load_mounts()
    initial_len = len(mounts)
    mounts = [m for m in mounts if m["id"] != mount_id]
    if len(mounts) < initial_len:
        save_mounts(mounts)
        return True
    return False


def get_s3_storage_options(config: Dict[str, Any]) -> Dict[str, str]:
    """Generates standard AWS/S3 storage options dictionary for deltalake.DeltaTable and write_deltalake."""
    endpoint = config.get("endpoint", "garage:3900").strip()
    use_ssl = bool(config.get("use_ssl", False))
    if endpoint.lower().startswith("https://"):
        use_ssl = True
        endpoint = endpoint[8:]
    elif endpoint.lower().startswith("http://"):
        use_ssl = False
        endpoint = endpoint[7:]
    endpoint = endpoint.rstrip("/")
    protocol = "https://" if use_ssl else "http://"
    full_endpoint = f"{protocol}{endpoint}"

    return {
        "AWS_ENDPOINT_URL": full_endpoint,
        "AWS_ACCESS_KEY_ID": config.get("key_id", ""),
        "AWS_SECRET_ACCESS_KEY": config.get("secret", ""),
        "AWS_REGION": config.get("region", "us-east-1"),
        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
        "AWS_ALLOW_HTTP": "true"
    }


def test_mount_connection(mount_data: Dict[str, Any]) -> Dict[str, Any]:
    """Tests connection to an external storage mount using an ephemeral DuckDB connection."""
    m_type = mount_data.get("type", "").lower().strip()
    config = mount_data.get("config", {})
    raw_catalog = mount_data.get("catalog_name", "test_mount").strip().lower()
    catalog_name = "".join(c if (c.isalnum() or c == "_") else "_" for c in raw_catalog)
    if not catalog_name:
        catalog_name = "test_mount"

    test_conn = duckdb.connect()
    try:
        if m_type == "postgres":
            host = config.get("host", "127.0.0.1")
            port = int(config.get("port", 5432))
            database = config.get("database", "postgres")
            user = config.get("user", "postgres")
            password = config.get("password", "")
            
            test_conn.execute("INSTALL postgres; LOAD postgres;")
            attach_sql = (
                f"ATTACH 'dbname={database} host={host} port={port} user={user} password={password} connect_timeout=5' "
                f"AS \"{catalog_name}\" (TYPE postgres, READ_ONLY true);"
            )
            test_conn.execute(attach_sql)
            
            # Fetch tables
            tables = test_conn.execute(
                f"SELECT table_schema, table_name FROM information_schema.tables "
                f"WHERE table_catalog = '{catalog_name}' AND table_schema NOT IN ('information_schema', 'pg_catalog') "
                f"LIMIT 25"
            ).fetchall()
            
            table_list = [f"{s}.{t}" for s, t in tables]
            return {
                "success": True,
                "type": "postgres",
                "message": f"Successfully connected to PostgreSQL database '{database}' on {host}:{port}.",
                "tables": table_list,
                "table_count": len(table_list)
            }

        elif m_type == "s3":
            bucket = config.get("bucket", "").strip()
            endpoint = config.get("endpoint", "127.0.0.1:9000").strip()
            url_style = config.get("url_style", "path").strip()
            use_ssl_bool = bool(config.get("use_ssl", False))
            if endpoint.lower().startswith("https://"):
                use_ssl_bool = True
                endpoint = endpoint[8:]
            elif endpoint.lower().startswith("http://"):
                use_ssl_bool = False
                endpoint = endpoint[7:]
            endpoint = endpoint.rstrip("/")
            use_ssl = "true" if use_ssl_bool else "false"
            region = config.get("region", "us-east-1").strip()
            key_id = config.get("key_id", "").strip()
            secret = config.get("secret", "").strip()

            test_conn.execute("INSTALL httpfs; LOAD httpfs;")
            test_conn.execute("SET http_timeout = 5; SET http_retries = 1; SET http_keep_alive = false;")
            secret_name = f"test_{uuid.uuid4().hex[:8]}"
            secret_sql = f"""
            CREATE SECRET "{secret_name}" (
                TYPE S3,
                KEY_ID '{key_id}',
                SECRET '{secret}',
                ENDPOINT '{endpoint}',
                URL_STYLE '{url_style}',
                USE_SSL {use_ssl},
                REGION '{region}'
            );
            """
            test_conn.execute(secret_sql)
            
            # Test globbing bucket
            glob_path = f"s3://{bucket}/**"
            files = test_conn.execute(f"SELECT * FROM glob('{glob_path}') LIMIT 15").fetchall()
            file_list = [row[0] for row in files]

            return {
                "success": True,
                "type": "s3",
                "message": f"Successfully connected to S3/Garage endpoint '{endpoint}' bucket '{bucket}'. ({len(file_list)} object(s) found)",
                "files": file_list,
                "file_count": len(file_list)
            }

        elif m_type == "sqlite":
            path = config.get("path", "").strip()
            if not os.path.exists(path):
                return {
                    "success": False,
                    "error": f"SQLite database file does not exist at path '{path}'"
                }
            test_conn.execute("INSTALL sqlite; LOAD sqlite;")
            test_conn.execute(f"ATTACH '{path}' AS \"{catalog_name}\" (TYPE sqlite, READ_ONLY true);")
            tables = test_conn.execute(
                f"SELECT table_name FROM information_schema.tables WHERE table_catalog = '{catalog_name}'"
            ).fetchall()
            table_list = [t[0] for t in tables]
            return {
                "success": True,
                "type": "sqlite",
                "message": f"Successfully connected to SQLite database at '{path}'.",
                "tables": table_list,
                "table_count": len(table_list)
            }
        else:
            return {"success": False, "error": f"Unsupported mount type: {m_type}"}

    except Exception as e:
        logger.warning(f"Test mount failed for {m_type}: {e}")
        err_str = str(e)
        if "Invalid signature" in err_str or "403 Forbidden" in err_str:
            err_str = f"Access denied (403): Invalid Key ID or Secret for endpoint '{config.get('endpoint')}'. Please verify your credentials."
        elif "Timeout was reached" in err_str or "Could not resolve host" in err_str or "Connection refused" in err_str:
            err_str = f"Network connection failed to '{config.get('endpoint')}'. Ensure host and port are reachable from the studio container."
        return {"success": False, "error": err_str}
    finally:
        try:
            test_conn.close()
        except Exception:
            pass


def attach_mount_to_duckdb(conn, mount: Dict[str, Any]) -> bool:
    """Attaches a single storage mount to a live DuckDB connection or duckrun DuckSession."""
    if not mount.get("enabled", True):
        return False

    raw_conn = getattr(conn, "con", conn)
    m_type = mount.get("type", "").lower().strip()
    config = mount.get("config", {})
    raw_catalog = mount.get("catalog_name", "").strip().lower()
    catalog_name = "".join(c if (c.isalnum() or c == "_") else "_" for c in raw_catalog)
    read_only = mount.get("read_only", True)
    ro_str = "true" if read_only else "false"

    if not catalog_name:
        return False

    try:
        # Check if already attached
        existing_dbs = [row[0] for row in raw_conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()]
        
        if m_type == "postgres":
            if catalog_name in existing_dbs:
                return True
            raw_conn.execute("INSTALL postgres; LOAD postgres;")
            host = config.get("host", "127.0.0.1")
            port = int(config.get("port", 5432))
            database = config.get("database", "postgres")
            user = config.get("user", "postgres")
            password = config.get("password", "")
            
            attach_sql = (
                f"ATTACH 'dbname={database} host={host} port={port} user={user} password={password} connect_timeout=5' "
                f"AS \"{catalog_name}\" (TYPE postgres, READ_ONLY {ro_str});"
            )
            raw_conn.execute(attach_sql)
            logger.info(f"Successfully attached PostgreSQL mount '{catalog_name}'")
            return True

        elif m_type == "s3":
            raw_conn.execute("INSTALL httpfs; LOAD httpfs;")
            raw_conn.execute("SET http_timeout = 5; SET http_retries = 1; SET http_keep_alive = false;")
            bucket = config.get("bucket", "").strip()
            endpoint = config.get("endpoint", "127.0.0.1:9000").strip()
            url_style = config.get("url_style", "path").strip()
            use_ssl_bool = bool(config.get("use_ssl", False))
            if endpoint.lower().startswith("https://"):
                use_ssl_bool = True
                endpoint = endpoint[8:]
            elif endpoint.lower().startswith("http://"):
                use_ssl_bool = False
                endpoint = endpoint[7:]
            endpoint = endpoint.rstrip("/")
            use_ssl = "true" if use_ssl_bool else "false"
            region = config.get("region", "us-east-1").strip()
            key_id = config.get("key_id", "").strip()
            secret = config.get("secret", "").strip()

            secret_sql = f"""
            CREATE SECRET IF NOT EXISTS "{catalog_name}" (
                TYPE S3,
                KEY_ID '{key_id}',
                SECRET '{secret}',
                ENDPOINT '{endpoint}',
                URL_STYLE '{url_style}',
                USE_SSL {use_ssl},
                REGION '{region}'
            );
            """
            try:
                raw_conn.execute(secret_sql)
            except Exception as e:
                logger.debug(f"Secret {catalog_name} notice: {e}")

            so = get_s3_storage_options(config)

            # 1. Attach S3 Lakehouse catalog in DuckSession if available
            if hasattr(conn, "_catalogs"):
                if catalog_name not in conn._catalogs:
                    try:
                        conn.attach(f"s3://{bucket}", name=catalog_name, storage_options=so, read_only=read_only)
                        logger.info(f"Attached S3 Lakehouse catalog '{catalog_name}' to duckrun session.")
                    except Exception as e_att:
                        logger.warning(f"Could not attach S3 via duckrun ({e_att}), falling back to memory catalog.")

            # 2. Ensure catalog exists in DuckDB databases
            dbs_now = [row[0] for row in raw_conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()]
            if catalog_name not in dbs_now:
                try:
                    raw_conn.execute(f"ATTACH ':memory:' AS \"{catalog_name}\";")
                except Exception as e_mem:
                    logger.warning(f"Could not ATTACH ':memory:' AS {catalog_name}: {e_mem}")

            # 3. Create schemas inside the attached catalog
            try:
                raw_conn.execute(f"CREATE SCHEMA IF NOT EXISTS \"{catalog_name}\".dbo;")
                if bucket:
                    raw_conn.execute(f"CREATE SCHEMA IF NOT EXISTS \"{catalog_name}\".\"{bucket}\";")
            except Exception as e_sch:
                logger.debug(f"Could not create schemas in {catalog_name}: {e_sch}")

            # 4. Discover and register Delta Lake tables
            try:
                delta_logs = raw_conn.execute(f"SELECT file FROM glob('s3://{bucket}/**/_delta_log/0*.json')").fetchall()
                seen_delta = set()
                for d_row in delta_logs:
                    fpath = d_row[0]
                    table_path = fpath.split("/_delta_log/")[0]
                    if table_path in seen_delta:
                        continue
                    seen_delta.add(table_path)

                    rel = table_path.replace(f"s3://{bucket}/", "").strip("/")
                    parts = rel.split("/")
                    if len(parts) >= 2:
                        sch = parts[0]
                        tbl = parts[1]
                    else:
                        sch = "dbo"
                        tbl = parts[0]

                    try:
                        raw_conn.execute(f"CREATE SCHEMA IF NOT EXISTS \"{catalog_name}\".\"{sch}\";")
                        raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}\".\"{sch}\".\"{tbl}\" AS SELECT * FROM delta_scan('{table_path}');")
                        if sch != "dbo":
                            raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}\".dbo.\"{tbl}\" AS SELECT * FROM delta_scan('{table_path}');")
                        raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}_{sch}_{tbl}\" AS SELECT * FROM delta_scan('{table_path}');")
                        raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}_{tbl}\" AS SELECT * FROM delta_scan('{table_path}');")
                    except Exception as e_v:
                        logger.debug(f"View creation notice for Delta table {table_path}: {e_v}")
            except Exception as e_dt:
                logger.debug(f"Delta table discovery notice for {catalog_name}: {e_dt}")

            # 5. Discover and register standalone Parquet files
            try:
                files = raw_conn.execute(f"SELECT file FROM glob('s3://{bucket}/**.parquet') LIMIT 20").fetchall()
                for f_row in files:
                    file_path = f_row[0]
                    if "_delta_log" in file_path:
                        continue
                    base_name = os.path.basename(file_path).replace(".parquet", "").replace("-", "_").replace(".", "_")
                    exact_name = os.path.basename(file_path)
                    try:
                        raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}\".dbo.\"{base_name}\" AS SELECT * FROM read_parquet('{file_path}');")
                        raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}\".dbo.\"{exact_name}\" AS SELECT * FROM read_parquet('{file_path}');")
                        if bucket:
                            raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}\".\"{bucket}\".\"{base_name}\" AS SELECT * FROM read_parquet('{file_path}');")
                            raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}\".\"{bucket}\".\"{exact_name}\" AS SELECT * FROM read_parquet('{file_path}');")
                        raw_conn.execute(f"CREATE OR REPLACE VIEW \"{catalog_name}_{base_name}\" AS SELECT * FROM read_parquet('{file_path}');")
                    except Exception:
                        pass
            except Exception as e_pq:
                logger.debug(f"S3 glob / view registration notice for {catalog_name}: {e_pq}")

            logger.info(f"Successfully configured and attached S3 mount '{catalog_name}'")
            return True

        elif m_type == "sqlite":
            if catalog_name in existing_dbs:
                return True
            path = config.get("path", "").strip()
            if os.path.exists(path):
                raw_conn.execute("INSTALL sqlite; LOAD sqlite;")
                raw_conn.execute(f"ATTACH '{path}' AS {catalog_name} (TYPE sqlite, READ_ONLY {ro_str});")
                logger.info(f"Successfully attached SQLite mount '{catalog_name}'")
                return True
            else:
                logger.warning(f"SQLite path '{path}' does not exist.")
                return False

    except Exception as e:
        logger.warning(f"Could not attach storage mount '{catalog_name}' ({m_type}): {e}")
        return False

    return False


def sync_all_mounts(conn):
    """Iterates through all configured storage mounts and attaches them to DuckDB."""
    mounts = load_mounts()
    for mount in mounts:
        try:
            attach_mount_to_duckdb(conn, mount)
        except Exception as e:
            logger.warning(f"Failed to sync mount {mount.get('catalog_name')}: {e}")


def get_mount_catalogs_metadata(conn) -> List[Dict[str, Any]]:
    """Inspects attached external mounts in DuckDB and extracts schema and table metadata for Catalog Explorer."""
    mounts = load_mounts()
    raw_conn = getattr(conn, "con", conn)
    catalogs_meta = []

    for mount in mounts:
        if not mount.get("enabled", True):
            continue

        cid = mount["catalog_name"]
        m_type = mount["type"]
        name = mount["name"]
        desc = mount.get("description", "")
        schemas_map: Dict[str, List[Dict[str, Any]]] = {}

        try:
            if m_type == "postgres":
                tables = raw_conn.execute(
                    f"SELECT table_schema, table_name FROM information_schema.tables "
                    f"WHERE table_catalog = '{cid}' AND table_schema NOT IN ('information_schema', 'pg_catalog')"
                ).fetchall()
                for schema_name, table_name in tables:
                    if schema_name not in schemas_map:
                        schemas_map[schema_name] = []
                    
                    # Row count estimate if available
                    row_count = 0
                    try:
                        row_count = raw_conn.execute(f"SELECT COUNT(*) FROM {cid}.{schema_name}.{table_name}").fetchone()[0]
                    except Exception:
                        pass

                    schemas_map[schema_name].append({
                        "catalog": cid,
                        "schema": schema_name,
                        "name": table_name,
                        "full_name": f"{cid}.{schema_name}.{table_name}",
                        "canonical_name": f"{cid}.{schema_name}.{table_name}",
                        "mount_type": "postgres",
                        "row_count": row_count,
                        "size_bytes": 0,
                        "is_federated": True
                    })

            elif m_type == "sqlite":
                tables = raw_conn.execute(
                    f"SELECT table_schema, table_name FROM information_schema.tables WHERE table_catalog = '{cid}'"
                ).fetchall()
                for schema_name, table_name in tables:
                    s_name = schema_name or "main"
                    if s_name not in schemas_map:
                        schemas_map[s_name] = []
                    
                    row_count = 0
                    try:
                        row_count = raw_conn.execute(f"SELECT COUNT(*) FROM {cid}.{s_name}.{table_name}").fetchone()[0]
                    except Exception:
                        pass

                    schemas_map[s_name].append({
                        "catalog": cid,
                        "schema": s_name,
                        "name": table_name,
                        "full_name": f"{cid}.{s_name}.{table_name}",
                        "canonical_name": f"{cid}.{s_name}.{table_name}",
                        "mount_type": "sqlite",
                        "row_count": row_count,
                        "size_bytes": 0,
                        "is_federated": True
                    })

            elif m_type == "s3":
                bucket = mount["config"].get("bucket", "localspark")
                schemas_map[bucket] = []
                delta_dirs = set()

                # 1. Discover Delta Lake tables in S3 (folders containing _delta_log/*.json)
                try:
                    delta_logs = raw_conn.execute(f"SELECT file FROM glob('s3://{bucket}/**/_delta_log/0*.json')").fetchall()
                    for (log_file,) in delta_logs:
                        dt_path = log_file.split('/_delta_log/')[0]
                        delta_dirs.add(dt_path)
                except Exception as e:
                    logger.debug(f"Delta S3 glob notice for {cid}: {e}")

                for dt_path in sorted(delta_dirs):
                    rel_parts = dt_path.replace(f"s3://{bucket}/", "").split("/")
                    if len(rel_parts) >= 2:
                        s_name = rel_parts[0]
                        t_name = "/".join(rel_parts[1:])
                    else:
                        s_name = "dbo"
                        t_name = rel_parts[0]

                    if s_name not in schemas_map:
                        schemas_map[s_name] = []

                    row_count = None
                    try:
                        row_count = raw_conn.execute(f"SELECT COUNT(*) FROM delta_scan('{dt_path}')").fetchone()[0]
                    except Exception:
                        pass

                    clean_id = f"{cid}_{s_name}_{t_name}".replace("-", "_").replace(".", "_").replace("/", "_")
                    schemas_map[s_name].append({
                        "catalog": cid,
                        "schema": s_name,
                        "name": t_name,
                        "full_name": f"delta_scan('{dt_path}')",
                        "canonical_name": f"{cid}.{s_name}.{t_name}",
                        "view_name": clean_id,
                        "mount_type": "s3",
                        "format": "delta",
                        "is_delta": True,
                        "row_count": row_count,
                        "size_bytes": 0,
                        "path": dt_path,
                        "is_federated": True
                    })

                # 2. Discover standalone Parquet files in S3
                try:
                    s3_files = raw_conn.execute(f"SELECT file FROM glob('s3://{bucket}/**.parquet')").fetchall()
                    for (fpath,) in s3_files:
                        # Exclude files inside Delta table directories
                        if any(dt_dir in fpath for dt_dir in delta_dirs):
                            continue

                        fname = os.path.basename(fpath)
                        base_name = fname.replace(".parquet", "").replace("-", "_").replace(".", "_")
                        schemas_map[bucket].append({
                            "catalog": cid,
                            "schema": bucket,
                            "name": fname,
                            "full_name": f"read_parquet('s3://{bucket}/{fname}')",
                            "canonical_name": f"{cid}.{bucket}.{base_name}",
                            "view_name": f"{cid}_{base_name}",
                            "mount_type": "s3",
                            "format": "parquet",
                            "is_delta": False,
                            "row_count": None,
                            "size_bytes": 0,
                            "path": fpath,
                            "is_federated": True
                        })
                except Exception as e:
                    logger.debug(f"Could not list S3 files for metadata: {e}")

        except Exception as e:
            logger.warning(f"Error reading catalog metadata for mount {cid}: {e}")

        schemas_list = [{"name": s_name, "tables": s_tables} for s_name, s_tables in sorted(schemas_map.items())]
        
        catalogs_meta.append({
            "id": cid,
            "name": name,
            "description": desc,
            "mount_id": mount["id"],
            "mount_type": m_type,
            "is_mounted": True,
            "read_only": mount.get("read_only", True),
            "owner": mount.get("owner", "admin"),
            "schemas": schemas_list,
            "table_count": sum(len(s["tables"]) for s in schemas_list)
        })

    return catalogs_meta
