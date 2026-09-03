# NBA 2025-26 data

Scraped from the public JSON API behind https://www.nba.com/stats (the page is a
JavaScript app; the data comes from `stats.nba.com/stats/leaguedash{team,player}stats`).
Standard library only, no install step. Shared HTTP/output code lives in `nba_stats.py`.

## Team stats

```sh
python3 scrape_teams.py                     # 2025-26 regular season, Traditional, per game
python3 scrape_teams.py --measure all       # Traditional, Advanced, Four Factors, Misc, Scoring, Opponent, Defense
python3 scrape_teams.py --season 2024-25
python3 scrape_teams.py --per-mode Totals
python3 scrape_teams.py --season-type Playoffs
```

Output lands in `teams/` as `<season>_<season-type>_<table>_<per-mode>.csv` and `.json`,
one row per team (30 rows), columns exactly as the API names them (`PTS`, `FG_PCT`,
`PTS_RANK`, ...).

## Player stats

```sh
python3 scrape_players.py                   # players in players/top10_2025.txt, Traditional, per game
python3 scrape_players.py --measure all     # Traditional, Advanced, Misc, Scoring, Usage, Defense
python3 scrape_players.py --all-players     # whole league (~580 rows), prefix "league_"
python3 scrape_players.py --player "LeBron James" --player "Stephen Curry" --label lal-gsw
python3 scrape_players.py --players-file my_list.txt
```

Output lands in `players/` as `<label>_<season>_<season-type>_<table>_<per-mode>.csv`
and `.json`. Rows keep the order of the name list. Name matching ignores case and
accents, so `Nikola Jokic` finds `Nikola Jokić` (full names only); unmatched names print a warning. Rank columns
are league-wide ranks, not ranks within the list.

`players/top10_2025.txt` holds the 2025 top-ten list (one name per line, `#` comments allowed).

## Notes

The endpoint drops requests that lack browser-like headers, so the scripts send the
same headers nba.com's front end does. If it starts timing out, the IP is probably
rate limited; the scripts retry with backoff, otherwise wait and rerun.
