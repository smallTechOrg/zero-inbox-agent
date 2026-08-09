'use client'

import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'
import { type Settings as SettingsShape, type VipEntry, type VipKind } from '@/lib/types'
import { StubPanel } from '@/components/Stub'
import { ErrorState, SkeletonRows } from '@/components/States'

function clamp(v: number, min: number, max: number) {
  return Math.min(Math.max(v, min), max)
}

interface SliderRowProps {
  label: string
  value: number
  min: number
  max: number
  step: number
  unit: string
  onChange: (v: number) => void
  hint?: string
  testid?: string
}

function SliderRow({ label, value, min, max, step, unit, onChange, hint, testid }: SliderRowProps) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-gray-700">{label}</span>
      <div className="flex items-center gap-2">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={e => onChange(clamp(Number(e.target.value), min, max))}
          data-testid={testid ?? `slider-${label.toLowerCase().replace(/\s/g, '-')}`}
          className="flex-1 accent-gray-900"
        />
        <span className="w-12 text-right font-mono text-xs text-gray-800">
          {value.toFixed(unit === '%' ? 0 : 2)}{unit}
        </span>
      </div>
      {hint ? <span className="text-[10px] leading-tight text-gray-500">{hint}</span> : null}
    </label>
  )
}

/** GET/POST/DELETE /api/vip — the never-hide list (spec/api.md Phase 2). */
function VipEditor() {
  const [entries, setEntries] = useState<VipEntry[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [kind, setKind] = useState<VipKind>('email')
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [removingId, setRemovingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setEntries(await api.vip.list())
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const add = useCallback(async () => {
    const trimmed = value.trim()
    if (!trimmed) return
    setSaving(true)
    setError(null)
    try {
      const created = await api.vip.add(kind, trimmed)
      setEntries(prev => (prev ? [created, ...prev] : [created]))
      setValue('')
    } catch (e) {
      setError(e)
    } finally {
      setSaving(false)
    }
  }, [kind, value])

  const remove = useCallback(async (id: string) => {
    setRemovingId(id)
    setError(null)
    try {
      await api.vip.remove(id)
      setEntries(prev => (prev ? prev.filter(e => e.id !== id) : prev))
    } catch (e) {
      setError(e)
    } finally {
      setRemovingId(null)
    }
  }, [])

  return (
    <section aria-label="VIP / never-hide list" className="space-y-2 rounded-lg border border-gray-200 bg-white p-3">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-800">VIP / never-hide list</h3>
        <p className="text-[11px] text-gray-500">A match here can never be archived automatically.</p>
      </div>

      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={e => {
          e.preventDefault()
          void add()
        }}
      >
        <select
          data-testid="vip-kind"
          value={kind}
          onChange={e => setKind(e.target.value as VipKind)}
          className="rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-800"
        >
          <option value="email">Email</option>
          <option value="domain">Domain</option>
          <option value="keyword">Keyword</option>
        </select>
        <input
          type="text"
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder={kind === 'email' ? 'someone@example.com' : kind === 'domain' ? 'example.com' : 'keyword'}
          data-testid="vip-value"
          className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-800"
        />
        <button
          type="submit"
          disabled={saving || !value.trim()}
          data-testid="vip-add"
          className="rounded bg-gray-900 px-2.5 py-1 text-xs font-semibold text-white hover:bg-gray-700 disabled:opacity-50"
        >
          {saving ? 'Adding…' : 'Add'}
        </button>
      </form>

      {error ? <ErrorState error={error} onRetry={() => void load()} /> : null}

      {loading ? (
        <SkeletonRows rows={2} label="Loading VIP list…" />
      ) : entries && entries.length > 0 ? (
        <ul className="divide-y divide-gray-100 rounded border border-gray-200">
          {entries.map(e => (
            <li key={e.id} className="flex items-center justify-between gap-2 px-2 py-1.5" data-testid="vip-row">
              <span className="flex min-w-0 items-center gap-2">
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-gray-600 uppercase">
                  {e.kind}
                </span>
                <span className="truncate text-sm text-gray-800">{e.value}</span>
              </span>
              <button
                type="button"
                onClick={() => void remove(e.id)}
                disabled={removingId === e.id}
                data-testid="vip-remove"
                className="shrink-0 rounded border border-gray-300 bg-white px-2 py-0.5 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
              >
                {removingId === e.id ? 'Removing…' : 'Remove'}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="rounded border border-dashed border-gray-300 px-3 py-3 text-xs text-gray-500">
          No VIP entries yet. Add an email, domain or keyword that should never be hidden.
        </p>
      )}
    </section>
  )
}

/** GET/PUT /api/profile — the plain-English priorities profile (spec/api.md Phase 2). */
function PriorityProfileEditor() {
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(true)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const p = await api.profile.get()
      setText(p.text ?? '')
      setSavedAt(p.updated_at)
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const save = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      const p = await api.profile.put(text)
      setSavedAt(p.updated_at)
      setSaved(true)
    } catch (e) {
      setError(e)
    } finally {
      setSaving(false)
    }
  }, [text])

  return (
    <section aria-label="Priorities profile" className="space-y-2 rounded-lg border border-gray-200 bg-white p-3">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-800">Priorities profile</h3>
        {savedAt ? (
          <p className="text-[11px] text-gray-500">
            Saved {new Date(savedAt).toLocaleString()}
          </p>
        ) : null}
      </div>
      <p className="text-xs text-gray-600">
        Plain English, written once, fed verbatim into the classifier and reviewer prompts — your own
        words, never rewritten by the agent.
      </p>
      {loading ? (
        <SkeletonRows rows={1} label="Loading profile…" />
      ) : (
        <>
          <textarea
            value={text}
            onChange={e => {
              setText(e.target.value)
              setSaved(false)
            }}
            rows={4}
            data-testid="priorities-profile-text"
            placeholder="e.g. I care about anything from investors and about our fundraise."
            className="w-full rounded border border-gray-300 bg-white p-2 text-sm text-gray-800"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving || saved}
              data-testid="priorities-profile-save"
              className="rounded bg-gray-900 px-3 py-1 text-xs font-semibold text-white hover:bg-gray-700 disabled:opacity-50"
            >
              {saving ? 'Saving…' : saved ? 'Saved' : 'Save profile'}
            </button>
          </div>
        </>
      )}
      {error ? <ErrorState error={error} onRetry={() => void load()} /> : null}
    </section>
  )
}

export default function SettingsPanel({
  settings,
  onSettingsChange,
}: {
  settings: SettingsShape | null
  onSettingsChange: (s: SettingsShape) => void
}) {
  const [draft, setDraft] = useState<SettingsShape | null>(settings)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<unknown>(null)
  const [savedOk, setSavedOk] = useState(false)

  useEffect(() => {
    setDraft(settings)
  }, [settings])

  const dirty =
    !!draft &&
    !!settings &&
    (draft.confidence_floor !== settings.confidence_floor ||
      draft.auto_act_threshold !== settings.auto_act_threshold ||
      draft.dry_run !== settings.dry_run ||
      draft.timezone !== settings.timezone ||
      draft.digest_hour_local !== settings.digest_hour_local)

  const save = useCallback(async () => {
    if (!draft) return
    setSaving(true)
    setSaveError(null)
    setSavedOk(false)
    try {
      const updated = await api.updateSettings({
        auto_act_threshold: draft.auto_act_threshold,
        confidence_floor: draft.confidence_floor,
        dry_run: draft.dry_run,
        llm_model: draft.llm_model,
        timezone: draft.timezone,
      })
      onSettingsChange(updated)
      setSavedOk(true)
    } catch (e) {
      setSaveError(e)
    } finally {
      setSaving(false)
    }
  }, [draft, onSettingsChange])

  if (!draft) {
    return (
      <section className="space-y-4 p-4">
        <div className="text-xs text-gray-500">Loading settings…</div>
      </section>
    )
  }

  return (
    <section className="space-y-6 p-4" aria-label="Settings">
      {/* Confidence floor */}
      <SliderRow
        label="Confidence floor"
        value={draft.confidence_floor}
        min={0.25}
        max={0.95}
        step={0.01}
        unit="%"
        hint="Threads below this confidence go to Needs your call instead of being archived."
        onChange={v => {
          setDraft({ ...draft, confidence_floor: v })
          setSavedOk(false)
        }}
      />

      {/* Auto-act threshold — real in Phase 2 */}
      <SliderRow
        label="Auto-act threshold"
        value={draft.auto_act_threshold}
        min={0.5}
        max={0.99}
        step={0.01}
        unit="%"
        testid="slider-auto-act-threshold"
        hint="When dry-run is off, approving a cluster at or above this confidence archives for real."
        onChange={v => {
          setDraft({ ...draft, auto_act_threshold: v })
          setSavedOk(false)
        }}
      />

      {/* Dry-run toggle — real in Phase 2 */}
      <div className="flex items-center justify-between gap-3">
        <label className="text-xs font-medium text-gray-700" htmlFor="dry-run-toggle">
          Dry run
        </label>
        <button
          id="dry-run-toggle"
          type="button"
          data-testid="dry-run-toggle"
          aria-pressed={draft.dry_run}
          title={
            draft.dry_run
              ? 'Dry-run is on — approving records intent only, nothing in Gmail changes. Turn off to act for real.'
              : 'Dry-run is off — approving a cluster or thread now really archives and labels mail in Gmail.'
          }
          onClick={() => {
            setDraft({ ...draft, dry_run: !draft.dry_run })
            setSavedOk(false)
          }}
          className={`relative inline-flex h-5 w-10 items-center rounded-full transition-colors ${
            draft.dry_run ? 'bg-emerald-400' : 'bg-rose-500'
          }`}
        >
          <span
            className={`absolute top-[3px] h-3.5 w-3.5 rounded-full bg-white shadow transition-transform ${
              draft.dry_run ? 'translate-x-[3px]' : 'translate-x-[21px]'
            }`}
          />
        </button>
      </div>
      <p className="-mt-4 text-[10px] leading-tight text-gray-500">
        {draft.dry_run
          ? 'ON — the agent never mutates your Gmail. Save to keep it this way.'
          : 'OFF — approved decisions really archive and label mail. Save to apply.'}
      </p>

      {/* Timezone + digest hour */}
      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-gray-700">Timezone</label>
        <select
          data-testid="timezone-select"
          value={draft.timezone}
          onChange={e => {
            setDraft({ ...draft, timezone: e.target.value })
            setSavedOk(false)
          }}
          className="rounded border border-gray-300 bg-white px-2 py-1 text-sm text-gray-800"
        >
          <option value="America/New_York">America/New_York</option>
          <option value="America/Los_Angeles">America/Los_Angeles</option>
          <option value="Europe/London">Europe/London</option>
          <option value="Asia/Kolkata">Asia/Kolkata</option>
        </select>
      </div>

      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-gray-700">Daily digest hour (local)</label>
        <input
          type="number"
          min={0}
          max={23}
          value={draft.digest_hour_local}
          onChange={e => {
            setDraft({
              ...draft,
              digest_hour_local: clamp(Number(e.target.value), 0, 23),
            })
            setSavedOk(false)
          }}
          data-testid="digest-hour"
          className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-800"
        />
        <span className="text-[10px] text-gray-500">COMING SOON · Phase 3 — the Digest screen itself is not built yet.</span>
      </div>

      <div className="flex items-center gap-2 border-t border-gray-200 pt-4">
        <button
          type="button"
          onClick={() => void save()}
          disabled={saving || !dirty}
          data-testid="settings-save"
          className="rounded-lg bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:bg-gray-700 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        {savedOk && !dirty ? (
          <span className="text-xs font-medium text-emerald-800">Saved.</span>
        ) : null}
      </div>
      {saveError ? <ErrorState error={saveError} onRetry={() => void save()} /> : null}

      {/* Phase 2: real VIP list + priorities profile */}
      <div className="space-y-3 border-t border-gray-200 pt-4">
        <VipEditor />
        <PriorityProfileEditor />
      </div>

      {/* Still stubs in Phase 2 */}
      <div className="border-t border-gray-200 pt-4">
        <p className="mb-2 text-[11px] font-semibold tracking-wide text-gray-500 uppercase">
          Not built yet
        </p>
        <StubPanel
          title="Taxonomy editor"
          phase={3}
          description="Rename, merge and reorder your categories directly from Settings."
        />
      </div>
    </section>
  )
}
