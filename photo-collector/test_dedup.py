# /// script
# requires-python = ">=3.11"
# dependencies = ["exifread>=3.0", "pytest>=8", "pillow>=10", "pillow-heif>=0.16", "numpy>=1.26"]
# ///
"""Fixture tests for dedup.py with photo_collector.py: scratch libraries only, built here.

    uv run test_dedup.py            (scratch in <repo>/out/pytest-dedup)
"""
from __future__ import annotations

import csv
import glob
import hashlib
import importlib.util
import io
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load("photo_collector")
dd = _load("dedup")


def scene(seed: int, size=(400, 300), shift=0):
    """A photo-like picture: smooth gradients and a few shapes, distinct per seed."""
    from PIL import Image, ImageDraw
    import numpy as np
    rng = np.random.default_rng(seed)
    w, h = size
    y, x = np.mgrid[0:h, 0:w]
    base = np.stack([(x * rng.uniform(0.2, 0.6) + y * rng.uniform(0.1, 0.5) + rng.uniform(0, 80)) % 256
                     for _ in range(3)], -1).astype(np.uint8)
    im = Image.fromarray(base)
    d = ImageDraw.Draw(im)
    for _ in range(6):
        x0, y0 = rng.integers(0, w - 60), rng.integers(0, h - 60)
        d.ellipse([x0 + shift, y0, x0 + shift + rng.integers(20, 60), y0 + rng.integers(20, 60)],
                  fill=tuple(int(c) for c in rng.integers(0, 255, 3)))
    return im


def save_jpeg(path, im, when=None, subsec=None, make="Apple", quality=92):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ex = Image.Exif()
    if make:
        ex[271] = make
        ex[272] = "iPhone"
    if when:
        ifd = ex.get_ifd(0x8769)
        ifd[0x9003] = when
        if subsec:
            ifd[0x9291] = subsec
    im.save(path, "JPEG", exif=ex.tobytes(), quality=quality)
    return path


def run(mod, *argv):
    old = sys.argv
    sys.argv = [mod.__name__ + ".py", *map(str, argv)]
    try:
        mod.main()
    finally:
        sys.argv = old


def manifest(lib):
    with open(os.path.join(lib, "_metadata", "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def latest(lib, suffix):
    return sorted(glob.glob(os.path.join(lib, "_metadata", "dedup", "*" + suffix)))[-1]


def rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def w(tmp_path):
    class W:
        src = str(tmp_path / "src")
        src2 = str(tmp_path / "src2")
        lib = str(tmp_path / "lib")
        zips = str(tmp_path / "zips")
        aside = str(tmp_path / "aside")
    os.makedirs(W.src)
    os.makedirs(W.src2)
    return W


def library_with_dups(w):
    """a: original; a resized copy (no EXIF); a re-encoded copy (same EXIF, other bytes);
    b: a burst frame of a (same second, other sub-second, slightly moved); c: unrelated."""
    when = "2023:05:01 10:00:00"
    a = scene(1)
    save_jpeg(os.path.join(w.src, "IMG_1.jpg"), a, when, "111")
    save_jpeg(os.path.join(w.src, "small", "IMG_1.jpg"), a.resize((200, 150)), make=None)
    save_jpeg(os.path.join(w.src, "again", "IMG_1.jpg"), a, when, "111", quality=80)
    save_jpeg(os.path.join(w.src, "IMG_2.jpg"), scene(1, shift=12), when, "555")
    save_jpeg(os.path.join(w.src, "IMG_3.jpg"), scene(3), "2023:05:02 10:00:00", "1")
    run(pc, "collect", w.src, "--library", w.lib, "--reserve-gb", "0")
    run(dd, "scan", "--library", w.lib, "--workers", "1")
    run(dd, "plan", "--library", w.lib)
    return latest(w.lib, "-plan.csv")


def test_plan_finds_resized_and_reencoded_not_burst(w):
    plan = rows(library_with_dups(w))
    removes = {(r["path"].rsplit("/", 1)[1], r["class"]) for r in plan if r["action"] == "remove"}
    keeps = [r for r in plan if r["action"] == "keep"]
    assert len(keeps) == 1 and keeps[0]["w"] == "400" and keeps[0]["exif_tags"] != "0"
    assert {c for _, c in removes} <= {"resized", "re-encoded"} and len(removes) == 2
    assert not any("IMG_2" in r["path"] or "IMG_3" in r["path"] for r in plan)


def test_apply_moves_aside_and_collect_never_recopies(w):
    plan = library_with_dups(w)
    before = len(manifest(w.lib)["files"])
    run(dd, "apply", "--library", w.lib, "--plan", plan, "--aside", w.aside)
    m = manifest(w.lib)
    assert len(m["files"]) == before - 2 and len(m["set_aside"]) == 2
    for s, e in m["set_aside"].items():
        assert os.path.isfile(e["aside_path"]) and sha(e["aside_path"]) == s
        assert not os.path.exists(pc.rel_to_abs(w.lib, e["path"]))
    run(pc, "collect", w.src, "--library", w.lib, "--reserve-gb", "0")
    assert len(manifest(w.lib)["files"]) == before - 2
    # retire: the set-aside sources are deletable, verified against both copies
    run(pc, "retire", w.src, "--library", w.lib)
    d = os.path.join(w.lib, "_metadata", "retire")
    r = rows(sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith("src.csv"))[-1])
    assert all(x["action"] == "delete" for x in r if x["path"].endswith(".jpg")), r
    assert sum("set-aside duplicate" in x["reason"] for x in r) == 2


def test_apply_on_sealed_volume_needs_unseal_and_zip_goes_aside(w):
    plan = library_with_dups(w)
    run(pc, "pack", "--library", w.lib, "--zips", w.zips)
    sealed_zip = os.path.join(w.zips, "photos-2023.zip")
    assert os.path.isfile(sealed_zip)
    w.lib_zips = os.path.join(os.path.dirname(w.lib), "lib Zips")
    with pytest.raises(SystemExit):
        run(dd, "apply", "--library", w.lib, "--plan", plan, "--aside", w.aside)
    assert len(manifest(w.lib).get("set_aside", {})) == 0
    # dedup moves the zip from '<library> Zips' (pack's default); point pack there
    os.replace(w.zips, w.lib_zips)
    run(dd, "apply", "--library", w.lib, "--plan", plan, "--aside", w.aside, "--unseal")
    m = manifest(w.lib)
    assert "photos/2023" not in m["volumes"] and "photos/2023" in m["unsealed"]
    assert not os.path.exists(os.path.join(w.lib_zips, "photos-2023.zip"))
    assert os.path.isfile(m["unsealed"]["photos/2023"]["zip_set_aside"])
    run(pc, "pack", "--library", w.lib)                     # rebuilds the volume's zip, smaller
    m = manifest(w.lib)
    assert m["volumes"]["photos/2023"]["sealed"] and m["volumes"]["photos/2023"]["files"] == 3   # IMG_1-3; the re-encoded copy went


def test_undo_restores(w):
    plan = library_with_dups(w)
    before = manifest(w.lib)["files"]
    run(dd, "apply", "--library", w.lib, "--plan", plan, "--aside", w.aside)
    run(dd, "undo", "--library", w.lib, "--record", latest(w.lib, "-removed.csv"))
    m = manifest(w.lib)
    assert m["files"] == before and not m["set_aside"]
    assert all(os.path.isfile(pc.rel_to_abs(w.lib, e["path"])) for e in m["files"].values())


def test_live_clip_of_set_aside_photo_pairs_with_keeper(w):
    """The resized copy is set aside; a clip that arrives later beside it pairs with the keeper."""
    plan = library_with_dups(w)
    run(dd, "apply", "--library", w.lib, "--plan", plan, "--aside", w.aside)
    from PIL import Image  # noqa: F401
    import shutil
    shutil.copy(os.path.join(w.src, "small", "IMG_1.jpg"), os.path.join(w.src2, "IMG_1.jpg"))
    with open(os.path.join(w.src2, "IMG_1.mov"), "wb") as f:
        import struct
        body = struct.pack(">I", 0) + struct.pack(">IIII", 0, 0, 1000, 2000) + b"\0" * 80
        mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
        f.write(struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0" +
                struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd)
    run(pc, "collect", w.src2, "--library", w.lib, "--reserve-gb", "0")
    files = manifest(w.lib)["files"]
    clip = [s for s, e in files.items() if e["kind"] == "live"]
    assert len(clip) == 1
    keeper = files[clip[0]]["live_photo_of"]
    assert files[keeper]["live_clips"] == clip and files[keeper]["path"].endswith("IMG_1.jpg")


def mov(path, seed):
    import struct
    body = struct.pack(">I", 0) + struct.pack(">IIII", 0, 0, 1000, 2000) + b"\0" * 80
    mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
    with open(path, "wb") as f:
        f.write(struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0" +
                struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd + str(seed).encode())


def test_two_live_copies_leave_one_photo_and_one_clip(w):
    a = scene(7)
    save_jpeg(os.path.join(w.src, "a", "IMG_7.jpg"), a, "2023:05:01 10:00:00", "7")
    mov(os.path.join(w.src, "a", "IMG_7.mov"), 1)
    save_jpeg(os.path.join(w.src, "b", "IMG_7.jpg"), a, "2023:05:01 10:00:00", "7", quality=80)
    mov(os.path.join(w.src, "b", "IMG_7.mov"), 2)
    run(pc, "collect", w.src, "--library", w.lib, "--reserve-gb", "0")
    run(dd, "scan", "--library", w.lib, "--workers", "1")
    run(dd, "plan", "--library", w.lib)
    plan = rows(latest(w.lib, "-plan.csv"))
    assert sorted(r["class"] for r in plan if r["action"] == "remove") == ["live clip", "re-encoded"]
    run(dd, "apply", "--library", w.lib, "--plan", latest(w.lib, "-plan.csv"), "--aside", w.aside)
    files = manifest(w.lib)["files"]
    photos = [s for s, e in files.items() if e["kind"] == "photo"]
    clips = [s for s, e in files.items() if e["kind"] == "live"]
    assert len(photos) == 1 and len(clips) == 1
    assert files[photos[0]]["live_clips"] == clips and files[clips[0]]["live_photo_of"] == photos[0]


def test_clip_follows_keeper_without_one(w):
    """The better copy has no clip; the lesser one's clip moves to it."""
    a = scene(8)
    save_jpeg(os.path.join(w.src, "a", "IMG_8.jpg"), a, "2023:05:01 10:00:00", "8")
    save_jpeg(os.path.join(w.src, "b", "IMG_8.jpg"), a.resize((200, 150)), "2023:05:01 10:00:00", "8")
    mov(os.path.join(w.src, "b", "IMG_8.mov"), 3)
    run(pc, "collect", w.src, "--library", w.lib, "--reserve-gb", "0")
    run(dd, "scan", "--library", w.lib, "--workers", "1")
    run(dd, "plan", "--library", w.lib)
    run(dd, "apply", "--library", w.lib, "--plan", latest(w.lib, "-plan.csv"), "--aside", w.aside)
    files = manifest(w.lib)["files"]
    (ph,) = [s for s, e in files.items() if e["kind"] == "photo"]
    (cl,) = [s for s, e in files.items() if e["kind"] == "live"]
    assert files[ph]["live_clips"] == [cl] and files[cl]["live_photo_of"] == ph


if __name__ == "__main__":
    base = os.path.join(HERE, "..", "out", "pytest-dedup")
    os.makedirs(os.path.dirname(base), exist_ok=True)
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider", "--basetemp", base, *sys.argv[1:]]))
