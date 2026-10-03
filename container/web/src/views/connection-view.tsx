import { CopyLine } from '@/components/copy-line'
import { ErrorSummary, type SummaryError } from '@/components/error-summary'
import { ConsoleLinkBadge, ControlLinkBadge, StatusBadge } from '@/components/status'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { formatBytes, type DeviceState } from '@/lib/model'
import { FLASH_COMMAND } from '@/lib/mounts'
import { useStore } from '@/app/store'
import { AlertTriangle, Bluetooth, Cable, Info, Link2Off, RefreshCw, Usb, Zap } from 'lucide-react'
import type { ReactNode } from 'react'

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/60 py-1.5 last:border-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-right text-sm break-all">{children}</dd>
    </div>
  )
}

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-xs">{children}</span>
}

function connectionErrors(state: DeviceState): SummaryError[] {
  const errors: SummaryError[] = []
  if (state.lastError && ['VER_MISMATCH', 'NEW_POWER'].includes(state.lastError.code)) {
    errors.push({ id: 'recovery', text: state.lastError.message })
  }
  if (state.control.link === 'BUSY') {
    errors.push({ id: 'control-link', text: `The serial port is held by ${state.control.heldBy ?? 'another process'}.` })
  }
  if (state.control.link === 'DOWN') {
    errors.push({ id: 'control-link', text: 'No board answered on the serial port.' })
  }
  return errors
}

function ControlLinkCard() {
  const { state, reconnect } = useStore()
  const { control } = state
  return (
    <Card id="control-link">
      <CardHeader className="gap-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Usb className="size-4" aria-hidden />
            Control link
          </CardTitle>
          <ControlLinkBadge link={control.link} heldBy={control.heldBy ?? undefined} />
        </div>
        <CardDescription>One wire carries control, the device log and flashing.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl>
          <Fact label="Port">
            <Mono>{control.port}</Mono>
          </Fact>
          <Fact label="Baud">
            <Mono>{control.baud}</Mono> <span className="text-muted-foreground">(115200 fallback)</span>
          </Fact>
          <Fact label="DTR / RTS">
            <StatusBadge tone="success">deasserted</StatusBadge>
          </Fact>
          {control.heldBy ? (
            <Fact label="Held by">
              <Mono>{control.heldBy}</Mono>
            </Fact>
          ) : null}
        </dl>

        {control.link === 'BUSY' ? (
          <div className="flex items-start gap-2 rounded-md border border-warning/35 bg-warning/10 p-3 text-sm text-warning dark:border-warning/25 dark:bg-warning/15">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <div className="space-y-1">
              <p className="font-medium">Stop the process holding the port</p>
              <p>
                Flashing and monitoring take the same wire. Stop <Mono>{control.heldBy ?? 'the other process'}</Mono>, then reconnect.
              </p>
            </div>
          </div>
        ) : null}

        {control.link === 'DOWN' ? (
          <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive dark:bg-destructive/20">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <div className="space-y-1">
              <p className="font-medium">No device on the port</p>
              <p>
                This fails silently: if DTR or RTS are asserted, the CH9102 holds the board in reset and it looks exactly like an unplugged
                board. The container always deasserts them, so check the cable, then the flash.
              </p>
              <ul className="list-disc space-y-0.5 pl-4">
                <li>Use a data cable, not a charge-only cable.</li>
                <li>Confirm the board is flashed and was not left in download mode.</li>
                <li>If you just ran <Mono>idf.py flash</Mono>, the container must be restarted — it lost the port.</li>
              </ul>
            </div>
          </div>
        ) : null}

        <Button type="button" variant="outline" size="sm" onClick={reconnect}>
          <RefreshCw aria-hidden />
          Reconnect
        </Button>
      </CardContent>
    </Card>
  )
}

function PairingCard() {
  const { state, requestUnpair } = useStore()
  const { console: link } = state
  return (
    <Card id="pairing">
      <CardHeader className="gap-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Bluetooth className="size-4" aria-hidden />
            Console link and pairing
          </CardTitle>
          <ConsoleLinkBadge link={link.link} bonded={link.bonded} />
        </div>
        <CardDescription>The firmware owns bonding. This app observes it and can only ask the board to forget.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl>
          <Fact label="Bond">
            {link.bonded ? (
              <StatusBadge tone="success">Paired</StatusBadge>
            ) : (
              <StatusBadge tone="neutral" icon={Link2Off}>
                Not paired
              </StatusBadge>
            )}
          </Fact>
          <Fact label="Console link">
            <Mono>{link.link.toLowerCase()}</Mono>
          </Fact>
          {link.lastDropReason ? (
            <Fact label="Last drop">
              <Mono>{link.lastDropReason}</Mono>
            </Fact>
          ) : null}
        </dl>

        <div className="flex items-start gap-2 rounded-md border border-info/30 bg-info/10 p-3 text-sm text-info dark:border-info/25 dark:bg-info/15">
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
          <div className="space-y-1">
            <p className="font-medium">What this app can and cannot do</p>
            <ul className="list-disc space-y-0.5 pl-4">
              <li>It cannot start pairing — you pair from the console’s controller menu.</li>
              <li>It can request that the board forget its bond; the console keeps its own copy.</li>
              <li>It reports the link and the bond; it never guesses them.</li>
              <li>If the console drops mid-macro, the app stops the run and will not restart it — the console re-initialises itself on reconnect.</li>
              <li>A placed amiibo survives a drop and is rotated to a fresh identity before the console’s next scan.</li>
            </ul>
          </div>
        </div>

        <Dialog>
          <DialogTrigger asChild>
            <Button type="button" variant="destructive" size="sm" disabled={!link.bonded}>
              <Link2Off aria-hidden />
              Request unpair
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Ask the board to forget its bond?</DialogTitle>
              <DialogDescription>
                The board drops the stored pairing key and starts advertising unpaired. The console still holds its side, so remove the
                controller there as well. Re-pairing is done from the console.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button type="button" variant="outline">
                  Keep bond
                </Button>
              </DialogClose>
              <DialogClose asChild>
                <Button type="button" variant="destructive" onClick={requestUnpair}>
                  Unpair board
                </Button>
              </DialogClose>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  )
}

const RECOVERY: { id: DeviceState['recovery']; title: string; body: string }[] = [
  { id: 'SAME_POWER', title: 'Same power', body: 'The board re-enumerated without rebooting. Volatile state is intact — the container re-reads STATUS and keeps the UI.' },
  { id: 'NEW_POWER', title: 'New power', body: 'boot_id changed: the board lost power and its plan and tag were cleared. Re-upload before starting.' },
  { id: 'DIFFERENT_FIRMWARE', title: 'Different firmware', body: 'The board is running another build. Capabilities may differ; the app announces it and degrades instead of failing.' },
]

function RecoveryCard() {
  const { state, reconnect } = useStore()
  return (
    <Card id="recovery">
      <CardHeader className="gap-1">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Zap className="size-4" aria-hidden />
          Recovery after a reconnect
        </CardTitle>
        <CardDescription>Every reconnect is container-led: HELLO, then the boot_id decides which of three cases this is.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <ul className="space-y-2">
          {RECOVERY.map((r) => {
            const active = state.recovery === r.id
            return (
              <li
                key={r.id}
                className={cn(
                  'rounded-lg border p-3',
                  active ? 'border-ring bg-accent/50' : 'border-border',
                )}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{r.title}</span>
                  {active ? <StatusBadge tone="info">Current</StatusBadge> : null}
                </div>
                <p className="mt-1 text-sm text-muted-foreground">{r.body}</p>
              </li>
            )
          })}
        </ul>
        <Button type="button" variant="outline" size="sm" onClick={reconnect}>
          <RefreshCw aria-hidden />
          Run HELLO now
        </Button>
      </CardContent>
    </Card>
  )
}

function DeviceCard() {
  const { state } = useStore()
  const fw = state.firmware
  return (
    <Card id="device">
      <CardHeader className="gap-1">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Cable className="size-4" aria-hidden />
          Device
        </CardTitle>
        <CardDescription>Read from HELLO. Nothing here is configurable from the app.</CardDescription>
      </CardHeader>
      <CardContent>
        {fw ? (
          <dl>
            <Fact label="Firmware">fw {fw.fwVersion}</Fact>
            <Fact label="Protocol">proto_ver {fw.protoVer}</Fact>
            <Fact label="Boot id">
              <Mono>{fw.bootId}</Mono>
            </Fact>
            <Fact label="Max frame">
              <Mono>{formatBytes(fw.maxFrame)}</Mono>
            </Fact>
            <Fact label="Chunk size">
              <Mono>{fw.chunkSize} B</Mono>
            </Fact>
            <Fact label="Plan capacity">
              <Mono>{formatBytes(fw.planCapacityBytes)}</Mono>
            </Fact>
            <Fact label="Features">
              <span className="flex flex-wrap justify-end gap-1">
                <StatusBadge tone={fw.features.macro ? 'success' : 'neutral'}>macro</StatusBadge>
                <StatusBadge tone={fw.features.amiibo ? 'success' : 'danger'}>amiibo</StatusBadge>
                <StatusBadge tone={fw.features.config ? 'success' : 'neutral'}>config</StatusBadge>
              </span>
            </Fact>
          </dl>
        ) : (
          <p className="text-sm text-muted-foreground">No capabilities: the board has not answered HELLO.</p>
        )}
      </CardContent>
    </Card>
  )
}

export function ConnectionView() {
  const { state } = useStore()
  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4 pb-28 md:p-6 md:pb-12">
      <ErrorSummary errors={connectionErrors(state)} />
      <div className="grid gap-4 lg:grid-cols-2">
        <ControlLinkCard />
        <PairingCard />
        <RecoveryCard />
        <DeviceCard />
      </div>
      <Card>
        <CardHeader className="gap-1">
          <CardTitle className="text-sm">Operating rules</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted-foreground">
          <p>Flashing needs the port, so the container must be stopped first — and holding the port asserted resets the board.</p>
          <CopyLine value={FLASH_COMMAND} label="flash command" />
        </CardContent>
      </Card>
    </div>
  )
}
