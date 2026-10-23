import hashlib
import json
import os
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote
from zlib import error as zlib_error

ROOT = "boot-recovery"
CATALOG_PATH = "catalogs/boot-recovery.json"
SCHEMA = 1
CHUNK = 1 << 20

# Download base for every file. The app accepts only this prefix (see ImagesCatalog.parse).
RAW_BASE = "https://raw.githubusercontent.com/itsnoly/RealmeNote50-Resources/refs/heads/main/"

EXTENSION = re.compile(r"\.(zip|img|bin|7z|tar|gz|xz|lz4)$", re.I)
KINDS = [
    ("boot", re.compile(r"^boot($|[_\-.])")),
    ("vendor_boot", re.compile(r"^vendor[_\-]boot($|[_\-.])")),
]
KIND_ORDER = [kind for kind, _ in KINDS]
ANDROID = re.compile(r"^android[\s_\-]*(\d+)", re.I)
REGION = re.compile(r"^(export|gdpr)$", re.I)
NAME_NOTE = re.compile(r"^(.*?)\s*\((.+)\)\s*$")


class CatalogError(Exception):
    """A problem that must stop the build, so a broken catalog is never written."""


def kind_of(name):
    lowered = name.lower()
    for kind, pattern in KINDS:
        if pattern.search(lowered):
            return kind
    return None


def head_of(folder):
    """Android level, region, version and note, taken from the folder names."""
    match = ANDROID.search(folder[0])
    android = int(match.group(1)) if match else 0
    region = next((part.lower() for part in folder if REGION.match(part)), "")
    raw = folder[-1]
    named = NAME_NOTE.match(raw)
    ver, note = (named.group(1), named.group(2)) if named else (raw, "")
    return android, region, ver, note


def download_url(path):
    """Full download address. Each path segment is percent-encoded (spaces and parentheses)."""
    url = RAW_BASE + "/".join(quote(segment, safe="") for segment in path.split("/"))
    if not url.startswith(RAW_BASE):
        raise CatalogError(f"Download address outside the allowed base: {url}")
    return url


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def images_in_zip(path):
    """
    Hashes the boot and vendor_boot images inside a ZIP, in one pass.

    Every entry is inflated once. zipfile checks the CRC-32 when an entry is read to its end, so reading
    all entries also proves the archive is intact (no separate testzip pass). Only image entries are hashed.
    Returns {kind: {"kind", "name", "size", "sha256"}}.
    """
    images = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename.rsplit("/", 1)[-1]
                kind = kind_of(name) if EXTENSION.search(name) else None
                digest = hashlib.sha256() if kind else None
                size = 0
                with archive.open(info) as member:
                    for chunk in iter(lambda: member.read(CHUNK), b""):
                        size += len(chunk)
                        if digest:
                            digest.update(chunk)
                if kind is None:
                    continue
                if kind in images:
                    raise CatalogError(f"{path}: two {kind} images ({images[kind]['name']} and {name}).")
                images[kind] = {"kind": kind, "name": name, "size": size, "sha256": digest.hexdigest()}
    except (zipfile.BadZipFile, zlib_error, EOFError, NotImplementedError) as e:
        raise CatalogError(f"Corrupt or unsupported ZIP {path}: {e}. Catalog not written.")
    return images


def describe(path):
    """One catalog file for [path]: the download hash (of the file itself) plus the hashes of its images."""
    name = os.path.basename(path)
    name_kind = kind_of(name)
    file_sha = sha256_of(path)

    if name.lower().endswith(".zip"):
        images = images_in_zip(path)
    elif name_kind:
        images = {name_kind: {"kind": name_kind, "name": name, "size": os.path.getsize(path), "sha256": file_sha}}
    else:
        images = {}

    kind = name_kind or next((k for k in KIND_ORDER if k in images), None)
    if kind is None:
        return None
    if kind not in images:
        # The app needs at least one image of the file's kind inside the ZIP to accept the download.
        raise CatalogError(f"{path}: the name says {kind}, but the ZIP has no {kind} image.")

    return {
        "kind": kind,
        "name": name,
        "path": path,
        "url": download_url(path),
        "size": os.path.getsize(path),
        "sha256": file_sha,
        "images": [images[k] for k in KIND_ORDER if k in images],
    }


def list_files():
    for dirpath, _, names in os.walk(ROOT):
        for name in names:
            yield os.path.join(dirpath, name).replace(os.sep, "/")


def build_entries():
    candidates = []
    for path in sorted(list_files()):
        parts = path.split("/")[1:]
        if len(parts) < 2 or not EXTENSION.search(parts[-1]):
            continue
        candidates.append((path, "/".join(parts[:-1]), parts[:-1]))

    # Hashing is I/O and zlib/hashlib work that release the GIL, so a few threads speed up large repositories.
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
        described = list(pool.map(lambda c: describe(c[0]), candidates))

    builds = {}
    for (path, key, folder), entry in zip(candidates, described):
        if entry is None:
            continue
        if key not in builds:
            android, region, ver, note = head_of(folder)
            builds[key] = {
                "id": key, "android": android, "region": region,
                "ver": ver, "note": note, "files": [],
            }
        builds[key]["files"].append(entry)

    for key, build in builds.items():
        kinds = [f["kind"] for f in build["files"]]
        if len(kinds) != len(set(kinds)):
            names = ", ".join(f["name"] for f in build["files"])
            raise CatalogError(f"Build '{key}' has two files of the same kind ({names}). The app rejects that.")
    return [builds[k] for k in sorted(builds)]


def load_previous():
    if not os.path.isfile(CATALOG_PATH):
        return None
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def main():
    try:
        builds = build_entries()
    except CatalogError as e:
        sys.exit(str(e))
    if not builds:
        sys.exit(f"No builds found in {ROOT}/.")

    previous = load_previous()
    if previous and previous.get("builds") == builds:
        print("No changes in the catalog.")
        return

    catalog = {
        "schema": SCHEMA,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "builds": builds,
    }
    os.makedirs(os.path.dirname(CATALOG_PATH), exist_ok=True)
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
        f.write("\n")
    total = sum(len(b["files"]) for b in builds)
    images = sum(len(f["images"]) for b in builds for f in b["files"])
    print(f"{len(builds)} builds, {total} files, {images} images.")


if __name__ == "__main__":
    main()
