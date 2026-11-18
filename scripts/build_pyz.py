"""Build the single-file release: ``dist/meetstream-agent-kit.pyz``.

A zip application that any Python 3.10+ runs directly
(``python3 meetstream-agent-kit.pyz serve``). The package has no dependencies,
so the archive is the whole program.
"""

from __future__ import annotations

import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
TARGET = ROOT / "dist" / "meetstream-agent-kit.pyz"


def _include(path: Path) -> bool:
    return "__pycache__" not in path.parts and path.suffix != ".pyc"


def main() -> None:
    TARGET.parent.mkdir(exist_ok=True)
    zipapp.create_archive(
        SOURCE,
        TARGET,
        interpreter="/usr/bin/env python3",
        main="meetstream_agent_kit.cli:main",
        filter=_include,
        compressed=True,
    )
    print(f"built {TARGET.relative_to(ROOT)} ({TARGET.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
