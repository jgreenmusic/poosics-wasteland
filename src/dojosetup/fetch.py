"""Downloading and hash verification.

urllib from the stdlib rather than `requests`, so the frozen exe carries no
third-party HTTP stack. Downloads resume: these files run to 1.3 GB and a
friend on a flaky connection should not start over.

Every file is verified against the SHA-256 pinned in the manifest. That check
is not paranoia about corruption - NV:MP kicks any player whose plugins differ
from the host, so a wrong-version or truncated download has to be caught here
rather than an hour later at the handshake.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CHUNK = 1 << 20  # 1 MiB
USER_AGENT = "DojoSetup/1.0 (+https://github.com/jgreenmusic)"

ProgressFn = Callable[[int, int], None]


class FetchError(RuntimeError):
    pass


class HashMismatch(FetchError):
    def __init__(self, path: Path, expected: str, actual: str):
        self.path, self.expected, self.actual = path, expected, actual
        super().__init__(
            f"{path.name} failed its integrity check.\n"
            f"  expected sha256 {expected}\n"
            f"  got      sha256 {actual}\n"
            "This usually means the download was interrupted, or it is a "
            "different version than the one this pack pins. Delete the file "
            "and let setup fetch it again."
        )


@dataclass
class Payload:
    """One manifest component resolved to a concrete local file."""

    id: str
    name: str
    filename: str
    size: int
    sha256: str
    path: Path | None = None
    verified: bool = False


def sha256_file(path: Path, progress: ProgressFn | None = None) -> str:
    total = path.stat().st_size
    done = 0
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK << 2)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if progress:
                progress(done, total)
    return digest.hexdigest()


def verify(path: Path, expected_sha256: str, expected_size: int | None = None,
           progress: ProgressFn | None = None) -> bool:
    """True if `path` matches the pinned hash. Size is checked first as a
    cheap reject, so an obviously-truncated 1.3 GB file fails instantly
    instead of after a full hash pass."""
    if not path.is_file():
        return False
    if expected_size and path.stat().st_size != expected_size:
        return False
    return sha256_file(path, progress).lower() == expected_sha256.lower()


def download(url: str, dest: Path, *, expected_size: int | None = None,
             progress: ProgressFn | None = None, resume: bool = True) -> Path:
    """Download `url` to `dest`, resuming a partial file when possible."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    start = part.stat().st_size if (resume and part.is_file()) else 0
    if start and expected_size and start >= expected_size:
        start = 0  # partial file is bogus; start clean

    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"

    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        if start and exc.code in (416, 200):
            # Server rejected the range; fall back to a clean download.
            part.unlink(missing_ok=True)
            return download(url, dest, expected_size=expected_size,
                            progress=progress, resume=False)
        raise FetchError(f"Could not download {url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Could not reach {url}: {exc.reason}") from exc

    with response:
        # A 200 to a Range request means the server ignored it - restart at 0.
        if start and response.status == 200:
            start = 0
            part.unlink(missing_ok=True)

        remaining = response.headers.get("Content-Length")
        total = (int(remaining) + start) if remaining else (expected_size or 0)

        mode = "ab" if start else "wb"
        done = start
        with open(part, mode) as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                if progress:
                    progress(done, total)

    os.replace(part, dest)
    return dest


def find_local(filename: str, extra_dirs: list[Path] | None = None) -> list[Path]:
    """Look for an already-downloaded copy in the usual places.

    Manual components (TTW and friends) are downloaded by the player in their
    browser, so setup has to go find the result rather than fetch it.
    """
    candidates: list[Path] = []
    home = Path(os.path.expanduser("~"))
    search = [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        Path.cwd(),
    ]
    if extra_dirs:
        search = list(extra_dirs) + search

    seen: set[str] = set()
    for directory in search:
        if not directory.is_dir():
            continue
        # Exact name first, then a relaxed glob: mod.pub sometimes decorates a
        # filename with " (1)" or bracketed tags on re-download.
        for pattern in (filename, f"{Path(filename).stem}*{Path(filename).suffix}"):
            try:
                matches = sorted(directory.glob(pattern))
            except OSError:
                continue
            for match in matches:
                key = str(match).lower()
                if match.is_file() and key not in seen:
                    seen.add(key)
                    candidates.append(match)
    return candidates


def resolve_manual(component: dict, cache_dir: Path,
                   progress: ProgressFn | None = None) -> Path | None:
    """Find and verify a player-downloaded component. None if not found yet."""
    filename = component["filename"]
    expected = component["sha256"]
    size = component.get("size")

    cached = cache_dir / filename
    if verify(cached, expected, size, progress):
        return cached

    for candidate in find_local(filename, extra_dirs=[cache_dir]):
        if verify(candidate, expected, size, progress):
            return candidate
    return None


def resolve_auto(component: dict, cache_dir: Path,
                 progress: ProgressFn | None = None) -> Path:
    """Fetch an auto component into the cache and verify it."""
    filename = component["filename"]
    expected = component["sha256"]
    size = component.get("size")
    dest = cache_dir / filename

    if verify(dest, expected, size, progress):
        return dest

    download(component["url"], dest, expected_size=size, progress=progress)

    actual = sha256_file(dest, progress)
    if actual.lower() != expected.lower():
        raise HashMismatch(dest, expected, actual)
    return dest
