'use client'

import { type Settings as SettingsShape } from '@/lib/types'
import { StubPanel } from '@/components/Stub'

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
}

function SliderRow({ label, value, min, max, step, unit, onChange, hint }: SliderRowProps) {
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
          data-testid={`slider-${label.toLowerCase().replace(/\s/g, '-')}`}
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

export default function SettingsPanel({
  settings,
  onSettingsChange,
}: {
  settings: SettingsShape | null
  onSettingsChange: (s: SettingsShape) => void
}) {
  if (!settings) {
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
        value={settings.confidence_floor}
        min={0.25}
        max={0.95}
        step={0.01}
        unit="%"
        hint="Threads below this confidence go to Needs your call instead of being archived."
        onChange={v => onSettingsChange({ ...settings, confidence_floor: v })}
      />

      {/* Auto-act threshold (Phase 1 stubbed — locked display, Phase 2 real) */}
      <SliderRow
        label="Auto-act threshold"
        value={settings.auto_act_threshold}
        min={0.95}
        max={0.99}
        step={0.01}
        unit="%"
        hint="COMING SOON · Phase 2. When dry-run is off, clusters above this confidence auto-archive."
        onChange={() => {}}
      />

      {/* Dry-run toggle — locked ON in Phase 1 */}
      <div className="flex items-center justify-between gap-3">
        <label className="text-xs font-medium text-gray-700">Dry run</label>
        <button
          type="button"
          disabled
          aria-disabled
          title="Dry-run is locked ON in Phase 1 — the agent never mutates your Gmail. Turn it off in Phase 2."
          className="relative inline-flex h-5 w-10 items-center rounded-full bg-emerald-400 opacity-60"
        >
          <span className="absolute left-1.5 top-[3px] text-[9px] font-bold text-emerald-900">
            ON
          </span>
          <span className="absolute right-2 top-[3px] text-[9px] font-medium text-emerald-700">
            dry
          </span>
        </button>
      </div>

      {/* Timezone + digest hour */}
      <div className="flex flex-col gap-1.5">
        <label className="text-xs font-medium text-gray-700">Timezone</label>
        <select
          data-testid="timezone-select"
          value={settings.timezone}
          onChange={e => onSettingsChange({ ...settings, timezone: e.target.value })}
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
          value={settings.digest_hour_local}
          onChange={e =>
            onSettingsChange({
              ...settings,
              digest_hour_local: clamp(Number(e.target.value), 0, 23),
            })
          }
          data-testid="digest-hour"
          className="w-16 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-800"
        />
      </div>

      {/* Phase-1 stubs: VIP list + priorities profile */}
      <div className="border-t border-gray-200 pt-4">
        <p className="mb-2 text-[11px] font-semibold tracking-wide text-gray-500 uppercase">
          Phase 2
        </p>
        <StubPanel
          title="VIP / never-hide list"
          phase={2}
          description="People whose mail is never hidden, whatever the agent thinks."
        />
        <StubPanel
          title="Priorities profile"
          phase={2}
          description="A plain-English description of what matters to you."
        />
      </div>
    </section>
  )
}
