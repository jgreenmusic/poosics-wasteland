"""The install steps themselves.

Everything writes into the player's real Fallout: New Vegas folder. There is no
mod manager and no virtual filesystem, because NV:MP launches its own process
and cannot see one - see the mo2 component note in the manifest.

Every step is idempotent: re-running setup after a failure re-verifies rather
than re-doing, so a player who loses their connection halfway does not start
over.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import archive, fetch

Log = Callable[[str], None]


@dataclass
class Context:
    """Everything a step needs, resolved once up front."""

    manifest: dict
    fnv: Path
    fo3: Path
    cache: Path
    log: Log = print
    dry_run: bool = False
    ttw_from: str | None = None  # --ttw-from: an existing TTW build to reuse
    results: dict = field(default_factory=dict)

    @property
    def fnv_data(self) -> Path:
        return self.fnv / "Data"

    @property
    def components(self) -> dict:
        return {c["id"]: c for c in self.manifest["components"]}


class StepError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# acquiring payloads
# --------------------------------------------------------------------------

def acquire(ctx: Context, component: dict, on_progress=None) -> Path:
    """Get one component onto disk and verified.

    `auto` components download from their pinned URL. `manual` ones are
    downloaded by the player in a browser - this only locates and verifies the
    result, and raises if it is not there yet so the GUI can prompt.
    """
    name = component["name"]
    if component.get("fetch") == "auto":
        ctx.log(f"  fetching {name} {component.get('version','')}".rstrip())
        return fetch.resolve_auto(component, ctx.cache, on_progress)

    found = fetch.resolve_manual(component, ctx.cache, on_progress)
    if found is None:
        raise StepError(
            f"{name} has not been downloaded yet.\n"
            f"  Download {component['filename']} from {component.get('pageUrl','its official site')}\n"
            + (f"  Tip: {component['downloadHint']}\n" if component.get("downloadHint") else "")
            + "  then run setup again - it checks your Downloads folder automatically."
        )
    ctx.log(f"  found and verified {found.name}")
    return found


# --------------------------------------------------------------------------
# install targets
# --------------------------------------------------------------------------

def install_fnv_root(ctx: Context, component: dict, payload: Path) -> None:
    """Extract loose into the game root (xNVSE, OGG DLLs, NV:MP)."""
    target = ctx.fnv
    ctx.log(f"  installing {component['name']} -> {target}")
    if ctx.dry_run:
        return
    archive.extract(
        payload, target,
        kind=component.get("archive"),
        strip_top_level=component.get("stripTopLevel"),
        log=None,
    )
    missing = archive.verify_extracted(target, component.get("verify", []))
    if missing:
        raise StepError(
            f"{component['name']} extracted but these expected files are "
            f"missing: {', '.join(missing)}"
        )

    # NV:MP's launcher self-updates the client and rewrites .nvmp_version, but
    # never touches nvmp_storyserver.exe (whose own version info just says
    # 1.0.0.0). This marker records what version the server files really are.
    marker = component.get("versionMarker")
    if marker:
        (target / marker).write_text(component["version"] + "\n", encoding="ascii")


def install_fnv_data(ctx: Context, component: dict, payload: Path) -> None:
    """Extract into Data/ (YUPTTW, NVSE plugins)."""
    target = ctx.fnv_data
    # A stub INI the plugin fills in itself on first launch (Stewie Tweaks):
    # once it exists, re-extracting would wipe the player's settings.
    if component.get("keepExisting") and all(
            (target / name).exists() for name in component.get("verify", [])):
        ctx.log(f"  {component['name']} already present - keeping your copy")
        return
    ctx.log(f"  installing {component['name']} -> {target}")
    if ctx.dry_run:
        return
    archive.extract(payload, target, kind=component.get("archive"),
                    strip_top_level=component.get("stripTopLevel"), log=None)
    missing = archive.verify_extracted(target, component.get("verify", []))
    if missing:
        raise StepError(
            f"{component['name']} extracted but these expected files are "
            f"missing: {', '.join(missing)}"
        )


#: Files a finished TTW build must contain, non-empty. The ESM alone proves
#: nothing: the installer writes it in its first second and then spends the
#: next half hour filling the BSAs, so a build that stopped early still has it.
TTW_CORE_FILES = [
    "TaleOfTwoWastelands.esm",
    "TaleOfTwoWastelands - Main.bsa",
    "TaleOfTwoWastelands - Textures.bsa",
    "Fallout3.esm",
    "Fallout3 - Meshes.bsa",
    "Fallout3 - Textures.bsa",
    "Fallout3 - Sound.bsa",
    "Fallout3 - Voices.bsa",
    "Fallout3 - MenuVoices.bsa",
    "Anchorage.esm", "Anchorage - Main.bsa",
    "ThePitt.esm", "ThePitt - Main.bsa",
    "BrokenSteel.esm", "BrokenSteel - Main.bsa",
    "PointLookout.esm", "PointLookout - Main.bsa",
    "Zeta.esm", "Zeta - Main.bsa",
]


def ttw_output_problems(folder: Path, manifest: dict) -> list[str]:
    """Why `folder` is not a finished TTW build, or [] if it is.

    Two layers. The core list catches an obviously unfinished build on any
    machine. The manifest's `ttwOutput.files` - sizes recorded from the host's
    own clean build - catches the subtle case: a build that stopped partway
    through a BSA, which is exactly what the host's first attempt produced.
    """
    problems: list[str] = []
    for name in TTW_CORE_FILES:
        path = folder / name
        if not path.is_file():
            problems.append(f"missing {name}")
        elif path.stat().st_size == 0:
            problems.append(f"{name} is empty")

    output = manifest.get("ttwOutput") or {}
    ratio = output.get("minSizeRatio", 0.95)
    for name, size in (output.get("files") or {}).items():
        path = folder / name
        if not path.is_file():
            problems.append(f"missing {name}")
        elif path.stat().st_size < size * ratio:
            problems.append(
                f"{name} is only {path.stat().st_size:,} bytes; the host's is {size:,} "
                "(the build stopped partway)"
            )
    # One line per problem is plenty; dedupe what both layers reported.
    return list(dict.fromkeys(problems))


def ttw_parity_problems(data: Path, manifest: dict, log: Log | None = None) -> list[str]:
    """Compare the built plugins against the host's hashes.

    TaleOfTwoWastelands.esm is built on each machine, never shipped, so this
    is the only way to find out before joining that a player's build differs
    from the host's. NV:MP would otherwise report it as 'Invalid mod
    revisions' at the handshake.
    """
    hashes = (manifest.get("ttwOutput") or {}).get("sha256") or {}
    problems: list[str] = []
    for name, expected in hashes.items():
        path = data / name
        if not path.is_file():
            problems.append(f"missing {name}")
            continue
        if log:
            log(f"    hashing {name}")
        actual = fetch.sha256_file(path)
        if actual.lower() != expected.lower():
            problems.append(
                f"{name} differs from the host's build "
                f"(got {actual[:12]}..., host has {expected[:12]}...)"
            )
    return problems


def ttw_output_dir(ctx: Context) -> Path:
    """Where the TTW installer is told to build.

    An empty sibling of the game folder, not Data itself: the installer wants
    an empty destination, and building beside Data then moving in keeps a
    half-finished build from ever mixing with the real game files. Same drive
    as the game, so the move afterwards is a rename, not a 5 GB copy.
    """
    return ctx.fnv.parent / "TTW_output"


def merge_into_data(ctx: Context, source: Path) -> int:
    """Move a finished TTW build into Data. Returns the number of files moved."""
    moved = 0
    for item in sorted(source.rglob("*")):
        if item.is_dir():
            continue
        target = ctx.fnv_data / item.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        shutil.move(str(item), str(target))
        moved += 1
    shutil.rmtree(source, ignore_errors=True)
    return moved


class ElevatedProcess:
    """A process started through UAC, which we can still wait on."""

    def __init__(self, handle: int):
        self.handle = handle

    def running(self) -> bool:
        WAIT_TIMEOUT = 0x102
        return ctypes.windll.kernel32.WaitForSingleObject(
            ctypes.c_void_p(self.handle), 0) == WAIT_TIMEOUT


def launch_elevated(exe: Path, args: list[str], cwd: Path,
                    verb: str = "runas") -> ElevatedProcess:
    """Start `exe` with the UAC prompt and return a handle to wait on.

    TTW Install.exe's manifest requires administrator, so subprocess.Popen
    fails with WinError 740. ShellExecuteEx with the "runas" verb is the only
    way to raise the prompt, and SEE_MASK_NOCLOSEPROCESS keeps the process
    handle so setup can tell when the installer has closed.
    """
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x40
    SW_SHOWNORMAL = 1
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = verb
    info.lpFile = str(exe)
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = str(cwd)
    info.nShow = SW_SHOWNORMAL

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:  # ERROR_CANCELLED - the player clicked No
            raise StepError(
                "The TTW installer needs admin permission and Windows' prompt was "
                "declined. Click Try again and choose Yes."
            )
        raise StepError(f"Could not start the TTW installer (Windows error {error}).")
    if not info.hProcess:
        raise StepError("The TTW installer started but setup cannot track it.")
    return ElevatedProcess(info.hProcess)


def has_existing_ttw(ctx: Context) -> bool:
    """Cheap check (no hashing) for the GUI's "what do I need to download"
    list: is there a complete TTW in Data or a Mod Organizer folder?"""
    if not ttw_output_problems(ctx.fnv_data, ctx.manifest):
        return True
    return any(not ttw_output_problems(f, ctx.manifest) for f in ttw_candidates(ctx))


def ttw_candidates(ctx: Context) -> list[Path]:
    """Folders that may already hold a TTW build - most players who have TTW
    installed it the standard way, into a Mod Organizer 2 mod folder."""
    found: list[Path] = []
    if ctx.ttw_from:
        found.append(Path(ctx.ttw_from))
    roots: list[Path] = []
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    roots += [p / "mods" for p in (local / "ModOrganizer").glob("*") if p.is_dir()]
    for drive in "CDEFGH":
        for base in (Path(f"{drive}:/Modding"), Path(f"{drive}:/Games"), Path(f"{drive}:/MO2")):
            if base.is_dir():
                roots += [p for p in base.glob("*/mods")] + [p for p in base.glob("mods")]
    roots.append(ctx.fnv.parent / "TTW")
    from . import mo2
    roots += [inst.mods for inst in mo2.instances()]   # the REAL mods folders
    for root in roots:
        try:
            if (root / "TaleOfTwoWastelands.esm").is_file():
                found.append(root)
            elif root.is_dir():
                found += [p.parent for p in root.glob("*/TaleOfTwoWastelands.esm")]
        except OSError:
            continue
    return list(dict.fromkeys(found))


def link_into_data(ctx: Context, source: Path) -> int:
    """Put an existing TTW build into Data without disturbing its original.

    Hard links when it is on the same drive (instant, no extra space, and the
    player's Mod Organizer setup keeps working), a copy otherwise.
    """
    count = 0
    for item in sorted(source.rglob("*")):
        if item.is_dir() or item.name.lower() == "meta.ini":  # MO2 bookkeeping
            continue
        target = ctx.fnv_data / item.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        try:
            os.link(item, target)
        except OSError:
            shutil.copy2(item, target)
        count += 1
    return count


def use_existing_ttw(ctx: Context) -> bool:
    """Reuse a TTW build the player already has, if it is complete AND the
    same build as the host's. True if one was placed into Data."""
    for folder in ttw_candidates(ctx):
        problems = ttw_output_problems(folder, ctx.manifest)
        if problems:
            ctx.log(f"  found TTW at {folder}, but it is incomplete - not using it")
            continue
        # Only the TTW plugins themselves: YUPTTW is laid over by its own step.
        hashes = dict((ctx.manifest.get("ttwOutput") or {}).get("sha256") or {})
        hashes.pop("YUPTTW.esm", None)
        check = {**ctx.manifest, "ttwOutput": {"sha256": hashes}}
        mismatch = ttw_parity_problems(folder, check)
        if mismatch:
            ctx.log(f"  found TTW at {folder}, but it is a different build from the "
                    "host's (different TTW version) - not using it")
            continue
        ctx.log(f"  found your existing TTW at {folder} - it matches the host's build")
        if not ctx.dry_run:
            ctx.log(f"  placed {link_into_data(ctx, folder)} files into Data "
                    "(your original is untouched)")
        return True
    return False


def run_ttw_installer(ctx: Context, component: dict, payload: Path,
                      wait: bool = True) -> None:
    """Extract and launch the TTW installer, then verify and place its output.

    This is the one step that cannot be automated: the installer is a Delphi
    GUI whose only switch is -NoOggEnc2. So setup extracts it, hands the player
    the exact paths to paste, launches it, waits for it to CLOSE, checks the
    build is complete, and only then moves it into Data.
    """
    output = ttw_output_dir(ctx)

    # Already done on an earlier run?
    if not ttw_output_problems(ctx.fnv_data, ctx.manifest):
        ctx.log("  TTW is already built into Data - skipping the installer.")
        return

    # A finished build that was not moved yet (setup closed in between).
    if output.is_dir() and not ttw_output_problems(output, ctx.manifest):
        ctx.log(f"  found a finished TTW build in {output}")
        if not ctx.dry_run:
            ctx.log(f"  moved {merge_into_data(ctx, output)} files into Data")
        return

    installer_dir = ctx.cache / "ttw_installer"
    marker = installer_dir / "TTW Install.exe"

    if not marker.is_file():
        ctx.log(f"  extracting the TTW installer ({payload.name}, this takes a minute)")
        if not ctx.dry_run:
            archive.extract(payload, installer_dir, kind=component.get("archive"), log=None)

    if ctx.dry_run:
        ctx.log("  [dry run] would launch the TTW installer")
        return

    if not marker.is_file():
        raise StepError(
            f"The TTW installer was not found after extraction (expected {marker})."
        )

    # The installer wants an empty destination. A leftover partial build is
    # set aside rather than deleted - it may be the player's only copy of
    # something they care about, and it costs nothing to keep.
    if output.is_dir() and any(output.iterdir()):
        aside = output.with_name(f"{output.name}.incomplete-{time.strftime('%Y%m%d-%H%M%S')}")
        ctx.log(f"  an unfinished TTW build is in the way; moving it to {aside.name}")
        output.rename(aside)
    output.mkdir(parents=True, exist_ok=True)

    ctx.log("")
    ctx.log("  The TTW installer is a GUI and has no silent mode, so this part")
    ctx.log("  needs you. Paste these three values when it asks:")
    ctx.log(f"     Fallout 3 folder        {ctx.fo3}")
    ctx.log(f"     Fallout New Vegas       {ctx.fnv}")
    ctx.log(f"     Install TTW to          {output}")
    ctx.log("")
    ctx.log("  Leave every other option at its default and click Install.")
    ctx.log("  It runs 30-90 minutes. CLOSE the TTW window when it says it is")
    ctx.log("  finished - setup carries on by itself after that.")
    ctx.log("")

    # -NoOggEnc2 skips the audio re-encode. The OGG component installs the
    # prebuilt Vorbis DLLs instead, which is both faster and removes a long
    # step that commonly fails on slower machines.
    ctx.log("  Windows will ask for permission - the TTW installer needs admin.")
    process = launch_elevated(marker, ["-NoOggEnc2"], installer_dir)

    if not wait:
        return

    # Wait for the installer to EXIT. Watching for the ESM is wrong: TTW
    # writes it in the first second and keeps building for half an hour, so
    # that check declared a half-built install finished.
    ctx.log("  Waiting for the TTW installer to finish and close...")
    started = time.monotonic()
    next_report = 10
    while process.running():
        time.sleep(15)
        minutes = (time.monotonic() - started) / 60
        if minutes >= next_report:
            ctx.log(f"    still running ({next_report} min)")
            next_report += 10
    ctx.log("  TTW installer closed. Checking the build...")

    problems = ttw_output_problems(output, ctx.manifest)
    if problems:
        raise StepError(
            "The TTW installer closed but the build is not complete:\n    "
            + "\n    ".join(problems[:12])
            + ("\n    ..." if len(problems) > 12 else "")
            + "\n  If you closed it early, click Try again - setup sets the partial"
            "\n  build aside and starts the TTW installer fresh."
        )
    ctx.log(f"  build complete; moved {merge_into_data(ctx, output)} files into Data")


def install_optional_tool(ctx: Context, component: dict, payload: Path) -> None:
    """Unpack an optional tool beside the game, touching nothing else."""
    target = ctx.fnv.parent / component["name"].replace(" ", "")
    ctx.log(f"  installing optional {component['name']} -> {target}")
    if ctx.dry_run:
        return
    archive.extract(payload, target, kind=component.get("archive"), log=None)


# --------------------------------------------------------------------------
# using what the player already has (the "already installed TTW" case)
# --------------------------------------------------------------------------

def _file_matches(path: Path, size: int, sha256: str) -> bool:
    """INI files only have to exist - players edit them. Everything else must
    be byte-identical (size first, so most misses cost no hashing)."""
    if not path.is_file():
        return False
    if path.suffix.lower() == ".ini":
        return True
    return path.stat().st_size == size and fetch.sha256_file(path) == sha256


def installs_present(folder: Path, installs: dict) -> bool:
    return bool(installs) and all(
        _file_matches(folder / rel, size, sha) for rel, (size, sha) in installs.items())


def _mo2_mod_folders() -> list[Path]:
    from . import mo2
    folders: list[Path] = []
    for inst in mo2.instances():
        try:
            folders += [p for p in inst.mods.iterdir() if p.is_dir()]
        except OSError:
            continue
    return folders


def _key_dll(component: dict) -> str | None:
    """The plugin's own DLL, e.g. NVSE/Plugins/jip_nvse.dll."""
    for rel in component.get("installs", {}):
        if rel.lower().endswith(".dll"):
            return rel
    return None


def _link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)   # instant, no extra space, same drive
    except OSError:
        shutil.copy2(source, target)


def satisfy_from_installed(ctx: Context, component: dict, apply: bool = True) -> bool:
    """True if this component needs no download because the player already has
    it: already in the game, 4GB-patched, or installed in Mod Organizer.

    apply=False only answers the question (the GUI's "what's missing" list);
    apply=True also links a Mod Organizer copy into the game folder.
    """
    if component.get("install") == "fnv_4gb":
        return is_large_address_aware(ctx.fnv / "FalloutNV.exe")

    installs = component.get("installs") or {}
    if not installs:
        return False
    target = ctx.fnv if component.get("install") == "fnv_root" else ctx.fnv_data

    if installs_present(target, installs):
        if apply:
            ctx.log(f"  already installed - skipping {component['name']}")
        return True

    # Exact copy installed as a Mod Organizer mod?
    for mod in _mo2_mod_folders():
        if installs_present(mod, installs):
            if apply:
                ctx.log(f"  found it in your Mod Organizer: {mod.name}")
                if not ctx.dry_run:
                    for rel in installs:
                        _link(mod / rel, target / rel)
            return True

    # A different version of the same NVSE plugin in Mod Organizer. Accepted
    # for plugins only: the server does not require identical NVSE plugins
    # (StrictNVSE is off) and TTW enforces its own minimum versions at startup.
    # TTW's own files are never accepted this way - those get players kicked.
    key = _key_dll(component)
    if component.get("acceptInstalledVersion") and key:
        for mod in _mo2_mod_folders():
            dll = mod / key
            if dll.is_file():
                if apply:
                    ctx.log(f"  using the version already in your Mod Organizer: {mod.name}")
                    if not ctx.dry_run:
                        plugins = dll.parent
                        for item in plugins.rglob("*"):
                            if item.is_file():
                                _link(item, ctx.fnv_data / "NVSE" / "Plugins"
                                      / item.relative_to(plugins))
                return True
    return False


def is_large_address_aware(exe: Path) -> bool:
    """True if the exe's PE header has the 4GB (LargeAddressAware) flag."""
    with open(exe, "rb") as handle:
        head = handle.read(4096)
    pe = int.from_bytes(head[0x3C:0x40], "little")
    characteristics = int.from_bytes(head[pe + 22:pe + 24], "little")
    return bool(characteristics & 0x20)


def install_4gb_patch(ctx: Context, component: dict, payload: Path) -> None:
    """Make FalloutNV.exe 4GB-aware with the FNV 4GB Patcher.

    Required, not optional: stock New Vegas has 2 GB of address space and
    TTW's ~16 GB of assets crash it while loading. The flag cannot simply be
    set by hand - Steam's DRM wrapper rejects any edited exe with
    "Application load error 3:0000065432" (seen on the host 2026-09-23). The
    patcher is built to get past that, and keeps FalloutNV_backup.exe.
    """
    exe = ctx.fnv / "FalloutNV.exe"
    if is_large_address_aware(exe):
        ctx.log("  FalloutNV.exe is already 4GB-patched - skipping.")
        return
    ctx.log(f"  patching {exe.name} for 4GB of memory")
    if ctx.dry_run:
        return
    archive.extract(payload, ctx.fnv, kind=component.get("archive"), log=None)
    patcher = ctx.fnv / component.get("runExe", "FNVpatch.exe")
    # It ends with "Press any key", so feed it a newline rather than hang.
    result = subprocess.run(
        [str(patcher)], cwd=str(ctx.fnv), input="\n", capture_output=True,
        text=True, timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not is_large_address_aware(exe):
        raise StepError(
            "The 4GB Patcher ran but FalloutNV.exe is still not patched.\n"
            f"  Patcher said: {(result.stdout or result.stderr).strip()[:300]}\n"
            "  Close the game if it is open and run setup again."
        )
    ctx.log("  FalloutNV.exe patched (original kept as FalloutNV_backup.exe)")


INSTALLERS = {
    "fnv_root": install_fnv_root,
    "fnv_data": install_fnv_data,
    "ttw_installer": run_ttw_installer,
    "optional_tool": install_optional_tool,
    "fnv_4gb": install_4gb_patch,
}


# --------------------------------------------------------------------------
# load order + launcher
# --------------------------------------------------------------------------

def fnv_config_dir() -> Path:
    """Where FNV keeps plugins.txt / loadorder.txt."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "FalloutNV"


#: Plugins are stamped this far apart, starting here. The date only has to be
#: fixed and older than anything a player might add later, so their own mods
#: sort after the pack instead of in between.
LOAD_ORDER_BASE = datetime(2010, 10, 19, 12, 0, 0)
LOAD_ORDER_STEP = timedelta(minutes=1)


def stamp_load_order(ctx: Context) -> None:
    """Set each plugin's modified time in manifest order.

    In Fallout: New Vegas the load order IS the plugins' file timestamps -
    plugins.txt only says which are active. Without this the real order is
    whatever Steam and the TTW installer happened to leave, which differs
    between machines.
    """
    for index, name in enumerate(ctx.manifest["loadOrder"]):
        path = ctx.fnv_data / name
        if not path.is_file():
            continue  # verify_install reports it
        stamp = (LOAD_ORDER_BASE + index * LOAD_ORDER_STEP).timestamp()
        os.utime(path, (stamp, stamp))


def load_order_is_stamped(ctx: Context) -> bool:
    """True if the plugins' timestamps put them in manifest order."""
    times = []
    for name in ctx.manifest["loadOrder"]:
        path = ctx.fnv_data / name
        if not path.is_file():
            return False
        times.append(path.stat().st_mtime)
    return all(earlier < later for earlier, later in zip(times, times[1:]))


def write_load_order(ctx: Context) -> list[Path]:
    """Write the parity contract to plugins.txt and loadorder.txt, and stamp
    the plugin timestamps that actually decide the order.

    NV:MP compares the active plugin set with the host and kicks on any
    mismatch, so this list is written exactly as the manifest specifies rather
    than merged with whatever was there before. The previous files are backed
    up so a player's single-player setup can be restored.
    """
    order = ctx.manifest["loadOrder"]
    config = fnv_config_dir()
    written: list[Path] = []

    ctx.log(f"  writing load order ({len(order)} plugins) -> {config}")
    if ctx.dry_run:
        return written

    config.mkdir(parents=True, exist_ok=True)

    for filename in ("plugins.txt", "loadorder.txt"):
        path = config / filename
        if path.is_file():
            backup = path.with_suffix(path.suffix + ".dojo-backup")
            if not backup.exists():
                shutil.copy2(path, backup)
                ctx.log(f"    backed up your existing {filename} -> {backup.name}")

        # Plain filenames in both. The '*' = active prefix is the Skyrim SE /
        # Fallout 4 format (per LOOT's load order docs); New Vegas lists
        # active plugins by name only.
        # newline="" stops Python translating the \n of "\r\n" into a second \r.
        # "\r\r\n" left every name ending in \r, which nothing matched to a file:
        # NV:MP then refused to start with "No mod files found on disk".
        path.write_text("\r\n".join(order) + "\r\n", encoding="utf-8", newline="")
        written.append(path)

    stamp_load_order(ctx)
    ctx.log("    plugin timestamps set to match the load order")
    return written


def known_folder(a: int, b: int, c: int, d: bytes, fallback: Path) -> Path:
    """A Windows known folder's REAL location. OneDrive commonly moves Desktop
    and Documents (to ~/OneDrive/Desktop), so ~/Desktop can be a folder the
    player never sees - which is how a friend ended up with no shortcut."""
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD),
                        ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        fid = GUID(a, b, c, (ctypes.c_ubyte * 8)(*d))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None,
                                                      ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except (AttributeError, OSError):
        pass
    return fallback


def documents_dir() -> Path:
    # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
    return known_folder(0xFDD39AD0, 0x238F, 0x46AF,
                        bytes([0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7]),
                        Path.home() / "Documents")


def desktop_dir() -> Path:
    # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
    return known_folder(0xB4BFCC3A, 0xDB2C, 0x424C,
                        bytes([0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41]),
                        Path.home() / "Desktop")


def primary_screen_size() -> tuple[int, int] | None:
    """The primary display's real pixel size, ignoring Windows' DPI scaling.

    EnumDisplaySettings reports the physical mode, so a 4K screen at 150%
    reads 3840x2160 - not the 2560x1440 a DPI-unaware process is told.
    """
    try:
        devmode = ctypes.create_string_buffer(220)          # DEVMODEW
        devmode[68:70] = (220).to_bytes(2, "little")        # dmSize
        if ctypes.windll.user32.EnumDisplaySettingsW(None, -1, devmode):  # ENUM_CURRENT_SETTINGS
            width = int.from_bytes(devmode[172:176], "little")
            height = int.from_bytes(devmode[176:180], "little")
            if width and height:
                return width, height
    except (AttributeError, OSError):
        pass
    return None


#: Tried largest first when the screen itself is not 16:9.
STANDARD_16_9 = [(3840, 2160), (2560, 1440), (1920, 1080), (1600, 900), (1280, 720)]


def pick_resolution(screen: tuple[int, int] | None) -> tuple[int, int]:
    """A 16:9 resolution for the game. NV:MP refuses anything else: its font
    texture fails with 'Font spritesheet ... video hardware error' (seen on
    the host at 1128x634)."""
    if screen:
        width, height = screen
        if width * 9 == height * 16:
            return width, height
        for w, h in STANDARD_16_9:
            if w <= width and h <= height:
                return w, h
    return 1920, 1080


def set_ini_values(path: Path, values: dict[str, str]) -> list[str]:
    """Set `key=value` lines in a Bethesda INI in place, keeping its line
    endings and encoding. Returns the keys that were not found."""
    raw = path.read_bytes().decode("latin-1")
    lines = raw.splitlines(keepends=True)
    missing = dict(values)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        key = body.split("=", 1)[0].strip() if "=" in body else None
        if key in missing:
            ending = line[len(body):]
            lines[i] = f"{key}={missing.pop(key)}{ending}"
    path.write_bytes("".join(lines).encode("latin-1"))
    return list(missing)


def controlled_folder_access() -> int | None:
    """Windows' ransomware protection: 0 off, 1 on (blocking), 2 audit only.
    None if it could not be read (no Defender, e.g. third-party antivirus)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-MpPreference).EnableControlledFolderAccess"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = out.stdout.strip()
        return int(value) if value.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def protect_save_folder(ctx: Context) -> None:
    """Make sure the GAME can write its saves.

    Learned from a friend's PC with 4,300 saves: NV:MP writes a temp save and
    then commits it; there the engine's write silently failed ("temp save
    ... is missing - the saves folder is likely blocked"), so NV:MP retried
    forever - endless saves and stutter. His Documents were in OneDrive. The
    usual blocker is Controlled Folder Access, which stops unlisted programs
    (FalloutNV.exe) writing into Documents without telling the player.
    """
    saves = documents_dir() / "My Games" / "FalloutNV" / "Saves"
    my_games = documents_dir() / "My Games"
    exe = ctx.fnv / "FalloutNV.exe"
    ctx.log(f"  save folder: {saves}")
    if ctx.dry_run:
        return
    saves.mkdir(parents=True, exist_ok=True)

    # 1. Can anything write there? (If Controlled Folder Access blocks setup
    #    itself, it certainly blocks the game.)
    probe = saves / "dojo-write-test.tmp"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
        ctx.log("  ! Windows blocked writing to your save folder.")

    # 2. Controlled Folder Access on? Allow the game through it (one UAC prompt).
    cfa = controlled_folder_access()
    if cfa == 1 or (not writable and cfa is None):
        ctx.log("  Windows 'Controlled folder access' is on - it would silently stop the")
        ctx.log("  game saving (the cause of a friend's 4,300 broken saves). Allowing")
        ctx.log("  Fallout through it now. Windows will ask for permission - click Yes.")
        quoted = str(exe).replace("'", "''")   # PowerShell single-quote escaping
        command = f"Add-MpPreference -ControlledFolderAccessAllowedApplications '{quoted}'"
        proc = launch_elevated(Path("powershell.exe"),
                               ["-NoProfile", "-NonInteractive", "-Command", command],
                               ctx.fnv)
        while proc.running():
            time.sleep(1)
        ctx.log("    asked Windows to allow FalloutNV.exe")
    elif cfa == 2:
        ctx.log("  Controlled folder access is in audit mode (logs only) - no change needed")

    # 3. OneDrive: keep My Games on this PC, never online-only. Same as
    #    right-click > "Always keep on this device" (attrib +P = pinned).
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive and str(my_games).lower().startswith(onedrive.lower()):
        ctx.log("  your Documents are in OneDrive - keeping My Games on this PC")
        subprocess.run(["attrib", "+P", "-U", str(my_games), "/S", "/D"],
                       capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # Re-test after the fixes. Not when Controlled Folder Access is on: it
    # blocks THIS setup program too (it is not on the allow list), so the
    # probe would fail even though the game itself is now allowed.
    if cfa == 1:
        return
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise StepError(
            f"Windows still blocks writing to your save folder ({exc}).\n"
            "  Open Windows Security > Virus & threat protection > Manage ransomware\n"
            "  protection > Allow an app through Controlled folder access, add\n"
            f"  {exe}, then run setup again.\n"
            "  (If you use another antivirus, allow FalloutNV.exe in it.)"
        )


def configure_game(ctx: Context) -> None:
    """Apply the game settings NV:MP + TTW need, learned the hard way on the host:

    * sIntroMovie blank - TTW ships a 330 MB 'Fallout INTRO Vsk.bik' under the
      name New Vegas' default INI already points at, and under NV:MP the game
      crashed partway through playing it.
    * a 16:9 resolution at the screen's real size - see pick_resolution.
    * anti-aliasing off (part of the combination that cleared the font error).
    * Fallout.ini writable - the host's was read-only, which NV:MP cannot use.
    """
    settings = ctx.manifest.get("gameSettings") or {}
    folder = documents_dir() / "My Games" / "FalloutNV"
    fallout_ini = folder / "Fallout.ini"
    prefs_ini = folder / "FalloutPrefs.ini"

    if not (fallout_ini.is_file() and prefs_ini.is_file()):
        raise StepError(
            "New Vegas has not created its settings files yet.\n"
            "  Launch Fallout: New Vegas once from Steam, let it reach the main\n"
            "  menu, quit, then run setup again. It picks up where it left off."
        )

    width, height = pick_resolution(primary_screen_size())
    prefs = {"iSize W": str(width), "iSize H": str(height)}
    prefs.update(settings.get("FalloutPrefs.ini") or {})
    main = dict(settings.get("Fallout.ini") or {})

    ctx.log(f"  game settings: {width}x{height} (16:9), "
            + ", ".join(f"{k}={v}" for k, v in {**prefs, **main}.items() if not k.startswith("iSize")))
    if ctx.dry_run:
        return

    custom = ctx.manifest.get("customIni")
    if custom:
        path = folder / custom.get("file", "FalloutCustom.ini")
        body = custom["content"].replace("\r\n", "\n").replace("\n", "\r\n")
        if not (path.is_file() and path.read_bytes() == body.encode("ascii")):
            if path.is_file():
                backup = path.with_suffix(path.suffix + ".dojo-backup")
                if not backup.exists():
                    shutil.copy2(path, backup)
            path.write_bytes(body.encode("ascii"))
        ctx.log(f"    {path.name} written (TTW's settings: starting quest, stability)")

    for path, values in ((fallout_ini, main), (prefs_ini, prefs)):
        if not values:
            continue
        os.chmod(path, 0o666)  # clears read-only
        backup = path.with_suffix(path.suffix + ".dojo-backup")
        if not backup.exists():
            shutil.copy2(path, backup)
        missing = set_ini_values(path, values)
        if missing:
            ctx.log(f"    note: {path.name} had no {', '.join(missing)} line; left as is")


def game_settings_ok(ctx: Context) -> bool:
    """True if the INIs carry what configure_game sets."""
    folder = documents_dir() / "My Games" / "FalloutNV"
    settings = ctx.manifest.get("gameSettings") or {}
    for name, values in settings.items():
        if name.startswith("_") or not isinstance(values, dict):
            continue  # "_comment" and other notes, not INI files
        path = folder / name
        if not path.is_file():
            return False
        lines = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
                 for l in path.read_bytes().decode("latin-1").splitlines() if "=" in l}
        if any(lines.get(k) != v for k, v in values.items()):
            return False
    custom = ctx.manifest.get("customIni")
    if custom:
        path = folder / custom.get("file", "FalloutCustom.ini")
        body = custom["content"].replace("\r\n", "\n").replace("\n", "\r\n")
        if not (path.is_file() and path.read_bytes() == body.encode("ascii")):
            return False
    prefs = folder / "FalloutPrefs.ini"
    if prefs.is_file():
        lines = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
                 for l in prefs.read_bytes().decode("latin-1").splitlines() if "=" in l}
        try:
            if int(lines.get("iSize W", 0)) * 9 != int(lines.get("iSize H", 0)) * 16:
                return False
        except ValueError:
            return False
    return True


def write_launcher(ctx: Context) -> Path:
    """Drop a launcher .bat + a Desktop shortcut that joins the server."""
    server = ctx.manifest["server"]
    host, port = server["host"], server["port"]
    launcher = ctx.fnv / "Join Poosics Wasteland.bat"

    ctx.log(f"  writing launcher for {host}:{port}")
    if ctx.dry_run:
        return launcher

    launcher.write_text(
        "@echo off\r\n"
        f"title {server['name']}\r\n"
        # The game folder by full path, NOT %~dp0: the Desktop copy of this
        # file would otherwise look for nvmp_launcher.exe on the Desktop.
        f"cd /d \"{ctx.fnv}\"\r\n"
        "echo Starting NV:MP...\r\n"
        f"echo Server: {host}:{port}\r\n"
        "echo.\r\n"
        "echo If the launcher asks you to log in, choose \"Launch in offline mode\".\r\n"
        "echo Then choose \"Connect via IP\" and enter:\r\n"
        f"echo     {host}:{port}\r\n"
        "echo.\r\n"
        "start \"\" nvmp_launcher.exe\r\n",
        encoding="utf-8",
        newline="",  # already CRLF - see write_load_order
    )

    # A .lnk needs COM; a .url-style .bat copy on the Desktop is dependency-free
    # and survives the exe being frozen.
    desktop = desktop_dir()
    try:
        desktop.mkdir(parents=True, exist_ok=True)
        shutil.copy2(launcher, desktop / launcher.name)
        ctx.log(f"    shortcut placed on your Desktop: {desktop / launcher.name}")
    except OSError as exc:
        ctx.log(f"    could not put the shortcut on your Desktop ({exc}).")
        ctx.log(f"    Launch it from here instead: {launcher}")
    return launcher


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify_install(ctx: Context) -> dict:
    """Check the finished install and report exactly what is present."""
    order = ctx.manifest["loadOrder"]
    checks: list[tuple[str, bool, str]] = []

    for component in ctx.manifest["components"]:
        if not component.get("required", True):
            continue
        target = {
            "fnv_root": ctx.fnv,
            "fnv_data": ctx.fnv_data,
            "ttw_installer": ctx.fnv_data,
        }.get(component.get("install"))
        if target is None:
            continue
        expected = component.get("verify") or (
            ["TaleOfTwoWastelands.esm"] if component["id"] == "ttw" else []
        )
        for name in expected:
            path = target / name
            checks.append((f"{component['id']}: {name}", path.exists(), str(path)))

    for plugin in order:
        path = ctx.fnv_data / plugin
        checks.append((f"plugin: {plugin}", path.is_file(), str(path)))

    config = fnv_config_dir()
    for filename in ("plugins.txt", "loadorder.txt"):
        path = config / filename
        checks.append((f"config: {filename}", path.is_file(), str(path)))

    if not ctx.dry_run:
        checks.append(("game: FalloutNV.exe is 4GB-patched",
                       is_large_address_aware(ctx.fnv / "FalloutNV.exe"), str(ctx.fnv)))
        checks.append(("game: settings (16:9, intro video off, AA off)",
                       game_settings_ok(ctx), str(documents_dir() / "My Games" / "FalloutNV")))

    if not ctx.dry_run:
        saves = documents_dir() / "My Games" / "FalloutNV" / "Saves"
        probe, writable = saves / "dojo-write-test.tmp", False
        try:
            probe.write_bytes(b"ok"); probe.unlink(); writable = True
        except OSError:
            pass
        if controlled_folder_access() != 1:   # see protect_save_folder
            checks.append(("game: save folder is writable", writable, str(saves)))

    checks.append(("load order: plugin timestamps in order",
                   load_order_is_stamped(ctx), str(ctx.fnv_data)))

    # Read plugins.txt back byte-level: it has to parse to exactly the manifest
    # list, with nothing (a stray \r, a '*') clinging to the names.
    plugins = config / "plugins.txt"
    try:
        listed = plugins.read_bytes().decode("utf-8").split("\r\n")
        # NV:MP's launcher adds a "# NV:MP" comment line; comments are fine.
        exact = [n for n in listed if n and not n.startswith("#")] == list(order)
    except OSError:
        exact = False
    checks.append(("config: plugins.txt lists exactly the pack's plugins",
                   exact, str(plugins)))

    for problem in ttw_output_problems(ctx.fnv_data, ctx.manifest):
        checks.append((f"ttw build: {problem}", False, str(ctx.fnv_data)))

    # Parity with the host - only possible once the host has published its
    # hashes, and skipped in a dry run because it reads ~70 MB.
    if not ctx.dry_run:
        parity = ttw_parity_problems(ctx.fnv_data, ctx.manifest, ctx.log)
        for problem in parity:
            checks.append((f"parity: {problem}", False, str(ctx.fnv_data)))
        if (ctx.manifest.get("ttwOutput") or {}).get("sha256") and not parity:
            checks.append(("parity: TTW plugins identical to the host's", True,
                           str(ctx.fnv_data)))

    failed = [c for c in checks if not c[1]]
    return {"checks": checks, "failed": failed, "ok": not failed}


def run_all(ctx: Context, *, include_optional: bool = False,
            wait_for_ttw: bool = True, on_progress=None) -> dict:
    """Run the whole install in dependency order."""
    manifest_components = {c["id"]: c for c in ctx.manifest["components"]}

    # xNVSE and the OGG DLLs first (game root), then TTW builds into Data,
    # then YUPTTW goes on top of TTW, then NV:MP last so its launcher sees a
    # finished install.
    # Manifest order IS install order, so a new plugin is a manifest-only change
    # (the exe does not need rebuilding). Optional ones are skipped below.
    order = [c["id"] for c in ctx.manifest["components"]]
    if include_optional:
        order.append("mo2")

    for component_id in order:
        component = manifest_components.get(component_id)
        if component is None:
            continue
        if component.get("skipByDefault") and not include_optional:
            continue

        ctx.log(f"[{component_id}] {component['name']} {component.get('version','')}".rstrip())
        # TTW already in Data, or a matching build found elsewhere (Mod
        # Organizer)? Then the 1.2 GB TTW download is not needed at all.
        if component_id == "ttw":
            if not ttw_output_problems(ctx.fnv_data, ctx.manifest):
                ctx.log("  TTW is already built into Data - skipping.")
                ctx.results[component_id] = "ok"
                continue
            ctx.log("  looking for a TTW you already have...")
            if use_existing_ttw(ctx):
                ctx.results[component_id] = "ok"
                continue

        # Already installed, 4GB-patched, or sitting in the player's Mod
        # Organizer? Then there is nothing to download for this one.
        if component_id != "ttw" and satisfy_from_installed(ctx, component):
            ctx.results[component_id] = "ok"
            continue

        payload = acquire(ctx, component, on_progress)

        installer = INSTALLERS.get(component.get("install"))
        if installer is None:
            ctx.log(f"  no installer for target '{component.get('install')}' - skipped")
            continue

        if component.get("install") == "ttw_installer":
            installer(ctx, component, payload, wait=wait_for_ttw)
        else:
            installer(ctx, component, payload)

        ctx.results[component_id] = "ok"

    ctx.log("[config] load order and launcher")
    write_load_order(ctx)
    protect_save_folder(ctx)
    configure_game(ctx)
    write_launcher(ctx)

    return verify_install(ctx)
