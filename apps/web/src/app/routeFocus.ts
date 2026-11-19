import type { RefObject } from 'react'
import { useEffect, useRef } from 'react'

const APP = 'Cloudoptics'
const WAIT_MS = 3000

/** The page's name: the h1's own text (not badges inside it, such as "Advisory only"). */
export function headingTitle(h1: HTMLElement): string {
  const own = [...h1.childNodes]
    .filter((n) => n.nodeType === Node.TEXT_NODE)
    .map((n) => n.textContent ?? '')
    .join('')
    .trim()
  return (own || h1.textContent || '').replace(/\s+/g, ' ').trim()
}

/**
 * Single-page navigation does not reload the page, so a screen reader hears nothing when the
 * route changes. On each new path: the tab title becomes "<page heading> · Cloudoptics" and,
 * after the first page, focus moves to the new page's h1 (or `main` if it has none), which
 * reads the page name and puts the next Tab at the start of its content.
 */
export function useRouteFocus(pathname: string, main: RefObject<HTMLElement | null>) {
  const first = useRef(true)
  useEffect(() => {
    const moveFocus = !first.current
    first.current = false
    const root = main.current
    if (!root) return
    let done = false
    const settle = (h1: HTMLElement | null) => {
      if (done) return
      done = true
      observer.disconnect()
      window.clearTimeout(timer)
      document.title = h1 && headingTitle(h1) ? `${headingTitle(h1)} · ${APP}` : APP
      if (!moveFocus) return
      // Leave focus alone if something on the new page already took it (a form field).
      const active = document.activeElement
      if (active && active !== document.body && root.contains(active) && active !== root) return
      const target = h1 ?? root
      if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1')
      target.focus({ preventScroll: true })
    }
    const find = () => root.querySelector<HTMLElement>('h1')
    const observer = new MutationObserver(() => {
      const h1 = find()
      if (h1) settle(h1)
    })
    const timer = window.setTimeout(() => settle(find()), WAIT_MS)
    // The old page may still be on screen for a moment: wait a frame for the new one.
    const frame = requestAnimationFrame(() => {
      const h1 = find()
      if (h1) settle(h1)
      else observer.observe(root, { childList: true, subtree: true })
    })
    return () => {
      done = true
      cancelAnimationFrame(frame)
      observer.disconnect()
      window.clearTimeout(timer)
    }
  }, [pathname, main])
}
