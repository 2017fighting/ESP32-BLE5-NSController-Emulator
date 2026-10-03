// shadcn's `components.json` points its `utils` alias at `@/lib/utils`, while
// the radix-nova preset's generated components import `cn` from the `cn`
// package directly. One implementation, two import styles.
export { cn } from 'cn'
