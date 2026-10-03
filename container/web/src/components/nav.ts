import { Cable, Gauge, ScrollText, SlidersHorizontal, type LucideIcon } from 'lucide-react'
import type { ViewId } from '@/app/store'

export interface NavItem {
  id: ViewId
  label: string
  description: string
  icon: LucideIcon
}

export const NAV: NavItem[] = [
  { id: 'control', label: 'Control', description: 'Mode, macro, amiibo', icon: Gauge },
  { id: 'logs', label: 'Logs', description: 'Link traffic and device log', icon: ScrollText },
  { id: 'connection', label: 'Connection', description: 'Links, pairing, recovery', icon: Cable },
  { id: 'settings', label: 'Settings', description: 'Report interval, LED, mounts', icon: SlidersHorizontal },
]
