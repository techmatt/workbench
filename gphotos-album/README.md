# gphotos-album

Downloads every item of a Google Photos **shared album**, from all contributors, at original
quality (videos included), into a stage folder that [photo-collector](../photo-collector/README.md)
then collects into the library. The official Photos APIs can't read other people's items in a
shared album, so this drives the installed Google Chrome through Playwright, the way a person
would: one item at a time, with long pauses. Photos are never committed here; the stage and
library are paths passed in.

## How to run

Needs [uv](https://docs.astral.sh/uv/) and Google Chrome (Playwright drives it via
`channel="chrome"`, so no Playwright browser download is needed). From this folder:

```sh
set STAGE=E:\Kids Photos\stage\julia-and-elizabeth
uv run gphotos_album.py list "<share link with ?key=...>" --stage "%STAGE%"
uv run gphotos_album.py download --stage "%STAGE%" --limit 10
cd ..\photo-collector
uv run photo_collector.py collect "%STAGE%" --library "E:\Kids Photos" --exclude _gphotos
cd ..\gphotos-album
uv run gphotos_album.py reconcile --stage "%STAGE%" --library "E:\Kids Photos"
```

- **list** opens the album and reads its item list (about 30 s for 2,482 items): id, owner,
  kind, capture time, size in pixels, and the item's image URL. It fails (exit 2) if it got
  fewer items than the album says it holds. Re-running it adds new items and refreshes the
  URLs of known ones; items gone from the album are marked `removed_from_album`, never deleted.
- **download** fetches items not yet downloaded, in album order (newest first), one at a time,
  `--pause` + up to `--jitter` seconds apart. Re-runs and top-ups fetch only what's missing. An
  item that fails is retried by the next run, up to `--max-attempts`, after every never-tried
  item (so a few items that always fail can't starve the rest); three failures in a row stop
  the run (throttled, expired URLs: run `list` again, then `download`).
- **collect** (photo-collector) copies what the library lacks, by exact bytes, dated and named
  by its usual rules. It accepts a source under `<library>\stage\`. Pass `--exclude _gphotos`.
- **reconcile** marks each downloaded item `in-library` with its library path (by SHA-256),
  and whether this download added it or the library already had those bytes. Each item gets an
  `outcome`: `placed`, `exact-duplicate` (the library already held the bytes),
  `near-duplicate` (photo-collector's `dedup.py` set it aside; `library_path` is the copy kept,
  `set_aside_path` where it went) or `failed`.
- **backfill** (`uv run backfill.py --stage "%STAGE%"`) runs a whole album unattended: chunks
  of `--chunk` 150, each a fresh `list` (URLs never go stale) then `download --limit 150` at the
  usual pace; a chunk that gets nothing waits `--backoff` 15 min, four in a row end the pass;
  then a retry pass for failed items (up to `--retry-attempts` 6). Writes
  `_gphotos\backfill-done.txt` (counts and what still failed) at the end. Start it detached with
  output to a log; it takes most of a day for a 2,500-item album. `--max-items N` makes it a
  chunk: the main pass stops after N new downloads, and the retry pass covers only items this
  run tried.
- **login** (`--profile DIR`) opens a headed Chrome on a persistent profile to sign in once;
  `list`/`download` with the same `--profile` then run signed in. Not needed so far.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `url` (list) | (required) | The album's share link, with its `?key=`. |
| `--stage` | (required) | Download folder for this album: `<owner name>\<file name>`, manifest in `_gphotos\`. |
| `--limit` | 0 (all) | download: at most this many items this run. |
| `--only ID` | none | download: just this item (repeatable). |
| `--pause` / `--jitter` | 20 / 10 | download: seconds between items, plus a random 0 to jitter. |
| `--max-attempts` | 3 | download: skip an item that has failed this often. |
| `--profile DIR` | none (signed out) | Chrome profile folder to run signed in. |
| `--library` (reconcile) | (required) | photo-collector library root. |

## How items are fetched

Signed out, the share key is enough to list and download everything.

- **photo, video**: the web app's own Download (Shift+D on the item page): photos come from
  the item's `lh3.googleusercontent.com/...=s0-d-ip` URL, videos from
  `video-downloads.googleusercontent.com`, each with the uploader's file name. If that fails,
  the direct URL (`=s0-d-ip`, or `=dv` for a video) is tried once.
- **motion-photo**: signed out, the web app's Download fails for these (video-downloads answers
  HTTP 500), so the still comes from `=s0-d-ip`. A Pixel motion photo (`PXL_....MP.jpg`) has its
  clip inside the file and is done. An iPhone Live Photo isn't: its clip comes from `=dv`
  (`IMG_0176.mov`) and is saved beside the still under the same stem, so photo-collector pairs
  them (`live/`).
- Each owner gets a folder, so two phones' `IMG_0350.MOV` never meet; a name another item already
  holds in that folder gets `_<last 8 of the item id>`.
- Failures are recorded in the manifest (`error`) and the page saved as
  `_gphotos\failed-<id>.html`. Never as a screenshot: collect would take a `.png` for a photo.

## Manifest

`<stage>\_gphotos\manifest.json`: `album` (id, key, title, `expected_items`, `listed_items`,
owners by id) and `items` by item id: `owner`, `kind` (photo, video, motion-photo),
`taken` (wall time where taken), `width`/`height`, `video_ms`, `album_index`, `status`
(`listed`, `downloaded`, `failed`, `in-library`), `filename`, `path` (in the stage), `size`,
`sha256`, `route` (`ui`, `direct`, `direct+embedded`, `direct+clip`), `seconds`, `attempts`,
`error`, `clip` (path, size, sha256 of a Live Photo's clip), and after reconcile
`library_path`, `library_new`.

## Quirks of this machine

- **"Julia and Elizabeth"** (2026-11-10): 2,482 items from 10 contributors (Daiany Silva
  1,160, Matthew Fisher 744, Spaz Monkey 299, then 89 or fewer each), 1,996 photos, 178 motion
  photos, 308 videos (3.2 hours of footage, longest 9.8 min; 24 in 4K), taken 2025-08-01 to
  2026-11-08. Stage `E:\Kids Photos\stage\julia-and-elizabeth`. Fully downloaded and collected
  (2026-11-11): every item is `in-library` except 10 `failed` iPhone motion photos (below).
- **iPhone motion photos whose clip won't come**: for 10 of this album's 178 motion photos,
  `=dv` answers an HTTP error on every try, so the item stays `failed` although its still
  downloaded and was collected (its `sha256` is in the library); only the Live clip is missing.
  Signed in (`--profile`) is the untried route.
- **Pace**: 31-35 s per item wall at the default pause (photos 4-8 s of handling including page
  load, then the 20-30 s pause; a 0.7 GB video ~60 s; ~27 MB/s for video), so a 2,500-item
  album takes about 22 h. No throttling seen signed out at that pace.
- **Chunking**: `backfill.py --max-items N` (about 630 here) keeps a restart cheap; after each
  chunk run collect, `dedup.py` scan/plan/apply `--unseal`, then reconcile. Run reconcile
  *after* `dedup.py apply`, so near duplicates get their outcome. Collect plus dedup plus
  reconcile takes a few minutes per chunk. Collecting from the stage alongside the download is
  safe (`.part` files are skipped).
- **Overlap with `D:\Julia Photos`**: many album items were already in the library from there,
  either as the same bytes (`exact-duplicate`) or as a re-encoded copy (same name, size and
  capture time; HEIC-named JPEG bytes) that dedup sets aside, keeping the album copy unless the
  other carries a Live clip.
- **Disk**: stage plus library copies need about twice the album's size on E: until the stage is
  retired (`photo_collector.py retire "<stage>" --library ... --exclude _gphotos --delete`).
