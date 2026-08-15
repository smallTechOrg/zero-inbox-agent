'use client'

import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { Category, CategoryRule } from '../lib/types'
import { ErrorNote, Loading, Section } from './ui'

const RULE_LABEL: Record<CategoryRule, string> = {
  label_only: 'Label only',
  label_and_archive: 'Label + archive',
}

function CategoryChip({
  cat,
  onChanged,
  onError,
}: {
  cat: Category
  onChanged: () => void
  onError: (msg: string) => void
}) {
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState(cat.name)
  const [busy, setBusy] = useState(false)

  const patch = async (body: Partial<Pick<Category, 'name' | 'rule'>>) => {
    setBusy(true)
    try {
      await api.taxonomy.patch(cat.id, body)
      onChanged()
    } catch (e) {
      onError(e instanceof ApiError ? e.message : 'Could not save that change — try again.')
    } finally {
      setBusy(false)
      setRenaming(false)
    }
  }

  const remove = async () => {
    if (!window.confirm(`Delete the "${cat.name}" category? Its Gmail label is not touched.`))
      return
    setBusy(true)
    try {
      await api.taxonomy.remove(cat.id)
      onChanged()
    } catch (e) {
      onError(
        e instanceof ApiError && e.status === 409
          ? `"${cat.name}" is in use by past decisions and can't be deleted in Phase 1.`
          : 'Could not delete that category — try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <li
      className={`flex flex-wrap items-center gap-2 rounded-zi-r-md border px-3 py-2 ${
        cat.is_needs_review
          ? 'border-zi-warn/40 bg-zi-warn-bg'
          : 'border-zi-border bg-zi-bg'
      }`}
    >
      {renaming ? (
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (name.trim() && name.trim() !== cat.name) void patch({ name: name.trim() })
            else setRenaming(false)
          }}
        >
          <label className="sr-only" htmlFor={`rename-${cat.id}`}>
            New name for {cat.name}
          </label>
          <input
            id={`rename-${cat.id}`}
            className="zi-input w-40"
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onBlur={() => setRenaming(false)}
          />
          <button type="submit" className="zi-btn zi-btn-secondary" disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </form>
      ) : (
        <button
          type="button"
          className="zi-focusable zi-body rounded-zi-r-sm font-semibold"
          title={cat.is_needs_review ? undefined : 'Click to rename'}
          disabled={cat.is_needs_review}
          onClick={() => setRenaming(true)}
        >
          {cat.name}
        </button>
      )}

      {cat.is_needs_review ? (
        <span className="zi-caption text-zi-warn">
          pinned · label only — anything the model isn&rsquo;t sure about lands here
        </span>
      ) : (
        <>
          <label className="sr-only" htmlFor={`rule-${cat.id}`}>
            Rule for {cat.name}
          </label>
          <select
            id={`rule-${cat.id}`}
            className="zi-input w-auto"
            value={cat.rule}
            disabled={busy}
            onChange={(e) => void patch({ rule: e.target.value as CategoryRule })}
          >
            {(Object.keys(RULE_LABEL) as CategoryRule[]).map((r) => (
              <option key={r} value={r}>
                {RULE_LABEL[r]}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="zi-focusable zi-caption rounded-zi-r-sm px-1 text-zi-fg-faint hover:text-zi-danger"
            aria-label={`Delete ${cat.name}`}
            disabled={busy}
            onClick={remove}
          >
            ✕
          </button>
        </>
      )}
    </li>
  )
}

/**
 * Taxonomy panel (spec/ui.md §5). Edits apply to the NEXT chunk — the agent
 * adapts between chunks, which the caption says out loud.
 */
export function TaxonomyPanel({
  categories,
  loading,
  loadError,
  onChanged,
}: {
  categories: Category[]
  loading: boolean
  loadError: string | null
  onChanged: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const add = async () => {
    if (!newName.trim()) return
    setBusy(true)
    setError(null)
    try {
      await api.taxonomy.add({ name: newName.trim(), description: '', rule: 'label_only' })
      setNewName('')
      setAdding(false)
      onChanged()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not add that category — try again.')
    } finally {
      setBusy(false)
    }
  }

  const sorted = [...categories].sort(
    (a, b) => Number(b.is_needs_review) - Number(a.is_needs_review) || a.position - b.position,
  )

  return (
    <Section
      title="Your categories"
      aside={
        <p className="zi-caption text-zi-fg-muted">Edits apply from the next chunk — the agent adapts.</p>
      }
    >
      {loading && <Loading label="Loading your categories…" />}
      {loadError && <ErrorNote message={loadError} onRetry={onChanged} />}
      {!loading && !loadError && (
        <>
          {error && (
            <div className="mb-3">
              <ErrorNote message={error} />
            </div>
          )}
          <ul className="flex flex-wrap gap-2">
            {sorted.map((cat) => (
              <CategoryChip key={cat.id} cat={cat} onChanged={onChanged} onError={setError} />
            ))}
            <li>
              {adding ? (
                <form
                  className="flex items-center gap-2"
                  onSubmit={(e) => {
                    e.preventDefault()
                    void add()
                  }}
                >
                  <label className="sr-only" htmlFor="new-category">
                    New category name
                  </label>
                  <input
                    id="new-category"
                    className="zi-input w-44"
                    placeholder="e.g. Receipts"
                    value={newName}
                    autoFocus
                    onChange={(e) => setNewName(e.target.value)}
                  />
                  <button type="submit" className="zi-btn zi-btn-primary" disabled={busy}>
                    {busy ? 'Adding…' : 'Add'}
                  </button>
                  <button
                    type="button"
                    className="zi-btn zi-btn-secondary"
                    onClick={() => setAdding(false)}
                  >
                    Cancel
                  </button>
                </form>
              ) : (
                <button
                  type="button"
                  className="zi-btn zi-btn-secondary"
                  onClick={() => setAdding(true)}
                >
                  + Add category
                </button>
              )}
            </li>
          </ul>
        </>
      )}
    </Section>
  )
}
