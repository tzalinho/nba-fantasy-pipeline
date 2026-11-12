"""FanTeam Notebook 3: safe merge of raw.csv with consensus fantasy projections.

Install dependencies: pip install pandas unidecode rapidfuzz
Run in a directory containing raw.csv and nba_player_fantasy_points.csv.
Outputs:
  merged_fantasy_players.csv      Original FanTeam columns + Player + all projection columns
  fantasy_names_not_in_raw.csv   Consensus names not assigned to a FanTeam row
  fanteam_match_review.csv       All unresolved FanTeam rows, with fuzzy suggestions
  fanteam_match_audit.csv        Every FanTeam row's match and status

Fuzzy suggestions are NOT automatically accepted by default. Set
AUTO_ACCEPT_FUZZY = True to accept only high-confidence, unambiguous,
one-to-one candidates. You can maintain aliases in MANUAL_NAME_MAP later.
"""

from pathlib import Path
import re
import sys

import pandas as pd
from unidecode import unidecode
from rapidfuzz import fuzz, process

RAW_CSV = Path('raw.csv')
PROJECTIONS_CSV = Path('nba_player_fantasy_points.csv')
OUTPUT_CSV = Path('merged_fantasy_players.csv')
UNMATCHED_PROJECTIONS_CSV = Path('fantasy_names_not_in_raw.csv')
REVIEW_CSV = Path('fanteam_match_review.csv')
AUDIT_CSV = Path('fanteam_match_audit.csv')

# FanTeam name -> bookmaker consensus name. Add known exceptions here later.
MANUAL_NAME_MAP = {
    # 'FanTeam spelling': 'Bookmaker spelling',
}

# Conservative default: review fuzzy candidates rather than silently using them.
AUTO_ACCEPT_FUZZY = False
FUZZY_MIN_SCORE = 93
FUZZY_MIN_GAP = 8
SUGGESTIONS_PER_PLAYER = 3


def normalize_name(value):
    if pd.isna(value):
        return ''
    name = unidecode(str(value)).lower().strip()
    name = re.sub(r"[^a-z0-9]+", ' ', name)
    tokens = name.split()
    while tokens and tokens[-1] in {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}:
        tokens.pop()
    return ' '.join(tokens)


def clean_text(value):
    return '' if pd.isna(value) else str(value).strip()


def main():
    for path in (RAW_CSV, PROJECTIONS_CSV):
        if not path.is_file():
            raise FileNotFoundError(f'Missing {path.resolve()}')

    # dtype=str retains FanTeam identifiers, including possible leading zeroes.
    raw = pd.read_csv(RAW_CSV, dtype=str, encoding='utf-8-sig', keep_default_na=False)
    projections = pd.read_csv(PROJECTIONS_CSV, encoding='utf-8-sig')
    if not {'FName', 'Name'}.issubset(raw.columns):
        raise ValueError('FanTeam raw.csv must contain FName and Name columns.')
    if not {'Player', 'Fantasy Points'}.issubset(projections.columns):
        raise ValueError('Consensus CSV must contain Player and Fantasy Points columns.')
    if 'Player' in raw.columns:
        raise ValueError('raw.csv already has a Player column; inspect it before merging.')
    if raw.empty or projections.empty:
        raise ValueError('An input CSV is empty. No outputs written.')

    raw = raw.copy()
    raw['Player'] = (raw['FName'].map(clean_text) + ' ' + raw['Name'].map(clean_text)).str.strip()
    raw['_norm'] = raw['Player'].map(normalize_name)
    projections = projections.copy()
    projections['Player'] = projections['Player'].map(clean_text)
    projections['_norm'] = projections['Player'].map(normalize_name)
    projections['Fantasy Points'] = pd.to_numeric(projections['Fantasy Points'], errors='coerce')

    # Collisions are dangerous: two distinct players must not share one normalized key.
    distinct = projections.groupby('_norm')['Player'].nunique()
    collisions = distinct[(distinct > 1) | (distinct.index == '')]
    if not collisions.empty:
        raise ValueError(f'Ambiguous normalized consensus names: {collisions.index.tolist()[:15]}')
    if projections['_norm'].duplicated().any():
        raise ValueError('Duplicate player rows in consensus CSV; investigate before merging.')
    if projections['Fantasy Points'].isna().any():
        raise ValueError('Some consensus Fantasy Points are missing or nonnumeric.')

    projection_lookup = projections.set_index('_norm')
    all_keys = set(projection_lookup.index)
    projection_columns = [c for c in projections.columns if c not in {'Player', '_norm'}]
    overlapping = set(projection_columns) & set(raw.columns)
    if overlapping:
        raise ValueError(f'Conflicting columns in FanTeam raw.csv: {sorted(overlapping)}')

    manual_map = {normalize_name(k): normalize_name(v) for k, v in MANUAL_NAME_MAP.items()}
    for source, target in manual_map.items():
        if not source or target not in all_keys:
            raise ValueError(f'Invalid manual alias: {source!r} -> {target!r}')

    # Preserve row count and order, even if FanTeam repeats players across contests.
    unique_raw_keys = list(dict.fromkeys(raw['_norm']))
    choices = sorted(all_keys)
    assignments = {}
    statuses = {}
    candidates = {}
    for key in unique_raw_keys:
        if key in manual_map:
            assignments[key], statuses[key] = manual_map[key], 'manual'
        elif key and key in all_keys:
            assignments[key], statuses[key] = key, 'exact'
        else:
            statuses[key] = 'unmatched'
            candidates[key] = process.extract(key, choices, scorer=fuzz.ratio, limit=SUGGESTIONS_PER_PLAYER) if key else []

    # Never allow two distinct FanTeam names to take the same consensus projection.
    # FanTeam duplicate rows with the SAME name (e.g., multiple contests) are allowed.
    used = {v: k for k, v in assignments.items()}
    for key in unique_raw_keys:
        if statuses[key] != 'unmatched':
            continue
        suggestions = candidates.get(key, [])
        if not AUTO_ACCEPT_FUZZY or not suggestions:
            continue
        best_name, best_score, _ = suggestions[0]
        second_score = suggestions[1][1] if len(suggestions) > 1 else 0
        if (best_score >= FUZZY_MIN_SCORE and best_score - second_score >= FUZZY_MIN_GAP
                and best_name not in used):
            assignments[key], statuses[key] = best_name, 'fuzzy_auto'
            used[best_name] = key

    reverse = {}
    for key, target in assignments.items():
        if target in reverse and reverse[target] != key:
            raise ValueError(f'Two distinct FanTeam names map to one consensus player: {reverse[target]!r}, {key!r} -> {target!r}. Fix MANUAL_NAME_MAP.')
        reverse[target] = key

    output = raw.drop(columns=['_norm']).copy()
    for col in projection_columns:
        output[col] = [projection_lookup.at[assignments[key], col] if key in assignments else pd.NA
                       for key in raw['_norm']]
    # Keep unmatched projections blank: NEVER turn them into 0 FP.
    audit = pd.DataFrame({
        'FanTeam Player': raw['Player'],
        'Normalized FanTeam Name': raw['_norm'],
        'Matched Consensus Player': [projection_lookup.at[assignments[key], 'Player'] if key in assignments else ''
                                     for key in raw['_norm']],
        'Match Status': [statuses[key] for key in raw['_norm']],
    })
    review_rows = []
    for key in unique_raw_keys:
        if key in assignments:
            continue
        name = raw.loc[raw['_norm'] == key, 'Player'].iloc[0]
        suggestions = candidates.get(key, [])
        record = {'FanTeam Player': name, 'Normalized Name': key}
        for i in range(SUGGESTIONS_PER_PLAYER):
            if i < len(suggestions):
                suggestion, score, _ = suggestions[i]
                record[f'Suggestion {i+1}'] = projection_lookup.at[suggestion, 'Player']
                record[f'Score {i+1}'] = round(score, 1)
            else:
                record[f'Suggestion {i+1}'] = ''
                record[f'Score {i+1}'] = ''
        review_rows.append(record)

    unmatched_projection_keys = sorted(all_keys - set(assignments.values()))
    unmatched_projections = projections[projections['_norm'].isin(unmatched_projection_keys)][['Player']].copy()
    unmatched_projections = unmatched_projections.rename(columns={'Player': 'Unmatched Fantasy Name'})

    output.to_csv(OUTPUT_CSV, index=False)
    audit.to_csv(AUDIT_CSV, index=False)
    pd.DataFrame(review_rows, columns=['FanTeam Player', 'Normalized Name'] +
                 [v for i in range(1, SUGGESTIONS_PER_PLAYER+1) for v in (f'Suggestion {i}', f'Score {i}')]).to_csv(REVIEW_CSV, index=False)
    unmatched_projections.to_csv(UNMATCHED_PROJECTIONS_CSV, index=False)

    print(f'FanTeam rows: {len(raw)} | Matched: {sum(key in assignments for key in raw["_norm"])} | Unmatched: {sum(key not in assignments for key in raw["_norm"])}')
    print('Match statuses:', audit['Match Status'].value_counts().to_dict())
    print(f'Unmatched bookmaker players: {len(unmatched_projections)}')
    print(f'Created: {OUTPUT_CSV}, {REVIEW_CSV}, {AUDIT_CSV}, {UNMATCHED_PROJECTIONS_CSV}')
    if review_rows:
        print('Review fuzzy suggestions in fanteam_match_review.csv; they were NOT assigned automatically.' if not AUTO_ACCEPT_FUZZY else 'Review remaining unmatched names.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise
