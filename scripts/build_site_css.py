"""Write assets/site.css from the one design system module.

    PYTHONPATH=src python scripts/build_site_css.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lineage_agent import design  # noqa: E402

OUTPUT = PROJECT_ROOT / "assets" / "site.css"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(design.SITE_CSS, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(PROJECT_ROOT)} ({len(design.SITE_CSS)} bytes)")


if __name__ == "__main__":
    main()
