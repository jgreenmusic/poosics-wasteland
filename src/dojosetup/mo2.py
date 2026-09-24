"""Find the player's Mod Organizer 2 installs.

Most people who already play TTW installed it through MO2 (the Best of Times
guide that NV:MP's own TTW page points to does exactly that), and MO2 keeps
every archive it ever downloaded in its downloads folder. So a player who
already has TTW usually already has every file this pack asks for - they are
just not in ~/Downloads. This finds those folders so setup can use them.

Two kinds of MO2 install exist:
  * global instances   %LOCALAPPDATA%/ModOrganizer/<name>/ModOrganizer.ini
  * portable instances ModOrganizer.ini next to ModOrganizer.exe, anywhere
The ini names the real download and mod folders; the defaults are
<base>/downloads and <base>/mods.
"""

from __future__ import annotations

import functools
import os
import re
import string
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Instance:
    base: Path
    downloads: Path
    mods: Path


def _read_setting(text: str, key: str) -> str | None:
    match = re.search(rf"(?mi)^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text)
    if not match:
        return None
    value = match.group(1).strip()
    # Qt writes paths either plain or as @ByteArray(...), with / or \\.
    if value.startswith("@ByteArray(") and value.endswith(")"):
        value = value[len("@ByteArray("):-1]
    return value.replace("\\\\", "\\").strip('"') or None


def _instance_from_ini(ini: Path) -> Instance | None:
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    base = ini.parent
    configured = _read_setting(text, "base_directory")
    if configured:
        base = Path(configured)

    def resolve(key: str, default: str) -> Path:
        value = _read_setting(text, key)
        if not value:
            return base / default
        value = value.replace("%BASE_DIR%", str(base))
        path = Path(value)
        return path if path.is_absolute() else base / path

    return Instance(base=base, downloads=resolve("download_directory", "downloads"),
                    mods=resolve("mod_directory", "mods"))


_SKIP_TOP = ("$", "Windows", "ProgramData", "System Volume", "Program Files",
             "Recovery", "PerfLogs", "Users", "Intel", "AMD", "NVIDIA")


def _registered_exes() -> list[Path]:
    """MO2 installs that registered themselves for Nexus 'Mod Manager
    Download' links - this finds them wherever they live."""
    exes: list[Path] = []
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    handler = local / "ModOrganizer" / "nxmhandler.ini"
    try:
        text = handler.read_text(encoding="utf-8", errors="replace")
        for value in re.findall(r"(?mi)^\s*handlers\\\d+\\executable\s*=\s*(.+?)\s*$", text):
            exes.append(Path(value.strip('"').replace("\\\\", "\\")))
    except OSError:
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Classes\nxm\shell\open\command") as key:
            command = winreg.QueryValueEx(key, "")[0]
        match = re.match(r'\s*"([^"]+)"', command) or re.match(r"\s*(\S+)", command)
        if match:
            exes.append(Path(match.group(1)))
    except OSError:
        pass
    return exes


def _scan(folder: Path, depth: int, found: list[Path]) -> None:
    if depth < 0:
        return
    try:
        entries = [e for e in os.scandir(folder) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for entry in entries:
        path = Path(entry.path)
        if (path / "ModOrganizer.ini").is_file():
            found.append(path / "ModOrganizer.ini")
        else:
            _scan(path, depth - 1, found)


def _ini_candidates() -> list[Path]:
    found: list[Path] = []
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    found += list((local / "ModOrganizer").glob("*/ModOrganizer.ini"))

    # Portable instances keep ModOrganizer.ini next to ModOrganizer.exe (or
    # nxmhandler.exe, which lives in the same folder).
    for exe in _registered_exes():
        if (exe.parent / "ModOrganizer.ini").is_file():
            found.append(exe.parent / "ModOrganizer.ini")

    # And a bounded look three levels deep on every drive, which covers
    # C:\Modding\The Best of Times\ and G:\Games\Fallout\MO2\, skipping the
    # system trees that are large and never hold a mod setup.
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if not root.exists():
                continue
            top = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in top:
            if entry.name.startswith(_SKIP_TOP):
                continue
            path = Path(entry.path)
            if (path / "ModOrganizer.ini").is_file():
                found.append(path / "ModOrganizer.ini")
            else:
                _scan(path, 1, found)
    return list(dict.fromkeys(found))


@functools.lru_cache(maxsize=1)
def instances() -> list[Instance]:
    """Every MO2 install found, with folders that actually exist."""
    result: list[Instance] = []
    for ini in _ini_candidates():
        inst = _instance_from_ini(ini)
        if inst and (inst.downloads.is_dir() or inst.mods.is_dir()):
            result.append(inst)
    return result
