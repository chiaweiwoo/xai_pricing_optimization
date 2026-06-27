import sqlite3

from xai_pricing.validation import run_quality_checks


def test_validation_flags_missing_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE stores (store_id INTEGER PRIMARY KEY);
        CREATE TABLE products (upc TEXT PRIMARY KEY);
        CREATE TABLE weekly_sales (
            dataset_version_id TEXT,
            week_end_date TEXT,
            store_id INTEGER,
            upc TEXT,
            units REAL,
            visits REAL,
            households REAL,
            spend REAL,
            price REAL,
            base_price REAL,
            feature INTEGER,
            display INTEGER,
            tpr_only INTEGER
        );
        CREATE TABLE quality_checks (
            quality_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_version_id TEXT,
            check_name TEXT,
            severity TEXT,
            passed INTEGER,
            details_json TEXT,
            created_at TEXT
        );
        """
    )

    checks = run_quality_checks(conn, "demo", persist=True)

    assert any(check.check_name == "sales_rows_present" and not check.passed for check in checks)
