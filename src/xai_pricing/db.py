import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DB_PATH, MIGRATIONS_DIR


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_name TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _apply_migrations(conn: sqlite3.Connection) -> None:
    _ensure_migration_table(conn)
    applied = {
        row["migration_name"]: row["checksum"]
        for row in conn.execute(
            "SELECT migration_name, checksum FROM schema_migrations"
        ).fetchall()
    }

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if path.name in applied:
            if applied[path.name] != checksum:
                raise RuntimeError(
                    f"Migration checksum drift detected for {path.name}. "
                    "Create a new migration instead of editing an applied one."
                )
            continue
        conn.executescript(sql)
        conn.execute(
            """
            INSERT INTO schema_migrations (migration_name, checksum, applied_at)
            VALUES (?, ?, ?)
            """,
            (path.name, checksum, utc_now()),
        )
        conn.commit()


def get_conn(path: str | Path = DB_PATH) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _apply_migrations(conn)
    return conn


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def record_quality_check(
    conn: sqlite3.Connection,
    *,
    dataset_version_id: str,
    check_name: str,
    severity: str,
    passed: bool,
    details: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO quality_checks (
            dataset_version_id, check_name, severity, passed, details_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            dataset_version_id,
            check_name,
            severity,
            int(passed),
            json_dumps(details),
            utc_now(),
        ),
    )


def replace_quality_checks(conn: sqlite3.Connection, dataset_version_id: str) -> None:
    conn.execute(
        "DELETE FROM quality_checks WHERE dataset_version_id = ?",
        (dataset_version_id,),
    )


def register_dataset_version(
    conn: sqlite3.Connection,
    *,
    dataset_version_id: str,
    source_name: str,
    source_url: str,
    archive_path: str,
    archive_sha256: str,
    workbook_path: str,
    metadata: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO dataset_versions (
            dataset_version_id,
            source_name,
            source_url,
            archive_path,
            archive_sha256,
            workbook_path,
            metadata_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dataset_version_id) DO UPDATE SET
            source_name = excluded.source_name,
            source_url = excluded.source_url,
            archive_path = excluded.archive_path,
            archive_sha256 = excluded.archive_sha256,
            workbook_path = excluded.workbook_path,
            metadata_json = excluded.metadata_json
        """,
        (
            dataset_version_id,
            source_name,
            source_url,
            archive_path,
            archive_sha256,
            workbook_path,
            json_dumps(metadata),
            utc_now(),
        ),
    )


def upsert_ingestion_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    dataset_version_id: str,
    status: str,
    started_at: str,
    completed_at: str | None,
    details: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, dataset_version_id, status, started_at, completed_at, details_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status = excluded.status,
            completed_at = excluded.completed_at,
            details_json = excluded.details_json
        """,
        (
            run_id,
            dataset_version_id,
            status,
            started_at,
            completed_at,
            json_dumps(details),
        ),
    )
