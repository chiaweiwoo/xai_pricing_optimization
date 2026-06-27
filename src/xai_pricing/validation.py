import json
import sqlite3
from dataclasses import dataclass

from .db import record_quality_check, replace_quality_checks


@dataclass(frozen=True)
class QualityResult:
    check_name: str
    severity: str
    passed: bool
    details: dict


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> object:
    return conn.execute(sql, params).fetchone()[0]


def run_quality_checks(
    conn: sqlite3.Connection,
    dataset_version_id: str,
    *,
    persist: bool = True,
) -> list[QualityResult]:
    checks: list[QualityResult] = []

    sales_rows = int(
        _scalar(
            conn,
            "SELECT COUNT(*) FROM weekly_sales WHERE dataset_version_id = ?",
            (dataset_version_id,),
        )
    )
    checks.append(
        QualityResult(
            "sales_rows_present",
            "error",
            sales_rows > 0,
            {"sales_rows": sales_rows},
        )
    )

    store_count = int(_scalar(conn, "SELECT COUNT(*) FROM stores"))
    product_count = int(_scalar(conn, "SELECT COUNT(*) FROM products"))
    active_counts = conn.execute(
        """
        SELECT COUNT(DISTINCT store_id), COUNT(DISTINCT upc)
        FROM weekly_sales
        WHERE dataset_version_id = ?
        """,
        (dataset_version_id,),
    ).fetchone()
    active_store_count = int(active_counts[0])
    active_product_count = int(active_counts[1])
    inactive_lookup_products = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM (
                SELECT upc FROM products
                EXCEPT
                SELECT DISTINCT upc FROM weekly_sales WHERE dataset_version_id = ?
            )
            """,
            (dataset_version_id,),
        )
    )
    checks.append(
        QualityResult(
            "lookup_coverage_expected",
            "warn",
            active_store_count == 77 and active_product_count == 55 and inactive_lookup_products <= 3,
            {
                "lookup_stores": store_count,
                "lookup_products": product_count,
                "active_stores": active_store_count,
                "active_products": active_product_count,
                "inactive_lookup_products": inactive_lookup_products,
            },
        )
    )

    orphan_products = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM weekly_sales s
            LEFT JOIN products p ON p.upc = s.upc
            WHERE s.dataset_version_id = ? AND p.upc IS NULL
            """,
            (dataset_version_id,),
        )
    )
    orphan_stores = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM weekly_sales s
            LEFT JOIN stores st ON st.store_id = s.store_id
            WHERE s.dataset_version_id = ? AND st.store_id IS NULL
            """,
            (dataset_version_id,),
        )
    )
    checks.append(
        QualityResult(
            "no_lookup_orphans",
            "error",
            orphan_products == 0 and orphan_stores == 0,
            {
                "orphan_products": orphan_products,
                "orphan_stores": orphan_stores,
            },
        )
    )

    distinct_weeks = int(
        _scalar(
            conn,
            "SELECT COUNT(DISTINCT week_end_date) FROM weekly_sales WHERE dataset_version_id = ?",
            (dataset_version_id,),
        )
    )
    checks.append(
        QualityResult(
            "expected_week_count",
            "warn",
            distinct_weeks == 156,
            {"distinct_weeks": distinct_weeks},
        )
    )

    missing_price = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*) FROM weekly_sales
            WHERE dataset_version_id = ? AND price IS NULL
            """,
            (dataset_version_id,),
        )
    )
    missing_base_price = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*) FROM weekly_sales
            WHERE dataset_version_id = ? AND base_price IS NULL
            """,
            (dataset_version_id,),
        )
    )
    checks.append(
        QualityResult(
            "price_missingness_small",
            "warn",
            missing_price <= 30 and missing_base_price <= 200,
            {
                "missing_price": missing_price,
                "missing_base_price": missing_base_price,
            },
        )
    )

    mismatched_spend = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM weekly_sales
            WHERE dataset_version_id = ?
              AND units IS NOT NULL
              AND price IS NOT NULL
              AND spend IS NOT NULL
              AND ABS((units * price) - spend) > 0.011
            """,
            (dataset_version_id,),
        )
    )
    checks.append(
        QualityResult(
            "spend_matches_units_times_price",
            "warn",
            mismatched_spend == 0,
            {"mismatched_rows": mismatched_spend},
        )
    )

    price_above_base = int(
        _scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM weekly_sales
            WHERE dataset_version_id = ?
              AND price IS NOT NULL
              AND base_price IS NOT NULL
              AND price > base_price
            """,
            (dataset_version_id,),
        )
    )
    checks.append(
        QualityResult(
            "price_above_base_flagged",
            "warn",
            True,
            {"rows": price_above_base},
        )
    )

    if persist:
        replace_quality_checks(conn, dataset_version_id)
        for check in checks:
            record_quality_check(
                conn,
                dataset_version_id=dataset_version_id,
                check_name=check.check_name,
                severity=check.severity,
                passed=check.passed,
                details=check.details,
            )
        conn.commit()

    return checks


def summarize_checks(checks: list[QualityResult]) -> dict[str, object]:
    return {
        "errors": [check.check_name for check in checks if check.severity == "error" and not check.passed],
        "warnings": [
            {
                "check_name": check.check_name,
                "passed": check.passed,
                "details": check.details,
            }
            for check in checks
            if check.severity == "warn"
        ],
    }


def checks_to_json(checks: list[QualityResult]) -> str:
    return json.dumps(
        [
            {
                "check_name": check.check_name,
                "severity": check.severity,
                "passed": check.passed,
                "details": check.details,
            }
            for check in checks
        ],
        indent=2,
        sort_keys=True,
    )
