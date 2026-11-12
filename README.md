# NBA Fantasy Pipeline

A Python pipeline that aggregates NBA player-prop projections, matches players to a FanTeam contest export, and generates constrained fantasy lineups. The supplied input snapshots allow stages 2–4 to run offline without an API key.

## Setup

Use Python 3.10 or newer. Open a terminal in this repository directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

PuLP 3.3.0 is pinned for the optimizer API used here. The optimizer requires its CBC solver to be available; the standard PuLP installation normally includes it on supported platforms.

## Input files

Keep these two files in the repository root:

| File | Purpose |
| --- | --- |
| `nba_player_props_all_with_tipoff_and_rounded.csv` | Bookmaker projections, original market lines, odds, and game metadata |
| `raw.csv` | FanTeam tournament, player IDs, names, club, lineup status, position, and price |

`Price` in the supplied FanTeam export is already expressed in millions: `4.3` means 4.3 million. Identifiers are read as text during matching.

These files are fixed input snapshots, not live feeds. Do not assume their game metadata, injury status, or contest eligibility is current or independently validated. A successful run verifies software execution rather than the accuracy of the source data.

## Run the offline pipeline

Run every command from the repository root. The numbered files are ordinary Python scripts, although some internal comments refer to notebooks or older stage numbers.

```bash
python 2_aggregate_and_bookmaker_fp.py
python 3_merge_fanteam.py
python 4_optimizer.py
```

### Stage 2: consensus and bookmaker research

The aggregator computes per-player means from observed, finite statistics. Polymarket is excluded from consensus but retained in the research output. A player appearing in multiple games is rejected rather than silently combined.

| Statistic | Weight |
| --- | ---: |
| Points | 1.0 |
| Rebounds | 1.25 |
| Assists | 1.5 |
| Three-pointers | 0.5 |
| Steals | 2.0 |
| Blocks | 2.0 |
| Turnovers | -0.5 |

A 1.5 bonus is added when at least two of the consensus points, rebounds, and assists values reach 10. This is the project's configurable scoring heuristic, including a threshold-based bonus; it is not a calibrated expected bonus or a guarantee of contest-exact scoring.

Outputs:

- `nba_player_fantasy_points.csv`: consensus statistics, fantasy points, and missing-stat indicators.
- `fp_by_book.csv`: bookmaker-level research with explicit imputation indicators. Missing bookmaker statistics may be filled with consensus values for research only; these filled values never feed back into consensus.

Statistics unavailable from every eligible source remain blank. The current fantasy-points calculation sums available statistics, so incomplete players receive partial projections. Duplicate source rows are not deduplicated before averaging.

### Stage 3: FanTeam matching

Names are normalized and matched exactly or through explicit aliases in `MANUAL_NAME_MAP`. Fuzzy suggestions are provided for review; automatic fuzzy matching is disabled by default. Unmatched projections remain blank.

Outputs:

- `merged_fantasy_players.csv`: FanTeam rows plus matched projections.
- `fanteam_match_audit.csv`: matching status for every FanTeam row.
- `fanteam_match_review.csv`: unresolved FanTeam names and suggested matches.
- `fantasy_names_not_in_raw.csv`: consensus names not assigned to a FanTeam row.

### Stage 4: lineup optimization

The script prompts for mode, budget, lineups per account, account count, and exposure fraction; it also prompts for tournament selection if multiple tournaments are present.

Example inputs for a small execution check:

```text
standard
100
2
1
1
```

This requests two distinct seven-player lineups, one account, a budget of 100, and 100% maximum player exposure. Choose the actual contest budget for real use.

Standard mode requires 1–3 players in each of the five position categories. Weird mode selects six players without position bounds. Exposure limits are rounded down using the total requested lineup count. Optimization is sequential and does not jointly optimize the entire lineup portfolio.

Outputs:

- `lineups.txt`, `exposures.txt`, and `groups.csv`.
- `lineups_final_custom_exposure_1.csv` and additional account files, only when all requested lineups are generated. Upload CSVs have no header and contain the tournament ID followed by player IDs.

The optional same-team cap defaults to disabled. The supplied export names its team column `Club`, which the current optimizer does not recognize as a team column. The optimizer also does not automatically exclude injured players based on `Lineup`. Review eligibility and contest rules before using generated entries.

## Optional live collection

Stage 1 uses The Odds API and is not needed to reproduce stages 2–4 with the supplied CSVs. It selects games for the current US Eastern date and overwrites the props snapshot if records are found.

```bash
export ODDS_API_KEY='your-own-key'
python 1_collect_player_props.py
```

Live requests consume the supplied key's quota. Never commit API keys or `.env` files. The collector selects complete Over/Under pairs and applies the existing half-point adjustment heuristic. Adjusted lines are heuristic inputs, not statistically calibrated expected values.

## Repository scope

Generated outputs are excluded through `.gitignore`; regenerate them using the scripts above. The value-ranking, bookmaker-coverage, and statistic-range report is not implemented in this baseline.

## License and data

Original project code is provided under the MIT License; third-party dependencies retain their own licenses. The MIT code license does not grant rights to third-party bookmaker or FanTeam data. Confirm permission to redistribute the supplied data snapshots before publishing them.
