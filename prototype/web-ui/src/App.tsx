import { AppHeader } from '@/components/app-header'
import { AppSidebar } from '@/components/app-sidebar'
import { MobileNav } from '@/components/mobile-nav'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { useStore } from '@/prototype/store'
import { ConnectionView } from '@/views/connection-view'
import { ControlView } from '@/views/control-view'
import { LogsView } from '@/views/logs-view'
import { SettingsView } from '@/views/settings-view'

export default function App() {
  const { view } = useStore()
  return (
    <SidebarProvider>
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-primary-foreground"
      >
        Skip to main content
      </a>
      <AppSidebar />
      <SidebarInset id="main-content" tabIndex={-1}>
        <AppHeader />
        {view === 'control' ? <ControlView /> : null}
        {view === 'logs' ? <LogsView /> : null}
        {view === 'connection' ? <ConnectionView /> : null}
        {view === 'settings' ? <SettingsView /> : null}
      </SidebarInset>
      <MobileNav />
    </SidebarProvider>
  )
}
