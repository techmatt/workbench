# /// script
# requires-python = ">=3.11"
# dependencies = ["exifread>=3.0", "pytest>=8", "pillow>=10"]
# ///
"""Fixture tests for photo_collector: scratch sources and libraries only, built here.

    uv run test_photo_collector.py            (scratch in <repo>/out/pytest-photo-collector)

PC_MODULE=<path to a photo_collector.py> runs the same tests against another copy (to show a
fixture fails on the code before a fix).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import sys
import unicodedata
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "photo_collector", os.environ.get("PC_MODULE") or os.path.join(HERE, "photo_collector.py"))
pc = importlib.util.module_from_spec(_spec)
sys.modules["photo_collector"] = pc
_spec.loader.exec_module(pc)


# --------------------------------------------------------------------------- fixture builders

def _seed(s) -> bytes:
    return hashlib.sha256(str(s).encode()).digest()


def jpeg_bytes(seed, when: str | None = None, offset: str | None = None) -> bytes:
    """A small JPEG, distinct per seed, with EXIF DateTimeOriginal (and OffsetTimeOriginal)."""
    import io
    from PIL import Image
    h = _seed(seed)
    img = Image.new("RGB", (8, 8), tuple(h[:3]))
    ex = Image.Exif()
    if when:
        ifd = ex.get_ifd(0x8769)
        ifd[0x9003] = when
        if offset:
            ifd[0x9011] = offset
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=ex.tobytes(), comment=h.hex().encode())
    return buf.getvalue()


def jpeg(path, when=None, offset=None, seed=None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(jpeg_bytes(seed if seed is not None else path, when, offset))
    return path


def mp4_bytes(seed, secs: float = 2.0, created_utc: dt.datetime | None = None) -> bytes:
    scale = 1000
    ct = int((created_utc - dt.datetime(1904, 1, 1)).total_seconds()) if created_utc else 0
    body = struct.pack(">I", 0) + struct.pack(">IIII", ct, ct, scale, int(secs * scale)) + b"\0" * 80
    mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\0\0\0\0"
    pad = _seed(seed)
    return ftyp + moov + struct.pack(">I4s", 8 + len(pad), b"mdat") + pad


def mp4(path, secs=2.0, created_utc=None, seed=None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(mp4_bytes(seed if seed is not None else path, secs, created_utc))
    return path


def sidecar_bytes(when: dt.datetime) -> bytes:
    return json.dumps({"photoTakenTime": {"timestamp": str(int(when.timestamp()))}}).encode()


def make_zip(path, members: list[tuple[str, bytes]]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")               # duplicate names on purpose
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            for n, b in members:
                zf.writestr(n, b)
    return path


def run(*argv):
    old = sys.argv
    sys.argv = ["photo_collector.py", *map(str, argv)]
    try:
        pc.main()
    finally:
        sys.argv = old


def collect(src, lib, *extra):
    run("collect", src, "--library", lib, "--reserve-gb", "0", *extra)


def retire(src, lib, *extra):
    run("retire", src, "--library", lib, *extra)


def pack(lib, zdir, *extra):
    run("pack", "--library", lib, "--zips", zdir, *extra)


def manifest(lib) -> dict:
    with open(os.path.join(lib, "_metadata", "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def by_name(lib) -> dict:
    """original file name (end of the library name) -> manifest entry"""
    return {e["path"]: e for e in manifest(lib)["files"].values()}


def retire_rows(lib, which="") -> list[dict]:
    d = os.path.join(lib, "_metadata", "retire")
    fn = sorted(f for f in os.listdir(d) if f.endswith(f"{which}.csv") and
                (which or not f.endswith(("-zip-members.csv", "-deleted.csv"))))[-1]
    with open(os.path.join(d, fn), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def tree(root) -> dict[str, bytes]:
    out = {}
    for dp, _, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


@pytest.fixture
def w(tmp_path):
    """src/, lib/, zips/ under one scratch folder."""
    class W:
        src = str(tmp_path / "src")
        lib = str(tmp_path / "lib")
        zips = str(tmp_path / "zips")
        root = str(tmp_path)
    os.makedirs(W.src)
    return W


# --------------------------------------------------------------------------- retire

def test_retire_zip_with_two_members_of_one_name_kept(w):
    """Opening members by name read the last 'a.jpg' twice: the first was never checked, and
    the zip was deleted though its first a.jpg was in no library."""
    first, second = jpeg_bytes("first", "2023:01:01 10:00:00"), jpeg_bytes("second", "2023:01:02 10:00:00")
    only = os.path.join(w.root, "only-second")
    os.makedirs(only)
    with open(os.path.join(only, "a.jpg"), "wb") as f:
        f.write(second)
    collect(only, w.lib)
    z = make_zip(os.path.join(w.src, "d.zip"), [("a.jpg", first), ("a.jpg", second)])
    retire(w.src, w.lib, "--delete")
    assert os.path.exists(z)
    row = [r for r in retire_rows(w.lib) if r["path"] == z][0]
    assert row["action"] == "keep"


def test_collect_zip_with_two_members_of_one_name_gets_both(w):
    first, second = jpeg_bytes("first", "2023:01:01 10:00:00"), jpeg_bytes("second", "2023:01:02 10:00:00")
    make_zip(os.path.join(w.src, "d.zip"), [("a.jpg", first), ("a.jpg", second)])
    collect(w.src, w.lib)
    shas = set(manifest(w.lib)["files"])
    assert {hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest()} <= shas


def corrupt_member(zp, data: bytes):
    with open(zp, "rb") as f:
        b = bytearray(f.read())
    i = b.find(data) + len(data) // 2
    b[i] ^= 0xFF
    with open(zp, "wb") as f:
        f.write(b)


def test_corrupt_zip_member_kept_and_no_crash(w):
    good, bad = jpeg_bytes("good", "2023:01:01 10:00:00"), jpeg_bytes("bad", "2023:01:02 10:00:00")
    z = make_zip(os.path.join(w.src, "d.zip"), [("good.jpg", good), ("bad.jpg", bad)])
    corrupt_member(z, bad)
    collect(w.src, w.lib)                               # no crash: the good member is collected
    assert hashlib.sha256(good).hexdigest() in manifest(w.lib)["files"]
    retire(w.src, w.lib, "--delete")
    assert os.path.exists(z)
    members = retire_rows(w.lib, "-zip-members")
    assert any(r["path"].endswith("!bad.jpg") and r["reason"].startswith("unreadable") for r in members)


def test_truncated_zip_kept(w):
    data = jpeg_bytes("x", "2023:01:01 10:00:00")
    z = make_zip(os.path.join(w.src, "d.zip"), [("x.jpg", data)])
    with open(os.path.join(w.root, "x.jpg"), "wb") as f:
        f.write(data)
    os.makedirs(os.path.join(w.root, "s2"))
    shutil.copy(os.path.join(w.root, "x.jpg"), os.path.join(w.root, "s2", "x.jpg"))
    collect(os.path.join(w.root, "s2"), w.lib)
    with open(z, "r+b") as f:                           # a partial download
        f.truncate(os.path.getsize(z) // 2)
    retire(w.src, w.lib, "--delete")
    assert os.path.exists(z)
    row = [r for r in retire_rows(w.lib) if r["path"] == z][0]
    assert row["action"] == "keep"


def test_nested_zip_excluded_member_and_sidecar_keep_the_zip(w):
    """(fixed by 006; kept as a regression fixture) members that would be lost with the zip."""
    a = jpeg_bytes("a", "2023:01:01 10:00:00")
    inner = os.path.join(w.root, "inner.zip")
    make_zip(inner, [("deep.jpg", jpeg_bytes("deep", "2023:01:03 10:00:00"))])
    with open(inner, "rb") as f:
        inner_b = f.read()
    zs = [make_zip(os.path.join(w.src, "nested.zip"), [("a.jpg", a), ("inner.zip", inner_b)]),
          make_zip(os.path.join(w.src, "side.zip"), [("a.jpg", a), ("a.jpg.json", b"{}")]),
          make_zip(os.path.join(w.src, "excl.zip"), [("a.jpg", a), ("face_labels/f.jpg", jpeg_bytes("f"))])]
    collect(w.src, w.lib, "--exclude", "face_labels")
    retire(w.src, w.lib, "--exclude", "face_labels", "--delete")
    assert all(os.path.exists(z) for z in zs)


def test_file_changed_after_hashing_is_not_deleted(w, monkeypatch):
    """Retire hashed a file, then deleted it up to an hour later whatever it held by then."""
    p = jpeg(os.path.join(w.src, "a.jpg"), "2023:01:01 10:00:00")
    q = jpeg(os.path.join(w.src, "b.jpg"), "2023:01:02 10:00:00")
    collect(w.src, w.lib)
    real = pc.Meter.add
    done = []

    def add(self, n):                                   # after a.jpg is hashed, rewrite it
        real(self, n)
        if self.label == "retire" and not done:
            done.append(1)
            with open(p, "ab") as f:
                f.write(b"new bytes written during the run")
    monkeypatch.setattr(pc.Meter, "add", add)
    retire(w.src, w.lib, "--delete")
    assert os.path.exists(p) and not os.path.exists(q)
    d = os.path.join(w.lib, "_metadata", "retire")
    rec = [f for f in os.listdir(d) if f.endswith("-deleted.csv")]
    assert rec
    with open(os.path.join(d, rec[0]), encoding="utf-8") as f:
        assert "changed since it was read" in f.read()


def test_manifest_path_missing_on_disk_is_kept(w):
    p = jpeg(os.path.join(w.src, "a.jpg"), "2023:01:01 10:00:00")
    collect(w.src, w.lib)
    for e in manifest(w.lib)["files"].values():
        os.remove(pc.rel_to_abs(w.lib, e["path"]))
    retire(w.src, w.lib, "--delete")
    assert os.path.exists(p)
    assert retire_rows(w.lib)[0]["reason"] == "library copy missing"


def test_library_copy_renamed_to_other_unicode_form_is_kept(w):
    nfc = unicodedata.normalize("NFC", "café.jpg")
    p = jpeg(os.path.join(w.src, nfc), "2023:01:01 10:00:00")
    collect(w.src, w.lib)
    e = list(manifest(w.lib)["files"].values())[0]
    lp = pc.rel_to_abs(w.lib, e["path"])
    os.rename(lp, unicodedata.normalize("NFD", lp))   # e.g. round-tripped through a Mac
    retire(w.src, w.lib, "--delete")
    assert os.path.exists(p)


def test_source_names_differing_by_case_or_unicode_form(w):
    """NFC and NFD names are two files on NTFS; each is judged on its own bytes."""
    a = jpeg(os.path.join(w.src, unicodedata.normalize("NFC", "é.jpg")), "2023:01:01 10:00:00")
    b = jpeg(os.path.join(w.src, unicodedata.normalize("NFD", "é.jpg")), "2023:01:02 10:00:00")
    assert a != b and os.path.exists(a) and os.path.exists(b)
    collect(w.src, w.lib)
    files = manifest(w.lib)["files"]
    assert len(files) == 2
    assert len({unicodedata.normalize("NFC", e["path"]).lower() for e in files.values()}) == 2
    os.remove(pc.rel_to_abs(w.lib, [e for e in files.values() if b in e["sources"]][0]["path"]))
    retire(w.src, w.lib, "--delete")
    assert not os.path.exists(a) and os.path.exists(b)


def test_library_copy_in_other_case_still_verifies(w):
    p = jpeg(os.path.join(w.src, "a.jpg"), "2023:01:01 10:00:00")
    collect(w.src, w.lib)
    e = list(manifest(w.lib)["files"].values())[0]
    lp = pc.rel_to_abs(w.lib, e["path"])
    os.rename(lp, os.path.join(os.path.dirname(lp), os.path.basename(lp).upper()))
    retire(w.src, w.lib, "--delete")
    assert not os.path.exists(p)                        # same bytes, verified


# --------------------------------------------------------------------------- collect: dates

@pytest.mark.parametrize("name, json_name, ok", [
    ("IMG_1234.jpg", "IMG_1234.jpg.json", True),
    ("IMG_1234.jpg", "IMG_1234.jpg.supplemental-metadata.json", True),
    ("IMG_1234.jpg", "IMG_1234.json", True),
    ("IMG_1234(1).jpg", "IMG_1234.jpg(1).json", True),
    ("IMG_1234(1).jpg", "IMG_1234.jpg.supplemental-metadata(1).json", True),
    ("IMG_1234.jpg", "IMG_1234.jpg(1).json", False),    # the (1) copy's sidecar, not this one's
    ("IMG_1234-edited.jpg", "IMG_1234.jpg.supplemental-metadata.json", True),
    ("PXL_20230514_101112345.PORTRAIT.jpg", "PXL_20230514_101112345.PORTRAIT.jpg.supplemen.json", True),
    ("Screenshot_20230514-101112_Some Long App Name.jpg",
     "Screenshot_20230514-101112_Some Long App Name.j.json", True),
    ("Screenshot_20230514-101112_Some Long App Name Extra.jpg",
     "Screenshot_20230514-101112_Some Long App Name.json", True),
    ("IMG_12.jpg", "IMG_1.jpg.json", False),
    ("IMG_1.jpg", "IMG_12.jpg.json", False),
])
def test_sidecar_names(name, json_name, ok):
    folder = "f"
    items = [pc.Item(src=n, rel=n, folder=folder, name=n, size=1, stamp="", kind=pc.classify(n))
             for n in (name, json_name)]
    side = pc.find_sidecar_dates(items)
    got = pc.sidecar_for(items[0], side)
    assert bool(got) == ok, (name, json_name, got)


def test_cut_sidecar_name_shared_by_two_files_is_not_used():
    a = "Screenshot_20230514-101112_Some Long App Name One.jpg"
    b = "Screenshot_20230514-101112_Some Long App Name Two.jpg"
    j = "Screenshot_20230514-101112_Some Long App Name.json"
    items = [pc.Item(src=n, rel=n, folder="f", name=n, size=1, stamp="", kind=pc.classify(n)) for n in (a, b, j)]
    side = pc.find_sidecar_dates(items)
    assert pc.sidecar_for(items[0], side) == [] and pc.sidecar_for(items[1], side) == []


def test_takeout_dup_gets_its_own_sidecar_date(w):
    """IMG_1.jpg and IMG_1(1).jpg, no EXIF: the old prefix match gave IMG_1.jpg's date to
    neither or the wrong one."""
    d = os.path.join(w.src, "Takeout", "Google Photos", "Album")
    jpeg(os.path.join(d, "IMG_1.jpg"), seed="one")
    jpeg(os.path.join(d, "IMG_1(1).jpg"), seed="two")
    for fn, when in (("IMG_1.jpg.supplemental-metadata.json", dt.datetime(2021, 3, 4, 12)),
                     ("IMG_1.jpg.supplemental-metadata(1).json", dt.datetime(2022, 5, 6, 12))):
        with open(os.path.join(d, fn), "wb") as f:
            f.write(sidecar_bytes(when))
    collect(w.src, w.lib)
    got = {os.path.basename(e["sources"][0]): (e["date"], e["date_source"]) for e in manifest(w.lib)["files"].values()}
    assert got["IMG_1.jpg"] == ("2021-03-04T12:00:00", "sidecar")
    assert got["IMG_1(1).jpg"] == ("2022-05-06T12:00:00", "sidecar")


@pytest.mark.parametrize("when, offset, want", [
    ("2023:05:14 10:11:12", None, "2023-05-14T10:11:12"),
    ("2023:05:14 10:11:12", "+02:00", "2023-05-14T10:11:12"),    # wall time, zone not applied
    ("2023:05:14 10:11:12+02:00", None, "2023-05-14T10:11:12"),
    ("0000:00:00 00:00:00", None, None),
    ("    :  :     :  :  ", None, None),
])
def test_exif_dates(tmp_path, when, offset, want):
    p = jpeg(str(tmp_path / "x.jpg"), when, offset)
    d = pc.date_from_exif(p)
    assert (d.isoformat() if d else None) == want


@pytest.mark.parametrize("name, want", [
    ("IMG_20230514_101112.jpg", "2023-05-14T10:11:12"),
    ("PXL_20230514_101112345.jpg", "2023-05-14T10:11:12"),
    ("Screenshot_20230514-101112_Chrome.jpg", "2023-05-14T10:11:12"),
    ("IMG-20230514-WA0001.jpg", "2023-05-14"),
    ("signal-2023-05-14-10-11-12-123.jpg", "2023-05-14T10:11:12"),
    ("VID_20230514_101112.mp4", "2023-05-14T10:11:12"),
    ("2023-05-14 10.11.12.jpg", "2023-05-14T10:11:12"),
    ("IMG_1234.jpg", None),
    ("20231399.jpg", None),
    ("1920x1080.jpg", None),
])
def test_filename_dates(name, want):
    r = pc.date_from_filename(name)
    got = None if r is None else (r[0].isoformat() if r[1] else r[0].date().isoformat())
    assert got == want


# --------------------------------------------------------------------------- sealed volumes

def test_name_clash_with_sealed_file_opens_next_volume(w):
    jpeg(os.path.join(w.src, "a", "IMG_1.jpg"), "2023:01:01 10:00:00", seed="one")
    collect(w.src, w.lib)
    pack(w.lib, w.zips)
    sealed = tree(os.path.join(w.lib, "photos", "2023"))
    src2 = os.path.join(w.root, "src2")
    jpeg(os.path.join(src2, "IMG_1.jpg"), "2023:01:01 10:00:00", seed="two")   # same name and date
    collect(src2, w.lib)
    assert tree(os.path.join(w.lib, "photos", "2023")) == sealed
    paths = sorted(e["path"] for e in manifest(w.lib)["files"].values())
    assert paths == ["photos/2023-2/2023-01-01_100000_IMG_1.jpg", "photos/2023/2023-01-01_100000_IMG_1.jpg"]
    zips_before = tree(w.zips)
    pack(w.lib, w.zips)
    after = tree(w.zips)
    assert all(after[k] == v for k, v in zips_before.items())
    assert "photos-2023-2.zip" in after


def test_live_clip_of_sealed_photo(w):
    jpeg(os.path.join(w.src, "IMG_5.jpg"), "2023:06:01 10:00:00")
    collect(w.src, w.lib)
    pack(w.lib, w.zips)
    sealed = tree(os.path.join(w.lib, "photos"))
    mp4(os.path.join(w.src, "IMG_5.mov"), secs=2.0)          # the clip arrives later
    collect(w.src, w.lib)
    assert tree(os.path.join(w.lib, "photos")) == sealed
    files = manifest(w.lib)["files"]
    clip = [s for s, e in files.items() if e["kind"] == "live"]
    assert len(clip) == 1 and files[clip[0]]["path"] == "live/2023/2023-06-01_100000_IMG_5.mov"
    photo = files[clip[0]]["live_photo_of"]
    assert files[photo]["live_clips"] == clip


def test_live_pairing_ignores_unicode_form(w):
    jpeg(os.path.join(w.src, unicodedata.normalize("NFC", "café.jpg")), "2023:06:01 10:00:00")
    mp4(os.path.join(w.src, unicodedata.normalize("NFD", "café.mov")), secs=2.0)
    collect(w.src, w.lib)
    assert any(e["kind"] == "live" for e in manifest(w.lib)["files"].values())


def test_sealed_live_clips_volume_refuses_migrate(w):
    """A clip still in photos/ inside a sealed volume: migrate must not move it out."""
    jpeg(os.path.join(w.src, "IMG_5.jpg"), "2023:06:01 10:00:00")
    mp4(os.path.join(w.src, "IMG_5.mov"), secs=2.0)
    collect(w.src, w.lib)
    m = manifest(w.lib)
    clip = [s for s, e in m["files"].items() if e["kind"] == "live"][0]
    old = m["files"][clip]["path"]
    new = "photos/2023/" + old.rsplit("/", 1)[1]
    os.replace(pc.rel_to_abs(w.lib, old), pc.rel_to_abs(w.lib, new))   # the pre-004 layout
    m["files"][clip]["path"] = new
    m["volumes"] = {"photos/2023": {"sealed": "x", "zip": "photos-2023.zip"}}
    pc.save_json(os.path.join(w.lib, "_metadata", "manifest.json"), m)
    before = tree(os.path.join(w.lib, "photos"))
    with pytest.raises(SystemExit):
        run("migrate", "--library", w.lib, "--apply")
    assert tree(os.path.join(w.lib, "photos")) == before


def test_pack_rebuild_refuses_a_changed_sealed_volume(w):
    jpeg(os.path.join(w.src, "a.jpg"), "2023:01:01 10:00:00")
    collect(w.src, w.lib)
    pack(w.lib, w.zips)
    m = manifest(w.lib)
    m["volumes"]["photos/2023"]["files"] = 2           # as if a file had gone since sealing
    pc.save_json(os.path.join(w.lib, "_metadata", "manifest.json"), m)
    os.remove(os.path.join(w.zips, "photos-2023.zip"))
    pack(w.lib, w.zips)
    assert not os.path.exists(os.path.join(w.zips, "photos-2023.zip"))
    assert "changed since sealing" in manifest(w.lib)["volumes"]["photos/2023"]["failure"]


# --------------------------------------------------------------------------- interrupted runs

class Stop(Exception):
    pass


def _src_of_six(w):
    for i in range(6):
        jpeg(os.path.join(w.src, f"p{i}.jpg"), f"2023:01:0{i + 1} 10:00:00")
    make_zip(os.path.join(w.src, "z.zip"), [(f"z{i}.jpg", jpeg_bytes(f"z{i}", f"2024:01:0{i + 1} 10:00:00"))
                                            for i in range(3)])


@pytest.mark.parametrize("stop_at", [1, 3, 5, 8])
def test_collect_stopped_then_rerun_matches_a_clean_run(w, monkeypatch, stop_at):
    _src_of_six(w)
    clean = os.path.join(w.root, "clean")
    collect(w.src, clean)
    want = {e["path"] for e in manifest(clean)["files"].values()}

    real = pc.hash_stream
    n = [0]

    def hs(f, out=None):                                # stop part way through a copy
        if out is not None:
            n[0] += 1
            if n[0] == stop_at:
                out.write(f.read(100))
                raise Stop()
        return real(f, out)
    monkeypatch.setattr(pc, "hash_stream", hs)
    with pytest.raises(Stop):
        collect(w.src, w.lib)
    monkeypatch.setattr(pc, "hash_stream", real)
    collect(w.src, w.lib)
    assert {e["path"] for e in manifest(w.lib)["files"].values()} == want
    on_disk = {os.path.relpath(p, w.lib).replace("\\", "/") for p in
               (os.path.join(dp, fn) for dp, _, fns in os.walk(w.lib) for fn in fns)
               if not os.path.relpath(p, w.lib).startswith("_metadata")}
    assert on_disk == want


@pytest.mark.parametrize("stop_at", [2, 4, 7, 9])
def test_collect_stopped_after_placing_a_file_then_rerun(w, monkeypatch, stop_at):
    """Stop right after a file lands in its volume (staged move or .part rename), before the
    manifest knows it: the next run adopts it."""
    _src_of_six(w)
    clean = os.path.join(w.root, "clean")
    collect(w.src, clean)
    want = {e["path"] for e in manifest(clean)["files"].values()}
    real = os.replace
    n = [0]

    def replace(a, b):
        r = real(a, b)
        if os.sep + "photos" + os.sep in str(b):
            n[0] += 1
            if n[0] == stop_at:
                raise Stop()
        return r
    monkeypatch.setattr(pc.os, "replace", replace)
    with pytest.raises(Stop):
        collect(w.src, w.lib)
    monkeypatch.setattr(pc.os, "replace", real)
    collect(w.src, w.lib)
    assert {e["path"] for e in manifest(w.lib)["files"].values()} == want
    pack(w.lib, w.zips)                                 # and every volume packs
    assert all(r.get("sealed") for r in manifest(w.lib)["volumes"].values())


def test_pack_stopped_mid_zip_then_rerun(w, monkeypatch):
    for y in (2021, 2022):
        jpeg(os.path.join(w.src, f"{y}.jpg"), f"{y}:01:01 10:00:00")
    collect(w.src, w.lib)

    def boom(lib, vol, names, dest):
        with open(dest, "wb") as f:
            f.write(b"PK half a zip")
        raise Stop()
    monkeypatch.setattr(pc, "build_zip", boom)
    with pytest.raises(Stop):
        pack(w.lib, w.zips)
    monkeypatch.undo()
    assert os.listdir(w.zips) == ["photos-2021.zip.part"]
    pack(w.lib, w.zips)
    assert sorted(os.listdir(w.zips)) == ["photos-2021.zip", "photos-2022.zip"]
    for z in os.listdir(w.zips):
        with zipfile.ZipFile(os.path.join(w.zips, z)) as zf:
            assert zf.testzip() is None


def test_stray_part_from_a_stopped_collect_is_cleared(w):
    """A copy cut short leaves x.jpg.part; if the next run places x elsewhere (here: the
    source is gone), the stray stayed and failed the volume's pack for good."""
    jpeg(os.path.join(w.src, "a.jpg"), "2023:01:01 10:00:00")
    collect(w.src, w.lib)
    stray = os.path.join(w.lib, "photos", "2023", "2023-01-02_100000_b.jpg.part")
    with open(stray, "wb") as f:
        f.write(b"partial")
    empty = os.path.join(w.root, "empty")
    os.makedirs(empty)
    collect(empty, w.lib)
    assert not os.path.exists(stray)
    pack(w.lib, w.zips)
    assert manifest(w.lib)["volumes"]["photos/2023"].get("sealed")


def test_orphan_in_volume_makes_pack_refuse_cleanly(w):
    jpeg(os.path.join(w.src, "a.jpg"), "2023:01:01 10:00:00")
    collect(w.src, w.lib)
    jpeg(os.path.join(w.lib, "photos", "2023", "orphan.jpg"), seed="orphan")
    pack(w.lib, w.zips)
    rec = manifest(w.lib)["volumes"]["photos/2023"]
    assert not rec.get("sealed") and "orphan.jpg" in rec["failure"]
    assert not os.path.exists(os.path.join(w.zips, "photos-2023.zip"))


def test_pack_stopped_after_sealing_still_lists_every_zip(w, monkeypatch):
    """Pack wrote its upload list only at the end: a stop after sealing some volumes left
    their zips on no list, and the next run, finding them sealed, never listed them."""
    for y in (2021, 2022, 2023):
        jpeg(os.path.join(w.src, f"{y}.jpg"), f"{y}:01:01 10:00:00")
    collect(w.src, w.lib)
    real = pc.Library.save
    n = [0]

    def save(self):
        real(self)
        n[0] += 1
        if n[0] == 2:                                   # after the second volume is sealed
            raise Stop()
    monkeypatch.setattr(pc.Library, "save", save)
    with pytest.raises(Stop):
        pack(w.lib, w.zips)
    monkeypatch.setattr(pc.Library, "save", real)
    pack(w.lib, w.zips)
    listed = set()
    d = os.path.join(w.lib, "_metadata", "uploads")
    for fn in os.listdir(d):
        with open(os.path.join(d, fn), encoding="utf-8", newline="") as f:
            listed |= {os.path.basename(r["path"]) for r in csv.DictReader(f) if r["kind"] == "zip"}
    assert listed == {"photos-2021.zip", "photos-2022.zip", "photos-2023.zip"}
    pack(w.lib, w.zips)                                 # nothing new: no list, nothing listed twice
    assert len(os.listdir(d)) == 1


def test_pack_marks_zips_listed_before_volumes_recorded_it(w):
    """E:\\Kids Photos' 38 zips were listed by 006's first pack, before volumes carried
    upload_listed: the next pack must not list them again."""
    jpeg(os.path.join(w.src, "a.jpg"), "2023:01:01 10:00:00")
    collect(w.src, w.lib)
    pack(w.lib, w.zips)
    m = manifest(w.lib)
    for rec in m["volumes"].values():
        rec.pop("upload_listed", None)
    pc.save_json(os.path.join(w.lib, "_metadata", "manifest.json"), m)
    pack(w.lib, w.zips)
    assert len(os.listdir(os.path.join(w.lib, "_metadata", "uploads"))) == 1
    assert all(r.get("upload_listed") for r in manifest(w.lib)["volumes"].values())


@pytest.mark.parametrize("stop_at", [1, 2])
def test_migrate_live_stopped_then_rerun(w, monkeypatch, stop_at):
    for i in range(2):
        jpeg(os.path.join(w.src, f"IMG_{i}.jpg"), f"2023:06:0{i + 1} 10:00:00")
        mp4(os.path.join(w.src, f"IMG_{i}.mov"), secs=2.0)
    collect(w.src, w.lib)
    m = manifest(w.lib)
    for s, e in m["files"].items():                    # back to the pre-004 layout
        if e["kind"] == "live":
            new = "photos/2023/" + e["path"].rsplit("/", 1)[1]
            os.replace(pc.rel_to_abs(w.lib, e["path"]), pc.rel_to_abs(w.lib, new))
            e["path"] = new
    pc.save_json(os.path.join(w.lib, "_metadata", "manifest.json"), m)
    real = os.rename
    n = [0]

    def rename(a, b):
        n[0] += 1
        if n[0] == stop_at:
            raise Stop()
        return real(a, b)
    monkeypatch.setattr(pc.os, "rename", rename)
    with pytest.raises(Stop):
        run("migrate", "--library", w.lib, "--apply")
    monkeypatch.setattr(pc.os, "rename", real)
    run("migrate", "--library", w.lib, "--apply")
    paths = {e["path"] for e in manifest(w.lib)["files"].values() if e["kind"] == "live"}
    assert paths == {"live/2023/2023-06-01_100000_IMG_0.mov", "live/2023/2023-06-02_100000_IMG_1.mov"}
    assert all(os.path.exists(pc.rel_to_abs(w.lib, p)) for p in paths)


if __name__ == "__main__":
    base = os.path.join(HERE, "..", "out", "pytest-photo-collector")
    os.makedirs(os.path.dirname(base), exist_ok=True)
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider", "--basetemp", base, *sys.argv[1:]]))
