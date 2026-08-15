# Capability: Cost-Tiered Triage

## What It Does
Decides a category, action, confidence and reasoning for every ingested thread using a four-tier
cascade — deterministic rules, then learned sender history, then a batched LLM call, then a deep read
of the full thread for borderline cases — so most threads are resolved without spending a token.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Threads | `Item[]` | thread ingestion | Yes |
| Taxonomy | `Category[]` | `categories` | Yes |
| Active rules | `Rule[]` | `rules` (status `active` or `automatic`) | Yes |
| Sender history | map | `sender_profiles` | Yes |
| Priorities profile | text | `priority_profiles` (Phase 2) | No |
| Confidence floor | float | `user_settings` | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Decisions | `Decision[]` | `decisions` |
| Tier attribution | enum per decision | `decisions.decided_by` |
| Reasoning | text per decision | `decisions.reasoning` |
| Needs-your-call queue | subset of decisions | `decisions.status = needs_your_call` |
| Token/cost records | rows | `llm_calls`, `triage_runs` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | batch classification (20–50 threads per call) | Retry 3× with backoff; on persistent failure every thread in the batch becomes `needs_your_call` with `decided_by=error` — **never archive** |
| LLM provider | single-thread deep read | Fall back to the batch verdict, capped at low confidence |
| Mailbox channel | fetch full thread + the user's past replies to that sender | Fall back to the snippet and lower the confidence |

## Business Rules
- **Tier 1 — deterministic rules.** Exact matches on sender, domain, `List-Id`, subject regex,
  attachment presence. No token cost. Sets `decided_by="rule"` and records `rule_id`.
- **Tier 1 is where a derived taxonomy pays off (Phase 9).** Discovery
  ([inbox-derived-taxonomy](inbox-derived-taxonomy.md#a6-concentration-becomes-tier-1-rules)) converts
  the user's measured sender concentration into **ordinary tier-1 rules** — `kind=deterministic`,
  `source=mined`, `status=active` — matched by this same matcher, unchanged. Phase 9 adds **no second
  classification path**: a mined rule is indistinguishable at triage time from a seed or user rule,
  lands `decided_by="rule"` with `rule_id` set, and costs zero tokens.
  This is what makes the Phase 9 target reachable: on the measured inbox the five Facebook addresses
  (~1,586 threads), the two BookMyShow addresses (~635) and `contact@jagrititheatre.com` (334) — about
  half the mail — resolve here, at confidence **well above `confidence_floor`**, so they never reach
  tier 3 to be squeezed into an ill-fitting generic category. `needs_your_call` and `below_threshold`
  are produced by asking the model a question it cannot answer well; concentration removes the
  question. **The LLM is reserved for the genuine long tail.**
- **Tier 2 — sender history.** Unresolved threads only. `ever_replied = true` → keep at high
  confidence. A bulk sender the user has never opened and has repeatedly archived → archive proposal.
  Sets `decided_by="sender_history"`.
- **Tier 3 — LLM.** Only the remainder. Threads are batched **20–50 per call**; a single call
  classifies the whole batch and returns strict JSON validated against a schema. A response that
  fails validation is retried once, then the batch degrades to `needs_your_call`.
- **Tier 4 — deep read.** Threads the LLM marks `unsure` escalate to a full-thread read plus the
  user's past replies to that sender. Capped at 25 per run; the overflow becomes `needs_your_call`.
- Every decision carries a confidence in [0, 1] and human-readable reasoning naming the evidence used.
- Confidence below `confidence_floor` → `needs_your_call`; the thread stays visible. The agent never
  archives below the floor, in any tier.
- Anything detected as time-sensitive (deadline, invoice, legal, security alert) sets
  `time_sensitive = true` and is biased heavily toward `keep`.
- Only the redacted snippet, subject and headers are sent by default; body escalation happens only in
  tier 4.
- Decisions are idempotent on `(run_id, item_id)`, so a resumed run never decides a thread twice.
- **A `decided_by="error"` row is not a decision — it is unfinished work.** From Phase 7,
  `already_decided_item_ids()` excludes rows with `decided_by="error"` or
  `review_state="review_failed"`, so resuming a run re-classifies exactly that tail rather than
  treating it as done, and `insert_provisional_decisions()` **overwrites** such a row in place instead
  of skipping it (skipping would silently discard the re-classification). Every other already-decided
  thread is still skipped, and `items_decided` is never double-counted. Inbox zero means the tail is
  finished, not abandoned — run `fbeed060` left 179 such rows stranded as un-triaged keeps. See
  [drive-to-inbox-zero](drive-to-inbox-zero.md#f-converge--the-tail-is-finished-not-abandoned).

## Success Criteria
- [ ] A run over 220 real-shaped threads produces exactly 220 decisions, each with a category,
      a confidence, non-empty reasoning and a `decided_by` value.
- [ ] The majority of the 220 are resolved by tiers 1–2, and the run's `counts.by_tier` reports it.
- [ ] LLM calls in the run number ≤ `ceil(remaining / 20)` — proving batching, not per-message calls.
- [ ] No decision with confidence below the configured floor proposes `archive`.
- [ ] A forced LLM failure results in `needs_your_call` decisions, never `archive` decisions.
- [ ] A thread whose sender the user has replied to is never proposed for archive.
- [ ] The same run resumed after interruption produces no duplicate decision rows.
- [ ] **Mined sender rules resolve at tier 1, not tier 3.** With the measured concentration fixture
      loaded (5 Facebook addresses, 2 BookMyShow addresses, Jagriti, Apple, PayPal, Twitter at their
      measured counts) and discovery's mined rules materialised, every thread from a sender with
      ≥ 10 threads ends `decided_by="rule"` with a non-null `rule_id` and confidence above
      `confidence_floor`, and **zero** of them appear in any LLM batch payload. A run that reaches the
      same categories via `decided_by="llm"` fails this criterion.
- [ ] A run containing 20 `decided_by="error"` rows, when resumed, re-classifies exactly those 20
      (none ends `decided_by="error"`), produces zero duplicate `(run_id, item_id)` pairs, and re-sends
      no already-`reviewed` thread to the LLM.
