import type { Metadata } from 'next'
import './globals.css'

/**
 * The root layout mounts NOTHING that talks to the API — a signed-out visitor
 * never constructs a fetch or an EventSource. All API access lives inside the
 * signed-in branch of `app/page.tsx`.
 */

export const metadata: Metadata = {
  title: 'Zero Inbox',
  description:
    'One button cleans your Gmail inbox — every action narrated live, every run undoable with one click.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-zi-bg text-zi-fg antialiased">{children}</body>
    </html>
  )
}
