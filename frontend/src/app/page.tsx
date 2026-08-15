'use client'

import { useCallback, useEffect, useState } from 'react'
import { Dashboard } from '../components/Dashboard'
import { SignedOut } from '../components/SignedOut'
import { Loading } from '../components/ui'
import { api, isSignedOut } from '../lib/api'
import type { Me } from '../lib/types'

/**
 * The single page (spec/ui.md): signed-out front door or the signed-in
 * dashboard — decided by one `GET /api/me`. No other routes exist.
 */
export default function Page() {
  const [state, setState] = useState<
    | { kind: 'checking' }
    | { kind: 'signed_out' }
    | { kind: 'signed_in'; me: Me }
    | { kind: 'unreachable'; message: string }
  >({ kind: 'checking' })

  const check = useCallback(async () => {
    try {
      const me = await api.me()
      setState({ kind: 'signed_in', me })
    } catch (e) {
      if (isSignedOut(e)) setState({ kind: 'signed_out' })
      else
        setState({
          kind: 'unreachable',
          message:
            'Could not reach the Zero Inbox backend — start it with `uv run python -m src` (port 8001) and reload.',
        })
    }
  }, [])

  useEffect(() => {
    void check()
  }, [check])

  if (state.kind === 'checking') {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <Loading label="Checking your session…" />
      </main>
    )
  }

  if (state.kind === 'unreachable') {
    return (
      <main className="mx-auto flex min-h-screen max-w-lg flex-col items-center justify-center gap-4 px-6 text-center">
        <h1 className="zi-h1">Zero Inbox</h1>
        <p className="zi-body text-zi-danger" role="alert">
          {state.message}
        </p>
        <button type="button" className="zi-btn zi-btn-secondary" onClick={() => void check()}>
          Try again
        </button>
      </main>
    )
  }

  if (state.kind === 'signed_out') {
    return <SignedOut />
  }

  return <Dashboard me={state.me} onSignedOut={() => setState({ kind: 'signed_out' })} />
}
