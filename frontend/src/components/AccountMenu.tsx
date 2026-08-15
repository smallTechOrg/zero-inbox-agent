'use client'

/**
 * The account menu — spec/ui.md screen 23.
 *
 * Replaces the bare connected-address text in the console top bar. It is a
 * REAL menu, not a div that happens to open: `Escape` closes it and returns
 * focus to the trigger, arrow keys move between items, `Home`/`End` jump, and
 * a click outside dismisses. The signed-in email is announced, not a link.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { logout } from '@/lib/api'

export type AccountMenuProps = {
  email: string
  displayName?: string | null
  /** Navigates the console to Settings → Account (screen 22, slice 4). */
  onAccountSecurity: () => void
  onSettings: () => void
}

function initialOf(email: string, displayName?: string | null): string {
  const source = (displayName || email || '?').trim()
  return (source[0] ?? '?').toUpperCase()
}

export function AccountMenu({
  email,
  displayName,
  onAccountSecurity,
  onSettings,
}: AccountMenuProps) {
  const [open, setOpen] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  const [signOutError, setSignOutError] = useState<string | null>(null)
  const [activeIndex, setActiveIndex] = useState(0)

  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([])

  const close = useCallback(
    (returnFocus = true) => {
      setOpen(false)
      if (returnFocus) triggerRef.current?.focus()
    },
    [],
  )

  const doSignOut = useCallback(async () => {
    setSigningOut(true)
    setSignOutError(null)
    try {
      await logout()
    } catch {
      // A failed logout call must never strand the user in a signed-in-looking
      // console: the cookie clear is server-side, so we still navigate and let
      // the front-door gate re-evaluate the real session.
      setSignOutError('Sign-out request failed — reloading to check your session.')
    }
    // Hard navigation, so every cached component state is discarded and /app/
    // re-evaluates the session from scratch (screen 19 for a signed-out user).
    window.location.href = '/app/'
  }, [])

  const items: { key: string; label: string; onSelect: () => void; danger?: boolean }[] = [
    { key: 'account', label: 'Account & security', onSelect: () => { close(false); onAccountSecurity() } },
    { key: 'settings', label: 'Settings', onSelect: () => { close(false); onSettings() } },
    { key: 'signout', label: signingOut ? 'Signing out…' : 'Sign out', onSelect: () => void doSignOut(), danger: true },
  ]

  // Move DOM focus with the roving index whenever the menu is open.
  useEffect(() => {
    if (!open) return
    itemRefs.current[activeIndex]?.focus()
  }, [open, activeIndex])

  // Dismiss on an outside click (focus stays where the user clicked).
  useEffect(() => {
    if (!open) return
    const onDocDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', onDocDown)
    return () => document.removeEventListener('mousedown', onDocDown)
  }, [open])

  const onMenuKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      close()
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex(i => (i + 1) % items.length)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex(i => (i - 1 + items.length) % items.length)
      return
    }
    if (e.key === 'Home') {
      e.preventDefault()
      setActiveIndex(0)
      return
    }
    if (e.key === 'End') {
      e.preventDefault()
      setActiveIndex(items.length - 1)
    }
  }

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        data-testid="account-menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Account menu for ${email}`}
        onClick={() => {
          setActiveIndex(0)
          setOpen(o => !o)
        }}
        onKeyDown={e => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault()
            setActiveIndex(0)
            setOpen(true)
          }
        }}
        className="zi-focusable zi-body flex items-center gap-2 rounded-zi-r-md border border-zi-border bg-zi-bg px-2 py-1 hover:bg-zi-bg-subtle"
      >
        <span
          aria-hidden="true"
          className="zi-caption flex h-6 w-6 items-center justify-center rounded-full bg-zi-bg-inverse font-semibold text-white"
        >
          {initialOf(email, displayName)}
        </span>
        <span data-testid="connected-address" className="zi-mono max-w-[14rem] truncate">
          {email}
        </span>
        <span aria-hidden="true" className="text-zi-fg-faint">
          ▾
        </span>
      </button>

      {open ? (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Account"
          data-testid="account-menu"
          onKeyDown={onMenuKeyDown}
          className="zi-menu absolute right-0 z-50 mt-2 w-64 py-1"
        >
          <p className="zi-caption px-3 py-2 text-zi-fg-muted">
            Signed in as
            <span className="zi-mono mt-0.5 block truncate text-zi-fg">{email}</span>
          </p>
          <hr className="my-1 border-zi-border" />

          {items.map((item, i) => (
            <div key={item.key}>
              {item.key === 'signout' ? <hr className="my-1 border-zi-border" /> : null}
              <button
                ref={el => {
                  itemRefs.current[i] = el
                }}
                type="button"
                role="menuitem"
                tabIndex={activeIndex === i ? 0 : -1}
                data-active={activeIndex === i}
                data-testid={`account-menu-${item.key}`}
                disabled={item.key === 'signout' && signingOut}
                title={
                  item.key === 'signout' && signingOut
                    ? 'Signing you out — waiting for the server to revoke this session.'
                    : undefined
                }
                onFocus={() => setActiveIndex(i)}
                onClick={item.onSelect}
                className={`zi-menuitem ${item.danger ? 'text-zi-danger' : ''} disabled:cursor-not-allowed disabled:opacity-60`}
              >
                {item.key === 'signout' && signingOut ? (
                  <span className="zi-spinner mr-2 align-middle" aria-hidden="true" />
                ) : null}
                {item.label}
              </button>
            </div>
          ))}

          {signOutError ? (
            <p role="alert" className="zi-caption px-3 py-2 text-zi-danger">
              Error — {signOutError}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
