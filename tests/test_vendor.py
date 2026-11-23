"""Release shape: the vendored metzler must be importable in isolation (T033).

Runs tools/vendor.py, then imports metzler with ONLY the vendor dir on
PYTHONPATH - proving the release install works without the dev path-dependency,
and that the module really comes from the vendored tree.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "custom_components" / "metzler_vdm10" / "vendor"


def test_vendor_script_stamps_and_imports():
    subprocess.run([sys.executable, str(ROOT / "tools" / "vendor.py")], check=True, cwd=ROOT)
    code = (
        "import metzler, pathlib, sys; "
        f"p = pathlib.Path(metzler.__file__).resolve(); "
        f"assert str({str(VENDOR)!r}) in str(p), p; "
        "from metzler.station.station import IndoorStation; "
        "from metzler.station.ha_media import HaCallMedia; print(p)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],  # -B: don't write __pycache__ into the vendored tree
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(VENDOR), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr


def test_vendored_tree_has_no_pycache():
    tree = VENDOR / "metzler"
    if not tree.is_dir():
        import pytest

        pytest.skip("vendor.py has not run")
    assert not list(tree.rglob("__pycache__"))
    assert (tree / "_version.py").exists()
