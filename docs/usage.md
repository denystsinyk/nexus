# Using nexus

[Back to the story](../README.md)

## Setup

Requires **Python 3.12+** and **Perl** on Linux, macOS, or Windows through WSL.
There are no third-party Python runtime packages. Metadata extraction uses
[ExifTool](https://exiftool.org/).

From the project directory:

```bash
python3 scripts/setup_exiftool.py
export PATH="$PWD/bin:$PATH"
nexus doctor
```

The setup script installs ExifTool 13.59 under the ignored `.tools/` directory,
using a pinned official source commit and verified SHA-256 checksum. It does not
need administrator access. Alternatively, install ExifTool with your OS package
manager, or set `NEXUS_EXIFTOOL` to its executable. Perl is already included on
many Linux/macOS systems; on Ubuntu/WSL it can be installed with `sudo apt install perl`.

You can always use `./bin/nexus` or `python3 -m nexus` from this directory instead
of changing PATH. Optional Python packaging is available with `pip install -e .`
in a virtual environment; when using that route, provide ExifTool on PATH or via
`NEXUS_EXIFTOOL`.

## Start with your Pittsburgh photos

Use originals exported with their metadata. A local folder downloaded from Drive
works too; nexus does not connect to Drive itself yet. Inspect first:

```bash
nexus inspect "/path/to/pittsburgh-photos"
```

This reads the photos without creating an archive or changing any files. It reports
formats, capture-date coverage, GPS coverage, explicit timezone coverage, and
files that cannot be processed. Photos whose metadata was removed during sharing
will still be usable, but cannot automatically supply missing locations or dates.

Then import:

```bash
nexus import "/path/to/pittsburgh-photos" --chapter "Pittsburgh"
nexus photos --limit 20
nexus moments
```

By default, the archive lives at `~/.local/share/nexus`, outside the code repository.
To use another disk or folder, set `NEXUS_ARCHIVE`, or put `--archive` **before**
the command:

```bash
nexus --archive "/path/to/my-nexus-archive" import "/path/to/photos"
```

In WSL, a Windows folder such as `C:\Users\you\Pictures\Pittsburgh` is usually
available as `/mnt/c/Users/you/Pictures/Pittsburgh`.

The importer copies each supported original into the archive. It never edits or
deletes the source. HEIC/HEIF, JPEG, and PNG are supported, including uppercase
extensions. Videos, Live Photo motion files, and edit sidecars are reported as
unsupported for now. The still image of a Live Photo imports normally.

## Weekly imports and optional labels

Repeat the import with the next folder, or the same growing folder. SHA-256 hashes
identify byte-identical photos even when names or folders change. Different files
with the same filename remain separate photos. Edited or converted versions are
also separate; visual duplicate detection is not part of this milestone.

Name an entire batch as a trip:

```bash
nexus import "/path/to/weekend-photos" --chapter "Pittsburgh" --trip "Weekend in Philadelphia"
```

Or organize individual photos afterward. `photos` prints short photo IDs; replace
`PHOTO_ID` below with a full ID or a unique prefix from that output:

```bash
nexus trip create "Weekend in Philadelphia"
nexus trip add "Weekend in Philadelphia" PHOTO_ID ANOTHER_PHOTO_ID
nexus trip remove "Weekend in Philadelphia" PHOTO_ID
nexus trip list
nexus photos --trip "Weekend in Philadelphia" --limit 0
nexus annotate PHOTO_ID --caption "Our usual coffee spot"
nexus show PHOTO_ID
```

`chapter` supports the same `create`, `add`, `remove`, and `list` commands as `trip`.
Photos can belong to multiple collections. Passing `--caption ""` clears a caption.
Trip/chapter membership and captions belong to stable photo IDs and survive
re-imports and moment regrouping. Labels are completely optional.

## How moments work

Moments are suggestions derived from the photos you actually captured:

- Order photos by capture time within the same timezone-offset context.
- Keep consecutive photos together when the time gap is at most **3 hours** and
  their locations are within **250 meters of the moment's first photo**.
- A larger gap, a more distant photo, or a different timezone offset starts a new
  moment. The anchor prevents a chain of nearby photos from drifting across a city.
- A single photo can form a moment. Photos without a usable date or GPS remain in
  the archive and can be assigned to trips/chapters manually.
- Unknown timezone offsets stay unknown; they group only with other unknown offsets.
  No timezone is guessed from the computer, GPS, or upload date. This conservative
  rule can split an outing when metadata or offsets differ.

Change the thresholds and regroup at any time:

```bash
nexus regroup --gap-hours 2 --radius-meters 150
```

These settings persist for later imports and apply to the whole archive. Moments
are rebuilt after each import; their IDs describe their current membership and
can change when new photos arrive. Use **photo IDs** for durable annotations.
Moment start/end values are first/last photo timestamps, not measured dwell time.
No routes, distance traveled, exact businesses, or exploration percentages are inferred.

## Export and archive layout

```bash
nexus export
```

Each export writes a new directory containing:

- `manifest.json`: a versioned snapshot of photos, normalized and raw metadata,
  captions, moments, chapters, and trips.
- `inventory.csv`: a spreadsheet-friendly photo inventory. Potential formula
  prefixes in text cells are escaped; JSON preserves the exact text.

The command prints the output paths. Both files are published together into a new
export directory. Archive file paths inside the manifest are relative to the archive
root; original source paths are retained separately as provenance.

```text
~/.local/share/nexus/
  archive.sqlite3
  originals/<hash-prefix>/<sha256>.<extension>
  exports/<timestamp-id>/
    manifest.json
    inventory.csv
```

See [the manifest contract](manifest.md) for the future map's input format.

## Recovery and error handling

Every import records a batch. Individual file failures do not stop other photos
from importing. Review the full report with `nexus batches`. `inspect` and `import`
return exit code 1 for file or directory-read failures; unsupported files are skipped.
Metadata warnings are reported but do not automatically reject an otherwise
readable file. Reading metadata is not a full image-decoding integrity check.

If interrupted, rerun the same command. Previously committed photos remain in the
archive, incomplete staging copies are cleaned up, and moments are regenerated
after the next completed import. Re-importing can also restore missing or damaged
archive originals from matching source files. Only one nexus command accesses an
archive at a time; a second command gets a clear message rather than competing.

Back up the **entire archive directory** while nexus is idle. Its originals and
database are the source of truth; exported manifests are snapshots. No automatic
cloud backup or sync is included.

## Public code, local memories

The GitHub repository contains code, documentation, and synthetic test generators.
The default archive is outside the repository. `.gitignore` additionally excludes
common photo formats, archive folders, databases, generated exports, local config,
and logs. Your originals, coordinates, captions, and exports stay local. Inspect
an export before choosing to share it: it contains personal metadata and source paths.

## Development

```bash
nexus doctor
python3 -m unittest discover -v
```

Tests create synthetic PNG/JPEG images and a minimal HEIF metadata container in
temporary directories, then exercise the real ExifTool reader. No personal photos
or downloaded photographs are committed. Integration tests skip when ExifTool is
missing, so run `doctor` first. CI installs the pinned dependency and checks it
before running the full suite.

The next real-data check is to inspect a representative Pittsburgh folder, review
several proposed moments, and repeat the import to confirm zero added duplicates.
