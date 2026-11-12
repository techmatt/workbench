"""Download the album items never tried yet, skipping ones that already failed.

backfill.py's chunks start with the failed items (album order) and stop on three failures in
a row, so a run of unfetchable clips at the front starves everything behind it. Here every
download uses --max-attempts 1, so only never-tried items (attempts 0) are taken; a fresh
`list` before each chunk keeps URLs current. Then items that failed in this run get one
retry each, singly (--only), so one bad item can't stop the others.

    python -I out/drive_rest.py "<stage>"
"""
import json
import os
import subprocess
import sys
import time

STAGE = sys.argv[1]
TOOL = r"C:\Code\workbench-worktrees\workbench\012_gphotos_album_chunk\gphotos-album\gphotos_album.py"
MAN = os.path.join(STAGE, "_gphotos", "manifest.json")
DONE = ("downloaded", "in-library")


def log(*a):
    print(time.strftime("%I:%M:%S%p").lstrip("0").lower(), *a, flush=True)


def items():
    with open(MAN, encoding="utf-8") as f:
        return json.load(f)["items"]


def run(*args):
    log("$", " ".join(args))
    return subprocess.run(["uv", "run", TOOL, *args], cwd=os.path.dirname(TOOL)).returncode


def untried():
    return [r for r in items().values() if r["status"] == "listed" and r["attempts"] == 0
            and not r.get("removed_from_album")]


start = {i: r["attempts"] for i, r in items().items()}
url = None
with open(MAN, encoding="utf-8") as f:
    url = json.load(f)["album"]["url"]
idle = 0
while untried():
    before = sum(r["status"] in DONE for r in items().values())
    run("list", url, "--stage", STAGE)
    run("download", "--stage", STAGE, "--limit", "150", "--max-attempts", "1")
    got = sum(r["status"] in DONE for r in items().values()) - before
    log(f"chunk got {got}; {len(untried())} never tried left")
    if got == 0:
        idle += 1
        if idle >= 4:
            log("4 chunks in a row got nothing; stopping")
            break
        time.sleep(15 * 60)
    else:
        idle = 0

# one more try each for items this run tried and didn't get (singly)
retry = [i for i, r in items().items() if r["status"] not in DONE and r["attempts"] > start.get(i, 0)]
if retry:
    log(f"retrying {len(retry)} singly in 5 min")
    time.sleep(300)
    run("list", url, "--stage", STAGE)
    for i in retry:
        run("download", "--stage", STAGE, "--only", i, "--max-attempts", "6")
        time.sleep(25)

left = [r for r in items().values() if r["status"] not in DONE]
lines = [f"drive_rest finished {time.strftime('%Y-%m-%d %I:%M%p')}: "
         f"{sum(r['status'] in DONE for r in items().values())} downloaded, {len(left)} not"]
lines += [f"  {r['album_index']}  {r['id']}  {r['kind']}  {r['owner']}  {r.get('filename')}  "
          f"attempts {r['attempts']}  {r.get('error')}" for r in sorted(left, key=lambda r: r["album_index"])]
log("\n".join(lines))
with open(os.path.join(STAGE, "_gphotos", "drive-rest-done.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
