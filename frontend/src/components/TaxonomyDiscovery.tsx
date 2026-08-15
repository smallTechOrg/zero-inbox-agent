'use client'

/**
 * Taxonomy discovery — spec/ui.md screen 26, Settings → Taxonomy.
 *
 * "Rebuild my categories from my mail." The proposal is derived from the user's
 * OWN senders: every proposed category names the senders it absorbs and their
 * thread counts, and says how many of the threads that had no category, and how
 * many low-confidence threads, it resolves. It is a **diff** (KEEP / RENAME /
 * MERGE / ADD / RETIRE), editable, and **nothing is mutated until Approve** —
 * which the button itself says.
 *
 * Endpoints (spec/api.md § Phase 9): `POST /api/taxonomy/discover` (read-only)
 * then `POST /api/taxonomy/apply`, then the offer of `POST /api/reorg`.
 */

import { useCallback, useMemo, useState } from 'react'
import {
  ApiError,
  isNeverArchiveKey,
  NEVER_ARCHIVE_NOTE,
  normaliseEvidenceSender,
  phase9Api,
  type Category,
  type DefaultAction,
  type DiscoveredCategory,
  type TaxonomyCoverage,
  type TaxonomyDiscoveryResult,
} from '@/lib/types'
import { ErrorState } from '@/components/States'
import { rememberReorgJob } from '@/components/ReorganiseCard'

const ACTION_LABELS: Record<DefaultAction, string> = {
  archive: 'Archive',
  keep: 'Keep',
  digest: 'Digest',
  needs_your_call: 'Needs your call',
}
const ACTION_OPTIONS: DefaultAction[] = ['archive', 'keep', 'digest', 'needs_your_call']

type DiffKind = 'KEEP' | 'RENAME' | 'MERGE' | 'ADD' | 'RETIRE'

const DIFF_TONE: Record<DiffKind, string> = {
  KEEP: 'border-gray-300 bg-gray-100 text-gray-700',
  RENAME: 'border-sky-400 bg-sky-50 text-sky-900',
  MERGE: 'border-violet-400 bg-violet-50 text-violet-900',
  ADD: 'border-emerald-500 bg-emerald-50 text-emerald-900',
  RETIRE: 'border-amber-500 bg-amber-50 text-amber-900',
}

/** Row state while the user edits the proposal before approving it. */
type DraftRow = DiscoveredCategory & { include: boolean; diff: DiffKind }

function classify(row: DiscoveredCategory, existing: Category[]): DiffKind {
  if (row.merge_keys && row.merge_keys.length > 0) return 'MERGE'
  const match = existing.find(c => c.key === row.key)
  if (!match) return 'ADD'
  if (match.name !== row.name) return 'RENAME'
  return 'KEEP'
}

export function coverageSentence(c: TaxonomyCoverage | null | undefined): string {
  if (!c) return 'Coverage not reported by the server for this proposal.'
  const total = c.total_threads ?? c.covered_threads + c.uncovered_threads
  const parts: string[] = [
    `Covers ${c.covered_threads.toLocaleString()} of ${total.toLocaleString()} threads`,
  ]
  if (c.no_fit_total != null && c.no_fit_resolved != null) {
    parts.push(
      `resolves ${c.no_fit_resolved.toLocaleString()} of ${c.no_fit_total.toLocaleString()} threads that had no category`,
    )
  }
  if (c.low_confidence_total != null && c.low_confidence_resolved != null) {
    parts.push(
      `resolves ${c.low_confidence_resolved.toLocaleString()} of ${c.low_confidence_total.toLocaleString()} low-confidence threads`,
    )
  }
  if (c.no_fit_total == null && c.low_confidence_total == null) {
    parts.push(
      `resolves ${c.gap_threads_resolved.toLocaleString()} of ${c.gap_threads_total.toLocaleString()} threads that had no category or too little confidence`,
    )
  }
  return `${parts.join(' · ')}.`
}

/** What is NOT covered, named rather than rounded away (ui.md screen 26). */
export function remainderSentence(c: TaxonomyCoverage | null | undefined): string | null {
  if (!c) return null
  const leftover = c.gap_threads_total - c.gap_threads_resolved
  if (c.uncovered_threads <= 0 && leftover <= 0) return null
  const bits: string[] = []
  if (c.uncovered_threads > 0)
    bits.push(`${c.uncovered_threads.toLocaleString()} threads still fall outside every category`)
  if (leftover > 0)
    bits.push(
      `${leftover.toLocaleString()} of the threads that had no category or too little confidence are still unresolved`,
    )
  return `${bits.join(' and ')} — you can add a category for them below or leave them for a human.`
}

function EvidenceList({ row }: { row: DiscoveredCategory }) {
  const senders = (row.evidence_senders ?? []).map(normaliseEvidenceSender)
  if (senders.length === 0) {
    return (
      <p data-testid="proposal-evidence-empty" className="text-[11px] font-semibold text-amber-800">
        No evidence senders were returned for this category — it cannot be approved as derived from
        your mail.
      </p>
    )
  }
  return (
    <ul data-testid="proposal-evidence" className="flex flex-wrap gap-1">
      {senders.map((s, i) => (
        <li
          key={`${s.address}-${i}`}
          data-testid="proposal-evidence-sender"
          className="rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 font-mono text-[10px] text-gray-700"
        >
          {s.address}
          {s.threadCount != null ? (
            <span className="ml-1 font-sans font-bold tabular-nums text-gray-900">
              {s.threadCount.toLocaleString()}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  )
}

export interface TaxonomyDiscoveryProps {
  /** The live taxonomy, used to render the proposal as a diff. */
  categories: Category[] | null
  /** Re-read the taxonomy after Approve mutated it. */
  onApplied?: () => void
  /** The global dry-run setting, so the re-organisation offer is honest about
   *  whether Gmail will actually be touched. */
  dryRun?: boolean
}

export function TaxonomyDiscovery({ categories, onApplied, dryRun }: TaxonomyDiscoveryProps) {
  const [discovering, setDiscovering] = useState(false)
  const [result, setResult] = useState<TaxonomyDiscoveryResult | null>(null)
  const [rows, setRows] = useState<DraftRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [approving, setApproving] = useState(false)
  const [approved, setApproved] = useState<{ reorgRecommended: boolean; count: number } | null>(null)
  const [starting, setStarting] = useState(false)
  const [startedJob, setStartedJob] = useState<string | null>(null)
  const [startError, setStartError] = useState<string | null>(null)

  const existing = useMemo(() => categories ?? [], [categories])

  const discover = useCallback(async () => {
    setDiscovering(true)
    setError(null)
    setApproved(null)
    setStartedJob(null)
    try {
      const res = await phase9Api.taxonomy.discover()
      setResult(res)
      setRows(
        (res.proposal ?? []).map(p => ({
          ...p,
          include: true,
          diff: classify(p, existing),
        })),
      )
    } catch (e) {
      setError(e)
    } finally {
      setDiscovering(false)
    }
  }, [existing])

  const patchRow = useCallback((key: string, patch: Partial<DraftRow>) => {
    setRows(prev => (prev ? prev.map(r => (r.key === key ? { ...r, ...patch } : r)) : prev))
  }, [])

  const included = rows?.filter(r => r.include) ?? []

  // Retirement is by omission from the approved payload, and the payload is
  // built from INCLUDED rows only. So `retiring` must be computed from the
  // included rows too — computing it from all rows let an existing category
  // absorbed by a row the user un-checked disappear silently while this panel
  // still claimed it was safe. Nothing may be lost that the UI did not name.
  const retiring = useMemo(() => {
    if (!rows) return []
    const named = new Set<string>()
    rows
      .filter(r => r.include)
      .forEach(r => {
        named.add(r.key)
        ;(r.merge_keys ?? []).forEach(k => named.add(k))
      })
    return existing.filter(c => !named.has(c.key))
  }, [rows, existing])

  const approve = useCallback(async () => {
    if (included.length === 0) return
    setApproving(true)
    setError(null)
    try {
      const payload: DiscoveredCategory[] = included.map(
        ({ include: _include, diff: _diff, ...rest }) => rest,
      )
      const res = await phase9Api.taxonomy.apply(payload)
      setApproved({ reorgRecommended: res.reorg_recommended !== false, count: payload.length })
      onApplied?.()
    } catch (e) {
      setError(e)
    } finally {
      setApproving(false)
    }
  }, [included, onApplied])

  const startReorg = useCallback(async () => {
    setStarting(true)
    setStartError(null)
    try {
      const { job_id } = await phase9Api.reorg.start()
      setStartedJob(job_id)
      rememberReorgJob(job_id)
    } catch (e) {
      setStartError(
        e instanceof ApiError
          ? e.code === 'reorg_in_progress' || e.code === 'run_in_progress'
            ? `${e.message} Nothing was started twice.`
            : `${e.code}: ${e.message}`
          : 'Could not start the re-organisation.',
      )
    } finally {
      setStarting(false)
    }
  }, [])

  const scopeTotal =
    result?.coverage?.total_threads ??
    (result ? result.coverage.covered_threads + result.coverage.uncovered_threads : 0)

  return (
    <section
      aria-label="Taxonomy discovery"
      data-testid="taxonomy-discovery"
      className="space-y-3 rounded-lg border border-gray-200 bg-white p-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-gray-800">Categories from your own mail</h3>
          <p className="text-[11px] text-gray-600">
            Reads who actually mails you — sender by sender — and proposes the categories your inbox
            is really made of. Nothing changes until you approve.
          </p>
        </div>
        <button
          type="button"
          data-testid="rebuild-taxonomy-btn"
          onClick={() => void discover()}
          disabled={discovering}
          title="Reads your senders and proposes a taxonomy. No mail, label or category is changed."
          className="shrink-0 rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-500 focus:outline-none disabled:opacity-50"
        >
          {discovering ? 'Reading your senders…' : 'Rebuild my categories from my mail'}
        </button>
      </div>

      {discovering ? (
        <p data-testid="discovery-loading" role="status" className="rounded border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-700">
          Reading your senders — no mail is changed. This can take a minute on a large mailbox.
        </p>
      ) : null}

      {error ? <ErrorState error={error} onRetry={() => void discover()} /> : null}

      {!discovering && !error && !rows ? (
        <p data-testid="discovery-empty" className="rounded border border-dashed border-gray-300 px-3 py-3 text-xs text-gray-500">
          You are using the categories you set up by hand. Rebuild them from your mail to see the
          categories your actual senders suggest — Apple, Google, Facebook, PayPal, BookMyShow and
          whoever else really fills your inbox.
        </p>
      ) : null}

      {rows && result ? (
        <div className="space-y-3">
          {result.partial ? (
            <p
              role="status"
              data-testid="discovery-partial"
              data-state="warn"
              className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-900"
            >
              Partial proposal — this is not the full picture.{' '}
              {result.partial_reason ?? 'The model was unavailable, so only deterministic sender signals were used.'}
            </p>
          ) : null}

          <p data-testid="discovery-coverage" className="text-xs font-semibold text-gray-900">
            {coverageSentence(result.coverage)}
          </p>
          {remainderSentence(result.coverage) ? (
            <p data-testid="discovery-remainder" className="text-[11px] text-gray-600">
              {remainderSentence(result.coverage)}
            </p>
          ) : null}

          {rows.length === 0 ? (
            <p data-testid="discovery-no-proposal" className="rounded border border-dashed border-gray-300 px-3 py-3 text-xs text-gray-500">
              The server returned no proposed categories. Your current taxonomy is unchanged.
            </p>
          ) : (
            <ul data-testid="proposal-list" className="space-y-2">
              {rows.map(row => {
                const neverArchive = isNeverArchiveKey(row.key)
                return (
                  <li
                    key={row.key}
                    data-testid="proposal-row"
                    data-diff={row.diff}
                    data-key={row.key}
                    data-included={String(row.include)}
                    className={`space-y-2 rounded-lg border p-2.5 ${row.include ? 'border-gray-200 bg-white' : 'border-gray-200 bg-gray-50 opacity-70'}`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        data-testid="proposal-diff-badge"
                        className={`rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wide uppercase ${DIFF_TONE[row.diff]}`}
                      >
                        {row.diff}
                      </span>
                      <input
                        type="text"
                        value={row.name}
                        onChange={e => patchRow(row.key, { name: e.target.value })}
                        data-testid="proposal-name-input"
                        aria-label={`Name for proposed category ${row.key}`}
                        className="min-w-0 flex-1 rounded border border-gray-300 px-1.5 py-1 text-sm font-medium text-gray-900 focus:ring-1 focus:ring-gray-900 focus:outline-none"
                      />
                      <select
                        value={row.default_action}
                        onChange={e =>
                          patchRow(row.key, { default_action: e.target.value as DefaultAction })
                        }
                        data-testid="proposal-action-select"
                        aria-label={`Default action for proposed category ${row.name}`}
                        className="shrink-0 rounded border border-gray-300 bg-white px-1.5 py-1 text-xs text-gray-800"
                      >
                        {ACTION_OPTIONS.map(a => (
                          <option key={a} value={a} disabled={neverArchive && a === 'archive'}>
                            {ACTION_LABELS[a]}
                            {neverArchive && a === 'archive' ? ' — not allowed' : ''}
                          </option>
                        ))}
                      </select>
                      <label className="flex shrink-0 items-center gap-1 text-[11px] text-gray-700">
                        <input
                          type="checkbox"
                          checked={row.include}
                          onChange={e => patchRow(row.key, { include: e.target.checked })}
                          data-testid="proposal-include"
                          className="accent-gray-900"
                        />
                        Include
                      </label>
                    </div>

                    {neverArchive ? (
                      <p
                        data-testid="proposal-never-archive-note"
                        className="text-[10px] font-medium text-amber-800"
                      >
                        {NEVER_ARCHIVE_NOTE}
                      </p>
                    ) : null}

                    <textarea
                      value={row.description}
                      onChange={e => patchRow(row.key, { description: e.target.value })}
                      rows={2}
                      data-testid="proposal-description-input"
                      aria-label={`Description for proposed category ${row.name}`}
                      className="w-full rounded border border-gray-200 px-1.5 py-1 text-[11px] text-gray-800 focus:ring-1 focus:ring-gray-900 focus:outline-none"
                    />

                    <p data-testid="proposal-rationale" className="text-[11px] italic text-gray-600">
                      {row.rationale}
                    </p>

                    <p data-testid="proposal-covered" className="text-[11px] text-gray-800">
                      Absorbs{' '}
                      <span className="font-bold tabular-nums">
                        {(row.covered_threads ?? 0).toLocaleString()}
                      </span>{' '}
                      of your threads, from:
                    </p>
                    <EvidenceList row={row} />

                    {row.merge_keys && row.merge_keys.length > 0 ? (
                      <p data-testid="proposal-merges" className="text-[11px] text-violet-900">
                        Folds in your existing {row.merge_keys.join(', ')} — their mail keeps its
                        Gmail label until the re-organisation moves it.
                      </p>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          )}

          {retiring.length > 0 ? (
            <div
              data-testid="proposal-retire-list"
              className="rounded-lg border border-amber-300 bg-amber-50 p-2.5"
            >
              <p className="text-xs font-bold text-amber-900">
                Retired — no sender in your mail supports {retiring.length === 1 ? 'it' : 'them'} any
                more
              </p>
              <p className="mt-0.5 text-[11px] text-amber-900">
                {retiring.map(c => c.name).join(', ')}. Retiring a category never deletes mail: the
                Gmail label and everything filed under it stay exactly where they are.
              </p>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-2 border-t border-gray-200 pt-2">
            <button
              type="button"
              data-testid="approve-taxonomy-btn"
              onClick={() => void approve()}
              disabled={approving || included.length === 0}
              className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-500 focus:outline-none disabled:opacity-50"
            >
              {approving
                ? 'Approving…'
                : `Approve ${included.length} categor${included.length === 1 ? 'y' : 'ies'}`}
            </button>
            <button
              type="button"
              data-testid="discard-taxonomy-btn"
              onClick={() => {
                setRows(null)
                setResult(null)
                setApproved(null)
              }}
              disabled={approving}
              className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100"
            >
              Discard
            </button>
            <span data-testid="approve-nothing-changes-note" className="text-[11px] text-gray-600">
              Nothing has changed yet — Approve is the first step that writes anything.
            </span>
          </div>
        </div>
      ) : null}

      {/* ── After Approve: the re-organisation offer (ui.md screen 26 tail) ── */}
      {approved ? (
        <div
          data-testid="reorg-offer"
          className="space-y-2 rounded-lg border border-emerald-300 bg-emerald-50 p-3"
        >
          <p className="text-xs font-bold text-emerald-900">
            Approved — {approved.count} categor{approved.count === 1 ? 'y is' : 'ies are'} now yours.
          </p>
          <p data-testid="reorg-offer-scope" className="text-[11px] text-emerald-900">
            Your existing mail is still filed under the old categories. Re-organising re-files{' '}
            <span className="font-bold tabular-nums">
              {scopeTotal > 0 ? scopeTotal.toLocaleString() : 'every one of your'}
            </span>{' '}
            past decisions under the new ones — including mail that is already archived, which is
            relabelled where it sits and never pulled back into your inbox. It takes a few minutes on
            a large mailbox, nothing is deleted, and the whole thing is undone in one click.
          </p>
          {dryRun ? (
            <p data-testid="reorg-offer-dry-run" className="text-[11px] font-semibold text-amber-900">
              Dry run is ON — the job will record every move and change nothing in Gmail. Turn dry
              run off above to re-organise for real.
            </p>
          ) : null}
          {startedJob ? (
            <p data-testid="reorg-started" className="text-xs font-semibold text-emerald-900">
              Started. The progress card is on your main page and updates itself — no clicking
              needed.
            </p>
          ) : (
            <button
              type="button"
              data-testid="reorg-everything-btn"
              onClick={() => void startReorg()}
              disabled={starting}
              className="rounded-lg bg-emerald-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-800 focus:ring-2 focus:ring-emerald-500 focus:outline-none disabled:opacity-50"
            >
              {starting ? 'Starting…' : 'Re-organise everything'}
            </button>
          )}
          {startError ? (
            <p data-testid="reorg-start-error" role="alert" className="text-[11px] font-semibold text-rose-800">
              {startError}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

export default TaxonomyDiscovery
