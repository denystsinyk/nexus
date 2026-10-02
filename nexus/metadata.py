"""Read-only metadata extraction. No inferred dates, places, or timezones."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime

SUPPORTED_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png"}
SUPPORTED_TYPES = {"HEIC", "HEIF", "JPEG", "PNG"}


class NexusError(Exception):
    """An actionable error safe to display without a traceback."""


def find_exiftool(override: str | None = None) -> str:
    candidate = override or os.environ.get("NEXUS_EXIFTOOL")
    if candidate:
        path = shutil.which(candidate) or str(Path(candidate).expanduser().resolve())
    else:
        local = Path(__file__).resolve().parents[1] / ".tools" / "exiftool" / "exiftool"
        path = shutil.which("exiftool") or (str(local) if local.is_file() else "")
    if not path or not os.access(path, os.X_OK):
        raise NexusError("ExifTool is missing. Run python3 scripts/setup_exiftool.py, "
                         "install exiftool on PATH, or set NEXUS_EXIFTOOL.")
    return path


class ExifTool:
    def __init__(self, executable: str | None = None):
        self.executable = find_exiftool(executable)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.executable, "-config", "", *args],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NexusError(f"ExifTool could not complete: {exc}") from exc

    def version(self) -> str:
        result = self._run(["-ver"])
        if result.returncode:
            raise NexusError(result.stderr.strip() or "ExifTool version check failed")
        return result.stdout.strip()

    def extract(self, path: Path) -> dict:
        result = self._run(["-j", "-G1", "-n", "-struct", str(path.resolve())])
        try:
            values = json.loads(result.stdout)
            raw = values[0]
            if not isinstance(raw, dict):
                raise ValueError("expected an object")
        except (ValueError, IndexError, TypeError) as exc:
            raise NexusError("ExifTool did not return readable metadata") from exc
        errors = [str(v) for k, v in raw.items() if k.split(":")[-1] == "Error"]
        if errors or result.returncode:
            raise NexusError("; ".join(errors) or result.stderr.strip() or "Unreadable image")
        if raw.get("File:FileType") not in SUPPORTED_TYPES:
            raise NexusError(f"Unsupported or damaged image: {raw.get('File:FileType', 'unknown type')}")
        # Extraction paths refer to transient staging files, not photo metadata.
        for key in ("SourceFile", "File:Directory", "File:FileName"):
            raw.pop(key, None)
        return normalize(raw)


def parse_capture(value: object, offset: object = None) -> datetime | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", value.strip())
    if not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None and isinstance(offset, str) and re.fullmatch(r"[+-]\d{2}:\d{2}", offset):
            parsed = datetime.fromisoformat(value + offset)
        return parsed
    except ValueError:
        return None


def normalize(raw: dict) -> dict:
    captured = None
    capture_tag = None
    # Prefer original exposure time, including subsecond and timezone information.
    for key in ("Composite:SubSecDateTimeOriginal", "ExifIFD:DateTimeOriginal",
                "XMP-exif:DateTimeOriginal", "QuickTime:CreationDate",
                "XMP-xmp:CreateDate", "QuickTime:CreateDate", "PNG:CreationTime"):
        captured = parse_capture(raw.get(key), raw.get("ExifIFD:OffsetTimeOriginal")
                                 if "DateTimeOriginal" in key else None)
        if captured is not None:
            capture_tag = key
            break

    def coordinate(kind: str, maximum: float) -> float | None:
        value = raw.get(f"Composite:GPS{kind}", raw.get(f"GPS:GPS{kind}"))
        if value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
            ref = raw.get(f"GPS:GPS{kind}Ref", "")
            if ref in ("S", "W"):
                value = -abs(value)
            if math.isfinite(value) and abs(value) <= maximum:
                return value
        except (ValueError, TypeError):
            pass
        return None

    latitude, longitude = coordinate("Latitude", 90), coordinate("Longitude", 180)
    if latitude is None or longitude is None:
        latitude = longitude = None

    def first(*keys):
        return next((raw[k] for k in keys if raw.get(k) is not None), None)

    warnings = [str(v) for k, v in raw.items() if k.split(":")[-1] == "Warning"]
    return {
        "captured_at": captured.isoformat() if captured else None,
        "capture_tag": capture_tag,
        "timezone_offset": captured.strftime("%z") if captured and captured.tzinfo else None,
        "latitude": latitude, "longitude": longitude,
        "width": first("ExifIFD:ExifImageWidth", "File:ImageWidth", "IFD0:ImageWidth", "PNG:ImageWidth", "QuickTime:ImageWidth"),
        "height": first("ExifIFD:ExifImageHeight", "File:ImageHeight", "IFD0:ImageHeight", "PNG:ImageHeight", "QuickTime:ImageHeight"),
        "camera_make": first("IFD0:Make", "QuickTime:Make"),
        "camera_model": first("IFD0:Model", "QuickTime:Model"),
        "file_type": raw.get("File:FileType"),
        "warnings": warnings, "raw_metadata": raw,
    }
