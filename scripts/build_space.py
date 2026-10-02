"""Assemble the files of the Hugging Face Space into a folder.

The Space is a separate git repository holding only what the container needs:
the application, the Space `README.md` (with its YAML header) and `Dockerfile`
from `deploy/huggingface/`, and the real layers of `data/lux/`. Files are
copied from an explicit list, so `.env` and other local files can never end up
in the Space.

Usage:  python scripts/build_space.py ../geo-assistant-space
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("build_space")

ROOT = Path(__file__).resolve().parents[1]

# Destination in the Space -> source in this repository.
FILES = {
    "README.md": "deploy/huggingface/README.md",
    "Dockerfile": "deploy/huggingface/Dockerfile",
    "pyproject.toml": "pyproject.toml",
    "LICENSE": "LICENSE",
}
FOLDERS = {"app": "app", "data/lux": "data/lux"}
# Extensions copied from the folders above (no caches, no stray files).
SUFFIXES = {".py", ".html", ".geojson", ".json"}
REQUIRED_DATA = "data/lux/SOURCES.json"


def build_space(root: Path, out: Path) -> list[Path]:
    """Copy the Space files from `root` to `out`; return the files written.

    Folders managed by this script are replaced, so files deleted from the
    project also disappear from the Space. Anything else in `out` (its `.git`
    folder in particular) is left untouched.
    """
    if not (root / REQUIRED_DATA).is_file():
        raise FileNotFoundError(
            f"{REQUIRED_DATA} not found: run scripts/fetch_lux_data.py first, so the Space "
            "ships the layers together with their provenance."
        )
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for destination, source in FILES.items():
        target = out / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, target)
        written.append(target)
    for destination, source in FOLDERS.items():
        target_dir = out / destination
        if target_dir.exists():
            shutil.rmtree(target_dir)
        for path in sorted((root / source).rglob("*")):
            if path.is_file() and path.suffix in SUFFIXES and "__pycache__" not in path.parts:
                target = target_dir / path.relative_to(root / source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                written.append(target)
    return written


def main() -> int:
    """Build the Space folder given on the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="folder of the Space repository (a git clone)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        written = build_space(ROOT, args.out)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    size_kb = sum(p.stat().st_size for p in written) / 1000
    logger.info("Wrote %d files (%.0f kB) to %s", len(written), size_kb, args.out.resolve())
    if not (args.out / ".git").exists():
        logger.warning("%s is not a git repository: clone the Space there first", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
