'use client'

import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { RunEvent } from './types'

/** Terminal event types — the stream is done after any of these. */
const TERMINAL = new Set(['run_finished', 'run_interrupted', 'undo_finished', 'undo_failed'])

export interface RunFeed {
  events: RunEvent[]
  /** True while the EventSource is open (or reconnecting). */
  live: boolean
  /** Set once a terminal event arrives — feed can collapse to the run card. */
  terminalType: string | null
  /** Human message when the stream cannot be (re)opened. */
  connectionError: string | null
}

function parseEvent(data: string): RunEvent | null {
  try {
    const raw = JSON.parse(data) as Record<string, unknown>
    let detail = raw.detail ?? raw.detail_json ?? {}
    if (typeof detail === 'string') {
      try {
        detail = JSON.parse(detail)
      } catch {
        detail = {}
      }
    }
    return {
      seq: Number(raw.seq ?? 0),
      type: String(raw.type ?? 'action'),
      sentence: String(raw.sentence ?? ''),
      detail: (detail as Record<string, unknown>) ?? {},
      created_at: raw.created_at as string | undefined,
    }
  } catch {
    return null
  }
}

/**
 * Streams a run's persisted+live events. Reconnect-safe by design
 * (spec/ui.md §3): every (re)connect passes `?after_seq=<max seen>` so a
 * page reload mid-run replays the full feed and continues live.
 *
 * `epoch` lets a caller re-arm the feed on the SAME run id (e.g. undo streams
 * on the same channel after the run finished).
 */
export function useRunFeed(runId: string | null, epoch = 0): RunFeed {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [live, setLive] = useState(false)
  const [terminalType, setTerminalType] = useState<string | null>(null)
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const lastSeq = useRef(0)

  useEffect(() => {
    setEvents([])
    setTerminalType(null)
    setConnectionError(null)
    lastSeq.current = 0
    if (!runId) return

    let es: EventSource | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let retries = 0
    let done = false

    const open = () => {
      es = new EventSource(api.runs.eventsUrl(runId, lastSeq.current))
      es.onopen = () => {
        retries = 0
        setLive(true)
        setConnectionError(null)
      }
      es.onmessage = (msg) => {
        const ev = parseEvent(msg.data)
        if (!ev || ev.seq <= lastSeq.current) return
        lastSeq.current = ev.seq
        setEvents((prev) => [...prev, ev])
        if (TERMINAL.has(ev.type)) {
          done = true
          setTerminalType(ev.type)
          es?.close()
          setLive(false)
        }
      }
      es.onerror = () => {
        es?.close()
        setLive(false)
        if (done) return
        retries += 1
        if (retries > 20) {
          setConnectionError('Lost the live feed — reload the page to catch up.')
          return
        }
        retryTimer = setTimeout(open, Math.min(1000 * retries, 8000))
      }
    }

    open()
    return () => {
      done = true
      es?.close()
      if (retryTimer) clearTimeout(retryTimer)
      setLive(false)
    }
  }, [runId, epoch])

  return { events, live, terminalType, connectionError }
}
