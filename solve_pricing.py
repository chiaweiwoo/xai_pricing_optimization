"""Solve the pricing optimization scenario and print an auditable OR report."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from xai_pricing.config import DEFAULT_SCENARIO_ID
from xai_pricing.db import get_conn
from xai_pricing.optimizer import PricingOptimizer, SolveRequest, format_solve_report


def main() -> None:
    scenario_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO_ID

    conn = get_conn()
    try:
        optimizer = PricingOptimizer(conn)
        result = optimizer.solve(SolveRequest(scenario_id=scenario_id))
    finally:
        conn.close()

    print(format_solve_report(result))


if __name__ == "__main__":
    main()
