'use client'

/**
 * The safety model — spec/ui.md screen 19.
 *
 * ⚠️  THESE FIVE PROMISES ARE THE COMPLETE PERMITTED SET. Every one of them
 * corresponds to a guarantee that is built and tested in this codebase. Adding
 * a sixth promise is a spec violation, because a promise the code does not keep
 * is worse than no promise at all.
 *
 * 1 → no delete/trash/spam method exists on the Gmail mutator (asserted structurally)
 * 2 → every mutation writes an undo token; per-action and per-run undo are live
 * 3 → the confidence floor, reply history, VIP and time-sensitive signals bind
 *     ahead of the never-miss reviewer
 * 4 → NEVER_ARCHIVE_KEYS, enforced on category create and update
 * 5 → only headers, subject and a redacted 200-char snippet are ever persisted
 *     (tests/integration/test_no_body_persisted.py)
 */

import { CONTENT } from '@/lib/tokens'

export const SAFETY_PROMISES: string[] = [
  'It never deletes. There is no delete, no trash, no spam-report anywhere in the system.',
  'Every change is undoable — per action or the whole run — from a snapshot taken before the change.',
  'It never archives mail it wasn’t confident about, and never mail from anyone you’ve replied to, anyone on your VIP list, or anything time-sensitive.',
  'People, Urgent and Legal mail can never be set to auto-archive. The setting does not exist.',
  'Only headers, subjects and a redacted 200-character snippet ever leave your machine. Message bodies are never stored.',
]

export function SafetyModel() {
  return (
    <section
      id="safety-model"
      aria-labelledby="zi-safety-heading"
      className="scroll-mt-16 bg-zi-bg-subtle px-6 py-12 md:py-16"
    >
      <div className={`${CONTENT} max-w-3xl`}>
        <h2 id="zi-safety-heading" className="zi-h1">
          The safety model
        </h2>
        <p className="zi-body-lg mt-4 text-zi-fg-muted">
          Five promises. Each one is a guarantee in the code, not a marketing line.
        </p>

        <ul data-testid="safety-promises" className="mt-8 space-y-4">
          {SAFETY_PROMISES.map((promise, i) => (
            <li
              key={promise}
              data-testid="safety-promise"
              className="zi-card flex gap-4 rounded-zi-r-lg p-5"
            >
              <span className="zi-mono shrink-0 pt-1 text-zi-fg-faint" aria-hidden="true">
                {String(i + 1).padStart(2, '0')}
              </span>
              <p className="zi-body-lg text-zi-fg">{promise}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
