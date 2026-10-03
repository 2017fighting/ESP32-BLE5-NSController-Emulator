import { ConsoleLinkBadge, ControlLinkBadge, ModeBadge } from '@/components/status'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { NAV } from '@/components/nav'
import { useStore } from '@/app/store'

export function AppHeader() {
  const { view, state } = useStore()
  const title = NAV.find((n) => n.id === view)?.label ?? 'Control'

  return (
    <header className="sticky top-0 z-30 border-b border-border bg-background/95 backdrop-blur">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2 px-3 py-2 md:px-4">
        <SidebarTrigger className="hidden md:inline-flex" />
        <h1 className="text-base font-semibold tracking-tight">{title}</h1>
        {/* Status is always visible in the header (Nielsen H1); its detail is on
            Connection. There is no theme toggle: the shipped app follows the OS
            setting (§8.8). */}
        <div className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
          <ControlLinkBadge link={state.control.link} heldBy={state.control.heldBy ?? undefined} />
          <ConsoleLinkBadge link={state.console.link} bonded={state.console.bonded} />
          <ModeBadge mode={state.mode} />
        </div>
      </div>
    </header>
  )
}
