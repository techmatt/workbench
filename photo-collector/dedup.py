# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10", "pillow-heif>=0.16", "numpy>=1.26"]
# ///
"""photo-collector dedup: find near-duplicates in a library and set the lesser copies aside.

    uv run dedup.py scan  --library <dir> [--workers 3]
    uv run dedup.py plan  --library <dir>
    uv run dedup.py apply --library <dir> --plan <plan.csv> [--aside <dir>] [--unseal]
    uv run dedup.py undo  --library <dir> --record <removed.csv>

The library holds one file per exact byte content already (collect's rule), so what is left
is near-duplicates: one shot at different bytes. scan reads every photo and video once
(features cached in _metadata/dedup/features.json by hash). plan groups confident matches,
picks a keeper per group (resolution, then a Live clip / embedded motion, then metadata,
then size) and writes a CSV to review. apply moves each lesser copy out of the library to
<library> Duplicates\\<run>\\<its library path>, records it (<run>-removed.csv), and moves
its manifest entry from `files` to `set_aside`, so collect never copies those bytes again.
undo moves them back. See README.md.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import struct
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

META = "_metadata"
DEDUP = "dedup"
FEATURES_VERSION = 2


def log(*a):
    print(time.strftime("%I:%M:%S%p").lstrip("0").lower(), *a, flush=True)


def rel_to_abs(lib, rel):
    return os.path.join(lib, *rel.split("/"))


def meta(lib, *parts):
    return os.path.join(lib, META, *parts)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data, sort=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=sort)
    os.replace(path + ".tmp", path)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------- features

def _open_image(path):
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    return Image.open(path)


def photo_features(path: str) -> dict:
    """Size as shown (EXIF orientation applied), capture date, camera, how much metadata,
    a 32x32 grey thumbnail (hex) and whether a motion clip is embedded."""
    from PIL import ImageOps
    im = _open_image(path)
    exif = im.getexif()
    ifd = exif.get_ifd(0x8769)
    gps = exif.get_ifd(0x8825)
    w, h = im.size
    fmt = im.format
    orient = exif.get(0x0112, 1)
    if im.format in ("JPEG", "PNG", "WEBP", "TIFF") and orient in (5, 6, 7, 8):
        w, h = h, w
    if im.format == "JPEG":
        im.draft("RGB", (256, 256))
    try:
        im = ImageOps.exif_transpose(im) if im.format != "HEIF" else im
    except Exception:   # noqa: BLE001 - a broken orientation tag: use as stored
        pass
    g = im.convert("L").resize((32, 32), 4)   # BOX
    with open(path, "rb") as f:
        head = f.read(512 * 1024)
    s = lambda v: str(v).strip("\x00 ").strip() if v is not None else None  # noqa: E731
    return {
        "w": w, "h": h, "format": fmt,
        "dto": s(ifd.get(36867)) or s(exif.get(306)),
        "dto_tag": "original" if ifd.get(36867) else ("datetime" if exif.get(306) else None),
        "subsec": s(ifd.get(37521)),
        "make": s(exif.get(271)), "model": s(exif.get(272)),
        "tags": len(exif) + len(ifd) + len(gps), "gps": bool(gps),
        "motion": b"MotionPhoto" in head or b"MicroVideo" in head,
        "thumb": g.tobytes().hex(),
    }


def _atoms(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        body = pos + 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
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


def video_features(path: str) -> dict:
    """Duration (mvhd) and the largest track size (tkhd), as stored (rotation not applied)."""
    out = {"seconds": None, "w": None, "h": None}
    with open(path, "rb") as f:
        end = os.fstat(f.fileno()).st_size
        moov = _child(f, 0, end, b"moov")
        if not moov:
            return out
        mvhd = _child(f, *moov, b"mvhd")
        if mvhd:
            f.seek(mvhd[0])
            ver = f.read(4)[0]
            if ver == 1:
                f.read(16)
                scale, dur = struct.unpack(">IQ", f.read(12))
            else:
                f.read(8)
                scale, dur = struct.unpack(">II", f.read(8))
            out["seconds"] = round(dur / scale, 3) if scale else None
        for t, b, e in _atoms(f, *moov):
            if t != b"trak":
                continue
            tkhd = _child(f, b, e, b"tkhd")
            if not tkhd:
                continue
            f.seek(tkhd[1] - 8)
            tw, th = struct.unpack(">II", f.read(8))
            tw, th = tw >> 16, th >> 16
            if tw * th > (out["w"] or 0) * (out["h"] or 0):
                out["w"], out["h"] = tw, th
    return out


def features_for(args):
    sha, path, kind = args
    try:
        f = photo_features(path) if kind == "photo" else video_features(path)
        return sha, dict(f, v=FEATURES_VERSION)
    except Exception as e:   # noqa: BLE001
        return sha, {"error": f"{type(e).__name__}: {e}"[:200], "v": FEATURES_VERSION}


def lowprio():
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    except Exception:   # noqa: BLE001 - not Windows
        try:
            os.nice(10)
        except Exception:   # noqa: BLE001
            pass


def cmd_scan(a):
    lib = os.path.abspath(a.library)
    files = load_json(meta(lib, "manifest.json"), None)["files"]
    fp = meta(lib, DEDUP, "features.json")
    feats = load_json(fp, {})
    todo = [(sha, rel_to_abs(lib, e["path"]), "photo" if e["kind"] == "photo" else "video")
            for sha, e in files.items()
            if feats.get(sha, {}).get("v") != FEATURES_VERSION]
    log(f"{len(files)} library files; {len(todo)} to read ({len(files) - len(todo)} cached)")
    lowprio()
    done = 0
    with ProcessPoolExecutor(a.workers, initializer=lowprio) as ex:
        for sha, f in ex.map(features_for, todo, chunksize=8):
            feats[sha] = f
            done += 1
            if done % 500 == 0:
                log(f"  {done}/{len(todo)}")
                save_json(fp, feats, sort=False)
    save_json(fp, feats, sort=False)
    errs = [(s, f["error"]) for s, f in feats.items() if "error" in f and s in files]
    log(f"features for {len(files)} files; {len(errs)} unreadable")
    for s, e in errs[:20]:
        log(f"  {files[s]['path']}: {e}")


# --------------------------------------------------------------------------- matching

def thumb(f):
    import numpy as np
    return np.frombuffer(bytes.fromhex(f["thumb"]), dtype=np.uint8).reshape(32, 32).astype(np.int16)


def mad(a, b):
    """Mean absolute difference of two 32x32 thumbnails, brightness-matched (0-255)."""
    a = a - a.mean()
    b = b - b.mean()
    return float(abs(a - b).mean())


def dhash(t) -> int:
    import numpy as np
    from PIL import Image
    small = np.asarray(Image.fromarray(t.astype(np.uint8)).resize((9, 8), 4), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if x else "0" for x in bits), 2)


def stems(e: dict) -> set[str]:
    """Original file stems this content was seen under (source paths), folded; plus the
    library name's stem without its date prefix and hash suffix."""
    out = set()
    for s in e.get("sources", []):
        n = re.split(r"[\\/!]", s)[-1]
        out.add(os.path.splitext(n)[0].lower())
    n = e["path"].rsplit("/", 1)[-1]
    n = os.path.splitext(n)[0]
    n = re.sub(r"^\d{4}-\d{2}-\d{2}_\d{6}_", "", n)
    n = re.sub(r"_[0-9a-f]{8}$", "", n)
    out.add(n.lower())
    out = {re.sub(r"\(\d+\)$", "", s).strip() for s in out}
    out = {re.sub(r"[-_ ]?(edited|copy)$", "", s) for s in out}
    return {s for s in out if s}


def full_pixels_equal(p1, p2) -> float:
    """Max per-channel difference of the full decoded images (orientation applied)."""
    import numpy as np
    from PIL import ImageOps
    arrs = []
    for p in (p1, p2):
        im = _open_image(p)
        if im.format != "HEIF":
            im = ImageOps.exif_transpose(im)
        arrs.append(np.asarray(im.convert("RGB"), dtype=np.int16))
    if arrs[0].shape != arrs[1].shape:
        return 999.0
    return float(abs(arrs[0] - arrs[1]).max())


def aspect_ok(f1, f2, tol=0.015):
    r1, r2 = f1["w"] / f1["h"], f2["w"] / f2["h"]
    return abs(r1 - r2) / max(r1, r2) <= tol


def rank_key(sha, e, f, files):
    """Higher is better: resolution, a Live clip or embedded motion, metadata, then bytes."""
    has_motion = bool(e.get("live_clips")) or bool(f.get("motion"))
    return ((f.get("w") or 0) * (f.get("h") or 0), has_motion, bool(f.get("dto")), bool(f.get("gps")),
            f.get("tags") or 0, e["size"], -ord(e.get("added", "z")[0]), sha)


def pair_verdict(e1, f1, e2, f2, T):
    """(class, evidence) for a confident near-duplicate pair, or (None, why not)."""
    m = mad(thumb(f1), thumb(f2))
    if not aspect_ok(f1, f2):
        return None, f"aspect differs (mad {m:.1f})"
    same_dims = (f1["w"], f1["h"]) == (f2["w"], f2["h"])
    d1, d2 = f1.get("dto"), f2.get("dto")
    s1, s2 = f1.get("subsec"), f2.get("subsec")
    same_date = bool(d1 and d1 == d2)
    sub_conflict = bool(s1 and s2 and s1 != s2)
    same_stem = bool(stems(e1) & stems(e2))
    ev = f"mad {m:.2f}; dims {f1['w']}x{f1['h']} / {f2['w']}x{f2['h']}; date {d1}|{d2} sub {s1}|{s2}; stem {'same' if same_stem else 'differs'}"
    if (d1 and d2 and d1 != d2):
        return None, "capture dates differ; " + ev
    if sub_conflict:
        return None, "sub-second capture times differ (burst); " + ev
    if same_date and same_stem and m <= T["strict"]:
        return ("resized" if not same_dims else "re-encoded"), ev
    if same_date and m <= T["tight"] and (s1 and s2 or same_stem):
        return ("resized" if not same_dims else "re-encoded"), ev
    if not (d1 and d2) and same_stem and m <= T["tight"]:
        return ("resized" if not same_dims else "re-encoded"), ev
    # a metadata-less copy (PNG export, messenger copy) of a dated camera photo, renamed: only
    # when near pixel-identical (bursts here differ by 3 or more; two undated screenshots
    # never match this way)
    if bool(d1) != bool(d2) and m <= T["copy"]:
        return ("resized" if not same_dims else "re-encoded"), ev
    return None, "not confident; " + ev


def find_photo_pairs(files, feats):
    """Candidate pairs: same capture date, or close dHash. Returns {(a, b): mad}."""
    photos = {s: feats[s] for s, e in files.items()
              if e["kind"] == "photo" and s in feats and "error" not in feats[s] and feats[s].get("w")}
    cands = set()
    by_date = defaultdict(list)
    for s, f in photos.items():
        if f.get("dto"):
            by_date[f["dto"]].append(s)
    for ss in by_date.values():
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                cands.add(tuple(sorted((ss[i], ss[j]))))
    dh = {s: dhash(thumb(f)) for s, f in photos.items()}
    buckets = defaultdict(list)
    for s, h in dh.items():
        for k in range(4):
            buckets[(k, (h >> (16 * k)) & 0xFFFF)].append(s)
    for ss in buckets.values():
        if len(ss) > 200:
            continue                     # a flat image (all-black): never confident anyway
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                if bin(dh[ss[i]] ^ dh[ss[j]]).count("1") <= 8:
                    cands.add(tuple(sorted((ss[i], ss[j]))))
    return photos, cands


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


# Calibrated on E:\Kids Photos (2026-11-10): the known re-download and resize pairs sit at
# 0.0-1.5; same-moment edits (another Live key frame, a filter) at 4.9-12; bursts at 3.1+.
THRESH = {"strict": 3.0, "tight": 2.0, "copy": 1.5}


def cmd_plan(a):
    lib = os.path.abspath(a.library)
    man = load_json(meta(lib, "manifest.json"), None)
    files = man["files"]
    feats = load_json(meta(lib, DEDUP, "features.json"), {})
    missing = [s for s in files if s not in feats]
    if missing:
        sys.exit(f"{len(missing)} library files have no features: run scan first")
    T = dict(THRESH, strict=a.strict, tight=a.tight)
    photos, cands = find_photo_pairs(files, feats)
    log(f"{len(photos)} photos, {len(cands)} candidate pairs")
    dsu, edges, rejected = DSU(), {}, []
    for s1, s2 in sorted(cands):
        cls, ev = pair_verdict(files[s1], photos[s1], files[s2], photos[s2], T)
        if cls is None:
            rejected.append((s1, s2, ev))
            continue
        if cls == "re-encoded" or (photos[s1]["w"], photos[s1]["h"]) == (photos[s2]["w"], photos[s2]["h"]):
            if a.pixels:
                diff = full_pixels_equal(rel_to_abs(lib, files[s1]["path"]), rel_to_abs(lib, files[s2]["path"]))
                if diff == 0:
                    cls = "same pixels"
                ev += f"; full-image max diff {diff:.0f}"
        edges[(s1, s2)] = (cls, ev)
        dsu.union(s1, s2)

    # videos: same capture time (to the second, from the manifest) and the same length
    vids = defaultdict(list)
    for s, e in files.items():
        if e["kind"] in ("video", "large") and e.get("date") and len(e["date"]) > 10 \
                and e.get("date_source") == "quicktime":
            f = feats.get(s, {})
            if f.get("seconds"):
                vids[e["date"]].append(s)
    for ss in vids.values():
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                f1, f2 = feats[ss[i]], feats[ss[j]]
                if abs(f1["seconds"] - f2["seconds"]) <= 0.1:
                    p = tuple(sorted((ss[i], ss[j])))
                    edges[p] = ("video re-encoded", f"same capture time {files[ss[i]]['date']}, "
                                f"{f1['seconds']} s / {f2['seconds']} s; {f1['w']}x{f1['h']} / {f2['w']}x{f2['h']}")
                    dsu.union(*p)

    groups = defaultdict(list)
    for s in list(dsu.p):
        groups[dsu.find(s)].append(s)
    run = time.strftime("%Y%m%d-%H%M%S")
    out = meta(lib, DEDUP, f"{run}-plan.csv")
    rows, counts = [], Counter()
    for gi, ss in enumerate(sorted(groups.values(), key=lambda ss: min(files[s]["path"] for s in ss)), 1):
        def key(s):
            e = files[s]
            f = feats.get(s, {})
            if e["kind"] == "photo":
                return rank_key(s, e, f, files)
            return ((f.get("w") or 0) * (f.get("h") or 0), e["size"], s)
        ss.sort(key=key, reverse=True)
        keep = ss[0]
        # Live clips of removed photos: the first such photo's clips move to a keeper that has
        # none (apply relinks them); any other's are a second clip of the same shot and go too
        has_clip = bool(files[keep].get("live_clips"))
        extra = []
        for s in ss[1:]:
            cl = [c for c in files[s].get("live_clips") or [] if c in files]
            if cl and not has_clip:
                has_clip = True
            else:
                extra += cl
        ss = ss + [c for c in extra if c not in ss]
        for s in ss:
            e, f = files[s], feats.get(s, {})
            if s == keep:
                why = ""
            elif s in extra:
                why, ev = "live clip", "the clip of a removed photo; the copy kept has its own clip"
                counts[why] += 1
            else:
                p = tuple(sorted((s, keep)))
                why = edges.get(p, (None, None))[0] or "same group"
                ev = edges.get(p, (None, ""))[1]
                # a group joined through a third file: say so and give that file's link
                if p not in edges:
                    links = [edges[q] for q in edges if s in q and dsu.find(q[0]) == dsu.find(keep)]
                    ev = "via another copy: " + (links[0][1] if links else "")
                counts[why] += 1
            rows.append([gi, "keep" if s == keep else "remove", why if s != keep else "",
                         e["path"], s, e["size"], e["kind"], f.get("w"), f.get("h"), f.get("dto") or e.get("date"),
                         f.get("subsec"), f.get("tags"), bool(e.get("live_clips")) or bool(f.get("motion")),
                         ";".join(e.get("live_clips") or []), ev if s != keep else "",
                         " | ".join(e.get("sources", [])[:3])])
    hdr = ["group", "action", "class", "path", "sha256", "size", "kind", "w", "h", "date", "subsec",
           "exif_tags", "live_or_motion", "live_clips", "evidence", "sources"]
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows)
    rej = meta(lib, DEDUP, f"{run}-not-confident.csv")
    with open(rej, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path_a", "path_b", "why not"])
        for s1, s2, ev in rejected:
            if "mad" in ev and float(re.search(r"mad ([\d.]+)", ev)[1]) <= a.strict * 2:
                w.writerow([files[s1]["path"], files[s2]["path"], ev])
    log(f"{len(groups)} groups, {sum(counts.values())} to remove: {dict(counts)}")
    log(f"plan: {out}")
    log(f"close but not confident: {rej}")


# --------------------------------------------------------------------------- apply / undo

def aside_dir(lib):
    return os.path.join(os.path.dirname(lib.rstrip("\\/")), os.path.basename(lib.rstrip("\\/")) + " Duplicates")


def zips_dir(lib):
    return os.path.join(os.path.dirname(lib.rstrip("\\/")), os.path.basename(lib.rstrip("\\/")) + " Zips")


def volume_of(rel):
    parts = rel.split("/")
    return "/".join(parts[:2]) if len(parts) == 3 and parts[0] in ("photos", "videos", "live") else None


def cmd_apply(a):
    lib = os.path.abspath(a.library)
    mp = meta(lib, "manifest.json")
    man = load_json(mp, None)
    files = man["files"]
    vols = man.setdefault("volumes", {})
    aside_root = os.path.abspath(a.aside or aside_dir(lib))
    run = time.strftime("%Y%m%d-%H%M%S")
    aside = os.path.join(aside_root, run)
    with open(a.plan, encoding="utf-8") as f:
        plan = list(csv.DictReader(f))
    keep_of = {}
    for r in plan:
        if r["action"] == "keep":
            keep_of[r["group"]] = r["sha256"]
    todo = [r for r in plan if r["action"] == "remove"]
    # check before moving anything: every file as planned, keepers present, sealed volumes
    probs, touched_sealed = [], set()
    for r in todo:
        s = r["sha256"]
        e = files.get(s)
        if not e or e["path"] != r["path"]:
            probs.append(f"{r['path']}: not in the manifest as planned")
            continue
        if keep_of.get(r["group"]) not in files:
            probs.append(f"{r['path']}: its keeper is gone from the manifest")
        p = rel_to_abs(lib, e["path"])
        if not os.path.isfile(p) or os.path.getsize(p) != e["size"]:
            probs.append(f"{r['path']}: missing or a different size on disk")
        v = volume_of(e["path"])
        if v and vols.get(v, {}).get("sealed"):
            touched_sealed.add(v)
    if probs:
        sys.exit("refusing to apply:\n  " + "\n  ".join(probs[:30]))
    if touched_sealed and not a.unseal:
        sys.exit(f"{len(touched_sealed)} sealed volumes would change ({', '.join(sorted(touched_sealed))}); "
                 "pass --unseal to unseal them (their zips are moved aside, the next pack rebuilds them)")
    os.makedirs(aside, exist_ok=True)
    rec_path = meta(lib, DEDUP, f"{run}-removed.csv")
    set_aside = man.setdefault("set_aside", {})
    n = 0
    # zips of volumes that change: moved aside first, so a stale zip is never uploaded
    unsealed = []
    for v in sorted(touched_sealed):
        vr = vols[v]
        z = os.path.join(zips_dir(lib), vr.get("zip") or "")
        zdest = os.path.join(aside, "_zips", vr.get("zip") or "none")
        if vr.get("zip") and os.path.isfile(z):
            os.makedirs(os.path.dirname(zdest), exist_ok=True)
            os.replace(z, zdest)
        man.setdefault("unsealed", {})[v] = dict(vr, unsealed_by=f"dedup {run}", zip_set_aside=zdest)
        vols.pop(v)
        unsealed.append(v)
    save_json(mp, man)
    with open(rec_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sha256", "library_path", "aside_path", "size", "class", "kept_sha256", "kept_path", "group"])
        for r in todo:
            s = r["sha256"]
            e = files[s]
            src = rel_to_abs(lib, e["path"])
            dest = os.path.join(aside, *e["path"].split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest)
            k = keep_of[r["group"]]
            # Live pairing: a removed photo's clips follow the keeper if it has none of its own
            cl = [c for c in e.get("live_clips") or [] if c in files]
            if cl and not files[k].get("live_clips"):    # (plan set aside any other photo's)
                for c in cl:
                    files[c]["live_photo_of"] = k
                files[k]["live_clips"] = cl
            if e.get("live_photo_of") and e["live_photo_of"] in files:
                ph = files[e["live_photo_of"]]
                ph["live_clips"] = [c for c in ph.get("live_clips", []) if c != s] or None
                if ph["live_clips"] is None:
                    ph.pop("live_clips")
            set_aside[s] = dict(e, aside_path=dest, kept=k, kept_path=files[k]["path"],
                                cls=r["class"], run=run)
            del files[s]
            w.writerow([s, e["path"], dest, e["size"], r["class"], k, files[k]["path"], r["group"]])
            f.flush()
            n += 1
            if n % 100 == 0:
                save_json(mp, man)
    save_json(mp, man)
    log(f"moved {n} files to {aside}; record {rec_path}")
    if unsealed:
        log(f"unsealed {len(unsealed)} volumes (zips moved to {os.path.join(aside, '_zips')}): {', '.join(unsealed)}")


def cmd_undo(a):
    lib = os.path.abspath(a.library)
    mp = meta(lib, "manifest.json")
    man = load_json(mp, None)
    files, set_aside = man["files"], man.get("set_aside", {})
    with open(a.record, encoding="utf-8") as f:
        recs = list(csv.DictReader(f))
    n = 0
    for r in recs:
        s = r["sha256"]
        e = set_aside.get(s)
        if not e:
            continue
        dest = rel_to_abs(lib, e["path"])
        if os.path.exists(dest):
            sys.exit(f"{dest} exists; not overwriting")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(e["aside_path"], dest)
        for k in ("aside_path", "kept", "kept_path", "cls", "run"):
            e.pop(k, None)
        files[s] = e
        if e.get("live_photo_of") in files:
            ph = files[e["live_photo_of"]]
            if s not in ph.setdefault("live_clips", []):
                ph["live_clips"].append(s)
        for c in e.get("live_clips") or []:
            if c in files:
                old = files[c].get("live_photo_of")
                files[c]["live_photo_of"] = s
                if old in files and old != s:
                    files[old]["live_clips"] = [x for x in files[old].get("live_clips", []) if x != c]
                    if not files[old]["live_clips"]:
                        files[old].pop("live_clips")
        del set_aside[s]
        n += 1
    save_json(mp, man)
    log(f"restored {n} files. Unsealed volumes stay unsealed (the next pack zips them); "
        "their old zips are under the run's _zips folder")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "plan", "apply", "undo"):
        sp = sub.add_parser(name)
        sp.add_argument("--library", required=True)
        if name == "scan":
            sp.add_argument("--workers", type=int, default=3)
        if name == "plan":
            sp.add_argument("--strict", type=float, default=THRESH["strict"],
                            help="thumbnail difference allowed with same date and same name")
            sp.add_argument("--tight", type=float, default=THRESH["tight"],
                            help="thumbnail difference allowed with only one of them")
            sp.add_argument("--pixels", action="store_true", help="compare full images of same-size pairs")
        if name == "apply":
            sp.add_argument("--plan", required=True)
            sp.add_argument("--aside", help="where removed copies go (default: '<library> Duplicates')")
            sp.add_argument("--unseal", action="store_true", help="allow changing sealed volumes")
        if name == "undo":
            sp.add_argument("--record", required=True)
    a = ap.parse_args()
    {"scan": cmd_scan, "plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo}[a.cmd](a)


if __name__ == "__main__":
    main()
