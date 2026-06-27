from pathlib import Path
from datetime import date, timedelta
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xai_pricing.db import get_conn, json_dumps, utc_now


@pytest.fixture
def seeded_conn(tmp_path: Path):
    dataset_version_id = "test_dataset_v1"
    conn = get_conn(tmp_path / "test.db")
    conn.execute(
        """
        INSERT INTO dataset_versions (
            dataset_version_id, source_name, source_url, archive_path,
            archive_sha256, workbook_path, metadata_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dataset_version_id,
            "test_source",
            "https://example.com/data.zip",
            "archive.zip",
            "sha256",
            "workbook.xlsx",
            json_dumps({}),
            utc_now(),
        ),
    )
    conn.execute(
        """
        INSERT INTO stores (
            store_id, store_name, city, state_prov_code, msa_code,
            seg_value_name, parking_space_qty, sales_area_size_num, avg_weekly_baskets
        )
        VALUES (1, 'Test Store', 'Austin', 'TX', '001', 'A', 100, 5000, 2500)
        """
    )

    products = [
        ("1001", "Cereal A", "Alpha", "COLD CEREAL", "CEREAL", "12 OZ", 260, 4.29),
        ("1002", "Cereal B", "Alpha", "COLD CEREAL", "CEREAL", "14 OZ", 225, 3.99),
        ("1003", "Pretzel A", "Bravo", "SALTY SNACKS", "PRETZELS", "10 OZ", 210, 2.79),
        ("1004", "Pretzel B", "Bravo", "SALTY SNACKS", "PRETZELS", "8 OZ", 180, 2.49),
        ("1005", "Pizza A", "Charlie", "FROZEN", "PIZZA", "16 OZ", 160, 5.99),
        ("1006", "Pizza B", "Charlie", "FROZEN", "PIZZA", "18 OZ", 145, 6.29),
        ("1007", "Mouthwash", "Delta", "ORAL CARE", "MOUTHWASH", "500 ML", 70, 5.49),
        ("1008", "Granola", "Echo", "BREAKFAST", "BARS", "6 CT", 120, 3.19),
    ]
    conn.executemany(
        """
        INSERT INTO products (upc, description, manufacturer, category, sub_category, product_size)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(upc, desc, mfg, cat, sub, size) for upc, desc, mfg, cat, sub, size, _, _ in products],
    )

    start_date = date(2021, 1, 6)
    sales_rows = []
    for week_idx in range(20):
        week_end = (start_date + timedelta(weeks=week_idx)).isoformat()
        for product_idx, (upc, _, _, _, _, _, base_units, base_price) in enumerate(products):
            unit_multiplier = 1 + (((week_idx + product_idx) % 4) - 1.5) * 0.03
            price_multiplier = 1 + (((week_idx + product_idx) % 3) - 1) * 0.01
            units = round(base_units * unit_multiplier, 2)
            price = round(base_price * price_multiplier, 2)
            spend = round(units * price, 2)
            sales_rows.append(
                (
                    dataset_version_id,
                    week_end,
                    1,
                    upc,
                    units,
                    units * 4,
                    units * 2,
                    spend,
                    price,
                    base_price,
                    0,
                    0,
                    0,
                )
            )
    conn.executemany(
        """
        INSERT INTO weekly_sales (
            dataset_version_id, week_end_date, store_id, upc,
            units, visits, households, spend, price, base_price,
            feature, display, tpr_only
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        sales_rows,
    )
    conn.commit()

    try:
        yield conn, dataset_version_id
    finally:
        conn.close()
