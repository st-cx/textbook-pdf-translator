"""Install the pinned official Windows x64 Typst without bundling its binary.

The archive SHA256 is the digest published by the official v0.15.1 release.
Only the executable and upstream license/notice files are extracted. No PDF,
credentials or translation content are read. --archive supports offline setup.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
from urllib.request import Request, urlopen
import uuid
import zipfile

VERSION = "0.15.1"
ARCHIVE = "typst-x86_64-pc-windows-msvc.zip"
SHA256 = "19ce3551153c2fe7ee9fa2f95208310c8f4d3209fedb699e0333faf8913f6736"
URL = f"https://github.com/typst/typst/releases/download/v{VERSION}/{ARCHIVE}"
ROOT = Path(__file__).resolve().parents[1]


def install(destination: Path, archive: Path | None = None) -> Path:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "typst.exe"
    if target.is_file():
        version = subprocess.run([str(target), "--version"], capture_output=True, text=True, check=True)
        if version.stdout.split()[:2] == ["typst", VERSION]:
            print(f"Typst {VERSION} already available: {target}")
            return target
        raise RuntimeError("A different Typst already exists here; use another --destination to preserve it")
    scratch = destination / (".typst-install-" + uuid.uuid4().hex)
    scratch.mkdir()
    try:
        package = archive.resolve() if archive else scratch / ARCHIVE
        if archive is None:
            request = Request(URL, headers={"User-Agent": "textbook-pdf-typst-installer/1"})
            with urlopen(request, timeout=90) as response, package.open("wb") as output:
                shutil.copyfileobj(response, output)
        with package.open("rb") as source:
            checksum = hashlib.file_digest(source, "sha256").hexdigest()
        if checksum != SHA256:
            raise RuntimeError("Official archive SHA256 mismatch; no existing executable changed")
        with zipfile.ZipFile(package) as bundle:
            executable = f"typst-x86_64-pc-windows-msvc/typst.exe"
            names = [executable] + [name for name in bundle.namelist()
                if Path(name).name.upper().startswith(("LICENSE", "NOTICE", "COPYING")) and not name.endswith("/")]
            for name in names:
                with bundle.open(name) as source, (scratch / Path(name).name).open("wb") as output:
                    shutil.copyfileobj(source, output)
        version = subprocess.run([str(scratch / "typst.exe"), "--version"], capture_output=True, text=True, check=True)
        if version.stdout.split()[:2] != ["typst", VERSION]:
            raise RuntimeError("Downloaded executable did not report the pinned version")
        for notice in scratch.iterdir():
            if notice.is_file() and notice.name != "typst.exe" and notice.suffix != ".zip":
                shutil.copy2(notice, destination / notice.name)
        (scratch / "typst.exe").replace(target)
    finally:
        shutil.rmtree(scratch)
    print(f"Installed Typst {VERSION}: {target}")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "tools")
    parser.add_argument("--archive", type=Path, help="Use an already downloaded official zip")
    arguments = parser.parse_args()
    install(arguments.destination, arguments.archive)
