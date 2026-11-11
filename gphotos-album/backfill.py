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


def todo(stage, max_attempts, only=None):
    return [r for r in manifest(stage)["items"].values()
            if r["status"] not in DONE and not r.get("removed_from_album") and r["attempts"] < max_attempts
            and (only is None or r["id"] in only)]


def attempts(stage):
    return {i: r["attempts"] for i, r in manifest(stage)["items"].items()}


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


def pass_(stage, a, max_attempts, label, only=None, start=None):
    """One pass. With --max-items, the main pass stops once this run has downloaded that
    many (`start`: n_done when the run began); `only` limits a pass to those item ids."""
    idle = 0
    while todo(stage, max_attempts, only):
        limit = a.chunk
        if a.max_items and start is not None:
            limit = min(limit, a.max_items - (n_done(stage) - start))
            if limit <= 0:
                log(f"{label}: {a.max_items} downloaded this run; stopping (--max-items)")
                return
        relist(stage)
        before = n_done(stage)
        extra = [x for i in sorted(only) for x in ("--only", i)] if only is not None else []
        run("download", "--stage", stage, "--limit", str(limit), "--pause", str(a.pause),
            "--jitter", str(a.jitter), "--max-attempts", str(max_attempts), *extra)
        got = n_done(stage) - before
        left = len(todo(stage, max_attempts, only))
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
    ap.add_argument("--max-items", type=int, default=0,
                    help="download at most this many items this run (0: the whole album); the retry "
                         "pass then covers only items this run tried")
    a = ap.parse_args()
    stage = os.path.abspath(a.stage)
    start, tried0 = n_done(stage), attempts(stage)

    pass_(stage, a, 3, "main pass", start=start)
    only = None
    if a.max_items:   # retry only what this run tried and didn't get
        only = {i for i, n in attempts(stage).items() if n > tried0.get(i, 0)}
    if todo(stage, a.retry_attempts, only):
        log(f"retry pass for {len(todo(stage, a.retry_attempts, only))} items in {a.retry_wait} min")
        time.sleep(a.retry_wait * 60)
        pass_(stage, a, a.retry_attempts, "retry pass", only=only)
    items = manifest(stage)["items"].values()
    left = [r for r in items if r["status"] not in DONE and not r.get("removed_from_album")]
    lines = [f"finished {time.strftime('%Y-%m-%d %I:%M%p')}: {n_done(stage)} downloaded "
             f"({n_done(stage) - start} this run), {len(left)} not"]
    if only is not None:
        lines[0] += f"; of this run's {len(only)} items tried, still failing:"
        left = [r for r in left if r["id"] in only]
    lines += [f"  {r['id']}  {r['kind']}  {r['owner']}  attempts {r['attempts']}  {r.get('error')}" for r in left]
    log("\n".join(lines))
    with open(os.path.join(stage, "_gphotos", "backfill-done.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
