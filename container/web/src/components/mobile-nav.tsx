import { NAV } from '@/components/nav'
import { cn } from '@/lib/utils'
import { useStore } from '@/app/store'

/** Mobile tab bar: 3–5 core destinations, always the same place (NN/g). */
export function MobileNav() {
  const { view, setView } = useStore()
  return (
    <nav
      aria-label="Primary"
      className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden"
    >
      <ul className="grid grid-cols-4">
        {NAV.map((item) => {
          const active = view === item.id
          return (
            <li key={item.id}>
              <button
                type="button"
                onClick={() => setView(item.id)}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'flex min-h-14 w-full flex-col items-center justify-center gap-1 py-2 text-xs font-medium transition-colors',
                  active ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                <item.icon className="size-5" aria-hidden />
                <span>{item.label}</span>
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
