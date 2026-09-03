#!/usr/bin/env python3
"""Scrape publicly available NBA team stats from stats.nba.com.

The page https://www.nba.com/stats/teams/traditional?Season=2025-26 is a
JavaScript app that renders data fetched from the JSON endpoint
https://stats.nba.com/stats/leaguedashteamstats. This script calls that
endpoint directly (with the browser headers it expects) and writes the
result as CSV and JSON into the ``teams/`` directory next to this file.

Usage examples::

    python3 scrape_teams.py                          # 2025-26 traditional, per game
    python3 scrape_teams.py --season 2024-25
    python3 scrape_teams.py --measure advanced --per-mode Totals
    python3 scrape_teams.py --measure all            # every table the page offers
    python3 scrape_teams.py --season-type "Playoffs"

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from nba_stats import PER_MODES, SEASON_TYPES, build_params, fetch_json, output_stem, parse_result_set, write_outputs

ENDPOINT = "leaguedashteamstats"
RESULT_SET = "LeagueDashTeamStats"
OUT_DIR = Path(__file__).resolve().parent / "teams"

# Tabs on the nba.com team stats page -> MeasureType query parameter.
MEASURE_TYPES = {
    "traditional": "Base",
    "advanced": "Advanced",
    "four-factors": "Four Factors",
    "misc": "Misc",
    "scoring": "Scoring",
    "opponent": "Opponent",
    "defense": "Defense",
}


def scrape(season: str, measure_key: str, per_mode: str, season_type: str, out_dir: Path) -> list[dict]:
    measure_type = MEASURE_TYPES[measure_key]
    print(f"Fetching {season} {season_type} / {measure_key} / {per_mode} ...")
    payload = fetch_json(ENDPOINT, build_params(season, measure_type, per_mode, season_type))
    rows = parse_result_set(payload, RESULT_SET)
    if not rows:
        print("  no rows returned (season may not have started or parameters are invalid)")
        return rows
    rows.sort(key=lambda r: r.get("TEAM_NAME", ""))
    csv_path, json_path = write_outputs(rows, output_stem(season, measure_key, per_mode, season_type), out_dir)
    print(f"  {len(rows)} teams -> {csv_path.name}, {json_path.name}")
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
    args = parser.parse_args(argv)

    measures = list(MEASURE_TYPES) if args.measure == "all" else [args.measure]
    for i, measure_key in enumerate(measures):
        if i:
            time.sleep(args.delay)
        scrape(args.season, measure_key, args.per_mode, args.season_type, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
