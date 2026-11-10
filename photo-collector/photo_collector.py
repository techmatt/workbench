# /// script
# requires-python = ">=3.11"
# dependencies = ["exifread>=3.0"]
# ///
"""photo-collector: gather photos and videos from download folders into one library.

    uv run photo_collector.py collect <source> --library <dir> [options]
    uv run photo_collector.py retire  <source> --library <dir> [--delete] [options]
    uv run photo_collector.py pack    --library <dir> [--zips <dir>] [--dry-run]
    uv run photo_collector.py migrate --library <dir> [--apply]

Sources are only ever read. See README.md for the layout and settings.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import sys
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field

PHOTO_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".gif", ".tif", ".tiff",
             ".dng", ".bmp", ".avif"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".wmv",
             ".mpg", ".mpeg", ".webm"}
LIVE_PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg"}
CHUNK = 8 << 20
META = "_metadata"              # everything the tool keeps that isn't a photo or video
MANIFEST = "manifest.json"
HASH_CACHE = "hashes.json"
STREAMS = ("photos", "videos", "live")   # live: Live Photo clips
OLD_STREAMS = ("photos", "videos")
# a volume folder: "2023" (first volume), "2023-2", ...; "undated", "undated-2", ...
VOLUME = re.compile(r"(\d{4}|undated)(?:-([2-9]|[1-9]\d+))?")
NOW = dt.datetime.now()

log = logging.getLogger("photo-collector")


# --------------------------------------------------------------------------- source items

@dataclass
class Item:
    src: str                 # absolute path, or "<zip path>!<member>"
    rel: str                 # path relative to the source root (posix), zips as "a.zip!m"
    folder: str              # pairing/sidecar scope: directory, or "<zip>!<member dir>"
    name: str
    size: int
    stamp: str               # cache validity stamp
    zip: str | None = None
    member: str | None = None
    kind: str = "other"      # photo | video | sidecar | other
    excluded: bool = False
    sha: str | None = None

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()


def classify(name: str) -> str:
    e = os.path.splitext(name)[1].lower()
    if e in PHOTO_EXT:
        return "photo"
    if e in VIDEO_EXT:
        return "video"
    if e == ".json":
        return "sidecar"
    return "other"


def is_excluded(rel: str, patterns: list[str]) -> bool:
    """A pattern matches a path or any folder above it. A zip counts as the folder it was made
    from (face_labels matches face_labels.zip), and its members as if extracted beside it
    (a.zip!face_labels/x.jpg is also tried as face_labels/x.jpg)."""
    rel = rel.lower()
    cands = [rel]
    if "!" in rel:
        z, member = rel.split("!", 1)
        zdir = z.rsplit("/", 1)[0] + "/" if "/" in z else ""
        cands = [z, zdir + member]
    prefixes = []
    for c in cands:
        parts = c.split("/")
        prefixes += ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    prefixes += [p[:-4] for p in prefixes if p.endswith(".zip")]
    for pat in patterns:
        pat = pat.lower().strip("/").replace("\\", "/")
        if any(fnmatch.fnmatchcase(p, pat) for p in prefixes):
            return True
    return False


def walk_source(root: str, excludes: list[str], read_zips: bool):
    """Yield every file under root; with read_zips, a zip's members follow the zip itself."""
    root = os.path.abspath(root)
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            st = os.stat(p)
            it = Item(src=p, rel=rel, folder=dp, name=fn, size=st.st_size,
                      stamp=str(st.st_mtime_ns), kind=classify(fn))
            it.excluded = is_excluded(rel, excludes)
            if fn.lower().endswith(".zip"):
                it.kind = "zip"
            yield it
            if it.kind == "zip" and read_zips and not it.excluded:
                try:
                    zf = zipfile.ZipFile(p)
                except zipfile.BadZipFile as e:
                    log.warning("unreadable zip %s: %s", p, e)
                    continue
                with zf:
                    for zi in zf.infolist():
                        if zi.is_dir():
                            continue
                        mdir, mname = os.path.split(zi.filename)
                        mrel = f"{rel}!{zi.filename}"
                        yield Item(src=f"{p}!{zi.filename}", rel=mrel,
                                   folder=f"{p}!{mdir}", name=mname, size=zi.file_size,
                                   stamp=f"{st.st_mtime_ns}:{zi.CRC}", zip=p, member=zi.filename,
                                   kind=classify(mname), excluded=is_excluded(mrel, excludes))


class Opener:
    """Opens loose files and zip members, keeping the current zip open."""

    def __init__(self):
        self._zp = None
        self._zf = None

    def open(self, it: Item):
        if it.zip is None:
            return open(it.src, "rb")
        if self._zp != it.zip:
            self.close()
            self._zf = zipfile.ZipFile(it.zip)
            self._zp = it.zip
        return self._zf.open(it.member)

    def close(self):
        if self._zf is not None:
            self._zf.close()
        self._zf = self._zp = None


def hash_stream(f, out=None) -> str:
    h = hashlib.sha256()
    while True:
        b = f.read(CHUNK)
        if not b:
            break
        h.update(b)
        if out is not None:
            out.write(b)
    return h.hexdigest()


def hash_path(path: str) -> str:
    with open(path, "rb") as f:
        return hash_stream(f)


class Meter:
    def __init__(self, label: str, total_bytes: int):
        self.label, self.total, self.done, self.n = label, total_bytes, 0, 0
        self.t0 = self.last = time.time()

    def add(self, nbytes: int):
        self.done += nbytes
        self.n += 1
        now = time.time()
        if now - self.last > 30:
            self.last = now
            rate = self.done / max(now - self.t0, 1e-6)
            left = (self.total - self.done) / max(rate, 1)
            log.info("%s: %d files, %.1f/%.1f GB, %.0f MB/s, ~%.0f min left", self.label,
                     self.n, self.done / 1e9, self.total / 1e9, rate / 1e6, left / 60)


# --------------------------------------------------------------------------- dates

FN_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[-_ T.]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d))?")


def plausible(d: dt.datetime | None) -> dt.datetime | None:
    if d is None or d.year < 1990 or d > NOW + dt.timedelta(days=2):
        return None
    return d


def date_from_filename(name: str) -> tuple[dt.datetime, bool] | None:
    """Returns (datetime, has_time)."""
    for m in FN_DATE.finditer(os.path.splitext(name)[0]):
        y, mo, d, hh, mm, ss = m.groups()
        try:
            if hh is not None:
                v = dt.datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
            else:
                v = dt.datetime(int(y), int(mo), int(d))
        except ValueError:
            continue
        if plausible(v):
            return v, hh is not None
    return None


def parse_exif_dt(s: str) -> dt.datetime | None:
    s = s.strip().replace("\x00", "")[:19]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return plausible(dt.datetime.strptime(s, fmt))
        except ValueError:
            continue
    return None


def date_from_exif(path: str) -> dt.datetime | None:
    import exifread
    logging.getLogger("exifread").setLevel(logging.CRITICAL)
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return None
    for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized"):
        if key in tags:
            d = parse_exif_dt(str(tags[key]))
            if d:
                return d
    return None


def _atoms(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        body = pos + 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            body = pos + 16
        elif size == 0:
            size = end - pos
        if size < 8:
            return
        yield typ, body, min(pos + size, end)
        pos += size


def _child(f, start, end, typ):
    for t, b, e in _atoms(f, start, end):
        if t == typ:
            return b, e
    return None


def date_from_quicktime(path: str) -> dt.datetime | None:
    """Apple's com.apple.quicktime.creationdate (local wall time), else mvhd (UTC -> local)."""
    try:
        with open(path, "rb") as f:
            end = os.fstat(f.fileno()).st_size
            moov = _child(f, 0, end, b"moov")
            if not moov:
                return None
            meta = _child(f, *moov, b"meta")
            if meta:
                keys = _child(f, *meta, b"keys")
                ilst = _child(f, *meta, b"ilst")
                if keys and ilst:
                    f.seek(keys[0])
                    _, count = struct.unpack(">II", f.read(8))
                    names, pos = [], keys[0] + 8
                    for _ in range(min(count, 1000)):
                        f.seek(pos)
                        ksize, _ns = struct.unpack(">I4s", f.read(8))
                        names.append(f.read(max(ksize - 8, 0)).decode("utf-8", "replace"))
                        pos += max(ksize, 8)
                    for t, b, e in _atoms(f, *ilst):
                        idx = struct.unpack(">I", t)[0]
                        if 1 <= idx <= len(names) and names[idx - 1] == "com.apple.quicktime.creationdate":
                            data = _child(f, b, e, b"data")
                            if data:
                                f.seek(data[0] + 8)
                                s = f.read(data[1] - data[0] - 8).decode("utf-8", "replace")
                                try:
                                    d = plausible(dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
                                except ValueError:
                                    d = None
                                if d:
                                    return d
            mvhd = _child(f, *moov, b"mvhd")
            if mvhd:
                f.seek(mvhd[0])
                ver = f.read(4)[0]
                secs = struct.unpack(">Q", f.read(8))[0] if ver == 1 else struct.unpack(">I", f.read(4))[0]
                if secs:
                    utc = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=secs)
                    return plausible(utc.astimezone().replace(tzinfo=None))
    except (OSError, struct.error, IndexError, OverflowError, ValueError):
        return None
    return None


def video_seconds(path: str) -> float | None:
    """Duration from the QuickTime/MP4 mvhd atom, or None."""
    try:
        with open(path, "rb") as f:
            end = os.fstat(f.fileno()).st_size
            moov = _child(f, 0, end, b"moov")
            mvhd = moov and _child(f, *moov, b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            ver = f.read(4)[0]
            if ver == 1:
                f.read(16)
                scale, dur = struct.unpack(">IQ", f.read(12))
            else:
                f.read(8)
                scale, dur = struct.unpack(">II", f.read(8))
            return dur / scale if scale else None
    except (OSError, struct.error, IndexError):
        return None


def date_from_sidecar(data: bytes) -> dt.datetime | None:
    try:
        j = json.loads(data)
        ts = int(j["photoTakenTime"]["timestamp"])
    except (ValueError, KeyError, TypeError):
        return None
    if ts <= 0:
        return None
    return plausible(dt.datetime.fromtimestamp(ts))


# --------------------------------------------------------------------------- library state

def rel_to_abs(lib: str, rel: str) -> str:
    return os.path.join(lib, *rel.split("/"))


def meta_path(lib: str, *parts: str) -> str:
    return os.path.join(lib, META, *parts)


def volume_folder(stream: str, year: str, n: int) -> str:
    """The first volume is the year folder itself; later ones are siblings: 2023, 2023-2, ..."""
    return f"{stream}/{year}" if n == 1 else f"{stream}/{year}-{n}"


def check_layout(lib: str):
    """Refuse a library still in the old layout (manifest at the base, vNN folders)."""
    if os.path.exists(os.path.join(lib, MANIFEST)):
        sys.exit(f"{lib} is in the old layout (manifest.json at its base): run "
                 f"'photo_collector.py migrate --library \"{lib}\" --apply' first")


def load_json(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def check_paths(source: str, lib: str):
    s, l = os.path.normcase(os.path.abspath(source)), os.path.normcase(os.path.abspath(lib))
    if s == l or l.startswith(s + os.sep) or s.startswith(l + os.sep):
        sys.exit(f"source and library must not contain each other: {source} / {lib}")


# --------------------------------------------------------------------------- collect

SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def scan_and_hash(source, lib, excludes, read_zips):
    items = list(walk_source(source, excludes, read_zips))
    cache_path = meta_path(lib, HASH_CACHE)
    cache = load_json(cache_path, {})
    todo = [it for it in items if it.kind in ("photo", "video") and not it.excluded]
    need = [it for it in todo if not (it.src in cache and cache[it.src][0] == it.size
                                     and cache[it.src][1] == it.stamp)]
    need_ids = {id(it) for it in need}
    for it in todo:
        if id(it) not in need_ids:
            it.sha = cache[it.src][2]
    log.info("scanned %d entries; %d media, %d to hash (%.1f GB), %d from cache", len(items),
             len(todo), len(need), sum(i.size for i in need) / 1e9, len(todo) - len(need))
    meter, op, since_save = Meter("hash", sum(i.size for i in need)), Opener(), 0
    try:
        for it in need:
            with op.open(it) as f:
                it.sha = hash_stream(f)
            cache[it.src] = [it.size, it.stamp, it.sha]
            meter.add(it.size)
            since_save += it.size
            if since_save > 5e9:
                save_json(cache_path, cache)
                since_save = 0
    finally:
        op.close()
        save_json(cache_path, cache)
    return items


class Library:
    def __init__(self, lib: str, volume_mb: int, large_mb: int):
        self.lib = lib
        self.path = meta_path(lib, MANIFEST)
        self.m = load_json(self.path, {"version": 2, "files": {}})
        self.files: dict = self.m["files"]
        self.volume_bytes = volume_mb * 1_000_000
        self.large_bytes = large_mb * 1_000_000
        self.vols: dict[tuple[str, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.taken: set[str] = set()
        # sealed volumes (zipped by pack): never added to again
        self.sealed = {v for v, rec in self.m.get("volumes", {}).items() if rec.get("sealed")}
        for e in self.files.values():
            self._note(e["path"], e["size"])
        for v in self.sealed:                      # a sealed volume counts even if emptied
            stream, folder = v.split("/")
            m = VOLUME.fullmatch(folder)
            self.vols[(stream, m[1])].setdefault(int(m[2] or 1), 0)

    def _note(self, rel: str, size: int):
        self.taken.add(rel.lower())
        parts = rel.split("/")
        m = len(parts) == 3 and parts[0] in STREAMS and VOLUME.fullmatch(parts[1])
        if m:
            self.vols[(parts[0], m[1])][int(m[2] or 1)] += size

    def save(self):
        save_json(self.path, self.m)

    def volume_for(self, stream: str, year: str, size: int) -> str:
        v = self.vols[(stream, year)]
        cur = max(v) if v else 1
        if volume_folder(stream, year, cur) in self.sealed or (
                v.get(cur, 0) and v[cur] + size > self.volume_bytes):
            cur += 1                               # sealed or full: open the next volume
        v.setdefault(cur, 0)
        return volume_folder(stream, year, cur)

    def unique_name(self, folder: str, base: str, ext: str, sha: str) -> tuple[str, bool]:
        """Returns (relative path, adopt). adopt: the file is already there with these bytes
        (copied by a run that stopped before saving the manifest)."""
        for rel in (f"{folder}/{base}{ext}", f"{folder}/{base}_{sha[:8]}{ext}"):
            if rel.lower() in self.taken:
                continue
            p = rel_to_abs(self.lib, rel)
            if not os.path.exists(p):
                return rel, False
            if hash_path(p) == sha:
                return rel, True
        sys.exit(f"cannot find a free name for {folder}/{base}{ext}")

    def add(self, rel: str, sha: str, entry: dict):
        self.files[sha] = entry
        self._note(rel, entry["size"])


def volume_year(rel: str) -> str:
    """The year (or 'undated') of a library path's volume: photos/2023-2/x -> 2023."""
    m = VOLUME.fullmatch(rel.split("/")[1])
    if m is None:                                  # videos/large/<year>/x
        m = VOLUME.fullmatch(rel.split("/")[2])
    return m[1]


def link_live(files: dict, clip_sha: str, photo_sha: str):
    """Record a Live Photo pairing both ways: the clip's live_photo_of, the photo's live_clips."""
    files[clip_sha]["live_photo_of"] = photo_sha
    clips = files[photo_sha].setdefault("live_clips", [])
    if clip_sha not in clips:
        clips.append(clip_sha)


def find_sidecar_dates(items: list[Item]):
    """folder -> {json name lower: item} for sidecar lookups."""
    by_folder = defaultdict(dict)
    for it in items:
        if it.kind == "sidecar" and not it.excluded:
            by_folder[it.folder][it.name.lower()] = it
    return by_folder


def sidecar_for(it: Item, side: dict) -> list[Item]:
    js = side.get(it.folder)
    if not js:
        return []
    n = it.name.lower()
    out = [j for name, j in js.items() if name.startswith(n) or (len(n) > 46 and name.startswith(n[:46]))]
    return sorted(out, key=lambda j: len(j.name))


def read_bytes(it: Item, op: Opener) -> bytes:
    with op.open(it) as f:
        return f.read()


def collect(args):
    source, lib = os.path.abspath(args.source), os.path.abspath(args.library)
    check_paths(source, lib)
    check_layout(lib)
    os.makedirs(lib, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    L = Library(lib, args.volume_mb, args.large_video_mb)
    if any(clips_to_move(L.files)):
        sys.exit(f"{lib} still has Live Photo clips in photos/: run "
                 f"'photo_collector.py migrate --library \"{lib}\" --apply' first")
    items = scan_and_hash(source, lib, args.exclude, not args.no_zips)
    side = find_sidecar_dates(items)

    media = [it for it in items if it.kind in ("photo", "video") and not it.excluded]
    counts = Counter()
    counts["entries scanned"] = len(items)
    counts["loose files"] = sum(1 for i in items if i.zip is None)
    counts["zip members"] = sum(1 for i in items if i.zip is not None)
    counts["media files"] = len(media)
    skipped = [(i.rel, i.size, "excluded" if i.excluded else
                ("zip (members read)" if i.kind == "zip" and not args.no_zips else "type " + (i.ext or "(none)")))
               for i in items if i.excluded or i.kind not in ("photo", "video")]
    counts["excluded"] = sum(1 for s in skipped if s[2] == "excluded")
    counts["skipped by type"] = sum(1 for s in skipped if s[2].startswith("type "))
    counts["zips read"] = sum(1 for s in skipped if s[2].startswith("zip "))

    # Live Photo pairing: a video sharing a stem with a photo in the same folder
    photos_by_folder = defaultdict(dict)
    for it in media:
        if it.kind == "photo" and it.ext in LIVE_PHOTO_EXT:
            cur = photos_by_folder[it.folder].get(it.stem.lower())
            if cur is None or (it.ext in (".heic", ".heif") and cur.ext not in (".heic", ".heif")):
                photos_by_folder[it.folder][it.stem.lower()] = it
    live_of: dict[str, str] = {}          # video sha -> photo sha
    for it in media:
        if it.kind == "video":
            p = photos_by_folder[it.folder].get(it.stem.lower())
            if p is not None and it.sha not in live_of:
                live_of[it.sha] = p.sha

    # Group occurrences by hash; record new source paths for known hashes
    occ: dict[str, list[Item]] = defaultdict(list)
    for it in media:
        occ[it.sha].append(it)
    for sha, its in occ.items():
        if sha in L.files:
            srcs = L.files[sha]["sources"]
            for it in its:
                if it.src not in srcs:
                    srcs.append(it.src)
    new = {sha: its for sha, its in occ.items() if sha not in L.files}
    counts["duplicates"] = len(media) - len(new)
    need = sum(its[0].size for its in new.values())
    free = shutil.disk_usage(lib).free
    budget = free - args.reserve_gb * 1e9
    log.info("%d new files (%.1f GB unique); %.1f GB free in %s", len(new), need / 1e9, free / 1e9, lib)
    counts["new unique GB"] = round(need / 1e9, 2)
    counts["library drive free GB before copy"] = round(free / 1e9, 1)
    if need > budget:
        log.warning("not everything fits: copying oldest first until %.0f GB remain free", args.reserve_gb)

    # Date every new file (zip-only ones are staged into the library first)
    staging = meta_path(lib, "staging")
    os.makedirs(staging, exist_ok=True)
    op = Opener()
    plan = []
    meter = Meter("date/stage", sum(its[0].size for its in new.values() if all(i.zip for i in its)))
    try:
        for sha, its in new.items():
            its.sort(key=lambda i: (i.zip is not None, i.src))
            rep = its[0]
            staged = os.path.join(staging, sha + rep.ext)
            if os.path.exists(staged) and os.path.getsize(staged) == rep.size:
                local = staged              # left by a stopped run (or a rebuild): reuse
            elif rep.zip is None:
                local = rep.src
            else:
                local = staged
                with op.open(rep) as f, open(local + ".part", "wb") as out:
                    got = hash_stream(f, out)
                if got != sha:
                    sys.exit(f"hash changed while staging {rep.src}")
                os.replace(local + ".part", local)
                meter.add(rep.size)
            d, src_kind, has_time = None, None, True
            if rep.kind == "photo":
                d = date_from_exif(local)
                src_kind = "exif" if d else None
            else:
                d = date_from_quicktime(local)
                src_kind = "quicktime" if d else None
            if d is None:
                for it in its:
                    for j in sidecar_for(it, side):
                        d = date_from_sidecar(read_bytes(j, op))
                        if d:
                            src_kind = "sidecar"
                            break
                    if d:
                        break
            if d is None:
                for it in its:
                    r = date_from_filename(it.name)
                    if r:
                        d, has_time = r
                        src_kind = "filename"
                        break
            plan.append(dict(sha=sha, its=its, rep=rep, local=local, date=d,
                             date_source=src_kind, has_time=has_time,
                             seconds=video_seconds(local) if rep.kind == "video" else None))
    finally:
        op.close()

    # Place: chronological, Live Photo clips straight after their photo (so live/ volumes fill
    # in the photos' date order)
    by_sha = {p["sha"]: p for p in plan}
    live_videos = defaultdict(list)
    for p in plan:
        ph = live_of.get(p["sha"]) if p["rep"].kind == "video" else None
        if ph is not None and (p["seconds"] is None or p["seconds"] > args.live_max_seconds):
            counts["same-stem videos too long to be Live Photos"] += 1
            ph = None
        if ph is not None:
            p["live_photo"] = ph
            if ph in by_sha:
                live_videos[ph].append(p)
    order = []
    for p in sorted(plan, key=lambda p: (p["date"] is None, p["date"] or NOW, p["rep"].name.lower())):
        if p.get("live_photo") in by_sha:
            continue
        order.append(p)
        lv = live_videos.get(p["sha"], [])
        order.extend(lv)
        # space on the drive: a photo is copied only if its clips fit too
        p["reserve"] = sum(v["rep"].size for v in lv)

    meter = Meter("copy", need)
    added = 0
    since_save = 0
    left_out, dropped = [], set()
    for p in order:
        rep, sha, d = p["rep"], p["sha"], p["date"]
        if p.get("live_photo") in dropped or rep.size + p.get("reserve", 0) > budget:
            dropped.add(sha)                    # out of space: leave for a later run
            left_out.append((rep.src, rep.size, p["date"].isoformat() if p["date"] else ""))
            continue
        budget -= rep.size
        year = f"{d.year:04d}" if d else "undated"
        stem = SAFE.sub("_", rep.stem)
        ext = rep.ext
        ph_sha = p.get("live_photo")
        if ph_sha is not None:
            ph = L.files[ph_sha]
            p["date_source"] = "live-photo" if ph["date"] else None
            date_s = ph["date"]
            prefix = ph["path"].rsplit("/", 1)[1].split("_", 2)
            base = f"{prefix[0]}_{prefix[1]}_{stem}" if ph["date"] else stem
            pyear = volume_year(ph["path"])
            if rep.size > L.large_bytes:
                folder = f"videos/large/{pyear}"
            else:
                folder = L.volume_for("live", pyear, rep.size)
            kind = "live"
        else:
            date_s = (d.isoformat() if p["has_time"] else d.date().isoformat()) if d else None
            base = f"{d:%Y-%m-%d_%H%M%S}_{stem}" if d else stem
            if rep.kind == "video" and rep.size > L.large_bytes:
                folder, kind = f"videos/large/{year}", "large"
            else:
                stream = "photos" if rep.kind == "photo" else "videos"
                folder, kind = L.volume_for(stream, year, rep.size), rep.kind
        rel, adopt = L.unique_name(folder, base, ext, sha)
        dest = rel_to_abs(lib, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if adopt:
            if p["local"] != rep.src:
                os.remove(p["local"])
        elif p["local"] != rep.src:              # staged from a zip: move into place
            os.replace(p["local"], dest)
        else:
            with open(rep.src, "rb") as f, open(dest + ".part", "wb") as out:
                got = hash_stream(f, out)
            if got != sha:
                os.remove(dest + ".part")
                sys.exit(f"hash changed while copying {rep.src}")
            os.replace(dest + ".part", dest)
        entry = dict(path=rel, size=rep.size, kind=kind, date=date_s, date_source=p["date_source"],
                     sources=[i.src for i in p["its"]], added=run_id)
        L.add(rel, sha, entry)
        if ph_sha is not None:
            link_live(L.files, sha, ph_sha)
        counts["added"] += 1
        counts[f"added {kind}"] += 1
        counts[f"date from {p['date_source'] or 'none (undated)'}"] += 1
        added += 1
        meter.add(rep.size)
        since_save += 1
        if since_save >= 100:
            L.save()
            since_save = 0
    L.m["settings"] = dict(volume_mb=args.volume_mb, large_video_mb=args.large_video_mb)
    L.save()
    shutil.rmtree(staging, ignore_errors=True)
    counts["left out for lack of space"] = len(left_out)
    if left_out:
        os.makedirs(meta_path(lib, "runs"), exist_ok=True)
        with open(meta_path(lib, "runs", f"{run_id}-left-out.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source", "size", "date"])
            w.writerows(left_out)

    write_run_log(lib, run_id, "collect", source, args, counts, skipped)
    return counts


def write_run_log(lib, run_id, what, source, args, counts, skipped):
    d = meta_path(lib, "runs")
    os.makedirs(d, exist_ok=True)
    base = os.path.join(d, f"{run_id}-{what}")
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(f"{what} {source}\nsettings: {vars(args)}\n\n")
        for k, v in sorted(counts.items()):
            f.write(f"{k}: {v}\n")
            print(f"{k}: {v}")
    with open(base + "-not-copied.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "size", "reason"])
        w.writerows(skipped)
    print(f"run log: {base}.txt")


# --------------------------------------------------------------------------- retire

def retire(args):
    source, lib = os.path.abspath(args.source), os.path.abspath(args.library)
    check_paths(source, lib)
    check_layout(lib)
    man = load_json(meta_path(lib, MANIFEST), None)
    if man is None:
        sys.exit(f"no manifest in {lib}")
    files = man["files"]
    items = list(walk_source(source, args.exclude, True))
    total = sum(i.size for i in items if i.kind != "zip" and not i.excluded)
    meter, op = Meter("retire", total), Opener()
    lib_ok: dict[str, str] = {}      # sha -> "ok" | reason, from re-reading the library copy

    def check_library(sha: str) -> str:
        if sha not in lib_ok:
            p = rel_to_abs(lib, files[sha]["path"])
            if not os.path.exists(p):
                lib_ok[sha] = "library copy missing"
            else:
                lib_ok[sha] = "ok" if hash_path(p) == sha else "hash mismatch in library copy"
        return lib_ok[sha]

    rows, member_rows = [], []
    # zip path -> [item, media ok, media total, problems, members that keep the zip (reason, name)]
    zip_state: dict[str, list] = {}
    try:
        for it in items:
            if it.excluded:
                if it.zip is None:
                    rows.append(["keep", "excluded", it.src, it.size, "", ""])
                else:                                    # would be lost with the zip
                    member_rows.append(["keep", "excluded member", it.src, it.size, "", ""])
                    zip_state[it.zip][4].append(("excluded", it.member))
                continue
            if it.kind == "zip":
                zip_state[it.src] = [it, 0, 0, [], []]
                continue
            if it.kind not in ("photo", "video"):
                if it.zip is None:
                    rows.append(["keep", "skipped type " + (it.ext or "(none)"), it.src, it.size, "", ""])
                else:                                    # sidecars, nested zips...: not in the library
                    member_rows.append(["keep", "non-media member", it.src, it.size, "", ""])
                    zip_state[it.zip][4].append(("non-media", it.member))
                continue
            with op.open(it) as f:
                sha = hash_stream(f)
            meter.add(it.size)
            if sha not in files:
                action, reason, lp = "keep", "no library copy", ""
            else:
                lp = files[sha]["path"]
                r = check_library(sha)
                action, reason = ("delete", "verified") if r == "ok" else ("keep", r)
            row = [action, reason, it.src, it.size, sha, lp]
            if it.zip is None:
                rows.append(row)
            else:
                member_rows.append(row)
                z = zip_state[it.zip]
                z[2] += 1
                if action == "delete":
                    z[1] += 1
                else:
                    z[3].append(reason)
                    z[4].append((f"unverified: {reason}", it.member))
    finally:
        op.close()
    for zp, (it, ok, tot, probs, keepers) in zip_state.items():
        if keepers:
            # a zip is deleted only if every member is verified media: any other member would be
            # lost with it. The members are listed (all of them in -zip-members.csv).
            by = defaultdict(list)
            for r, m in keepers:
                by[r.split(":")[0]].append(m)
            why = "; ".join(f"{len(ms)} {r} member{'s' if len(ms) > 1 else ''}: {', '.join(ms[:20])}"
                            + (f" and {len(ms) - 20} more" if len(ms) > 20 else "") for r, ms in by.items())
            if probs:
                why += " (" + "; ".join(f"{n} {r}" for r, n in Counter(probs).most_common()) + ")"
            rows.append(["keep", f"zip: members not verified in library: {why}", zp, it.size, "", ""])
        elif tot and ok == tot:
            rows.append(["delete", f"verified zip: all {tot} members in library", zp, it.size, "", ""])
        else:
            rows.append(["keep", "zip with no members", zp, it.size, "", ""])

    run_id = time.strftime("%Y%m%d-%H%M%S")
    d = meta_path(lib, "retire")
    os.makedirs(d, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(source.rstrip("\\/"))).strip("_")
    base = os.path.join(d, f"{run_id}-{tag}")
    hdr = ["action", "reason", "path", "size", "sha256", "library_path"]
    for path, rs in ((base + ".csv", rows), (base + "-zip-members.csv", member_rows)):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(hdr)
            w.writerows(sorted(rs, key=lambda r: (r[0], r[1], r[2])))
    summary = Counter()
    for r in rows:
        key = r[0] if r[0] == "delete" else f"keep: {r[1].split(' (')[0]}"
        if r[1].startswith("zip:"):
            key = "keep: zip with unverified members"
        summary[key + " files"] += 1
        summary[key + " bytes"] += r[3]
    with open(base + "-summary.txt", "w", encoding="utf-8") as f:
        f.write(f"retire {source} ({'DELETE' if args.delete else 'dry run'})\n")
        for k, v in sorted(summary.items()):
            line = f"{k}: {v/1e9:.2f} GB" if k.endswith("bytes") else f"{k}: {v}"
            f.write(line + "\n")
            print(line)
    print(f"list: {base}.csv")

    if args.delete:
        n = 0
        for r in rows:
            if r[0] == "delete":
                os.remove(r[2])
                n += 1
        print(f"deleted {n} files")


# --------------------------------------------------------------------------- pack

def zips_dir_for(lib: str) -> str:
    """E:\\Kids Photos -> E:\\Kids Photos Zips: a sibling of the library, never inside it."""
    return os.path.abspath(lib).rstrip("\\/") + " Zips"


def zip_name(vol: str) -> str:
    """photos/2023-2 -> photos-2023-2.zip"""
    return vol.replace("/", "-") + ".zip"


def library_volumes(files: dict) -> dict[str, dict[str, str]]:
    """volume ('photos/2023') -> {file name: sha}, from the manifest."""
    vols: dict[str, dict[str, str]] = defaultdict(dict)
    for sha, e in files.items():
        parts = e["path"].split("/")
        if len(parts) == 3 and parts[0] in STREAMS and VOLUME.fullmatch(parts[1]):
            vols[f"{parts[0]}/{parts[1]}"][parts[2]] = sha
    return vols


def build_zip(lib: str, vol: str, names: list[str], dest: str):
    """Store-mode zip of the volume folder's files, paths relative to the folder."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as zf:
        for n in names:
            zf.write(rel_to_abs(lib, f"{vol}/{n}"), arcname=n)


def verify_zip(lib: str, vol: str, path: str, want: dict[str, str], files: dict) -> list[str]:
    """Read the zip back in full. Problems found (empty: verified): member list equal to the
    folder's files and to the manifest, every member's CRC, size and SHA-256 as the manifest has."""
    probs = []
    on_disk = {n for n in os.listdir(rel_to_abs(lib, vol)) if os.path.isfile(rel_to_abs(lib, f"{vol}/{n}"))}
    if on_disk != set(want):
        probs.append(f"folder and manifest differ: {len(on_disk - set(want))} files only in the folder, "
                     f"{len(set(want) - on_disk)} only in the manifest")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            names = [zi.filename for zi in infos]
            if len(names) != len(set(names)):
                probs.append("duplicate member names")
            if set(names) != on_disk:
                probs.append(f"members differ from the folder: {len(set(names) - on_disk)} extra, "
                             f"{len(on_disk - set(names))} missing")
            for zi in infos:
                sha = want.get(zi.filename)
                try:
                    with zf.open(zi) as f:             # zipfile checks the CRC at the end of the read
                        got = hash_stream(f)
                except (zipfile.BadZipFile, OSError) as e:
                    probs.append(f"{zi.filename}: {e}")
                    continue
                if sha is None:
                    continue                           # already reported as extra
                if zi.file_size != files[sha]["size"]:
                    probs.append(f"{zi.filename}: size {zi.file_size} != manifest {files[sha]['size']}")
                if got != sha:
                    probs.append(f"{zi.filename}: SHA-256 differs from the manifest")
    except (zipfile.BadZipFile, OSError) as e:
        probs.append(f"unreadable zip: {e}")
    return probs


def pack(args):
    """Zip and seal every volume; write the upload list. Resumable: a re-run seals nothing twice
    and rebuilds only missing or failed zips."""
    lib = os.path.abspath(args.library)
    check_layout(lib)
    man_path = meta_path(lib, MANIFEST)
    if not os.path.exists(man_path):
        sys.exit(f"no {META}/{MANIFEST} in {lib}: not a library")
    zdir = os.path.abspath(args.zips) if args.zips else zips_dir_for(lib)
    check_paths(zdir, lib)
    L = Library(lib, 2000, 100)                         # sizes unused: pack adds nothing
    files, man = L.files, L.m
    if any(clips_to_move(files)):
        sys.exit(f"{lib} still has Live Photo clips in photos/: run migrate --apply first")
    vrecs: dict = man.setdefault("volumes", {})
    vols = library_volumes(files)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    t0 = time.time()

    todo = []
    for vol in sorted(vols):
        rec = vrecs.get(vol, {})
        zp = os.path.join(zdir, zip_name(vol))
        if rec.get("sealed"):
            if os.path.exists(zp) and os.path.getsize(zp) == rec.get("zip_size"):
                continue                                # sealed, zip in place
            log.info("%s is sealed but its zip is missing or the wrong size: rebuilding", vol)
        todo.append(vol)
    total = sum(files[s]["size"] for v in todo for s in vols[v].values())
    log.info("%d volumes, %d to zip (%.1f GB) into %s", len(vols), len(todo), total / 1e9, zdir)
    if args.dry_run:
        for v in todo:
            print(f"{zip_name(v)}: {len(vols[v])} files, {sum(files[s]['size'] for s in vols[v].values()) / 1e6:.0f} MB")
    if not args.dry_run and todo:
        need = total * 1.01 + 2e9
        free = shutil.disk_usage(os.path.dirname(zdir)).free
        if need > free:
            sys.exit(f"not enough space for the zips: {need / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free")
        os.makedirs(zdir, exist_ok=True)

    created, failed = [], []
    meter = Meter("pack", total)
    for vol in [] if args.dry_run else todo:
        want = vols[vol]
        name = zip_name(vol)
        zp = os.path.join(zdir, name)
        tmp = zp + ".part"
        if os.path.exists(tmp):
            os.remove(tmp)                              # left by a stopped run
        names = sorted(want, key=str.lower)
        try:
            build_zip(lib, vol, names, tmp)
            probs = verify_zip(lib, vol, tmp, want, files)
        except (OSError, ValueError, zipfile.LargeZipFile) as e:
            probs = [f"could not build: {e}"]
        vbytes = sum(files[s]["size"] for s in want.values())
        meter.add(vbytes)
        if probs:
            if os.path.exists(tmp):
                os.remove(tmp)
            log.warning("%s FAILED verification, left unsealed: %s", vol, "; ".join(probs[:5]))
            failed.append((vol, probs))
            rec = vrecs.setdefault(vol, {})
            rec.update(last_failure=run_id, failure="; ".join(probs[:20]))
            L.save()
            continue
        os.replace(tmp, zp)
        rec = vrecs.get(vol, {})
        rec.pop("failure", None)
        rec.pop("last_failure", None)
        rec.update(zip=name, zip_size=os.path.getsize(zp), files=len(want), bytes=vbytes,
                   zipped=run_id, sealed=rec.get("sealed") or run_id)
        vrecs[vol] = rec
        L.save()                                        # sealed as soon as its zip is in place
        created.append((vol, zp, rec["zip_size"]))
        log.info("%s: %d files, sealed (%s)", vol, len(want), name)

    # upload list: this run's zips, plus large videos not listed by an earlier run
    large = sorted((e["path"], sha) for sha, e in files.items()
                   if e["path"].startswith("videos/large/") and not e.get("upload_listed"))
    unsealed = [v for v in sorted(vols) if not vrecs.get(v, {}).get("sealed")]
    print(f"zips created: {len(created)} ({sum(c[2] for c in created) / 1e9:.2f} GB); "
          f"failed: {len(failed)}; volumes still unsealed: {len(unsealed)}")
    for v, p in failed:
        print(f"  FAILED {v}: {'; '.join(p[:5])}")
    print(f"large videos not yet listed: {len(large)} ({sum(files[s]['size'] for _, s in large) / 1e9:.2f} GB)")
    if args.dry_run:
        print("dry run: nothing zipped, sealed or listed")
        return
    if not created and not failed and not large:
        print("nothing to do: every volume sealed with its zip in place, every large video listed")
        return
    d = meta_path(lib, "uploads")
    os.makedirs(d, exist_ok=True)
    up = os.path.join(d, f"{run_id}-upload.csv")
    with open(up, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "path", "size", "sha256"])
        for vol, zp, size in created:
            w.writerow(["zip", zp, size, ""])
        for rel, sha in large:
            w.writerow(["large video", rel_to_abs(lib, rel), files[sha]["size"], sha])
    for _, sha in large:
        files[sha]["upload_listed"] = run_id
    L.save()
    secs = time.time() - t0
    os.makedirs(meta_path(lib, "runs"), exist_ok=True)
    with open(meta_path(lib, "runs", f"{run_id}-pack.txt"), "w", encoding="utf-8") as f:
        f.write(f"pack {lib} -> {zdir}\n")
        f.write(f"time: {secs / 60:.1f} min\nvolumes: {len(vols)}\n")
        f.write(f"zips created: {len(created)} ({sum(c[2] for c in created) / 1e9:.2f} GB)\n")
        f.write("".join(f"  {os.path.basename(zp)}: {size / 1e6:.0f} MB\n" for _, zp, size in created))
        f.write(f"large videos listed: {len(large)} ({sum(files[s]['size'] for _, s in large) / 1e9:.2f} GB)\n")
        f.write(f"upload list: {up}\n")
        f.write(f"failed verification (left unsealed): {len(failed)}\n")
        f.write("".join(f"  {v}: {'; '.join(p[:5])}\n" for v, p in failed))
        f.write(f"unsealed volumes: {', '.join(unsealed) or 'none'}\n")
    print(f"upload list: {up} ({len(created)} zips, {len(large)} large videos); {secs / 60:.1f} min")


# --------------------------------------------------------------------------- migrate

OLD_VOLUME_PATH = re.compile(r"(photos|videos)/(\d{4}|undated)/v(\d+)/([^/]+)")


def new_volume_path(rel: str) -> str:
    """Old layout path -> new: photos/2023/v01/x -> photos/2023/x, photos/2023/v02/x -> photos/2023-2/x.
    Anything else (videos/large/...) is unchanged."""
    m = OLD_VOLUME_PATH.fullmatch(rel)
    if not m:
        return rel
    stream, year, n, name = m.groups()
    return f"{volume_folder(stream, year, int(n))}/{name}"


def migrate(args):
    """Bring a library up to the current layout: first vNN folders -> year folders (if needed),
    then Live Photo clips photos/ -> live/. Moves only; a dry run unless --apply."""
    lib = os.path.abspath(args.library)
    if os.path.exists(os.path.join(lib, MANIFEST)):
        migrate_old_layout(args)
        if not args.apply:
            print("after that, migrate moves Live Photo clips from photos/ to live/ (re-run to see the plan)")
            return
    migrate_live(lib, args.apply)


def clips_to_move(files: dict):
    """Live Photo clips still in photos/ (where collects before live/ put them)."""
    return ((sha, e) for sha, e in files.items() if e["kind"] == "live" and e["path"].startswith("photos/"))


def migrate_live(lib: str, apply: bool):
    """Move Live Photo clips from photos/ to live/<year>[-N]/ (year and order from the paired
    photo), record each photo's clips, rewrite the manifest. Renames only; resumable."""
    new_man = meta_path(lib, MANIFEST)
    if not os.path.exists(new_man):
        sys.exit(f"no {META}/{MANIFEST} in {lib}: not a library")
    settings = load_json(new_man, {}).get("settings") or {}
    L = Library(lib, settings.get("volume_mb", 2000), settings.get("large_video_mb", 100))
    files = L.files
    # pairing both ways, for every clip (also any already in live/ or videos/large/)
    missing = [sha for sha, e in files.items() if e["kind"] == "live" and e.get("live_photo_of") not in files]
    if missing:
        sys.exit(f"{len(missing)} Live Photo clips whose photo is not in the manifest, e.g. {missing[:3]}")
    links = 0
    for sha, e in files.items():
        if e["kind"] == "live" and sha not in files[e["live_photo_of"]].get("live_clips", []):
            link_live(files, sha, e["live_photo_of"])
            links += 1

    todo = sorted(clips_to_move(files), key=lambda se: (
        volume_year(files[se[1]["live_photo_of"]]["path"]) == "undated",
        files[se[1]["live_photo_of"]]["path"].rsplit("/", 1)[1].lower(),   # date-prefixed name
        se[1]["path"].lower()))
    moves, problems, done = [], [], 0
    for sha, e in todo:
        old = e["path"]
        name = old.rsplit("/", 1)[1]
        year = volume_year(files[e["live_photo_of"]]["path"])
        new = f"{L.volume_for('live', year, e['size'])}/{name}"
        if new.lower() in L.taken:
            problems.append(f"{old}: {new} is taken in the manifest")
        po, pn = rel_to_abs(lib, old), rel_to_abs(lib, new)
        if os.path.exists(po):
            if os.path.exists(pn):
                problems.append(f"{old}: {new} already exists")
        elif os.path.exists(pn) and os.path.getsize(pn) == e["size"]:
            done += 1                                   # moved by a stopped run
        else:
            problems.append(f"{old}: missing on disk")
        L._note(new, e["size"])
        moves.append((sha, old, new))
    if problems:
        sys.exit("not migrating:\n  " + "\n  ".join(problems[:50]))

    vols = Counter(new.rsplit("/", 1)[0] for _, _, new in moves)
    for v, n in sorted(vols.items()):
        mb = sum(files[s]["size"] for s, _, new in moves if new.rsplit("/", 1)[0] == v) / 1e6
        print(f"{v}: {n} clips, {mb:.0f} MB")
    print(f"{len(moves)} Live Photo clips to move from photos/ to live/ ({done} already moved by a stopped run); "
          f"{links} photos to get their live_clips")
    if not moves and not links:
        print("nothing to do")
        return
    if not apply:
        print("dry run: nothing moved (add --apply)")
        return

    run_id = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(meta_path(lib, "runs"), exist_ok=True)
    kept = meta_path(lib, "runs", f"{run_id}-migrate-live-old-manifest.json")
    shutil.copyfile(new_man, kept)                       # the manifest only; media are renamed
    t0 = time.time()
    for sha, old, new in moves:
        po, pn = rel_to_abs(lib, old), rel_to_abs(lib, new)
        if os.path.exists(po):
            os.makedirs(os.path.dirname(pn), exist_ok=True)
            os.rename(po, pn)                            # same drive: a rename, never a copy
    for sha, old, new in moves:
        p = rel_to_abs(lib, new)
        if not os.path.exists(p) or os.path.getsize(p) != files[sha]["size"]:
            sys.exit(f"after the moves, {new} is missing or the wrong size; the manifest is "
                     f"untouched, so a re-run resumes")
        files[sha]["path"] = new
    L.save()
    with open(meta_path(lib, "runs", f"{run_id}-migrate-live.txt"), "w", encoding="utf-8") as f:
        f.write(f"migrate {lib}: Live Photo clips photos/ -> live/\n")
        f.write(f"clips moved: {len(moves)} ({done} by an earlier stopped run)\n")
        f.write("".join(f"  {v}: {n}\n" for v, n in sorted(vols.items())))
        f.write(f"photos given live_clips: {links}\n")
        f.write(f"old manifest kept as {os.path.basename(kept)}\n")
    print(f"migrated in {time.time() - t0:.1f} s: {len(moves)} clips now in live/")


def migrate_old_layout(args):
    """Old layout (vNN folders; manifest, .cache, runs, retire at the base) -> new layout.
    Moves only, nothing recopied; resumable: a re-run after a stop finishes the job."""
    lib = os.path.abspath(args.library)
    old_man, new_man = os.path.join(lib, MANIFEST), meta_path(lib, MANIFEST)
    if not os.path.exists(old_man):
        sys.exit(f"no {MANIFEST} at the base of {lib}: already migrated, or not a library")
    if os.path.exists(new_man):
        sys.exit(f"both {old_man} and {new_man} exist: look before migrating")
    # other base entries (face_labels.zip, ...) are left alone; only a stopped run's .staging stops it
    if os.path.exists(os.path.join(lib, ".staging")):
        sys.exit(f"{os.path.join(lib, '.staging')} exists (left by a stopped collect?): look before migrating")

    # media moves, planned from the disk
    moves, empty_dirs, problems = [], [], []
    for stream in OLD_STREAMS:
        sroot = os.path.join(lib, stream)
        if not os.path.isdir(sroot):
            continue
        for year in sorted(os.listdir(sroot)):
            ydir = os.path.join(sroot, year)
            if stream == "videos" and year == "large" or not os.path.isdir(ydir):
                continue
            if VOLUME.fullmatch(year) and "-" in year:
                continue                     # a new-style later volume (resumed run)
            if not VOLUME.fullmatch(year):
                problems.append(f"{stream}/{year}: not a year folder")
                continue
            for v in sorted(os.listdir(ydir)):
                vdir = os.path.join(ydir, v)
                if not os.path.isdir(vdir):
                    continue                 # a file already moved up out of v01
                if not re.fullmatch(r"v\d+", v):
                    problems.append(f"{stream}/{year}/{v}: not a vNN folder")
                    continue
                for name in sorted(os.listdir(vdir)):
                    old = f"{stream}/{year}/{v}/{name}"
                    if os.path.isdir(rel_to_abs(lib, old)):
                        problems.append(f"{old}: a folder inside a volume")
                        continue
                    new = new_volume_path(old)
                    if os.path.exists(rel_to_abs(lib, new)):
                        problems.append(f"{old}: {new} already exists")
                    moves.append((old, new))
                empty_dirs.append(vdir)
    if problems:
        sys.exit("not migrating:\n  " + "\n  ".join(problems[:50]))

    man = load_json(old_man, None)
    files = man["files"]
    remap = {e["path"]: new_volume_path(e["path"]) for e in files.values()}
    moved_to = dict(moves)
    in_manifest = sum(1 for o in moves if o[0] in remap)
    meta_moves = [(os.path.join(lib, ".cache", HASH_CACHE), meta_path(lib, HASH_CACHE)),
                  (os.path.join(lib, "runs"), meta_path(lib, "runs")),
                  (os.path.join(lib, "retire"), meta_path(lib, "retire"))]
    meta_moves = [(a, b) for a, b in meta_moves if os.path.exists(a)]
    for a, b in meta_moves:
        if os.path.exists(b):
            sys.exit(f"{b} already exists")
    cache_dir = os.path.join(lib, ".cache")
    if os.path.isdir(cache_dir) and set(os.listdir(cache_dir)) - {HASH_CACHE}:
        sys.exit(f"{cache_dir} holds more than {HASH_CACHE}: look before migrating")
    vols = Counter(new.rsplit("/", 1)[0] for _, new in moves)
    print(f"{len(moves)} media files to move ({in_manifest} in the manifest) into {len(vols)} volume folders")
    print(f"{len(remap)} manifest entries, {sum(1 for a, b in remap.items() if a != b)} with a new path")
    not_on_disk = [a for a, b in remap.items() if a not in moved_to
                   and not os.path.exists(rel_to_abs(lib, b))]     # b == a, or moved by a stopped run
    if not_on_disk:
        sys.exit(f"{len(not_on_disk)} manifest paths missing on disk, e.g. {not_on_disk[:3]}")
    for a, b in meta_moves:
        print(f"move {os.path.relpath(a, lib)} -> {os.path.relpath(b, lib)}")
    if not args.apply:
        print("dry run: nothing moved (add --apply)")
        return

    run_id = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(meta_path(lib), exist_ok=True)
    for a, b in meta_moves:
        os.replace(a, b)
    if os.path.isdir(cache_dir):
        os.rmdir(cache_dir)
    for old, new in moves:
        dest = rel_to_abs(lib, new)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(rel_to_abs(lib, old), dest)  # same drive: a rename, never a copy
    for d in empty_dirs:
        os.rmdir(d)
    for sha, e in files.items():
        e["path"] = remap[e["path"]]
        p = rel_to_abs(lib, e["path"])
        if not os.path.exists(p) or os.path.getsize(p) != e["size"]:
            sys.exit(f"after the moves, {e['path']} is missing or the wrong size; "
                     f"{old_man} is untouched, so a re-run resumes")
    man["version"] = 2
    save_json(new_man, man)
    os.makedirs(meta_path(lib, "runs"), exist_ok=True)
    os.replace(old_man, meta_path(lib, "runs", f"{run_id}-migrate-old-manifest.json"))
    with open(meta_path(lib, "runs", f"{run_id}-migrate.txt"), "w", encoding="utf-8") as f:
        f.write(f"migrate {lib}: vNN folders -> year folders, metadata -> {META}/\n")
        f.write(f"media files moved: {len(moves)}\nvolume folders: {len(vols)}\n")
        f.write(f"manifest entries: {len(files)}\nold manifest kept as {run_id}-migrate-old-manifest.json\n")
    print(f"migrated: base now holds {sorted(os.listdir(lib))}")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "retire"):
        p = sub.add_parser(name)
        p.add_argument("source")
        p.add_argument("--library", required=True)
        p.add_argument("--exclude", action="append", default=[],
                       help="glob, relative to the source, of files or folders to leave out (repeatable)")
        if name == "collect":
            p.add_argument("--volume-mb", type=int, default=2000,
                           help="volume fill limit in MB (10^6 bytes); keep under 2147 (2 GiB)")
            p.add_argument("--large-video-mb", type=int, default=100)
            p.add_argument("--no-zips", action="store_true", help="do not read media inside .zip files")
            p.add_argument("--live-max-seconds", type=float, default=4,
                           help="a same-stem video longer than this is not a Live Photo")
            p.add_argument("--reserve-gb", type=float, default=5,
                           help="free space to leave on the library drive; past it, newest files wait")
        else:
            p.add_argument("--delete", action="store_true",
                           help="actually delete the verified files (default: dry run)")
    p = sub.add_parser("pack", help="zip and seal every volume into <library> Zips; write the upload list")
    p.add_argument("--library", required=True)
    p.add_argument("--zips", help="folder for the zips (default: '<library> Zips', beside the library)")
    p.add_argument("--dry-run", action="store_true", help="show what would be zipped; change nothing")
    p = sub.add_parser("migrate", help="bring a library to this layout: vNN folders -> year folders, "
                                       "Live Photo clips photos/ -> live/")
    p.add_argument("--library", required=True)
    p.add_argument("--apply", action="store_true", help="actually move (default: dry run)")
    args = ap.parse_args()
    if args.cmd == "collect" and not 0 < args.volume_mb < 2147:
        sys.exit("--volume-mb must stay under 2147 (2 GiB) so a volume's zip never needs Zip64")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%I:%M:%S%p")
    {"collect": collect, "retire": retire, "pack": pack, "migrate": migrate}[args.cmd](args)


if __name__ == "__main__":
    main()
