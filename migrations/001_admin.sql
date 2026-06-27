CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_version_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    workbook_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    dataset_version_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    details_json TEXT NOT NULL,
    FOREIGN KEY (dataset_version_id) REFERENCES dataset_versions(dataset_version_id)
);

CREATE TABLE IF NOT EXISTS quality_checks (
    quality_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_version_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    passed INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (dataset_version_id) REFERENCES dataset_versions(dataset_version_id)
);

CREATE INDEX IF NOT EXISTS idx_quality_checks_dataset
ON quality_checks(dataset_version_id, created_at);
