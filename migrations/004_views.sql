CREATE VIEW IF NOT EXISTS v_valid_weekly_sales AS
SELECT
    dataset_version_id,
    week_end_date,
    store_id,
    upc,
    units,
    visits,
    households,
    spend,
    price,
    base_price,
    feature,
    display,
    tpr_only,
    CASE
        WHEN price IS NOT NULL AND base_price IS NOT NULL AND base_price <> 0
            THEN (base_price - price) / base_price
        ELSE NULL
    END AS discount_rate
FROM weekly_sales
WHERE units IS NOT NULL
  AND spend IS NOT NULL;

CREATE VIEW IF NOT EXISTS v_chain_sku_week AS
SELECT
    dataset_version_id,
    week_end_date,
    upc,
    SUM(units) AS units,
    SUM(visits) AS visits,
    SUM(households) AS households,
    SUM(spend) AS spend,
    AVG(price) AS price,
    AVG(base_price) AS base_price,
    MAX(feature) AS feature,
    MAX(display) AS display,
    MAX(tpr_only) AS tpr_only
FROM v_valid_weekly_sales
GROUP BY dataset_version_id, week_end_date, upc;
