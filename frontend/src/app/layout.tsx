import type { Metadata } from 'next'
import './globals.css'
import { ActivityDrawer } from '@/components/ActivityDrawer'

export const metadata: Metadata = {
  title: 'Zero Inbox Agent',
  description: 'Clustered, explainable Gmail triage — dry run in Phase 1.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50 text-gray-900 antialiased">
        {children}
        <ActivityDrawer />
      </body>
    </html>
  )
}
