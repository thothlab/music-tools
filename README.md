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
- Cover art is detected by scoring image filenames by keyword (`front`, `cover`, `folder`, `album` win; `back`, `booklet`, `matrix`, ... lose) rather than requiring an exact name, so files like `front CD1.jpg` are recognized. Images in `Covers/`, `Artwork/`, `Scans/`, etc. are searched too, and the best match is embedded when possible
- Multi-disc boxes: a folder holding several disc images (one `cue` + audio per disc, e.g. `CD1.cue`/`CD1.flac`, `CD2.cue`/`CD2.flac`) is split per disc into its own subfolder (`CD1/`, `CD2/`, ...), and each disc gets the cover whose name matches that disc
- CUE sheets are decoded robustly: genuine UTF-8 (incl. BOM) is used as-is; otherwise the sheet is decoded as both cp1251 (Russian) and cp1252 (Western) and the result that looks most like real text wins (ties favor cp1252), with latin-1 as a final fallback. This fixes both Windows-1252 cues (e.g. byte `0x92` for a typographic apostrophe in a `FILE` name) and Windows-1251 Cyrillic cues that would otherwise turn into mojibake tags
- In preserve-structure mode (the default, no `--rebuild-by-artist`), if the album folder name starts with a release year separated from the title by a space, `.`, `,`, `-`, or `_` (anything other than `" - "`), the output folder normalizes it: `1994. Брёл, брёл, брёл` -> `1994 - Брёл, брёл, брёл`, `1994, Greatest Hits` -> `1994 - Greatest Hits`. Only the leading separator is replaced, so the title's own dots/commas are kept; year glued to the title (`1994Title`) or separated by other punctuation (`1994: Title`) and artist folders are left untouched. With `--rebuild-by-artist` the name is built from tags as `<year> - <album>` instead
- `--copy-artwork` gathers image files from the release root and from any recognized artwork subfolder (`Covers/`, `Art/`, `Artwork/`, `Scans/`, `tiff/`, ...) and copies them into a single `Covers/` folder in the output, flattening the differently-named source folders into one place (for a multi-disc box, once into the release root next to `CD1/`, `CD2/`, ...). Non-image files (logs, `.inf`, ...) are skipped; `.tif/.tiff/.bmp/.gif/.webp` are copied alongside `.jpg/.png`
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

Also copy artwork folders/images alongside the converted tracks:

```bash
python3 music-tools/batch_split_cues.py \
  --root "/Volumes/PHOTOS/Музыка" \
  --apple-library \
  --copy-artwork
```

In this mode:

```text
/Volumes/PHOTOS/Музыка/Artist/Album
```

becomes:

```text
/Volumes/PHOTOS/Музыка-Apple/Artist/Album
```

### Failure report

If any releases fail (bad cue, missing/locked audio, no access, ...), the run
continues and prints the list of failed folders at the end, tagged by stage
(`[CUE]` / `[TRACK]`), then exits non-zero — so you can see exactly what needs
a second pass:

```text
Finished with 2 failure(s):
  [CUE] /Volumes/PHOTOS/Музыка/Artist/Some Broken Album
  [TRACK] /Volumes/PHOTOS/Музыка/Artist/Another Folder
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
- Any extra flags are forwarded to `batch_split_cues.py`
- Uses the default Apple workflow:
  - `--apple-library`
  - `--copy-non-cue-alac`
  - `--copy-artwork`

Example:

```bash
music-apple
music-apple "/Volumes/PHOTOS/Музыка"
music-apple "/Volumes/PHOTOS/Музыка" --copy-artwork
```
