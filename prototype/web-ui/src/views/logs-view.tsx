import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'
import { formatClock, type LogEntry } from '@/prototype/model'
import { useStore } from '@/prototype/store'
import { Pause, Play, Search, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

type SourceFilter = 'all' | LogEntry['source']
type LevelFilter = 'all' | 'info' | 'warn' | 'error'

const LEVEL_TONE: Record<LogEntry['level'], string> = {
  debug: 'border-border bg-muted text-muted-foreground',
  info: 'border-info/30 bg-info/10 text-info dark:border-info/25 dark:bg-info/15',
  warn: 'border-warning/35 bg-warning/10 text-warning dark:border-warning/25 dark:bg-warning/15',
  error: 'border-destructive/30 bg-destructive/10 text-destructive dark:border-destructive/30 dark:bg-destructive/20',
}

const LEVEL_ORDER: LogEntry['level'][] = ['debug', 'info', 'warn', 'error']

const SOURCE_LABEL: Record<LogEntry['source'], string> = {
  container: 'container',
  device: 'device log',
  frame: 'frame',
}

function LevelTag({ level }: { level: LogEntry['level'] }) {
  return (
    <Badge variant="outline" className={cn('w-14 justify-center font-mono text-[0.65rem] uppercase', LEVEL_TONE[level])}>
      {level}
    </Badge>
  )
}

export function LogsView() {
  const { state, clearLogs } = useStore()
  const [source, setSource] = useState<SourceFilter>('all')
  const [level, setLevel] = useState<LevelFilter>('all')
  const [q, setQ] = useState('')
  const [follow, setFollow] = useState(true)
  const [paused, setPaused] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const minLevel = level === 'all' ? 0 : LEVEL_ORDER.indexOf(level)
    return state.logs.filter((l) => {
      if (source !== 'all' && l.source !== source) return false
      if (level !== 'all' && LEVEL_ORDER.indexOf(l.level) < minLevel) return false
      if (needle && !l.message.toLowerCase().includes(needle)) return false
      return true
    })
  }, [state.logs, source, level, q])

  useEffect(() => {
    if (follow && !paused) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [visible.length, follow, paused])

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 pb-28 md:p-6 md:pb-12">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">Control link and device log</h2>
        <p className="text-xs text-muted-foreground">
          {state.logs.length} lines retained. Device log shares the control wire; bad-CRC log bytes are discarded by the frame parser.
        </p>
      </div>

      <Card>
        <CardHeader className="gap-3">
          <CardTitle className="text-sm">Filters</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="log-source" className="text-xs">
                Source
              </Label>
              <Select value={source} onValueChange={(v) => setSource(v as SourceFilter)}>
                <SelectTrigger id="log-source" className="w-[10rem]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">Everything</SelectItem>
                  <SelectItem value="container">Container</SelectItem>
                  <SelectItem value="device">Device log</SelectItem>
                  <SelectItem value="frame">Frames</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="log-level" className="text-xs">
                Minimum level
              </Label>
              <Select value={level} onValueChange={(v) => setLevel(v as LevelFilter)}>
                <SelectTrigger id="log-level" className="w-[10rem]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">Everything</SelectItem>
                  <SelectItem value="info">Info and above</SelectItem>
                  <SelectItem value="warn">Warnings and above</SelectItem>
                  <SelectItem value="error">Errors only</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid min-w-[12rem] flex-1 gap-1.5">
              <Label htmlFor="log-search" className="text-xs">
                Search
              </Label>
              <div className="relative">
                <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
                <Input id="log-search" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter lines…" className="pl-8" />
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <Switch id="log-follow" checked={follow} onCheckedChange={setFollow} />
              <Label htmlFor="log-follow" className="text-xs">
                Follow new lines
              </Label>
            </div>
            <Button type="button" variant="outline" size="sm" onClick={() => setPaused((p) => !p)}>
              {paused ? <Play aria-hidden /> : <Pause aria-hidden />}
              {paused ? 'Resume' : 'Pause'}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={clearLogs}>
              <Trash2 aria-hidden />
              Clear
            </Button>
            <span className="ml-auto text-xs text-muted-foreground">
              Showing {visible.length} of {state.logs.length}
            </span>
          </div>
        </CardContent>
      </Card>

      <Card className="overflow-hidden">
        <div
          className="max-h-[60vh] min-h-[16rem] overflow-auto bg-card"
          role="log"
          tabIndex={0}
          aria-live={follow && !paused ? 'polite' : 'off'}
          aria-label="Log lines"
        >
          {visible.length === 0 ? (
            <p className="p-6 text-center text-sm text-muted-foreground">No lines match the current filters.</p>
          ) : (
            <table className="w-full border-collapse text-left">
              <caption className="sr-only">Log lines, newest last</caption>
              <thead className="sticky top-0 z-10 bg-card">
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    t (s)
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Level
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Source
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Message
                  </th>
                </tr>
              </thead>
              <tbody className="font-mono text-xs">
                {visible.map((l) => (
                  <tr key={l.id} className="border-b border-border/60 last:border-0 align-top">
                    <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">{formatClock(l.t)}</td>
                    <td className="px-3 py-1.5">
                      <LevelTag level={l.level} />
                    </td>
                    <td className="px-3 py-1.5 whitespace-nowrap text-muted-foreground">{SOURCE_LABEL[l.source]}</td>
                    <td className="px-3 py-1.5 break-words text-foreground">{l.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div ref={bottomRef} />
        </div>
      </Card>
    </div>
  )
}
