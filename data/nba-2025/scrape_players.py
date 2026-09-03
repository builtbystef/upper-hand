#!/usr/bin/env python3
"""Scrape publicly available NBA player stats from stats.nba.com.

Uses the JSON endpoint behind https://www.nba.com/stats/players/traditional
(https://stats.nba.com/stats/leaguedashplayerstats), which returns every
player in the league. By default the rows are filtered to the names listed in
``players/top10_2025.txt`` (in that order) and written to ``players/`` as CSV
and JSON. Rank columns (``PTS_RANK`` etc.) are league-wide, not within the list.

Usage examples::

    python3 scrape_players.py                         # top ten, traditional, per game
    python3 scrape_players.py --measure all           # every player table
    python3 scrape_players.py --all-players           # whole league, no filtering
    python3 scrape_players.py --player "LeBron James" --player "Stephen Curry" --label lakers-warriors
    python3 scrape_players.py --season 2024-25 --per-mode Totals

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import sys
import time
import unicodedata
from pathlib import Path

from nba_stats import PER_MODES, SEASON_TYPES, build_params, fetch_json, output_stem, parse_result_set, write_outputs

ENDPOINT = "leaguedashplayerstats"
RESULT_SET = "LeagueDashPlayerStats"
OUT_DIR = Path(__file__).resolve().parent / "players"
DEFAULT_PLAYERS_FILE = OUT_DIR / "top10_2025.txt"

# Tabs on the nba.com player stats page -> MeasureType query parameter.
MEASURE_TYPES = {
    "traditional": "Base",
    "advanced": "Advanced",
    "misc": "Misc",
    "scoring": "Scoring",
    "usage": "Usage",
    "defense": "Defense",
}


def fold(name: str) -> str:
    """Case- and accent-insensitive key so 'Jokic' matches 'Jokić'."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold().strip()


def read_players_file(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def filter_players(rows: list[dict], wanted: list[str]) -> list[dict]:
    """Keep rows for the wanted names, in the order given; warn about misses."""
    by_name = {fold(r["PLAYER_NAME"]): r for r in rows}
    selected = []
    for name in wanted:
        row = by_name.get(fold(name))
        if row is None:
            print(f"  warning: no row for '{name}' (check spelling, or the player did not play this season)", file=sys.stderr)
            continue
        selected.append(row)
    return selected


def scrape(
    season: str,
    measure_key: str,
    per_mode: str,
    season_type: str,
    out_dir: Path,
    wanted: list[str] | None,
    label: str,
) -> list[dict]:
    measure_type = MEASURE_TYPES[measure_key]
    print(f"Fetching {season} {season_type} / {measure_key} / {per_mode} ...")
    payload = fetch_json(ENDPOINT, build_params(season, measure_type, per_mode, season_type))
    rows = parse_result_set(payload, RESULT_SET)
    if not rows:
        print("  no rows returned (season may not have started or parameters are invalid)")
        return rows

    total = len(rows)
    if wanted is None:
        rows.sort(key=lambda r: r.get("PLAYER_NAME", ""))
    else:
        rows = filter_players(rows, wanted)
        if not rows:
            print("  none of the requested players matched; nothing written")
            return rows

    stem = f"{label}_{output_stem(season, measure_key, per_mode, season_type)}"
    csv_path, json_path = write_outputs(rows, stem, out_dir)
    print(f"  {len(rows)}/{total} players -> {csv_path.name}, {json_path.name}")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default="2025-26", help="season in YYYY-YY form (default: 2025-26)")
    parser.add_argument(
        "--measure",
        default="traditional",
        choices=[*MEASURE_TYPES, "all"],
        help="which stats table to pull (default: traditional); 'all' pulls every table",
    )
    parser.add_argument("--per-mode", default="PerGame", choices=PER_MODES, help="default: PerGame")
    parser.add_argument("--season-type", default="Regular Season", choices=SEASON_TYPES, help="default: Regular Season")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help=f"default: {OUT_DIR}")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds to wait between requests when pulling several tables")

    who = parser.add_mutually_exclusive_group()
    who.add_argument("--players-file", type=Path, default=DEFAULT_PLAYERS_FILE, help=f"one player name per line (default: {DEFAULT_PLAYERS_FILE})")
    who.add_argument("--player", action="append", dest="players", metavar="NAME", help="player to include; repeatable (overrides --players-file)")
    who.add_argument("--all-players", action="store_true", help="write every player in the league instead of filtering")
    parser.add_argument("--label", default=None, help="output filename prefix (default: 'top10' for the default list, 'league' for --all-players, 'selected' for --player)")
    args = parser.parse_args(argv)

    if args.all_players:
        wanted, label = None, args.label or "league"
    elif args.players:
        wanted, label = args.players, args.label or "selected"
    else:
        wanted = read_players_file(args.players_file)
        label = args.label or ("top10" if args.players_file == DEFAULT_PLAYERS_FILE else args.players_file.stem)

    measures = list(MEASURE_TYPES) if args.measure == "all" else [args.measure]
    for i, measure_key in enumerate(measures):
        if i:
            time.sleep(args.delay)
        scrape(args.season, measure_key, args.per_mode, args.season_type, args.out_dir, wanted, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
