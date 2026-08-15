'use client'

import type { AuditSnapshot } from '../lib/types'
import { ErrorNote, Loading } from './ui'

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-24">
      <p className="zi-h2 zi-num">{value}</p>
      <p className="zi-caption text-zi-fg-muted">{label}</p>
    </div>
  )
}

/**
 * Command strip (spec/ui.md §2): the one primary action + the latest
 * mini-audit counts. First visit auto-triggers the audit (owned by
 * Dashboard); this renders its observable progress.
 */
export function CommandStrip({
  audit,
  auditState,
  onRetryAudit,
  runActive,
  disabledReason,
  resume,
  onClean,
  starting,
}: {
  audit: AuditSnapshot | null
  auditState: 'loading' | 'ready' | 'error'
  onRetryAudit: () => void
  runActive: boolean
  /** Why the button is disabled, or null when it is clickable. */
  disabledReason: string | null
  /** True when the last run was interrupted — button reads "Resume cleaning". */
  resume: boolean
  onClean: () => void
  starting: boolean
}) {
  const label = starting ? 'Starting…' : runActive ? 'Cleaning…' : resume ? 'Resume cleaning' : 'Clean my inbox'
  const disabled = starting || runActive || disabledReason != null

  return (
    <section className="zi-card flex flex-wrap items-center justify-between gap-6 p-6">
      <div className="space-y-2">
        <button
          type="button"
          className="zi-btn zi-btn-primary zi-btn-lg"
          disabled={disabled}
          title={disabledReason ?? undefined}
          onClick={onClean}
        >
          {(starting || runActive) && <span className="zi-spinner" aria-hidden />}
          {label}
        </button>
        {disabledReason && !runActive && !starting && (
          <p className="zi-caption text-zi-fg-muted">{disabledReason}</p>
        )}
        {runActive && (
          <p className="zi-caption text-zi-fg-muted">A run is in progress — follow it live below.</p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-8">
        {auditState === 'loading' && <Loading label="Auditing your inbox…" />}
        {auditState === 'error' && (
          <ErrorNote message="Audit failed — Gmail didn't answer in time." onRetry={onRetryAudit} />
        )}
        {auditState === 'ready' && audit && (
          <>
            <Stat label="threads in INBOX" value={String(audit.total_inbox_threads)} />
            <Stat label="unread" value={String(audit.unread)} />
            <Stat
              label="oldest"
              value={audit.oldest_days > 0 ? `${audit.oldest_days}d` : '—'}
            />
            {audit.top_senders.length > 0 && (
              <div className="max-w-64">
                <p className="zi-caption mb-1 text-zi-fg-muted">busiest senders (approximate)</p>
                <p className="zi-body truncate text-zi-fg-muted">
                  {audit.top_senders
                    .slice(0, 3)
                    .map((s) => s.address)
                    .join(', ')}
                </p>
              </div>
            )}
          </>
        )}
        {auditState === 'ready' && !audit && (
          <p className="zi-body text-zi-fg-muted">
            No audit yet — it runs automatically on your first visit.
          </p>
        )}
      </div>
    </section>
  )
}
