#!/usr/bin/env python3
"""Scrape publicly available NFL player stats from nfl.com.

Two kinds of data are collected, both from server-rendered HTML:

1. **Leaderboards** - https://www.nfl.com/stats/player-stats/ lists every
   player with a stat line in a category, 25 per page, at::

       /stats/player-stats/category/{category}/{season}/reg/all/{sort}/desc
       ?aftercursor=...

   Every page is followed until the "Next Page" link disappears. One CSV +
   JSON per category lands in ``players/`` (e.g. ``2025_passing.csv``).
   These tables carry only the player's name and profile slug; team and
   position are not shown, so they come from the profile scrape below.

2. **Player profiles** - for each ``--player`` slug (the last part of
   https://www.nfl.com/players/{slug}/) three pages are read:

       /players/{slug}/                       bio, position, current team
       /players/{slug}/stats/career           per-season tables per category
       /players/{slug}/stats/logs/{season}/   game logs (pre/regular/post)

   The result is ``players/profiles/{slug}.json`` plus, when several
   players are requested, ``players/{season}_players_summary.csv``.
   Leaderboard files already on disk for the season are joined in so each
   profile also records the player's rank in every category they appear in.

Usage examples::

    python3 scrape_players.py                              # 2025 leaderboards, all categories
    python3 scrape_players.py --category passing rushing
    python3 scrape_players.py --season 2024 --max-pages 2  # just the top 50 per category
    python3 scrape_players.py --player matthew-stafford myles-garrett --no-leaderboards
    python3 scrape_players.py --players-file top10_players.txt --no-leaderboards

Quirks handled here:

* Like the team pages, the season-type segment is ignored by the site for
  leaderboards (``post`` returns regular-season numbers). Game logs do have
  separate preseason / regular season / post season tables, and those are
  kept apart.
* The leaderboard "Next Page" cursor intermittently serves the previous
  page again. A page that starts with an already-collected player is
  retried with a cache-busting parameter, overlapping rows are dropped,
  and pagination stops once the sort stat reaches 0 (``--include-zeros``
  keeps going; the tail is every roster player with an empty stat line).
* Game-log tables repeat column names across stat groups (passing YDS,
  rushing YDS ...) without any group label in the markup. Columns are
  split into groups where a header repeats or a known leading column
  (COMP, REC, ATT, FUM, RET, ...) starts, and keyed ``group.COLUMN``.
* Career tables end with a TOTAL row in ``<tfoot>``; it is stored under
  ``totals`` rather than mixed in with the per-season rows.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

SITE = "https://www.nfl.com"
OUT_DIR = Path(__file__).resolve().parent / "players"
DEFAULT_SEASON = 2025
PAGE_SIZE = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# category slug -> (default sort key used by nfl.com, expected first stat column)
CATEGORIES: dict[str, tuple[str, str]] = {
    "passing": ("passingyards", "Pass Yds"),
    "rushing": ("rushingyards", "Rush Yds"),
    "receiving": ("receivingreceptions", "Rec"),
    "fumbles": ("defensiveforcedfumble", "FF"),
    "tackles": ("defensivecombinetackles", "Comb"),
    "interceptions": ("defensiveinterceptions", "INT"),
    "field-goals": ("kickingfgmade", "FGM"),
    "kickoffs": ("kickofftotal", "KO"),
    "kickoff-returns": ("kickreturnsaverageyards", "Avg"),
    "punts": ("puntingaverageyards", "Avg"),
    "punt-returns": ("puntreturnsaverageyards", "Avg"),
}


# ---------------------------------------------------------------------------
# Generic HTML table extraction
# ---------------------------------------------------------------------------

class Table:
    def __init__(self, label: str) -> None:
        self.label = label          # nearest preceding heading text
        self.headers: list[str] = []
        self.rows: list[dict] = []  # {"cells": [...], "links": [...], "images": [...]}
        self.footer: list[str] = []


class PageTables(HTMLParser):
    """Collect every <table> on a page along with the heading that precedes it.

    Also records the "Next Page" pagination link when one exists.
    """

    HEADING_TAGS = ("h1", "h2", "h3", "h4")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self.next_href: str | None = None
        self._heading = ""
        self._in_heading = False
        self._table: Table | None = None
        self._section = ""          # thead / tbody / tfoot
        self._row: dict | None = None
        self._in_cell = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and "nfl-o-table-pagination__next" in (a.get("class") or ""):
            self.next_href = a.get("href")
        if tag in self.HEADING_TAGS:
            self._in_heading = True
            self._text = []
        elif tag == "table":
            self._table = Table(self._heading)
            self._section = "tbody"
        elif self._table is None:
            return
        elif tag in ("thead", "tbody", "tfoot"):
            self._section = tag
        elif tag == "tr":
            self._row = {"cells": [], "links": [], "images": []}
        elif tag in ("td", "th") and self._row is not None:
            self._in_cell = True
            self._text = []
        elif tag == "a" and self._in_cell and a.get("href"):
            self._row["links"].append(a["href"])
        elif tag == "img" and self._in_cell and a.get("src"):
            self._row["images"].append(a["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HEADING_TAGS and self._in_heading:
            self._heading = clean("".join(self._text))
            self._in_heading = False
        elif self._table is None:
            return
        elif tag in ("td", "th") and self._in_cell:
            self._row["cells"].append(clean("".join(self._text)))
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            if self._section == "thead":
                if not self._table.headers:
                    self._table.headers = self._row["cells"]
            elif self._section == "tfoot":
                self._table.footer = self._row["cells"]
            elif self._row["cells"]:
                self._table.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._in_heading or self._in_cell:
            self._text.append(data)


def clean(text: str) -> str:
    return " ".join(text.split())


def parse_number(value: str):
    """Return int/float for numeric strings, None for blanks, else the string."""
    text = value.replace(",", "").strip()
    if text in ("", "-", "--"):
        return None
    if "_" in text:  # int("13_9") would silently become 139
        return value
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _decode_body(resp) -> bytes:
    body = resp.read()
    encoding = resp.headers.get("Content-Encoding", "")
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    return body


def fetch_html(url: str, retries: int = 3, timeout: int = 45) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _decode_body(resp).decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            wait = 2 ** attempt
            print(f"  attempt {attempt}/{retries} failed ({exc}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}") from last_error


def fetch_tables(url: str) -> PageTables:
    parser = PageTables()
    parser.feed(fetch_html(url))
    return parser


# ---------------------------------------------------------------------------
# Leaderboards
# ---------------------------------------------------------------------------

SLUG_RE = re.compile(r"^/players/([^/]+)/?")
HEADSHOT_RE = re.compile(r"/league/([A-Za-z0-9]+)$")


def leaderboard_url(category: str, season: int) -> str:
    sort_key, _ = CATEGORIES[category]
    return f"{SITE}/stats/player-stats/category/{category}/{season}/reg/all/{sort_key}/desc"


STALE_RETRIES = 5
STALE_WAIT = 6  # nfl.com caches pages for 5 seconds


def fetch_leaderboard_page(url: str, category: str, season: int, seen: set[str]) -> PageTables:
    """Fetch one page, retrying when nfl.com serves a stale (already seen) page.

    The site's "Next Page" cursor intermittently returns the previous page's
    content. A page whose first player was already collected is treated as
    stale and re-requested with a cache-busting parameter.
    """
    expected_first = CATEGORIES[category][1]
    for attempt in range(1, STALE_RETRIES + 1):
        probe = url if attempt == 1 else f"{url}&_={int(time.time() * 1000)}"
        parsed = fetch_tables(probe)
        if not parsed.tables or not parsed.tables[0].rows:
            raise ValueError(f"{category} {season}: no table on {url}")
        table = parsed.tables[0]
        stat_headers = table.headers[1:]
        if not stat_headers or stat_headers[0] != expected_first:
            raise ValueError(f"{category} {season}: unexpected columns {stat_headers}")
        if row_key(table.rows[0]) not in seen:
            return parsed
        print(f"  stale page (starts with {table.rows[0]['cells'][0]} again); retry {attempt}/{STALE_RETRIES} in {STALE_WAIT}s", file=sys.stderr)
        time.sleep(STALE_WAIT)
    raise ValueError(f"{category} {season}: nfl.com kept returning an already-collected page for {url}")


def row_slug(r: dict) -> str:
    return next((m.group(1) for h in r["links"] for m in [SLUG_RE.match(h)] if m), "")


def row_key(r: dict) -> str:
    """Identity used for de-duplication: profile slug, or the name when nfl.com has no profile link."""
    return row_slug(r) or f"name:{r['cells'][0]}"


def scrape_leaderboard(category: str, season: int, delay: float, max_pages: int | None, include_zeros: bool) -> list[dict]:
    url = leaderboard_url(category, season)
    sort_column = CATEGORIES[category][1]
    rows: list[dict] = []
    seen: set[str] = set()
    page = 0
    while url:
        page += 1
        print(f"Fetching {season} {category} page {page} ...")
        parsed = fetch_leaderboard_page(url, category, season, seen)
        table = parsed.tables[0]
        stat_headers = table.headers[1:]
        hit_zero = False
        for r in table.rows:
            cells = r["cells"]
            if len(cells) - 1 != len(stat_headers):
                raise ValueError(f"{category} {season}: row {cells[:1]} has {len(cells) - 1} stat cells, expected {len(stat_headers)}")
            slug = row_slug(r)
            key = row_key(r)
            if key in seen:
                continue  # pages sometimes overlap
            row = {
                "season": season,
                "category": category,
                "rank": len(rows) + 1,
                "player": cells[0],
                "slug": slug,
                "headshot_id": next((m.group(1) for src in r["images"] for m in [HEADSHOT_RE.search(src)] if m), ""),
            }
            for header, cell in zip(stat_headers, cells[1:]):
                row[header] = parse_number(cell)
            if not include_zeros and not row.get(sort_column):
                hit_zero = True
                break
            seen.add(key)
            rows.append(row)
        if hit_zero or (max_pages and page >= max_pages):
            break
        url = urllib.parse.urljoin(SITE, parsed.next_href) if parsed.next_href else None
        if url:
            time.sleep(delay)
    return rows


# ---------------------------------------------------------------------------
# Player profiles
# ---------------------------------------------------------------------------

META_COLUMNS = {"SEASON", "YEAR", "TEAM", "G", "GS", "WK", "Game Date", "OPP", "RESULT"}
GROUP_LEADERS = {"COMP", "REC", "FUM", "TKL", "Total", "COMBINED", "RET", "FGM", "XPM", "KO", "PUNTS"}


def group_name(columns: list[str]) -> str:
    cols = set(columns)
    if "COMP" in cols or "PCT" in cols:
        return "passing"
    if "REC" in cols:
        return "receiving"
    if "ATT" in cols and "FGM" not in cols:
        return "rushing"
    if "FUM" in cols and "LOST" in cols:
        return "fumbles"
    if cols & {"TKL", "Total", "COMBINED", "SOLO", "Solo", "SCK", "PDEF"}:
        return "defense"
    if "RET" in cols:
        return "returns"
    if cols & {"FGM", "XPM", "FG"}:
        return "kicking"
    if cols & {"KO", "TB"}:
        return "kickoffs"
    if cols & {"PUNTS", "NET AVG"}:
        return "punting"
    return "other"


def group_columns(headers: list[str], fixed_group: str | None = None) -> list[str]:
    """Return one key per header: meta columns as-is, stats as ``group.COLUMN``.

    With ``fixed_group`` (career tables, which are labelled on the page) every
    stat column gets that prefix. Otherwise a new group starts whenever a
    column name repeats within the current group or a known leading column
    appears (``ATT`` counts unless it sits next to ``COMP``, the passing layout).
    """
    keys: list[str] = []
    groups: list[list[str]] = []
    current: list[str] = []
    for i, h in enumerate(headers):
        if h in META_COLUMNS and not current:
            keys.append(h)
            continue
        if fixed_group:
            keys.append(f"{fixed_group}.{h}")
            continue
        nxt = headers[i + 1] if i + 1 < len(headers) else ""
        starts_new = bool(current) and (
            h in current
            or h in GROUP_LEADERS
            or (h == "ATT" and current != ["COMP"] and nxt != "COMP")
        )
        if starts_new:
            groups.append(current)
            current = []
        current.append(h)
        keys.append(None)  # filled below once the group is named
    if current:
        groups.append(current)
    # Second pass: name the groups and fill in the keys.
    named = []
    for g in groups:
        name = group_name(g)
        # Kick vs punt returns cannot be told apart by columns; number them.
        if name in [n for n, _ in named]:
            name = f"{name}{sum(1 for n, _ in named if n.startswith(name)) + 1}"
        named.append((name, g))
    it = iter(named)
    name, cols, idx = None, [], 0
    for i, k in enumerate(keys):
        if k is not None:
            continue
        if idx == 0 or idx >= len(cols):
            name, cols = next(it)
            idx = 0
        keys[i] = f"{name}.{cols[idx]}"
        idx += 1
    return keys


def table_to_records(table: Table, fixed_group: str | None = None) -> list[dict]:
    keys = group_columns(table.headers, fixed_group)
    records = []
    for r in table.rows:
        cells = r["cells"]
        if len(cells) != len(keys):
            raise ValueError(f"table '{table.label}': row {cells[:2]} has {len(cells)} cells, expected {len(keys)} ({table.headers})")
        records.append({k: parse_number(c) for k, c in zip(keys, cells)})
    return records


def footer_to_record(table: Table, fixed_group: str | None = None) -> dict | None:
    if not table.footer:
        return None
    keys = group_columns(table.headers, fixed_group)
    if len(table.footer) != len(keys):
        return {"raw": table.footer}
    return {k: parse_number(c) for k, c in zip(keys, table.footer) if k not in META_COLUMNS or c}


def parse_profile(html: str) -> dict:
    """Bio and current-team block from /players/{slug}/."""
    def text(fragment: str) -> str:
        return clean(unescape(re.sub(r"<[^>]+>", " ", fragment)))

    def find(pattern: str, flags=re.S) -> str:
        m = re.search(pattern, html, flags)
        return text(m.group(1)) if m else ""

    info: dict[str, str] = {}
    for key, val in re.findall(
        r'nfl-c-player-info__key">(.*?)</div>\s*<div class="nfl-c-player-info__value">(.*?)</div>', html, re.S
    ):
        info[text(key)] = text(val)

    ld: dict = {}
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "SportsTeam" and "member" in data:
            ld = data
            break
    role = ld.get("member", {})
    person = role.get("member", {})

    player_data = find(r'nfl-c-player-header__player-data[^>]*>(.*?)</div>')
    number = re.search(r"#(\d+)", player_data)
    team_href = re.search(r'nfl-c-player-header__team[^>]*>\s*<a[^>]*href="([^"]*)"', html, re.S)
    return {
        "name": find(r'<h1 class="nfl-c-player-header__title">(.*?)</h1>') or person.get("name", ""),
        "position": find(r'nfl-c-player-header__position">(.*?)</span>') or role.get("roleName", ""),
        "number": int(number.group(1)) if number else None,
        "current_team": find(r'nfl-c-player-header__team[^>]*>(.*?)</div>') or ld.get("name", ""),
        "current_team_url": urllib.parse.urljoin(SITE, team_href.group(1)) if team_href else "",
        "roster_status": find(r'nfl-c-player-header__roster-status[^>]*>(.*?)</h3>'),
        "birth_date": person.get("birthDate", ""),
        "height": info.get("Height") or person.get("height", {}).get("value", ""),
        "weight": parse_number(info.get("Weight", "")) or person.get("weight", {}).get("value", ""),
        "college": info.get("College") or person.get("alumniOf", {}).get("alumniOf", {}).get("name", ""),
        "rookie_year": parse_number(role.get("startDate", "")),
        "experience_years": parse_number(info.get("Experience", "")),
        "age": parse_number(info.get("Age", "")),
        "hometown": info.get("Hometown", ""),
        "arms": info.get("Arms", ""),
        "hands": info.get("Hands", ""),
    }


def parse_career(parsed: PageTables) -> dict[str, dict]:
    """/players/{slug}/stats/career -> {"Passing": {"rows": [...], "totals": {...}}, ...}."""
    career: dict[str, dict] = {}
    for t in parsed.tables:
        if not t.headers or t.headers[0] not in ("YEAR", "SEASON"):
            continue
        label = t.label.lower().replace(" ", "_") or f"table{len(career) + 1}"
        career[label] = {
            "columns": t.headers,
            "rows": table_to_records(t, label),
            "totals": footer_to_record(t, label),
        }
    return career


LOG_SECTIONS = {"preseason": "preseason", "regular season": "regular_season", "post season": "post_season"}


def parse_logs(parsed: PageTables) -> dict[str, list[dict]]:
    """/players/{slug}/stats/logs/{season}/ -> {"regular_season": [...], ...} (most recent game first)."""
    logs: dict[str, list[dict]] = {}
    for t in parsed.tables:
        if not t.headers or t.headers[0] != "WK":
            continue
        key = LOG_SECTIONS.get(t.label.lower(), t.label.lower().replace(" ", "_") or "unknown")
        logs[key] = table_to_records(t)
        logs[f"{key}_columns"] = t.headers
    return logs


def load_leaderboards(season: int, out_dir: Path) -> dict[str, list[dict]]:
    boards: dict[str, list[dict]] = {}
    for category in CATEGORIES:
        path = out_dir / f"{season}_{category}.json"
        if path.exists():
            with path.open() as fh:
                boards[category] = json.load(fh)
    return boards


def scrape_player(slug: str, season: int, delay: float, boards: dict[str, list[dict]]) -> dict:
    profile_url = f"{SITE}/players/{slug}/"
    career_url = f"{SITE}/players/{slug}/stats/career"
    logs_url = f"{SITE}/players/{slug}/stats/logs/{season}/"

    print(f"Fetching profile {slug} ...")
    html = fetch_html(profile_url)
    profile = parse_profile(html)
    if not profile["name"] or profile["name"].startswith("player."):
        raise ValueError(f"{slug}: profile page has no player data (bad slug?)")
    time.sleep(delay)
    career = parse_career(fetch_tables(career_url))
    time.sleep(delay)
    logs = parse_logs(fetch_tables(logs_url))

    season_stats: dict[str, list[dict]] = {}
    season_teams: list[str] = []
    for group, data in career.items():
        hits = [r for r in data["rows"] if r.get("YEAR") == season or r.get("SEASON") == season]
        if hits:
            season_stats[group] = hits
            for r in hits:
                if r.get("TEAM") and r["TEAM"] not in season_teams:
                    season_teams.append(r["TEAM"])

    ranks: dict[str, dict] = {}
    for category, rows in boards.items():
        for r in rows:
            if r["slug"] == slug:
                ranks[category] = {"rank": r["rank"], "of": len(rows), **{k: v for k, v in r.items() if k not in ("season", "category", "rank", "player", "slug", "headshot_id")}}
                break

    return {
        "slug": slug,
        **profile,
        "season": season,
        "season_teams": season_teams,
        "season_stats": season_stats,
        "leaderboards": ranks,
        "game_logs": logs,
        "career": career,
        "sources": {"profile": profile_url, "career": career_url, "logs": logs_url},
    }


def first(records: list[dict] | None, key: str):
    """Value of ``key`` from the first record (season rows are one per team; totals come first when present)."""
    if not records:
        return None
    if len(records) == 1:
        return records[0].get(key)
    # Traded mid-season: sum counting stats, leave rates blank.
    vals = [r.get(key) for r in records if isinstance(r.get(key), (int, float))]
    return sum(vals) if vals and all(isinstance(v, int) for v in vals) else None


def summary_row(p: dict) -> dict:
    s = p["season_stats"]
    return {
        "slug": p["slug"],
        "name": p["name"],
        "position": p["position"],
        "team_2025": " / ".join(p["season_teams"]),
        "current_team": p["current_team"],
        "age": p["age"],
        "games": first(s.get("passing") or s.get("rushing") or s.get("receiving") or s.get("defense"), "G"),
        "pass_att": first(s.get("passing"), "passing.ATT"),
        "pass_cmp": first(s.get("passing"), "passing.COMP"),
        "pass_yds": first(s.get("passing"), "passing.YDS"),
        "pass_td": first(s.get("passing"), "passing.TD"),
        "pass_int": first(s.get("passing"), "passing.INT"),
        "pass_rate": first(s.get("passing"), "passing.RATE"),
        "rush_att": first(s.get("rushing"), "rushing.ATT"),
        "rush_yds": first(s.get("rushing"), "rushing.YDS"),
        "rush_td": first(s.get("rushing"), "rushing.TD"),
        "rec": first(s.get("receiving"), "receiving.REC"),
        "rec_yds": first(s.get("receiving"), "receiving.YDS"),
        "rec_td": first(s.get("receiving"), "receiving.TD"),
        "tackles": first(s.get("defense"), "defense.Total"),
        "sacks": first(s.get("defense"), "defense.SCK"),
        "forced_fumbles": first(s.get("fumbles"), "fumbles.FF"),
        "interceptions": first(s.get("defense"), "defense.INT"),
    }


# ---------------------------------------------------------------------------
# Output + driver
# ---------------------------------------------------------------------------

def write_outputs(rows: list[dict], stem: str, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"
    fieldnames: list[str] = []
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    with json_path.open("w") as fh:
        json.dump(rows, fh, indent=2)
    return csv_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, default=DEFAULT_SEASON, help=f"season year (default: {DEFAULT_SEASON})")
    parser.add_argument("--category", nargs="+", default=["all"], help="leaderboard categories: " + ", ".join(CATEGORIES) + " (default: all)")
    parser.add_argument("--max-pages", type=int, default=None, help="stop after N pages (25 players each) per category")
    parser.add_argument("--include-zeros", action="store_true", help="keep paginating past players whose sort stat is 0 (default stops there)")
    parser.add_argument("--no-leaderboards", action="store_true", help="skip the leaderboard scrape")
    parser.add_argument("--player", nargs="*", default=[], metavar="SLUG", help="player profile slugs, e.g. matthew-stafford")
    parser.add_argument("--players-file", type=Path, help="file with one player slug per line (# comments allowed)")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help=f"default: {OUT_DIR}")
    parser.add_argument("--delay", type=float, default=0.75, help="seconds to wait between requests")
    args = parser.parse_args(argv)

    failures: list[str] = []

    if not args.no_leaderboards:
        cats = list(CATEGORIES) if "all" in args.category else args.category
        bad = [c for c in cats if c not in CATEGORIES]
        if bad:
            parser.error(f"unknown category {bad}; choose from {list(CATEGORIES)}")
        for i, category in enumerate(cats):
            if i:
                time.sleep(args.delay)
            try:
                rows = scrape_leaderboard(category, args.season, args.delay, args.max_pages, args.include_zeros)
            except (RuntimeError, ValueError) as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
                failures.append(category)
                continue
            if not rows:
                print("  no rows returned")
                continue
            csv_path, json_path = write_outputs(rows, f"{args.season}_{category}", args.out_dir)
            print(f"  {len(rows)} players -> {csv_path.name}, {json_path.name}")

    slugs = list(args.player)
    if args.players_file:
        for line in args.players_file.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                slugs.append(line)
    if slugs:
        boards = load_leaderboards(args.season, args.out_dir)
        profiles: list[dict] = []
        profile_dir = args.out_dir / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        for i, slug in enumerate(slugs):
            if i:
                time.sleep(args.delay)
            try:
                p = scrape_player(slug, args.season, args.delay, boards)
            except (RuntimeError, ValueError) as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
                failures.append(slug)
                continue
            path = profile_dir / f"{slug}.json"
            with path.open("w") as fh:
                json.dump(p, fh, indent=2)
            reg = len(p["game_logs"].get("regular_season", []))
            print(f"  {p['name']} ({p['position']}, {' / '.join(p['season_teams']) or p['current_team']}): "
                  f"{len(p['career'])} career tables, {reg} regular-season games, "
                  f"ranked in {len(p['leaderboards'])} categories -> profiles/{path.name}")
            profiles.append(p)
        if len(profiles) > 1:
            csv_path, json_path = write_outputs([summary_row(p) for p in profiles], f"{args.season}_players_summary", args.out_dir)
            print(f"Summary of {len(profiles)} players -> {csv_path.name}, {json_path.name}")

    if failures:
        print(f"{len(failures)} item(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
