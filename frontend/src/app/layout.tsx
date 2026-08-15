import type { Metadata } from 'next'
import './globals.css'

/**
 * The root layout deliberately mounts NOTHING that talks to the API.
 *
 * `<ActivityDrawer />` used to live here, which meant a signed-out visitor on
 * the marketing homepage mounted `useSse()`, opened `new EventSource('/api/events')`,
 * got a 401, and then sat in an exponential reconnect loop on the front door.
 * The drawer is now mounted only inside the authenticated branches of
 * `app/page.tsx`, so no `EventSource` is ever constructed for a signed-out
 * visitor — not hidden, not unmounted after the fact: never created.
 */

export const metadata: Metadata = {
  title: 'Zero Inbox Agent',
  description: 'Clustered, explainable Gmail triage — dry run in Phase 1.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-zi-bg text-zi-fg antialiased">{children}</body>
    </html>
  )
}
