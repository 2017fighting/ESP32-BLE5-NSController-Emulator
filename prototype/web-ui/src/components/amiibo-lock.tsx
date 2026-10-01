import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { CopyLine } from '@/components/copy-line'
import { cn } from '@/lib/utils'
import { MOUNTS, KEY_DIR, KEY_FILE } from '@/prototype/mounts'
import { KeyRound, Library, RefreshCw, TriangleAlert, Cpu } from 'lucide-react'
import type { DeviceState } from '@/prototype/model'

export type AmiiboLockKind = 'none' | 'key-absent' | 'key-invalid' | 'key-unverified' | 'library-empty' | 'no-feature'

export function amiiboLock(state: DeviceState): AmiiboLockKind {
  if (state.firmware && !state.firmware.features.amiibo) return 'no-feature'
  if (state.key === 'KEY_ABSENT') return 'key-absent'
  if (state.key === 'KEY_INVALID') return 'key-invalid'
  if (state.amiiboLibrary === 'EMPTY') return 'library-empty'
  if (state.key === 'KEY_UNVERIFIED') return 'key-unverified'
  return 'none'
}

export function amiiboBlocked(kind: AmiiboLockKind): boolean {
  return kind !== 'none' && kind !== 'key-unverified'
}

/**
 * The three amiibo unavailability states are distinct and each names the ONE
 * action that fixes it (#13). Collapsing them sends the user to the wrong fix.
 */
export function AmiiboLockNotice({ kind, onRescan }: { kind: AmiiboLockKind; onRescan: () => void }) {
  if (kind === 'none') return null

  if (kind === 'key-unverified') {
    return (
      <Alert id="amiibo-key" className={cn('border-warning/35 bg-warning/10 text-warning dark:border-warning/25 dark:bg-warning/15')}>
        <TriangleAlert aria-hidden className="text-warning" />
        <AlertTitle>Key not verified yet</AlertTitle>
        <AlertDescription className="text-warning/90">
          No library tag was available to check the key against, so it is only verified at the first placement. Amiibo is available;
          a bad key will be reported then and the placement refused.
        </AlertDescription>
      </Alert>
    )
  }

  if (kind === 'key-absent' || kind === 'key-invalid') {
    const absent = kind === 'key-absent'
    return (
      <Alert variant="destructive" id="amiibo-key">
        <KeyRound aria-hidden />
        <AlertTitle>{absent ? 'Amiibo is locked: no key material' : 'Amiibo is locked: key invalid'}</AlertTitle>
        <AlertDescription className="space-y-3">
          <p>
            {absent
              ? `The container reads its key once at startup. Mount your own key_retail.bin read-only at ${KEY_FILE} and restart the container. This app never uploads, stores or transmits it.`
              : `The file at ${KEY_DIR} failed the nfc3d unpack round-trip, so it is not a usable retail key. Two spellings are accepted: the single 160 B key_retail.bin, or unfixed-info.bin and locked-secret.bin concatenated in that order.`}
          </p>
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-foreground">Single file</p>
            <CopyLine value={MOUNTS.keyFile} label="key mount" />
            <p className="text-xs font-medium text-foreground">Or the two-file spelling</p>
            <CopyLine value={MOUNTS.keyDir} label="key directory mount" />
          </div>
          <p className="text-xs">Then restart the container — the key is not hot-reloaded by design.</p>
        </AlertDescription>
      </Alert>
    )
  }

  if (kind === 'library-empty') {
    return (
      <Alert variant="destructive" id="amiibo-library">
        <Library aria-hidden />
        <AlertTitle>Amiibo is locked: no figures found</AlertTitle>
        <AlertDescription className="space-y-3">
          <p>No .bin files were found under the amiibo library mount. Mount the Amiibo clone and rescan.</p>
          <CopyLine value={MOUNTS.amiibo} label="amiibo library mount" />
          <Button type="button" variant="outline" size="sm" onClick={onRescan}>
            <RefreshCw aria-hidden />
            Rescan library
          </Button>
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <Alert variant="destructive" id="amiibo-feature">
      <Cpu aria-hidden />
      <AlertTitle>Amiibo is locked: this firmware has no NFC path</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>
          The connected board reported <span className="font-mono">features.amiibo = false</span> in HELLO. Macro mode still works on
          this firmware; presenting a tag needs the build with the NFC command path.
        </p>
        <p className="text-xs">Flash the NS2 firmware that implements command 0x01, then reconnect.</p>
      </AlertDescription>
    </Alert>
  )
}
