"""Run data quality checks against the ingested SQLite dataset."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from ingest import DATASET_VERSION_ID
from xai_pricing.db import get_conn
from xai_pricing.validation import run_quality_checks


def main() -> None:
    conn = get_conn()
    try:
        checks = run_quality_checks(conn, DATASET_VERSION_ID, persist=True)
    finally:
        conn.close()

    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"[{status}] {check.severity.upper():5s} {check.check_name} :: {check.details}")


if __name__ == "__main__":
    main()
