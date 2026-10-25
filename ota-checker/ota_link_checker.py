#!/usr/bin/env python3
"""Check the download links in OTA-Packages.md and update their badges.

A badge only changes when the server gives a clear answer. Timeouts and
server errors are treated as "uncertain" and leave the badge unchanged.
The file is written only if something actually changed.

Exit codes: 0 = ok, 1 = error or no clear result, 2 = safety guard stopped the run.
"""

import argparse
import bisect
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Badge colors (hex, without #)
COLOR_WORKING = "2563EB"
COLOR_DENIED = "DC2626"

MAX_THREADS = 8
TIMEOUT = (6, 12)  # (connect, read) in seconds
SECOND_PASS_DELAY = 30  # seconds to wait before retesting uncertain links

# The safety guard stops the run if too many working links would turn red
# at once. This usually means the CDN blocked the runner, not that the files
# were removed.
GUARD_MIN_DROPS = 3
GUARD_RATIO = 0.5

# Mirror hosts, tried in this order
HOSTS = [
    "gauss-opexcostmanual-sg.allawnofs.com",
    "gauss-opexcostmanual-eu.allawnofs.com",
    "gauss-otacostmanual-sg.allawnofs.com",
    "gauss-otacostmanual-eu.allawnofs.com",
]

# Used to check that the runner has internet before testing anything
CONTROL_URL = "https://www.google.com/generate_204"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15) "
        "AppleWebKit/537.36 "
        "Chrome/140 Mobile Safari/537.36"
    ),
    # Request only the first byte. This confirms the file exists without
    # downloading gigabytes.
    "Range": "bytes=0-0",
}

ONLINE, OFFLINE, UNCERTAIN = "online", "offline", "uncertain"

_USE_COLOR = not os.environ.get("NO_COLOR")
RESET, BOLD = "\033[0m", "\033[1m"
GREEN, RED, YELLOW = "\033[92m", "\033[91m", "\033[93m"
CYAN, BLUE, GRAY, WHITE = "\033[96m", "\033[94m", "\033[90m", "\033[97m"


def paint(text, code):
    return f"{code}{text}{RESET}" if _USE_COLOR else str(text)


# Matches one download badge: <a href="..."><img src="...Download-COLOR.svg"></a>
LINK_PATTERN = re.compile(
    r'<a\b[^>]*\bhref="(?P<url>[^"]+)"[^>]*>'
    r"\s*"
    r'<img\b[^>]*\bsrc="https://shieldcn\.dev/badge/Download-'
    r"(?P<color>[A-Fa-f0-9]{6,8})"
    r'\.svg(?:\?[^"]*)?"[^>]*>'
    r"\s*</a>",
    re.IGNORECASE | re.DOTALL,
)

# Section headers, e.g. name="export--android-15" and name="gdpr--android-15"
SECTION_PATTERN = re.compile(r'name="(export|gdpr)--android-(\d+)"', re.IGNORECASE)

DATE_PATTERN = re.compile(r"(Last URL check:\s*)\d{1,2}\s+[A-Za-z]{3}\s+\d{4}", re.IGNORECASE)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def today_str():
    """Date in the file's format, e.g. '23 Oct 2026'. Uses UTC so it matches the CI clock."""
    now = datetime.now(timezone.utc)
    return f"{now.day:02d} {MONTHS[now.month - 1]} {now.year}"


def find_version(text, position):
    """Return the version in the first cell of the table row that contains `position`."""
    tr_start = text.rfind("<tr", 0, position)
    if tr_start == -1:
        return "UNKNOWN"
    m = re.search(r"<td[^>]*>\s*([^<]+?)\s*</td>", text[tr_start:position], re.IGNORECASE)
    return m.group(1).strip() if m else "UNKNOWN"


def extract_links(text):
    """Return a dict for each badge in the file, with its version, section and color."""
    # Each section starts at its header. A link belongs to the last header before it.
    sections = [
        (m.start(), f"{'Export' if m.group(1).lower() == 'export' else 'GDPR'} · Android {m.group(2)}")
        for m in SECTION_PATTERN.finditer(text)
    ]
    positions = [p for p, _ in sections]

    links = []
    for m in LINK_PATTERN.finditer(text):
        i = bisect.bisect_right(positions, m.start()) - 1
        links.append({
            "match": m,
            "url": m.group("url"),
            "current_color": m.group("color").upper(),
            "version": find_version(text, m.start()),
            "section": sections[i][1] if i >= 0 else "?",
        })
    return links


def rewrite_link(m, new_url, new_color):
    """Return the badge text with a new href and color. Everything else stays the same."""
    snippet, base = m.group(0), m.start()
    # Replace from the end first, so the earlier offsets stay valid
    parts = [
        (m.start("url") - base, m.end("url") - base, new_url),
        (m.start("color") - base, m.end("color") - base, new_color),
    ]
    for start, end, value in sorted(parts, reverse=True):
        snippet = snippet[:start] + value + snippet[end:]
    return snippet


def is_allawnofs(url):
    return "allawnofs.com" in url.lower()


def url_host(url):
    return (urlsplit(url).hostname or "").lower()


def swap_host(url, host):
    """Replace the host of `url` with `host`, keeping the path."""
    return re.sub(r"(?<=https://)[^/]+(?=/)", host, url, count=1, flags=re.IGNORECASE)


def host_label(host):
    """Short name for logs, e.g. OP-SG."""
    h = host.lower()
    for key, label in (
        ("opexcostmanual-sg", "OP-SG"), ("opexcostmanual-eu", "OP-EU"),
        ("otacostmanual-sg", "OT-SG"), ("otacostmanual-eu", "OT-EU"),
        ("ota-manual-in", "OTA-IN"), ("drive.google", "GDR"),
    ):
        if key in h:
            return label
    return "OTH"


def alternate_hosts(url):
    current = url_host(url)
    return [h for h in HOSTS if h.lower() != current]


def make_session():
    """HTTP session with connection reuse and automatic retries for temporary failures."""
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=MAX_THREADS * 2, pool_maxsize=MAX_THREADS * 2, max_retries=retry
    )
    s = requests.Session()
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


session = make_session()


def classify(status, content_type, final_url, tested_url):
    """Turn an HTTP response into ONLINE, OFFLINE or UNCERTAIN."""
    # 416 means the range is invalid, but the file exists. Treat it as online.
    if 200 <= status < 300 or status == 416:
        # Google Drive redirects to a login page when the file is private
        if url_host(final_url) == "accounts.google.com":
            return OFFLINE
        # Some removed files return an HTML error page with status 200
        path = urlsplit(tested_url).path.lower()
        if path.endswith(".zip") and "text/html" in (content_type or "").lower():
            return OFFLINE
        return ONLINE
    # Rate limits and server errors are temporary, so we don't judge the link
    if status in (408, 425, 429) or status >= 500:
        return UNCERTAIN
    if 400 <= status < 500:
        return OFFLINE
    return UNCERTAIN


def probe(url):
    """Request one URL. Only the headers are read, not the file."""
    try:
        with session.get(
            url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True
        ) as r:
            state = classify(r.status_code, r.headers.get("Content-Type"), r.url, url)
            return {"url": url, "status": r.status_code, "state": state}
    except requests.exceptions.Timeout:
        return {"url": url, "status": "TIMEOUT", "state": UNCERTAIN}
    except requests.exceptions.RequestException as e:
        return {"url": url, "status": type(e).__name__, "state": UNCERTAIN}


def evaluate(url, allow_alternates_on_uncertain):
    """Check a link and, if needed, its mirror hosts.

    The link's URL changes only when a mirror host is confirmed to work.
    A temporary error on the original host does not switch hosts on the
    first pass. That avoids switching because of a single timeout.
    """
    probes = [probe(url)]
    original = probes[0]

    if original["state"] == ONLINE:
        return {"state": ONLINE, "final_url": url, "probes": probes}

    # Links outside allawnofs (e.g. Google Drive) have no mirrors to try
    if not is_allawnofs(url):
        return {"state": original["state"], "final_url": url, "probes": probes}

    if original["state"] == UNCERTAIN and not allow_alternates_on_uncertain:
        return {"state": UNCERTAIN, "final_url": url, "probes": probes}

    for host in alternate_hosts(url):
        p = probe(swap_host(url, host))
        probes.append(p)
        if p["state"] == ONLINE:
            return {"state": ONLINE, "final_url": p["url"], "probes": probes}

    # Only mark as offline when every host answered clearly with "not found"
    all_offline = all(p["state"] == OFFLINE for p in probes)
    return {
        "state": OFFLINE if all_offline else UNCERTAIN,
        "final_url": url,
        "probes": probes,
    }


def format_probes(probes):
    parts = []
    for p in probes:
        label = host_label(url_host(p["url"]))
        color = GREEN if p["state"] == ONLINE else YELLOW if p["state"] == UNCERTAIN else RED
        parts.append(f"{label}={paint(p['status'], color)}")
    return "  ".join(parts)


def log_result(i, total, version, original_url, res):
    state = res["state"]
    label = {
        ONLINE: paint("WORKING      ", GREEN),
        OFFLINE: paint("ACCESS DENIED", RED),
        UNCERTAIN: paint("INCONCLUSIVE ", YELLOW),
    }[state]
    extra = ""
    if state == ONLINE and res["final_url"] != original_url:
        extra = paint(f"  -> URL={host_label(url_host(res['final_url']))}", GREEN)
    counter = paint("[%03d/%03d]" % (i, total), GRAY)
    ver = paint("%-5s" % version, BOLD + CYAN)
    print(f"{counter} {ver} {label} {format_probes(res['probes'])}{extra}", flush=True)


def evaluate_batch(urls, allow_alternates, title, versions):
    """Check many URLs in parallel. One failing link never stops the batch."""
    results = {}
    total = len(urls)
    print(paint(f"\n{title} ({total} link(s))", CYAN), flush=True)
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
        futures = {u: ex.submit(evaluate, u, allow_alternates) for u in urls}
        for i, (u, f) in enumerate(futures.items(), 1):
            try:
                res = f.result()
            except Exception as e:
                res = {
                    "state": UNCERTAIN, "final_url": u,
                    "probes": [{"url": u, "status": type(e).__name__, "state": UNCERTAIN}],
                }
            results[u] = res
            log_result(i, total, versions[u], u, res)
    return results


def internet_ok():
    """Return False if the runner has no internet. Then no badge is changed."""
    try:
        r = session.get(CONTROL_URL, timeout=TIMEOUT, headers={"User-Agent": HEADERS["User-Agent"]})
        return r.status_code < 500
    except requests.exceptions.RequestException:
        return False


def write_summary(lines):
    """Add lines to the GitHub Actions run summary (only exists inside Actions)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Check the links in OTA-Packages.md")
    ap.add_argument("--file", default=os.environ.get("OTA_FILE", "OTA-Packages.md"))
    ap.add_argument("--dry-run", action="store_true",
                    default=os.environ.get("OTA_DRY_RUN", "").lower() == "true",
                    help="check everything but do not write the file")
    ap.add_argument("--force", action="store_true",
                    default=os.environ.get("OTA_FORCE", "").lower() == "true",
                    help="skip the safety guard")
    ap.add_argument("--msg-file", default=os.environ.get("OTA_MSG_FILE"),
                    help="write the commit message to this file when there are changes")
    args = ap.parse_args()
    # When true, the date is updated even if no link changed
    always_bump = os.environ.get("OTA_ALWAYS_BUMP_DATE", "").lower() == "true"

    path = Path(args.file)
    if not path.exists():
        print(paint(f"File not found: {path}", RED))
        return 1

    # Read bytes and decode, so the original line endings (CRLF or LF) are kept
    text = path.read_bytes().decode("utf-8")
    links = extract_links(text)
    if not links:
        print(paint("No link/badge found in the Markdown.", RED))
        return 1

    # The same URL can appear in more than one table. Test it only once.
    versions = {}
    for l in links:
        versions.setdefault(l["url"], l["version"])
    urls = list(versions)

    print(f"File: {path} | badges: {len(links)} | unique URLs: {len(urls)}")
    print(f"Mode: {'dry run (no writes)' if args.dry_run else 'normal'}")

    if not internet_ok():
        print(paint("No connectivity. Nothing was changed.", RED))
        return 1

    # Pass 2 retests only the links that were uncertain on pass 1
    res = evaluate_batch(urls, False, "Pass 1", versions)
    uncertain = [u for u, r in res.items() if r["state"] == UNCERTAIN]
    if uncertain:
        print(paint(f"\n{len(uncertain)} inconclusive link(s). Waiting "
                    f"{SECOND_PASS_DELAY}s before retesting...", YELLOW), flush=True)
        time.sleep(SECOND_PASS_DELAY)
        res.update(evaluate_batch(uncertain, True, "Pass 2", versions))

    # Decide the new color and URL for each badge
    decisions = []  # (link, new_color, new_url)
    changes = []
    still_uncertain = []
    for l in links:
        r = res[l["url"]]
        if r["state"] == ONLINE:
            new_color, new_url = COLOR_WORKING, r["final_url"]
        elif r["state"] == OFFLINE:
            new_color, new_url = COLOR_DENIED, l["url"]
        else:
            # No clear answer: keep the badge as it is
            new_color, new_url = l["current_color"], l["url"]
            still_uncertain.append(l)
        decisions.append((l, new_color, new_url))

        tag = f"{l['version']} ({l['section']})"
        if new_color.upper() != l["current_color"]:
            old, new = ("online", "offline") if new_color == COLOR_DENIED else ("offline", "online")
            changes.append(f"{tag}: {old} -> {new}")
        if new_url != l["url"]:
            changes.append(
                f"{tag}: host {host_label(url_host(l['url']))} -> "
                f"{host_label(url_host(new_url))}"
            )

    # Stop if a large share of working links would turn red at once
    blue_before = sum(1 for l in links if l["current_color"] == COLOR_WORKING)
    drops = sum(1 for l, c, _ in decisions
                if c == COLOR_DENIED and l["current_color"] == COLOR_WORKING)
    if (not args.force and drops >= GUARD_MIN_DROPS
            and blue_before and drops / blue_before >= GUARD_RATIO):
        warning = (f"Safety guard: {drops} of {blue_before} working links would turn red "
                   f"at once. This may be the CDN blocking the runner. Nothing was written. "
                   f"If the drop is real, run again with --force.")
        print(paint("\n" + warning, RED))
        write_summary(["## ⚠️ Safety guard triggered", warning])
        return 2

    if len(still_uncertain) == len(links):
        print(paint("\nNo link got a clear answer. Nothing was changed.", RED))
        return 1

    # Rewrite only the badges, then the date if anything changed
    mapping = {l["match"].start(): (c, u) for l, c, u in decisions}
    new_text = LINK_PATTERN.sub(
        lambda m: rewrite_link(m, mapping[m.start()][1], mapping[m.start()][0]), text
    )
    if changes or always_bump:
        new_text = DATE_PATTERN.sub(lambda m: f"{m.group(1)}{today_str()}", new_text, count=1)

    changed = new_text != text

    working = sum(1 for _, c, _ in decisions if c == COLOR_WORKING)
    denied = sum(1 for _, c, _ in decisions if c == COLOR_DENIED)
    print(f"\nWorking: {working} | Denied: {denied} | "
          f"Inconclusive (kept): {len(still_uncertain)} | Changes: {len(changes)}")
    for c in changes:
        print(f"  - {c}")

    summary = ["## Link check result",
               f"- Unique links tested: **{len(urls)}**",
               f"- Changes: **{len(changes)}**",
               f"- Inconclusive (kept): **{len(still_uncertain)}**"]
    summary += [f"  - {c}" for c in changes]
    if args.dry_run:
        summary.append("\n> 🧪 Dry run: nothing was written.")
    write_summary(summary)

    if not changed:
        print(paint("\nNo changes needed. File untouched.", GREEN))
        return 0

    if args.dry_run:
        print(paint("\nDry run: the file would change, but nothing was written.", YELLOW))
        return 0

    path.write_bytes(new_text.encode("utf-8"))
    print(paint(f"\nFile updated: {path}", GREEN))

    # The workflow uses this message as the Wiki commit message
    if args.msg_file:
        body = "\n".join(f"- {c}" for c in changes) or "- Updated the last-check date"
        Path(args.msg_file).write_text(
            f"chore: update OTA link status ({len(changes)} change(s))\n\n{body}\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(paint("\nInterrupted by user.", YELLOW))
        sys.exit(130)
