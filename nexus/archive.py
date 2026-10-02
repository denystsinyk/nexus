"""Persistent, incremental archive. Source files are only ever read."""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .metadata import ExifTool, NexusError, SUPPORTED_EXTENSIONS

SCHEMA_VERSION = 1
DEFAULT_GAP_HOURS = 3.0
DEFAULT_RADIUS_METERS = 250.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def is_inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def scan(source: Path, excluded: Path | None = None) -> tuple[list[Path], list[dict]]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise NexusError(f"Photo folder does not exist or is not a directory: {source}")
    if excluded and is_inside(source, excluded.resolve()):
        raise NexusError("Import source must be outside the managed archive")
    files, skipped = [], []

    def walk_error(exc):
        skipped.append({"path": str(exc.filename), "reason": str(exc), "error": True})

    for directory, dirs, names in os.walk(source, followlinks=False, onerror=walk_error):
        current = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (current / d).is_symlink()
                         and (excluded is None or not is_inside((current / d).resolve(), excluded.resolve())))
        for name in sorted(names):
            path = current / name
            if path.is_symlink():
                skipped.append({"path": str(path), "reason": "symlink"})
            elif path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                skipped.append({"path": str(path), "reason": "unsupported file type"})
            else:
                files.append(path)
    return sorted(files), skipped


def inspect(source: Path, extractor: ExifTool, excluded: Path | None = None) -> dict:
    files, skipped = scan(source, excluded)
    result = {"source": str(source.expanduser().resolve()), "supported_files": len(files),
              "formats": {}, "with_date": 0, "with_gps": 0, "with_timezone": 0,
              "date_start": None, "date_end": None, "photos": [], "failures": [],
              "skipped": skipped}
    dates = []
    for path in files:
        extension = path.suffix.lower()
        result["formats"][extension] = result["formats"].get(extension, 0) + 1
        try:
            metadata = extractor.extract(path)
            if metadata["captured_at"]:
                result["with_date"] += 1
                dates.append(metadata["captured_at"][:10])
            result["with_gps"] += metadata["latitude"] is not None
            result["with_timezone"] += metadata["timezone_offset"] is not None
            result["photos"].append({"path": str(path), **{k: v for k, v in metadata.items() if k != "raw_metadata"}})
        except (NexusError, OSError) as exc:
            result["failures"].append({"path": str(path), "error": str(exc)})
    if dates:
        result["date_start"], result["date_end"] = min(dates), max(dates)
    return result


def distance_meters(a_lat, a_lon, b_lat, b_lon) -> float:
    lat1, lat2 = math.radians(a_lat), math.radians(b_lat)
    dlat, dlon = lat2 - lat1, math.radians(b_lon - a_lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


class Archive:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.db_path = self.root / "archive.sqlite3"

    @contextmanager
    def connection(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The local CLI has one writer at a time. flock is released even on a crash.
        with (self.root / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise NexusError("Another nexus command is using this archive; try again when it finishes") from exc
            db = sqlite3.connect(self.db_path)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys = ON")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise NexusError(f"Unsupported archive schema {version}; this version supports {SCHEMA_VERSION}")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS photos (
                        id TEXT PRIMARY KEY, archive_path TEXT NOT NULL UNIQUE,
                        original_filename TEXT NOT NULL, byte_size INTEGER NOT NULL,
                        imported_at TEXT NOT NULL, captured_at TEXT, timezone_offset TEXT,
                        latitude REAL, longitude REAL, metadata_json TEXT NOT NULL,
                        caption TEXT
                    );
                    CREATE TABLE IF NOT EXISTS sources (
                        photo_id TEXT NOT NULL REFERENCES photos(id), source_path TEXT NOT NULL,
                        PRIMARY KEY(photo_id, source_path)
                    );
                    CREATE TABLE IF NOT EXISTS batches (
                        id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
                        finished_at TEXT, status TEXT NOT NULL, summary_json TEXT
                    );
                    CREATE TABLE IF NOT EXISTS batch_items (
                        batch_id TEXT NOT NULL REFERENCES batches(id), source_path TEXT NOT NULL,
                        photo_id TEXT REFERENCES photos(id), status TEXT NOT NULL, detail TEXT,
                        PRIMARY KEY(batch_id, source_path)
                    );
                    CREATE TABLE IF NOT EXISTS collections (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('chapter', 'trip')),
                        name TEXT NOT NULL, UNIQUE(kind, name)
                    );
                    CREATE TABLE IF NOT EXISTS collection_photos (
                        collection_id TEXT NOT NULL REFERENCES collections(id),
                        photo_id TEXT NOT NULL REFERENCES photos(id),
                        PRIMARY KEY(collection_id, photo_id)
                    );
                    CREATE TABLE IF NOT EXISTS moments (
                        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
                        timezone_offset TEXT, latitude REAL NOT NULL, longitude REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS moment_photos (
                        moment_id TEXT NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
                        photo_id TEXT NOT NULL UNIQUE REFERENCES photos(id), position INTEGER NOT NULL,
                        PRIMARY KEY(moment_id, photo_id)
                    );
                    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS photos_capture ON photos(captured_at);
                """)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                db.execute("INSERT OR IGNORE INTO settings VALUES ('gap_hours', ?)", (str(DEFAULT_GAP_HOURS),))
                db.execute("INSERT OR IGNORE INTO settings VALUES ('radius_meters', ?)", (str(DEFAULT_RADIUS_METERS),))
                db.commit()
                yield db
            finally:
                db.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _collection(db, kind: str, name: str, create: bool = False) -> str:
        if kind not in ("trip", "chapter") or not name.strip():
            raise NexusError("Collection kind must be trip/chapter and its name must not be blank")
        row = db.execute("SELECT id FROM collections WHERE kind=? AND name=?", (kind, name)).fetchone()
        if row:
            return row[0]
        if not create:
            raise NexusError(f"No {kind} named {name!r}; create it first")
        identifier = f"{kind}_{uuid.uuid4().hex}"
        db.execute("INSERT INTO collections VALUES (?, ?, ?)", (identifier, kind, name))
        return identifier

    @staticmethod
    def _resolve(db, prefix: str) -> str:
        if not prefix or any(c not in "0123456789abcdef" for c in prefix):
            raise NexusError("Photo IDs are lowercase SHA-256 hashes (or a unique prefix)")
        rows = db.execute("SELECT id FROM photos WHERE id LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        if len(rows) != 1:
            raise NexusError(f"Photo ID {prefix!r} is {'ambiguous' if rows else 'unknown'}")
        return rows[0][0]

    def import_folder(self, source: Path, extractor: ExifTool, chapter: str | None = None,
                      trip: str | None = None, progress=None) -> dict:
        files, skipped = scan(source, self.root)
        if chapter is not None and not chapter.strip() or trip is not None and not trip.strip():
            raise NexusError("Chapter and trip names must not be blank")
        batch_id = "batch_" + uuid.uuid4().hex
        report = {"batch_id": batch_id, "source": str(source.expanduser().resolve()),
                  "supported_files": len(files), "added": 0, "duplicates": 0,
                  "missing_date": 0, "missing_gps": 0, "failures": [],
                  "warnings": [], "skipped": skipped}
        with self.connection() as db:
            # Recover interrupted batches. Their committed photos remain usable.
            db.execute("UPDATE batches SET status='interrupted', finished_at=? WHERE status='running'", (now(),))
            db.execute("INSERT INTO batches VALUES (?, ?, ?, NULL, 'running', NULL)",
                       (batch_id, report["source"], now()))
            collections = [self._collection(db, kind, name, create=True)
                           for kind, name in (("chapter", chapter), ("trip", trip)) if name is not None]
            db.commit()
            staging = self.root / ".staging"
            staging.mkdir(exist_ok=True)
            # These are only nexus-owned partial copies, under the exclusive lock.
            for leftover in staging.glob("copy-*"):
                if leftover.is_file():
                    leftover.unlink()
            try:
                for index, path in enumerate(files, 1):
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(prefix="copy-", suffix=path.suffix.lower(), dir=staging,
                                                         delete=False) as target:
                            temporary = Path(target.name)
                            digest = hashlib.sha256()
                            with path.open("rb") as original:
                                before = os.fstat(original.fileno())
                                while chunk := original.read(1024 * 1024):
                                    target.write(chunk)
                                    digest.update(chunk)
                                after = os.fstat(original.fileno())
                            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                raise NexusError("Source changed during import; retry when copying has finished")
                            target.flush()
                            os.fsync(target.fileno())
                        identifier = digest.hexdigest()
                        existing = db.execute("SELECT * FROM photos WHERE id=?", (identifier,)).fetchone()
                        if existing:
                            metadata = json.loads(existing["metadata_json"])
                            destination = self.root / existing["archive_path"]
                            if destination.is_file():
                                with destination.open("rb") as current:
                                    intact = hashlib.file_digest(current, "sha256").hexdigest() == identifier
                            else:
                                intact = False
                            if not intact:
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                os.replace(temporary, destination)
                            status = "duplicate"
                        else:
                            metadata = extractor.extract(temporary)
                            suffix = {"JPEG": ".jpg", "PNG": ".png", "HEIC": ".heic", "HEIF": ".heif"}[metadata["file_type"]]
                            relative = Path("originals") / identifier[:2] / (identifier + suffix)
                            destination = self.root / relative
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(temporary, destination)
                            db.execute("""INSERT INTO photos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                                       (identifier, relative.as_posix(), path.name, before.st_size, now(),
                                        metadata["captured_at"], metadata["timezone_offset"],
                                        metadata["latitude"], metadata["longitude"], encode(metadata)))
                            status = "added"
                        db.execute("INSERT OR IGNORE INTO sources VALUES (?, ?)", (identifier, str(path)))
                        for collection in collections:
                            db.execute("INSERT OR IGNORE INTO collection_photos VALUES (?, ?)", (collection, identifier))
                        db.execute("INSERT INTO batch_items VALUES (?, ?, ?, ?, NULL)",
                                   (batch_id, str(path), identifier, status))
                        db.commit()
                        report["added" if status == "added" else "duplicates"] += 1
                        report["missing_date"] += metadata["captured_at"] is None
                        report["missing_gps"] += metadata["latitude"] is None
                        if metadata["warnings"]:
                            report["warnings"].append({"path": str(path), "warnings": metadata["warnings"]})
                    except (OSError, NexusError) as exc:
                        db.rollback()
                        report["failures"].append({"path": str(path), "error": str(exc)})
                        db.execute("INSERT INTO batch_items VALUES (?, ?, NULL, 'failed', ?)",
                                   (batch_id, str(path), str(exc)))
                        db.commit()
                    finally:
                        if temporary:
                            temporary.unlink(missing_ok=True)
                    if progress:
                        progress(index, len(files))
                report["moments"] = self._regroup(db)
                report["status"] = "partial" if report["failures"] or any(s.get("error") for s in skipped) else "complete"
                db.execute("UPDATE batches SET finished_at=?, status=?, summary_json=? WHERE id=?",
                           (now(), report["status"], encode(report), batch_id))
                db.commit()
            except BaseException:
                db.rollback()
                db.execute("UPDATE batches SET finished_at=?, status='interrupted', summary_json=? WHERE id=?",
                           (now(), encode(report), batch_id))
                db.commit()
                raise
        return report

    @staticmethod
    def _regroup(db) -> int:
        settings = dict(db.execute("SELECT key, value FROM settings"))
        gap, radius = float(settings["gap_hours"]) * 3600, float(settings["radius_meters"])
        rows = db.execute("""SELECT * FROM photos WHERE captured_at IS NOT NULL AND latitude IS NOT NULL
                             ORDER BY COALESCE(timezone_offset, 'unknown'), captured_at, id""").fetchall()
        groups, current = [], []
        for row in rows:
            if current:
                anchor, previous = current[0], current[-1]
                compatible = row["timezone_offset"] == previous["timezone_offset"]
                elapsed = ((datetime.fromisoformat(row["captured_at"]) - datetime.fromisoformat(previous["captured_at"])).total_seconds()
                           if compatible else math.inf)
                if (not compatible or elapsed > gap or elapsed < 0 or
                    distance_meters(anchor["latitude"], anchor["longitude"], row["latitude"], row["longitude"]) > radius):
                    groups.append(current)
                    current = []
            current.append(row)
        if current:
            groups.append(current)
        db.execute("DELETE FROM moment_photos")
        db.execute("DELETE FROM moments")
        for group in groups:
            first, last = group[0], group[-1]
            # Membership defines an honest derived identity; photo IDs never change.
            identifier = "moment_" + hashlib.sha256("\n".join(p["id"] for p in group).encode()).hexdigest()[:24]
            db.execute("INSERT INTO moments VALUES (?, ?, ?, ?, ?, ?)",
                       (identifier, first["captured_at"], last["captured_at"], first["timezone_offset"], first["latitude"], first["longitude"]))
            db.executemany("INSERT INTO moment_photos VALUES (?, ?, ?)",
                           [(identifier, p["id"], i) for i, p in enumerate(group)])
        return len(groups)

    def regroup(self, gap_hours: float | None = None, radius_meters: float | None = None) -> dict:
        with self.connection() as db:
            for key, value in (("gap_hours", gap_hours), ("radius_meters", radius_meters)):
                if value is not None:
                    if not math.isfinite(value) or value <= 0:
                        raise NexusError(f"{key} must be a positive finite number")
                    db.execute("UPDATE settings SET value=? WHERE key=?", (str(value), key))
            count = self._regroup(db)
            db.commit()
            return {"moments": count, "settings": dict(db.execute("SELECT key, value FROM settings"))}

    def create_collection(self, kind: str, name: str) -> dict:
        with self.connection() as db:
            identifier = self._collection(db, kind, name, create=True)
            db.commit()
            return {"id": identifier, "kind": kind, "name": name}

    def assign(self, kind: str, name: str, prefixes: list[str], remove: bool = False) -> dict:
        with self.connection() as db:
            collection = self._collection(db, kind, name)
            identifiers = list(dict.fromkeys(self._resolve(db, p) for p in prefixes))
            for identifier in identifiers:
                if remove:
                    db.execute("DELETE FROM collection_photos WHERE collection_id=? AND photo_id=?", (collection, identifier))
                else:
                    db.execute("INSERT OR IGNORE INTO collection_photos VALUES (?, ?)", (collection, identifier))
            db.commit()
            return {"kind": kind, "name": name, "photo_ids": identifiers, "action": "removed" if remove else "added"}

    def annotate(self, prefix: str, caption: str) -> dict:
        with self.connection() as db:
            identifier = self._resolve(db, prefix)
            db.execute("UPDATE photos SET caption=? WHERE id=?", (caption or None, identifier))
            db.commit()
            return {"id": identifier, "caption": caption or None}

    @staticmethod
    def _photos(db) -> list[dict]:
        rows = db.execute("SELECT * FROM photos ORDER BY captured_at IS NULL, captured_at, id").fetchall()
        sources, memberships, moments = {}, {}, {}
        for row in db.execute("SELECT * FROM sources ORDER BY source_path"):
            sources.setdefault(row["photo_id"], []).append(row["source_path"])
        for row in db.execute("SELECT * FROM collection_photos ORDER BY collection_id"):
            memberships.setdefault(row["photo_id"], []).append(row["collection_id"])
        for row in db.execute("SELECT photo_id, moment_id FROM moment_photos"):
            moments[row["photo_id"]] = row["moment_id"]
        result = []
        for row in rows:
            photo = dict(row)
            metadata = json.loads(photo.pop("metadata_json"))
            result.append({**photo, **metadata, "source_paths": sources.get(row["id"], []),
                           "collection_ids": memberships.get(row["id"], []), "moment_id": moments.get(row["id"])})
        return result

    @staticmethod
    def _moments(db) -> list[dict]:
        memberships = {}
        for row in db.execute("SELECT * FROM moment_photos ORDER BY position"):
            memberships.setdefault(row["moment_id"], []).append(row["photo_id"])
        return [{**dict(row), "photo_ids": memberships.get(row["id"], [])}
                for row in db.execute("SELECT * FROM moments ORDER BY started_at, id")]

    @staticmethod
    def _collections(db) -> list[dict]:
        memberships = {}
        for row in db.execute("SELECT * FROM collection_photos ORDER BY photo_id"):
            memberships.setdefault(row["collection_id"], []).append(row["photo_id"])
        return [{**dict(row), "photo_ids": memberships.get(row["id"], [])}
                for row in db.execute("SELECT * FROM collections ORDER BY kind, name")]

    def photos(self, prefix: str | None = None, chapter: str | None = None, trip: str | None = None) -> list[dict]:
        with self.connection() as db:
            identifier = self._resolve(db, prefix) if prefix else None
            filters = [self._collection(db, kind, name) for kind, name in (("chapter", chapter), ("trip", trip)) if name is not None]
            return [p for p in self._photos(db) if (identifier is None or p["id"] == identifier)
                    and all(c in p["collection_ids"] for c in filters)]

    def moments(self) -> list[dict]:
        with self.connection() as db:
            return self._moments(db)

    def collections(self, kind: str) -> list[dict]:
        with self.connection() as db:
            return [c for c in self._collections(db) if c["kind"] == kind]

    def batches(self) -> list[dict]:
        with self.connection() as db:
            return [{**dict(row), "summary": json.loads(row["summary_json"]) if row["summary_json"] else None}
                    for row in db.execute("SELECT * FROM batches ORDER BY started_at DESC")]

    def export(self) -> dict:
        with self.connection() as db:
            manifest = {"schema_version": SCHEMA_VERSION, "generated_at": now(),
                        "path_base": "archive_root", "settings": dict(db.execute("SELECT key, value FROM settings")),
                        "photos": self._photos(db), "moments": self._moments(db),
                        "collections": self._collections(db)}
            # Publish both files together as a new immutable export directory.
            output = self.root / "exports"
            output.mkdir(exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".export-", dir=output))
            try:
                (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
                columns = ["id", "original_filename", "archive_path", "captured_at", "timezone_offset", "latitude",
                           "longitude", "width", "height", "camera_make", "camera_model", "caption", "moment_id"]
                with (temporary / "inventory.csv").open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
                    writer.writeheader()
                    for photo in manifest["photos"]:
                        # Keep spreadsheet applications from interpreting captions/filenames as formulas.
                        row = {k: v for k, v in photo.items() if k in columns}
                        for key in ("original_filename", "camera_make", "camera_model", "caption"):
                            value = row.get(key)
                            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
                                row[key] = "'" + value
                        writer.writerow(row)
                destination = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return {"directory": str(destination), "manifest": str(destination / "manifest.json"),
                "inventory": str(destination / "inventory.csv"), "photos": len(manifest["photos"]),
                "moments": len(manifest["moments"])}
