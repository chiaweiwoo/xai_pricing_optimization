"""Create deterministic synthetic commercial context and candidate outcomes."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from ingest import DATASET_VERSION_ID
from xai_pricing.db import get_conn
from xai_pricing.synthetic import PROFILE_CONFIG, ScenarioSettings, build_demo_scenario


def main() -> None:
    conn = get_conn()
    try:
        stats = []
        for profile_id, profile in PROFILE_CONFIG.items():
            scenario_settings = ScenarioSettings(
                scenario_id=profile_id,
                scenario_name=profile["scenario_name"],
                profile_id=profile_id,
            )
            stats.append(build_demo_scenario(conn, DATASET_VERSION_ID, scenario_settings))
    finally:
        conn.close()

    for item in stats:
        print(f"Scenario:                  {item['profile_id']}")
        print(f"Products in scenario:      {item['products']}")
        print(f"Candidate outcomes loaded: {item['candidate_outcomes']}")
        print(f"Hard-valid candidates:     {item['hard_valid_candidates']}")
        print(f"Promotable products:       {item['promotable_products']}")
        print(f"Planning week end:         {item['planning_week_end']}")
        print()


if __name__ == "__main__":
    main()
