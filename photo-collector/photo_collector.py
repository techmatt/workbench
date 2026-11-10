# /// script
# requires-python = ">=3.11"
# dependencies = ["exifread>=3.0"]
# ///
"""photo-collector: gather photos and videos from download folders into one library.

    uv run photo_collector.py collect <source> --library <dir> [options]
    uv run photo_collector.py retire  <source> --library <dir> [--delete] [options]

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
MANIFEST = "manifest.json"
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
    rel = rel.lower()
    parts = rel.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
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
                        yield Item(src=f"{p}!{zi.filename}", rel=f"{rel}!{zi.filename}",
                                   folder=f"{p}!{mdir}", name=mname, size=zi.file_size,
                                   stamp=f"{st.st_mtime_ns}:{zi.CRC}", zip=p, member=zi.filename,
                                   kind=classify(mname))


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
    cache_path = os.path.join(lib, ".cache", "hashes.json")
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
        self.path = os.path.join(lib, MANIFEST)
        self.m = load_json(self.path, {"version": 1, "files": {}})
        self.files: dict = self.m["files"]
        self.volume_bytes = volume_mb * 1_000_000
        self.large_bytes = large_mb * 1_000_000
        self.vols: dict[tuple[str, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.taken: set[str] = set()
        for e in self.files.values():
            self._note(e["path"], e["size"])

    def _note(self, rel: str, size: int):
        self.taken.add(rel.lower())
        parts = rel.split("/")
        if len(parts) == 4 and re.fullmatch(r"v\d+", parts[2]):
            self.vols[(parts[0], parts[1])][int(parts[2][1:])] += size

    def save(self):
        save_json(self.path, self.m)

    def volume_for(self, stream: str, year: str, size: int) -> str:
        v = self.vols[(stream, year)]
        cur = max(v) if v else 1
        if v.get(cur, 0) and v[cur] + size > self.volume_bytes:
            cur += 1
        v.setdefault(cur, 0)
        return f"{stream}/{year}/v{cur:02d}"

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
    os.makedirs(lib, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    items = scan_and_hash(source, lib, args.exclude, not args.no_zips)
    L = Library(lib, args.volume_mb, args.large_video_mb)
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
    staging = os.path.join(lib, ".staging")
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

    # Place: chronological, Live Photo videos straight after their photo
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
        # the photo's volume must also hold its Live Photo videos
        p["reserve"] = sum(v["rep"].size for v in lv if v["rep"].size <= L.large_bytes)

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
            folder = ph["path"].rsplit("/", 1)[0]
            if rep.size > L.large_bytes:
                pyear = ph["path"].split("/")[1]
                folder = f"videos/large/{pyear}"
            kind = "live"
        else:
            date_s = (d.isoformat() if p["has_time"] else d.date().isoformat()) if d else None
            base = f"{d:%Y-%m-%d_%H%M%S}_{stem}" if d else stem
            if rep.kind == "video" and rep.size > L.large_bytes:
                folder, kind = f"videos/large/{year}", "large"
            else:
                stream = "photos" if rep.kind == "photo" else "videos"
                folder, kind = L.volume_for(stream, year, rep.size + p.get("reserve", 0)), rep.kind
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
        if ph_sha is not None:
            entry["live_photo_of"] = ph_sha
        L.add(rel, sha, entry)
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
        os.makedirs(os.path.join(lib, "runs"), exist_ok=True)
        with open(os.path.join(lib, "runs", f"{run_id}-left-out.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source", "size", "date"])
            w.writerows(left_out)

    write_run_log(lib, run_id, "collect", source, args, counts, skipped)
    return counts


def write_run_log(lib, run_id, what, source, args, counts, skipped):
    d = os.path.join(lib, "runs")
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
    man = load_json(os.path.join(lib, MANIFEST), None)
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
    zip_state: dict[str, list] = {}
    try:
        for it in items:
            if it.excluded:
                if it.zip is None:
                    rows.append(["keep", "excluded", it.src, it.size, "", ""])
                continue
            if it.kind == "zip":
                zip_state[it.src] = [it, 0, 0, []]       # item, media ok, media total, problems
                continue
            if it.kind not in ("photo", "video"):
                if it.zip is None:
                    rows.append(["keep", "skipped type " + (it.ext or "(none)"), it.src, it.size, "", ""])
                else:
                    member_rows.append(["ignored", "non-media member", it.src, it.size, "", ""])
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
    finally:
        op.close()
    for zp, (it, ok, tot, probs) in zip_state.items():
        if tot and ok == tot:
            rows.append(["delete", f"verified zip: all {tot} media members in library", zp, it.size, "", ""])
        elif not tot:
            rows.append(["keep", "zip with no media members", zp, it.size, "", ""])
        else:
            why = "; ".join(f"{n} {r}" for r, n in Counter(probs).most_common())
            rows.append(["keep", f"zip: {tot - ok} of {tot} media members not verified ({why})", zp, it.size, "", ""])

    run_id = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(lib, "retire")
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
    args = ap.parse_args()
    if args.cmd == "collect" and not 0 < args.volume_mb < 2147:
        sys.exit("--volume-mb must stay under 2147 (2 GiB) so a volume's zip never needs Zip64")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%I:%M:%S%p")
    (collect if args.cmd == "collect" else retire)(args)


if __name__ == "__main__":
    main()
