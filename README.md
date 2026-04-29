# Music Tools

Utilities for reshaping archive-style music releases into track-based files that are easier to use with Navidrome, Apple Music, and mobile clients.

## split_album_image.py

Split a single large album image using a `.cue` sheet and export individual tracks as either `FLAC` or `ALAC`.

### Why this exists

- Navidrome handles track-based libraries better than `album.flac + cue`
- seeking and startup are usually faster on separate track files
- Apple Music on macOS is much happier with `ALAC` than with `FLAC`

### Requirements

- `python3`
- `ffmpeg`
- `ffprobe`

### Supported inputs

- A folder containing:
  - one `.cue` file
  - one large audio file referenced by the cue sheet, such as `.flac`, `.wav`, `.ape`, `.wv`, `.m4a`
- Or explicit paths passed with flags

### Examples

Split to track-based FLAC:

```bash
python3 split_album_image.py \
  --input-dir "/Volumes/PHOTOS/Музыка/Some Album" \
  --format flac
```

Split to ALAC for Apple Music import:

```bash
python3 split_album_image.py \
  --input-dir "/Volumes/PHOTOS/Музыка/Some Album" \
  --format alac \
  --output-root "/Volumes/PHOTOS/Музыка-Apple"
```

Apple Music preset:

```bash
python3 split_album_image.py \
  --input-dir "/Volumes/PHOTOS/Музыка/Some Album" \
  --apple-library
```

Dry run:

```bash
python3 split_album_image.py \
  --input-dir "/Volumes/PHOTOS/Музыка/Some Album" \
  --format flac \
  --dry-run
```

### Output layout

By default:

```text
<source dir>/split-output/<album artist>/<year - album>/<NN - title>.<ext>
```

### Notes

- The script reads standard cue fields such as `PERFORMER`, `TITLE`, `REM DATE`, `REM GENRE`, `TRACK`, and `INDEX 01`
- It uses lossless encoding only:
  - `flac` -> `.flac`
  - `alac` -> `.m4a`
- If a `cover.jpg`, `cover.png`, `folder.jpg`, or `folder.png` exists beside the source, the script embeds it in the output files when possible
- `--apple-library` implies:
  - `--format alac`
  - output root `/Volumes/PHOTOS/Музыка-Apple`
  - tag normalization
- `--normalize-tags` applies conservative cleanup for messy cue sheets:
  - de-duplicates album artist lists
  - cleans whitespace
  - trims known noisy suffixes in titles
  - reduces per-track performer strings to likely playable artist names when cue metadata is overloaded

## batch_split_cues.py

Walk a root folder recursively, find directories containing `.cue` files, and run `split_album_image.py` for each release.

### Example

Preview a whole tree:

```bash
python3 music-tools/batch_split_cues.py \
  --root "/Volumes/PHOTOS/Музыка" \
  --format alac \
  --dry-run
```

Process the tree for Apple Music friendly output:

```bash
python3 music-tools/batch_split_cues.py \
  --root "/Volumes/PHOTOS/Музыка" \
  --apple-library
```

Process both:

- `.cue + single image` releases
- already track-based folders in `FLAC`, `ALAC`, `WavPack`, `APE`, `WAV`, `AIFF`

into one Apple-friendly ALAC library:

```bash
python3 music-tools/batch_split_cues.py \
  --root "/Volumes/PHOTOS/Музыка" \
  --apple-library \
  --copy-non-cue-alac
```

Preserve the original directory structure under the Apple output root:

```bash
python3 music-tools/batch_split_cues.py \
  --root "/Volumes/PHOTOS/Музыка" \
  --apple-library \
  --copy-non-cue-alac \
  --preserve-structure
```

In this mode:

```text
/Volumes/PHOTOS/Музыка/Artist/Album
```

becomes:

```text
/Volumes/PHOTOS/Музыка-Apple/Artist/Album
```

## convert_track_album.py

Convert an already track-based album folder into `ALAC`, preserving tags and embedded or sidecar artwork where possible.

### Example

```bash
python3 music-tools/convert_track_album.py \
  --input-dir "/Volumes/PHOTOS/Музыка/1993 - Gary Moore - Blues Alive (2LP, Virgin, V2716, UK&EU, 24-96)" \
  --apple-library
```

## music-apple

Small wrapper command for the default workflow.

- If called without arguments, it processes the current directory
- If called with one argument, it treats it as the source root
- Uses the default Apple workflow:
  - `--apple-library`
  - `--copy-non-cue-alac`

Example:

```bash
music-apple
music-apple "/Volumes/PHOTOS/Музыка"
```
