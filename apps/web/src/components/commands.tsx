import { Check, Copy } from 'lucide-react'
import { useState } from 'react'

export function CopyButton({ text, label = 'Copy command' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      aria-label={label}
      className="shrink-0 rounded-sm p-1.5 text-secondary hover:bg-surface-2"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        })
      }}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
    </button>
  )
}

/** AWS CLI commands, numbered, each with a copy button. */
export function Commands({ steps }: { steps: string[] }) {
  return (
    <ol className="flex flex-col gap-2">
      {steps.map((step, i) => (
        <li key={step} className="flex items-start gap-2 rounded-sm bg-surface-2 px-3 py-2">
          <span className="pt-0.5 text-xs text-muted">{i + 1}</span>
          <code className="min-w-0 flex-1 break-all font-mono text-xs">{step}</code>
          <CopyButton text={step} />
        </li>
      ))}
    </ol>
  )
}
