---
name: jobs-ingest
description: Promote Network Jobs triage batches into the local searchable corpus. Use ONLY after the user explicitly confirms a triage folder should be ingested — never auto-chain from careers-discover.
license: MIT
allowed-tools: Bash(*)
metadata:
  version: "1.1.0"
---

# Jobs Ingest

Normalize staged listings → `~/.network-jobs/corpus/` shards + `manifest.json`.

Read [SCHEMA.md](../../SCHEMA.md) first.

## Asking the user

Whenever the next step is picking from a known set, use the host's choice tool —
`AskUserQuestion` (Claude Code), `question` (opencode), `ask_user_choice`, or the
harness equivalent — instead of a prose list.

- **One question per call.** Ask it, wait for the answer, then ask the next.
  Never bundle two questions into one prompt or one tool call, even when the
  tool accepts several.
- **Recommend, and say why.** Lead with the option you would pick and a one-line
  reason.
- Put the deciding data in the option label (listing counts, department, date),
  never in surrounding prose the user has to correlate.
- Always keep a free-text / "none of these" path.
- No such tool in this host? Use a short numbered list, recommendation first,
  never more than 4 options.

## Gate

**Do not ingest without explicit user confirmation** for the triage path(s).

- Confirmation looks like: “ingest these”, “ingest matches”, “ingest all”, “promote the Slack batch”, “yes, put them in the corpus”.
- When the user has not already named the set, get that confirmation as **one question** (matches / all / department, counts in labels, matches recommended) — one decision per call, never bundled with the dir choice.
- Default ingest set is **matches** (`index/matches.json`) when present. “ingest all” uses `listings.json`. A department name slices whichever set you chose.
- Not confirmation: the user earlier saying “run careers-discover”, finishing background agents, or you planning a full pipeline.
- If unsure, list staged dirs + listing counts and ask: **one question per decision** — first which triage dir (if more than one), then which set (see below). Put the counts in the option labels.

## Workflow

1. **Select triage dir(s)** under `$DATA/triage/careers-*`
2. **Choose the ingest set** — ask **one question**, never a prose “matches/all/department” list, with counts in the labels and matches recommended:
   - Default / “ingest these” / “ingest matches” → `index/matches.json` (`.jobs`)
   - “ingest all” → `index/listings.json`
   - “ingest Product” / a department name → filter that set by `department`
   - If crawl-state said `unchanged: true`, **skip ingest** (delta empty) unless the user insists.
3. **Read** the chosen listings + `INVENTORY.md` (quote **N of M match prefs**, not the raw board)
4. **Classify** with the helper (required) — do not category-map the whole board in the model:

```bash
python3 "$SUITE/skills/jobs-ingest/helpers/classify-listings.py" \
  --input "$DATA/corpus/.work/raw.json" --out "$DATA/corpus/.work/batch.json" \
  --company "$NAME"
```

   Persist `category`, `track`, `seniority`, `senioritySignals` (`intern` / `staff+`), parsed `locations[]`, `locationBuckets` (hybrid NYC-or-Remote hits both), and `fingerprint`.
   Keep any `description` / `department` the triage rows already carry — they ride through classify, merge, and shards untouched.
   **LLM only for `needsLlm` titles** (ambiguous lead/head/player-coach). Leave high-confidence rows alone.
   Fill `companyDomain` from the graph when known. Keep `salary` / `postedAt` only if present in triage.
5. **Merge** into corpus via helper (required):
   - Write the **new/updated jobs only** to a working file (e.g. `$DATA/corpus/.work/batch.json`)
   - Read `index/pagination.json` (or INVENTORY). Pass `--pagination-complete` **only** when `complete` is true **and** `status` is neither `blocked` nor `captured`, and **never** for a user-capture run (`status: captured`) — a blocked board captured zero rows because it could not be read, and a capture is one saved page, not the board; neither is evidence that roles closed. `status: truncated` behaves like `complete: false`. `status: empty` is a clean, parsed board listing zero roles (`complete` is true, so `--expect-count 0` works). If the batch has **zero incoming rows** for that company and it already had any open corpus rows, rebuild **skips expiry** and warns — whatever the status (omitted, jq `null`, or `empty`). A filtered or region-limited URL can look empty. Pass `--force-expire` only when the board really closed every role. A company that had zero open rows can still record empty.
   - Pass `--pagination-status "$(jq -r '.status' "$TRIAGE/index/pagination.json")"` with every rebuild: the helper refuses expiry on `blocked` / `captured` even if `--pagination-complete` slipped through.
   - Always pass `--expect-count` with the full board size (`index/listings.json` length). A matches-only batch over a bigger board then **skips expiry loudly** instead of closing live roles; only `--force-expire` overrides (use when you know the board genuinely shrank).
   - Run [`helpers/rebuild-corpus.sh`](helpers/rebuild-corpus.sh) on that file:

```bash
N_LISTINGS=$(jq 'length' "$TRIAGE/index/listings.json")
STATUS=$(jq -r '.status' "$TRIAGE/index/pagination.json")
"$SUITE/skills/jobs-ingest/helpers/rebuild-corpus.sh" "$DATA/corpus/.work/batch.json" \
  --expire-company "$SLUG" --pagination-complete --expect-count "$N_LISTINGS" \
  --pagination-status "$STATUS"
# or, when truncated / incomplete / blocked / captured:
"$SUITE/skills/jobs-ingest/helpers/rebuild-corpus.sh" "$DATA/corpus/.work/batch.json" \
  --expire-company "$SLUG" --pagination-incomplete --pagination-status "$STATUS"
```

   - The helper **merges by fingerprint** with existing `corpus/jobs-all.json` (incoming wins, `firstSeen` kept), rewrites shards + manifest, and **deletes stale shard files**
   - Unseen jobs for that company are marked `closed` **only** when `--pagination-complete` is set **and** the batch covers the expected board size **and** `--pagination-status` is not `blocked`/`captured`. Never expire on incomplete crawls, user-capture runs, or filtered batches. A batch with zero incoming rows for that company and prior open roles also skips expiry unless `--force-expire`, even if `--pagination-status` is omitted or `null`.
6. **Report** incoming count, total after merge, removed stale shards; show `manifest.totalJobs` and `lastUpdated`. If rebuild set `expirySkipped`, tell the user expiry was skipped and quote `expiryReason` — do not present a zero-row ingest as a clean empty board.
7. Leave triage dirs in place (do not delete unless user asks)

## Categories

`engineering` | `product` | `design` | `data` | `ai-ml` | `sales` | `marketing` | `customer-success` | `operations` | `finance` | `people` | `legal` | `it-security` | `retail` | `other`

## Location heuristics

Buckets remain the fast path. The classifier also parses city/region onto `locations[]`.

- NYC / New York / Brooklyn / Manhattan → `nyc`
- SF / San Francisco / Bay Area / Palo Alto / Mountain View / Oakland → `sf`
- Remote / Distributed / Work from home → `remote`
- `New York, NY or Remote` → **both** `nyc` and `remote` shards
- else → `other`

Honor `preferences.onsiteLocations` when matching hybrid/onsite (explicit cities, not every office).

## Helper

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
SUITE="$(cat "$DATA/suite-root" 2>/dev/null || true)"
SUITE="${NETWORK_JOBS_SUITE:-${SUITE:-}}"
python3 "$SUITE/skills/jobs-ingest/helpers/classify-listings.py" \
  --input "$DATA/corpus/.work/raw.json" --out "$DATA/corpus/.work/batch.json" --company "$NAME"
# After classifying (and optionally LLM-fixing needsLlm rows):
N_LISTINGS=$(jq 'length' "$TRIAGE/index/listings.json")
STATUS=$(jq -r '.status' "$TRIAGE/index/pagination.json")
"$SUITE/skills/jobs-ingest/helpers/rebuild-corpus.sh" "$DATA/corpus/.work/batch.json" \
  --expire-company "$SLUG" --pagination-complete --expect-count "$N_LISTINGS" \
  --pagination-status "$STATUS"   # only if pagination.complete and status is not blocked/captured
```

The classify helper is deterministic. Only send `needsLlm` titles to the model. Rebuild merges by fingerprint, shards by category/location/seniority (hybrid jobs land in every `locationBuckets` shard), and closes unseen jobs for `--expire-company` **only** when `--pagination-complete` is set and the status is not `blocked`/`captured`. A zero-row batch with prior open roles also skips expiry unless `--force-expire`, even if status is omitted or `null`.

## Helpers

- [`helpers/classify-listings.py`](helpers/classify-listings.py) — title/department → category, track, senioritySignals, locationBuckets
- [`helpers/rebuild-corpus.sh`](helpers/rebuild-corpus.sh) — fingerprint merge + shards; expire only if pagination.complete and status is not `blocked`/`captured`; zero incoming rows with prior open roles needs `--force-expire`

## Related

- Upstream: **careers-discover**
- Downstream search: **network-jobs**
