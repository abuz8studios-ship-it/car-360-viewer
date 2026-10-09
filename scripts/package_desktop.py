"""Native one-file installers built from an already frozen desktop package."""
from __future__ import annotations

import gzip
import hashlib
import os
import tarfile
from pathlib import Path

LINUX_RUNFILE_HEADER = b'''#!/bin/sh
# LotVue360 one-file Linux installer. The compressed package follows the marker.
set -eu

SELF=$0
case "$SELF" in
    /*) ;;
    *) SELF="$(pwd)/$SELF" ;;
esac
PAYLOAD_LINE=$(awk '$0 == "__LOTVUE360_ARCHIVE_BELOW__" { print NR + 1; exit }' "$SELF")
if [ -z "$PAYLOAD_LINE" ]; then
    echo "LotVue360 installer payload marker is missing." >&2
    exit 1
fi
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/lotvue360-installer.XXXXXX")
cleanup() {
    rm -rf -- "$TMP_ROOT"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if ! tail -n +"$PAYLOAD_LINE" "$SELF" | tar -xzf - -C "$TMP_ROOT"; then
    echo "Could not extract the LotVue360 installer payload." >&2
    exit 1
fi
"$TMP_ROOT/LotVue360/Install-LotVue360.sh" "$@"
exit 0
'''
LINUX_RUNFILE_MARKER = b"\n__LOTVUE360_ARCHIVE_BELOW__\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256_sidecar(path: Path) -> Path:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar.write_text(f"{sha256_file(path)}  {path.name}\n", encoding="ascii")
    return sidecar


def create_linux_one_click_installer(package_root: Path, output_path: Path) -> Path:
    """Create a self-extracting Linux installer; no prior ZIP extraction is needed."""
    package_root = package_root.resolve()
    output_path = output_path.resolve()
    if package_root.name != "LotVue360" or not package_root.is_dir():
        raise ValueError("Linux installer input must be the built LotVue360 package directory")
    required = {
        "LotVue360": package_root / "LotVue360",
        "Install-LotVue360.sh": package_root / "Install-LotVue360.sh",
        "app/main.py": package_root / "app" / "main.py",
        "app/.env.example": package_root / "app" / ".env.example",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise ValueError(f"Linux installer package is incomplete: {', '.join(missing)}")
    for name in ("LotVue360", "Install-LotVue360.sh"):
        if not os.access(required[name], os.X_OK):
            raise ValueError(f"Linux installer payload must be executable: {name}")
    for path in package_root.rglob("*"):
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(package_root):
                raise ValueError(f"Linux installer package contains an escaping symlink: {path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as stream:
        stream.write(LINUX_RUNFILE_HEADER)
        stream.write(LINUX_RUNFILE_MARKER)
        with gzip.GzipFile(filename="", fileobj=stream, mode="wb", compresslevel=6, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                archive.add(package_root, arcname="LotVue360", recursive=True)
    output_path.chmod(output_path.stat().st_mode | 0o111)
    write_sha256_sidecar(output_path)
    return output_path
