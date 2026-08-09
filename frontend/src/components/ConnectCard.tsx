'use client'

import { AUTH_START_URL } from '@/lib/api'

export function ConnectCard() {
  return (
    <section className="mx-auto max-w-xl rounded-lg border border-gray-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg font-semibold text-gray-900">Connect Gmail</h2>
      <p className="mt-1.5 text-sm text-gray-700">
        Zero Inbox Agent reads your most recent inbox threads, categorises each one, and shows you
        what it <em>would</em> archive. Nothing is changed until you say so.
      </p>

      <div className="mt-4 rounded border border-gray-200 bg-gray-50 p-3">
        <p className="text-xs font-semibold tracking-wide text-gray-500 uppercase">
          What we ask Google for
        </p>
        <ul className="mt-1.5 list-disc space-y-1 pl-5 text-sm text-gray-700">
          <li>Read your mail headers, subjects and short snippets</li>
          <li>Archive and label mail (used only after you approve, never in Phase 1)</li>
          <li>Create Gmail filters and draft replies (later phases)</li>
        </ul>
      </div>

      <ul className="mt-4 space-y-1.5 text-sm font-medium text-emerald-900">
        <li>We never delete anything — no trash, no spam, ever.</li>
        <li>Nothing changes in your mailbox without your explicit approval.</li>
      </ul>

      <a
        href={AUTH_START_URL}
        data-testid="connect-gmail"
        className="mt-5 inline-flex items-center rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none"
      >
        Connect Gmail
      </a>
    </section>
  )
}
