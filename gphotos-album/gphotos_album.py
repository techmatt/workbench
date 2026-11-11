# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.50"]
# ///
"""Download a Google Photos shared album, every contributor's items at original quality.

    uv run gphotos_album.py list     "<share link>" --stage "E:\\Kids Photos\\stage\\<album>"
    uv run gphotos_album.py download --stage "E:\\Kids Photos\\stage\\<album>" [--limit 10]
    uv run gphotos_album.py reconcile --stage "..." --library "E:\\Kids Photos"

Drives the installed Google Chrome through Playwright, signed out (the link's share key is
enough) unless --profile is given. One download at a time, as the Photos web app's own
Download (Shift+D) gives it: the uploader's original file and name. State is a per-item
manifest at <stage>/_gphotos/manifest.json, so re-runs and top-ups fetch only new items.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

META = "_gphotos"
SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DONE = ("downloaded", "in-library")


def log(*a):
    print(time.strftime("%I:%M:%S%p").lstrip("0").lower(), *a, flush=True)


# --------------------------------------------------------------------------- manifest

def manifest_path(stage: str) -> str:
    return os.path.join(stage, META, "manifest.json")


def load(stage: str) -> dict:
    p = manifest_path(stage)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"album": {}, "items": {}}


def save(stage: str, m: dict):
    p = manifest_path(stage)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, ensure_ascii=False)
    os.replace(p + ".tmp", p)


# --------------------------------------------------------------------------- browser

def browser(p, args, headless=True):
    """Installed Chrome (channel=chrome). Signed out in a throwaway context unless --profile."""
    if args.profile:
        ctx = p.chromium.launch_persistent_context(args.profile, channel="chrome",
                                                   headless=headless, accept_downloads=True)
        return None, ctx
    b = p.chromium.launch(channel="chrome", headless=headless)
    return b, b.new_context(accept_downloads=True)


def close(b, ctx):
    ctx.close()
    if b:
        b.close()


# --------------------------------------------------------------------------- listing

def album_from_html(html: str):
    """The album page embeds its first page of items (AF_initDataCallback): [_, items, token, album]."""
    for m in re.finditer(r"AF_initDataCallback\(\{key: '(ds:\d+)'.*?data:(.*?), sideChannel", html, re.S):
        try:
            d = json.loads(m.group(2))
        except ValueError:
            continue
        if (isinstance(d, list) and len(d) > 3 and isinstance(d[1], list) and isinstance(d[3], list)
                and len(d[3]) > 21 and isinstance(d[3][1], str)):
            return d
    return None


def pages_from_rpc(body: str):
    """snAcKc (album page) payloads in a batchexecute response."""
    out = []
    for line in body.splitlines():
        if not line.startswith("["):
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        for e in chunk:
            if isinstance(e, list) and len(e) > 2 and e[:2] == ["wrb.fr", "snAcKc"] and e[2]:
                out.append(json.loads(e[2]))
    return out


def owner_names(album: list) -> dict:
    names = {}
    for mem in album[9] or []:
        if not (isinstance(mem, list) and mem and isinstance(mem[0], str)):
            continue
        name = mem[2] if len(mem) > 2 else None
        if not name and len(mem) > 11 and isinstance(mem[11], list) and mem[11]:
            name = mem[11][0]
        names[mem[0]] = name or mem[0]
    return names


def item_record(it: list) -> dict:
    extra = it[-1] if isinstance(it[-1], dict) else {}
    kind = "video" if "76647426" in extra else "motion-photo" if "146008172" in extra else "photo"
    taken = dt.datetime.fromtimestamp((it[2] + (it[4] or 0)) / 1000, dt.timezone.utc).replace(tzinfo=None)
    return {
        "id": it[0],
        "owner_id": it[6][0] if isinstance(it[6], list) and it[6] else None,
        "kind": kind,
        "taken": taken.isoformat(timespec="seconds"),          # wall time where it was taken
        "width": it[1][1], "height": it[1][2],
        "video_ms": extra["76647426"][0] if kind == "video" else None,
        "base_url": it[1][0],     # lh3 URL; +"=s0-d-ip" is the original still (refreshed by list)
    }


def cmd_list(args):
    from playwright.sync_api import sync_playwright
    q = parse_qs(urlparse(args.url).query)
    album_id = urlparse(args.url).path.rstrip("/").split("/")[-1]
    key = q.get("key", [None])[0]
    m = load(args.stage)
    pages, seen_tokens = [], set()

    with sync_playwright() as p:
        b, ctx = browser(p, args)
        page = ctx.new_page()

        def on_route(route):
            # fetch it ourselves so the body can be read (a response event's body can be gone)
            resp = route.fetch()
            try:
                pages.extend(pages_from_rpc(resp.text()))
            except Exception as e:   # noqa: BLE001 - a lost page shows up in the count check
                log("could not read a listing response:", e)
            route.fulfill(response=resp)
        page.route(re.compile(r"batchexecute\?rpcids=snAcKc"), on_route)
        page.goto(args.url, wait_until="load")
        time.sleep(4)
        first = album_from_html(page.content())
        if not first:
            close(b, ctx)
            sys.exit("no album data on the page (signed out and the link needs sign-in? try --profile)")
        album = first[3]
        expected = album[21]
        got: dict[str, list] = {}
        order: list[str] = []

        def take(d):
            for it in d[1] or []:
                if it[0] not in got:
                    got[it[0]] = it
                    order.append(it[0])
            return d[2]
        token = take(first)
        log(f"album {album[1]!r}: {expected} items; first page {len(order)}")
        stall = 0
        while token and stall < 15:
            n0 = len(order)
            page.mouse.move(600, 500)
            for _ in range(5):
                page.mouse.wheel(0, 4000)
                time.sleep(0.4)
            page.keyboard.press("End")
            time.sleep(2)
            while pages:
                d = pages.pop(0)
                if d[2] in seen_tokens:
                    continue
                token = take(d)
                if token:
                    seen_tokens.add(token)
            stall = 0 if len(order) > n0 else stall + 1
            if len(order) > n0:
                log(f"listed {len(order)} / {expected}")
        close(b, ctx)

    names = owner_names(album)
    m["album"] = {"id": album_id, "key": key, "url": args.url, "title": album[1],
                  "expected_items": expected, "listed_items": len(order),
                  "listed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "owners": names}
    items = m["items"]
    new = 0
    for i, iid in enumerate(order):
        rec = item_record(got[iid])
        rec["owner"] = names.get(rec["owner_id"], rec["owner_id"])
        rec["album_index"] = i
        if iid in items:
            items[iid].update({k: rec[k] for k in ("album_index", "owner", "kind", "base_url")})
        else:
            rec.update({"status": "listed", "filename": None, "path": None, "size": None,
                        "sha256": None, "attempts": 0, "error": None})
            items[iid] = rec
            new += 1
    gone = [iid for iid in items if iid not in got]
    for iid in gone:
        items[iid]["removed_from_album"] = True
    save(args.stage, m)
    print(f"listed {len(order)} of {expected} the album reports; {new} new to the manifest; "
          f"{len(gone)} in the manifest no longer listed")
    by_owner: dict[str, int] = {}
    for iid in order:
        by_owner[items[iid]["owner"]] = by_owner.get(items[iid]["owner"], 0) + 1
    for o, n in sorted(by_owner.items(), key=lambda x: -x[1]):
        print(f"  {n:6}  {o}")
    if len(order) != expected:
        print(f"WARNING: listing incomplete ({len(order)} of {expected}); re-run list")
        sys.exit(2)


# --------------------------------------------------------------------------- download

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def held_paths(items: dict, but: str) -> set[str]:
    out = set()
    for o in items.values():
        if o["id"] != but:
            out |= {p.lower() for p in (o.get("path"), (o.get("clip") or {}).get("path")) if p}
    return out


def target_for(stage: str, items: dict, rec: dict, name: str) -> str:
    """<owner>/<name>; a name another item already holds in that folder gets _<id tail>."""
    owner = SAFE.sub("_", rec["owner"] or "unknown").strip(" .") or "unknown"
    name = SAFE.sub("_", name).strip(" ") or rec["id"]
    rel = f"{owner}/{name}"
    if rel.lower() in held_paths(items, rec["id"]) or (
            os.path.exists(os.path.join(stage, rel)) and rec.get("path") != rel):
        stem, ext = os.path.splitext(name)
        rel = f"{owner}/{stem}_{rec['id'][-8:]}{ext}"
    return rel


def has_embedded_motion(path: str) -> bool:
    """A Pixel/Samsung motion photo carries its clip inside the JPEG (XMP names it)."""
    with open(path, "rb") as f:
        head = f.read(512 * 1024)
    return b"MotionPhoto" in head or b"MicroVideo" in head


def ui_download(page, item_url: str):
    """What the web app's Download (Shift+D) gives: photos via lh3 =s0-d-ip, videos via
    video-downloads.googleusercontent.com (the uploaded file and its name)."""
    page.goto(item_url, wait_until="load", timeout=60000)
    time.sleep(3)
    with page.expect_download(timeout=60000) as di:
        page.keyboard.press("Shift+D")
    return di.value


def direct_download(page, url: str):
    """Navigate to an attachment URL; Chrome streams it to disk as a download."""
    from playwright.sync_api import Error as PwError
    with page.expect_download(timeout=60000) as di:
        try:
            page.goto(url, timeout=60000)
        except PwError as e:
            if "Download is starting" not in str(e):
                raise
    return di.value


def save_download(stage: str, rel: str, d) -> dict:
    dest = os.path.join(stage, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    d.save_as(dest + ".part")           # waits for the download to finish
    if d.failure():
        raise RuntimeError(f"download failed: {d.failure()}")
    size = os.path.getsize(dest + ".part")
    if size == 0:
        raise RuntimeError("empty download")
    os.replace(dest + ".part", dest)
    return {"filename": d.suggested_filename, "path": rel, "size": size, "sha256": sha256(dest)}


def fetch_item(page, stage: str, items: dict, rec: dict, item_url: str) -> str:
    """Download one item into the stage; fills rec. Returns the route used.

    photo, video: the web app's Download; if that fails, the direct URL (=s0-d-ip / =dv).
    motion-photo: signed out, the web app's Download fails (video-downloads answers 500), so
      the still comes from =s0-d-ip (the same URL the app uses for photos). A Pixel-style file
      has its clip inside; an iPhone Live Photo doesn't, and its clip (=dv) is saved beside it
      under the same stem, so photo-collector pairs the two.
    """
    route = "ui"
    if rec["kind"] == "motion-photo":
        route = "direct"
        d = direct_download(page, rec["base_url"] + "=s0-d-ip")
    else:
        try:
            d = ui_download(page, item_url)
        except Exception as e:   # noqa: BLE001 - fall back to the direct URL once
            log(f"  web-app download failed ({str(e).splitlines()[0][:120]}); trying the direct URL")
            route = "direct"
            d = direct_download(page, rec["base_url"] + ("=dv" if rec["kind"] == "video" else "=s0-d-ip"))
    main = save_download(stage, target_for(stage, items, rec, d.suggested_filename), d)
    rec.update(main)
    rec["clip"] = None
    if rec["kind"] == "motion-photo":
        if has_embedded_motion(os.path.join(stage, main["path"])):
            route += "+embedded"
        else:
            time.sleep(3)
            c = direct_download(page, rec["base_url"] + "=dv")
            stem = os.path.splitext(main["path"])[0]
            ext = os.path.splitext(c.suggested_filename)[1].lower() or ".mov"
            rec["clip"] = save_download(stage, stem + ext, c)
            route += "+clip"
    return route


def cmd_download(args):
    from playwright.sync_api import sync_playwright
    m = load(args.stage)
    a, items = m["album"], m["items"]
    if not a:
        sys.exit("no manifest: run list first")
    todo = sorted((r for r in items.values()
                   if r["status"] not in DONE and not r.get("removed_from_album")
                   and r["attempts"] < args.max_attempts),
                  key=lambda r: r["album_index"])
    if args.only:
        todo = [r for r in todo if r["id"] in args.only]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(todo)} to download; pause {args.pause}-{args.pause + args.jitter} s between items")
    fails = 0
    t_start, bytes_done = time.time(), 0
    with sync_playwright() as p:
        b, ctx = browser(p, args)
        page = ctx.new_page()
        for n, rec in enumerate(todo, 1):
            url = f"https://photos.google.com/share/{a['id']}/photo/{rec['id']}?key={a['key']}"
            rec["attempts"] += 1
            t0 = time.time()
            try:
                route = fetch_item(page, args.stage, items, rec, url)
                size = rec["size"] + ((rec.get("clip") or {}).get("size") or 0)
                rec.update({"status": "downloaded", "route": route, "error": None,
                            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "seconds": round(time.time() - t0, 1)})
                bytes_done += size
                fails = 0
                clip = f" + {rec['clip']['path']}" if rec.get("clip") else ""
                log(f"[{n}/{len(todo)}] {rec['path']}{clip} {size / 1e6:.1f} MB {rec['kind']} "
                    f"({route}) in {time.time() - t0:.1f} s")
            except Exception as e:   # noqa: BLE001 - one item's failure never stops the run
                fails += 1
                rec.update({"status": "failed", "error": str(e).splitlines()[0][:300]})
                log(f"[{n}/{len(todo)}] {rec['id']} FAILED: {rec['error']}")
                try:   # the page as HTML, never a screenshot: collect would take a .png for a photo
                    with open(os.path.join(args.stage, META, f"failed-{rec['id']}.html"), "w",
                              encoding="utf-8") as fh:
                        fh.write(f"<!-- {page.url} -->\n" + page.content())
                except Exception:   # noqa: BLE001
                    pass
            save(args.stage, m)
            if fails >= 3:
                log("3 failures in a row: stopping (throttled, links expired, or the page changed); "
                    "re-run list, then download")
                break
            if n < len(todo):
                time.sleep(args.pause + random.uniform(0, args.jitter))
        close(b, ctx)
    el = time.time() - t_start
    log(f"done: {bytes_done / 1e6:.1f} MB in {el / 60:.1f} min")
    counts: dict[str, int] = {}
    for r in items.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("manifest:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


# --------------------------------------------------------------------------- reconcile

def cmd_reconcile(args):
    """Mark downloaded items found in photo-collector's library manifest (after its collect)."""
    m = load(args.stage)
    with open(os.path.join(args.library, "_metadata", "manifest.json"), encoding="utf-8") as f:
        lib = json.load(f)["files"]
    stage = os.path.abspath(args.stage)

    def find(f: dict):
        """(library entry, whether this download is what added it) by the file's SHA-256."""
        e = lib.get(f["sha256"])
        if not e:
            return None, None
        mine = os.path.normcase(os.path.join(stage, f["path"].replace("/", os.sep)))
        first = os.path.normcase(e["sources"][0]) if e.get("sources") else ""
        return e, first == mine

    n = {"in-library": 0, "not yet collected": 0}
    for r in m["items"].values():
        if r["status"] not in DONE or not r.get("path"):
            continue
        e, new = find(r)
        clip = r.get("clip")
        ce, cnew = find(clip) if clip else (None, None)
        if not e or (clip and not ce):
            r["status"] = "downloaded"
            n["not yet collected"] += 1
            continue
        r.update({"status": "in-library", "library_path": e["path"], "library_new": new,
                  "library_date_source": e.get("date_source")})
        if clip:
            clip.update({"library_path": ce["path"], "library_new": cnew})
        n["in-library"] += 1
    save(args.stage, m)
    print(", ".join(f"{k} {v}" for k, v in n.items()))
    for r in sorted(m["items"].values(), key=lambda r: r["album_index"]):
        if r["status"] == "in-library":
            for f in (r, r.get("clip")):
                if f:
                    print(f"  {f['path']:50} -> {f['library_path']}"
                          f"{'' if f['library_new'] else '  (already in the library)'}")


# --------------------------------------------------------------------------- login

def cmd_login(args):
    """Headed window on the persistent --profile, for a one-time sign-in; waits until closed."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        _, ctx = browser(p, args, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/")
        log("sign in in the window, then close it")
        page.wait_for_event("close", timeout=0)
        ctx.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "download", "reconcile", "login"):
        sp = sub.add_parser(name)
        if name == "list":
            sp.add_argument("url", help="the album's share link (with ?key=...)")
        if name != "login":
            sp.add_argument("--stage", required=True, help="download folder for this album")
        sp.add_argument("--profile", help="Chrome profile folder: use it (signed in) instead of signed out")
        if name == "download":
            sp.add_argument("--limit", type=int, default=0, help="at most this many items (0: all)")
            sp.add_argument("--only", action="append", metavar="ID", help="just this item (repeatable)")
            sp.add_argument("--pause", type=float, default=20, help="seconds between items")
            sp.add_argument("--jitter", type=float, default=10, help="extra random seconds, 0 to this")
            sp.add_argument("--max-attempts", type=int, default=3, help="skip an item failed this often")
        if name == "reconcile":
            sp.add_argument("--library", required=True)
    args = ap.parse_args()
    if args.cmd == "login" and not args.profile:
        ap.error("login needs --profile")
    {"list": cmd_list, "download": cmd_download, "reconcile": cmd_reconcile, "login": cmd_login}[args.cmd](args)


if __name__ == "__main__":
    main()
