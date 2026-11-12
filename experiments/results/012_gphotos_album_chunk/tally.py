"""Read-only tallies for the Julia and Elizabeth backfill (009-012).

    python -I out/tally.py
"""
import collections
import csv
import glob
import json
import os

STAGE = r"E:\Kids Photos\stage\julia-and-elizabeth"
LIB = r"E:\Kids Photos"
# album_index ranges per item (009 got 0-586 before the restart)
CHUNKS = [("009", 0, 586), ("010", 587, 1216), ("011", 1217, 1849), ("012", 1850, 2481)]
# dedup apply runs per item (009's two plans were empty)
RUNS = {"009": ["20261110-220129"], "010": ["20261111-101055"], "011": ["20261111-154212"]}
DONE_BEFORE_012 = "20261111-154212"

C = collections.Counter
sm = json.load(open(os.path.join(STAGE, "_gphotos", "manifest.json"), encoding="utf-8"))
items = list(sm["items"].values())
lm = json.load(open(os.path.join(LIB, "_metadata", "manifest.json"), encoding="utf-8"))

print("ALBUM", sm["album"].get("expected_items"), "expected,", sm["album"].get("listed_items"), "listed,",
      len(items), "in manifest,", sum(bool(r.get("removed_from_album")) for r in items), "removed_from_album")
print("status:", dict(C(r["status"] for r in items)))
print("outcome:", dict(C(r.get("outcome") or r["status"] for r in items)))
print("kinds:", dict(C(r["kind"] for r in items)))
print("GB downloaded:", round(sum(r.get("size") or 0 for r in items) / 1e9, 2),
      "+ clips", round(sum((r.get("clip") or {}).get("size", 0) for r in items) / 1e9, 2))
print()
for name, lo, hi in CHUNKS:
    ch = [r for r in items if lo <= r["album_index"] <= hi]
    print(f"{name} items {lo}-{hi}: {len(ch)}  status {dict(C(r['status'] for r in ch))}")
    print("   outcome", dict(C(r.get("outcome") or r["status"] for r in ch)),
          " kinds", dict(C(r["kind"] for r in ch)))
    print("   clip outcomes", dict(C((r.get("clip") or {}).get("outcome") for r in ch if r.get("clip"))),
          " attempts>1", sum(r["attempts"] > 1 for r in ch),
          " GB", round(sum(r.get("size") or 0 for r in ch) / 1e9, 2),
          " handling s mean", round(sum(r.get("seconds") or 0 for r in ch) / max(1, sum(bool(r.get("seconds")) for r in ch)), 1))
    bad = [r for r in ch if r["status"] not in ("downloaded", "in-library")]
    for r in bad[:40]:
        print("   NOT DONE", r["id"], r["kind"], r["owner"], "attempts", r["attempts"], r.get("error"))

print()
removed = sorted(glob.glob(os.path.join(LIB, "_metadata", "dedup", "*-removed.csv")))
runs12 = [os.path.basename(p)[:15] for p in removed if os.path.basename(p)[:15] > DONE_BEFORE_012]
RUNS["012"] = runs12
stage_l = STAGE.lower()
aside = lm["set_aside"]
files = lm["files"]
for name, runs in [(k, RUNS[k]) for k in ("009", "010", "011", "012")]:
    rows = []
    for run in runs:
        rows += list(csv.DictReader(open(os.path.join(LIB, "_metadata", "dedup", f"{run}-removed.csv"), encoding="utf-8")))
    def side(sha):
        e = aside.get(sha) or files.get(sha) or {}
        return "album" if any(s.lower().startswith(stage_l) for s in e.get("sources", [])) else "other"
    print(f"dedup {name} runs {runs}: removed {len(rows)}  classes {dict(C(r['class'] for r in rows))}")
    print("   removed/kept side", dict(C((side(r['sha256']), side(r['kept_sha256'])) for r in rows)))

print()
sealed = {k for k, v in lm["volumes"].items() if v.get("sealed")}
folders = []
for top in ("photos", "videos", "live"):
    for d in sorted(os.listdir(os.path.join(LIB, top))):
        if top == "videos" and d == "large":
            continue
        if os.path.isdir(os.path.join(LIB, top, d)):
            folders.append(f"{top}/{d}")
uns = [f for f in folders if f not in sealed]
print(f"volumes: {len(folders)} folders, {len(sealed)} sealed, {len(uns)} unsealed:")
for f in uns:
    n = sum(1 for e in files.values() if e["path"].startswith(f + "/"))
    print("  ", f, "(was sealed, unsealed by " + lm["unsealed"][f]["unsealed_by"] + ")" if f in lm["unsealed"] else "(never sealed)", n, "files")
large = [e for e in files.values() if e["path"].startswith("videos/large/")]
print("videos/large:", len(large), "files,", sum(not e.get("upload_listed") for e in large), "not yet upload-listed",
      round(sum(e["size"] for e in large if not e.get("upload_listed")) / 1e9, 2), "GB")
print("library files", len(files), "set_aside", len(aside))
print()
print("failed items: is the still in the library?")
for r in sorted(items, key=lambda r: r["album_index"]):
    if r["status"] == "failed":
        e = files.get(r.get("sha256") or "")
        a = aside.get(r.get("sha256") or "")
        where = e["path"] if e else ("set aside, kept " + a.get("kept_path", "?")) if a else "NOT in library"
        print(f"  {r['album_index']} {r['owner']}/{r['filename']} attempts {r['attempts']}: {where}")
print()
ch = [r for r in items if r["album_index"] >= 1850]
print("012 exact-duplicate first source:", dict(C(
    (files.get(r["sha256"]) or {}).get("sources", ["?"])[0].split("\\")[0:2][-1] for r in ch if r.get("outcome") == "exact-duplicate")))
print("012 exact-duplicate by owner/kind:", dict(C((r["owner"], r["kind"]) for r in ch if r.get("outcome") == "exact-duplicate")))
print("012 placed by owner:", dict(C(r["owner"] for r in ch if r.get("outcome") == "placed")))
print("012 clip outcomes:", dict(C((r.get("clip") or {}).get("outcome") for r in ch if r.get("clip"))))
