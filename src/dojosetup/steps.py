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
            "  then run setup again - it checks your Downloads folder automatically."
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


def install_fnv_data(ctx: Context, component: dict, payload: Path) -> None:
    """Extract into Data/ (YUPTTW)."""
    target = ctx.fnv_data
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


INSTALLERS = {
    "fnv_root": install_fnv_root,
    "fnv_data": install_fnv_data,
    "ttw_installer": run_ttw_installer,
    "optional_tool": install_optional_tool,
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
        "echo In the launcher choose \"Connect via IP\" and enter:\r\n"
        f"echo     {host}:{port}\r\n"
        "echo.\r\n"
        "start \"\" nvmp_launcher.exe\r\n",
        encoding="utf-8",
        newline="",  # already CRLF - see write_load_order
    )

    # A .lnk needs COM; a .url-style .bat copy on the Desktop is dependency-free
    # and survives the exe being frozen.
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        try:
            shutil.copy2(launcher, desktop / launcher.name)
            ctx.log(f"    shortcut placed on your Desktop: {launcher.name}")
        except OSError as exc:
            ctx.log(f"    could not write the Desktop shortcut ({exc}); "
                    f"launch from {launcher} instead")
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

    checks.append(("load order: plugin timestamps in order",
                   load_order_is_stamped(ctx), str(ctx.fnv_data)))

    # Read plugins.txt back byte-level: it has to parse to exactly the manifest
    # list, with nothing (a stray \r, a '*') clinging to the names.
    plugins = config / "plugins.txt"
    try:
        listed = plugins.read_bytes().decode("utf-8").split("\r\n")
        exact = [name for name in listed if name] == list(order)
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
    order = ["xnvse", "ttw_ogg", "ttw", "yupttw", "nvmp"]
    if include_optional:
        order.append("mo2")

    for component_id in order:
        component = manifest_components.get(component_id)
        if component is None:
            continue
        if component.get("skipByDefault") and not include_optional:
            continue

        ctx.log(f"[{component_id}] {component['name']} {component.get('version','')}".rstrip())
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
    write_launcher(ctx)

    return verify_install(ctx)
