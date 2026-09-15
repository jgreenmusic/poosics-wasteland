# Poosic's Wasteland — one-download setup

Tale of Two Wastelands + NV:MP co-op, set up for you.

**[⬇ Download the setup tool](../../releases/latest)** — one file, ~16 MB. Run it. That's it.

You do **not** need Python, 7-Zip, Visual C++, or any other tool installed first.

---

## Before you start, you need

| | |
|---|---|
| **Fallout 3 — Game of the Year Edition** | on Steam, **installed** |
| **Fallout: New Vegas** | on Steam, **installed**, with all DLC |
| **~45 GB free disk space** | on whichever drive New Vegas lives on |
| **Neither game inside `C:\Program Files`** | see below |

GOTY / all-DLC matters: Tale of Two Wastelands is *built* from those DLC files. If
any are missing the setup stops and tells you which, instead of failing an hour in.

If a game is under `C:\Program Files`, Steam can move it for you:
**right-click the game → Properties → Installed Files → Move Install Folder.**
Windows protects that folder in a way that breaks the script extender, so setup
refuses to continue until it's moved.

---

## What it does

1. **Checks your PC** — finds Steam, both games, every DLC, and your free space.
2. **Tells you what to download** — see the next section.
3. **Installs everything** — script extender, audio libraries, TTW, the YUP patch,
   and the NV:MP co-op client, all into your real New Vegas folder.
4. **Sets your load order** — this is the part that decides whether you can
   actually join (see *Why the load order matters*).
5. **Puts a shortcut on your Desktop** that points at the server.

Your existing `plugins.txt` / `loadorder.txt` are backed up as `.dojo-backup`
before anything is changed, so your single-player setup isn't lost.

---

## The one part you have to do yourself

Setup downloads the script extender automatically, but **Tale of Two Wastelands
has to be downloaded by you**, and its installer has to be clicked through.

This isn't laziness in the tool — it's a condition of TTW existing at all.
Bethesda asked mod sites not to host TTW and required that it ship as an
installer that builds from *your own* Fallout 3 files, so that Fallout 3's assets
are never redistributed. Nobody is allowed to mirror it, this pack included.

So setup will:

- open the official download page for you,
- wait while you download the files (anywhere — your Downloads folder is fine),
- **verify each one is the exact version this pack pins**, then
- launch the TTW installer and show you the exact three paths to paste in.

The TTW installer itself has no silent mode — it's a GUI with exactly one
command-line switch — so this step needs a human. It runs 30–90 minutes.
Everything before and after it is automatic.

---

## Why the load order matters

NV:MP compares your active plugins against the host's and **kicks you if they
differ** — the error reads `Invalid mod revisions`.

That's the whole reason this tool exists, and the reason it pins exact versions
instead of "latest". `TaleOfTwoWastelands.esm` is *built on your machine* from
your own game files; it's never shipped. Identical inputs produce an identical
file, which is what lets you join. Feed it a different TTW version and you get a
different `.esm` and a failed handshake.

The plugin order is the contract:

```
FalloutNV.esm          ClassicPack.esm
DeadMoney.esm          MercenaryPack.esm
HonestHearts.esm       TribalPack.esm
OldWorldBlues.esm      CaravanPack.esm
LonesomeRoad.esm       TaleOfTwoWastelands.esm
GunRunnersArsenal.esm  YUPTTW.esm
```

**Don't add other mods.** Any plugin the host doesn't have will get you kicked.

---

## Joining

Run **Join Poosics Wasteland** from your Desktop, pick **Connect via IP**, and
enter the address setup printed at the end.

The server only accepts players while the host has it running, and the address
can change — setup always reads it live from this repo rather than from a value
baked into the download, so a change of address never means reinstalling.

---

## If something goes wrong

Everything is logged to `%LOCALAPPDATA%\DojoSetup\setup.log`. The window has an
**Open log folder** button. Send that file to Julian.

Setup is safe to re-run. It re-verifies what's already done instead of redoing
it, so an interrupted install picks up where it stopped — downloads resume
rather than starting over.

---

## No mod manager, on purpose

This installs straight into the real `Fallout New Vegas` folder rather than
through Mod Organizer 2.

NV:MP launches its own process, which **cannot see MO2's virtual filesystem** —
mods staged behind it are invisible to the game NV:MP actually starts, which
produces exactly the `Invalid mod revisions` kick above. NV:MP's own
documentation says the server "must be ran alongside your current Fallout
installation." Installing directly means the host and every player are
structurally identical, which is the best parity guarantee available.

MO2 is still available as an opt-in (`--with-mo2`) if you want it for
single-player modding, but it is not used to play here.

---

## For the host

```powershell
.\build.ps1 -Clean          # build dist\Poosics-Wasteland-Setup.exe
.\publish.ps1               # push the manifest + upload the exe to a release
```

`manifest/ttw.json` is the single source of truth — versions, SHA-256 hashes,
the DLC checklist, the load order, and the server address. Change the address
there and re-publish; nobody reinstalls.

**Re-hash whenever you bump a version.** A version in the manifest that doesn't
match what the host is actually running is a guaranteed failed handshake for
every friend.

Test without touching anything:

```powershell
python src\dojo_setup.py --check                       # PC checks only
python src\dojo_setup.py --dry-run --cache G:\path     # every step, no writes
```

The host also runs `nvmp_storyserver.exe` from inside its own New Vegas folder —
NV:MP co-op is peer-hosted, not a standalone dedicated server.
