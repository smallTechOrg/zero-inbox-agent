'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { type Category, type DefaultAction, type TaxonomyProposal } from '@/lib/types'
import { ErrorState, SkeletonRows } from '@/components/States'

const ACTION_LABELS: Record<DefaultAction, string> = {
  archive: 'Archive',
  keep: 'Keep',
  digest: 'Digest',
  needs_your_call: 'Needs your call',
}

const ACTION_OPTIONS: DefaultAction[] = ['archive', 'keep', 'digest', 'needs_your_call']

// ---- Propose modal ----

function ProposeModal({
  proposals,
  model,
  onAccept,
  onClose,
}: {
  proposals: TaxonomyProposal[]
  model: string
  onAccept: (accepted: TaxonomyProposal[]) => Promise<void>
  onClose: () => void
}) {
  const [selected, setSelected] = useState<Set<number>>(new Set(proposals.map((_, i) => i)))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const toggle = (i: number) =>
    setSelected(prev => {
      const next = new Set(prev)
      next.has(i) ? next.delete(i) : next.add(i)
      return next
    })

  const acceptSelected = async () => {
    setSaving(true)
    setError(null)
    try {
      await onAccept(proposals.filter((_, i) => selected.has(i)))
      onClose()
    } catch (e) {
      setError(e)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="Proposed taxonomy">
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col rounded-xl bg-white shadow-2xl">
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <div>
            <h2 className="text-sm font-semibold text-gray-900">Proposed taxonomy changes</h2>
            <p className="text-[11px] text-gray-500">from {model} · select the proposals you want to accept</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-2">
          {proposals.length === 0 ? (
            <p className="text-xs text-gray-500">No proposals returned.</p>
          ) : (
            proposals.map((p, i) => (
              <label
                key={i}
                className={`flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors ${
                  selected.has(i) ? 'border-gray-900 bg-gray-50' : 'border-gray-200 bg-white'
                }`}
              >
                <input
                  type="checkbox"
                  checked={selected.has(i)}
                  onChange={() => toggle(i)}
                  className="mt-0.5 accent-gray-900"
                />
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-gray-600">
                      {p.action}
                    </span>
                    <span className="text-sm font-medium text-gray-900">{p.name}</span>
                  </div>
                  {p.description ? (
                    <p className="mt-0.5 text-xs text-gray-600">{p.description}</p>
                  ) : null}
                  <p className="mt-1 text-[11px] italic text-gray-500">{p.reasoning}</p>
                  {p.merge_keys && p.merge_keys.length > 0 ? (
                    <p className="mt-0.5 text-[11px] text-gray-400">
                      Merges: {p.merge_keys.join(', ')}
                    </p>
                  ) : null}
                </div>
              </label>
            ))
          )}
        </div>

        {error ? (
          <div className="px-4 pb-2">
            <ErrorState error={error} onRetry={() => void acceptSelected()} />
          </div>
        ) : null}

        <div className="flex items-center justify-between border-t border-gray-200 px-4 py-3">
          <button
            type="button"
            onClick={() => setSelected(new Set(proposals.map((_, i) => i)))}
            className="text-xs text-gray-500 hover:text-gray-800 underline"
          >
            Select all
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void acceptSelected()}
              disabled={saving || selected.size === 0}
              className="rounded bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-gray-700 disabled:opacity-50"
            >
              {saving ? 'Accepting…' : `Accept ${selected.size} proposal${selected.size !== 1 ? 's' : ''}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---- Single category row ----

interface CategoryRowProps {
  category: Category
  dragIndex: number
  draggingIndex: number | null
  onDragStart: (i: number) => void
  onDragOver: (i: number) => void
  onDrop: () => void
  onNameChange: (id: string, name: string) => Promise<void>
  onActionChange: (id: string, action: DefaultAction) => Promise<void>
}

function CategoryRow({
  category,
  dragIndex,
  draggingIndex,
  onDragStart,
  onDragOver,
  onDrop,
  onNameChange,
  onActionChange,
}: CategoryRowProps) {
  const [editing, setEditing] = useState(false)
  const [nameValue, setNameValue] = useState(category.name)
  const [nameSaving, setNameSaving] = useState(false)
  const [nameError, setNameError] = useState<string | null>(null)
  const [nameSaved, setNameSaved] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  // Sync prop name if it changes externally
  useEffect(() => {
    if (!editing) setNameValue(category.name)
  }, [category.name, editing])

  const commitName = useCallback(async () => {
    const trimmed = nameValue.trim()
    if (!trimmed || trimmed === category.name) {
      setEditing(false)
      setNameValue(category.name)
      return
    }
    setNameSaving(true)
    setNameError(null)
    try {
      await onNameChange(category.id, trimmed)
      setNameSaved(true)
      setTimeout(() => setNameSaved(false), 1500)
    } catch (e) {
      setNameError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setNameSaving(false)
      setEditing(false)
    }
  }, [nameValue, category.name, category.id, onNameChange])

  const isDragging = draggingIndex === dragIndex

  return (
    <li
      className={`flex items-center gap-2 rounded border bg-white px-2 py-2 transition-opacity ${
        isDragging ? 'opacity-40' : 'opacity-100'
      } border-gray-200`}
      draggable
      onDragStart={() => onDragStart(dragIndex)}
      onDragOver={e => { e.preventDefault(); onDragOver(dragIndex) }}
      onDrop={e => { e.preventDefault(); onDrop() }}
      data-testid="category-row"
    >
      {/* Drag handle */}
      <span
        className="cursor-grab select-none text-gray-300 hover:text-gray-500 active:cursor-grabbing"
        title="Drag to reorder"
        aria-label="Drag handle"
      >
        ⠿
      </span>

      {/* Name — inline edit on click */}
      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        {editing ? (
          <input
            ref={inputRef}
            type="text"
            value={nameValue}
            onChange={e => setNameValue(e.target.value)}
            onBlur={() => void commitName()}
            onKeyDown={e => {
              if (e.key === 'Enter') void commitName()
              if (e.key === 'Escape') { setEditing(false); setNameValue(category.name) }
            }}
            autoFocus
            className="min-w-0 flex-1 rounded border border-gray-300 px-1.5 py-0.5 text-sm text-gray-900 focus:outline-none focus:ring-1 focus:ring-gray-900"
            data-testid="category-name-input"
          />
        ) : (
          <button
            type="button"
            onClick={() => setEditing(true)}
            title="Click to rename"
            className="min-w-0 truncate rounded px-1 py-0.5 text-left text-sm text-gray-900 hover:bg-gray-100"
            data-testid="category-name"
          >
            {category.name}
          </button>
        )}

        {nameSaving && (
          <span className="text-[10px] text-gray-400">saving…</span>
        )}
        {nameSaved && !nameSaving && (
          <span className="text-[10px] text-emerald-600">✓</span>
        )}
        {nameError && (
          <span className="text-[10px] text-red-600" title={nameError}>!</span>
        )}
        {category.is_default && (
          <span className="shrink-0 rounded bg-gray-100 px-1 py-0.5 text-[9px] font-bold uppercase tracking-wide text-gray-500">
            system
          </span>
        )}
      </div>

      {/* Default action dropdown */}
      <select
        value={category.default_action}
        onChange={e => void onActionChange(category.id, e.target.value as DefaultAction)}
        className="shrink-0 rounded border border-gray-200 bg-white px-1.5 py-1 text-xs text-gray-700 focus:outline-none focus:ring-1 focus:ring-gray-900"
        data-testid="category-action-select"
        aria-label={`Default action for ${category.name}`}
      >
        {ACTION_OPTIONS.map(a => (
          <option key={a} value={a}>{ACTION_LABELS[a]}</option>
        ))}
      </select>
    </li>
  )
}

// ---- New category row ----

function NewCategoryRow({ onAdd }: { onAdd: (name: string) => Promise<void> }) {
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = useCallback(async () => {
    const trimmed = name.trim()
    if (!trimmed) return
    setSaving(true)
    setError(null)
    try {
      await onAdd(trimmed)
      setName('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Add failed')
    } finally {
      setSaving(false)
    }
  }, [name, onAdd])

  return (
    <form
      className="flex items-center gap-2 rounded border border-dashed border-gray-300 bg-gray-50 px-2 py-2"
      onSubmit={e => { e.preventDefault(); void submit() }}
    >
      <span className="select-none text-gray-200">⠿</span>
      <input
        type="text"
        value={name}
        onChange={e => setName(e.target.value)}
        placeholder="New category name…"
        disabled={saving}
        data-testid="new-category-name"
        className="min-w-0 flex-1 rounded border border-gray-300 bg-white px-1.5 py-0.5 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-gray-900"
        onKeyDown={e => e.key === 'Escape' && setName('')}
      />
      <button
        type="submit"
        disabled={saving || !name.trim()}
        data-testid="new-category-add"
        className="shrink-0 rounded bg-gray-900 px-2.5 py-1 text-xs font-semibold text-white hover:bg-gray-700 disabled:opacity-50"
      >
        {saving ? 'Adding…' : 'Add'}
      </button>
      {error && <span className="text-xs text-red-600">{error}</span>}
    </form>
  )
}

// ---- Main TaxonomyEditor ----

export function TaxonomyEditor() {
  const [categories, setCategories] = useState<Category[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [proposing, setProposing] = useState(false)
  const [proposeModal, setProposeModal] = useState<{ proposals: TaxonomyProposal[]; model: string } | null>(null)

  // Drag state
  const [draggingIndex, setDraggingIndex] = useState<number | null>(null)
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setCategories(await api.categories.list())
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const handleNameChange = useCallback(async (id: string, name: string) => {
    const updated = await api.categories.patch(id, { name })
    setCategories(prev => prev ? prev.map(c => c.id === id ? updated : c) : prev)
  }, [])

  const handleActionChange = useCallback(async (id: string, defaultAction: DefaultAction) => {
    const updated = await api.categories.patch(id, { default_action: defaultAction })
    setCategories(prev => prev ? prev.map(c => c.id === id ? updated : c) : prev)
  }, [])

  const handleAdd = useCallback(async (name: string) => {
    const created = await api.categories.create(name, 'keep')
    setCategories(prev => prev ? [...prev, created] : [created])
  }, [])

  // Drag-and-drop reorder
  const handleDrop = useCallback(async () => {
    if (draggingIndex === null || dragOverIndex === null || !categories) return
    if (draggingIndex === dragOverIndex) { setDraggingIndex(null); setDragOverIndex(null); return }

    const reordered = [...categories]
    const [moved] = reordered.splice(draggingIndex, 1)
    reordered.splice(dragOverIndex, 0, moved)

    // Optimistic update
    setCategories(reordered)
    setDraggingIndex(null)
    setDragOverIndex(null)

    // Persist new sort_order for each row that changed
    const patches = reordered
      .map((c, i) => ({ id: c.id, sort_order: i, original: categories.find(x => x.id === c.id)?.sort_order }))
      .filter(p => p.sort_order !== p.original)

    try {
      await Promise.all(patches.map(p => api.categories.patch(p.id, { sort_order: p.sort_order })))
    } catch (e) {
      setError(e)
      void load() // revert on failure
    }
  }, [draggingIndex, dragOverIndex, categories, load])

  const handlePropose = useCallback(async () => {
    setProposing(true)
    setError(null)
    try {
      const result = await api.categories.propose()
      setProposeModal({ proposals: result.proposals, model: result.model })
    } catch (e) {
      setError(e)
    } finally {
      setProposing(false)
    }
  }, [])

  const handleAcceptProposals = useCallback(async (accepted: TaxonomyProposal[]) => {
    const addable = accepted.filter(p => p.action === 'add')
    const created = await Promise.all(
      addable.map(p => api.categories.create(p.name, 'keep', p.key))
    )
    setCategories(prev => prev ? [...prev, ...created] : created)
  }, [])

  return (
    <section aria-label="Taxonomy editor" className="space-y-2 rounded-lg border border-gray-200 bg-white p-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-800">Taxonomy editor</h3>
          <p className="text-[11px] text-gray-500">Click a name to rename · drag ⠿ to reorder · dropdown sets the default action</p>
        </div>
        <button
          type="button"
          onClick={() => void handlePropose()}
          disabled={proposing}
          data-testid="propose-taxonomy-btn"
          className="shrink-0 rounded border border-gray-300 bg-white px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
        >
          {proposing ? 'Proposing…' : '✦ Propose taxonomy'}
        </button>
      </div>

      {error ? <ErrorState error={error} onRetry={() => void load()} /> : null}

      {loading ? (
        <SkeletonRows rows={4} label="Loading categories…" />
      ) : !categories || categories.length === 0 ? (
        <p className="rounded border border-dashed border-gray-300 px-3 py-3 text-xs text-gray-500">
          No categories yet. Add one below or use Propose taxonomy.
        </p>
      ) : (
        <ul className="space-y-1" data-testid="category-list">
          {categories.map((c, i) => (
            <CategoryRow
              key={c.id}
              category={c}
              dragIndex={i}
              draggingIndex={draggingIndex}
              onDragStart={idx => setDraggingIndex(idx)}
              onDragOver={idx => setDragOverIndex(idx)}
              onDrop={() => void handleDrop()}
              onNameChange={handleNameChange}
              onActionChange={handleActionChange}
            />
          ))}
        </ul>
      )}

      <NewCategoryRow onAdd={handleAdd} />

      {proposeModal ? (
        <ProposeModal
          proposals={proposeModal.proposals}
          model={proposeModal.model}
          onAccept={handleAcceptProposals}
          onClose={() => setProposeModal(null)}
        />
      ) : null}
    </section>
  )
}
