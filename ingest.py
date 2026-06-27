"""Stream the dunnhumby workbook into SQLite."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from xai_pricing.config import (
    SOURCE_ARCHIVE_SHA256,
    SOURCE_NAME,
    SOURCE_URL,
)
from xai_pricing.db import get_conn, register_dataset_version, upsert_ingestion_run, utc_now
from xai_pricing.dunnhumby import ensure_source_download, open_workbook, sheet_headers

DATASET_VERSION_ID = f"{SOURCE_NAME}_{SOURCE_ARCHIVE_SHA256[:12].lower()}"


def _coerce_date(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _coerce_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _chunked(rows: Iterable[tuple], batch_size: int = 5000) -> Iterable[list[tuple]]:
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _store_rows(workbook) -> list[tuple]:
    ws = workbook["dh Store Lookup"]
    headers = sheet_headers(ws)
    expected = [
        "STORE_ID",
        "STORE_NAME",
        "ADDRESS_CITY_NAME",
        "ADDRESS_STATE_PROV_CODE",
        "MSA_CODE",
        "SEG_VALUE_NAME",
        "PARKING_SPACE_QTY",
        "SALES_AREA_SIZE_NUM",
        "AVG_WEEKLY_BASKETS",
    ]
    if headers[: len(expected)] != expected:
        raise RuntimeError(f"Unexpected store sheet headers: {headers[:len(expected)]}")
    by_store_id: dict[int, tuple] = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        if row[0] is None:
            continue
        store_id = _coerce_int(row[0])
        if store_id is None:
            continue
        by_store_id[store_id] = (
            store_id,
            _coerce_text(row[1]),
            _coerce_text(row[2]),
            _coerce_text(row[3]),
            _coerce_text(row[4]),
            _coerce_text(row[5]),
            _coerce_int(row[6]),
            _coerce_float(row[7]),
            _coerce_float(row[8]),
        )
    return list(by_store_id.values())


def _product_rows(workbook) -> list[tuple]:
    ws = workbook["dh Products Lookup"]
    headers = sheet_headers(ws)
    expected = [
        "UPC",
        "DESCRIPTION",
        "MANUFACTURER",
        "CATEGORY",
        "SUB_CATEGORY",
        "PRODUCT_SIZE",
    ]
    if headers[: len(expected)] != expected:
        raise RuntimeError(f"Unexpected product sheet headers: {headers[:len(expected)]}")
    by_upc: dict[str, tuple] = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        if row[0] is None:
            continue
        upc = _coerce_text(row[0])
        if upc is None:
            continue
        by_upc[upc] = (
            upc,
            _coerce_text(row[1]),
            _coerce_text(row[2]),
            _coerce_text(row[3]),
            _coerce_text(row[4]),
            _coerce_text(row[5]),
        )
    return list(by_upc.values())


def _transaction_rows(workbook) -> tuple[Iterable[tuple], dict[str, int]]:
    ws = workbook["dh Transaction Data"]
    headers = sheet_headers(ws)
    expected = [
        "WEEK_END_DATE",
        "STORE_NUM",
        "UPC",
        "UNITS",
        "VISITS",
        "HHS",
        "SPEND",
        "PRICE",
        "BASE_PRICE",
        "FEATURE",
        "DISPLAY",
        "TPR_ONLY",
    ]
    if headers[: len(expected)] != expected:
        raise RuntimeError(f"Unexpected transaction sheet headers: {headers[:len(expected)]}")

    stats = {"populated_rows": 0, "ignored_rows": 0}

    def iterator() -> Iterable[tuple]:
        for row in ws.iter_rows(min_row=3, values_only=True):
            week_end_date = _coerce_date(row[0])
            store_id = _coerce_int(row[1])
            upc = _coerce_text(row[2])
            if week_end_date is None and store_id is None and upc is None:
                stats["ignored_rows"] += 1
                continue
            if week_end_date is None or store_id is None or upc is None:
                stats["ignored_rows"] += 1
                continue
            stats["populated_rows"] += 1
            yield (
                DATASET_VERSION_ID,
                week_end_date,
                store_id,
                upc,
                _coerce_float(row[3]),
                _coerce_float(row[4]),
                _coerce_float(row[5]),
                _coerce_float(row[6]),
                _coerce_float(row[7]),
                _coerce_float(row[8]),
                _coerce_int(row[9]) or 0,
                _coerce_int(row[10]) or 0,
                _coerce_int(row[11]) or 0,
            )

    return iterator(), stats


def main() -> None:
    source = ensure_source_download()
    workbook = open_workbook(source.workbook_path)
    started_at = utc_now()
    run_id = f"ingest_{started_at.replace(':', '').replace('+00:00', 'z')}"

    conn = get_conn()
    try:
        register_dataset_version(
            conn,
            dataset_version_id=DATASET_VERSION_ID,
            source_name=SOURCE_NAME,
            source_url=SOURCE_URL,
            archive_path=str(source.archive_path),
            archive_sha256=SOURCE_ARCHIVE_SHA256,
            workbook_path=str(source.workbook_path),
            metadata={
                "workbook_name": source.workbook_path.name,
                "guide_name": source.guide_path.name,
            },
        )
        upsert_ingestion_run(
            conn,
            run_id=run_id,
            dataset_version_id=DATASET_VERSION_ID,
            status="running",
            started_at=started_at,
            completed_at=None,
            details={},
        )
        conn.commit()

        store_rows = _store_rows(workbook)
        product_rows = _product_rows(workbook)
        transaction_iter, transaction_stats = _transaction_rows(workbook)

        conn.execute("DELETE FROM weekly_sales WHERE dataset_version_id = ?", (DATASET_VERSION_ID,))
        conn.executemany(
            """
            INSERT OR REPLACE INTO stores (
                store_id, store_name, city, state_prov_code, msa_code,
                seg_value_name, parking_space_qty, sales_area_size_num, avg_weekly_baskets
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            store_rows,
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO products (
                upc, description, manufacturer, category, sub_category, product_size
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            product_rows,
        )
        for batch in _chunked(transaction_iter):
            conn.executemany(
                """
                INSERT INTO weekly_sales (
                    dataset_version_id, week_end_date, store_id, upc,
                    units, visits, households, spend, price, base_price,
                    feature, display, tpr_only
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
        conn.commit()

        post_counts = conn.execute(
            """
            SELECT
                COUNT(*) AS sales_rows,
                COUNT(DISTINCT week_end_date) AS weeks,
                COUNT(DISTINCT store_id) AS stores,
                COUNT(DISTINCT upc) AS products
            FROM weekly_sales
            WHERE dataset_version_id = ?
            """,
            (DATASET_VERSION_ID,),
        ).fetchone()

        register_dataset_version(
            conn,
            dataset_version_id=DATASET_VERSION_ID,
            source_name=SOURCE_NAME,
            source_url=SOURCE_URL,
            archive_path=str(source.archive_path),
            archive_sha256=SOURCE_ARCHIVE_SHA256,
            workbook_path=str(source.workbook_path),
            metadata={
                "workbook_name": source.workbook_path.name,
                "guide_name": source.guide_path.name,
                "populated_transaction_rows": transaction_stats["populated_rows"],
                "ignored_transaction_rows": transaction_stats["ignored_rows"],
                "distinct_weeks": post_counts["weeks"],
                "distinct_stores": post_counts["stores"],
                "distinct_products": post_counts["products"],
            },
        )
        upsert_ingestion_run(
            conn,
            run_id=run_id,
            dataset_version_id=DATASET_VERSION_ID,
            status="completed",
            started_at=started_at,
            completed_at=utc_now(),
            details={
                "stores_loaded": len(store_rows),
                "products_loaded": len(product_rows),
                "sales_rows_loaded": post_counts["sales_rows"],
                "populated_transaction_rows": transaction_stats["populated_rows"],
                "ignored_transaction_rows": transaction_stats["ignored_rows"],
            },
        )
        conn.commit()
    except Exception:
        upsert_ingestion_run(
            conn,
            run_id=run_id,
            dataset_version_id=DATASET_VERSION_ID,
            status="failed",
            started_at=started_at,
            completed_at=utc_now(),
            details={},
        )
        conn.commit()
        raise
    finally:
        workbook.close()
        conn.close()

    print(f"Dataset version: {DATASET_VERSION_ID}")
    print(f"Stores loaded:   {len(store_rows)}")
    print(f"Products loaded: {len(product_rows)}")
    print(f"Sales rows:      {post_counts['sales_rows']:,}")
    print(f"Ignored rows:    {transaction_stats['ignored_rows']:,}")


if __name__ == "__main__":
    main()
