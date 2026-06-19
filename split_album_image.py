#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


SUPPORTED_AUDIO_EXTS = {
    ".flac",
    ".wav",
    ".wv",
    ".ape",
    ".m4a",
    ".aiff",
    ".aif",
    ".alac",
}
COVER_EXTS = (".jpg", ".jpeg", ".png")
COVER_SUBDIRS = (
    "scan", "scans", "cover", "covers", "artwork", "art", "front",
    "tiff", "tif", "images", "image", "pics", "pictures",
)
# Image formats copied as-is by --copy-artwork (broader than the embeddable set).
ARTWORK_IMAGE_EXTS = COVER_EXTS + (".tif", ".tiff", ".bmp", ".gif", ".webp")
# Filename keywords that mark a front-cover candidate, strongest first.
COVER_POSITIVE_KEYWORDS = (("front", 100), ("cover", 90), ("folder", 85), ("album", 80))
# Keywords that mark a non-front scan (back, booklet pages, disc matrix, etc.).
COVER_NEGATIVE_KEYWORDS = (
    "back", "rear", "booklet", "matrix", "tray", "inlay", "spine", "obi", "sticker",
)
# Matches per-disc subfolder names like "CD1", "CD 1", "Disc 2", "Disk-3".
DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk|disco)[\s._-]*\d+", re.IGNORECASE)
DEFAULT_APPLE_OUTPUT_ROOT = "/Volumes/PHOTOS/Музыка-Apple"
APPLE_ALAC_SAMPLE_RATE = "96000"
APPLE_ALAC_SAMPLE_FMT = "s32"
APPLE_ALAC_MAX_RATE = 96000
APPLE_ALAC_MAX_BITS = 24


@dataclass
class Track:
    number: int
    title: str = ""
    performer: str | None = None
    index_01: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    file_name: str | None = None


@dataclass
class CueSheet:
    title: str = ""
    performer: str = ""
    date: str = ""
    genre: str = ""
    disc_number: int = 1
    file_name: str | None = None
    tracks: list[Track] = field(default_factory=list)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def _format_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


_BAR_BLOCKS = " ▏▎▍▌▋▊▉█"


def _render_progress(label: str, cur: float, total: float) -> None:
    width = shutil.get_terminal_size((80, 24)).columns
    ratio = 0.0 if total <= 0 else min(1.0, max(0.0, cur / total))
    pct = f"{int(ratio * 100):3d}%"
    timing = f"{_format_mmss(cur)}/{_format_mmss(total)}"
    suffix = f" {pct} {timing}"
    prefix = f"  {label} "
    bar_width = max(10, width - len(prefix) - len(suffix) - 1)
    total_eighths = int(round(bar_width * 8 * ratio))
    full = total_eighths // 8
    remainder = total_eighths % 8
    filled_part = "█" * full
    partial = _BAR_BLOCKS[remainder] if remainder and full < bar_width else ""
    empty_count = bar_width - full - (1 if partial else 0)
    bar = filled_part + partial + "░" * max(0, empty_count)
    line = f"{prefix}{bar}{suffix}"
    sys.stderr.write("\r" + line)
    sys.stderr.flush()


def run_ffmpeg_with_progress(
    cmd: list[str],
    expected_seconds: float,
    label: str,
) -> subprocess.CompletedProcess[str]:
    is_tty = sys.stderr.isatty()
    if not is_tty or expected_seconds <= 0:
        return subprocess.run(cmd, text=True, capture_output=True, check=False)

    full_cmd = cmd[:1] + ["-nostats", "-progress", "pipe:1"] + cmd[1:]
    proc = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_chunks: list[str] = []
    last_render = 0.0
    max_seconds = 0.0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    raw = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                cur_seconds = raw / 1_000_000.0
                if cur_seconds <= max_seconds:
                    continue
                max_seconds = cur_seconds
                now = time.monotonic()
                if now - last_render >= 0.1:
                    _render_progress(label, cur_seconds, expected_seconds)
                    last_render = now
            elif line == "progress=end":
                _render_progress(label, expected_seconds, expected_seconds)
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        proc.wait()
        sys.stderr.write("\n")
        sys.stderr.flush()
        raise
    finally:
        if proc.stderr is not None:
            try:
                stderr_chunks.append(proc.stderr.read())
            except Exception:
                pass
        proc.wait()
        sys.stderr.write("\n")
        sys.stderr.flush()
    return subprocess.CompletedProcess(
        full_cmd, proc.returncode, stdout="", stderr="".join(stderr_chunks)
    )


def fail(message: str, *, details: str | None = None, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    if details:
        print(details, file=sys.stderr)
    raise SystemExit(code)


def shell_preview(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split a large album image using a CUE sheet and export track files."
    )
    parser.add_argument("--input-dir", help="Folder containing the .cue and source audio file")
    parser.add_argument("--cue", help="Path to .cue file")
    parser.add_argument("--audio", help="Path to source audio file")
    parser.add_argument(
        "--format",
        choices=("flac", "alac"),
        default="flac",
        help="Output format",
    )
    parser.add_argument(
        "--output-root",
        help="Root output folder. Defaults to <input-dir>/split-output",
    )
    parser.add_argument("--source-root", help="Root of the source tree, used with --preserve-structure")
    parser.add_argument(
        "--preserve-structure",
        action="store_true",
        help="Preserve source directory structure under the output root instead of rebuilding artist/album folders",
    )
    parser.add_argument(
        "--album-artist",
        help="Override album artist used for tags and folders",
    )
    parser.add_argument("--album-title", help="Override album title")
    parser.add_argument("--year", help="Override album year")
    parser.add_argument("--genre", help="Override album genre")
    parser.add_argument("--disc-number", type=int, help="Override disc number")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commands without writing files",
    )
    parser.add_argument(
        "--apple-library",
        action="store_true",
        help="Shortcut for Apple Music friendly output: ALAC into a separate library root",
    )
    parser.add_argument(
        "--normalize-tags",
        action="store_true",
        help="Normalize noisy CUE performer/title fields into cleaner tags",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing track files",
    )
    parser.add_argument(
        "--copy-artwork",
        action="store_true",
        help="Copy artwork dirs (Covers/Artwork/...) and loose images into the output, mirroring their source layout",
    )
    return parser.parse_args()


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_mmssff(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        fail(f"Invalid CUE timestamp: {value}")
    minutes, seconds, frames = [int(x) for x in parts]
    return minutes * 60 + seconds + (frames / 75.0)


def _legacy_text_score(text: str) -> int:
    """Higher = more like coherent text. Letters reward, stray high symbols penalize.

    Mojibake (e.g. a Cyrillic cp1251 sheet decoded as cp1252) shows up as a run of
    high-range symbols/punctuation rather than letters, so it scores low.
    """
    letters = symbols = 0
    for ch in text:
        if ord(ch) < 0x80:
            continue
        if ch.isalpha():
            letters += 1
        elif not ch.isspace():
            symbols += 1
    return letters - symbols


def read_cue_text(cue_path: Path) -> str:
    """Decode a CUE sheet, tolerating the legacy encodings EAC/rippers emit.

    Genuine UTF-8 (incl. BOM) is authoritative. Otherwise the sheet is a legacy
    single-byte codepage — usually cp1251 (Russian) or cp1252 (Western). Both
    decode almost any byte without error, so we decode with each and keep the
    result that looks most like real text, breaking ties toward cp1252 so a lone
    Western accent is not misread as Cyrillic. latin-1 maps every byte as a final
    safety net.
    """
    raw = cue_path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    best: tuple[int, str] | None = None
    for encoding in ("cp1252", "cp1251"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        score = _legacy_text_score(text)
        if best is None or score > best[0]:
            best = (score, text)
    if best is not None:
        return best[1]
    return raw.decode("latin-1")


def parse_cue(cue_path: Path) -> CueSheet:
    cue = CueSheet()
    current_track: Track | None = None
    inside_track = False

    for raw_line in read_cue_text(cue_path).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.upper().startswith("REM DATE "):
            cue.date = unquote(line[9:])
            continue
        if line.upper().startswith("REM GENRE "):
            cue.genre = unquote(line[10:])
            continue
        if line.upper().startswith("REM DISCNUMBER "):
            try:
                cue.disc_number = int(unquote(line[15:]))
            except ValueError:
                pass
            continue
        if line.upper().startswith("FILE "):
            match = re.match(r'^FILE\s+"?(.*?)"?\s+\S+$', line, flags=re.IGNORECASE)
            if match:
                cue.file_name = match.group(1)
            continue
        if line.upper().startswith("TRACK "):
            match = re.match(r"^TRACK\s+(\d+)\s+\S+$", line, flags=re.IGNORECASE)
            if not match:
                fail(f"Could not parse TRACK line in {cue_path}", details=line)
            current_track = Track(number=int(match.group(1)), file_name=cue.file_name)
            cue.tracks.append(current_track)
            inside_track = True
            continue

        target_track = current_track if inside_track and current_track else None
        target_is_track = target_track is not None

        if line.upper().startswith("TITLE "):
            value = unquote(line[6:])
            if target_is_track:
                target_track.title = value
            else:
                cue.title = value
            continue
        if line.upper().startswith("PERFORMER "):
            value = unquote(line[10:])
            if target_is_track:
                target_track.performer = value
            else:
                cue.performer = value
            continue
        if line.upper().startswith("INDEX 01 "):
            if not target_is_track:
                continue
            target_track.index_01 = unquote(line[9:])
            target_track.start_seconds = parse_mmssff(target_track.index_01)
            continue

    if not cue.tracks:
        fail(f"No tracks found in {cue_path}")
    for track in cue.tracks:
        if track.start_seconds is None:
            fail(f"Track {track.number:02d} has no INDEX 01 in {cue_path}")
    return cue


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(",")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _name_tokens(path: Path) -> set[str]:
    return {t for t in _TOKEN_SPLIT_RE.split(path.stem.lower()) if t}


def _cover_keyword_score(tokens: set[str]) -> int:
    positive = 0
    for keyword, value in COVER_POSITIVE_KEYWORDS:
        if keyword in tokens:
            positive = max(positive, value)
    negative = 40 * sum(1 for keyword in COVER_NEGATIVE_KEYWORDS if keyword in tokens)
    return positive - negative


def _disc_tokens(disc_hint: str | None) -> set[str]:
    """Filename tokens that identify a specific disc, e.g. 'CD1' -> {cd1, disc1, disk1}."""
    if not disc_hint:
        return set()
    tokens = _name_tokens(Path(disc_hint))
    tokens.add(re.sub(r"[^a-z0-9]+", "", disc_hint.lower()))
    match = re.search(r"(\d+)$", disc_hint.lower())
    if match:
        n = int(match.group(1))
        tokens.update({f"cd{n}", f"disc{n}", f"disk{n}"})
    return {t for t in tokens if t}


def _aspect_distance(path: Path) -> float:
    dims = _image_dimensions(path)
    if not dims or dims[1] <= 0:
        return 99.0
    return abs(dims[0] / dims[1] - 1.0)


def _collect_cover_candidates(folder: Path) -> list[Path]:
    candidates: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for p in entries:
        try:
            if p.is_file() and p.suffix.lower() in COVER_EXTS:
                candidates.append(p)
            elif p.is_dir() and p.name.lower() in COVER_SUBDIRS:
                try:
                    sub_entries = list(p.iterdir())
                except OSError:
                    continue
                candidates += [
                    q for q in sub_entries if q.is_file() and q.suffix.lower() in COVER_EXTS
                ]
        except OSError:
            continue
    return candidates


def find_cover_art(folder: Path, disc_hint: str | None = None) -> Path | None:
    """Pick the best front-cover image for a release (or a specific disc).

    Candidates are scored by filename keywords (front/cover/folder/album boost,
    back/booklet/matrix/... penalty) rather than an exact stem match, so files
    like ``front CD1.jpg`` are recognised. When ``disc_hint`` is given (e.g.
    ``"CD1"``), images whose name carries the matching disc token win over the
    generic release cover.

    Multi-disc boxes keep their covers in the album root (loose ``Cover.jpg`` /
    ``front.png`` or a ``Covers/`` subfolder) rather than inside each ``CD 1`` /
    ``CD 2`` disc folder, so when nothing matches inside ``folder`` the search
    falls back one level up to the parent.
    """
    found = _find_cover_in(folder, disc_hint)
    if found:
        return found
    parent = folder.parent
    if parent != folder:
        return _find_cover_in(parent, disc_hint)
    return None


def _find_cover_in(folder: Path, disc_hint: str | None) -> Path | None:
    candidates = _collect_cover_candidates(folder)
    if not candidates:
        return None

    disc_tokens = _disc_tokens(disc_hint)
    scored = [(p, _cover_keyword_score(_name_tokens(p)), _name_tokens(p)) for p in candidates]

    def best(pool: list[tuple[Path, int, set[str]]]) -> Path:
        return sorted(pool, key=lambda it: (-it[1], _aspect_distance(it[0]), it[0].name.lower()))[0][0]

    if disc_tokens:
        disc_hits = [it for it in scored if it[1] > 0 and (disc_tokens & it[2])]
        if disc_hits:
            return best(disc_hits)

    positives = [it for it in scored if it[1] > 0]
    if positives:
        return best(positives)
    return None


def _cue_references_existing_audio(cue_path: Path) -> bool:
    try:
        text = read_cue_text(cue_path)
    except OSError:
        return False
    folder = cue_path.parent
    saw_file = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("FILE "):
            continue
        match = re.match(r'^FILE\s+"?(.*?)"?\s+\S+$', stripped, flags=re.IGNORECASE)
        if not match:
            continue
        saw_file = True
        if not (folder / match.group(1)).exists():
            return False
    return saw_file


def first_audio_file(folder: Path) -> Path | None:
    candidates = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTS]
    candidates.sort()
    return candidates[0] if candidates else None


def resolve_input_dir(args: argparse.Namespace) -> Path:
    if args.input_dir:
        input_dir = Path(args.input_dir).expanduser().resolve()
    elif args.cue:
        input_dir = Path(args.cue).expanduser().resolve().parent
    elif args.audio:
        input_dir = Path(args.audio).expanduser().resolve().parent
    else:
        fail("Provide --input-dir or --cue")

    if not input_dir.exists():
        fail(f"Input directory does not exist: {input_dir}")
    return input_dir


def select_cue_paths(args: argparse.Namespace, input_dir: Path) -> tuple[list[Path], bool]:
    """Return (cue_paths, multi_disc).

    multi_disc is True when the folder holds several disc images (each its own
    cue+audio), in which case every playable cue is processed into its own
    subfolder. Single-disc and "one real cue plus leftovers" cases keep the
    original single-cue behaviour.
    """
    if args.cue:
        return [Path(args.cue).expanduser().resolve()], False

    cues = sorted(input_dir.glob("*.cue"))
    if not cues:
        fail(f"Expected exactly one .cue in {input_dir}", details="Found 0 cue files")
    if len(cues) == 1:
        return [cues[0]], False

    playable = [c for c in cues if _cue_references_existing_audio(c)]
    if len(playable) == 1:
        return [playable[0]], False
    if len(playable) >= 2:
        return playable, True
    fail(
        f"Expected exactly one .cue in {input_dir}",
        details=(
            f"Found {len(cues)} cue files; "
            f"{len(playable)} reference audio present on disk"
        ),
    )


def resolve_audio_for_cue(
    args: argparse.Namespace, cue_path: Path, cue: CueSheet
) -> dict[str, Path]:
    distinct_files: list[str] = []
    seen: set[str] = set()
    for track in cue.tracks:
        name = track.file_name or cue.file_name or ""
        if name and name not in seen:
            seen.add(name)
            distinct_files.append(name)

    audio_paths: dict[str, Path] = {}
    if args.audio:
        override = Path(args.audio).expanduser().resolve()
        if len(distinct_files) > 1:
            fail("--audio cannot be used with a multi-file CUE")
        key = distinct_files[0] if distinct_files else override.name
        audio_paths[key] = override
    elif distinct_files:
        for name in distinct_files:
            audio_paths[name] = (cue_path.parent / name).resolve()
    else:
        guess = first_audio_file(cue_path.parent)
        if guess is None:
            fail(f"Could not find source audio beside {cue_path}")
        audio_paths[""] = guess
        for track in cue.tracks:
            if not track.file_name:
                track.file_name = ""

    for name, path in audio_paths.items():
        if not path.exists():
            fail(f"Source audio file does not exist: {path}")

    return audio_paths


def probe_duration(audio_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(audio_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        fail("ffprobe failed", details=result.stderr.strip())
    try:
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception as exc:
        fail("Could not parse ffprobe duration", details=str(exc))


def probe_audio_properties(audio_path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,bits_per_raw_sample,bits_per_sample",
        "-of",
        "json",
        str(audio_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        fail("ffprobe failed", details=result.stderr.strip())
    try:
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [{}])[0]
        sample_rate = int(stream.get("sample_rate") or 0)
        bit_depth = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 16)
        return sample_rate, bit_depth
    except Exception as exc:
        fail("Could not parse ffprobe audio properties", details=str(exc))


def sanitize_part(value: str) -> str:
    value = value.replace("�", "'")
    value = re.sub(r"[/:]+", " - ", value.strip())
    value = re.sub(r'[<>:"\\|?*]', "", value)
    value = re.sub(r"\s+", " ", value).strip().strip(".")
    return value or "Unknown"


def split_multi_value(value: str) -> list[str]:
    parts = re.split(r"\s*;\s*|\s*,\s*|\s+/\s+|\s+\|\s+", value.strip())
    return [p.strip() for p in parts if p.strip()]


def dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def clean_whitespace(value: str) -> str:
    value = value.replace("�", "'")
    return re.sub(r"\s+", " ", value).strip(" -;,\t")


def normalize_title(value: str) -> str:
    value = clean_whitespace(value)
    value = re.sub(r"\s*-\s*Bock to Bock$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^Something'\b", "Somethin'", value)
    return value


def guess_artist_from_track_performer(track_performer: str | None, album_artist: str) -> str:
    if not track_performer:
        return album_artist
    raw_groups = [clean_whitespace(p) for p in re.split(r"\s*;\s*", track_performer.strip()) if p.strip()]
    album_parts = {p.casefold() for p in split_multi_value(album_artist)}

    best_group = ""
    best_score = -1
    for group in raw_groups:
        parts = split_multi_value(group)
        score = sum(1 for p in parts if p.casefold() in album_parts)
        if score > best_score:
            best_score = score
            best_group = group

    if best_score > 0 and best_group:
        return ", ".join(dedupe_keep_order(split_multi_value(best_group)))

    candidates = split_multi_value(track_performer)
    kept = [c for c in candidates if c.casefold() in album_parts]
    if kept:
        return ", ".join(dedupe_keep_order(kept))
    return clean_whitespace(track_performer)


def normalize_album_artist(value: str) -> str:
    parts = dedupe_keep_order(split_multi_value(clean_whitespace(value)))
    return ", ".join(parts) if parts else "Unknown Artist"


def normalize_cue_fields(cue: CueSheet) -> CueSheet:
    cue.performer = normalize_album_artist(cue.performer)
    cue.title = normalize_title(cue.title)
    cue.genre = clean_whitespace(cue.genre)
    cue.date = clean_whitespace(cue.date)
    for track in cue.tracks:
        track.title = normalize_title(track.title)
        track.performer = guess_artist_from_track_performer(track.performer, cue.performer)
    return cue


def choose_output_root(input_dir: Path, output_root_arg: str | None) -> Path:
    if output_root_arg:
        return Path(output_root_arg).expanduser().resolve()
    return input_dir / "split-output"


def build_output_dir(root: Path, album_artist: str, year: str, album_title: str) -> Path:
    album_artist_dir = sanitize_part(album_artist or "Unknown Artist")
    album_stub = sanitize_part(album_title or "Unknown Album")
    if year:
        album_stub = f"{sanitize_part(year)} - {album_stub}"
    return root / album_artist_dir / album_stub


# A leading release year (1900-2099) followed by a non-digit, e.g. "1994. Title".
_YEAR_PREFIX_RE = re.compile(r"^((?:19|20)\d{2})(\D.*)$", re.DOTALL)
# Separator chars stripped between the year and the title (incl. comma).
_YEAR_SEP_CHARS = " \t.,_-–—"


def normalize_year_prefix(name: str) -> str:
    """Normalize ``"1994. Title"`` / ``"1994, Title"`` / ``"1994 Title"`` -> ``"1994 - Title"``.

    Only touches a leading 4-digit year that is not already separated from the
    title by ``" - "``; only the *leading* separator is replaced, so the title's
    own dots and commas are kept.
    """
    match = _YEAR_PREFIX_RE.match(name)
    if not match:
        return name
    year, rest = match.group(1), match.group(2)
    if rest[0] not in _YEAR_SEP_CHARS:
        return name  # year not followed by a recognized separator -> leave as-is
    title = rest.lstrip(_YEAR_SEP_CHARS)
    if not title:
        return name
    return f"{year} - {title}"


def build_preserved_output_dir(output_root: Path, source_root: Path, input_dir: Path) -> Path:
    try:
        relative = input_dir.relative_to(source_root)
    except ValueError:
        fail(f"Input directory {input_dir} is not inside source root {source_root}")
    # Normalize every component, not just the leaf: with multi-disc layouts the
    # year-bearing album folder is a parent of the actual input dir (e.g.
    # ".../1996. Концерт/CD 1"), so the leaf alone would miss it.
    parts = [normalize_year_prefix(part) for part in relative.parts]
    if parts:
        return output_root.joinpath(*parts)
    return output_root / relative


def assign_track_boundaries(cue: CueSheet, file_durations: dict[str, float]) -> None:
    by_file: dict[str | None, list[Track]] = {}
    for track in cue.tracks:
        by_file.setdefault(track.file_name, []).append(track)
    for file_name, group in by_file.items():
        duration = file_durations.get(file_name or "", 0.0)
        for idx, track in enumerate(group):
            if idx + 1 < len(group):
                track.end_seconds = group[idx + 1].start_seconds
            else:
                track.end_seconds = duration


def codec_settings(fmt: str) -> tuple[str, list[str]]:
    if fmt == "flac":
        return ".flac", ["-c:a", "flac", "-compression_level", "8"]
    if fmt == "alac":
        return ".m4a", ["-c:a", "alac", "-movflags", "+faststart"]
    fail(f"Unsupported output format: {fmt}")


def apple_alac_filter_args() -> list[str]:
    return ["-af", f"aformat=sample_fmts={APPLE_ALAC_SAMPLE_FMT}:sample_rates={APPLE_ALAC_SAMPLE_RATE}"]


def apple_alac_cap_filter_args(sample_rate: int, bit_depth: int) -> list[str]:
    target_rate = min(sample_rate or APPLE_ALAC_MAX_RATE, APPLE_ALAC_MAX_RATE)
    target_fmt = APPLE_ALAC_SAMPLE_FMT if (bit_depth or APPLE_ALAC_MAX_BITS) > 16 else "s16"
    return ["-af", f"aformat=sample_fmts={target_fmt}:sample_rates={target_rate}"]


_LEADING_TRACKNUM_RE = re.compile(r"^(\d{1,3})[.\-_)\s]+(?=\S)")


def strip_redundant_track_prefix(title: str, expected_number: int) -> str:
    if not title:
        return title
    match = _LEADING_TRACKNUM_RE.match(title)
    if not match:
        return title
    try:
        leading = int(match.group(1))
    except ValueError:
        return title
    if leading != expected_number:
        return title
    stripped = title[match.end():].strip()
    return stripped or title


def format_track_filename(track_number: int, title: str, ext: str) -> str:
    cleaned = strip_redundant_track_prefix(title or "", track_number)
    return f"{track_number:02d} - {sanitize_part(cleaned or f'Track {track_number:02d}')}{ext}"


def build_ffmpeg_command(
    *,
    audio_path: Path,
    cover_art: Path | None,
    output_path: Path,
    start_seconds: float,
    end_seconds: float,
    output_format: str,
    metadata: dict[str, str],
    force: bool,
    source_sample_rate: int | None = None,
    source_bit_depth: int | None = None,
) -> list[str]:
    ext, codec_args = codec_settings(output_format)
    _ = ext
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd.append("-y" if force else "-n")
    cmd += ["-ss", f"{start_seconds:.3f}", "-to", f"{end_seconds:.3f}", "-i", str(audio_path)]

    if cover_art:
        cmd += ["-i", str(cover_art)]

    cmd += ["-map", "0:a:0"]
    if cover_art:
        cmd += ["-map", "1:v:0"]

    cmd += codec_args
    if output_format == "alac":
        cmd += apple_alac_cap_filter_args(source_sample_rate or APPLE_ALAC_MAX_RATE, source_bit_depth or APPLE_ALAC_MAX_BITS)
    if cover_art:
        cmd += ["-c:v", "copy"]
        if output_format == "alac":
            cmd += ["-disposition:v:0", "attached_pic"]

    for key, value in metadata.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]

    cmd += [str(output_path)]
    return cmd


def process_cue(
    args: argparse.Namespace,
    input_dir: Path,
    cue_path: Path,
    output_subdir: str | None,
) -> None:
    cue = parse_cue(cue_path)
    audio_paths = resolve_audio_for_cue(args, cue_path, cue)

    if args.album_artist:
        cue.performer = args.album_artist
    if args.album_title:
        cue.title = args.album_title
    if args.year:
        cue.date = args.year
    if args.genre:
        cue.genre = args.genre
    if args.disc_number:
        cue.disc_number = args.disc_number

    if not cue.performer:
        cue.performer = "Unknown Artist"
    if not cue.title:
        first_path = next(iter(audio_paths.values()))
        cue.title = first_path.stem
    if args.normalize_tags or args.apple_library:
        cue = normalize_cue_fields(cue)

    output_root = choose_output_root(input_dir, args.output_root)
    if args.preserve_structure:
        if not args.source_root:
            fail("--preserve-structure requires --source-root")
        source_root = Path(args.source_root).expanduser().resolve()
        output_dir = build_preserved_output_dir(output_root, source_root, input_dir)
    else:
        output_dir = build_output_dir(output_root, cue.performer, cue.date, cue.title)
    if output_subdir:
        output_dir = output_dir / sanitize_part(output_subdir)
    cover_art = find_cover_art(cue_path.parent, disc_hint=output_subdir)

    file_durations: dict[str, float] = {}
    file_props: dict[str, tuple[int, int]] = {}
    for name, path in audio_paths.items():
        file_durations[name] = probe_duration(path)
        file_props[name] = probe_audio_properties(path)
    assign_track_boundaries(cue, file_durations)
    ext, _codec_args = codec_settings(args.format)

    print(f"CUE:        {cue_path}")
    if len(audio_paths) == 1:
        only_path = next(iter(audio_paths.values()))
        print(f"SOURCE:     {only_path}")
    else:
        print(f"SOURCES:    {len(audio_paths)} files")
        for name, path in audio_paths.items():
            print(f"  - {path}")
    print(f"FORMAT:     {args.format}")
    print(f"OUTPUT DIR: {output_dir}")
    if cover_art:
        print(f"COVER:      {cover_art}")

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for track in cue.tracks:
        file_name = track.file_name or ""
        audio_path = audio_paths.get(file_name)
        if audio_path is None:
            fail(f"Track {track.number:02d} references unknown source FILE: {file_name!r}")
        sample_rate, bit_depth = file_props[file_name]
        duration = file_durations[file_name]
        track_artist = track.performer or cue.performer
        track_title = track.title or f"Track {track.number:02d}"
        output_path = output_dir / format_track_filename(track.number, track_title, ext)
        metadata = {
            "title": track_title,
            "artist": track_artist,
            "album": cue.title,
            "album_artist": cue.performer,
            "track": str(track.number),
            "disc": str(cue.disc_number),
            "date": cue.date,
            "genre": cue.genre,
        }
        cmd = build_ffmpeg_command(
            audio_path=audio_path,
            cover_art=cover_art,
            output_path=output_path,
            start_seconds=track.start_seconds or 0.0,
            end_seconds=track.end_seconds or duration,
            output_format=args.format,
            metadata=metadata,
            force=args.force,
            source_sample_rate=sample_rate,
            source_bit_depth=bit_depth,
        )

        print(f"\n[{track.number:02d}] {track_title}")
        print(f"  start={track.start_seconds:.3f}s end={track.end_seconds:.3f}s")
        print(f"  -> {output_path}")

        if args.dry_run:
            print(f"  CMD: {shell_preview(cmd)}")
            continue

        track_duration = max(0.0, (track.end_seconds or duration) - (track.start_seconds or 0.0))
        label = f"[{track.number:02d}] {track_title}"
        result = run_ffmpeg_with_progress(cmd, track_duration, label)
        if result.returncode != 0:
            fail(
                f"ffmpeg failed for track {track.number:02d}",
                details=result.stderr.strip() or result.stdout.strip(),
            )

    print("\nDone.")
    return output_dir


def is_disc_dir(folder: Path) -> bool:
    """True if the folder name looks like a per-disc subfolder (CD 1, Disc 2, ...)."""
    return bool(DISC_DIR_RE.match(folder.name.strip()))


def artwork_source_and_dest(input_dir: Path, output_dir: Path, *, preserve: bool) -> tuple[Path, Path]:
    """Where to read artwork from and which release dir to copy it into.

    For a per-disc subfolder (``CD 1``/``CD 2``/...), covers usually live one
    level up in the album root (loose images or a ``Covers/`` folder) and the
    release output is the parent of the per-disc output, so read from and write
    to the album level. Otherwise use the folder itself.
    """
    if is_disc_dir(input_dir):
        dest = output_dir.parent if preserve else output_dir
        return input_dir.parent, dest
    return input_dir, output_dir


def copy_artwork(src_folder: Path, dest_dir: Path, *, dry_run: bool, force: bool) -> None:
    """Consolidate artwork images into a single ``Covers/`` folder in the output.

    Image files from the release root and from any recognised artwork subfolder
    (``Covers``/``Art``/``Artwork``/``Scans``/``tiff``/...) are gathered and
    copied into ``<dest_dir>/Covers``, flattening the differently-named source
    folders into one place.
    """
    try:
        entries = sorted(src_folder.iterdir())
    except OSError:
        return
    images: list[Path] = []
    for p in entries:
        try:
            if p.is_file() and p.suffix.lower() in ARTWORK_IMAGE_EXTS:
                images.append(p)
            elif p.is_dir() and p.name.lower() in COVER_SUBDIRS:
                images += [
                    q for q in sorted(p.rglob("*"))
                    if q.is_file() and q.suffix.lower() in ARTWORK_IMAGE_EXTS
                ]
        except OSError:
            continue
    if not images:
        return

    covers_dir = dest_dir / "Covers"
    for src in images:
        dst = covers_dir / src.name
        print(f"COPY ART:   {src} -> {dst}")
        if dry_run:
            continue
        if dst.exists() and not force:
            continue
        covers_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def main() -> None:
    args = parse_args()
    if args.apple_library:
        args.format = "alac"
        if not args.output_root:
            args.output_root = DEFAULT_APPLE_OUTPUT_ROOT

    input_dir = resolve_input_dir(args)
    cue_paths, multi_disc = select_cue_paths(args, input_dir)

    if multi_disc:
        print(f"Multi-disc box: {len(cue_paths)} discs in {input_dir}")

    failures = 0
    produced_dirs: list[Path] = []
    for cue_path in cue_paths:
        output_subdir = cue_path.stem if multi_disc else None
        if multi_disc:
            print(f"\n=== Disc: {cue_path.name} -> {output_subdir}/ ===")
        try:
            produced_dirs.append(process_cue(args, input_dir, cue_path, output_subdir))
        except SystemExit:
            if not multi_disc:
                raise
            print(f"ERROR: failed on {cue_path.name}", file=sys.stderr)
            failures += 1

    if args.copy_artwork:
        if args.preserve_structure:
            output_root = choose_output_root(input_dir, args.output_root)
            source_root = Path(args.source_root).expanduser().resolve()
            base = build_preserved_output_dir(output_root, source_root, input_dir)
            src, dest = artwork_source_and_dest(input_dir, base, preserve=True)
            copy_artwork(src, dest, dry_run=args.dry_run, force=args.force)
        else:
            for dest0 in dict.fromkeys(produced_dirs):
                src, dest = artwork_source_and_dest(input_dir, dest0, preserve=False)
                copy_artwork(src, dest, dry_run=args.dry_run, force=args.force)

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
