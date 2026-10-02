# Manifest contract — version 1

`nexus export` writes an immutable snapshot for a future local map. The manifest
does not contain photo bytes; the viewer reads originals from the archive. All
timestamps are ISO 8601 strings. A missing value is `null`, never a fabricated date
or coordinate. An offset-free timestamp remains local wall time of unknown timezone.

## Top-level fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`; consumers must reject unsupported versions. |
| `generated_at` | Export time in UTC. |
| `path_base` | `archive_root`; resolve each photo's `archive_path` against the selected archive root. |
| `settings` | Current `gap_hours` and `radius_meters`, stored as numeric strings. |
| `photos` | Photo records, ordered by capture time with undated photos last. Offsets may differ; order is not a guaranteed global UTC chronology. |
| `moments` | Derived location/time groups. |
| `collections` | Manually named chapters and trips. |

## Photo records

| Field | Meaning |
| --- | --- |
| `id` | Full lowercase SHA-256 hash of the original bytes; durable identity. |
| `archive_path` | Relative POSIX path to the managed original. |
| `original_filename` | Filename when first imported. |
| `source_paths` | Absolute paths where those same bytes have been imported; provenance only. |
| `byte_size`, `imported_at` | Original size and first successful import time. |
| `captured_at` | Embedded capture timestamp, including fractional seconds and offset when available. |
| `capture_tag` | Metadata field selected as the capture-time source. |
| `timezone_offset` | Explicit offset as `±HHMM`, or `null` when unknown. |
| `latitude`, `longitude` | Decimal degrees, signed north/east positive; both `null` if unavailable or invalid. Zero is a valid coordinate. |
| `width`, `height` | Stored image dimensions when available; orientation is retained in raw metadata. |
| `camera_make`, `camera_model`, `file_type` | Available camera information and detected file type. |
| `caption` | Optional user-authored caption. |
| `warnings` | ExifTool metadata warnings. |
| `raw_metadata` | Original extracted metadata keyed by ExifTool group and tag; transient extraction paths are omitted. |
| `collection_ids` | Chapter/trip memberships. |
| `moment_id` | Current derived moment, or `null` for an ungrouped photo. |

Capture-time preference is original exposure time (including subseconds and
offset), then other embedded creation timestamps. Filesystem dates are never used.
Original extracted values remain available so future normalization can be revised.

## Moment and collection records

A moment has `id`, `started_at`, `ended_at`, `timezone_offset`, `latitude`,
`longitude`, and ordered `photo_ids`. Its coordinate is the first photo's coordinate,
not a detected venue or computed route. Its ID is a deterministic digest of its
ordered membership and can change after an import or regroup.

A collection has `id`, `kind` (`chapter` or `trip`), `name`, and `photo_ids`.
Collection IDs remain stable. Names are unique within a kind and case-sensitive.
Collections are independently assigned; a photo may be in both a chapter and a trip.

## Consumer expectations

- Derive markers from photos with known coordinates; retain an unlocated-photo view.
- Use collection membership to display trips without inferring paths between them.
- Keep user captions distinct from any future generated descriptions.
- Support unknown capture times/timezones without guessing a chronological position.
- Treat imported captions and metadata as text, not HTML.
- Resolve media relative to the archive root, never via provenance paths.
