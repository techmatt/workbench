# photo-collector

Gathers photos and videos of Matt's kids, downloaded from many sources (Google Photos albums
and the like), into one library that holds a single copy of each file, sorted by year and split
into photos and videos. Runs are incremental: point it at a new download and it adds only what
the library lacks. Live Photo clips get a stream of their own. Photos and videos are never committed
here; the source and library folders are passed in.

## How to run

Needs [uv](https://docs.astral.sh/uv/); the one dependency (`exifread`) is declared inline.

```sh
cd photo-collector
uv run photo_collector.py collect "D:\Julia Photos" --library "E:\Kids Photos" --exclude face_labels
uv run photo_collector.py retire  "D:\Julia Photos" --library "E:\Kids Photos" --exclude face_labels
uv run photo_collector.py pack --library "E:\Kids Photos"
```

**collect** copies every photo and video in the source that the library doesn't hold yet. The
source is only read: nothing in it is written, moved or deleted, and zips are read in place, never
extracted into the source. A file or zip member that can't be read (a corrupt member, a locked
file) is skipped with a warning and listed as `unreadable` in the run's not-copied list. A run
first removes any `.part` file a stopped run left in an unsealed volume (only collect writes them
there), so the stray can't fail that volume's pack.

**retire** is a dry run unless given `--delete`. It re-hashes every file in the source (and every
media member of every zip), re-reads the library copy for each hash rather than trusting the
manifest, and writes `_metadata/retire/<time>-<source name>.csv` in the library: one row per source file,
`delete` (verified) or `keep` with the reason (`no library copy`, `skipped type .xyz`, `excluded`,
`hash mismatch in library copy`, `library copy missing`, `unreadable (...)`, `unreadable zip`).
Zip members are read by position, so two members of one name are each checked. A zip is `delete` only when every
member is media that verified: a member that is excluded, non-media (sidecar `.json`, nested zip,
anything else) or unverified would be lost with it, so the zip is `keep` and its reason names
those members (the first 20 of each kind; all of them, one row each, in `...-zip-members.csv`).
Totals go in `...-summary.txt`. With `--delete` it removes the `delete` rows' files after
writing the list, but only a file (or zip) whose size and modified time are still what the scan
saw: one changed since it was read is kept and printed (run retire again for it). Every delete is
recorded as it happens in `...-deleted.csv`, so a run that stops part way leaves a true record.

**pack** zips and seals every volume not yet sealed (`photos/`, `videos/`, `live/`, partly filled
ones included) and writes the upload list. Zips go to `<library> Zips\` beside the library
(`E:\Kids Photos Zips\`; `--zips DIR` to change), never inside it, one per volume, named by stream
and folder: `photos-2023.zip`, `photos-2023-2.zip`, `live-undated.zip`. Store mode (no
compression; photos and videos don't shrink), member paths relative to the volume folder, no
Zip64 (volumes stay under 2 GiB). Each zip is written as `<name>.zip.part` and read back in
full before it is renamed into place: every member's CRC, the member list equal to both the
folder's files and the manifest's entries for that volume, and each member's size and SHA-256
equal to the manifest. A zip that fails is deleted, its volume stays unsealed, the problems are
printed and kept in the manifest (`failure`), and the run carries on. `--dry-run` lists what it
would zip.

- **Sealing** marks the volume in the manifest (`volumes`, below). Collect never adds to a sealed
  volume: a new file for that year opens the next one (`2023-3` after a sealed `2023-2`, even one
  that isn't full).
- **Upload list**: `_metadata/uploads/<time>-upload.csv` (`kind, path, size, sha256`), what Matt
  must upload by hand to Google Drive: every sealed zip no list has carried yet (this run's, and
  any sealed by a run that stopped before writing its list), plus the `videos/large/` files no
  earlier run listed. Both get `upload_listed` in the manifest (volumes listed before 007 added
  it are marked from the existing lists on the next run). A run with nothing new writes no list.
- **Resumable**: a re-run seals nothing twice and rebuilds only a zip that is missing, the wrong
  size, or failed before (a stray `.part` is discarded). A rebuilt zip is on that run's list
  again. **Keep the zips after uploading**: deleting one makes the next pack rebuild it (same
  contents, listed again). A sealed volume whose file count or bytes differ from its seal is
  not rebuilt: it fails with `sealed volume changed since sealing`.
- A failure naming files `only in the folder` means something not in the manifest sits in the
  volume (a file copied by a stopped collect that a later run placed elsewhere, or one put there
  by hand): look, move it out, and re-run.

**migrate** (`uv run photo_collector.py migrate --library "E:\Kids Photos" [--apply]`) brings a
library up to the layout below, in two steps, each only if needed:

1. From the old layout (`photos/<year>/vNN/`, manifest and logs at the base). Collect and retire
   refuse a library still in it.
2. Live Photo clips from `photos/` (where they sat next to their photo) to `live/`, placed as
   collect would place them (year and order from the paired photo), and each photo given its
   `live_clips`. Collect refuses a library with clips still in `photos/`, and this step refuses
   to move a clip out of a sealed volume.

Both are renames on the library drive, never a copy; files keep their names, the manifest's paths
are rewritten and the old manifest kept as `_metadata/runs/<time>-migrate[-live]-old-manifest.json`.
A dry run unless given `--apply`; a stopped `--apply` resumes when re-run. `E:\Kids Photos` had
step 1 on 2026-11-09 (6,568 files moved, 8 s) and step 2 at 9:57pm the same day (624 clips,
2.7 GB, 0.9 s); paths inside `runs/` and `retire/` records written before each are in the
earlier layout.

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
- **Layout**: the tool's entries at the library base are `photos/` (stills only), `videos/`,
  `live/` (Live Photo clips) and `_metadata/` (see Files in the library). Anything else there
  (Matt keeps `face_labels.zip` at the base of `E:\Kids Photos`) is left alone: collect, retire and
  migrate never move, read or refuse on it. A year's first volume is the year folder itself
  (`photos/2023/`); further volumes are siblings, `photos/2023-2/`, `photos/2023-3/`, and the same
  for `videos/` and `live/`. Undated files use
  `undated` (`undated-2`, ...) in place of the year. Videos over `--large-video-mb` go in
  `videos/large/<year>/` instead, outside the volumes. A volume folder is never renamed once
  made, so a sealed volume (zipped by pack) never changes when the next one opens.
  Volumes fill to `--volume-mb`; files go in date order, so a first run gives chronological
  volumes, and a later run fills a year's highest-numbered volume before opening the next. Volumes
  only ever hold files of at most `--large-video-mb`, so none overshoots its limit by more than
  that. A sealed volume is skipped: the year's next file opens a new volume.
- **Date**, the first found: embedded capture date (EXIF `DateTimeOriginal`, then
  `DateTimeDigitized`, for images; for videos Apple's `com.apple.quicktime.creationdate`, else
  the QuickTime/MP4 `mvhd` creation time, which is UTC and is converted to this machine's time
  zone; EXIF and Apple dates are the camera's wall time, any zone offset ignored); a Google JSON
  sidecar's `photoTakenTime` in the same folder (`<name>.json`, `<name>.supplemental-metadata.json`
  or any cut of that suffix, `<stem>.json`; Takeout's second `IMG_1.jpg`, saved as `IMG_1(1).jpg`,
  owns `IMG_1.jpg(1).json` and `IMG_1.jpg.supplemental-metadata(1).json`, never `IMG_1.jpg.json`;
  `IMG_1-edited.jpg` shares `IMG_1.jpg`'s; a name Takeout cut short, at 40 characters or more
  before `.json`, counts only when it fits media of one stem in the folder); a date in the filename (`IMG_20230514_101112`,
  `PXL_...`, `Screenshot_...`, `IMG-20230514-WA0001`, `signal-2023-05-14-...`). A file's modified
  time is never used. Names are compared ignoring case and Unicode form (a Mac's NFD vs NFC),
  here and for Live Photo pairing and name clashes.
- **Names**: `YYYY-MM-DD_HHMMSS_<original stem>.<ext>` (extension lower-cased; a date-only
  filename date gives `000000`; undated files keep their original name). On a clash, `_<8 hex of
  hash>` is added.
- **Live Photos**: a video sharing its stem with a `.heic/.jpg` in the same folder (or the same
  folder of the same zip), and no longer than `--live-max-seconds`, is that photo's clip. It goes
  to `live/<year>[-N]/` (2 GB volumes like the other streams; `live/undated` for an undated
  photo), named with the photo's date prefix; its year is the photo's year, and clips are placed
  straight after their photo, so `live/` volumes fill in the photos' date order. A clip over
  `--large-video-mb` (none so far; clips run 1-7 MB) would go to `videos/large/<year>/`. The
  manifest records the pair both ways (see below). The length test is
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
  path it was seen at (`<zip>!<member>` for zip members), the run that `added` it, and the Live
  Photo pairing both ways, by hash: `live_photo_of` on each clip (its photo), `live_clips` on each
  photo that has one (a list; so far always one clip). Saved every 100 files and at the end, so a stopped run
  loses little; a file copied but not yet in the manifest is adopted (same bytes) on the next run.
  `volumes` (written by pack), per volume (`photos/2023-2`): `sealed` (the pack run), `zip`,
  `zip_size`, `files`, `bytes`, `zipped`; or `failure` and `last_failure` for one whose zip
  failed verification. Large videos carry `upload_listed` (the pack run that listed them).
- `uploads/<time>-upload.csv`: pack's upload lists; `runs/<time>-pack.txt`: its counts, time
  and failures.
- `runs/<time>-collect.txt`: the run's counts; `runs/<time>-collect-not-copied.csv`: every file
  not copied, with why (excluded, type, or a zip whose members were read).
- `hashes.json`: hashes by source path, size and modified time (zip members by zip
  modified time and CRC), so a re-run over the same source doesn't re-read it. Collect only;
  retire never uses it. Safe to delete.
- `retire/`: retire's lists (with `--delete`, also `...-deleted.csv`).
- `staging/`: zip-only files extracted during a collect, moved into place as it goes; removed at
  the end of the run (a stopped run's leftovers are reused by the next).

## Tests

```sh
cd photo-collector
uv run test_photo_collector.py        # 60 tests, a few seconds; scratch in <repo>/out/pytest-photo-collector
```

Fixtures only: each test builds its own small source, library and zips folder (JPEGs with EXIF via
Pillow, hand-made MP4 headers, Takeout sidecars, zips with corrupt, cut-off, duplicate, nested and
excluded members) and stops runs part way to check that the next one completes or refuses. They
never touch a real library. `PC_MODULE=<path to another photo_collector.py>` runs them against
another copy (to show a fixture fails before its fix).

## Quirks of this machine

- The library lives on `E:\Kids Photos` (`D:` lacks the space). Measured on the first collect of
  `D:\Julia Photos` (265 GB including 97 GB of zips, 168.6 GB unique): hashing ~140 MB/s
  (26 min), staging zip-only files ~45 MB/s (4 min), copying D: to E: ~74 MB/s (38 min); about
  70 minutes in all. A re-run over the same source takes seconds (hash cache). A retire dry run
  over it re-reads source and library (433 GB): 68 minutes; the real `--delete` run, 65 minutes
  (~67 MB/s), the deletes themselves under 10 s.
- **First pack, 2026-11-09** (`_metadata/runs/20261109-235241-pack.txt`): 38 volumes into 38
  zips (50.33 GB, 6,568 files) in `E:\Kids Photos Zips\`, all verified and sealed, none failed;
  23.5 minutes (~36 MB/s of volume data, each byte read, written and read back on E:). Its upload
  list, `_metadata/uploads/20261109-235241-upload.csv`, holds the 38 zips and all 370 large
  videos (118.29 GB). The read-back of a just-written 2 GB zip is likely served partly from
  Windows' file cache (32 GB RAM), so it proves the zip's contents, less so the disk surface.
- **`D:\Julia Photos` was retired on 2026-11-09** (`_metadata/retire/20261109-231532-Julia_Photos*`):
  7,516 files deleted (7,498 loose, 167.7 GB; 18 zips, 96.9 GB), every one verified; 23 directories
  left empty were removed. Only `face_labels/`, `face_labels.zip` (excluded) and `upload.py`
  (skipped type) remain there.
- `D:\Julia Photos` needs `--exclude face_labels` (2,328 face crops from a labelling tool, as a
  folder and, since 2026-11-09, also as `face_labels.zip` beside it; the exclude covers both).
  Collect run `20261109-215820`, made before the exclude covered the zip, added the crops; they
  were removed again (`_metadata/runs/20261109-*-undo-face-labels.txt`).
  `E:\Kids Photos` holds 624 Live Photo clips (2.7 GB) in `live/2022` to `live/2025-2`.
- **Read-only check, 2026-11-10 12:50am**: 6,938 manifest entries, every one on disk at its size;
  no file in `photos/`, `videos/`, `live/` outside the manifest, no `.part`, no two paths alike by
  case or Unicode form; all 38 volumes sealed and agreeing with their zips; all 38 zips found on
  the first upload list (the next pack marks them listed and lists nothing again). No library date
  came from a sidecar (exif 4,787, quicktime 1,344, live-photo 624, filename 26, undated 157), so
  the old sidecar matching gave no wrong dates here.
- **First collect from a stage, 2026-11-10 9:16pm** (`_metadata/runs/20261110-211636-collect.txt`):
  13 files from `E:\Kids Photos\stage\julia-and-elizabeth` (gphotos-album's test run) into
  `photos/2026-2`, `live/2026` and `videos/large/2026` (2026 sealed, so a new volume opened); the
  library now holds 6,951 entries. It also took in a failure screenshot from the stage's
  `_gphotos/` as an undated photo, removed again (`_metadata/runs/20261110-*-undo-screenshot.txt`).
  Collect from a gphotos-album stage with `--exclude _gphotos`.
- The guard on this machine refuses `Remove-Item` on `D:\Kids Photos`; an abandoned first attempt
  left only `.cache\hashes.json` there, safe to delete by hand.
