#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path

TRACK_AUDIO_EXTS = {".flac", ".m4a", ".wv", ".ape", ".wav", ".aiff", ".aif", ".alac"}
CUE_FILE_LINE_RE = re.compile(r'^\s*FILE\s+"?(.*?)"?\s+\S+\s*$', re.IGNORECASE)


class Interrupted(Exception):
    pass


def run_child(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(cmd, text=True, check=False)
    except KeyboardInterrupt:
        raise Interrupted()
    if result.returncode in (130, -signal.SIGINT, -signal.SIGTERM):
        raise Interrupted()
    return result


def shell_preview(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def cue_is_track_based(cue_path: Path) -> bool:
    """True if every FILE in the cue has exactly one TRACK (album is already split)."""
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    file_count = 0
    track_count = 0
    saw_any_file = False
    for line in text.splitlines():
        stripped = line.lstrip()
        upper = stripped.upper()
        if upper.startswith("FILE "):
            file_count += 1
            saw_any_file = True
        elif upper.startswith("TRACK "):
            track_count += 1
    return saw_any_file and file_count >= 2 and track_count == file_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recursively find CUE-based releases and split them into tracks."
    )
    parser.add_argument("--root", required=True, help="Root folder to scan")
    parser.add_argument("--format", choices=("flac", "alac"), default="flac")
    parser.add_argument("--output-root", help="Shared output root for all releases")
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
        "--copy-non-cue-alac",
        action="store_true",
        help="Also convert track-based folders without .cue into a separate ALAC library",
    )
    parser.add_argument(
        "--preserve-structure",
        dest="preserve_structure",
        action="store_true",
        default=True,
        help="Preserve source directory structure under the output root (default)",
    )
    parser.add_argument(
        "--rebuild-by-artist",
        dest="preserve_structure",
        action="store_false",
        help="Instead of mirroring, rebuild output as <album_artist>/<year - album>/",
    )
    parser.add_argument(
        "--source-root",
        help="Root the relative output paths are computed from; defaults to the parent of --root",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--copy-artwork",
        action="store_true",
        help="Copy artwork dirs and loose images into each release output, mirroring source layout",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N matching folders; 0 means no limit",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"ERROR: root does not exist: {root}", file=sys.stderr)
        raise SystemExit(1)

    script_path = Path(__file__).with_name("split_album_image.py")
    track_script_path = Path(__file__).with_name("convert_track_album.py")

    if args.preserve_structure:
        source_root = (
            Path(args.source_root).expanduser().resolve()
            if args.source_root
            else root.parent
        )
        if root != source_root and source_root not in root.parents:
            print(
                f"ERROR: --source-root {source_root} is not an ancestor of --root {root}",
                file=sys.stderr,
            )
            raise SystemExit(1)
    else:
        source_root = None

    all_cue_dirs = sorted({path.parent for path in root.rglob("*.cue")})

    image_cue_dirs: list[Path] = []
    track_based_cue_dirs: list[Path] = []
    for folder in all_cue_dirs:
        cues = sorted(folder.glob("*.cue"))
        if cues and all(cue_is_track_based(cue) for cue in cues):
            track_based_cue_dirs.append(folder)
        else:
            image_cue_dirs.append(folder)

    failed: list[tuple[str, Path]] = []
    processed = 0
    limit = args.limit if args.limit > 0 else None

    print(f"Found {len(image_cue_dirs)} CUE folders under {root}")
    if track_based_cue_dirs:
        print(
            f"Detected {len(track_based_cue_dirs)} track-based CUE folders "
            f"(multiple FILE entries) — will route to track converter"
        )
    for idx, folder in enumerate(image_cue_dirs, start=1):
        if limit is not None and processed >= limit:
            break
        cmd = [sys.executable, str(script_path), "--input-dir", str(folder), "--format", args.format]
        if args.output_root:
            cmd += ["--output-root", args.output_root]
        if args.apple_library:
            cmd.append("--apple-library")
        if args.normalize_tags:
            cmd.append("--normalize-tags")
        if args.preserve_structure and source_root is not None:
            cmd += ["--preserve-structure", "--source-root", str(source_root)]
        if args.dry_run:
            cmd.append("--dry-run")
        if args.force:
            cmd.append("--force")
        if args.copy_artwork:
            cmd.append("--copy-artwork")

        print(f"\n[CUE {idx}/{len(image_cue_dirs)}] {folder}")
        print(shell_preview(cmd))

        result = run_child(cmd)
        processed += 1
        if result.returncode != 0:
            failed.append(("CUE", folder))

    if args.copy_non_cue_alac and (limit is None or processed < limit):
        cue_dir_set = set(all_cue_dirs)
        output_root = Path(args.output_root).expanduser().resolve() if args.output_root else None
        non_cue_dirs = []
        for folder in sorted({path.parent for path in root.rglob("*")}):
            if not folder.is_dir():
                continue
            if folder in cue_dir_set:
                continue
            if output_root and (folder == output_root or output_root in folder.parents):
                continue
            try:
                files = [p for p in folder.iterdir() if p.is_file()]
            except OSError:
                continue
            if any(p.suffix.lower() == ".cue" for p in files):
                continue
            if any(p.suffix.lower() in TRACK_AUDIO_EXTS for p in files):
                non_cue_dirs.append(folder)

        track_dirs = track_based_cue_dirs + non_cue_dirs
        print(
            f"Found {len(non_cue_dirs)} non-CUE track folders under {root}"
            + (f" (+{len(track_based_cue_dirs)} track-based CUE folders)" if track_based_cue_dirs else "")
        )
        for idx, folder in enumerate(track_dirs, start=1):
            if limit is not None and processed >= limit:
                break
            cmd = [sys.executable, str(track_script_path), "--input-dir", str(folder), "--apple-library"]
            if args.output_root:
                cmd += ["--output-root", args.output_root]
            if args.preserve_structure and source_root is not None:
                cmd += ["--preserve-structure", "--source-root", str(source_root)]
            if args.dry_run:
                cmd.append("--dry-run")
            if args.force:
                cmd.append("--force")
            if args.copy_artwork:
                cmd.append("--copy-artwork")

            print(f"\n[TRACK {idx}/{len(track_dirs)}] {folder}")
            print(shell_preview(cmd))

            result = run_child(cmd)
            processed += 1
            if result.returncode != 0:
                failed.append(("TRACK", folder))

    if failed:
        print(f"\nFinished with {len(failed)} failure(s):", file=sys.stderr)
        for kind, folder in failed:
            print(f"  [{kind}] {folder}", file=sys.stderr)
        raise SystemExit(1)

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, Interrupted):
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
