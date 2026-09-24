"""Tkinter wizard.

Tkinter is stdlib and PyInstaller bundles it, so the frozen exe still needs
nothing installed on the player's machine.

One window rather than a multi-page wizard: a status list, a log, and one
button whose label always says what will happen next. The install is long and
partly manual, so the player needs to see where they are at all times.
"""

from __future__ import annotations

import threading
import traceback
import webbrowser
from pathlib import Path
from queue import Empty, Queue

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import archive, fetch, steamfind, steps

BG = "#1b1b1f"
FG = "#e8e8ea"
DIM = "#9a9aa2"
OK = "#5fc26a"
BAD = "#e55a5a"
WARN = "#e0a33e"
ACCENT = "#c8703a"


class SetupWindow:
    def __init__(self, root: tk.Tk, manifest: dict, logger, cache: str | None = None):
        self.root = root
        self.manifest = manifest
        self.log_fn = logger
        self.queue: Queue = Queue()
        self.preflight: dict | None = None
        self.worker: threading.Thread | None = None
        self.busy = False
        # --cache lets the host point setup at payloads it already has staged
        # instead of re-downloading 1.4 GB. Friends never pass it.
        self.cache_override = Path(cache) if cache else None

        pack = manifest["pack"]
        server = manifest["server"]

        root.title(f"{pack['name']} - Setup")
        root.configure(bg=BG)
        # Fit the screen: a fixed 620 px was taller than a scaled laptop screen,
        # which pushed the buttons off the bottom ("I don't see Install").
        height = min(620, root.winfo_screenheight() - 90)
        root.geometry(f"820x{max(height, 420)}")
        root.minsize(640, 400)

        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 8))
        tk.Label(header, text=pack["name"], bg=BG, fg=FG,
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(header,
                 text=f"Tale of Two Wastelands + NV:MP co-op   ·   "
                      f"{server['host']}:{server['port']}   ·   pack {pack['version']}",
                 bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))

        self.status_frame = tk.LabelFrame(
            root, text=" Your PC ", bg=BG, fg=DIM,
            font=("Segoe UI", 9, "bold"), bd=1, relief="solid",
            labelanchor="nw", padx=12, pady=10,
        )
        self.status_frame.pack(fill="x", padx=24, pady=(4, 8))
        self.status_rows: dict[str, tk.Label] = {}

        log_frame = tk.LabelFrame(
            root, text=" Progress ", bg=BG, fg=DIM,
            font=("Segoe UI", 9, "bold"), bd=1, relief="solid",
            labelanchor="nw", padx=2, pady=2,
        )
        # packed LAST (below), so the buttons and progress bar keep their space
        # and only the log shrinks when the window is short.

        self.text = tk.Text(log_frame, bg="#131316", fg=FG, bd=0,
                            font=("Consolas", 9), wrap="word",
                            insertbackground=FG, padx=10, pady=8)
        scroll = ttk.Scrollbar(log_frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.bar = ttk.Progressbar(root, mode="determinate", maximum=100)

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(side="bottom", fill="x", padx=24, pady=(0, 18))
        self.bar.pack(side="bottom", fill="x", padx=24, pady=(0, 6))
        log_frame.pack(fill="both", expand=True, padx=24, pady=(4, 8))

        self.action = tk.Button(
            buttons, text="Check my PC", command=self.on_action,
            bg=ACCENT, fg="white", activebackground="#d88451",
            activeforeground="white", bd=0, padx=22, pady=9,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        self.action.pack(side="right")

        self.secondary = tk.Button(
            buttons, text="Open downloads page", command=self.open_pages,
            bg="#2a2a30", fg=FG, activebackground="#35353d",
            activeforeground=FG, bd=0, padx=16, pady=9,
            font=("Segoe UI", 9), cursor="hand2",
        )
        self.secondary.pack(side="right", padx=(0, 10))
        self.secondary.pack_forget()

        self.extra_dirs: list[Path] = []
        self.search_btn = tk.Button(
            buttons, text="Search another folder...", command=self.pick_folder,
            bg="#2a2a30", fg=FG, activebackground="#35353d",
            activeforeground=FG, bd=0, padx=16, pady=9,
            font=("Segoe UI", 9), cursor="hand2",
        )
        self.search_btn.pack(side="right", padx=(0, 10))

        tk.Button(buttons, text="Open log folder", command=self.open_log,
                  bg=BG, fg=DIM, activebackground=BG, activeforeground=FG,
                  bd=0, padx=4, pady=9, font=("Segoe UI", 9),
                  cursor="hand2").pack(side="left")

        # Enter presses the main button, whatever it currently says.
        root.bind("<Return>", lambda _event: self.on_action())

        self.stage = "check"
        self.root.after(100, self.drain)
        self.root.after(300, self.on_action)  # check immediately on open

    # ---------------------------------------------------------------- output

    def emit(self, message: str = "") -> None:
        self.queue.put(("log", message))

    def drain(self) -> None:
        """Pump worker messages onto the Tk thread. Tk is not thread-safe, so
        every widget touch happens here and nowhere else."""
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.text.configure(state="normal")
                    self.text.insert("end", payload + "\n")
                    self.text.see("end")
                    self.text.configure(state="disabled")
                elif kind == "progress":
                    self.bar.configure(value=payload)
                elif kind == "status":
                    self.render_status(payload)
                elif kind == "stage":
                    self.set_stage(*payload)
                elif kind == "done":
                    self.busy = False
                    self.action.configure(state="normal")
        except Empty:
            pass
        self.root.after(100, self.drain)

    def render_status(self, result: dict) -> None:
        for row in self.status_frame.winfo_children():
            row.destroy()
        self.status_rows.clear()

        def add(label: str, value: str, colour: str) -> None:
            row = tk.Frame(self.status_frame, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, bg=BG, fg=DIM, width=16, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            tk.Label(row, text=value, bg=BG, fg=colour, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True)

        add("Steam", str(result["steam_root"] or "not found"),
            FG if result["steam_root"] else BAD)
        for game in result["games"].values():
            if game.ok:
                add(game.role.upper(), str(game.path), OK)
            elif game.found:
                detail = ", ".join(Path(f).name for f in game.missing_files) or "problem"
                add(game.role.upper(), f"missing: {detail}", BAD)
            else:
                add(game.role.upper(), "not installed", BAD)
        disk = result["disk"]
        add("Free space", f"{disk['free_gb']:.1f} GB (need {disk['need_gb']:.0f} GB)",
            OK if disk["free_gb"] >= disk["need_gb"] else BAD)

    def set_stage(self, stage: str, label: str) -> None:
        self.stage = stage
        self.action.configure(text=label)
        if stage == "manual":
            self.secondary.pack(side="right", padx=(0, 10))
        else:
            self.secondary.pack_forget()

    # ---------------------------------------------------------------- actions

    def on_action(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.action.configure(state="disabled")
        if self.stage == "check":
            self.spawn(self.do_check)
        elif self.stage in ("manual", "install"):
            self.spawn(self.do_install)
        elif self.stage == "done":
            self.root.destroy()

    def spawn(self, target) -> None:
        def wrapped():
            try:
                target()
            except Exception:  # noqa: BLE001 - never let the window die silently
                self.emit("")
                self.emit("Something went wrong:")
                for line in traceback.format_exc().splitlines():
                    self.emit("  " + line)
            finally:
                self.queue.put(("done", None))

        self.worker = threading.Thread(target=wrapped, daemon=True)
        self.worker.start()

    def pick_folder(self) -> None:
        """Let the player point at wherever they keep mod downloads - e.g. a
        Mod Organizer 'downloads' or 'mods' folder we did not find ourselves."""
        if self.busy:
            return
        folder = filedialog.askdirectory(
            title="Pick a folder with your Fallout mod downloads "
                  "(e.g. Mod Organizer's 'downloads' folder)")
        if not folder:
            return
        self.extra_dirs.append(Path(folder))
        self.emit(f"Also searching: {folder}")
        self.stage = "check"
        self.on_action()

    def do_check(self) -> None:
        self.emit("Checking your PC...")
        dirs = fetch.use_search_dirs(self.extra_dirs)
        if dirs:
            self.emit(f"  Also looking in {len(dirs)} Mod Organizer / chosen folder(s)")
            self.emit("  for files you already have:")
            for folder in dirs[:6]:
                self.emit(f"    {folder}")
            if len(dirs) > 6:
                self.emit(f"    ...and {len(dirs) - 6} more")
        result = steamfind.preflight(self.manifest)
        self.preflight = result
        self.queue.put(("status", result))

        if not result["ok"]:
            self.emit("")
            for blocker in result["blockers"]:
                self.emit(f"  ! {blocker}")
            self.emit("")
            self.emit("Fix the above, then click Check again.")
            self.queue.put(("stage", ("check", "Check again")))
            return

        self.emit("  Both games found with all their DLC.")
        self.emit("")

        missing = self.missing_manual()
        if missing:
            self.emit("You need to download these yourself first:")
            self.emit("")
            for component in missing:
                self.emit(f"  - {component['name']} {component.get('version','')}")
                self.emit(f"      file: {component['filename']}")
                self.emit(f"      from: {component.get('pageUrl','(see its site)')}")
                if component.get("downloadHint"):
                    self.emit(f"      tip:  {component['downloadHint']}")
            self.emit("")
            self.emit("These come from mod.pub (Tale of Two Wastelands) and Nexus Mods")
            self.emit("(free account). None of them may be re-hosted, so each player")
            self.emit("downloads their own copy.")
            self.emit("")
            self.emit("Tale of Two Wastelands cannot be bundled or mirrored: Bethesda")
            self.emit("required it to be distributed only by its own installer, so")
            self.emit("everyone downloads it from the official page. Save the files")
            self.emit("anywhere (your Downloads folder is fine) - setup finds them")
            self.emit("automatically and checks each one is the exact right version.")
            self.emit("")
            self.emit("Click 'Open downloads page', grab the files, then click Install.")
            self.emit("")
            self.emit("Already installed TTW with Mod Organizer? Click 'Search another")
            self.emit("folder...' and pick Mod Organizer's 'downloads' folder - setup")
            self.emit("will use the files you already have.")
            self.queue.put(("stage", ("manual", "Install")))
            return

        self.emit("All required files are present and verified.")
        self.queue.put(("stage", ("install", "Install now")))

    def missing_manual(self) -> list[dict]:
        cache = self.cache_dir()
        missing = []
        # A player who already has a complete TTW (usually through Mod
        # Organizer) never needs the 1.2 GB TTW download - setup reuses it.
        have_ttw = False
        if self.preflight and self.preflight.get("ok"):
            probe = steps.Context(manifest=self.manifest,
                                  fnv=self.preflight["games"]["fnv"].path,
                                  fo3=self.preflight["games"]["fo3"].path, cache=cache)
            have_ttw = steps.has_existing_ttw(probe)
        for component in self.manifest["components"]:
            if not component.get("required", True):
                continue
            if component.get("fetch") != "manual":
                continue
            if component["id"] == "ttw" and have_ttw:
                continue
            if self.preflight and self.preflight.get("ok") and                     steps.satisfy_from_installed(probe, component, apply=False):
                continue
            if fetch.resolve_manual(component, cache) is None:
                missing.append(component)
        return missing

    def cache_dir(self) -> Path:
        import os
        if self.cache_override is not None:
            self.cache_override.mkdir(parents=True, exist_ok=True)
            return self.cache_override
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        cache = base / "DojoSetup" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        return cache

    def open_pages(self) -> None:
        opened: set[str] = set()
        for component in self.missing_manual():
            url = component.get("pageUrl")
            if url and url not in opened:
                opened.add(url)
                webbrowser.open(url)
        if not opened:
            messagebox.showinfo("Nothing to download",
                                "Every required file is already on your PC.")

    def open_log(self) -> None:
        import os
        path = Path(getattr(self.log_fn, "path", "")) if self.log_fn else None
        target = path.parent if path else self.cache_dir().parent
        try:
            os.startfile(str(target))  # noqa: S606 - opening a folder for the user
        except OSError:
            messagebox.showinfo("Log location", str(target))

    def do_install(self) -> None:
        result = self.preflight or steamfind.preflight(self.manifest)
        if not result["ok"]:
            self.emit("Cannot install: your PC did not pass the checks.")
            self.queue.put(("stage", ("check", "Check again")))
            return

        still_missing = self.missing_manual()
        if still_missing:
            self.emit("")
            self.emit("Still waiting on:")
            for component in still_missing:
                self.emit(f"  - {component['filename']}")
            self.emit("")
            self.emit("If you already downloaded them, they may still be saving, or")
            self.emit("they may be a different version than this pack pins.")
            self.queue.put(("stage", ("manual", "Install")))
            return

        state = {"last": -1}

        def on_progress(done: int, total: int) -> None:
            if not total:
                return
            pct = int(done * 100 / total)
            self.queue.put(("progress", pct))
            if pct // 10 > state["last"] // 10:
                state["last"] = pct
                self.emit(f"    {pct:3d}%  ({done/1048576:.0f} / {total/1048576:.0f} MB)")

        ctx = steps.Context(
            manifest=self.manifest,
            fnv=result["games"]["fnv"].path,
            fo3=result["games"]["fo3"].path,
            cache=self.cache_dir(),
            log=self.emit,
        )

        self.emit("")
        self.emit(f"Installing into {ctx.fnv}")
        self.emit("")

        try:
            verdict = steps.run_all(ctx, on_progress=on_progress)
        except (steps.StepError, fetch.HashMismatch, archive.ExtractError) as exc:
            self.emit("")
            self.emit(f"STOPPED: {exc}")
            self.queue.put(("stage", ("manual", "Try again")))
            return

        self.emit("")
        self.emit("Verification")
        for name, ok, path in verdict["checks"]:
            self.emit(f"  [{'OK ' if ok else 'MISSING'}] {name}")
            if not ok:
                self.emit(f"           expected at {path}")

        server = self.manifest["server"]
        if verdict["ok"]:
            self.emit("")
            self.emit("Done. There is now a 'Join Poosics Wasteland' shortcut on your")
            self.emit("Desktop. Run it, choose \"Connect via IP\", and enter:")
            self.emit("")
            self.emit(f"      {server['host']}:{server['port']}")
            self.queue.put(("progress", 100))
            self.queue.put(("stage", ("done", "Close")))
        else:
            self.emit("")
            self.emit(f"{len(verdict['failed'])} item(s) missing. Click 'Open log folder'")
            self.emit("and send setup.log to Julian.")
            self.queue.put(("stage", ("manual", "Try again")))


def main(args, *, load_manifest, Logger) -> int:
    root = tk.Tk()

    class Tee:
        """Logger that also feeds the window once it exists."""

        def __init__(self):
            self.inner = Logger()
            self.path = self.inner.path
            self.window = None

        def __call__(self, message: str = "") -> None:
            self.inner(message)

    logger = Tee()
    manifest = load_manifest(args.manifest, logger)

    window = SetupWindow(root, manifest, logger, cache=getattr(args, "cache", None))

    # Route every log line into the window too.
    original = logger.__call__

    def both(message: str = "") -> None:
        original(message)

    logger.__call__ = both  # type: ignore[method-assign]
    window.emit(f"Log: {logger.path}")

    root.mainloop()
    return 0
