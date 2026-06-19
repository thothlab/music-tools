#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from split_album_image import (
    DEFAULT_APPLE_OUTPUT_ROOT,
    APPLE_ALAC_MAX_BITS,
    APPLE_ALAC_MAX_RATE,
    apple_alac_cap_filter_args,
    build_output_dir,
    build_preserved_output_dir,
    artwork_source_and_dest,
    choose_output_root,
    clean_whitespace,
    codec_settings,
    copy_artwork,
    dedupe_keep_order,
    find_cover_art,
    is_disc_dir,
    normalize_album_artist,
    run_ffmpeg_with_progress,
    sanitize_part,
    shell_preview,
    strip_redundant_track_prefix,
)


TRACK_AUDIO_EXTS = {".flac", ".m4a", ".wv", ".ape", ".wav", ".aiff", ".aif", ".alac"}


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def fail(message: str, *, details: str | None = None, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    if details:
        print(details, file=sys.stderr)
    raise SystemExit(code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a folder of track-based audio files into a track-based ALAC or FLAC album."
    )
    parser.add_argument("--input-dir", required=True, help="Folder containing track files")
    parser.add_argument("--format", choices=("flac", "alac"), default="alac")
    parser.add_argument("--output-root", help="Root output folder")
    parser.add_argument("--source-root", help="Root of the source tree, used with --preserve-structure")
    parser.add_argument(
        "--preserve-structure",
        action="store_true",
        help="Preserve source directory structure under the output root instead of rebuilding artist/album folders",
    )
    parser.add_argument("--apple-library", action="store_true")
    parser.add_argument(
        "--copy-artwork",
        action="store_true",
        help="Copy artwork dirs and loose cover images into the release output Covers/ folder",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def probe_file(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration,format_tags:stream=index,codec_type,codec_name,disposition,sample_rate,bits_per_raw_sample,bits_per_sample,sample_fmt,duration:stream_tags",
        "-of",
        "json",
        str(path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        fail("ffprobe failed", details=result.stderr.strip() or result.stdout.strip())
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"Could not parse ffprobe output for {path}", details=str(exc))


def track_files(folder: Path) -> list[Path]:
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in TRACK_AUDIO_EXTS]
    files.sort()
    return files


def parse_track_number(value: str) -> tuple[int, str]:
    raw = clean_whitespace(value or "")
    if not raw:
        return 0, ""
    if "/" in raw:
        left, _, right = raw.partition("/")
        return int(left or 0), right
    return int(raw), ""


def metadata_from_probe(data: dict) -> dict[str, str]:
    tags = {}
    format_tags = (data.get("format") or {}).get("tags") or {}
    for key, value in format_tags.items():
        tags[key.lower()] = value
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            for key, value in (stream.get("tags") or {}).items():
                tags.setdefault(key.lower(), value)
            break
    return tags


def has_embedded_cover(data: dict) -> bool:
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        disposition = stream.get("disposition") or {}
        if disposition.get("attached_pic") == 1:
            return True
    return False


def audio_codec_name(data: dict) -> str:
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            return (stream.get("codec_name") or "").lower()
    return ""


def audio_duration(data: dict) -> float:
    fmt = data.get("format") or {}
    try:
        value = float(fmt.get("duration") or 0.0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            try:
                value = float(stream.get("duration") or 0.0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
    return 0.0


def audio_properties(data: dict) -> tuple[int, int]:
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            sample_rate = int(stream.get("sample_rate") or 0)
            bit_depth = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 16)
            return sample_rate, bit_depth
    return APPLE_ALAC_MAX_RATE, APPLE_ALAC_MAX_BITS


def build_album_context(folder: Path, first_tags: dict[str, str]) -> tuple[str, str, str, str]:
    album_artist = (
        first_tags.get("album_artist")
        or first_tags.get("albumartist")
        or first_tags.get("artist")
        or folder.parent.name
    )
    album = first_tags.get("album") or folder.name
    year = first_tags.get("date") or first_tags.get("year") or ""
    genre = first_tags.get("genre") or ""
    return normalize_album_artist(album_artist), clean_whitespace(album), clean_whitespace(year), clean_whitespace(genre)


def build_track_context(path: Path, tags: dict[str, str], fallback_index: int, album_artist: str) -> dict[str, str]:
    number, _ = parse_track_number(tags.get("track") or tags.get("tracknumber") or "")
    disc, _ = parse_track_number(tags.get("disc") or tags.get("discnumber") or "")
    effective_number = number or fallback_index
    raw_title = tags.get("title") or path.stem
    title = clean_whitespace(strip_redundant_track_prefix(raw_title, effective_number))
    artist = clean_whitespace(tags.get("artist") or album_artist)
    return {
        "track": str(effective_number),
        "disc": str(disc or 1),
        "title": title,
        "artist": artist,
    }


def format_output_filename(track_number: str, title: str, ext: str) -> str:
    try:
        number = int(track_number)
    except ValueError:
        number = 0
    prefix = f"{number:02d}" if number > 0 else "00"
    cleaned = strip_redundant_track_prefix(title, number)
    return f"{prefix} - {sanitize_part(cleaned)}{ext}"


def build_ffmpeg_command(
    *,
    source_path: Path,
    output_path: Path,
    output_format: str,
    metadata: dict[str, str],
    sidecar_cover: Path | None,
    embedded_cover: bool,
    source_codec: str,
    force: bool,
    source_sample_rate: int,
    source_bit_depth: int,
) -> list[str]:
    ext, codec_args = codec_settings(output_format)
    _ = ext
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd.append("-y" if force else "-n")
    cmd += ["-i", str(source_path)]
    if sidecar_cover:
        cmd += ["-i", str(sidecar_cover)]

    cmd += ["-map_metadata", "0", "-map", "0:a:0"]
    if sidecar_cover:
        cmd += ["-map", "1:v:0"]
    elif embedded_cover:
        cmd += ["-map", "0:v?"]

    if output_format == "alac" and source_codec == "alac":
        if source_sample_rate <= APPLE_ALAC_MAX_RATE and source_bit_depth <= APPLE_ALAC_MAX_BITS:
            cmd += ["-c:a", "copy", "-movflags", "+faststart"]
        else:
            cmd += codec_args
            cmd += apple_alac_cap_filter_args(source_sample_rate, source_bit_depth)
    else:
        cmd += codec_args
        if output_format == "alac":
            cmd += apple_alac_cap_filter_args(source_sample_rate, source_bit_depth)

    if sidecar_cover or embedded_cover:
        cmd += ["-c:v", "copy", "-disposition:v:0", "attached_pic"]

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

    input_dir = Path(args.input_dir).expanduser().resolve()
    if not input_dir.exists():
        fail(f"Input directory does not exist: {input_dir}")

    files = track_files(input_dir)
    if not files:
        fail(f"No supported track files found in {input_dir}")

    probes = {path: probe_file(path) for path in files}
    first_tags = metadata_from_probe(probes[files[0]])
    album_artist, album_title, year, genre = build_album_context(input_dir, first_tags)
    output_root = choose_output_root(input_dir, args.output_root)
    if args.preserve_structure:
        if not args.source_root:
            fail("--preserve-structure requires --source-root")
        source_root = Path(args.source_root).expanduser().resolve()
        output_dir = build_preserved_output_dir(output_root, source_root, input_dir)
    else:
        output_dir = build_output_dir(output_root, album_artist, year, album_title)
    disc_hint = input_dir.name if is_disc_dir(input_dir) else None
    sidecar_cover = find_cover_art(input_dir, disc_hint=disc_hint)
    ext, _codec_args = codec_settings(args.format)

    print(f"SOURCE DIR: {input_dir}")
    print(f"FORMAT:     {args.format}")
    print(f"OUTPUT DIR: {output_dir}")
    if sidecar_cover:
        print(f"COVER:      {sidecar_cover}")

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for idx, source_path in enumerate(files, start=1):
        probe = probes[source_path]
        tags = metadata_from_probe(probe)
        source_sample_rate, source_bit_depth = audio_properties(probe)
        track_meta = build_track_context(source_path, tags, idx, album_artist)
        output_name = format_output_filename(track_meta["track"], track_meta["title"], ext)
        output_path = output_dir / output_name
        metadata = {
            "title": track_meta["title"],
            "artist": track_meta["artist"],
            "album": album_title,
            "album_artist": album_artist,
            "track": track_meta["track"],
            "disc": track_meta["disc"],
            "date": year,
            "genre": genre,
        }
        cmd = build_ffmpeg_command(
            source_path=source_path,
            output_path=output_path,
            output_format=args.format,
            metadata=metadata,
            sidecar_cover=sidecar_cover,
            embedded_cover=has_embedded_cover(probe),
            source_codec=audio_codec_name(probe),
            force=args.force,
            source_sample_rate=source_sample_rate,
            source_bit_depth=source_bit_depth,
        )

        print(f"\n[{idx:02d}] {source_path.name}")
        print(f"  -> {output_path}")
        if args.dry_run:
            print(f"  CMD: {shell_preview(cmd)}")
            continue

        duration = audio_duration(probe)
        label = f"[{idx:02d}] {track_meta['title']}"
        result = run_ffmpeg_with_progress(cmd, duration, label)
        if result.returncode != 0:
            fail(
                f"ffmpeg failed for {source_path.name}",
                details=result.stderr.strip() or result.stdout.strip(),
            )

    if args.copy_artwork:
        art_src, art_dest = artwork_source_and_dest(
            input_dir, output_dir, preserve=args.preserve_structure
        )
        copy_artwork(art_src, art_dest, dry_run=args.dry_run, force=args.force)

    print("\nDone.")


if __name__ == "__main__":
    main()
