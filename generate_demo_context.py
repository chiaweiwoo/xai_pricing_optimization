"""Create deterministic synthetic commercial context and candidate outcomes."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from ingest import DATASET_VERSION_ID
from xai_pricing.db import get_conn
from xai_pricing.synthetic import build_demo_scenario


def main() -> None:
    conn = get_conn()
    try:
        stats = build_demo_scenario(conn, DATASET_VERSION_ID)
    finally:
        conn.close()

    print(f"Products in scenario:      {stats['products']}")
    print(f"Candidate outcomes loaded: {stats['candidate_outcomes']}")
    print(f"Planning week end:         {stats['planning_week_end']}")


if __name__ == "__main__":
    main()
