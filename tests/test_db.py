from pathlib import Path

from xai_pricing.db import get_conn


def test_get_conn_applies_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_conn(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "dataset_versions" in tables
    assert "weekly_sales" in tables
    assert "candidate_outcomes" in tables
    assert "v_chain_sku_week" in tables
