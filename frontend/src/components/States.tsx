'use client'

import type { ReactNode } from 'react'
import { ApiError } from '@/lib/types'

export function SkeletonRows({ rows = 4, label }: { rows?: number; label: string }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className="space-y-2">
      <p className="text-xs text-gray-500">{label}</p>
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-14 animate-pulse rounded-lg border border-gray-200 bg-gray-100"
          data-testid="skeleton-row"
        />
      ))}
    </div>
  )
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string
  body: string
  action?: ReactNode
}) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-8 text-center">
      <h3 className="text-base font-semibold text-gray-900">{title}</h3>
      <p className="mx-auto mt-1.5 max-w-prose text-sm text-gray-600">{body}</p>
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  )
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  const code = error instanceof ApiError ? error.code : 'unexpected_error'
  const message =
    error instanceof Error ? error.message : 'Something went wrong. Please try again.'
  const needsReauth = code === 'reauth_required' || code === 'unauthenticated'

  return (
    <div
      role="alert"
      data-testid="error-state"
      className="rounded-lg border border-red-300 bg-red-50 p-4"
    >
      <p className="text-sm font-semibold text-red-900">
        {needsReauth ? 'Your Gmail connection needs attention' : 'Something went wrong'}
      </p>
      <p className="mt-1 text-sm text-red-800">{message}</p>
      <p className="mt-1 font-mono text-xs text-red-700">code: {code}</p>
      <div className="mt-3 flex gap-2">
        {needsReauth ? (
          <a
            href="/auth/google/start"
            className="rounded bg-red-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-800 focus:ring-2 focus:ring-red-400 focus:outline-none"
          >
            Reconnect Gmail
          </a>
        ) : null}
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="rounded border border-red-300 bg-white px-3 py-1.5 text-xs font-semibold text-red-800 hover:bg-red-100 focus:ring-2 focus:ring-red-400 focus:outline-none"
          >
            Retry
          </button>
        ) : null}
      </div>
    </div>
  )
}
