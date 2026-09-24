# Poosic's Wasteland — one-download setup

Tale of Two Wastelands + NV:MP co-op, set up for you.

**[⬇ Download the setup tool](../../releases/latest)** — one file, ~16 MB. Run it. That's it.

You do **not** need Python, 7-Zip, Visual C++, or any other tool installed first.
(7-Zip is built into the setup tool. It's LGPL-licensed, and its license ships
inside the exe.)

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

**Download these three files** from the Files tab of
**<https://mod.pub/ttw/133/files>**. Take
exactly these versions. Setup checks each file's fingerprint and won't accept
a different one:

| File | What it is |
|---|---|
| `TTW_3.4.0_2026.06.11.7z` (~1.2 GB) | Tale of Two Wastelands 3.4 |
| `YUPTTW_13.9.1_2026.05.09.7z` | the YUP bug-fix patch for TTW |
| `TTW_OGG_Vorbis_2026.06.13.7z` | audio libraries TTW needs |

**And these from Nexus Mods** (free account needed). On each page open the
**Files** tab and use **Manual download**:

| Page | File to take |
|---|---|
| [FNV 4GB Patcher](https://www.nexusmods.com/newvegas/mods/62552?tab=files) | **"4GB Patcher"** (v1.5, 102 KB). *Not* "FNV4GB for Linux". |
| [NVTF - New Vegas Tick Fix](https://www.nexusmods.com/newvegas/mods/66537?tab=files) | **"NVTF"** (v10.61) **and** **"NVTF - INI"**. That's two files from the same page. |

Why these are required: without the 4GB patch, New Vegas runs out of memory
with TTW and crashes. Without NVTF, the host's game crashed over and over under
NV:MP. Both are fixed and tested. The window lists exactly which of all these
files you're still missing, with a tip for each one.

Setup downloads the rest for you: the script extender, the NV:MP client, and
the JohnnyGuitar and ShowOff plugins TTW needs.

Save them anywhere: your Downloads folder, Desktop or Documents all work. mod.pub
sometimes adds `[mod.pub]` to the filename. That's fine, setup still finds them.

**Before running setup, launch Fallout: New Vegas once from Steam** to the main
menu, then quit. That creates the settings files setup adjusts. Setup also:
turns off the intro video (TTW's intro crashed the game under NV:MP), picks a
**16:9 resolution** for your screen (NV:MP won't start otherwise), and turns off
anti-aliasing. Your original settings are backed up as `.dojo-backup`.

### Already have TTW installed?

If you set up TTW before (for example through Mod Organizer 2), **you don't need
to download or rebuild it.** Setup looks for it in the usual Mod Organizer
places, checks that it's the **same TTW version** as the host's, and links it
into your game folder. That's instant and uses no extra space, and your Mod
Organizer setup is left untouched. If yours is somewhere unusual, run setup with
`--ttw-from "D:\path\to\your\TTW mod folder"`.

If your TTW is a *different* version, setup says so and builds the matching
one, because a different version gets you kicked from the server. You'll still
need the YUPTTW and OGG files above, plus the Nexus ones.

Then setup will:

- **verify each one is the exact version this pack pins**,
- launch the TTW installer. **Windows asks for admin permission; click Yes.**
  The TTW installer requires it.
- show you the exact three paths to paste in. The destination is a new empty
  `TTW_output` folder next to your game, **not** the game's `Data` folder. Setup
  moves the result into `Data` itself once it has checked the build.
- **wait until you close the TTW window**, then check the build is complete and
  identical to the host's before carrying on.

The TTW installer itself has no silent mode — it's a GUI with exactly one
command-line switch — so this step needs a human. It runs 30–90 minutes.
Everything before and after it is automatic. If you close it early, run setup
again. It sets the partial build aside and starts fresh.

---

## Why the load order matters

NV:MP compares your active plugins against the host's and **kicks you if they
differ** — the error reads `Invalid mod revisions`.

That's the whole reason this tool exists, and the reason it pins exact versions
instead of "latest". `TaleOfTwoWastelands.esm` is *built on your machine* from
your own game files; it's never shipped. Identical inputs produce an identical
file, which is what lets you join. Feed it a different TTW version and you get a
different `.esm` and a failed handshake.

After the build, setup compares your `TaleOfTwoWastelands.esm` and `YUPTTW.esm`
against the host's fingerprints. A mismatch shows up **during setup**, with a
clear message, instead of as a kick when you try to join.

In New Vegas the load order is set by the plugins' **file dates**, not by
`plugins.txt`, so setup stamps those dates too.

The plugin order is the contract:

```
FalloutNV.esm          Fallout3.esm        ClassicPack.esm
DeadMoney.esm          Anchorage.esm       MercenaryPack.esm
HonestHearts.esm       ThePitt.esm         TribalPack.esm
OldWorldBlues.esm      BrokenSteel.esm     CaravanPack.esm
LonesomeRoad.esm       PointLookout.esm    TaleOfTwoWastelands.esm
GunRunnersArsenal.esm  Zeta.esm            YUPTTW.esm
```
(Read down each column.) The six Fallout 3 plugins in the middle are required
by TTW itself. Without them it doesn't load at all.

**Don't add other mods.** Any plugin the host doesn't have will get you kicked.

---

## Joining

Run **Join Poosics Wasteland** from your Desktop, pick **Connect via IP**, and
enter the address setup printed at the end.

The first time, the NV:MP launcher shows its Terms of Service. If a "Script
Error" box pops up over it, click **Yes** (or **No** if it keeps appearing).
It's the launcher's old built-in browser, and harmless. The launcher may then
download an update and close. Just run the shortcut again.

**You don't need an NV:MP account.** If it asks you to log in, choose **Launch in
offline mode**. NV:MP's own guide says login isn't required for co-op.

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

Mod Organizer only shows its mods to programs started *from inside* Mod
Organizer. Start the NV:MP launcher any other way (a shortcut, say) and those
mods are invisible, which gets you the `Invalid mod revisions` kick. NV:MP's own
TTW guide works around that by adding the launcher to Mod Organizer. This pack
does something simpler: it puts everything in the real game folder, so the
shortcut always works and the host and every player have the same layout.

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
