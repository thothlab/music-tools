#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

TRACK_AUDIO_EXTS = {".flac", ".m4a", ".wv", ".ape", ".wav", ".aiff", ".aif", ".alac"}

def shell_preview(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


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
        action="store_true",
        help="Preserve source directory structure under the output root",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
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
    cue_dirs = sorted({path.parent for path in root.rglob("*.cue")})

    failures = 0
    processed = 0
    limit = args.limit if args.limit > 0 else None

    print(f"Found {len(cue_dirs)} CUE folders under {root}")
    for idx, folder in enumerate(cue_dirs, start=1):
        if limit is not None and processed >= limit:
            break
        cmd = [sys.executable, str(script_path), "--input-dir", str(folder), "--format", args.format]
        if args.output_root:
            cmd += ["--output-root", args.output_root]
        if args.apple_library:
            cmd.append("--apple-library")
        if args.normalize_tags:
            cmd.append("--normalize-tags")
        if args.preserve_structure:
            cmd += ["--preserve-structure", "--source-root", str(root)]
        if args.dry_run:
            cmd.append("--dry-run")
        if args.force:
            cmd.append("--force")

        print(f"\n[CUE {idx}/{len(cue_dirs)}] {folder}")
        print(shell_preview(cmd))

        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        processed += 1
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0:
            failures += 1
            if result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)

    if args.copy_non_cue_alac and (limit is None or processed < limit):
        cue_dir_set = set(cue_dirs)
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

        print(f"Found {len(non_cue_dirs)} non-CUE track folders under {root}")
        for idx, folder in enumerate(non_cue_dirs, start=1):
            if limit is not None and processed >= limit:
                break
            cmd = [sys.executable, str(track_script_path), "--input-dir", str(folder), "--apple-library"]
            if args.output_root:
                cmd += ["--output-root", args.output_root]
            if args.preserve_structure:
                cmd += ["--preserve-structure", "--source-root", str(root)]
            if args.dry_run:
                cmd.append("--dry-run")
            if args.force:
                cmd.append("--force")

            print(f"\n[TRACK {idx}/{len(non_cue_dirs)}] {folder}")
            print(shell_preview(cmd))

            result = subprocess.run(cmd, text=True, capture_output=True, check=False)
            processed += 1
            if result.stdout:
                print(result.stdout.rstrip())
            if result.returncode != 0:
                failures += 1
                if result.stderr:
                    print(result.stderr.rstrip(), file=sys.stderr)

    if failures:
        print(f"\nFinished with {failures} failure(s).", file=sys.stderr)
        raise SystemExit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
