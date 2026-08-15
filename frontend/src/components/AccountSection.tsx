'use client'

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ApiError } from '@/lib/types'
import { ErrorState, SkeletonRows } from '@/components/States'

/**
 * Screen 22 — Account & security (spec/ui.md#22-account--security).
 *
 * This slice owns no shared client: the Phase 8 account routes are declared
 * locally here, exactly as pinned in spec/api.md#routes, so this file never
 * needs to edit `lib/api.ts` (owned by another slice).
 *
 * Nothing on this screen can mutate Gmail. Disconnect revokes a token and
 * deletes a row; delete-account cascades user-scoped rows. Neither un-archives,
 * deletes or trashes a single message — and the copy says so rather than
 * letting the user assume an undo that does not exist.
 */
const ACCOUNT_URL = '/api/account'
const accountConnectionUrl = (connectionId: string) =>
  `/api/account/connections/${encodeURIComponent(connectionId)}`
const accountSessionUrl = (sessionId: string) =>
  `/api/account/sessions/${encodeURIComponent(sessionId)}`
const REVOKE_ALL_URL = '/api/account/sessions/revoke-all'
const LOGOUT_URL = '/auth/logout'
/** Mailbox consent — the `connect` intent, not the minimal-scope `signin` one. */
const CONNECT_URL = '/auth/google/start?intent=connect'
/** Where a user with no session belongs: the homepage (screen 19), not a blank console. */
const HOME_URL = '/app/'

export interface AccountUser {
  id: string
  email: string
  display_name: string | null
  created_at: string
}

export interface AccountConnection {
  id: string
  channel: string
  account_email: string
  status: string
  connected_at: string
  last_synced_at: string | null
}

export interface AccountSession {
  id: string
  created_at: string
  last_seen_at: string | null
  user_agent_summary: string | null
  current: boolean
}

export interface AccountCounts {
  decisions: number
  action_logs: number
  connections: number
}

export interface AccountPayload {
  user: AccountUser
  connections: AccountConnection[]
  sessions: AccountSession[]
  counts: AccountCounts
}

interface Envelope<T> {
  data?: T
  error?: { code: string; message: string }
}

/** Same envelope contract as `lib/api.ts`, declared locally for this slice. */
async function accountRequest<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ApiError(
      'network_error',
      'Could not reach the server — is it running on http://localhost:8001 ?',
      0,
    )
  }

  let body: Envelope<T> | null = null
  try {
    body = (await res.json()) as Envelope<T>
  } catch {
    body = null
  }

  if (!res.ok || body?.error) {
    const code = body?.error?.code ?? `http_${res.status}`
    const message =
      body?.error?.message ?? `Request to ${path} failed (${res.status}).`
    throw new ApiError(code, message, res.status)
  }
  return body?.data as T
}

function formatDate(value: string | null | undefined): string {
  if (!value) return 'never'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return 'unknown'
  return d.toLocaleString()
}

function goHome() {
  // Hard navigation: the session cookie is gone, so every cached client
  // state is stale. The homepage (screen 19) is the signed-out surface.
  window.location.assign(HOME_URL)
}

/* ------------------------------------------------------------------ modal */

function ConfirmModal({
  title,
  tone,
  confirmLabel,
  busyLabel,
  busy,
  disabledReason,
  error,
  onCancel,
  onConfirm,
  children,
  testid,
}: {
  title: string
  tone: 'danger' | 'neutral'
  confirmLabel: string
  busyLabel: string
  busy: boolean
  disabledReason?: string
  error?: unknown
  onCancel: () => void
  onConfirm: () => void
  children: ReactNode
  testid: string
}) {
  const headingId = useId()
  const panelRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null
    const panel = panelRef.current
    const focusables = () =>
      Array.from(
        panel?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      )
    focusables()[0]?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCancel()
        return
      }
      if (e.key !== 'Tab') return
      const items = focusables()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || !panel?.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      previouslyFocused?.focus?.()
    }
  }, [onCancel])

  const confirmDisabled = busy || !!disabledReason

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-gray-900/40 p-4"
      data-testid={`${testid}-backdrop`}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        data-testid={testid}
        className="w-full max-w-md rounded-lg border border-gray-200 bg-white p-4 shadow-[0_8px_24px_rgba(17,24,39,.12)]"
      >
        <h3
          id={headingId}
          className={`text-base font-semibold ${
            tone === 'danger' ? 'text-red-900' : 'text-gray-900'
          }`}
        >
          {title}
        </h3>
        <div className="mt-2 space-y-2 text-sm text-gray-700">{children}</div>
        {error ? (
          <div className="mt-3">
            <ErrorState error={error} />
          </div>
        ) : null}
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            title={busy ? 'Wait for the current request to finish.' : undefined}
            data-testid={`${testid}-cancel`}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs font-semibold text-gray-800 transition-colors duration-[120ms] hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2 focus-visible:outline-none active:bg-gray-200 disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={confirmDisabled}
            title={
              busy
                ? `${busyLabel} — request in flight.`
                : (disabledReason ?? undefined)
            }
            data-testid={`${testid}-confirm`}
            className={`rounded px-3 py-1.5 text-xs font-semibold text-white transition-colors duration-[120ms] focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2 focus-visible:outline-none disabled:opacity-60 ${
              tone === 'danger'
                ? 'bg-red-700 hover:bg-red-800 active:bg-red-900'
                : 'bg-gray-900 hover:bg-gray-700 active:bg-gray-950'
            }`}
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------- rows */

/** State is text, never colour alone — the label is the source of truth. */
function StatusChip({ status }: { status: string }) {
  const ok = status === 'connected'
  const label =
    status === 'connected'
      ? 'Connected'
      : status === 'reauth_required'
        ? 'Needs reconnect'
        : status
  return (
    <span
      data-testid="connection-status"
      className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${
        ok
          ? 'bg-emerald-50 text-emerald-800'
          : 'bg-amber-50 text-amber-800'
      }`}
    >
      {label}
    </span>
  )
}

const rowButton =
  'shrink-0 rounded border border-gray-300 bg-white px-2 py-1 text-xs font-semibold text-gray-800 transition-colors duration-[120ms] hover:bg-gray-100 focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2 focus-visible:outline-none active:bg-gray-200 disabled:opacity-60'

/* --------------------------------------------------------------- section */

export default function AccountSection() {
  const [account, setAccount] = useState<AccountPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const [disconnectTarget, setDisconnectTarget] =
    useState<AccountConnection | null>(null)
  const [disconnecting, setDisconnecting] = useState(false)
  const [disconnectError, setDisconnectError] = useState<unknown>(null)

  const [revokingId, setRevokingId] = useState<string | null>(null)
  const [sessionError, setSessionError] = useState<unknown>(null)

  const [signingOut, setSigningOut] = useState(false)
  const [revokeAllOpen, setRevokeAllOpen] = useState(false)
  const [revokingAll, setRevokingAll] = useState(false)
  const [revokeAllError, setRevokeAllError] = useState<unknown>(null)

  const [deleteOpen, setDeleteOpen] = useState(false)
  const [confirmEmail, setConfirmEmail] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<unknown>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setAccount(await accountRequest<AccountPayload>(ACCOUNT_URL))
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const disconnect = useCallback(async () => {
    if (!disconnectTarget) return
    setDisconnecting(true)
    setDisconnectError(null)
    try {
      await accountRequest(accountConnectionUrl(disconnectTarget.id), {
        method: 'DELETE',
      })
      setDisconnectTarget(null)
      await load()
    } catch (e) {
      setDisconnectError(e)
    } finally {
      setDisconnecting(false)
    }
  }, [disconnectTarget, load])

  const revokeSession = useCallback(
    async (session: AccountSession) => {
      setRevokingId(session.id)
      setSessionError(null)
      try {
        await accountRequest(accountSessionUrl(session.id), { method: 'DELETE' })
        if (session.current) {
          goHome()
          return
        }
        await load()
      } catch (e) {
        setSessionError(e)
      } finally {
        setRevokingId(null)
      }
    },
    [load],
  )

  const signOut = useCallback(async () => {
    setSigningOut(true)
    setSessionError(null)
    try {
      await accountRequest(LOGOUT_URL, { method: 'POST' })
      goHome()
    } catch (e) {
      setSessionError(e)
      setSigningOut(false)
    }
  }, [])

  const revokeAll = useCallback(async () => {
    setRevokingAll(true)
    setRevokeAllError(null)
    try {
      await accountRequest(REVOKE_ALL_URL, { method: 'POST' })
      goHome()
    } catch (e) {
      setRevokeAllError(e)
      setRevokingAll(false)
    }
  }, [])

  const deleteAccount = useCallback(async () => {
    setDeleting(true)
    setDeleteError(null)
    try {
      await accountRequest(ACCOUNT_URL, {
        method: 'DELETE',
        body: JSON.stringify({ confirm_email: confirmEmail.trim() }),
      })
      goHome()
    } catch (e) {
      setDeleteError(e)
      setDeleting(false)
    }
  }, [confirmEmail])

  if (loading) {
    return (
      <section
        aria-label="Account and security"
        data-testid="account-section"
        className="space-y-2 rounded-lg border border-gray-200 bg-white p-3"
      >
        <h3 className="text-sm font-semibold text-gray-800">Account &amp; security</h3>
        <SkeletonRows rows={3} label="Loading your account…" />
      </section>
    )
  }

  if (error || !account) {
    return (
      <section
        aria-label="Account and security"
        data-testid="account-section"
        className="space-y-2 rounded-lg border border-gray-200 bg-white p-3"
      >
        <h3 className="text-sm font-semibold text-gray-800">Account &amp; security</h3>
        <ErrorState
          error={error ?? new ApiError('not_found', 'No account data was returned.', 404)}
          onRetry={() => void load()}
        />
      </section>
    )
  }

  const { user, connections, sessions, counts } = account
  const initial = (user.display_name || user.email || '?').trim().charAt(0).toUpperCase()
  const emailMatches =
    confirmEmail.trim().toLowerCase() === user.email.trim().toLowerCase()

  return (
    <section
      aria-label="Account and security"
      data-testid="account-section"
      className="space-y-4 rounded-lg border border-gray-200 bg-white p-3"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-800">Account &amp; security</h3>
        <p className="text-[11px] text-gray-500">
          Your identity is your Google account — there is no Zero Inbox password to change.
        </p>
      </div>

      {/* ------------------------------------------------------------ You */}
      <div className="rounded border border-gray-200 p-3" data-testid="account-identity">
        <div className="flex items-center gap-3">
          <span
            aria-hidden="true"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gray-900 text-sm font-semibold text-white"
          >
            {initial}
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-gray-900">
              {user.display_name || user.email}
            </p>
            <p className="truncate font-mono text-xs text-gray-600" data-testid="account-email">
              {user.email}
            </p>
            <p className="text-[11px] text-gray-500">
              Account created {formatDate(user.created_at)}
            </p>
          </div>
        </div>
        <p className="mt-2 text-[11px] leading-tight text-gray-600">
          Two people signing in with the <em>same</em> Google account share one Zero Inbox
          account and one set of data — that is how Google sign-in works, not a bug. Two{' '}
          <em>different</em> Zero Inbox accounts can never connect the same mailbox: the second
          one is refused with{' '}
          <span className="font-mono">mailbox_already_connected</span>, so two agents can never
          act on one inbox.
        </p>
      </div>

      {/* --------------------------------------------- Connected mailboxes */}
      <div className="space-y-2" data-testid="account-connections">
        <h4 className="text-xs font-semibold tracking-wide text-gray-700 uppercase">
          Connected mailboxes
        </h4>
        {connections.length === 0 ? (
          <div className="rounded border border-dashed border-gray-300 p-3 text-center">
            <p className="text-sm font-semibold text-gray-800">No mailbox connected yet</p>
            <p className="mt-1 text-xs text-gray-600">
              Connect Gmail to let the agent triage your inbox. Read and label access only —
              it never deletes and never sends.
            </p>
            <a
              href={CONNECT_URL}
              data-testid="account-connect-cta"
              className="mt-3 inline-block rounded bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white transition-colors duration-[120ms] hover:bg-gray-700 focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2 focus-visible:outline-none active:bg-gray-950"
            >
              Connect Gmail
            </a>
          </div>
        ) : (
          <ul className="divide-y divide-gray-100 rounded border border-gray-200">
            {connections.map(c => (
              <li
                key={c.id}
                data-testid="connection-row"
                className="flex flex-col gap-2 px-2 py-2 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-medium text-gray-900">
                      {c.account_email}
                    </span>
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-gray-600 uppercase">
                      {c.channel}
                    </span>
                    <StatusChip status={c.status} />
                  </div>
                  <p className="font-mono text-[11px] text-gray-500 tabular-nums">
                    connected {formatDate(c.connected_at)} · last sync{' '}
                    {formatDate(c.last_synced_at)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  {c.status === 'reauth_required' ? (
                    <a
                      href={CONNECT_URL}
                      data-testid="connection-reconnect"
                      className={rowButton}
                    >
                      Reconnect
                    </a>
                  ) : null}
                  <button
                    type="button"
                    data-testid="connection-disconnect"
                    onClick={() => {
                      setDisconnectError(null)
                      setDisconnectTarget(c)
                    }}
                    className={rowButton}
                  >
                    Disconnect
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ---------------------------------------------- Signed-in devices */}
      <div className="space-y-2" data-testid="account-sessions">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h4 className="text-xs font-semibold tracking-wide text-gray-700 uppercase">
            Signed-in devices
          </h4>
          <p className="text-[11px] text-gray-500">
            A session you can see is a session you can revoke.
          </p>
        </div>
        {sessions.length === 0 ? (
          <p className="rounded border border-dashed border-gray-300 px-3 py-3 text-xs text-gray-500">
            No other active sessions.
          </p>
        ) : (
          <ul className="divide-y divide-gray-100 rounded border border-gray-200">
            {sessions.map(s => (
              <li
                key={s.id}
                data-testid="session-row"
                className="flex flex-col gap-2 px-2 py-2 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm text-gray-900">
                      {s.user_agent_summary || 'Unknown device'}
                    </span>
                    {s.current ? (
                      <span
                        data-testid="session-current"
                        className="rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] font-semibold text-emerald-800"
                      >
                        This device
                      </span>
                    ) : null}
                  </div>
                  <p className="font-mono text-[11px] text-gray-500 tabular-nums">
                    first seen {formatDate(s.created_at)} · last seen{' '}
                    {formatDate(s.last_seen_at)}
                  </p>
                </div>
                <button
                  type="button"
                  data-testid="session-revoke"
                  onClick={() => void revokeSession(s)}
                  disabled={revokingId !== null}
                  title={
                    revokingId === s.id
                      ? 'Signing this device out…'
                      : revokingId !== null
                        ? 'Another sign-out is in flight — wait for it to finish.'
                        : s.current
                          ? 'Signs this browser out and returns you to the homepage.'
                          : 'Signs this device out immediately.'
                  }
                  className={rowButton}
                >
                  {revokingId === s.id ? 'Signing out…' : 'Sign out'}
                </button>
              </li>
            ))}
          </ul>
        )}
        {sessionError ? <ErrorState error={sessionError} onRetry={() => void load()} /> : null}
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="account-sign-out"
            onClick={() => void signOut()}
            disabled={signingOut}
            title={
              signingOut
                ? 'Signing out…'
                : 'Signs out this browser only. Other devices stay signed in.'
            }
            className={rowButton}
          >
            {signingOut ? 'Signing out…' : 'Sign out'}
          </button>
          <button
            type="button"
            data-testid="account-sign-out-everywhere"
            onClick={() => {
              setRevokeAllError(null)
              setRevokeAllOpen(true)
            }}
            title="Revokes every session, including this one."
            className={rowButton}
          >
            Sign out everywhere
          </button>
        </div>
      </div>

      {/* ------------------------------------------------ Delete account */}
      <div
        className="space-y-2 rounded border border-red-200 bg-red-50/40 p-3"
        data-testid="account-danger-zone"
      >
        <h4 className="text-xs font-semibold tracking-wide text-red-900 uppercase">
          Delete account
        </h4>
        <p className="text-xs text-gray-700">
          Permanently deletes your account and{' '}
          <span className="font-mono tabular-nums">{counts.decisions}</span> decisions,{' '}
          <span className="font-mono tabular-nums">{counts.action_logs}</span> action logs and{' '}
          <span className="font-mono tabular-nums">{counts.connections}</span> connected
          mailboxes. Your Gmail is untouched — archived mail stays archived and labelled.
        </p>
        <button
          type="button"
          data-testid="account-delete-open"
          onClick={() => {
            setDeleteError(null)
            setConfirmEmail('')
            setDeleteOpen(true)
          }}
          title="Opens a confirmation that requires typing your email address."
          className="rounded bg-red-700 px-3 py-1.5 text-xs font-semibold text-white transition-colors duration-[120ms] hover:bg-red-800 focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2 focus-visible:outline-none active:bg-red-900"
        >
          Delete account
        </button>
      </div>

      {/* --------------------------------------------------------- modals */}
      {disconnectTarget ? (
        <ConfirmModal
          testid="disconnect-modal"
          tone="neutral"
          title={`Disconnect ${disconnectTarget.account_email}?`}
          confirmLabel="Disconnect"
          busyLabel="Disconnecting…"
          busy={disconnecting}
          error={disconnectError}
          onCancel={() => {
            if (!disconnecting) setDisconnectTarget(null)
          }}
          onConfirm={() => void disconnect()}
        >
          <p>
            We revoke our access at Google and delete the stored token. Your triage history
            stays, and nothing in Gmail changes — nothing is un-archived and nothing is deleted.
          </p>
          <p className="text-xs text-gray-600">
            Disconnecting is not an undo. To put messages back in your inbox, use Undo on the
            run that archived them, before you disconnect.
          </p>
        </ConfirmModal>
      ) : null}

      {revokeAllOpen ? (
        <ConfirmModal
          testid="revoke-all-modal"
          tone="danger"
          title="Sign out everywhere?"
          confirmLabel="Sign out everywhere"
          busyLabel="Signing out everywhere…"
          busy={revokingAll}
          error={revokeAllError}
          onCancel={() => {
            if (!revokingAll) setRevokeAllOpen(false)
          }}
          onConfirm={() => void revokeAll()}
        >
          <p>
            This revokes all{' '}
            <span className="font-mono tabular-nums">{sessions.length}</span> signed-in
            sessions, including this browser. You will be returned to the homepage and will
            need to sign in again.
          </p>
          <p className="text-xs text-gray-600">
            Your mailbox connection, settings and triage history are unaffected, and nothing in
            Gmail changes.
          </p>
        </ConfirmModal>
      ) : null}

      {deleteOpen ? (
        <ConfirmModal
          testid="delete-account-modal"
          tone="danger"
          title="Delete your Zero Inbox account?"
          confirmLabel="Delete account"
          busyLabel="Deleting…"
          busy={deleting}
          disabledReason={
            emailMatches
              ? undefined
              : `Type ${user.email} exactly to confirm this deletion.`
          }
          error={deleteError}
          onCancel={() => {
            if (!deleting) setDeleteOpen(false)
          }}
          onConfirm={() => void deleteAccount()}
        >
          <p>
            This permanently deletes your account,{' '}
            <span className="font-mono tabular-nums">{counts.decisions}</span> decisions,{' '}
            <span className="font-mono tabular-nums">{counts.action_logs}</span> action logs and{' '}
            <span className="font-mono tabular-nums">{counts.connections}</span> connected
            mailboxes. Your Gmail is untouched — archived mail stays archived and labelled.
            This cannot be undone.
          </p>
          <label className="block text-xs font-medium text-gray-700">
            Type <span className="font-mono">{user.email}</span> to confirm
            <input
              type="email"
              value={confirmEmail}
              onChange={e => setConfirmEmail(e.target.value)}
              autoComplete="off"
              data-testid="delete-account-confirm-email"
              className="mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1 text-sm text-gray-900 focus-visible:ring-2 focus-visible:ring-blue-600 focus-visible:ring-offset-2 focus-visible:outline-none"
            />
          </label>
          {!emailMatches && confirmEmail.trim().length > 0 ? (
            <p role="alert" className="text-xs font-semibold text-red-800">
              That does not match your account email — deletion stays disabled.
            </p>
          ) : null}
        </ConfirmModal>
      ) : null}
    </section>
  )
}
