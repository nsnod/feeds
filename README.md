# GemBot state

This branch is GemBot's memory between runs. It is written automatically by the scan
workflow at the end of every run: **do not edit it by hand** (the next run would
overwrite your change, or the push would conflict).

| file | what it holds |
|---|---|
| `games.json` | games and the posts that mention them (last 30 days) |
| `seen.json` | post IDs already processed (last 14 days) |
| `posted.json` | Discord messages GemBot sent and which games were alarmed / in a roundup |
| `baselines.json` | normal engagement per channel, used to spot unusually hot posts |
| `weights.json` | Gem Score weights learned from your 👍 / 👎 reactions |
| `labels.jsonl` | every 👍 / 👎 label, one per line |
| `meta.json` | run bookkeeping: last roundup, daily alarm counts, source health, Discord IDs |

To wipe GemBot's memory, delete this branch and run the "Setup GemBot" workflow again;
it recreates the branch with just this README.
