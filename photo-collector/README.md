# photo-collector

Gathers photos and videos of Matt's kids, downloaded from many sources (Google Photos albums
and the like), into one library that holds a single copy of each file, sorted by year and split
into photos and videos. Runs are incremental: point it at a new download and it adds only what
the library lacks. Photos and videos are never committed here; the source and library folders are
passed in.

## How to run

Needs [uv](https://docs.astral.sh/uv/); the one dependency (`exifread`) is declared inline.

```sh
cd photo-collector
uv run photo_collector.py collect "D:\Julia Photos" --library "E:\Kids Photos" --exclude face_labels
uv run photo_collector.py retire  "D:\Julia Photos" --library "E:\Kids Photos" --exclude face_labels
```

**collect** copies every photo and video in the source that the library doesn't hold yet. The
source is only read: nothing in it is written, moved or deleted, and zips are read in place, never
extracted into the source.

**retire** is a dry run unless given `--delete`. It re-hashes every file in the source (and every
media member of every zip), re-reads the library copy for each hash rather than trusting the
manifest, and writes `_metadata/retire/<time>-<source name>.csv` in the library: one row per source file,
`delete` (verified) or `keep` with the reason (`no library copy`, `skipped type .xyz`, `excluded`,
`hash mismatch in library copy`, `library copy missing`). A zip is `delete` only when all its
media members verified; the per-member detail goes in `...-zip-members.csv`, and totals in
`...-summary.txt`. With `--delete` it removes the `delete` rows' files after writing the list.

**migrate** (`uv run photo_collector.py migrate --library "E:\Kids Photos" [--apply]`) moves a
library from the old layout (`photos/<year>/vNN/`, manifest and logs at the base) to the one
below: renames on the library drive, never a copy, files keep their names, the manifest's paths
are rewritten and the old manifest kept as `_metadata/runs/<time>-migrate-old-manifest.json`.
A dry run unless given `--apply`; a stopped `--apply` resumes when re-run. Collect and retire
refuse a library still in the old layout. `E:\Kids Photos` was migrated on 2026-11-09 (6,568
files moved, 8 s); paths inside the `runs/` and `retire/` records written before then are in the
old layout.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `source` | (required) | Folder to collect from (or retire). Read only. |
| `--library` | (required) | Library root. Must not be inside the source or contain it. |
| `--exclude GLOB` | none | File or folder, relative to the source, to leave out; repeatable, case-insensitive (`face_labels`, `*/thumbs`). Pass the same excludes to retire. |
| `--volume-mb` | 2000 | Volume fill limit, MB (10^6 bytes). Must stay under 2147 (2 GiB) so a volume's zip never needs Zip64. |
| `--large-video-mb` | 100 | Videos above this skip the volumes and go to `videos/large/`. |
| `--live-max-seconds` | 4 | A same-stem video longer than this is not a Live Photo (see below). |
| `--reserve-gb` | 5 | Free space to leave on the library drive. If the new files don't all fit, collect copies oldest first (undated last) until only this much is left, lists the rest in `_metadata/runs/<time>-left-out.csv`, and a later run picks them up. |
| `--no-zips` | off | Treat `.zip` files as a skipped type instead of reading their media members. |
| `--delete` (retire) | off | Actually delete verified source files. |

## What it does

- **Identity is exact bytes** (SHA-256). A file whose hash is in the library is a duplicate: it
  isn't copied, but its source path is added to the manifest.
- **Layout**: the library's base holds only `photos/`, `videos/` and `_metadata/` (see Files in
  the library). A year's first volume is the year folder itself (`photos/2023/`); further volumes
  are siblings, `photos/2023-2/`, `photos/2023-3/`, and the same for `videos/`. Undated files use
  `undated` (`undated-2`, ...) in place of the year. Videos over `--large-video-mb` go in
  `videos/large/<year>/` instead, outside the volumes. A volume folder is never renamed once
  made, so a sealed volume (zipped and uploaded later) never changes when the next one opens.
  Volumes fill to `--volume-mb`; files go in date order, so a first run gives chronological
  volumes, and a later run fills a year's highest-numbered volume before opening the next. Volumes
  only ever hold files of at most `--large-video-mb`, so none overshoots its limit by more than
  that. Nothing is sealed or zipped yet; every volume stays open.
- **Date**, the first found: embedded capture date (EXIF `DateTimeOriginal`, then
  `DateTimeDigitized`, for images; for videos Apple's `com.apple.quicktime.creationdate`, else
  the QuickTime/MP4 `mvhd` creation time, which is UTC and is converted to this machine's time
  zone); a Google JSON sidecar's `photoTakenTime` (`<name>.json`,
  `<name>.supplemental-metadata.json`, ...); a date in the filename (`IMG_20230514_101112`,
  `PXL_...`, `Screenshot_...`, `IMG-20230514-WA0001`, `signal-2023-05-14-...`). A file's modified
  time is never used.
- **Names**: `YYYY-MM-DD_HHMMSS_<original stem>.<ext>` (extension lower-cased; a date-only
  filename date gives `000000`; undated files keep their original name). On a clash, `_<8 hex of
  hash>` is added.
- **Live Photos**: a video sharing its stem with a `.heic/.jpg` in the same folder (or the same
  folder of the same zip), and no longer than `--live-max-seconds`, goes next to that photo,
  under the photo's date prefix, and the photo's volume keeps room for it. The length test is
  needed because iPhone `IMG_NNNN` numbers repeat: in `D:\Julia Photos` every real Live Photo
  clip ran 0.4-3.5 s, while the 65 same-stem false matches ran 4 s to many minutes and were
  taken weeks or years from the photo. The clip's own timestamp can't decide it: Google's MP4
  re-exports of Live Photos carry timestamps hours or days off.
- **Near-duplicates are kept.** Google re-downloads of one photo often differ by a few KB of
  metadata, and resized copies differ entirely; being different bytes, both are kept, and the
  second to arrive gets the hash suffix.
- Non-media files (`.json`, `desktop.ini`, scripts, unknown types) are not copied; they are
  counted and listed.

## Files in the library

Everything the tool keeps that isn't a photo or video is in `_metadata/`:

- `manifest.json`: per hash, the library `path`, `size`, `kind` (photo, video, live, large),
  `date`, `date_source` (exif, quicktime, sidecar, filename, live-photo, or null), every source
  path it was seen at (`<zip>!<member>` for zip members), the run that `added` it, and
  `live_photo_of` for Live Photo videos. Saved every 100 files and at the end, so a stopped run
  loses little; a file copied but not yet in the manifest is adopted (same bytes) on the next run.
- `runs/<time>-collect.txt`: the run's counts; `runs/<time>-collect-not-copied.csv`: every file
  not copied, with why (excluded, type, or a zip whose members were read).
- `hashes.json`: hashes by source path, size and modified time (zip members by zip
  modified time and CRC), so a re-run over the same source doesn't re-read it. Collect only;
  retire never uses it. Safe to delete.
- `retire/`: retire's lists.
- `staging/`: zip-only files extracted during a collect, moved into place as it goes; removed at
  the end of the run (a stopped run's leftovers are reused by the next).

## Quirks of this machine

- The library lives on `E:\Kids Photos` (`D:` lacks the space). Measured on the first collect of
  `D:\Julia Photos` (265 GB including 97 GB of zips, 168.6 GB unique): hashing ~140 MB/s
  (26 min), staging zip-only files ~45 MB/s (4 min), copying D: to E: ~74 MB/s (38 min); about
  70 minutes in all. A re-run over the same source takes seconds (hash cache). A retire dry run
  over it re-reads source and library (433 GB): 68 minutes.
- `D:\Julia Photos` needs `--exclude face_labels` (2,328 face crops from a labelling tool).
- The guard on this machine refuses `Remove-Item` on `D:\Kids Photos`; an abandoned first attempt
  left only `.cache\hashes.json` there, safe to delete by hand.
