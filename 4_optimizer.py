"""FanTeam Notebook 5 — lineup optimizer.

Requires: pip install pulp
Input: merged_fantasy_players.csv from FanTeam Notebook 3.
Outputs: lineups.txt, exposures.txt, groups.csv,
         lineups_final_custom_exposure_1.csv (and one file per account).

Lineup CSV format intentionally matches the original FanTeam script:
Tournament ID, PlayerID 1, ..., PlayerID 7 (or 6 for weird days), NO HEADER.

Optimization is sequential (best available lineup at each iteration), not a
joint global optimization across all lineups. No claim of global portfolio optimality.
"""

import csv
import math
import re
from collections import Counter
from pathlib import Path

from pulp import (LpBinary, LpMaximize, LpProblem, LpStatus, LpVariable,
                  PULP_CBC_CMD, lpSum, value)

INPUT = Path('merged_fantasy_players.csv')
# Optional hard cap on players from the same real NBA team. None = disabled.
MAX_PLAYERS_PER_NBA_TEAM = None  # e.g. 3
# If FanTeam file has several tournaments, explicitly choose one in the prompt.
POSITION_LIMITS = {
    'guard_point': (1, 3),
    'guard_shooting': (1, 3),
    'forward_small': (1, 3),
    'forward_power': (1, 3),
    'center': (1, 3),
}
ID_COLUMNS = ('PlayerID', 'Player Id', 'Player ID', 'ID')
TOURNAMENT_COLUMNS = ('Tournament', 'Tournament ID')
TEAM_COLUMNS = ('Team', 'TeamAbbrev', 'Team Abbrev', 'TeamName', 'Team Name')


def first_existing(columns, candidates):
    return next((c for c in candidates if c in columns), None)


def clean_number(raw):
    try:
        x = float(str(raw).strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def load_players(path=INPUT):
    if not path.is_file():
        raise FileNotFoundError(f'Missing {path}. Run FanTeam Notebook 3 first.')
    with path.open(newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        required = {'Player', 'Price', 'Position', 'Fantasy Points'}
        if not required.issubset(cols):
            raise ValueError(f'Missing columns: {sorted(required - set(cols))}')
        id_col = first_existing(cols, ID_COLUMNS)
        tournament_col = first_existing(cols, TOURNAMENT_COLUMNS)
        team_col = first_existing(cols, TEAM_COLUMNS)
        if not id_col or not tournament_col:
            raise ValueError('FanTeam export needs player ID and Tournament columns.')
        rows = list(reader)

    eligible = []
    skipped = Counter()
    for row in rows:
        name = (row.get('Player') or '').strip()
        pid = (row.get(id_col) or '').strip()
        tournament = (row.get(tournament_col) or '').strip()
        position = (row.get('Position') or '').strip().lower()
        cost = clean_number(row.get('Price'))
        fp = clean_number(row.get('Fantasy Points'))
        if not name or not pid or not tournament:
            skipped['missing player name / ID / tournament'] += 1
        elif fp is None:
            skipped['unmatched / invalid fantasy projection'] += 1
        elif cost is None or cost <= 0:
            skipped['invalid price'] += 1
        elif position not in POSITION_LIMITS:
            skipped['unrecognized position'] += 1
        else:
            eligible.append({
                'Player': name, 'ID': pid, 'Tournament': tournament,
                'Position': position, 'Cost': cost, 'Fantasy Points': fp,
                'Team': (row.get(team_col) or '').strip() if team_col else '',
            })
    print(f'Input rows: {len(rows)} | Eligible: {len(eligible)} | Skipped: {sum(skipped.values())}')
    for reason, count in skipped.items():
        print(f'  Skipped {count}: {reason}')
    if not eligible:
        raise ValueError('No eligible players with valid projections.')
    return eligible, bool(team_col)


def select_tournament(players):
    tournaments = sorted({p['Tournament'] for p in players})
    if len(tournaments) == 1:
        print(f'Tournament: {tournaments[0]}')
        chosen = tournaments[0]
    else:
        print('Multiple tournament IDs detected:')
        for idx, t in enumerate(tournaments, 1):
            print(f'  {idx}. {t} ({sum(p["Tournament"] == t for p in players)} players)')
        raw = input('Select tournament number: ').strip()
        if not raw.isdigit() or not 1 <= int(raw) <= len(tournaments):
            raise ValueError('Invalid tournament selection; no files generated.')
        chosen = tournaments[int(raw) - 1]
    result = [p for p in players if p['Tournament'] == chosen]
    # The same ID may occur multiple times in a tournament export; retain one.
    unique = {}
    for p in result:
        if p['ID'] in unique:
            previous = unique[p['ID']]
            if any(p[k] != previous[k] for k in ('Cost', 'Position', 'Fantasy Points', 'Player')):
                raise ValueError(f'Conflicting duplicate player ID: {p["ID"]}')
        else:
            unique[p['ID']] = p
    return list(unique.values()), chosen


def optimize(players, requested, budget, mode, exposure_fraction, max_same_team=None):
    slots = 7 if mode == 'standard' else 6
    if len(players) < slots:
        raise ValueError(f'Only {len(players)} eligible players; need {slots}.')
    if not 0 < exposure_fraction <= 1:
        raise ValueError('Exposure must be in (0, 1].')
    cap = math.floor(requested * exposure_fraction + 1e-9)
    if cap < 1 or cap * len(players) < requested * slots:
        raise ValueError('Exposure cap infeasible for requested lineups and eligible player pool.')
    if max_same_team is not None:
        if max_same_team < 1:
            raise ValueError('Team cap must be positive.')
        if any(not p['Team'] for p in players):
            raise ValueError('Team cap requested but some eligible players have no NBA team value.')

    problem = LpProblem('FanTeam_Optimizer', LpMaximize)
    x = {p['ID']: LpVariable(f'player_{i}', cat=LpBinary) for i, p in enumerate(players)}
    problem += lpSum(p['Fantasy Points'] * x[p['ID']] for p in players)
    problem += lpSum(x.values()) == slots, 'Roster_Size'
    problem += lpSum(p['Cost'] * x[p['ID']] for p in players) <= budget, 'Salary_Cap'
    if mode == 'standard':
        for pos, (lo, hi) in POSITION_LIMITS.items():
            subset = lpSum(x[p['ID']] for p in players if p['Position'] == pos)
            problem += subset >= lo, f'Min_{pos}'
            problem += subset <= hi, f'Max_{pos}'
    if max_same_team is not None:
        for idx, team in enumerate(sorted({p['Team'] for p in players})):
            problem += lpSum(x[p['ID']] for p in players if p['Team'] == team) <= max_same_team, f'Team_{idx}'

    appearances = Counter()
    lineups = []
    solver = PULP_CBC_CMD(msg=False)
    for i in range(requested):
        # A player at the exposure limit is unavailable for remaining lineups.
        for j, p in enumerate(players):
            if appearances[p['ID']] >= cap:
                problem += x[p['ID']] == 0, f'Exposure_{i}_{j}'
        problem.solve(solver)
        if LpStatus[problem.status] != 'Optimal':
            print(f'Only {i}/{requested} lineups possible with these constraints.')
            break
        chosen = [p for p in players if x[p['ID']].value() > 0.5]
        if len(chosen) != slots:
            raise RuntimeError('Solver returned an invalid roster size.')
        lineups.append((chosen, float(value(problem.objective))))
        appearances.update(p['ID'] for p in chosen)
        # Exclude this exact roster, irrespective of player ordering.
        problem += lpSum(x[p['ID']] for p in chosen) <= slots - 1, f'Unique_{i}'
        print(f'Lineup {i+1}/{requested}: {lineups[-1][1]:.2f} FP | {sum(p["Cost"] for p in chosen):.2f} cost')
    return lineups, cap


def write_outputs(lineups, accounts, per_account, tournament):
    # Never write misleading partial upload files.
    expected = accounts * per_account
    if len(lineups) != expected:
        for old in Path('.').glob('lineups_final_custom_exposure_*.csv'):
            old.unlink()
        print(f'WARNING: Generated {len(lineups)} of {expected}; no upload CSV files written.')

    with open('lineups.txt', 'w', encoding='utf-8') as f:
        if not lineups:
            f.write('No feasible lineups.\n')
        for i, (lineup, fp) in enumerate(lineups, 1):
            f.write(f'Lineup {i}: Total Points: {fp:.2f} Total Cost: {sum(p["Cost"] for p in lineup):.2f}\n')
            for p in lineup:
                f.write(f'  {p["Player"]} - {p["Position"]} - Cost: {p["Cost"]} - Points: {p["Fantasy Points"]}\n')
            f.write('--------\n')

    counts = Counter(p['ID'] for lineup, _ in lineups for p in lineup)
    lookup = {p['ID']: p['Player'] for lineup, _ in lineups for p in lineup}
    with open('exposures.txt', 'w', encoding='utf-8') as f:
        f.write(f'Player Exposure Report ({len(lineups)} generated lineups)\n')
        for pid, n in sorted(counts.items(), key=lambda kv: (-kv[1], lookup[kv[0]])):
            f.write(f'{lookup[pid]} [{pid}]: {n} appearances ({100*n/len(lineups):.1f}%)\n')

    with open('groups.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        roster_size = len(lineups[0][0]) if lineups else 0
        writer.writerow(['group_id'] + [f'player{i+1}' for i in range(roster_size)])
        for i, (lineup, _) in enumerate(lineups, 1):
            writer.writerow([i] + [p['Player'] for p in lineup])

    if len(lineups) == expected:
        for old in Path('.').glob('lineups_final_custom_exposure_*.csv'):
            old.unlink()
        for a in range(accounts):
            filename = f'lineups_final_custom_exposure_{a+1}.csv'
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                for lineup, _ in lineups[a*per_account:(a+1)*per_account]:
                    writer.writerow([tournament] + [p['ID'] for p in lineup])
            print(f'Created {filename}: {per_account} lineups (no header).')
    print('Created lineups.txt, exposures.txt, groups.csv')


def ask_int(prompt):
    raw = input(f'{prompt}: ').strip()
    if not raw:
        raise ValueError(f'{prompt} is required.')
    result = int(raw)
    if result <= 0:
        raise ValueError(f'{prompt} must be positive.')
    return result


def ask_float(prompt):
    raw = input(f'{prompt}: ').strip()
    if not raw:
        raise ValueError(f'{prompt} is required.')
    result = float(raw)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f'{prompt} must be a positive finite number.')
    return result


def main():
    mode = input('Standard or weird day? [standard]: ').strip().lower() or 'standard'
    if mode not in {'standard', 'weird'}:
        raise ValueError('Mode must be standard or weird.')
    budget = ask_float('Team budget')
    per_account = ask_int('Lineups per account')
    accounts = ask_int('Number of FanTeam accounts')
    exposure = ask_float('Max player exposure (0.9 = 90%)')
    if exposure > 1:
        raise ValueError('Exposure must be <= 1.')

    players, has_team_col = load_players()
    players, tournament = select_tournament(players)
    if MAX_PLAYERS_PER_NBA_TEAM is not None and not has_team_col:
        raise ValueError('NBA team cap enabled but no team column exists.')
    requested = per_account * accounts
    print(f'Optimizing {requested} lineups for tournament {tournament}; {len(players)} eligible players.')
    lineups, cap = optimize(players, requested, budget, mode, exposure, MAX_PLAYERS_PER_NBA_TEAM)
    print(f'Player exposure cap: {cap} appearances out of {requested} requested lineups.')
    write_outputs(lineups, accounts, per_account, tournament)


if __name__ == '__main__':
    main()
