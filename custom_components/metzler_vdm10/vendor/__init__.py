"""Release loader for the vendored metzler library.

In a Marketplace install there is no `metzler` package on the Python path: this
package's directory (containing `metzler/` after `tools/vendor.py` has run) is
appended to sys.path so `import metzler` resolves to the vendored copy. The
vendored tree keeps its absolute `from metzler.x import y` imports byte-identical.

Appending (not prepending) keeps the dev path-dependency winning in development.

`custom_components/vdm10/__init__.py` does `from . import vendor` before the
first `import metzler` — the side effect is all that is needed.
"""

import sys
from pathlib import Path

_vendor_dir = str(Path(__file__).resolve().parent)
if _vendor_dir not in sys.path:
    sys.path.append(_vendor_dir)
