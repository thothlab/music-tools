#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
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
SUPPORTED_COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png")
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


def parse_cue(cue_path: Path) -> CueSheet:
    cue = CueSheet()
    current_track: Track | None = None
    inside_track = False

    for raw_line in cue_path.read_text(encoding="utf-8", errors="replace").splitlines():
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
            current_track = Track(number=int(match.group(1)))
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


def find_cover_art(folder: Path) -> Path | None:
    for name in SUPPORTED_COVER_NAMES:
        candidate = folder / name
        if candidate.exists():
            return candidate
    return None


def first_audio_file(folder: Path) -> Path | None:
    candidates = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTS]
    candidates.sort()
    return candidates[0] if candidates else None


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
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

    if args.cue:
        cue_path = Path(args.cue).expanduser().resolve()
    else:
        cues = sorted(input_dir.glob("*.cue"))
        if len(cues) != 1:
            fail(
                f"Expected exactly one .cue in {input_dir}",
                details=f"Found {len(cues)} cue files",
            )
        cue_path = cues[0]

    cue = parse_cue(cue_path)
    if args.audio:
        audio_path = Path(args.audio).expanduser().resolve()
    elif cue.file_name:
        audio_path = (cue_path.parent / cue.file_name).resolve()
    else:
        audio_path = first_audio_file(cue_path.parent)
        if audio_path is None:
            fail(f"Could not find source audio beside {cue_path}")

    if not audio_path.exists():
        fail(f"Source audio file does not exist: {audio_path}")

    return input_dir, cue_path, audio_path


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


def build_preserved_output_dir(output_root: Path, source_root: Path, input_dir: Path) -> Path:
    try:
        relative = input_dir.relative_to(source_root)
    except ValueError:
        fail(f"Input directory {input_dir} is not inside source root {source_root}")
    return output_root / relative


def assign_track_boundaries(cue: CueSheet, audio_duration: float) -> None:
    for idx, track in enumerate(cue.tracks):
        next_track = cue.tracks[idx + 1] if idx + 1 < len(cue.tracks) else None
        track.end_seconds = next_track.start_seconds if next_track else audio_duration


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


def format_track_filename(track_number: int, title: str, ext: str) -> str:
    return f"{track_number:02d} - {sanitize_part(title or f'Track {track_number:02d}')}{ext}"


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


def main() -> None:
    args = parse_args()
    if args.apple_library:
        args.format = "alac"
        if not args.output_root:
            args.output_root = DEFAULT_APPLE_OUTPUT_ROOT
    input_dir, cue_path, audio_path = resolve_paths(args)
    cue = parse_cue(cue_path)

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
        cue.title = audio_path.stem
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
    cover_art = find_cover_art(cue_path.parent)
    duration = probe_duration(audio_path)
    sample_rate, bit_depth = probe_audio_properties(audio_path)
    assign_track_boundaries(cue, duration)
    ext, _codec_args = codec_settings(args.format)

    print(f"CUE:        {cue_path}")
    print(f"SOURCE:     {audio_path}")
    print(f"FORMAT:     {args.format}")
    print(f"OUTPUT DIR: {output_dir}")
    if cover_art:
        print(f"COVER:      {cover_art}")

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for track in cue.tracks:
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

        result = run(cmd)
        if result.returncode != 0:
            fail(
                f"ffmpeg failed for track {track.number:02d}",
                details=result.stderr.strip() or result.stdout.strip(),
            )

    print("\nDone.")


if __name__ == "__main__":
    main()
