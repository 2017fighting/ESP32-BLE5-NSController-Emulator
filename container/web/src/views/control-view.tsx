import { AmiiboLockNotice, amiiboLock } from '@/components/amiibo-lock'
import { AmiiboPicker } from '@/components/amiibo-picker'
import { ErrorSummary, type SummaryError } from '@/components/error-summary'
import { MacroPicker } from '@/components/macro-picker'
import { ModeSelector, type PendingMode } from '@/components/mode-selector'
import { MobileActionBar, RunCard } from '@/components/run-card'
import { useStore } from '@/app/store'
import type { DeviceState } from '@/lib/model'
import { useState } from 'react'

function defaultPending(state: DeviceState): PendingMode {
  if (state.mode === 'MACRO' || state.mode === 'AMIIBO') return state.mode
  return 'MACRO'
}

function controlErrors(state: DeviceState): SummaryError[] {
  if (!state.lastError) return []
  const { code, message } = state.lastError
  const anchor =
    code === 'BAD_PLAN' && state.plan?.macroId
      ? `macro-${state.plan.macroId}`
      : code === 'LIBRARY_EMPTY'
        ? 'macro-library'
        : code.startsWith('KEY_')
          ? 'amiibo-key'
          : 'run-card'
  return [{ id: anchor, text: message }]
}

export function ControlView() {
  const { state, selectMacro, selectFigure, rescan } = useStore()
  const [pending, setPending] = useState<PendingMode>(() => defaultPending(state))

  // Adjust the selector when the running mode changes. Doing it during render
  // (React's "adjust state on prop change" pattern) keeps a running mode
  // authoritative without an extra render pass from an effect.
  const [lastMode, setLastMode] = useState(state.mode)
  if (lastMode !== state.mode) {
    setLastMode(state.mode)
    setPending(defaultPending(state))
  }

  const lock = amiiboLock(state)

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 p-4 pb-32 md:p-6 md:pb-12">
      <ErrorSummary errors={controlErrors(state)} />

      <ModeSelector mode={state.mode} pending={pending} onSelect={setPending} />

      {pending === 'AMIIBO' ? <AmiiboLockNotice kind={lock} onRescan={() => void rescan()} /> : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        {pending === 'MACRO' ? (
          <MacroPicker selectedId={state.selectedMacroId} onSelect={selectMacro} />
        ) : (
          <AmiiboPicker
            selectedId={state.selectedFigureId}
            onSelect={selectFigure}
            locked={lock !== 'none' && lock !== 'key-unverified'}
          />
        )}
        <RunCard pending={pending} />
      </div>

      <MobileActionBar pending={pending} />
    </div>
  )
}
