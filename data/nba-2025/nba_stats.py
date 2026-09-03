"""Shared helpers for talking to the stats.nba.com JSON API (standard library only)."""

from __future__ import annotations

import csv
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

BASE_URL = "https://stats.nba.com/stats"

# stats.nba.com silently drops connections that do not look like they come
# from the nba.com front end, so send the same headers a browser would.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Connection": "keep-alive",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

SEASON_TYPES = ["Regular Season", "Playoffs", "Pre Season", "PlayIn", "IST"]
PER_MODES = ["PerGame", "Totals", "Per100Possessions", "Per48", "Per36", "PerMinute", "PerPossession"]


def build_params(season: str, measure_type: str, per_mode: str, season_type: str) -> dict[str, str]:
    """Full parameter set the leaguedash* endpoints require (blank keys must be present)."""
    return {
        "Conference": "",
        "DateFrom": "",
        "DateTo": "",
        "Division": "",
        "GameScope": "",
        "GameSegment": "",
        "Height": "",
        "LastNGames": "0",
        "LeagueID": "00",
        "Location": "",
        "MeasureType": measure_type,
        "Month": "0",
        "OpponentTeamID": "0",
        "Outcome": "",
        "PORound": "0",
        "PaceAdjust": "N",
        "PerMode": per_mode,
        "Period": "0",
        "PlayerExperience": "",
        "PlayerPosition": "",
        "PlusMinus": "N",
        "Rank": "N",
        "Season": season,
        "SeasonSegment": "",
        "SeasonType": season_type,
        "ShotClockRange": "",
        "StarterBench": "",
        "TeamID": "0",
        "TwoWay": "0",
        "VsConference": "",
        "VsDivision": "",
    }


def _decode_body(resp) -> bytes:
    body = resp.read()
    encoding = resp.headers.get("Content-Encoding", "")
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    return body


def fetch_json(endpoint: str, params: dict[str, str], retries: int = 3, timeout: int = 45) -> dict:
    """GET ``{BASE_URL}/{endpoint}`` with retries and exponential backoff."""
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(_decode_body(resp).decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            wait = 2**attempt
            print(f"  attempt {attempt}/{retries} failed ({exc}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}") from last_error


def parse_result_set(payload: dict, name: str) -> list[dict]:
    """Convert the API's headers + rowSet layout into a list of row dicts."""
    result_sets = payload.get("resultSets") or [payload.get("resultSet")]
    rs = next(r for r in result_sets if r and r.get("name") == name)
    headers = rs["headers"]
    return [dict(zip(headers, row)) for row in rs["rowSet"]]


def slug(text: str) -> str:
    return text.lower().replace(" ", "-")


def output_stem(season: str, measure_key: str, per_mode: str, season_type: str) -> str:
    return f"{season}_{slug(season_type)}_{measure_key}_{slug(per_mode)}"


def write_outputs(rows: list[dict], stem: str, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)

    return csv_path, json_path
