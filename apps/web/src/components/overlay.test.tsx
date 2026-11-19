import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, useState } from 'react'
import { describe, expect, it } from 'vitest'

import { Drawer, useEscapeToClose } from './overlay'

function DrawerHarness() {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  return (
    <>
      <button ref={trigger} type="button" onClick={() => setOpen(true)}>
        Open menu
      </button>
      <button type="button">Behind</button>
      <Drawer open={open} onClose={() => setOpen(false)} label="Menu" trigger={trigger}>
        <a href="#one">One</a>
        <a href="#two">Two</a>
      </Drawer>
    </>
  )
}

function PanelHarness() {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLFormElement>(null)
  useEscapeToClose(open, () => setOpen(false), trigger, panel)
  return open ? (
    <form ref={panel}>
      <label>
        Reason <input />
      </label>
      <button type="button" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </form>
  ) : (
    <button ref={trigger} type="button" onClick={() => setOpen(true)}>
      Dismiss…
    </button>
  )
}

describe('Drawer', () => {
  it('is a modal dialog: focus moves in, Tab wraps inside, Escape closes and focus returns', async () => {
    const user = userEvent.setup()
    render(<DrawerHarness />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    const dialog = screen.getByRole('dialog', { name: 'Menu' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    const close = screen.getByRole('button', { name: 'Close menu' })
    expect(document.activeElement).toBe(close)
    await user.tab()
    expect(document.activeElement).toBe(screen.getByRole('link', { name: 'One' }))
    await user.tab()
    await user.tab()
    // Past the last link: back to the first control, never to "Behind".
    expect(document.activeElement).toBe(close)
    await user.tab({ shift: true })
    expect(document.activeElement).toBe(screen.getByRole('link', { name: 'Two' }))
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open menu' })))
  })

  it('moves focus itself on every Tab, so a browser that skips links (Safari) stays inside', async () => {
    const user = userEvent.setup()
    render(<DrawerHarness />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    const close = screen.getByRole('button', { name: 'Close menu' })
    // fireEvent returns false when the handler took over (preventDefault): the browser's own
    // next stop, which in Safari would be outside the menu, is never used.
    expect(fireEvent.keyDown(close, { key: 'Tab' })).toBe(false)
    expect(document.activeElement).toBe(screen.getByRole('link', { name: 'One' }))
    // From the panel itself (focus nowhere in particular), Tab starts at the first control.
    screen.getByRole('dialog', { name: 'Menu' }).focus()
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(screen.getByRole('link', { name: 'Two' }))
  })

  it('closes from its close button and locks the page scroll while open', async () => {
    const user = userEvent.setup()
    render(<DrawerHarness />)
    await user.click(screen.getByRole('button', { name: 'Open menu' }))
    expect(document.body.style.overflow).toBe('hidden')
    await user.click(screen.getByRole('button', { name: 'Close menu' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.body.style.overflow).toBe('')
  })
})

describe('useEscapeToClose', () => {
  it('focuses the first field, closes on Escape and gives focus back to the trigger', async () => {
    const user = userEvent.setup()
    render(<PanelHarness />)
    await user.click(screen.getByRole('button', { name: 'Dismiss…' }))
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Reason' }))
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('textbox')).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Dismiss…' })))
  })

  it('also returns focus when the panel is closed by its own button', async () => {
    const user = userEvent.setup()
    render(<PanelHarness />)
    await user.click(screen.getByRole('button', { name: 'Dismiss…' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Dismiss…' })))
  })
})
