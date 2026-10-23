import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote

ROOT = "boot-recovery"
CATALOG_PATH = "catalogs/boot-recovery.json"
SCHEMA = 1

# Download base for every file. The app accepts only this prefix (see BootCatalog.parse).
RAW_BASE = "https://raw.githubusercontent.com/itsnoly/RealmeNote50-Resources/refs/heads/main/"

EXTENSION = re.compile(r"\.(zip|img|bin|7z|tar|gz|xz|lz4)$", re.I)
KINDS = [
    ("boot", re.compile(r"^boot($|[_\-.])")),
    ("vendor_boot", re.compile(r"^vendor[_\-]boot($|[_\-.])")),
]
ANDROID = re.compile(r"^android[\s_\-]*(\d+)", re.I)
REGION = re.compile(r"^(export|gdpr)$", re.I)
NAME_NOTE = re.compile(r"^(.*?)\s*\((.+)\)\s*$")


def kind_of(name):
    for kind, pattern in KINDS:
        if pattern.search(name):
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
        sys.exit(f"Download address outside the allowed base: {url}")
    return url


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_zip(path):
    """Reads every entry and checks its CRC-32. Stops the build if any entry is damaged."""
    with zipfile.ZipFile(path) as archive:
        bad_entry = archive.testzip()
    if bad_entry is not None:
        sys.exit(f"Corrupt entry '{bad_entry}' in {path}. Catalog not written.")


def list_files():
    for dirpath, _, names in os.walk(ROOT):
        for name in names:
            yield os.path.join(dirpath, name).replace(os.sep, "/")


def build_entries():
    builds = {}
    for path in sorted(list_files()):
        parts = path.split("/")[1:]
        if len(parts) < 2:
            continue
        *folder, name = parts
        kind = kind_of(name)
        if kind is None or not EXTENSION.search(name):
            continue
        if name.lower().endswith(".zip"):
            check_zip(path)

        key = "/".join(folder)
        if key not in builds:
            android, region, ver, note = head_of(folder)
            builds[key] = {
                "id": key, "android": android, "region": region,
                "ver": ver, "note": note, "files": [],
            }
        builds[key]["files"].append({
            "kind": kind,
            "name": name,
            "path": path,
            "url": download_url(path),
            "size": os.path.getsize(path),
            "sha256": sha256_of(path),
        })
    return [builds[k] for k in sorted(builds)]


def load_previous():
    if not os.path.isfile(CATALOG_PATH):
        return None
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def main():
    builds = build_entries()
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
    print(f"{len(builds)} builds, {total} files.")


if __name__ == "__main__":
    main()
