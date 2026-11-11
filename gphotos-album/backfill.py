# /// script
# requires-python = ">=3.11"
# ///
"""Unattended full download of an album already listed into a stage, in chunks.

    uv run backfill.py --stage "E:\\Kids Photos\\stage\\<album>" [--chunk 150]

Each chunk refreshes the listing (`list`, so image URLs never go stale) and then runs
`download --limit <chunk>` at gphotos_album.py's own pace. A chunk that downloads nothing
(throttled, page changed) waits `--backoff` minutes before the next; `--give-up` such chunks
in a row end the main pass. Then failed items get a retry pass (`--retry-attempts`), after
a pause, and whatever is still not downloaded is printed. Writes `<stage>/_gphotos/backfill-done.txt`
at the end, so a waiter can watch for it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DONE = ("downloaded", "in-library")


def log(*a):
    print(time.strftime("%I:%M:%S%p").lstrip("0").lower(), *a, flush=True)


def manifest(stage):
    with open(os.path.join(stage, "_gphotos", "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def todo(stage, max_attempts):
    return [r for r in manifest(stage)["items"].values()
            if r["status"] not in DONE and not r.get("removed_from_album") and r["attempts"] < max_attempts]


def n_done(stage):
    return sum(r["status"] in DONE for r in manifest(stage)["items"].values())


def run(*args):
    cmd = ["uv", "run", os.path.join(HERE, "gphotos_album.py"), *args]
    log("$", " ".join(args))
    return subprocess.run(cmd, cwd=HERE).returncode


def relist(stage):
    url = manifest(stage)["album"]["url"]
    for _ in range(3):
        if run("list", url, "--stage", stage) == 0:
            return
        time.sleep(60)
    log("listing incomplete three times; carrying on with what the manifest has")


def pass_(stage, a, max_attempts, label):
    idle = 0
    while todo(stage, max_attempts):
        relist(stage)
        before = n_done(stage)
        run("download", "--stage", stage, "--limit", str(a.chunk), "--pause", str(a.pause),
            "--jitter", str(a.jitter), "--max-attempts", str(max_attempts))
        got = n_done(stage) - before
        left = len(todo(stage, max_attempts))
        log(f"{label}: chunk got {got}; {n_done(stage)} done, {left} still to try")
        if got == 0:
            idle += 1
            if idle >= a.give_up:
                log(f"{label}: {idle} chunks in a row got nothing; ending this pass")
                return
            log(f"waiting {a.backoff} min before the next chunk")
            time.sleep(a.backoff * 60)
        else:
            idle = 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--chunk", type=int, default=150)
    ap.add_argument("--pause", type=float, default=20)
    ap.add_argument("--jitter", type=float, default=10)
    ap.add_argument("--backoff", type=float, default=15, help="minutes to wait after a chunk that got nothing")
    ap.add_argument("--give-up", type=int, default=4, help="chunks in a row that get nothing before a pass ends")
    ap.add_argument("--retry-attempts", type=int, default=6, help="attempt cap for the retry pass")
    ap.add_argument("--retry-wait", type=float, default=10, help="minutes between the main and retry pass")
    a = ap.parse_args()
    stage = os.path.abspath(a.stage)

    pass_(stage, a, 3, "main pass")
    if todo(stage, a.retry_attempts):
        log(f"retry pass for {len(todo(stage, a.retry_attempts))} items in {a.retry_wait} min")
        time.sleep(a.retry_wait * 60)
        pass_(stage, a, a.retry_attempts, "retry pass")
    items = manifest(stage)["items"].values()
    left = [r for r in items if r["status"] not in DONE and not r.get("removed_from_album")]
    lines = [f"finished {time.strftime('%Y-%m-%d %I:%M%p')}: {n_done(stage)} downloaded, {len(left)} not"]
    lines += [f"  {r['id']}  {r['kind']}  {r['owner']}  attempts {r['attempts']}  {r.get('error')}" for r in left]
    log("\n".join(lines))
    with open(os.path.join(stage, "_gphotos", "backfill-done.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
