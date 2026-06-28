from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency in early environments
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[2]
if load_dotenv is not None:
    load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RAW_DATA_DIR = DATA_DIR / "raw" / "dunnhumby_breakfast_at_the_frat"
DB_PATH = ROOT / "db" / "pricing_optimization.db"
REPORTS_DIR = ROOT / "reports" / "generated"
MIGRATIONS_DIR = ROOT / "migrations"

SOURCE_NAME = "dunnhumby_breakfast_at_the_frat"
SOURCE_URL = (
    "https://downloads.ctfassets.net/psj0p18eh7z1/7B4xI3FKcgMLak9Wm95IHC/"
    "3b63fe3d37bc7122d13524514be4ab51/dunnhumby_Breakfast-at-the-Frat.zip"
)
SOURCE_ARCHIVE_NAME = "dunnhumby_Breakfast-at-the-Frat.zip"
SOURCE_ARCHIVE_SHA256 = "74CB41CB8B19DC61BB8A5731C3774B802E9A8DA3B64CDD1872640890D0B54216"
SOURCE_WORKBOOK_NAME = "dunnhumby - Breakfast at the Frat.xlsx"
SOURCE_GUIDE_NAME = "dunnhumby - Breakfast at the Frat User Guide.pdf"

DEFAULT_SCENARIO_ID = "promotion_campaign_v1"
DEFAULT_SCENARIO_NAME = "Promotion Campaign v1"
DEFAULT_SYNTHETIC_SEED = 20260628
