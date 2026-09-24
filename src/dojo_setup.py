"""Dojo Setup - entry point.

Run with no arguments for the wizard. Flags exist mainly so the host can test
the whole thing without clicking through a GUI:

    dojo_setup.py --check              preflight only, changes nothing
    dojo_setup.py --dry-run            full run, no writes
    dojo_setup.py --cli                text mode install
    dojo_setup.py --manifest PATH      use a local manifest instead of fetching

The manifest is fetched at run time rather than compiled in, so a change of
server IP is fixed by re-publishing one small file - nobody reinstalls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # running as a script or frozen exe
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from dojosetup import __version__, archive, fetch, steamfind, steps

MANIFEST_URL = (
    "https://raw.githubusercontent.com/jgreenmusic/poosics-wasteland/main/manifest/ttw.json"
)

BANNER = r"""
  ____   ___  _  ___    ____  _____ _____ _   _ ____
 |  _ \ / _ \| |/ _ \  / ___|| ____|_   _| | | |  _ \
 | | | | | | | | | | | \___ \|  _|   | | | | | | |_) |
 | |_| | |_| | | |_| |  ___) | |___  | | | |_| |  __/
 |____/ \___/|_|\___/  |____/|_____| |_|  \___/|_|
"""


def log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DojoSetup"
    base.mkdir(parents=True, exist_ok=True)
    return base / "setup.log"


class Logger:
    """Print to the console and tee to a log the player can send back."""

    def __init__(self, sink=None):
        self.sink = sink
        self.path = log_path()
        self.handle = open(self.path, "a", encoding="utf-8")
        self.handle.write(f"\n=== Dojo Setup {__version__} "
                          f"{datetime.now().isoformat(timespec='seconds')} ===\n")

    def __call__(self, message: str = "") -> None:
        print(message)
        self.handle.write(message + "\n")
        self.handle.flush()
        if self.sink:
            self.sink(message)


def load_manifest(source: str | None, log) -> dict:
    """Local file if given, otherwise the published manifest, otherwise the
    copy next to the exe as a last resort."""
    if source:
        log(f"manifest: {source}")
        return json.loads(Path(source).read_text(encoding="utf-8"))

    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    local = bundled / "manifest" / "ttw.json"

    try:
        log(f"manifest: fetching {MANIFEST_URL}")
        request = urllib.request.Request(
            MANIFEST_URL, headers={"User-Agent": fetch.USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any failure falls back
        log(f"  could not fetch it ({exc})")
        if local.is_file():
            log(f"  using the bundled copy: {local}")
            return json.loads(local.read_text(encoding="utf-8"))
        raise SystemExit(
            "Could not load the setup manifest, and no bundled copy was found.\n"
            "Check your internet connection and try again."
        ) from exc


def print_preflight(result: dict, log) -> None:
    log("")
    log("Checking your PC")
    log(f"  Steam            {result['steam_root'] or 'NOT FOUND'}")
    log(f"  Steam libraries  {len(result['libraries'])}")
    for game in result["games"].values():
        state = "OK" if game.ok else "PROBLEM"
        log(f"  [{state:7}] {game.name}")
        if game.path:
            log(f"             {game.path}")
        for name in game.missing_files:
            log(f"             missing: {name}")
    disk = result["disk"]
    log(f"  Free space       {disk['free_gb']:.1f} GB "
        f"(need {disk['need_gb']:.0f} GB)")

    if result["ok"]:
        log("")
        log("  Everything checks out.")
    else:
        log("")
        log("  Setup cannot continue yet:")
        for blocker in result["blockers"]:
            log(f"    - {blocker}")


def progress_printer(log):
    """Coarse percentage ticker - one line per 10% so logs stay readable."""
    state = {"last": -1}

    def report(done: int, total: int) -> None:
        if not total:
            return
        pct = int(done * 100 / total)
        if pct // 10 > state["last"] // 10:
            state["last"] = pct
            log(f"    {pct:3d}%  ({done/1048576:.0f} / {total/1048576:.0f} MB)")

    return report


def run_cli(args) -> int:
    log = Logger()
    log(BANNER)
    log(f"Dojo Setup {__version__}   log: {log.path}")

    manifest = load_manifest(args.manifest, log)
    pack = manifest["pack"]
    server = manifest["server"]
    log(f"pack: {pack['name']} {pack['version']}  ->  {server['host']}:{server['port']}")

    dirs = fetch.use_search_dirs(args.search)
    if dirs:
        log(f"Also looking for downloads in {len(dirs)} Mod Organizer / chosen folder(s):")
        for folder in dirs[:8]:
            log(f"  {folder}")

    result = steamfind.preflight(manifest)
    print_preflight(result, log)
    if not result["ok"]:
        return 2
    if args.check:
        log("")
        log("--check only: nothing was installed.")
        return 0

    fnv = result["games"]["fnv"].path
    fo3 = result["games"]["fo3"].path
    cache = Path(args.cache) if args.cache else (
        Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DojoSetup" / "cache"
    )
    cache.mkdir(parents=True, exist_ok=True)

    ctx = steps.Context(
        manifest=manifest, fnv=fnv, fo3=fo3, cache=cache,
        log=log, dry_run=args.dry_run, ttw_from=args.ttw_from,
    )

    log("")
    log(f"Installing into {fnv}")
    log(f"Cache           {cache}")
    if args.dry_run:
        log("DRY RUN - nothing will be written.")
    log("")

    try:
        verdict = steps.run_all(
            ctx,
            include_optional=args.with_mo2,
            wait_for_ttw=not args.no_wait,
            on_progress=progress_printer(log),
        )
    except steps.StepError as exc:
        log("")
        log(f"STOPPED: {exc}")
        return 1
    except (fetch.HashMismatch, archive.ExtractError) as exc:
        log("")
        log(f"STOPPED: {exc}")
        return 1

    log("")
    log("Verification")
    for name, ok, path in verdict["checks"]:
        log(f"  [{'OK ' if ok else 'MISSING'}] {name}")
        if not ok:
            log(f"           expected at {path}")

    if verdict["ok"]:
        log("")
        log(f"Done. Launch \"Join Poosics Wasteland\" from your Desktop, choose")
        log(f"\"Connect via IP\", and enter {server['host']}:{server['port']}")
        return 0

    log("")
    log(f"{len(verdict['failed'])} item(s) are missing - see above. "
        f"Send {log.path} to Julian if you are stuck.")
    return 1


def run_gui(args) -> int:
    try:
        from dojosetup import gui
    except ImportError as exc:
        print(f"GUI unavailable ({exc}); falling back to text mode.\n")
        return run_cli(args)
    return gui.main(args, load_manifest=load_manifest, Logger=Logger)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="DojoSetup",
        description="One-download setup for Poosic's Wasteland (TTW + NV:MP).",
    )
    parser.add_argument("--check", action="store_true",
                        help="run the PC checks only, install nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="go through every step without writing anything")
    parser.add_argument("--cli", action="store_true", help="text mode, no window")
    parser.add_argument("--manifest", help="use a local manifest file")
    parser.add_argument("--cache", help="where to keep downloads")
    parser.add_argument("--with-mo2", action="store_true",
                        help="also install Mod Organizer 2 (not needed to play)")
    parser.add_argument("--no-wait", action="store_true",
                        help="do not block waiting for the TTW installer to finish")
    parser.add_argument("--search", metavar="FOLDER", action="append", default=[],
                        help="also look for downloads here (repeatable); Mod Organizer folders are found automatically")
    parser.add_argument("--ttw-from", metavar="FOLDER",
                        help="reuse a TTW build you already have (e.g. a Mod Organizer mod folder)")
    parser.add_argument("--version", action="version", version=f"Dojo Setup {__version__}")
    args = parser.parse_args(argv)

    try:
        if args.cli or args.check or args.dry_run:
            return run_cli(args)
        return run_gui(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception:  # noqa: BLE001 - last resort so the window never just vanishes
        print("\nSomething went wrong. Full details:\n")
        traceback.print_exc()
        print(f"\nA copy of this is in {log_path()}")
        if not (args.cli or args.check):
            input("\nPress Enter to close...")
        return 1


if __name__ == "__main__":
    sys.exit(main())
