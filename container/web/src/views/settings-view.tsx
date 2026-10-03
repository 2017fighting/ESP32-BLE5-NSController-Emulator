import { CopyLine } from '@/components/copy-line'
import { KeyStateBadge, StatusBadge } from '@/components/status'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { useStore } from '@/app/store'
import { MOUNTS, KEY_FILE } from '@/lib/mounts'
import { KeyRound, FolderInput, Save, SlidersHorizontal } from 'lucide-react'
import { useState } from 'react'

const INTERVALS = [5, 10, 15, 20]

export function SettingsView() {
  const { state, saveConfig } = useStore()
  const [interval, setIntervalMs] = useState(String(state.config.reportIntervalMs))
  const [led, setLed] = useState(state.config.led)
  const dirty = Number(interval) !== state.config.reportIntervalMs || led !== state.config.led

  return (
    <div className="mx-auto w-full max-w-4xl space-y-4 p-4 pb-28 md:p-6 md:pb-12">
      <Card id="config">
        <CardHeader className="gap-1">
          <CardTitle className="flex items-center gap-2 text-sm">
            <SlidersHorizontal className="size-4" aria-hidden />
            Device config
          </CardTitle>
          <CardDescription>Volatile: sent to the board and re-sent on every reconnect. Applied at the next loop boundary.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-1.5">
              <Label htmlFor="report-interval">HID report interval</Label>
              <Select value={interval} onValueChange={setIntervalMs}>
                <SelectTrigger id="report-interval" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {INTERVALS.map((ms) => (
                    <SelectItem key={ms} value={String(ms)}>
                      {ms} ms{ms === 5 ? ' (target)' : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                The console may renegotiate the connection interval; the app reports what it observes, not what it asked for.
              </p>
            </div>
            <div className="flex items-start justify-between gap-3 rounded-lg border border-border p-3">
              <div className="space-y-1">
                <Label htmlFor="led">Status LED</Label>
                <p className="text-xs text-muted-foreground">On the board. Off is useful when the panel glows in a dark room.</p>
              </div>
              <Switch id="led" checked={led} onCheckedChange={setLed} />
            </div>
          </div>
          <Button type="button" onClick={() => saveConfig({ reportIntervalMs: Number(interval), led })} disabled={!dirty}>
            <Save aria-hidden />
            Save config
          </Button>
        </CardContent>
      </Card>

      <Card id="mounts">
        <CardHeader className="gap-1">
          <CardTitle className="flex items-center gap-2 text-sm">
            <FolderInput className="size-4" aria-hidden />
            Mounts
          </CardTitle>
          <CardDescription>Read-only host mounts. Nothing here is uploaded, stored or transmitted by the app.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-sm font-medium">Key material</p>
              <KeyStateBadge state={state.key} />
              {state.keySpelling ? <span className="text-xs text-muted-foreground">read as {state.keySpelling}</span> : null}
            </div>
            <CopyLine value={MOUNTS.keyFile} label="key mount" />
            <p className="text-xs text-muted-foreground">Read once at startup from {KEY_FILE}. Changing it requires a container restart.</p>
          </div>

          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-sm font-medium">Macro library</p>
              <StatusBadge tone={state.macroLibrary === 'READY' ? 'success' : 'danger'}>
                {state.macroLibrary === 'READY' ? 'mounted' : 'empty'}
              </StatusBadge>
            </div>
            <CopyLine value={MOUNTS.macros} label="macro mount" />
          </div>

          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-sm font-medium">Amiibo library</p>
              <StatusBadge tone={state.amiiboLibrary === 'READY' ? 'success' : 'danger'}>
                {state.amiiboLibrary === 'READY' ? `${state.figures.length} tags` : 'empty'}
              </StatusBadge>
            </div>
            <CopyLine value={MOUNTS.amiibo} label="amiibo mount" />
          </div>
        </CardContent>
      </Card>

      <Card id="about">
        <CardHeader className="gap-1">
          <CardTitle className="flex items-center gap-2 text-sm">
            <KeyRound className="size-4" aria-hidden />
            About
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm text-muted-foreground">
          <p>Local container tied to one board over USB serial. No account, no Wi-Fi, no remote access — the cable is the trust boundary.</p>
          <p className="font-mono text-xs">proto_ver {state.firmware?.protoVer ?? '—'} · app 0.1.0</p>
        </CardContent>
      </Card>
    </div>
  )
}
