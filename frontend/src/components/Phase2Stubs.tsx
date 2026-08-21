'use client'

import { Phase2Badge, Section } from './ui'

/**
 * The three Phase-2 panels, present as designed but clearly NON-FUNCTIONAL
 * stubs (spec/ui.md §§6–8 + Stub convention). Every input is disabled with a
 * title explaining why — a stub must never be mistakable for a bug.
 */

const DISABLED_WHY = 'Coming in Phase 2 — not yet functional'

export function LedgerStub() {
  return (
    <Section title="Ledger" aside={<Phase2Badge />}>
      <p className="zi-body mb-3 text-zi-fg-muted">
        In Phase 2 you&rsquo;ll search every decision the agent ever made — by sender, subject or
        category — and see the reason, the needs-review flag and the undo state per email.
      </p>
      <label className="sr-only" htmlFor="ledger-search">
        Search the ledger
      </label>
      <input
        id="ledger-search"
        className="zi-input mb-3 max-w-md"
        placeholder="Search by sender, subject or category…"
        disabled
        title={DISABLED_WHY}
      />
      <table className="zi-body w-full text-left text-zi-fg-muted">
        <thead>
          <tr className="zi-caption border-b border-zi-border text-zi-fg-faint">
            <th className="py-2 pr-4 font-medium">Email</th>
            <th className="py-2 pr-4 font-medium">Decision</th>
            <th className="py-2 pr-4 font-medium">Reason</th>
            <th className="py-2 font-medium">Undo state</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colSpan={4} className="py-4 text-zi-fg-faint">
              No results to show yet — this table goes live in Phase 2.
            </td>
          </tr>
        </tbody>
      </table>
    </Section>
  )
}

export function CostsStub() {
  return (
    <Section title="Cost history" aside={<Phase2Badge />}>
      <p className="zi-body text-zi-fg-muted">
        The live cost ticker above is real today. In Phase 2 this panel adds per-run cost cards,
        cumulative totals across all runs, and fallback-event counts.
      </p>
    </Section>
  )
}

export function ProfilesStub() {
  return (
    <Section title="Sender profiles" aside={<Phase2Badge />}>
      <p className="zi-body mb-3 text-zi-fg-muted">
        In Phase 2 the agent learns repeat senders (&ldquo;GitHub → Notifications, 41 hits&rdquo;)
        and files them without an LLM call. You&rsquo;ll manage the learned profiles here.
      </p>
      <button type="button" className="zi-btn zi-btn-secondary" disabled title={DISABLED_WHY}>
        Add a profile
      </button>
    </Section>
  )
}
