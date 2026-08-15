'use client'

import { useEffect, useRef, useState } from 'react'
import { api, LOGIN_URL } from '../lib/api'
import type { Me } from '../lib/types'

/**
 * Header + the ONLY error surface for token problems: the persistent
 * "Reconnect Gmail to continue" banner (spec/ui.md §1).
 */
export function Header({
  me,
  needsReconnect,
  onSignedOut,
}: {
  me: Me
  needsReconnect: boolean
  onSignedOut: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menuOpen) return
    const close = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [menuOpen])

  const signOut = async () => {
    setBusy('Signing out…')
    try {
      await api.logout()
    } catch {
      // Session may already be gone — either way, return to the front door.
    }
    onSignedOut()
  }

  const disconnect = async () => {
    if (!window.confirm('Disconnect Gmail? The agent will lose access until you reconnect.')) return
    setBusy('Disconnecting…')
    try {
      await api.disconnectGmail()
    } catch {
      // Fall through — reload state either way.
    }
    window.location.reload()
  }

  return (
    <header className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="zi-h3">Zero Inbox</p>
        <div className="relative" ref={menuRef}>
          <button
            type="button"
            className="zi-focusable flex items-center gap-2 rounded-zi-r-md border border-zi-border bg-zi-bg px-3 py-1.5"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          >
            {me.picture_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={me.picture_url} alt="" className="h-6 w-6 rounded-full" />
            ) : (
              <span
                aria-hidden
                className="flex h-6 w-6 items-center justify-center rounded-full bg-zi-bg-inverse text-xs font-semibold text-white"
              >
                {(me.name ?? me.email).charAt(0).toUpperCase()}
              </span>
            )}
            <span className="zi-body">{me.name ?? me.email}</span>
          </button>
          {menuOpen && (
            <div role="menu" className="zi-menu absolute right-0 z-10 mt-1 w-56 py-1">
              <p className="zi-caption px-3 py-1.5 text-zi-fg-faint">{me.email}</p>
              <button type="button" role="menuitem" className="zi-menuitem" onClick={signOut}>
                {busy === 'Signing out…' ? busy : 'Sign out'}
              </button>
              <button type="button" role="menuitem" className="zi-menuitem" onClick={disconnect}>
                {busy === 'Disconnecting…' ? busy : 'Disconnect Gmail'}
              </button>
            </div>
          )}
        </div>
      </div>

      {needsReconnect && (
        <div
          role="alert"
          data-testid="reconnect-banner"
          className="flex flex-wrap items-center justify-between gap-3 rounded-zi-r-md border border-zi-warn/40 bg-zi-warn-bg px-4 py-3"
        >
          <p className="zi-body text-zi-warn">
            <strong>Reconnect Gmail to continue.</strong> Google revoked or expired the
            connection — nothing was lost, and nothing runs until you reconnect.
          </p>
          <a href={LOGIN_URL} className="zi-btn zi-btn-primary no-underline">
            Reconnect Gmail
          </a>
        </div>
      )}
    </header>
  )
}
