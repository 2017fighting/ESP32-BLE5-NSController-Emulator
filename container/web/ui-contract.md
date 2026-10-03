# UI Contract — NS2 Controller container console

Single source of truth for cross-screen visual + interaction consistency in the
`container/web` product UI (promoted from the `prototype/web-ui` prototype by
issue #29). Every value a screen uses **references a token** —
never a raw hex or pixel literal. If a surface needs a value that is not here,
add the token to the theme layer first, then cite it here.

> **Token authority.** The live theme is `container/web/src/index.css`
> (shadcn/ui `radix-nova` preset — neutral base, Geist — plus the semantic
> tokens this product adds). `ui-contract.tokens.json` in this directory is the
> preset's upstream DTCG snapshot, kept for provenance; where the two differ,
> **`src/index.css` wins**.

> **Two deliberate deviations from the preset defaults**, both forced by the
> WCAG-AA gate (axe found them; do not revert): light-mode `--muted-foreground`
> is darkened from the preset's L 0.556 to **L 0.46** so muted text clears
> 4.5:1 on `--muted` surfaces (badges, log level chips, inline notes), and
> light-mode `--destructive` from L 0.577 to **L 0.50** so destructive text
> clears 4.5:1 on its own `/10` tint. Both are token-layer changes, so they
> propagate everywhere rather than being patched per component.

## Direction (GROUND)

An **instrument panel for one local device**, not a marketing page and not a
multi-tenant dashboard. Dark-first (the screen sits next to a TV), neutral
surface, colour reserved for **state** only. Deliberate reference: the reference
project's own control panel (`switch-controller-macro/web/index.html` — dark,
panel-based, status chip in the header), adapted to shadcn and *cut down*: the
reference carries recording, key-mapping and gamepad binding, all of which are
out of scope here (map Q8). We keep its shape, drop its authoring tools.

Cited rules behind the shape:

| Decision | Rule |
| --- | --- |
| Sidebar + inset content, header status | Jakob's Law — work like the dashboards users know (lawsofux.com/jakobs-law) |
| One primary action per view | Von Restorff + Nielsen H8 — one isolated focal action (nngroup.com/articles/ten-usability-heuristics) |
| Mode choice collapsed to two options | Hick's Law — decision time grows with choices (lawsofux.com/hicks-law) |
| Status always visible in the header | Nielsen H1 — visibility of system status |
| Status conveyed by icon + text, not colour alone | WCAG 2.2 SC 1.4.1 (w3.org/TR/WCAG22) |
| Errors: inline + linked summary | GOV.UK error summary pattern (design-system.service.gov.uk/components/error-summary) |
| Empty/locked states name the fix | Nielsen H9 + NN/g empty-state (nngroup.com/articles/empty-state-interface-design) |
| Reconnect/upload shows determinate progress + cancel | NN/g response-time limits (nngroup.com/articles/response-times-3-important-limits) |
| Mobile bottom bar ≤ 5 destinations | NN/g hamburger/mobile nav (nngroup.com/articles/hamburger-menus) |
| Copy follows `CONTEXT.md` vocabulary | Nielsen H2 (match real world) + this repo's glossary |

## Tokens (roles)

| Role | Token(s) | Notes |
| --- | --- | --- |
| page surface | `--background`, `--foreground` | app canvas |
| card / panel | `--card`, `--card-foreground`, `--border` | every panel |
| muted surface | `--muted`, `--muted-foreground` | secondary text, chips, empty-state art |
| popover / overlay | `--popover`, `--popover-foreground` | dialogs, sheets, dropdowns |
| primary action | `--primary`, `--primary-foreground` | the one dominant button per view |
| secondary action | `--secondary`, `--secondary-foreground` | subordinate buttons |
| destructive | `--destructive` | unpair, discard — irreversible only |
| focus | `--ring` | every focusable element |
| sidebar | `--sidebar`… `--sidebar-ring` | nav rail |
| **live / running** | `--success` | connected link, active mode, passing check |
| **degraded / pending** | `--warning` | unverified key, stopped-at-board, mid-upload |
| **observed / informational** | `--info` | firmware-owned facts the UI merely observes |
| numeric / log text | `--font-mono` | telemetry, plan bytes, UIDs, log lines |

`--success` / `--warning` / `--info` are **product tokens added to the theme**,
not decorative accents. They are the only non-neutral colour on a resting
screen, and each always appears with an icon and a word (never colour alone).

## Spacing scale

Tailwind's numeric scale only (`gap-1/2/3/4/6/8`, `p-*`, `space-y-*`). No
arbitrary `[...]` spacing. Within-group spacing is tighter than between-group
(Proximity): panel padding `p-4`, panel gap `gap-4`, section gap `gap-6`.

## Type scale

| Step | Recipe (tokens only) |
| --- | --- |
| page title | `text-2xl font-semibold tracking-tight` |
| section title | `text-sm font-semibold` (card headers) |
| body | `text-sm` |
| secondary | `text-sm text-muted-foreground` |
| meta / label | `text-xs text-muted-foreground` |
| figure/list name | `text-sm font-medium` |
| telemetry / log | `font-mono text-xs` |

## Elevation

One tier. Cards use `bg-card` + `border-border` + `rounded-lg`; **no shadows for
structure** — elevation is not used to signal hierarchy (the reference's flat
panels already read correctly). Overlays (dialog, sheet, dropdown, popover) use
the shadcn default ring + border. One focal surface per view, never two.

## Radius

`--radius` only, through the shadcn scale (`rounded-sm/md/lg/xl`). Cards
`rounded-lg`; badges `rounded-4xl` (shadcn badge default); inputs, buttons, tabs
follow their component defaults. No bespoke corner values.

## Component invariants

| Component | Recipe (tokens only) |
| --- | --- |
| panel | `bg-card border-border rounded-lg`; header `text-sm font-semibold`; body `p-4` |
| primary action | shadcn `Button` (default variant), `size="lg"`, ≥44px tall, one per view |
| status pill | `Badge` + semantic token (`success`/`warning`/`destructive`/`info`) + lucide icon + word |
| mode badge | header `Badge`; `IDLE` = `secondary`, `MACRO`/`AMIIBO` = `success` |
| key/library notice | `Alert` with `warning`/`destructive` tints; body names the single fixing action |
| error summary | top-of-view `Alert` (`destructive`) listing each failure, each a link to its control |
| picker row | selectable `Card`/row with `aria-pressed`; selected = `ring-2 ring-ring` |
| log line | `font-mono text-xs`; level chip uses semantic token + label |
| progress | shadcn `Progress` + numeric offset + `Cancel`, only for staged uploads |
| dialog | shadcn `Dialog`; one primary action; destructive action uses `destructive` variant |
| empty state | `muted` icon + one-sentence outcome + exactly one primary CTA |

## Status semantics (must stay distinct)

- **Control link** (`IDLE`/`UP`/`BUSY`/`DOWN`) — USB serial to the board.
  `BUSY` means the port is held by another process; it is *not* the same as
  `DOWN` and has a different fix.
- **Console link** (`CONNECTED`/`ADVERTISING`/`DISCONNECTED`) — BLE to the NS2,
  plus a separate **bonded** boolean. The UI **observes**; the firmware owns
  pairing (Q12).
- **Mode** — `IDLE` | `MACRO` | `AMIIBO`.
- **Stop reason** — `CONTAINER_STOP` (info, normal) vs `BOOT_LOCAL`
  (warning: stopped by a person at the board, never auto-restarted) vs
  `CONSOLE_LOST` (warning: the container stopped the run because the console
  link dropped) vs `ERROR` (destructive). `BOOT_LOCAL` is a distinct reason, not
  an error; with an error also pending it reads as both — stopped by hand *and*
  broken.
- **Key state** — `KEY_READY` | `KEY_ABSENT` | `KEY_INVALID` | `KEY_UNVERIFIED`,
  rendered **separately** from `LIBRARY_EMPTY` and from
  `features.amiibo == false`. Three different problems, three different fixes.
- **Recovery** — `SAME_POWER` (keep state, re-`STATUS`), `NEW_POWER` (state
  cleared, re-upload), `DIFFERENT_FIRMWARE` (announce; capabilities may differ).

## Motion

`transition-colors` / `transition-opacity` at Tailwind's default duration for
hover and pending feedback only. No entrance animation on data (the 2 Hz
`STATUS` poll already reads as live). `prefers-reduced-motion: reduce` collapses
all animation and transitions, declared once in `index.css`.

## Anti-slop guardrails

- No purple gradient, no centred hero, no decorative illustration.
- Geist (not Inter); mono for telemetry. Real macro names and real amiibo
  figures from the pinned corpus — never "Acme" or "Jane Doe" fixtures.
- No element that does not serve the current goal (Nielsen H8 / extraneous
  cognitive load).
- Contrast verified WCAG-AA in **both** themes via axe before "done".
- One dominant action per view; everything else is subordinate.

## Screen inventory (shared recipes)

| Screen | Focal action | Uses |
| --- | --- | --- |
| Control | Start macro / Place amiibo (toggles to Stop / Unplace) | mode selector, picker, progress, error summary |
| Logs | none (read-only) | log line, level chip, filters, sticky header |
| Connection | Reconnect / Unpair (contextual) | status pills, observed-state cards, recovery list |
| Settings | Save config (applies at loop boundary) | form rows, mount list, read-only device facts |
