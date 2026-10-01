import { ScenarioSelect } from '@/components/scenario-select'
import { ConsoleLinkBadge, ControlLinkBadge, ModeBadge } from '@/components/status'
import { ThemeToggle } from '@/components/theme-toggle'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { NAV } from '@/components/nav'
import { FlaskConical } from 'lucide-react'
import { useStore } from '@/prototype/store'
import { cn } from '@/lib/utils'

export function AppHeader() {
  const { view, state } = useStore()
  const title = NAV.find((n) => n.id === view)?.label ?? 'Control'

  return (
    <header className="sticky top-0 z-30 border-b border-border bg-background/95 backdrop-blur">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2 px-3 py-2 md:px-4">
        <SidebarTrigger className="hidden md:inline-flex" />
        <h1 className="text-base font-semibold tracking-tight">{title}</h1>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
          <ControlLinkBadge link={state.control.link} heldBy={state.control.heldBy} />
          <ConsoleLinkBadge link={state.console.link} bonded={state.console.bonded} />
          <ModeBadge mode={state.mode} />
          <ThemeToggle />
        </div>
      </div>
      <div
        className={cn(
          'flex items-center gap-2 border-t border-dashed border-border px-3 py-1 md:px-4',
          'text-muted-foreground',
        )}
      >
        <FlaskConical className="size-3.5 shrink-0" aria-hidden />
        <span className="text-xs font-medium">Prototype</span>
        <span className="hidden text-xs sm:inline">— jump the simulated device between states</span>
        <ScenarioSelect />
      </div>
    </header>
  )
}
