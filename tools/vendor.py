#!/usr/bin/env python3
"""Vendor the metzler library into custom_components/vdm10/vendor/ for release.

The Marketplace installs this repository as-is; manifest `requirements` can only
name PyPI packages, so the sibling `metzler-vdm10` source tree is copied in.
Run from the vdm10-ha project root: `uv run python tools/vendor.py`.
"""

import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT.parent / "metzler-vdm10" / "src" / "metzler"
DST = ROOT / "custom_components" / "vdm10" / "vendor" / "metzler"


def main():
    if not SRC.is_dir():
        sys.exit(f"library source not found: {SRC} (expected sibling checkout ../metzler-vdm10)")
    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    pyproject = SRC.parents[1] / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text())["project"]["version"] if pyproject.exists() else "unknown"
    (DST / "_version.py").write_text(f'"""vendored library version stamp"""\n\nVERSION = "{version}"\n')
    print(f"vendored {SRC} -> {DST} (version {version})")


if __name__ == "__main__":
    main()
