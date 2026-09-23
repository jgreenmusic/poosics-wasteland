"""Archive extraction for .7z and .zip.

.7z goes through a 7-Zip binary bundled in the frozen exe (see _extract_7z
for why py7zr alone is not enough), so the player still needs nothing
installed. .zip is stdlib.

`strip_top_level` matters: xNVSE's archive wraps everything in an
`nvse_6_4_8/` folder, and extracting that verbatim into the game root produces
a game that silently launches without the script extender. The manifest names
the folder to strip so that failure cannot happen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable

import py7zr
import py7zr.exceptions

# The exe is --windowed; without this every 7z/tar call flashes a console.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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


def _seven_zip() -> Path | None:
    """The 7-Zip binary to use: the copy bundled in the frozen exe, else an
    installed one. None if neither exists."""
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "tools" / "7z.exe"
    installed = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe"
    for candidate in (bundled, installed):
        if candidate.is_file():
            return candidate
    return None


def _extract_7z(archive: Path, dest: Path) -> None:
    """Extract a .7z, trying extractors in order of how much they support.

    py7zr alone is NOT enough: the TTW and TTW OGG archives use the BCJ2
    filter, which py7zr cannot decode - it raises partway through and leaves
    zero-byte files behind. 7-Zip handles everything and is bundled in the
    exe (LGPL, license shipped alongside). Windows' own tar.exe (libarchive)
    also reads BCJ2 on current Windows 11, but some Windows 10 builds ship it
    without LZMA, so it is only the second choice.
    """
    with py7zr.SevenZipFile(archive, "r") as handle:
        _safe_members(handle.getnames(), dest)

    seven = _seven_zip()
    if seven is not None:
        result = subprocess.run(
            [str(seven), "x", "-y", "-bso0", "-bsp0", f"-o{dest}", str(archive)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            return
        raise ExtractError(
            f"7-Zip could not extract {archive.name} (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )

    # Windows' own bsdtar by full path: shutil.which("tar") finds Git's GNU tar
    # first on any PC with Git installed, and GNU tar cannot read .7z at all.
    tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    if tar.is_file():
        result = subprocess.run(
            [str(tar), "-xf", str(archive), "-C", str(dest)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            return

    try:
        with py7zr.SevenZipFile(archive, "r") as handle:
            handle.extractall(path=str(dest))
    except py7zr.exceptions.UnsupportedCompressionMethodError as exc:
        raise ExtractError(
            f"{archive.name} uses a compression method this PC cannot unpack "
            "without 7-Zip. Install 7-Zip from https://www.7-zip.org and run "
            "setup again."
        ) from exc


def _extract_raw(archive: Path, dest: Path, kind: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "7z":
        _extract_7z(archive, dest)
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
