"""The install steps themselves.

Everything writes into the player's real Fallout: New Vegas folder. There is no
mod manager and no virtual filesystem, because NV:MP launches its own process
and cannot see one - see the mo2 component note in the manifest.

Every step is idempotent: re-running setup after a failure re-verifies rather
than re-doing, so a player who loses their connection halfway does not start
over.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
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


def run_ttw_installer(ctx: Context, component: dict, payload: Path,
                      wait: bool = True) -> None:
    """Extract and launch the TTW installer, then verify its output.

    This is the one step that cannot be automated: the installer is a Delphi
    GUI whose only switch is -NoOggEnc2. So setup extracts it, hands the player
    the exact two paths to paste, launches it, and then checks the result.
    """
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

    ctx.log("")
    ctx.log("  The TTW installer is a GUI and has no silent mode, so this part")
    ctx.log("  needs you. Paste these three values when it asks:")
    ctx.log(f"     Fallout 3 folder        {ctx.fo3}")
    ctx.log(f"     Fallout New Vegas       {ctx.fnv}")
    ctx.log(f"     Install TTW to          {ctx.fnv_data}")
    ctx.log("")
    ctx.log("  Leave every other option at its default. It runs 30-90 minutes.")
    ctx.log("")

    # -NoOggEnc2 skips the audio re-encode. The OGG component installs the
    # prebuilt Vorbis DLLs instead, which is both faster and removes a long
    # step that commonly fails on slower machines.
    subprocess.Popen([str(marker), "-NoOggEnc2"], cwd=str(installer_dir))

    if not wait:
        return

    ctx.log("  Waiting for TaleOfTwoWastelands.esm to appear...")
    expected = ctx.fnv_data / "TaleOfTwoWastelands.esm"
    while not expected.is_file():
        time.sleep(5)
    ctx.log("  TTW output detected.")


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


def write_load_order(ctx: Context) -> list[Path]:
    """Write the parity contract to plugins.txt and loadorder.txt.

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

        # plugins.txt marks enabled plugins with a leading '*'; loadorder.txt
        # is a plain ordered list. Writing both keeps the game and the tools
        # that read them in agreement.
        if filename == "plugins.txt":
            body = "\n".join(f"*{name}" for name in order)
        else:
            body = "\n".join(order)

        path.write_text(body + "\n", encoding="utf-8")
        written.append(path)

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
        "cd /d \"%~dp0\"\r\n"
        "echo Starting NV:MP...\r\n"
        f"echo Server: {host}:{port}\r\n"
        "echo.\r\n"
        "echo In the launcher choose \"Connect via IP\" and enter:\r\n"
        f"echo     {host}:{port}\r\n"
        "echo.\r\n"
        "start \"\" nvmp_launcher.exe\r\n",
        encoding="utf-8",
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
