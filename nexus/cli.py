"""Command-line interface for the local nexus archive."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .archive import Archive, inspect
from .metadata import ExifTool, NexusError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="nexus", description="nexus — a local photo archive for your memories")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--archive", type=Path, default=Path(os.environ.get("NEXUS_ARCHIVE", "~/.local/share/nexus")),
                      help="archive directory (default: NEXUS_ARCHIVE or ~/.local/share/nexus)")
    root.add_argument("--exiftool", help="ExifTool executable (overrides NEXUS_EXIFTOOL)")
    commands = root.add_subparsers(dest="command", required=True)

    def command(name, help):
        child = commands.add_parser(name, help=help)
        child.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        return child

    command("doctor", "check Python, SQLite, ExifTool, and archive location without creating an archive")
    check = command("inspect", "read photo metadata and report coverage without importing")
    check.add_argument("folder", type=Path)
    ingest = command("import", "copy photos into the archive and organize moments")
    ingest.add_argument("folder", type=Path)
    ingest.add_argument("--chapter", help="add these photos to a named chapter")
    ingest.add_argument("--trip", help="add these photos to a named trip")
    photos = command("photos", "list photo IDs, dates, and filenames")
    photos.add_argument("--chapter")
    photos.add_argument("--trip")
    photos.add_argument("--limit", type=int, default=50, help="maximum results; 0 means all")
    show = command("show", "show a photo and its original extracted metadata")
    show.add_argument("photo_id")
    annotate = command("annotate", "save a caption; an empty caption clears it")
    annotate.add_argument("photo_id")
    annotate.add_argument("--caption", required=True)
    command("moments", "list proposed moments and their photo IDs")
    regroup = command("regroup", "rebuild moments without changing captions or collections")
    regroup.add_argument("--gap-hours", type=float)
    regroup.add_argument("--radius-meters", type=float)
    command("batches", "review import history and failures")
    command("export", "write a versioned JSON manifest and CSV inventory")
    for kind in ("trip", "chapter"):
        collection = commands.add_parser(kind, help=f"manage named {kind}s")
        actions = collection.add_subparsers(dest="action", required=True)
        for action in ("create", "add", "remove", "list"):
            operation = actions.add_parser(action)
            operation.add_argument("--json", action="store_true")
            if action != "list":
                operation.add_argument("name")
            if action in ("add", "remove"):
                operation.add_argument("photo_ids", nargs="+")
    return root


def emit(data, as_json=False):
    # Operations with a detailed shape deliberately use JSON even in human mode.
    print(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))


def issues(result):
    for failure in result.get("failures", []):
        print(f"  Failed: {failure['path']} — {failure['error']}")
    for skip in result.get("skipped", []):
        print(f"  Skipped: {skip['path']} — {skip['reason']}")
    for warning in result.get("warnings", []):
        print(f"  Warning: {warning['path']} — {'; '.join(warning['warnings'])}")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    archive = Archive(args.archive)
    try:
        if args.command == "doctor":
            try:
                extractor = ExifTool(args.exiftool)
                version = extractor.version()
                error = None
            except NexusError as exc:
                version, error = None, str(exc)
            result = {"nexus": __version__, "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                      "exiftool": version, "archive": str(archive.root), "archive_exists": archive.db_path.is_file(),
                      "ready": error is None, "error": error}
            emit(result)
            return 0 if result["ready"] else 1
        if args.command == "inspect":
            result = inspect(args.folder, ExifTool(args.exiftool), archive.root)
            if args.json:
                emit(result)
            else:
                print(f"{result['supported_files']} supported files · {len(result['failures'])} failures · {len(result['skipped'])} skipped")
                print(f"Capture dates: {result['with_date']} · GPS: {result['with_gps']} · Explicit timezones: {result['with_timezone']}")
                print(f"Date range: {result['date_start'] or 'unknown'} → {result['date_end'] or 'unknown'}")
                print(f"Formats: {result['formats']}")
                issues(result)
                for photo in result["photos"]:
                    for warning in photo["warnings"]:
                        print(f"  Warning: {photo['path']} — {warning}")
            return 1 if result["failures"] or any(s.get("error") for s in result["skipped"]) else 0
        if args.command == "import":
            def progress(index, total):
                if not args.json and (index == 1 or index % 25 == 0 or index == total):
                    print(f"Processing photos: {index}/{total}", file=sys.stderr)
            result = archive.import_folder(args.folder, ExifTool(args.exiftool), args.chapter, args.trip, progress)
            if args.json:
                emit(result)
            else:
                print(f"Imported {result['added']} new photos · {result['duplicates']} duplicates · {result['moments']} total moments")
                print(f"Missing capture date: {result['missing_date']} · Missing GPS: {result['missing_gps']}")
                print(f"Batch: {result['batch_id']} ({result['status']})\nArchive: {archive.root}")
                issues(result)
            return 1 if result["status"] == "partial" else 0
        # Avoid creating empty archives due to typos in read/organization commands.
        if not archive.db_path.is_file():
            raise NexusError("No archive found here. Run nexus import first (or select the correct --archive)")
        if args.command == "photos":
            if args.limit < 0:
                raise NexusError("--limit must be zero or greater")
            result = archive.photos(chapter=args.chapter, trip=args.trip)
            if args.limit:
                result = result[:args.limit]
            if args.json:
                emit(result)
            else:
                for photo in result:
                    print(f"{photo['id'][:12]}  {photo['captured_at'] or 'unknown date':<32}  {photo['original_filename']}")
                print(f"{len(result)} photos shown; use --limit 0 for all or --json for full records")
        elif args.command == "show":
            emit(archive.photos(prefix=args.photo_id)[0])
        elif args.command == "annotate":
            emit(archive.annotate(args.photo_id, args.caption))
        elif args.command == "regroup":
            emit(archive.regroup(args.gap_hours, args.radius_meters))
        elif args.command == "moments":
            emit(archive.moments())
        elif args.command == "batches":
            emit(archive.batches())
        elif args.command == "export":
            emit(archive.export())
        elif args.command in ("trip", "chapter"):
            if args.action == "create":
                emit(archive.create_collection(args.command, args.name))
            elif args.action == "list":
                emit(archive.collections(args.command))
            else:
                emit(archive.assign(args.command, args.name, args.photo_ids, remove=args.action == "remove"))
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Committed photos are safe; repeat the import to resume.", file=sys.stderr)
        return 130
    except (NexusError, OSError, sqlite3.Error) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"nexus: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
