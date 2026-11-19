import type { KeyboardEvent as ReactKeyboardEvent, ReactNode, RefObject } from 'react'
import { useEffect, useId, useRef } from 'react'
import { X } from 'lucide-react'

// Keyboard rules for anything that opens over or inside the page (docs/08 accessibility
// table): Escape closes it and focus goes back to what opened it; a modal keeps Tab inside.

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function focusables(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => !el.closest('[inert]'))
}

/**
 * For inline panels (a confirmation, a reason form) that replace or follow the button that
 * opened them. When `open` becomes true, focus moves to the panel's first field or button; while open,
 * Escape (pressed inside the panel, or with focus nowhere) calls `close`; when it closes,
 * focus returns to `trigger` if it was in the panel or lost.
 */
export function useEscapeToClose(
  open: boolean,
  close: () => void,
  trigger: RefObject<HTMLElement | null>,
  within?: RefObject<HTMLElement | null>,
) {
  const closeRef = useRef(close)
  useEffect(() => {
    closeRef.current = close
  })
  const wasOpen = useRef(open)
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      const panel = within?.current
      // Only when the keyboard is in the panel (or nowhere), so two panels do not both close.
      if (panel && document.activeElement !== document.body && !panel.contains(document.activeElement)) return
      e.preventDefault()
      closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, within])
  useEffect(() => {
    if (!wasOpen.current && open && within?.current) {
      const panel = within.current
      const field = panel.querySelector<HTMLElement>('input:not([disabled]), select:not([disabled]), textarea:not([disabled])')
      // A field to fill in, else the panel's first button (a confirmation's action).
      ;(field ?? panel.querySelector<HTMLElement>('button:not([disabled])') ?? focusables(panel)[0])?.focus()
    }
    if (wasOpen.current && !open) {
      const panel = within?.current
      // After React has put the trigger back (inline panels often replace it).
      requestAnimationFrame(() => {
        const active = document.activeElement
        const lost = !active || active === document.body || !active.isConnected
        if (lost || (panel && panel.contains(active))) trigger.current?.focus()
      })
    }
    wasOpen.current = open
  }, [open, trigger, within])
}

/**
 * A modal side panel (the navigation on small screens): focus moves into it and stays there,
 * Escape or the backdrop closes it, focus returns to the trigger, and the page behind is inert.
 */
export function Drawer({
  open,
  onClose,
  label,
  trigger,
  children,
}: {
  open: boolean
  onClose: () => void
  label: string
  trigger: RefObject<HTMLElement | null>
  children: ReactNode
}) {
  const panel = useRef<HTMLDivElement>(null)
  const titleId = useId()
  useEscapeToClose(open, onClose, trigger)
  useEffect(() => {
    if (!open) return
    const el = panel.current
    if (el) (focusables(el)[0] ?? el).focus()
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = overflow
    }
  }, [open])
  if (!open) return null
  // The panel moves focus itself on every Tab rather than only at its ends: the browser's own
  // order can skip what is inside. Safari, by default, Tabs only between form fields and
  // buttons (links need Option+Tab), so from the close button its next stop was outside the
  // menu, past the inert page.
  const trap = (e: ReactKeyboardEvent) => {
    if (e.key !== 'Tab' || !panel.current) return
    const items = focusables(panel.current)
    if (!items.length) return
    e.preventDefault()
    const at = items.indexOf(document.activeElement as HTMLElement)
    const next = at === -1 ? (e.shiftKey ? items.length - 1 : 0) : (at + (e.shiftKey ? -1 : 1) + items.length) % items.length
    items[next]?.focus()
  }
  return (
    <div className="fixed inset-0 z-40 md:hidden print:hidden">
      <div className="absolute inset-0 bg-black/50" aria-hidden="true" onClick={onClose} />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={trap}
        className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col overflow-y-auto bg-brand-navy text-white shadow-xl"
      >
        <div className="flex h-14 items-center justify-between px-4">
          <span id={titleId} className="font-semibold">
            {label}
          </span>
          <button
            type="button"
            aria-label="Close menu"
            className="inline-flex size-10 items-center justify-center rounded-sm hover:bg-sidebar-active"
            onClick={onClose}
          >
            <X size={20} aria-hidden="true" />
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
