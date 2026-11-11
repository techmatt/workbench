# photo-collector

Gathers photos and videos of Matt's kids, downloaded from many sources (Google Photos albums
and the like), into one library that holds a single copy of each file, sorted by year and split
into photos and videos. Runs are incremental: point it at a new download and it adds only what
the library lacks. Live Photo clips get a stream of their own. Photos and videos are never committed
here; the source and library folders are passed in.

## How to run

Needs [uv](https://docs.astral.sh/uv/); dependencies are declared inline.

```sh
cd photo-collector
uv run photo_collector.py collect "<source>" --library "E:\Kids Photos" [--exclude ...]
uv run photo_collector.py retire  "<source>" --library "E:\Kids Photos" [--exclude ...] [--delete]
uv run photo_collector.py pack --library "E:\Kids Photos"
```

**collect** copies every photo and video in the source that the library doesn't hold yet. The
source is only read, zips in place. A file or zip member that can't be read is skipped with a
warning and listed as `unreadable` in the run's not-copied list. A run first removes any `.part`
file a stopped run left in an unsealed volume, so the stray can't fail that volume's pack.

**retire** is a dry run unless given `--delete`. It re-hashes every source file (and every media
member of every zip), re-reads the library copy for each hash rather than trusting the manifest,
and writes `_metadata/retire/<time>-<source name>.csv`: one row per source file, `delete`
(verified) or `keep` with the reason (`no library copy`, `skipped type .xyz`, `excluded`, `hash
mismatch in library copy`, `library copy missing`, `unreadable (...)`, `unreadable zip`). Zip
members are read by position. A zip is `delete` only when every member is media that verified;
otherwise it is `keep` and the reason names the members that would be lost (all of them in
`...-zip-members.csv`). Totals go in `...-summary.txt`. `--delete` removes only files whose size
and modified time are still what the scan saw, and logs each delete as it happens in
`...-deleted.csv`.

**pack** zips and seals every volume not yet sealed (partly filled ones included) and writes the
upload list. Zips go to `<library> Zips\` beside the library (`--zips DIR` to change), one per
volume: `photos-2023.zip`, `photos-2023-2.zip`, `live-undated.zip`. Store mode, member paths
relative to the volume folder, no Zip64. Each zip is written as `.zip.part` and read back in full
(every CRC, member list equal to the folder and the manifest, each size and SHA-256 equal to the
manifest) before it is renamed into place. A zip that fails is deleted, its volume stays unsealed,
the problems are kept in the manifest (`failure`), and the run carries on. `--dry-run` lists what
it would zip.

- **Sealing** marks the volume in the manifest (`volumes`). Collect never adds to a sealed
  volume: the year's next file opens the next one (`2023-3` after a sealed `2023-2`).
- **Upload list**: `_metadata/uploads/<time>-upload.csv` (`kind, path, size, sha256`), what Matt
  uploads by hand to Google Drive: every sealed zip no list has carried yet, plus `videos/large/`
  files no earlier list carried (both marked `upload_listed`). No list when nothing is new.
- **Resumable**: a re-run seals nothing twice and rebuilds only a zip that is missing, the wrong
  size, or failed before; a rebuilt zip is listed again. **Keep the zips after uploading.** A
  sealed volume whose file count or bytes differ from its seal fails with `sealed volume changed
  since sealing` (dedup's `--unseal` is the way to change one).
- A failure naming files `only in the folder` means a file not in the manifest sits in the
  volume (left by a stopped collect, or put there by hand): look, move it out, and re-run.

**migrate** (`--library ... [--apply]`) brings an older library up to the layout below (from
`photos/<year>/vNN/`, then Live clips from `photos/` to `live/`), by renames only. A dry run unless
`--apply`; resumable. `E:\Kids Photos` is current; its older `runs/` and `retire/` records may
name paths in the earlier layout.

**dedup** (`dedup.py`, its own script: Pillow, pillow-heif, numpy) removes near-duplicates,
which collect keeps. Removal is a move, never a delete:

```sh
uv run dedup.py scan  --library "E:\Kids Photos"            # features of every file, cached by hash
uv run dedup.py plan  --library "E:\Kids Photos"            # writes _metadata/dedup/<time>-plan.csv
uv run dedup.py apply --library "E:\Kids Photos" --plan <plan.csv> --unseal
uv run dedup.py undo  --library "E:\Kids Photos" --record _metadata/dedup/<time>-removed.csv
```

- **Match** (photos): candidates share an EXIF capture time or a near dHash; a pair is merged
  only with the same aspect ratio, a near-identical 32x32 grey thumbnail (mean difference of
  at most 3 of 255 with same capture time and same original name, 2 with one of them, 1.5 for an
  undated copy of a dated photo), and no conflict in capture time or sub-second, so bursts and
  same-moment edits (another Live key frame, a filter: 4.9 and up) stay. Classes: `resized` (other pixel
  size), `re-encoded` (same size, other bytes: re-downloads, HEIC vs JPEG). Videos: same
  QuickTime capture second and length within 0.1 s. Close pairs not merged go to
  `<time>-not-confident.csv`.
- **Keeper**: most pixels, then a Live clip or embedded motion, then EXIF date, GPS, more EXIF
  tags, then more bytes. A set-aside photo's Live clip moves to the keeper if it has none;
  otherwise it is set aside too (class `live clip`).
- **apply** moves each lesser copy to `<library> Duplicates\<time>\<library path>`, records it in
  `_metadata/dedup/<time>-removed.csv`, and moves its manifest entry from `files` to `set_aside`.
  Collect treats `set_aside` hashes as known (never re-copied; a clip whose photo was set aside
  pairs with the keeper); retire deletes a source file of set-aside bytes only once both the
  kept library copy and the set-aside copy verify.
- **Sealed volumes**: apply refuses to change one unless given `--unseal`. Then each touched
  volume's zip moves to `<library> Duplicates\<time>\_zips\`, its seal record to the manifest's
  `unsealed`, and the next pack zips the volume again and lists the new zip for upload.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `source` | (required) | Folder to collect from (or retire). Read only. |
| `--library` | (required) | Library root. Must not be inside the source or contain it, except that a source under `<library>\stage\` is allowed for collect and retire (downloads waiting to be collected, as [gphotos-album](../gphotos-album/README.md) leaves them; the tool never walks or writes `stage\`). |
| `--exclude GLOB` | none | File or folder, relative to the source, to leave out; repeatable, case-insensitive (`face_labels`, `*/thumbs`). A zip counts as the folder it was made from (`face_labels` also leaves out `face_labels.zip`), and zip members are also matched as if extracted beside their zip. Pass the same excludes to retire. |
| `--volume-mb` | 2000 | Volume fill limit, MB (10^6 bytes). Must stay under 2147 (2 GiB) so a volume's zip never needs Zip64. |
| `--large-video-mb` | 100 | Videos above this skip the volumes and go to `videos/large/`. |
| `--live-max-seconds` | 4 | A same-stem video longer than this is not a Live Photo (see below). |
| `--reserve-gb` | 5 | Free space to leave on the library drive. If the new files don't all fit, collect copies oldest first (undated last) until only this much is left, lists the rest in `_metadata/runs/<time>-left-out.csv`, and a later run picks them up. |
| `--no-zips` | off | Treat `.zip` files as a skipped type instead of reading their media members. |
| `--delete` (retire) | off | Actually delete verified source files. |

## What it does

- **Identity is exact bytes** (SHA-256). A file whose hash is in the library is a duplicate: it
  isn't copied, but its source path is added to the manifest.
- **Layout**: the tool's entries at the library base are `photos/` (stills), `videos/`, `live/`
  (Live Photo clips) and `_metadata/`; anything else there (Matt's `face_labels.zip`) is left
  alone. A year's first volume is the year folder (`photos/2023/`), further ones are siblings
  (`photos/2023-2/`); undated files use `undated`. Videos over `--large-video-mb` go in
  `videos/large/<year>/`, outside the volumes. Volumes fill to `--volume-mb` in date order, a
  later run filling a year's highest-numbered unsealed volume before opening the next. A volume
  folder is never renamed.
- **Date**, the first found: EXIF `DateTimeOriginal`, then `DateTimeDigitized` (camera wall time);
  for videos Apple's `com.apple.quicktime.creationdate`, else `mvhd` creation time (UTC, converted
  to this machine's zone); a Google JSON sidecar's `photoTakenTime` in the same folder
  (`<name>.json`, `<name>.supplemental-metadata.json` or any cut of that suffix, `<stem>.json`;
  Takeout's `IMG_1(1).jpg` owns `IMG_1.jpg(1).json`; `IMG_1-edited.jpg` shares `IMG_1.jpg`'s; a
  name cut short at 40+ characters counts only when it fits media of one stem); a date in the
  filename (`IMG_20230514_101112`, `PXL_...`, `Screenshot_...`, `IMG-20230514-WA0001`,
  `signal-2023-05-14-...`). Never the modified time. Names compare ignoring case and Unicode form.
- **Names**: `YYYY-MM-DD_HHMMSS_<original stem>.<ext>` (extension lower-cased; undated files keep
  their name). On a clash, `_<8 hex of hash>` is added.
- **Live Photos**: a video sharing its stem with a `.heic/.jpg` in the same folder (or zip
  folder), and no longer than `--live-max-seconds`, is that photo's clip. It goes to
  `live/<year>[-N]/` (the photo's year), named with the photo's date prefix, placed straight after
  its photo. The length test matters because iPhone `IMG_NNNN` numbers repeat: real clips run
  0.4-3.5 s, same-stem false matches 4 s to minutes. The clip's own timestamp can't decide it
  (Google's MP4 re-exports carry timestamps hours or days off).
- **Near-duplicates are kept by collect** (different bytes: Google re-downloads a few KB apart,
  resized copies); the second to arrive gets the hash suffix. `dedup.py` removes them.
- Non-media files (`.json`, `desktop.ini`, scripts, unknown types) are not copied; they are
  counted and listed.

## Files in the library

Everything the tool keeps that isn't a photo or video is in `_metadata/`:

- `manifest.json`: `files` by hash: library `path`, `size`, `kind` (photo, video, live, large),
  `date`, `date_source`, every source path seen (`<zip>!<member>` for members), the run that
  `added` it, and Live pairing both ways (`live_photo_of` on a clip, `live_clips` on its photo).
  Saved every 100 files; a file copied but not yet in the manifest is adopted on the next run.
  `volumes` (pack): `sealed`, `zip`, `zip_size`, `files`, `bytes`, `zipped`, `upload_listed`, or
  `failure`. `set_aside` and `unsealed`: dedup's (above).
- `runs/<time>-collect.txt` and `-collect-not-copied.csv`; `runs/<time>-pack.txt`;
  `uploads/<time>-upload.csv`; `retire/` (retire's lists); `dedup/` (`features.json`, a cache
  safe to delete, and each run's `-plan.csv`, `-not-confident.csv`, `-removed.csv`).
- `hashes.json`: collect's hash cache by source path, size and modified time. Safe to delete.
- `staging/`: zip-only files extracted during a collect; removed at the end of the run.

## Tests

```sh
cd photo-collector
uv run test_photo_collector.py        # 60 tests, a few seconds; scratch in <repo>/out/pytest-photo-collector
uv run test_dedup.py                  # dedup with collect, retire, pack; scratch in <repo>/out/pytest-dedup
```

Fixtures only (JPEGs with EXIF via Pillow, hand-made MP4 headers, Takeout sidecars, damaged and
nested zips, runs stopped part way); they never touch a real library.
`PC_MODULE=<path to another photo_collector.py>` runs the first set against another copy.

## Quirks of this machine

- The library is `E:\Kids Photos` (`D:` lacks the space); zips in `E:\Kids Photos Zips\`,
  set-aside duplicates in `E:\Kids Photos Duplicates\`. `D:\Julia Photos` is retired: only
  `face_labels/`, `face_labels.zip` and `upload.py` remain; it needs `--exclude face_labels`.
- Collect from a gphotos-album stage with `--exclude _gphotos`.
- **Timings** on E:
  - collect: hashing ~140 MB/s, staging zip members ~45 MB/s, copying D: to E: ~74 MB/s (265 GB
    of source, 169 GB unique: ~70 min). A re-run over the same source takes seconds.
  - retire: re-reads source and library at ~67 MB/s (433 GB: ~65-70 min).
  - pack: ~36 MB/s of volume data (50 GB: ~24 min). The read-back is likely served partly from
    Windows' file cache (32 GB RAM): it proves the zip's contents, less so the disk surface.
  - dedup: scan reads every library file once, ~7 files/s with 3 workers (6,951 files: 16 min,
    beside a download); later scans read only new files. plan takes seconds (`--pixels` adds
    ~1 min per 100 same-size pairs); apply and undo are renames, seconds.
  - E:\Kids Photos to E: copy (a stage collect): ~75 MB/s.
- The guard here refuses `Remove-Item` on `D:\Kids Photos`; it holds only a stale
  `.cache\hashes.json`, safe to delete by hand.
