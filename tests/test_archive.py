from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr

from nexus.archive import Archive, distance_meters, inspect, scan
from nexus.cli import main
from nexus.metadata import ExifTool, NexusError, normalize
from tests.fixtures import png, jpeg, heic


class MetadataTests(unittest.TestCase):
    def test_original_datetime_offset_and_signed_gps(self):
        result = normalize({"ExifIFD:DateTimeOriginal": "2025:03:03 15:15:00",
                            "ExifIFD:OffsetTimeOriginal": "-05:00", "GPS:GPSLatitude": 40.44,
                            "GPS:GPSLatitudeRef": "N", "GPS:GPSLongitude": 79.99, "GPS:GPSLongitudeRef": "W"})
        self.assertEqual(result["captured_at"], "2025-03-03T15:15:00-05:00")
        self.assertEqual(result["longitude"], -79.99)
        self.assertEqual(result["timezone_offset"], "-0500")

    def test_missing_metadata_never_uses_filesystem_dates(self):
        result = normalize({"System:FileModifyDate": "2025:03:03 15:15:00-05:00"})
        self.assertIsNone(result["captured_at"])
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["timezone_offset"])

    def test_invalid_dates_gps_and_unknown_timezone(self):
        result = normalize({"ExifIFD:DateTimeOriginal": "0000:00:00 00:00:00",
                            "GPS:GPSLatitude": "nan", "GPS:GPSLongitude": 10})
        self.assertIsNone(result["captured_at"])
        self.assertIsNone(result["latitude"])
        result = normalize({"ExifIFD:DateTimeOriginal": "2025:03:03 15:15:00"})
        self.assertIsNone(result["timezone_offset"])
        self.assertEqual(result["captured_at"], "2025-03-03T15:15:00")

    def test_equator_prime_meridian_and_subseconds(self):
        result = normalize({"Composite:SubSecDateTimeOriginal": "2025:03:03 15:15:00.123+00:00",
                            "Composite:GPSLatitude": 0, "Composite:GPSLongitude": 0})
        self.assertEqual(result["latitude"], 0)
        self.assertEqual(result["longitude"], 0)
        self.assertEqual(result["captured_at"], "2025-03-03T15:15:00.123000+00:00")


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.extractor = ExifTool()
            cls.extractor.version()
        except NexusError as exc:
            raise unittest.SkipTest(str(exc))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nexus-test-")
        self.base = Path(self.temp.name)
        self.source = self.base / "input"
        self.source.mkdir()
        self.archive = Archive(self.base / "archive")
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def photo(self, name="photo.png", date="2025:03:03 12:00:00", lat=40.44, lon=-79.99, offset="-05:00", source=None):
        self.counter += 1
        path = (source or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        png(path, self.counter)
        args = [self.extractor.executable, "-config", "", "-overwrite_original", "-Make=Synthetic", "-Model=Test camera"]
        if date:
            args.append(f"-DateTimeOriginal={date}")
        if offset and date:
            args.append(f"-OffsetTimeOriginal={offset}")
        if lat is not None and lon is not None:
            args += [f"-GPSLatitude={abs(lat)}", f"-GPSLongitude={abs(lon)}",
                     f"-GPSLatitudeRef={'S' if lat < 0 else 'N'}", f"-GPSLongitudeRef={'W' if lon < 0 else 'E'}"]
        subprocess.run([*args, str(path)], check=True, capture_output=True)
        return path

    def test_inspect_does_not_create_archive_or_change_original(self):
        path = self.photo()
        before = path.read_bytes()
        result = inspect(self.source, self.extractor, self.archive.root)
        self.assertEqual(result["with_gps"], 1)
        self.assertEqual(result["with_date"], 1)
        self.assertEqual(result["with_timezone"], 1)
        self.assertFalse(self.archive.root.exists())
        self.assertEqual(path.read_bytes(), before)

    def test_repeat_import_and_source_deletion(self):
        path = self.photo()
        before = path.read_bytes()
        first = self.archive.import_folder(self.source, self.extractor, chapter="Pittsburgh")
        second = self.archive.import_folder(self.source, self.extractor, chapter="Pittsburgh")
        self.assertEqual((first["added"], second["added"], second["duplicates"]), (1, 0, 1))
        photo = self.archive.photos()[0]
        self.assertEqual(photo["id"], hashlib.sha256(before).hexdigest())
        self.assertEqual(path.read_bytes(), before)
        path.unlink()
        self.assertEqual((self.archive.root / photo["archive_path"]).read_bytes(), before)
        self.assertEqual(len(self.archive.collections("chapter")[0]["photo_ids"]), 1)

    def test_same_filename_different_contents_and_same_contents_different_name(self):
        a = self.photo("a/IMG_0001.png")
        self.photo("b/IMG_0001.png")
        (self.source / "duplicate.PNG").write_bytes(a.read_bytes())
        report = self.archive.import_folder(self.source, self.extractor)
        self.assertEqual((report["added"], report["duplicates"]), (2, 1))
        self.assertEqual(sum(len(p["source_paths"]) for p in self.archive.photos()), 3)

    def test_bad_file_does_not_stop_batch_missing_data_kept(self):
        self.photo()
        self.photo("unknown.png", date=None, lat=None, lon=None)
        (self.source / "broken.jpg").write_bytes(b"broken image")
        (self.source / "video.mov").write_bytes(b"placeholder")
        report = self.archive.import_folder(self.source, self.extractor)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["added"], 2)
        self.assertEqual(len(report["failures"]), 1)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual((report["missing_date"], report["missing_gps"]), (1, 1))
        unknown = next(p for p in self.archive.photos() if p["captured_at"] is None)
        self.assertIsNone(unknown["moment_id"])

    def test_grouping_time_radius_and_timezone(self):
        self.photo("a.png", date="2025:03:03 10:00:00")
        self.photo("b.png", date="2025:03:03 13:00:00", lat=40.4401)
        self.photo("c.png", date="2025:03:03 16:00:01")
        self.photo("d.png", date="2025:03:03 16:01:00", lat=41.0)
        self.photo("e.png", date="2025:03:03 10:01:00", offset=None)
        self.photo("f.png", date="2025:03:03 10:02:00", offset="+01:00")
        self.archive.import_folder(self.source, self.extractor)
        moments = self.archive.moments()
        self.assertEqual(sorted(len(m["photo_ids"]) for m in moments), [1, 1, 1, 1, 2])
        for m in moments:
            offsets = {self.archive.photos(prefix=p)[0]["timezone_offset"] for p in m["photo_ids"]}
            self.assertEqual(len(offsets), 1)

    def test_spatial_chain_does_not_drift_from_anchor(self):
        for i in range(3):
            self.photo(f"{i}.png", date=f"2025:03:03 12:0{i}:00", lat=40.44 + i * 0.0018)
        self.archive.import_folder(self.source, self.extractor)
        self.assertEqual(sorted(len(m["photo_ids"]) for m in self.archive.moments()), [1, 2])

    def test_radius_boundary_and_dateline(self):
        self.photo("a.png")
        self.photo("b.png", date="2025:03:03 12:01:00", lat=40.442)
        self.archive.import_folder(self.source, self.extractor)
        a, b = self.archive.photos()
        distance = distance_meters(a["latitude"], a["longitude"], b["latitude"], b["longitude"])
        self.assertEqual(self.archive.regroup(radius_meters=distance)["moments"], 1)
        self.assertEqual(self.archive.regroup(radius_meters=distance - 0.001)["moments"], 2)
        self.assertLess(distance_meters(0, 179.999, 0, -179.999), 250)

    def test_annotations_collections_and_exports_survive_regroup(self):
        self.photo("a.png")
        self.photo("b.png", date="2025:03:03 12:10:00", lat=40.441)
        self.archive.import_folder(self.source, self.extractor, chapter="Pittsburgh")
        photo = self.archive.photos()[0]
        self.archive.annotate(photo["id"][:12], "=Our café ☕")
        self.archive.create_collection("trip", "Weekend")
        self.archive.assign("trip", "Weekend", [photo["id"][:12]])
        self.archive.regroup(radius_meters=10)
        self.archive.import_folder(self.source, self.extractor)
        self.assertEqual(self.archive.photos(prefix=photo["id"])[0]["caption"], "=Our café ☕")
        self.assertEqual(len(self.archive.photos(trip="Weekend")), 1)
        output = self.archive.export()
        manifest = json.loads(Path(output["manifest"]).read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["settings"]["radius_meters"], "10")
        for item in manifest["photos"]:
            self.assertFalse(Path(item["archive_path"]).is_absolute())
            self.assertTrue((self.archive.root / item["archive_path"]).exists())
        with Path(output["inventory"]).open(newline="") as stream:
            inventory = list(csv.DictReader(stream))
        self.assertEqual(inventory[0]["caption"], "'=Our café ☕")
        self.assertEqual(manifest["photos"][0]["caption"], "=Our café ☕")
        self.assertNotEqual(self.archive.export()["directory"], output["directory"])

    def test_interrupt_and_resume_committed_photos(self):
        self.photo("a.png")
        self.photo("b.png")
        def interrupt(index, total):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.archive.import_folder(self.source, self.extractor, progress=interrupt)
        self.assertEqual(len(self.archive.photos()), 1)
        self.assertEqual(self.archive.batches()[0]["status"], "interrupted")
        report = self.archive.import_folder(self.source, self.extractor)
        self.assertEqual((report["added"], report["duplicates"]), (1, 1))
        self.assertEqual(len(self.archive.photos()), 2)
        self.assertFalse(list((self.archive.root / ".staging").iterdir()))

    def test_interrupted_staging_and_running_batch_are_recovered(self):
        self.photo()
        self.archive.import_folder(self.source, self.extractor)
        with self.archive.connection() as db:
            db.execute("INSERT INTO batches VALUES ('interrupted', 'test', '2025', NULL, 'running', NULL)")
            db.commit()
        (self.archive.root / ".staging" / "copy-orphan.png").write_bytes(b"partial")
        self.archive.import_folder(self.source, self.extractor)
        self.assertFalse((self.archive.root / ".staging" / "copy-orphan.png").exists())
        self.assertEqual(next(b for b in self.archive.batches() if b["id"] == "interrupted")["status"], "interrupted")

    def test_missing_or_corrupted_archive_copy_repaired_on_reimport(self):
        original = self.photo()
        self.archive.import_folder(self.source, self.extractor)
        photo = self.archive.photos()[0]
        stored = self.archive.root / photo["archive_path"]
        for broken in (None, b"damaged"):
            if broken is None:
                stored.unlink()
            else:
                stored.write_bytes(broken)
            self.archive.import_folder(self.source, self.extractor)
            self.assertEqual(stored.read_bytes(), original.read_bytes())

    def test_unknown_ids_assignment_atomic_and_remove(self):
        self.photo()
        self.archive.import_folder(self.source, self.extractor)
        photo = self.archive.photos()[0]
        self.archive.create_collection("trip", "Test")
        with self.assertRaises(NexusError):
            self.archive.assign("trip", "Test", [photo["id"], "missing"])
        self.assertEqual(self.archive.collections("trip")[0]["photo_ids"], [])
        self.archive.assign("trip", "Test", [photo["id"]])
        self.archive.assign("trip", "Test", [photo["id"]], remove=True)
        self.assertEqual(self.archive.photos(trip="Test"), [])

    def test_invalid_settings_rollback_and_unknown_schema_rejected(self):
        self.photo()
        self.archive.import_folder(self.source, self.extractor)
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(NexusError):
                self.archive.regroup(gap_hours=invalid)
        self.assertEqual(self.archive.regroup()["settings"]["gap_hours"], "3.0")
        with self.archive.connection() as db:
            db.execute("PRAGMA user_version = 999")
        with self.assertRaises(NexusError):
            self.archive.photos()

    def test_no_recursive_archive_import_or_symlinks(self):
        self.photo()
        archive = Archive(self.source / "managed")
        archive.import_folder(self.source, self.extractor)
        (self.source / "link.png").symlink_to(self.source / "photo.png")
        files, skipped = scan(self.source, archive.root)
        self.assertEqual(len(files), 1)
        self.assertEqual(skipped[0]["reason"], "symlink")
        with self.assertRaises(NexusError):
            archive.import_folder(archive.root / "originals", self.extractor)

    def test_archive_lock_blocks_concurrent_commands(self):
        with self.archive.connection():
            with self.assertRaisesRegex(NexusError, "Another nexus command"):
                self.archive.photos()

    def test_jpeg_and_heic_metadata_extraction(self):
        jpg = jpeg(self.source / "synthetic.jpg")
        subprocess.run([self.extractor.executable, "-config", "", "-overwrite_original",
                        "-DateTimeOriginal=2025:03:03 12:00:00", "-GPSLatitude=40.44", "-GPSLatitudeRef=N",
                        "-GPSLongitude=79.99", "-GPSLongitudeRef=W", str(jpg)], check=True, capture_output=True)
        tiff = subprocess.run([self.extractor.executable, "-config", "", "-EXIF", "-b", str(jpg)],
                              check=True, capture_output=True).stdout
        heic(self.source / "synthetic.heic", tiff)
        report = self.archive.import_folder(self.source, self.extractor)
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["added"], 2)
        for photo in self.archive.photos():
            self.assertEqual(photo["captured_at"], "2025-03-03T12:00:00")
            self.assertAlmostEqual(photo["longitude"], -79.99)

    def test_cli_import_list_annotate_export_and_exit_codes(self):
        self.photo()
        def run(*args):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--archive", str(self.archive.root), *args])
            return code, stdout.getvalue(), stderr.getvalue()
        code, _, _ = run("inspect", str(self.source), "--json")
        self.assertEqual(code, 0)
        self.assertFalse(self.archive.root.exists())
        code, text, _ = run("import", str(self.source), "--chapter", "Pittsburgh", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["added"], 1)
        code, text, _ = run("photos", "--limit", "0", "--json")
        identifier = json.loads(text)[0]["id"]
        self.assertEqual(run("annotate", identifier, "--caption", "A memory")[0], 0)
        self.assertEqual(run("export")[0], 0)
        (self.source / "broken.jpg").write_bytes(b"not a photo")
        self.assertEqual(run("import", str(self.source), "--json")[0], 1)
        self.assertEqual(run("photos", "--limit", "-1")[0], 1)


if __name__ == "__main__":
    unittest.main()
