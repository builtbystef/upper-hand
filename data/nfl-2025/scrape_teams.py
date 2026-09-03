#!/usr/bin/env python3
"""Scrape publicly available NFL team stats from nfl.com.

The pages under https://www.nfl.com/stats/team-stats/ are server-rendered
HTML, one table per category, at URLs shaped like::

    /stats/team-stats/{side}/{category}/{season}/reg/all

This script downloads each table, parses it with the standard library's
``html.parser`` and writes CSV + JSON into the ``teams/`` directory next to
this file. It also writes ``{season}_all.json``: one record per team with
every category's columns merged, keyed as ``"{side}.{category}.{column}"``.

Categories (19 tables):

    offense        passing, rushing, receiving, scoring, downs
    defense        passing, rushing, receiving, scoring, tackles, downs,
                   fumbles, interceptions
    special-teams  field-goals, scoring, kickoffs, kickoff-returns, punts,
                   punt-returns

Usage examples::

    python3 scrape_teams.py                          # 2025, every table
    python3 scrape_teams.py --season 2024
    python3 scrape_teams.py --category offense/passing
    python3 scrape_teams.py --category defense/tackles special-teams/punts

Notes on nfl.com quirks handled here:

* The season-type URL segment (``reg``) is ignored by the site for team
  stats; the same regular-season table is returned whatever is sent, so
  there is no ``--season-type`` option.
* An unknown category slug does not 404: the site silently serves the
  offense passing table instead. Every response is therefore checked
  against the column headers we expect for that category.
* "Lng" (longest play) cells can carry a trailing ``T`` when the play was
  a touchdown (e.g. ``64T``). The raw string is kept in ``Lng`` and the
  numeric part is stored alongside it in ``Lng_num`` (``Lng_td`` flags it).
* Field-goal distance buckets ("30-39 > A-M") hold ``attempts_made`` pairs
  such as ``13_9``; they are split into ``30-39 Att`` and ``30-39 Md``.

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
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path

BASE_URL = "https://www.nfl.com/stats/team-stats"
OUT_DIR = Path(__file__).resolve().parent / "teams"
DEFAULT_SEASON = 2025

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

# side -> {category slug: first stat column we expect}. The expected column is
# used to detect nfl.com's silent fallback to the passing table.
CATEGORIES: dict[str, dict[str, str]] = {
    "offense": {
        "passing": "Att",
        "rushing": "Att",
        "receiving": "Rec",
        "scoring": "Rsh TD",
        "downs": "3rd Att",
    },
    "defense": {
        "passing": "Att",
        "rushing": "Att",
        "receiving": "Rec",
        "scoring": "FR TD",
        "tackles": "Sck",
        "downs": "3rd Att",
        "fumbles": "FF",
        "interceptions": "INT",
    },
    "special-teams": {
        "field-goals": "FGM",
        "scoring": "FGM",
        "kickoffs": "KO",
        "kickoff-returns": "Avg",
        "punts": "Net Avg",
        "punt-returns": "Avg",
    },
}

ALL_CATEGORY_KEYS = [f"{side}/{cat}" for side, cats in CATEGORIES.items() for cat in cats]

PASSING_HEADERS = [
    "Att", "Cmp", "Cmp %", "Yds/Att", "Pass Yds", "TD", "INT", "Rate",
    "1st", "1st%", "20+", "40+", "Lng", "Sck", "SckY",
]

# nfl.com club codes -> full club name (the page only shows the nickname).
TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "AZ": "Arizona Cardinals",
    "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers", "CHI": "Chicago Bears", "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys", "DEN": "Denver Broncos",
    "DET": "Detroit Lions", "GB": "Green Bay Packers", "HOU": "Houston Texans",
    "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams", "LAR": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
    # Historical codes for older seasons.
    "OAK": "Oakland Raiders", "SD": "San Diego Chargers", "STL": "St. Louis Rams",
}


class TeamStatsTable(HTMLParser):
    """Pull the header row and body rows out of the first <table> on the page.

    Each body row yields ``(abbr, nickname, [cell, ...])``. The abbreviation is
    read from the club logo URL (``.../clubs/logos/{ABBR}``) and the nickname
    from the ``d3-o-club-fullname`` div.
    """

    LOGO_RE = re.compile(r"/clubs/logos/([A-Z]+)")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[tuple[str, str, list[str]]] = []
        self.seasons: list[int] = []
        self._in_table = False
        self._done = False
        self._in_thead = False
        self._in_th = False
        self._in_td = False
        self._text: list[str] = []
        self._abbr = ""
        self._nickname = ""
        self._in_nickname = False
        self._cells: list[str] = []
        self._in_row = False

    # -- tag handling ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "option" and a.get("value", "").startswith("/stats/team-stats/"):
            m = re.search(r"/(\d{4})/", a["value"])
            if m:
                self.seasons.append(int(m.group(1)))
        if self._done:
            return
        if tag == "table":
            self._in_table = True
        if not self._in_table:
            return
        if tag == "thead":
            self._in_thead = True
        elif tag == "th":
            self._in_th = True
            self._text = []
        elif tag == "tr" and not self._in_thead:
            self._in_row = True
            self._abbr, self._nickname, self._cells = "", "", []
        elif tag == "td" and self._in_row and a.get("scope") != "row":
            # The scope="row" cell holds the club logo/name; stat cells follow.
            self._in_td = True
            self._text = []
        elif tag == "div" and "d3-o-club-fullname" in (a.get("class") or ""):
            self._in_nickname = True
            self._text = []
        elif tag in ("img", "source") and self._in_row and not self._abbr:
            src = a.get("src") or a.get("srcset") or ""
            m = self.LOGO_RE.search(src)
            if m:
                self._abbr = m.group(1)

    def handle_endtag(self, tag: str) -> None:
        if self._done or not self._in_table:
            return
        if tag == "th" and self._in_th:
            self.headers.append(" ".join("".join(self._text).split()))
            self._in_th = False
        elif tag == "thead":
            self._in_thead = False
        elif tag == "div" and self._in_nickname:
            self._nickname = "".join(self._text).strip()
            self._in_nickname = False
        elif tag == "td" and self._in_td:
            self._cells.append("".join(self._text).strip())
            self._in_td = False
        elif tag == "tr" and self._in_row:
            if self._abbr or self._nickname:
                self.rows.append((self._abbr, self._nickname, self._cells))
            self._in_row = False
        elif tag == "table":
            self._in_table = False
            self._done = True

    def handle_data(self, data: str) -> None:
        if self._in_th or self._in_td or self._in_nickname:
            self._text.append(data)


# -- fetching ----------------------------------------------------------------

def _decode_body(resp) -> bytes:
    body = resp.read()
    encoding = resp.headers.get("Content-Encoding", "")
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    return body


def page_url(side: str, category: str, season: int) -> str:
    return f"{BASE_URL}/{side}/{category}/{season}/reg/all"


def fetch_html(url: str, retries: int = 3, timeout: int = 45) -> str:
    """GET a page with retries and exponential backoff."""
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


# -- parsing -----------------------------------------------------------------

def parse_number(value: str):
    """Return int/float for numeric strings, else the original string."""
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


LNG_RE = re.compile(r"^(-?\d+)T?$")
RANGE_RE = re.compile(r"^(.+?)\s*>\s*A-M$")  # field-goal distance buckets, e.g. "30-39 > A-M"
PAIR_RE = re.compile(r"^(\d+)_(\d+)$")  # their cells hold "attempts_made", e.g. "13_9"


def build_rows(parser: TeamStatsTable, season: int, side: str, category: str) -> list[dict]:
    """Turn parsed cells into a list of dicts, one per team."""
    stat_headers = parser.headers[1:]  # first header is "Team"
    rows: list[dict] = []
    for abbr, nickname, cells in parser.rows:
        if len(cells) != len(stat_headers):
            raise ValueError(
                f"{side}/{category}: row for {abbr or nickname} has {len(cells)} cells, "
                f"expected {len(stat_headers)} ({stat_headers})"
            )
        row: dict = {
            "season": season,
            "team": abbr,
            "team_name": TEAM_NAMES.get(abbr, nickname),
            "nickname": nickname,
        }
        for header, cell in zip(stat_headers, cells):
            bucket = RANGE_RE.match(header)
            pair = PAIR_RE.match(cell.strip())
            if bucket and pair:
                row[f"{bucket.group(1)} Att"] = int(pair.group(1))
                row[f"{bucket.group(1)} Md"] = int(pair.group(2))
                continue
            row[header] = parse_number(cell)
            if header == "Lng":
                m = LNG_RE.match(cell.strip())
                row["Lng_num"] = int(m.group(1)) if m else None
                row["Lng_td"] = cell.strip().endswith("T")
        rows.append(row)
    rows.sort(key=lambda r: r["team"])
    return rows


def validate(parser: TeamStatsTable, side: str, category: str, season: int) -> None:
    expected_first = CATEGORIES[side][category]
    if not parser.headers:
        raise ValueError(f"{side}/{category} {season}: no table found on page")
    stat_headers = parser.headers[1:]
    if not stat_headers or stat_headers[0] != expected_first:
        hint = ""
        if stat_headers == PASSING_HEADERS and category != "passing":
            hint = " (nfl.com fell back to the passing table; the category slug is probably wrong)"
        raise ValueError(
            f"{side}/{category} {season}: unexpected columns {stat_headers}{hint}"
        )
    if parser.seasons and season not in parser.seasons:
        raise ValueError(
            f"season {season} is not offered by nfl.com "
            f"(available: {min(parser.seasons)}-{max(parser.seasons)})"
        )


# -- output ------------------------------------------------------------------

def write_outputs(rows: list[dict], stem: str, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"

    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    with json_path.open("w") as fh:
        json.dump(rows, fh, indent=2)

    return csv_path, json_path


def write_combined(tables: dict[str, list[dict]], season: int, out_dir: Path) -> Path | None:
    """Merge every category into one record per team, keyed as side.category.column."""
    if not tables:
        return None
    merged: dict[str, dict] = {}
    for key, rows in tables.items():
        prefix = key.replace("/", ".")
        for row in rows:
            rec = merged.setdefault(
                row["team"],
                {"season": season, "team": row["team"], "team_name": row["team_name"], "nickname": row["nickname"]},
            )
            for col, val in row.items():
                if col not in ("season", "team", "team_name", "nickname"):
                    rec[f"{prefix}.{col}"] = val
    records = [merged[k] for k in sorted(merged)]
    path = out_dir / f"{season}_all.json"
    with path.open("w") as fh:
        json.dump(records, fh, indent=2)
    return path


# -- driver ------------------------------------------------------------------

def scrape(side: str, category: str, season: int, out_dir: Path) -> list[dict]:
    url = page_url(side, category, season)
    print(f"Fetching {season} {side}/{category} ...")
    html = fetch_html(url)
    parser = TeamStatsTable()
    parser.feed(html)
    validate(parser, side, category, season)
    rows = build_rows(parser, season, side, category)
    if not rows:
        print("  no rows returned (season may not have started)")
        return rows
    csv_path, json_path = write_outputs(rows, f"{season}_{side}_{category}", out_dir)
    print(f"  {len(rows)} teams, {len(parser.headers) - 1} columns -> {csv_path.name}, {json_path.name}")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, default=DEFAULT_SEASON, help=f"season year (default: {DEFAULT_SEASON})")
    parser.add_argument(
        "--category",
        nargs="+",
        default=["all"],
        metavar="SIDE/CATEGORY",
        help="one or more of: " + ", ".join(ALL_CATEGORY_KEYS) + " (default: all)",
    )
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help=f"default: {OUT_DIR}")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds to wait between requests")
    parser.add_argument("--no-combined", action="store_true", help="skip writing {season}_all.json")
    args = parser.parse_args(argv)

    keys = ALL_CATEGORY_KEYS if "all" in args.category else args.category
    bad = [k for k in keys if k not in ALL_CATEGORY_KEYS]
    if bad:
        parser.error(f"unknown category {bad}; choose from {ALL_CATEGORY_KEYS}")

    tables: dict[str, list[dict]] = {}
    failures: list[str] = []
    for i, key in enumerate(keys):
        if i:
            time.sleep(args.delay)
        side, category = key.split("/", 1)
        try:
            rows = scrape(side, category, args.season, args.out_dir)
        except (RuntimeError, ValueError) as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            failures.append(key)
            continue
        if rows:
            tables[key] = rows

    if not args.no_combined and len(tables) > 1:
        combined = write_combined(tables, args.season, args.out_dir)
        print(f"Combined {len(tables)} tables -> {combined.name}")

    if failures:
        print(f"{len(failures)} table(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
