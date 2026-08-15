'use client'

/** How it works — spec/ui.md screen 19. Three steps, numbered in text.
 *  Never icons-only: the numeral and the sentence both carry the meaning. */

import { CONTENT } from '@/lib/tokens'

const STEPS: { n: number; title: string; body: string }[] = [
  {
    n: 1,
    title: 'Connect Gmail',
    body: 'Read, archive and label — never delete.',
  },
  {
    n: 2,
    title: 'It classifies every thread in clusters',
    body: '“142 threads from Substack newsletters” — with the reason it decided.',
  },
  {
    n: 3,
    title: 'You watch it work live',
    body: 'And undo anything, per action or per run.',
  },
]

export function HowItWorks() {
  return (
    <section aria-labelledby="zi-how-heading" className="bg-zi-bg px-6 py-12 md:py-16">
      <div className={CONTENT}>
        <h2 id="zi-how-heading" className="zi-h1">
          How it works
        </h2>

        <ol className="mt-8 grid gap-6 md:grid-cols-3">
          {STEPS.map(step => (
            <li key={step.n} className="zi-card p-6">
              <p className="zi-caption text-zi-fg-muted">Step {step.n}</p>
              <h3 className="zi-h2 mt-2">{step.title}</h3>
              <p className="zi-body mt-2 text-zi-fg-muted">{step.body}</p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}
