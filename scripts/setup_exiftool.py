#!/usr/bin/env python3
"""Install a pinned official ExifTool locally; no root or Python packages needed."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

COMMIT = "2200871d9cef988051d2a99d67df3bda6cbb30a8"
VERSION = "13.59"
SHA256 = "e1e2ad6c6fbf568afee5993ef8b2b91ab013d21698c9304e079e633ad82776f5"
URL = f"https://codeload.github.com/exiftool/exiftool/tar.gz/{COMMIT}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarball", type=Path, help="use an already downloaded official tarball; checksum is still verified")
    args = parser.parse_args()
    if not shutil.which("perl"):
        parser.exit(1, "Perl is required. Install it with your operating system's package manager first.\n")
    tools = Path(__file__).resolve().parents[1] / ".tools"
    tools.mkdir(exist_ok=True)
    destination = tools / "exiftool"
    if destination.exists():
        print(f"ExifTool already exists at {destination}; run bin/nexus doctor to check it.")
        return
    with tempfile.TemporaryDirectory(prefix="setup-", dir=tools) as temp:
        temp = Path(temp)
        tarball = temp / "exiftool.tar.gz"
        if args.tarball:
            shutil.copyfile(args.tarball, tarball)
        else:
            with urlopen(URL, timeout=60) as response, tarball.open("wb") as output:
                shutil.copyfileobj(response, output)
        with tarball.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
                parser.exit(1, "ExifTool download checksum did not match; nothing was installed.\n")
        with tarfile.open(tarball) as bundle:
            bundle.extractall(temp, filter="data")
        source = temp / f"exiftool-{COMMIT}"
        executable = source / "exiftool"
        executable.chmod(0o755)
        result = subprocess.run([str(executable), "-config", "", "-ver"], capture_output=True, text=True, check=True)
        if result.stdout.strip() != VERSION:
            parser.exit(1, "Unexpected ExifTool version; nothing was installed.\n")
        source.rename(destination)
    print(f"Installed ExifTool {VERSION} at {destination}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"ExifTool setup failed: {exc}")
