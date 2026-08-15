/**
 * The design-system contract, in TypeScript — spec/ui.md § Design system.
 *
 * `globals.css` owns the *values*; this file owns the *names* components use so
 * a raw hex, a one-off shadow or a bare coloured dot never gets typed by hand.
 * Other slices may import from here, but the binding contract is the class name
 * (they can equally write `bg-zi-bg-subtle` directly).
 *
 * BINDING RULE — state is never colour alone. `stateClasses()` deliberately
 * requires the caller to supply a text label; there is no API here that emits a
 * coloured element with no accessible text.
 */

export type StateTone = 'ok' | 'warn' | 'danger' | 'info' | 'neutral'

/** Text + background + border for a state surface. Never used bare. */
export const STATE_SURFACE: Record<StateTone, string> = {
  ok: 'bg-zi-ok-bg text-zi-ok border-zi-ok',
  warn: 'bg-zi-warn-bg text-zi-warn border-zi-warn',
  danger: 'bg-zi-danger-bg text-zi-danger border-zi-danger',
  info: 'bg-zi-info-bg text-zi-info border-zi-info',
  neutral: 'bg-zi-bg-subtle text-zi-fg-muted border-zi-border',
}

/** The class list for a state chip. Callers must render `label` inside it. */
export function stateChipClass(tone: StateTone): string {
  return `zi-caption inline-flex items-center gap-1 rounded-zi-r-sm border px-2 py-0.5 ${STATE_SURFACE[tone]}`
}

/** The class list for a full-width state bar (apply failure, degraded, dry run). */
export function stateBarClass(tone: StateTone): string {
  return `zi-body flex flex-wrap items-center gap-3 rounded-zi-r-md border px-4 py-3 ${STATE_SURFACE[tone]}`
}

export const BTN = {
  primary: 'zi-btn zi-btn-primary',
  primaryLarge: 'zi-btn zi-btn-primary zi-btn-lg',
  secondary: 'zi-btn zi-btn-secondary',
  danger: 'zi-btn zi-btn-danger',
} as const

export const SURFACE = {
  card: 'zi-card',
  menu: 'zi-menu',
  page: 'bg-zi-bg text-zi-fg',
  band: 'bg-zi-bg-subtle',
  inverse: 'bg-zi-bg-inverse text-white',
} as const

/** The centred max-width used by every page column at ≥ 1280px. */
export const CONTENT = 'mx-auto w-full max-w-[1120px]'

/**
 * The `loading` component state: a spinner *plus* the label rewritten as a
 * present participle. The label never disappears — that is the whole point.
 */
export function loadingLabel(idle: string, busy: string, isBusy: boolean): string {
  return isBusy ? busy : idle
}
