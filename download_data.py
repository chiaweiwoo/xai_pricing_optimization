"""Download and verify the public Breakfast at the Frat source archive."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from xai_pricing.dunnhumby import ensure_source_download, sha256_file


def main() -> None:
    paths = ensure_source_download()
    print(f"Archive:  {paths.archive_path}")
    print(f"SHA-256:  {sha256_file(paths.archive_path)}")
    print(f"Workbook: {paths.workbook_path}")
    print(f"Guide:    {paths.guide_path}")


if __name__ == "__main__":
    main()
