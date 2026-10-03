import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarSeparator,
} from '@/components/ui/sidebar'
import { NAV } from '@/components/nav'
import { StatusBadge } from '@/components/status'
import { useStore } from '@/app/store'
import { findFigure, findMacro, formatLoop } from '@/lib/model'
import { Gamepad2, ScanLine, Timer } from 'lucide-react'

export function AppSidebar() {
  const { view, setView, state } = useStore()
  const macro = findMacro(state, state.selectedMacroId)
  const figure = findFigure(state, state.placement?.figureId ?? state.selectedFigureId)

  const deviceLabel =
    state.control.link === 'UP'
      ? `fw ${state.firmware?.fwVersion ?? '—'}`
      : state.control.link === 'BUSY'
        ? 'port busy'
        : 'no device'

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" tooltip="NS2 Controller" onClick={() => setView('control')}>
              <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                <Gamepad2 className="size-4" aria-hidden />
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-medium">NS2 Controller</span>
                <span className="truncate text-xs text-muted-foreground">{deviceLabel}</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Console</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV.map((item) => (
                <SidebarMenuItem key={item.id}>
                  <SidebarMenuButton isActive={view === item.id} tooltip={item.label} onClick={() => setView(item.id)}>
                    <item.icon aria-hidden />
                    <span>{item.label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarSeparator />

      <SidebarFooter className="gap-2 pb-3">
        <div className="flex flex-col gap-1.5 group-data-[collapsible=icon]:hidden">
          <span className="px-1 text-xs font-medium text-muted-foreground">
            {state.mode === 'MACRO' ? 'Running' : state.mode === 'AMIIBO' ? 'Tag placed' : 'Idle'}
          </span>
          {state.mode === 'MACRO' && macro ? (
            <StatusBadge tone="success" icon={Timer}>
              <span className="max-w-[10rem] truncate">{macro.name}</span>
            </StatusBadge>
          ) : null}
          {state.mode === 'AMIIBO' && figure ? (
            <StatusBadge tone="success" icon={ScanLine}>
              <span className="max-w-[10rem] truncate">{figure.name}</span>
            </StatusBadge>
          ) : null}
          {state.mode === 'IDLE' ? (
            <span className="px-1 text-xs text-muted-foreground">
              {state.plan ? `Plan loaded (${formatLoop(macro?.loopMs ?? 0)})` : state.placement ? 'Tag placed' : 'Nothing running'}
            </span>
          ) : null}
        </div>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
