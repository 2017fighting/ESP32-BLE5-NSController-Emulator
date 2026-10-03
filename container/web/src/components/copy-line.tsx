import { Button } from '@/components/ui/button'
import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

/** Copyable mono line for a mount path or a docker run fragment. */
export function CopyLine({ value, label }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      toast.success('Copied')
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('Copy failed — select the text manually')
    }
  }
  return (
    <div className="flex items-start gap-2">
      <code
        tabIndex={0}
        className="flex-1 overflow-x-auto rounded-md border border-border bg-muted/50 px-2 py-1.5 font-mono text-xs text-foreground"
      >
        {value}
      </code>
      <Button type="button" variant="ghost" size="icon" onClick={copy} aria-label={label ? `Copy ${label}` : 'Copy'}>
        {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
      </Button>
    </div>
  )
}
