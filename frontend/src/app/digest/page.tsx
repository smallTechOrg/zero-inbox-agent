'use client'

import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'
import { ApiError, type DigestData, type DigestItem } from '@/lib/types'
import { ErrorState, SkeletonRows } from '@/components/States'

function DigestSection({
  title,
  items,
  reason_field,
  extraAction,
}: {
  title: string
  items: DigestItem[]
  reason_field: 'reason' | 'reasoning'
  extraAction?: React.ReactNode
}) {
  const [collapsed, setCollapsed] = useState(false)

  return (
    <section className="rounded-xl border border-gray-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={() => setCollapsed(c => !c)}
        className="flex w-full items-center justify-between px-4 py-3 text-left focus:ring-2 focus:ring-inset focus:ring-gray-400 focus:outline-none"
        aria-expanded={!collapsed}
      >
        <h2 className="text-sm font-bold text-gray-900">
          {title}
          <span className="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600">
            {items.length}
          </span>
        </h2>
        <span aria-hidden="true" className="text-gray-400">
          {collapsed ? '▶' : '▼'}
        </span>
      </button>

      {!collapsed && (
        <div className="border-t border-gray-100 px-4 pb-4 pt-2">
          {extraAction && <div className="mb-3">{extraAction}</div>}
          {items.length === 0 ? (
            <p className="text-xs text-gray-400">None.</p>
          ) : (
            <ul className="space-y-2">
              {items.map((item, i) => (
                <li key={i} className="flex flex-col gap-0.5 rounded-md bg-gray-50 px-3 py-2">
                  <span className="text-xs font-semibold text-gray-800">
                    {item.subject || '(no subject)'}
                  </span>
                  <span className="text-[11px] text-gray-500">{item.from}</span>
                  {(item[reason_field]) && (
                    <span className="mt-0.5 text-[11px] text-gray-600 italic">
                      {item[reason_field]}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

function AutoArchivedSection({ data }: { data: DigestData['auto_archived'] }) {
  const [collapsed, setCollapsed] = useState(false)

  return (
    <section className="rounded-xl border border-gray-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={() => setCollapsed(c => !c)}
        className="flex w-full items-center justify-between px-4 py-3 text-left focus:ring-2 focus:ring-inset focus:ring-gray-400 focus:outline-none"
        aria-expanded={!collapsed}
      >
        <h2 className="text-sm font-bold text-gray-900">
          Auto-archived
          <span className="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-600">
            {data.count}
          </span>
        </h2>
        <span aria-hidden="true" className="text-gray-400">
          {collapsed ? '▶' : '▼'}
        </span>
      </button>

      {!collapsed && (
        <div className="border-t border-gray-100 px-4 pb-4 pt-2">
          <p className="mb-2 text-xs text-gray-600 font-medium">
            {data.count} thread{data.count !== 1 ? 's' : ''} archived
          </p>
          {data.by_category.length === 0 ? (
            <p className="text-xs text-gray-400">No category breakdown available.</p>
          ) : (
            <ul className="space-y-1">
              {data.by_category.map(cat => (
                <li key={cat.name} className="flex items-center justify-between text-xs text-gray-700">
                  <span>{cat.name}</span>
                  <span className="font-semibold">{cat.count}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

export default function DigestPage() {
  const [digest, setDigest] = useState<DigestData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [notFound, setNotFound] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setNotFound(false)
    try {
      const d = await api.digestLatest()
      setDigest(d)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setNotFound(true)
      } else {
        setError(e)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-900">Catch-up Digest</h1>
          {digest && (
            <p className="mt-0.5 text-xs text-gray-500">
              Based on run {digest.run_id.slice(0, 8)}… · generated{' '}
              {new Date(digest.generated_at).toLocaleString()}
            </p>
          )}
        </div>
        <a
          href="/app/"
          className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:bg-gray-50 focus:ring-2 focus:ring-gray-400 focus:outline-none"
        >
          ← Back to Triage
        </a>
      </div>

      {loading ? (
        <SkeletonRows rows={6} label="Loading digest…" />
      ) : notFound ? (
        <div className="rounded-xl border border-gray-200 bg-white p-8 text-center">
          <p className="text-sm font-semibold text-gray-700">No digest yet</p>
          <p className="mt-1 text-xs text-gray-500">Run triage first to generate a digest.</p>
          <a
            href="/app/"
            className="mt-4 inline-block rounded-lg bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none"
          >
            Go to Triage
          </a>
        </div>
      ) : error ? (
        <ErrorState error={error} onRetry={() => void load()} />
      ) : digest ? (
        <>
          <DigestSection
            title="Time-sensitive kept"
            items={digest.time_sensitive_kept}
            reason_field="reason"
          />
          <DigestSection
            title="VIP mail"
            items={digest.vip_mail}
            reason_field="reasoning"
          />
          <DigestSection
            title="Needs your call"
            items={digest.needs_your_call}
            reason_field="reasoning"
            extraAction={
              digest.needs_your_call.length > 0 ? (
                <a
                  href="/app/"
                  className="inline-block rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-semibold text-amber-800 hover:bg-amber-100 focus:ring-2 focus:ring-amber-400 focus:outline-none"
                >
                  Go review in Triage →
                </a>
              ) : undefined
            }
          />
          <AutoArchivedSection data={digest.auto_archived} />
        </>
      ) : null}
    </div>
  )
}
