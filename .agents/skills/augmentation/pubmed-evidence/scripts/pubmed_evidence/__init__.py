"""The pubmed-evidence skill's library: one module per step of the augmentation agent.

`augment.py` next to this package runs the steps, and `agents/augmentation/cli.py` runs them
for `kgb-augment` and the pipeline. Every step reads and writes the repository's KGX records
(`shared/schema.py`), so the repository root goes on the import path when it isn't there
already (a script run with `uv run --script`).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[6]
if (_REPO / "shared" / "schema.py").exists() and str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))
