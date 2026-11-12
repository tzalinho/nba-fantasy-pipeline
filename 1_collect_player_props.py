"""Shared NBA player-props collector (FanTeam + DraftKings).

Install: pip install requests
Set your API key before running:
  macOS/Linux: export ODDS_API_KEY='your_key_here'

Output: nba_player_props_all_with_tipoff_and_rounded.csv
The stat columns contain the existing +/-0.5 odds-direction heuristic.
Original market lines are retained in '<Stat> Raw Line' columns.
Fantasy scoring is intentionally deferred to notebook 2.
When a bookmaker lists alternative lines, select the complete Over/Under pair
with the most balanced implied probabilities before applying +/-0.5.
"""

import csv
import os
from collections import defaultdict
from math import isfinite
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
SPORT = "basketball_nba"
REGIONS = "us,us2,us_dfs,us_ex,eu,uk,au"
ODDS_FORMAT = "decimal"
CSV_FILENAME = "nba_player_props_all_with_tipoff_and_rounded.csv"
EASTERN = ZoneInfo("America/New_York")
API_ROOT = "https://api.the-odds-api.com/v4"
TIMEOUT_SECONDS = 30

MARKET_MAP = {
    "player_points": "Points",
    "player_rebounds": "Rebounds",
    "player_assists": "Assists",
    "player_threes": "3PT",
    "player_steals": "Steals",
    "player_blocks": "Blocks",
    "player_turnovers": "Turnovers",
}
STATS = list(MARKET_MAP.values())
FIELDNAMES = ["Game", "Tipoff (ET)", "Player", "Bookmaker"]
for stat in STATS:
    FIELDNAMES += [stat, f"{stat} Raw Line", f"{stat} Over Odds", f"{stat} Under Odds"]


def adjusted_line(raw_line, over_odds, under_odds):
    """Keep the original heuristic: favored over +0.5, favored under -0.5."""
    if raw_line in ("", None):
        return ""
    try:
        line = float(raw_line)
    except (TypeError, ValueError):
        return ""
    if over_odds in ("", None) or under_odds in ("", None):
        return line
    try:
        over, under = float(over_odds), float(under_odds)
    except (TypeError, ValueError):
        return line
    if over <= 0 or under <= 0:
        return line
    if over < under:
        return round(line + 0.5, 3)
    if under < over:
        return round(line - 0.5, 3)
    return line


def fetch_json(session, url, params):
    response = session.get(url, params=params, timeout=TIMEOUT_SECONDS)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"OddsAPI HTTP {response.status_code}: {response.text[:500]}") from exc
    return response.json(), response.headers


def collect_props():
    if not API_KEY:
        raise RuntimeError("ODDS_API_KEY is not set. Export it in your terminal before running.")

    today_et = datetime.now(EASTERN).date()
    params = {"apiKey": API_KEY, "regions": REGIONS,
              "markets": ",".join(MARKET_MAP), "oddsFormat": ODDS_FORMAT}
    records = defaultdict(lambda: {field: "" for field in FIELDNAMES})
    last_headers = {}
    failures = []
    # Each candidate is indexed by its actual raw line, preventing mismatched odds.
    candidates = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    # key -> stat -> raw line -> {"Over": odds, "Under": odds}

    with requests.Session() as session:
        events, last_headers = fetch_json(session, f"{API_ROOT}/sports/{SPORT}/events", {"apiKey": API_KEY})
        today_games = []
        for event in events:
            start = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            if start.astimezone(EASTERN).date() == today_et:
                today_games.append(event)

        print(f"NBA games scheduled for {today_et} (US Eastern): {len(today_games)}")
        for event in today_games:
            event_id = event["id"]
            game_name = f"{event['home_team']} vs {event['away_team']}"
            start_et = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00")).astimezone(EASTERN)
            tipoff = start_et.strftime("%Y-%m-%d %I:%M %p")
            try:
                odds, last_headers = fetch_json(
                    session, f"{API_ROOT}/sports/{SPORT}/events/{event_id}/odds", params
                )
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                failures.append((game_name, str(exc)))
                print(f"WARNING: Skipping {game_name}: {exc}")
                continue

            for bookmaker in odds.get("bookmakers", []):
                book_name = bookmaker.get("title", bookmaker.get("key", "Unknown"))
                for market in bookmaker.get("markets", []):
                    stat = MARKET_MAP.get(market.get("key"))
                    if stat is None:
                        continue
                    for outcome in market.get("outcomes", []):
                        side = outcome.get("name")
                        if side not in ("Over", "Under"):
                            continue
                        player = outcome.get("description", "").strip()
                        if not player:
                            continue
                        key = (game_name, tipoff, player, book_name)
                        record = records[key]
                        record.update({"Game": game_name, "Tipoff (ET)": tipoff,
                                       "Player": player, "Bookmaker": book_name})
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if point is None or price is None:
                            continue
                        try:
                            line = float(point)
                            odds_price = float(price)
                        except (TypeError, ValueError):
                            continue
                        if not (isfinite(line) and isfinite(odds_price)) or odds_price <= 1:
                            continue
                        candidates[key][stat][line][side] = odds_price

        selection_count = 0
        incomplete_count = 0
        for key, stat_candidates in candidates.items():
            record = records[key]
            for stat, line_options in stat_candidates.items():
                complete = []
                for line, sides in line_options.items():
                    if "Over" not in sides or "Under" not in sides:
                        continue
                    over, under = sides["Over"], sides["Under"]
                    # Compare no-vig implied probabilities, not raw decimal odds.
                    p_over, p_under = 1 / over, 1 / under
                    balance = abs(p_over / (p_over + p_under) - 0.5)
                    complete.append((balance, line, over, under))
                if not complete:
                    incomplete_count += 1
                    continue
                # Deterministic tie-break: prefer the smaller raw line.
                balance, line, over, under = min(complete)
                record[f"{stat} Raw Line"] = line
                record[f"{stat} Over Odds"] = over
                record[f"{stat} Under Odds"] = under
                if len(line_options) > 1:
                    selection_count += 1
                    print(
                        f"ALTERNATIVE LINES | {record['Bookmaker']} | {record['Player']} | {stat}: "
                        f"{len(line_options)} lines, {len(complete)} complete pairs; "
                        f"selected {line:g} (Over {over:g}, Under {under:g})"
                    )
        print(f"Alternative-line selections: {selection_count} | "
              f"Markets without a complete Over/Under pair: {incomplete_count}")

        for record in records.values():
            for stat in STATS:
                record[stat] = adjusted_line(record[f"{stat} Raw Line"],
                                             record[f"{stat} Over Odds"],
                                             record[f"{stat} Under Odds"])

    return records, today_games, failures, last_headers


def main():
    records, games, failures, headers = collect_props()
    if not records:
        print("No player props found; existing output file has not been overwritten.")
        return

    rows = sorted(records.values(), key=lambda r: (r["Game"], r["Player"].lower(), r["Bookmaker"]))
    with open(CSV_FILENAME, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} player/bookmaker rows to {CSV_FILENAME}")
    print(f"Games scheduled: {len(games)} | Failed game requests: {len(failures)}")
    print(f"API requests used: {headers.get('x-requests-used', 'unknown')}")
    print(f"API requests remaining: {headers.get('x-requests-remaining', 'unknown')}")
    for stat in STATS:
        available = sum(row[stat] != "" for row in rows)
        print(f"  {stat}: {available}/{len(rows)} bookmaker rows")
    if failures:
        print("WARNING: Some games were skipped. Output is incomplete; check warnings above.")


if __name__ == "__main__":
    main()
