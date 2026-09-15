"""Locate Steam, its library folders, and the two Fallout installs.

No third-party dependencies: this module has to work inside a frozen one-file exe
on a friend's machine where nothing is installed.

The DLC file check, not the appid, is what decides whether an install qualifies.
Fallout 3 GOTY (22370) and plain Fallout 3 (22300) can both be acceptable, and
Steam happily reports an app as installed while its DLC is absent, so the only
trustworthy signal is whether the .esm files are actually on disk.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Registry is the reliable way to find Steam on Windows. winreg is stdlib and
# present in a frozen build, but guard the import so this module stays testable
# on a non-Windows box.
try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None


STEAM_REG_KEYS = [
    (r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    (r"SOFTWARE\Valve\Steam", "InstallPath"),
]

STEAM_FALLBACK_DIRS = [
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
]


@dataclass
class GameInstall:
    """One located game, plus exactly why it passed or failed validation."""

    role: str
    appid: int
    name: str
    path: Path | None = None
    missing_files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.path is not None

    @property
    def ok(self) -> bool:
        return self.found and not self.missing_files and not self.problems


def find_steam_root() -> Path | None:
    """Steam's install directory, from the registry first, then known paths."""
    if winreg is not None:
        for subkey, value in STEAM_REG_KEYS:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        raw, _ = winreg.QueryValueEx(key, value)
                except OSError:
                    continue
                candidate = Path(raw)
                if candidate.is_dir():
                    return candidate

    for raw in STEAM_FALLBACK_DIRS:
        candidate = Path(raw)
        if candidate.is_dir():
            return candidate
    return None


def _parse_library_paths(vdf_text: str) -> list[Path]:
    """Pull every "path" value out of a libraryfolders.vdf.

    A hand-rolled regex beats a full VDF parser here: the file's shape has
    changed across Steam versions (numbered string values in old builds, nested
    blocks in new ones) but the quoted "path" key has been stable throughout.
    """
    paths = []
    for match in re.finditer(r'"path"\s*"([^"]+)"', vdf_text):
        raw = match.group(1).replace("\\\\", "\\")
        candidate = Path(raw)
        if candidate.is_dir():
            paths.append(candidate)
    return paths


def find_library_folders(steam_root: Path | None = None) -> list[Path]:
    """Every Steam library folder on this machine, deduplicated, order kept."""
    if steam_root is None:
        steam_root = find_steam_root()

    found: list[Path] = []
    if steam_root is not None:
        found.append(steam_root)
        # Newer Steam writes config/libraryfolders.vdf; older wrote it under
        # steamapps. Read both and merge - a machine can have either or both.
        for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            vdf = steam_root / relative
            if vdf.is_file():
                try:
                    text = vdf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                found.extend(_parse_library_paths(text))

    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path).rstrip("\\/").lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _read_installdir(acf_path: Path) -> str | None:
    try:
        text = acf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"installdir"\s*"([^"]+)"', text)
    return match.group(1) if match else None


def locate_app(appid: int, libraries: list[Path]) -> Path | None:
    """Resolve an appid to its install directory via its appmanifest."""
    for library in libraries:
        steamapps = library / "steamapps"
        acf = steamapps / f"appmanifest_{appid}.acf"
        if not acf.is_file():
            continue
        installdir = _read_installdir(acf)
        if not installdir:
            continue
        candidate = steamapps / "common" / installdir
        if candidate.is_dir():
            return candidate
    return None


def _under_program_files(path: Path) -> bool:
    resolved = str(path).replace("/", "\\").lower()
    return resolved.startswith("c:\\program files")


def check_game(spec: dict, libraries: list[Path], *, refuse_program_files: bool = True) -> GameInstall:
    """Locate one game from its manifest spec and validate it completely."""
    appids = [int(spec["appid"])] + [int(a) for a in spec.get("altAppids", [])]

    install = GameInstall(role=spec["role"], appid=appids[0], name=spec["name"])

    for appid in appids:
        path = locate_app(appid, libraries)
        if path is not None:
            install.path = path
            install.appid = appid
            break

    if install.path is None:
        install.problems.append(
            f"{spec['name']} is not installed in any Steam library on this PC."
        )
        return install

    for relative in spec.get("requiredFiles", []):
        if not (install.path / relative).is_file():
            install.missing_files.append(relative)

    if refuse_program_files and _under_program_files(install.path):
        install.problems.append(
            f"{spec['name']} is installed under Program Files, which breaks "
            "Mod Organizer and xNVSE. Move it to another drive or folder "
            "(Steam: right-click the game > Properties > Installed Files > "
            "Move Install Folder) and run this setup again."
        )

    return install


def free_space_gb(path: Path) -> float:
    """Free space on the volume holding `path`, in GB, walking up if needed."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / (1024**3)
    except OSError:
        return 0.0


def preflight(manifest: dict) -> dict:
    """Run every prerequisite check and return a structured, printable result."""
    prereqs = manifest.get("prereqs", {})
    libraries = find_library_folders()

    games = {}
    for spec in prereqs.get("steamApps", []):
        games[spec["role"]] = check_game(
            spec,
            libraries,
            refuse_program_files=bool(prereqs.get("refuseProgramFiles", True)),
        )

    need_gb = float(prereqs.get("diskFreeGB", 45))
    target = games.get("fnv")
    disk_path = target.path if (target and target.path) else Path(os.path.expanduser("~"))
    have_gb = free_space_gb(disk_path)

    blockers: list[str] = []
    for game in games.values():
        blockers.extend(game.problems)
        if game.missing_files:
            blockers.append(
                f"{game.name} is missing required DLC files: "
                + ", ".join(os.path.basename(f) for f in game.missing_files)
                + ". TTW is built against every DLC and cannot install without them."
            )

    if have_gb < need_gb:
        blockers.append(
            f"Not enough free space on {disk_path.drive or disk_path}: "
            f"{have_gb:.1f} GB free, {need_gb:.0f} GB needed."
        )

    return {
        "steam_root": find_steam_root(),
        "libraries": libraries,
        "games": games,
        "disk": {"path": disk_path, "free_gb": have_gb, "need_gb": need_gb},
        "blockers": blockers,
        "ok": not blockers,
    }
