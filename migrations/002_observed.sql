CREATE TABLE IF NOT EXISTS stores (
    store_id INTEGER PRIMARY KEY,
    store_name TEXT,
    city TEXT,
    state_prov_code TEXT,
    msa_code TEXT,
    seg_value_name TEXT,
    parking_space_qty INTEGER,
    sales_area_size_num REAL,
    avg_weekly_baskets REAL
);

CREATE TABLE IF NOT EXISTS products (
    upc TEXT PRIMARY KEY,
    description TEXT,
    manufacturer TEXT,
    category TEXT,
    sub_category TEXT,
    product_size TEXT
);

CREATE TABLE IF NOT EXISTS weekly_sales (
    dataset_version_id TEXT NOT NULL,
    week_end_date TEXT NOT NULL,
    store_id INTEGER NOT NULL,
    upc TEXT NOT NULL,
    units REAL,
    visits REAL,
    households REAL,
    spend REAL,
    price REAL,
    base_price REAL,
    feature INTEGER,
    display INTEGER,
    tpr_only INTEGER,
    PRIMARY KEY (dataset_version_id, week_end_date, store_id, upc),
    FOREIGN KEY (dataset_version_id) REFERENCES dataset_versions(dataset_version_id),
    FOREIGN KEY (store_id) REFERENCES stores(store_id),
    FOREIGN KEY (upc) REFERENCES products(upc)
);

CREATE INDEX IF NOT EXISTS idx_weekly_sales_upc_week
ON weekly_sales(dataset_version_id, upc, week_end_date);

CREATE INDEX IF NOT EXISTS idx_weekly_sales_store_week
ON weekly_sales(dataset_version_id, store_id, week_end_date);
