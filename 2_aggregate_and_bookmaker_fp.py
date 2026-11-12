"""Notebook 2 — shared NBA fantasy projections + bookmaker research.

Input:  nba_player_props_all_with_tipoff_and_rounded.csv (Notebook 1)
Outputs:
  nba_player_fantasy_points.csv  — player consensus for lineup optimizers
  fp_by_book.csv                — per-bookmaker stats with transparent imputation

Only observed NON-POLYMARKET bookmaker projections contribute to consensus means. Missing per-book
statistics are filled using those means for RESEARCH ONLY. A statistic missing
from every bookmaker remains blank, not an invented zero.

Scoring below retains the original project's weights and DD bonus, with
turnovers added at -0.5. Verify scoring against each contest's current rules
before treating these as platform-exact projections.
"""

import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean

INPUT_CSV = Path('nba_player_props_all_with_tipoff_and_rounded.csv')
CONSENSUS_CSV = Path('nba_player_fantasy_points.csv')
BOOKMAKER_CSV = Path('fp_by_book.csv')

STATS = ['Points', 'Rebounds', 'Assists', '3PT', 'Steals', 'Blocks', 'Turnovers']
WEIGHTS = {
    'Points': 1.0,
    'Rebounds': 1.25,
    'Assists': 1.5,
    '3PT': 0.5,
    'Steals': 2.0,
    'Blocks': 2.0,
    'Turnovers': -0.5,
}
DD_BONUS = 1.5
DD_STATS = ('Points', 'Rebounds', 'Assists')

# Keep Polymarket in fp_by_book.csv for research, but exclude it from ALL
# consensus statistics, fantasy points, and imputation reference values.
EXCLUDED_FROM_CONSENSUS = {'polymarket'}


def is_research_only(bookmaker):
    return str(bookmaker or '').strip().casefold() in EXCLUDED_FROM_CONSENSUS


def number_or_none(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    from math import isfinite
    return number if isfinite(number) else None


def display(value):
    return '' if value is None else round(value, 2)


def calculate_fp(stats):
    """Return FP, DD flag, and incomplete-stat names.

    For compatibility with the old script, entirely unavailable stats contribute
    zero to the *partial* FP. The missingness is explicitly exposed in outputs.
    """
    missing = [stat for stat in STATS if stats.get(stat) is None]
    total = sum(stats[stat] * WEIGHTS[stat]
                for stat in STATS if stats.get(stat) is not None)
    dd = int(sum((stats.get(stat) or 0) >= 10 for stat in DD_STATS) >= 2)
    total += dd * DD_BONUS
    return round(total, 2), dd, missing


def main():
    if not INPUT_CSV.is_file():
        raise FileNotFoundError(f'Cannot find {INPUT_CSV.resolve()} — run Notebook 1 first.')

    with INPUT_CSV.open(newline='', encoding='utf-8-sig') as infile:
        reader = csv.DictReader(infile)
        required = {'Player', 'Bookmaker', *STATS}
        absent = required - set(reader.fieldnames or [])
        if absent:
            raise ValueError(f'Input CSV is missing columns: {sorted(absent)}')
        source_rows = list(reader)

    if not source_rows:
        raise ValueError('Input CSV is empty; existing outputs were not overwritten.')

    # Use game as part of the player key to avoid accidentally combining games.
    # The downstream lineup-merge expects one row per player, so detect clashes.
    player_games = defaultdict(set)
    observed = defaultdict(lambda: defaultdict(list))
    for row in source_rows:
        player = row['Player'].strip()
        if not player:
            continue
        player_games[player].add(row.get('Game', ''))
        if is_research_only(row.get('Bookmaker')):
            continue
        for stat in STATS:
            value = number_or_none(row.get(stat))
            if value is not None:
                observed[player][stat].append(value)

    clashes = {p: g for p, g in player_games.items() if len(g) > 1}
    if clashes:
        raise ValueError(
            f'{len(clashes)} player(s) appear in multiple games: '
            f'{list(clashes)[:10]}. Refusing to merge different games.'
        )

    # Never silently generate zero-valued fantasy projections for players
    # whose only source is excluded research-only data.
    eligible_players = [p for p in player_games if any(observed[p][s] for s in STATS)]
    excluded_only = sorted(set(player_games) - set(eligible_players))
    consensus = {}
    consensus_rows = []
    for player in sorted(eligible_players, key=str.casefold):
        averages = {
            stat: mean(observed[player][stat]) if observed[player][stat] else None
            for stat in STATS
        }
        consensus[player] = averages
        fp, dd, missing = calculate_fp(averages)
        consensus_rows.append({
            'Player': player,
            **{stat: display(averages[stat]) for stat in STATS},
            'dd': dd,
            'Fantasy Points': fp,
            'Missing Stats': ', '.join(missing),
            'Available Stats': len(STATS) - len(missing),
        })

    consensus_fields = ['Player', *STATS, 'dd', 'Fantasy Points',
                        'Missing Stats', 'Available Stats']
    with CONSENSUS_CSV.open('w', newline='', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=consensus_fields)
        writer.writeheader()
        writer.writerows(consensus_rows)

    # Per-bookmaker research. Retain bookmaker's observed stats and fill ONLY
    # missing ones with the player consensus; never feed these back into means.
    book_rows = []
    imputation_totals = defaultdict(int)
    for source in source_rows:
        player = source['Player'].strip()
        if not player:
            continue
        completed = {}
        imputed = []
        unavailable = []
        for stat in STATS:
            actual = number_or_none(source.get(stat))
            if actual is not None:
                completed[stat] = actual
            elif player in consensus and consensus[player][stat] is not None:
                completed[stat] = consensus[player][stat]
                imputed.append(stat)
                imputation_totals[stat] += 1
            else:
                completed[stat] = None
                unavailable.append(stat)

        fp, dd, _ = calculate_fp(completed)
        # Keep the raw market lines and odds for auditability, where present.
        output = {
            'Game': source.get('Game', ''),
            'Tipoff (ET)': source.get('Tipoff (ET)', ''),
            'Player': player,
            'Bookmaker': source.get('Bookmaker', ''),
            'Research Only': 'Yes' if is_research_only(source.get('Bookmaker')) else 'No',
            'Consensus Eligible': 'No' if is_research_only(source.get('Bookmaker')) else 'Yes',
            **{stat: display(completed[stat]) for stat in STATS},
            'dd': dd,
            'FP': fp,
            'Imputed Stats': ', '.join(imputed),
            'Imputed Count': len(imputed),
            'Unavailable Stats': ', '.join(unavailable),
            'Available Stats': len(STATS) - len(unavailable),
        }
        for stat in STATS:
            for suffix in (' Raw Line', ' Over Odds', ' Under Odds'):
                field = stat + suffix
                if field in source:
                    output[field] = source[field]
        book_rows.append(output)

    research_fields = ['Game', 'Tipoff (ET)', 'Player', 'Bookmaker',
                       'Research Only', 'Consensus Eligible', *STATS,
                       'dd', 'FP', 'Imputed Stats', 'Imputed Count',
                       'Unavailable Stats', 'Available Stats']
    research_fields += [stat + suffix for stat in STATS
                        for suffix in (' Raw Line', ' Over Odds', ' Under Odds')
                        if stat + suffix in source_rows[0]]
    with BOOKMAKER_CSV.open('w', newline='', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=research_fields)
        writer.writeheader()
        writer.writerows(book_rows)

    print(f'Saved {len(consensus_rows)} players to {CONSENSUS_CSV}')
    print(f'Saved {len(book_rows)} bookmaker rows to {BOOKMAKER_CSV}')
    excluded_rows = sum(is_research_only(r.get('Bookmaker')) for r in source_rows)
    print(f'Polymarket research-only rows excluded from consensus: {excluded_rows}')
    print(f'Players with no eligible non-Polymarket projections (omitted from consensus): {len(excluded_only)}')
    print('Observed bookmaker coverage and research-only imputations:')
    for stat in STATS:
        actual = sum(len(observed[p][stat]) for p in observed)
        print(f'  {stat}: {actual} observed; {imputation_totals[stat]} imputed')
    incomplete = sum(bool(r['Missing Stats']) for r in consensus_rows)
    print(f'Players with at least one completely unavailable stat: {incomplete}')
    print('NOTE: Polymarket FP in fp_by_book.csv is RESEARCH ONLY and may be unreliable.')
    print('NOTE: FP is partial when stats are unavailable from eligible bookmakers.')
    print('NOTE: Scoring is configurable; verify platform-specific rules.')


if __name__ == '__main__':
    main()
