#!/usr/bin/env python3
"""Check the download links in OTA-Packages.md and update their badges.

A badge only changes when the server gives a clear answer. Timeouts and
server errors are treated as "uncertain" and leave the badge unchanged.
The file is written only if something actually changed.

Exit codes: 0 = ok, 1 = error or no clear result, 2 = safety guard stopped the run.
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Settings

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

# Mirror hosts, tried in this order, with the short name used in the logs
MIRRORS = {
    "gauss-opexcostmanual-sg.allawnofs.com": "OP-SG",
    "gauss-opexcostmanual-eu.allawnofs.com": "OP-EU",
    "gauss-otacostmanual-sg.allawnofs.com": "OT-SG",
    "gauss-otacostmanual-eu.allawnofs.com": "OT-EU",
}
# Hosts that are not mirrors but can still show up in the Wiki
OTHER_HOST_LABELS = {"ota-manual-in": "OTA-IN", "drive.google": "GDR"}

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

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class State(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNCERTAIN = "uncertain"


ONLINE, OFFLINE, UNCERTAIN = State.ONLINE, State.OFFLINE, State.UNCERTAIN


class Exit(IntEnum):
    OK = 0
    ERROR = 1
    GUARD = 2
    INTERRUPTED = 130


def env_flag(name: str) -> bool:
    """True when the environment variable is set to 'true' (any case)."""
    return os.environ.get(name, "").lower() == "true"


# Console output

_USE_COLOR = not os.environ.get("NO_COLOR")
IN_ACTIONS = env_flag("GITHUB_ACTIONS")

RESET, BOLD = "\033[0m", "\033[1m"
GREEN, RED, YELLOW = "\033[92m", "\033[91m", "\033[93m"
CYAN, GRAY = "\033[96m", "\033[90m"

STATE_COLOR = {ONLINE: GREEN, UNCERTAIN: YELLOW, OFFLINE: RED}
STATE_LABEL = {ONLINE: "WORKING      ", OFFLINE: "ACCESS DENIED", UNCERTAIN: "INCONCLUSIVE "}


def paint(text: object, code: str) -> str:
    return f"{code}{text}{RESET}" if _USE_COLOR else str(text)


@contextmanager
def log_group(title: str) -> Iterator[None]:
    """Collapsible group in GitHub Actions; a plain cyan header elsewhere."""
    if IN_ACTIONS:
        print(f"::group::{title}", flush=True)
    else:
        print(paint(f"\n{title}", CYAN), flush=True)
    try:
        yield
    finally:
        if IN_ACTIONS:
            print("::endgroup::", flush=True)


def annotate(level: str, message: str) -> None:
    """Show a notice, warning or error on the Actions run page (no-op elsewhere)."""
    if IN_ACTIONS:
        print(f"::{level}::{message}", flush=True)


def write_summary(lines: list[str]) -> None:
    """Add lines to the GitHub Actions run summary (only exists inside Actions)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# Data types


@dataclass(frozen=True)
class Probe:
    """The answer of one request to one URL."""

    url: str
    status: int | str  # HTTP status, or the name of the error
    state: State


@dataclass
class Result:
    """The verdict for one link, after trying its mirrors if needed."""

    state: State
    final_url: str
    probes: list[Probe]


@dataclass
class Link:
    """One download badge found in the Markdown."""

    match: re.Match
    url: str
    current_color: str
    version: str
    section: str


@dataclass
class Decision:
    """What one badge should look like after the run."""

    link: Link
    color: str
    url: str


@dataclass
class Plan:
    decisions: list[Decision]
    changes: list[str]
    uncertain: list[Link]

    @property
    def working(self) -> int:
        return sum(1 for d in self.decisions if d.color == COLOR_WORKING)

    @property
    def denied(self) -> int:
        return sum(1 for d in self.decisions if d.color == COLOR_DENIED)


# Reading the Markdown

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


def today_str() -> str:
    """Date in the file's format, e.g. '23 Oct 2026'. Uses UTC so it matches the CI clock."""
    now = datetime.now(timezone.utc)
    return f"{now.day:02d} {MONTHS[now.month - 1]} {now.year}"


def find_version(text: str, position: int) -> str:
    """Return the version in the first cell of the table row that contains `position`."""
    tr_start = text.rfind("<tr", 0, position)
    if tr_start == -1:
        return "UNKNOWN"
    m = re.search(r"<td[^>]*>\s*([^<]+?)\s*</td>", text[tr_start:position], re.IGNORECASE)
    return m.group(1).strip() if m else "UNKNOWN"


def extract_links(text: str) -> list[Link]:
    """Return every badge in the file, with its version, section and color."""
    # Each section starts at its header. A link belongs to the last header before it.
    sections = [
        (m.start(), f"{'Export' if m.group(1).lower() == 'export' else 'GDPR'} · Android {m.group(2)}")
        for m in SECTION_PATTERN.finditer(text)
    ]
    positions = [pos for pos, _ in sections]

    links = []
    for m in LINK_PATTERN.finditer(text):
        i = bisect.bisect_right(positions, m.start()) - 1
        links.append(
            Link(
                match=m,
                url=m.group("url"),
                current_color=m.group("color").upper(),
                version=find_version(text, m.start()),
                section=sections[i][1] if i >= 0 else "?",
            )
        )
    return links


def rewrite_link(m: re.Match, new_url: str, new_color: str) -> str:
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


# Hosts


def is_allawnofs(url: str) -> bool:
    return "allawnofs.com" in url.lower()


def url_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def swap_host(url: str, host: str) -> str:
    """Replace the host of `url` with `host`, keeping the path."""
    return re.sub(r"(?<=https://)[^/]+(?=/)", host, url, count=1, flags=re.IGNORECASE)


def host_label(host: str) -> str:
    """Short name for the logs, e.g. OP-SG."""
    host = host.lower()
    if host in MIRRORS:
        return MIRRORS[host]
    return next((label for key, label in OTHER_HOST_LABELS.items() if key in host), "OTH")


def alternate_hosts(url: str) -> list[str]:
    current = url_host(url)
    return [h for h in MIRRORS if h != current]


# Checking links


def make_session() -> requests.Session:
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


def classify(status: int, content_type: str | None, final_url: str, tested_url: str) -> State:
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


def probe(url: str) -> Probe:
    """Request one URL. Only the headers are read, not the file."""
    try:
        with session.get(
            url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True
        ) as r:
            state = classify(r.status_code, r.headers.get("Content-Type"), r.url, url)
            return Probe(url, r.status_code, state)
    except requests.exceptions.Timeout:
        return Probe(url, "TIMEOUT", UNCERTAIN)
    except requests.exceptions.RequestException as e:
        return Probe(url, type(e).__name__, UNCERTAIN)


def evaluate(url: str, allow_alternates_on_uncertain: bool) -> Result:
    """Check a link and, if needed, its mirror hosts.

    The link's URL changes only when a mirror host is confirmed to work.
    A temporary error on the original host does not switch hosts on the
    first pass. That avoids switching because of a single timeout.
    """
    original = probe(url)
    probes = [original]

    if original.state is ONLINE:
        return Result(ONLINE, url, probes)

    # Links outside allawnofs (e.g. Google Drive) have no mirrors to try
    if not is_allawnofs(url):
        return Result(original.state, url, probes)

    if original.state is UNCERTAIN and not allow_alternates_on_uncertain:
        return Result(UNCERTAIN, url, probes)

    for host in alternate_hosts(url):
        p = probe(swap_host(url, host))
        probes.append(p)
        if p.state is ONLINE:
            return Result(ONLINE, p.url, probes)

    # Only mark as offline when every host answered clearly with "not found"
    all_offline = all(p.state is OFFLINE for p in probes)
    return Result(OFFLINE if all_offline else UNCERTAIN, url, probes)


def format_probes(probes: list[Probe]) -> str:
    return "  ".join(
        f"{host_label(url_host(p.url))}={paint(p.status, STATE_COLOR[p.state])}" for p in probes
    )


def log_result(i: int, total: int, version: str, original_url: str, res: Result) -> None:
    extra = ""
    if res.state is ONLINE and res.final_url != original_url:
        extra = paint(f"  -> URL={host_label(url_host(res.final_url))}", GREEN)
    counter = paint("[%03d/%03d]" % (i, total), GRAY)
    ver = paint("%-5s" % version, BOLD + CYAN)
    label = paint(STATE_LABEL[res.state], STATE_COLOR[res.state])
    print(f"{counter} {ver} {label} {format_probes(res.probes)}{extra}", flush=True)


def evaluate_batch(
    urls: list[str], allow_alternates: bool, title: str, versions: dict[str, str]
) -> dict[str, Result]:
    """Check many URLs in parallel. One failing link never stops the batch.

    Results are logged as they finish, so one slow link does not hide the others.
    """
    results: dict[str, Result] = {}
    total = len(urls)
    with log_group(f"{title} ({total} link(s))"):
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
            futures = {ex.submit(evaluate, u, allow_alternates): u for u in urls}
            for i, future in enumerate(as_completed(futures), 1):
                u = futures[future]
                try:
                    res = future.result()
                except Exception as e:
                    res = Result(UNCERTAIN, u, [Probe(u, type(e).__name__, UNCERTAIN)])
                results[u] = res
                log_result(i, total, versions[u], u, res)
    return results


def internet_ok() -> bool:
    """Return False if the runner has no internet. Then no badge is changed."""
    try:
        r = session.get(CONTROL_URL, timeout=TIMEOUT, headers={"User-Agent": HEADERS["User-Agent"]})
        return r.status_code < 500
    except requests.exceptions.RequestException:
        return False


def run_checks(urls: list[str], versions: dict[str, str]) -> dict[str, Result]:
    """Pass 1 tests every URL. Pass 2 retests only the ones that were uncertain."""
    results = evaluate_batch(urls, False, "Pass 1", versions)
    uncertain = [u for u, r in results.items() if r.state is UNCERTAIN]
    if uncertain:
        print(
            paint(
                f"\n{len(uncertain)} inconclusive link(s). Waiting "
                f"{SECOND_PASS_DELAY}s before retesting...",
                YELLOW,
            ),
            flush=True,
        )
        time.sleep(SECOND_PASS_DELAY)
        results.update(evaluate_batch(uncertain, True, "Pass 2", versions))
    return results


# Deciding and writing


def build_plan(links: list[Link], results: dict[str, Result]) -> Plan:
    """Decide the new color and URL for each badge, and list what changed."""
    decisions, changes, uncertain = [], [], []
    for link in links:
        res = results[link.url]
        if res.state is ONLINE:
            color, url = COLOR_WORKING, res.final_url
        elif res.state is OFFLINE:
            color, url = COLOR_DENIED, link.url
        else:
            # No clear answer: keep the badge as it is
            color, url = link.current_color, link.url
            uncertain.append(link)
        decisions.append(Decision(link, color, url))

        tag = f"{link.version} ({link.section})"
        if color.upper() != link.current_color:
            old, new = ("online", "offline") if color == COLOR_DENIED else ("offline", "online")
            changes.append(f"{tag}: {old} -> {new}")
        if url != link.url:
            changes.append(
                f"{tag}: host {host_label(url_host(link.url))} -> {host_label(url_host(url))}"
            )
    return Plan(decisions, changes, uncertain)


def guard_warning(links: list[Link], plan: Plan, force: bool) -> str | None:
    """Return a warning if a large share of working links would turn red at once."""
    blue_before = sum(1 for l in links if l.current_color == COLOR_WORKING)
    drops = sum(
        1 for d in plan.decisions
        if d.color == COLOR_DENIED and d.link.current_color == COLOR_WORKING
    )
    if force or drops < GUARD_MIN_DROPS or not blue_before or drops / blue_before < GUARD_RATIO:
        return None
    return (
        f"Safety guard: {drops} of {blue_before} working links would turn red "
        f"at once. This may be the CDN blocking the runner. Nothing was written. "
        f"If the drop is real, run again with --force."
    )


def apply_plan(text: str, plan: Plan, always_bump: bool) -> str:
    """Rewrite only the badges, then the date if anything changed."""
    by_position = {d.link.match.start(): d for d in plan.decisions}

    def rewrite(m: re.Match) -> str:
        d = by_position[m.start()]
        return rewrite_link(m, d.url, d.color)

    new_text = LINK_PATTERN.sub(rewrite, text)
    if plan.changes or always_bump:
        new_text = DATE_PATTERN.sub(lambda m: f"{m.group(1)}{today_str()}", new_text, count=1)
    return new_text


def summary_lines(
    urls: list[str], plan: Plan, results: dict[str, Result], dry_run: bool
) -> list[str]:
    """Markdown for the GitHub Actions run summary."""
    lines = [
        "## Link check result",
        f"- Unique links tested: **{len(urls)}**",
        f"- Changes: **{len(plan.changes)}**",
        f"- Inconclusive (kept): **{len(plan.uncertain)}**",
    ]
    lines += [f"  - {c}" for c in plan.changes]
    if dry_run:
        lines.append("\n> 🧪 Dry run: nothing was written.")

    icon = {ONLINE: "✅ working", OFFLINE: "⛔ denied", UNCERTAIN: "⚠️ inconclusive"}
    lines += ["", "<details><summary>All links</summary>", "",
              "| Version | Section | Status | Host |", "|---|---|---|---|"]
    for d in plan.decisions:
        state = results[d.link.url].state
        lines.append(
            f"| {d.link.version} | {d.link.section} | {icon[state]} | "
            f"{host_label(url_host(d.url))} |"
        )
    lines += ["", "</details>"]
    return lines


def write_commit_message(path: Path, changes: list[str]) -> None:
    """The workflow uses this message as the Wiki commit message."""
    body = "\n".join(f"- {c}" for c in changes) or "- Updated the last-check date"
    path.write_text(
        f"chore: update OTA link status ({len(changes)} change(s))\n\n{body}\n",
        encoding="utf-8",
    )


# Command line


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Check the links in OTA-Packages.md")
    ap.add_argument("--file", default=os.environ.get("OTA_FILE", "OTA-Packages.md"))
    ap.add_argument("--dry-run", action="store_true", default=env_flag("OTA_DRY_RUN"),
                    help="check everything but do not write the file")
    ap.add_argument("--force", action="store_true", default=env_flag("OTA_FORCE"),
                    help="skip the safety guard")
    ap.add_argument("--msg-file", default=os.environ.get("OTA_MSG_FILE"),
                    help="write the commit message to this file when there are changes")
    return ap.parse_args()


def main() -> Exit:
    args = parse_args()
    # When true, the date is updated even if no link changed
    always_bump = env_flag("OTA_ALWAYS_BUMP_DATE")

    path = Path(args.file)
    if not path.exists():
        print(paint(f"File not found: {path}", RED))
        annotate("error", f"File not found: {path}")
        return Exit.ERROR

    # Read bytes and decode, so the original line endings (CRLF or LF) are kept
    text = path.read_bytes().decode("utf-8")
    links = extract_links(text)
    if not links:
        print(paint("No link/badge found in the Markdown.", RED))
        annotate("error", "No link/badge found in the Markdown.")
        return Exit.ERROR

    # The same URL can appear in more than one table. Test it only once.
    versions: dict[str, str] = {}
    for link in links:
        versions.setdefault(link.url, link.version)
    urls = list(versions)

    print(f"File: {path} | badges: {len(links)} | unique URLs: {len(urls)}")
    print(f"Mode: {'dry run (no writes)' if args.dry_run else 'normal'}")

    if not internet_ok():
        print(paint("No connectivity. Nothing was changed.", RED))
        annotate("error", "No connectivity. Nothing was changed.")
        return Exit.ERROR

    results = run_checks(urls, versions)
    plan = build_plan(links, results)

    warning = guard_warning(links, plan, args.force)
    if warning:
        print(paint("\n" + warning, RED))
        annotate("error", warning)
        write_summary(["## ⚠️ Safety guard triggered", warning])
        return Exit.GUARD

    if len(plan.uncertain) == len(links):
        print(paint("\nNo link got a clear answer. Nothing was changed.", RED))
        annotate("error", "No link got a clear answer. Nothing was changed.")
        return Exit.ERROR

    if plan.uncertain:
        versions_list = ", ".join(sorted({l.version for l in plan.uncertain}))
        annotate("warning", f"{len(plan.uncertain)} link(s) inconclusive, badge kept: {versions_list}")

    new_text = apply_plan(text, plan, always_bump)
    changed = new_text != text

    print(f"\nWorking: {plan.working} | Denied: {plan.denied} | "
          f"Inconclusive (kept): {len(plan.uncertain)} | Changes: {len(plan.changes)}")
    for c in plan.changes:
        print(f"  - {c}")
    write_summary(summary_lines(urls, plan, results, args.dry_run))

    if not changed:
        print(paint("\nNo changes needed. File untouched.", GREEN))
        return Exit.OK

    if args.dry_run:
        print(paint("\nDry run: the file would change, but nothing was written.", YELLOW))
        return Exit.OK

    path.write_bytes(new_text.encode("utf-8"))
    print(paint(f"\nFile updated: {path}", GREEN))

    if args.msg_file:
        write_commit_message(Path(args.msg_file), plan.changes)
    return Exit.OK


if __name__ == "__main__":
    try:
        sys.exit(int(main()))
    except KeyboardInterrupt:
        print(paint("\nInterrupted by user.", YELLOW))
        sys.exit(int(Exit.INTERRUPTED))
