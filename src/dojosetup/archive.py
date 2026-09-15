"""Archive extraction for .7z and .zip.

py7zr is a pure-Python 7z reader, so PyInstaller can bundle it and the frozen
exe needs no 7-Zip installed on the player's machine. .zip is stdlib.

`strip_top_level` matters: xNVSE's archive wraps everything in an
`nvse_6_4_8/` folder, and extracting that verbatim into the game root produces
a game that silently launches without the script extender. The manifest names
the folder to strip so that failure cannot happen.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Callable

import py7zr

ProgressFn = Callable[[str], None]


class ExtractError(RuntimeError):
    pass


def _safe_members(names: list[str], dest: Path) -> None:
    """Refuse absolute paths and ../ escapes before writing anything.

    These archives come from trusted sources and are hash-pinned, but this code
    runs on other people's machines and an extractor that can be talked into
    writing outside its destination is not something to ship.
    """
    dest_resolved = dest.resolve()
    for name in names:
        candidate = (dest / name).resolve()
        if not str(candidate).startswith(str(dest_resolved)):
            raise ExtractError(
                f"Archive entry '{name}' tries to write outside the target "
                "folder. Refusing to extract."
            )


def extract(archive: Path, dest: Path, *, kind: str | None = None,
            strip_top_level: str | None = None,
            log: ProgressFn | None = None) -> Path:
    """Extract `archive` into `dest`, optionally stripping one wrapper folder.

    Returns the directory the payload actually landed in.
    """
    dest.mkdir(parents=True, exist_ok=True)
    kind = (kind or archive.suffix.lstrip(".")).lower()

    if log:
        log(f"Extracting {archive.name} -> {dest}")

    if strip_top_level:
        # Extract to a staging dir, then lift the wrapper's contents up. Doing
        # it this way keeps the strip logic identical for 7z and zip.
        staging = dest.parent / f".{dest.name}.staging"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _extract_raw(archive, staging, kind)
        inner = staging / strip_top_level
        source = inner if inner.is_dir() else staging
        _merge_tree(source, dest)
        shutil.rmtree(staging, ignore_errors=True)
    else:
        _extract_raw(archive, dest, kind)

    return dest


def _extract_raw(archive: Path, dest: Path, kind: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "7z":
        with py7zr.SevenZipFile(archive, "r") as handle:
            _safe_members(handle.getnames(), dest)
            handle.extractall(path=str(dest))
    elif kind == "zip":
        with zipfile.ZipFile(archive) as handle:
            _safe_members(handle.namelist(), dest)
            handle.extractall(path=str(dest))
    else:
        raise ExtractError(f"Don't know how to extract '{kind}' ({archive.name}).")


def _merge_tree(source: Path, dest: Path) -> None:
    """Copy source into dest, overwriting files but keeping dest's other
    contents. shutil.copytree(dirs_exist_ok=True) would do it, but this also
    works when source and dest sit on different drives mid-install."""
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = dest / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def verify_extracted(dest: Path, expected: list[str]) -> list[str]:
    """Return whichever expected files are missing after an extraction."""
    return [name for name in expected if not (dest / name).exists()]
