# Web UI prototype — issue #7

A throwaway prototype of the container's web console, built to answer
[#7](https://github.com/2017fighting/ESP32-BLE5-NSController-Emulator/issues/7)
by feeling the screens rather than describing them. **This is not product code.**
The map's destination is a locked design (map Q1); this directory is the
prototype artifact, and the decisions it produced live in
[`docs/research/container-and-web-ui.md`](../../docs/research/container-and-web-ui.md).

Stack: Vite + React 19 + TypeScript (strict) + Tailwind v4 + shadcn/ui
(`radix-nova` preset, neutral base, Geist), plus Noto Sans SC for the real
Chinese macro filenames. The device is **simulated** — there is no serial code
here; the store mimics the verbs and legal sequencing fixed by #5/#6.

## Run it

```sh
npm install
npm run dev            # http://localhost:5173
# or, for a production build served as static files:
npm run build && npx vite preview --host
```

Every state is deep-linkable, so a reviewer can open one exact screen:

```
/?scenario=key-absent&view=control&theme=light
```

`scenario` is one of the ids in `src/prototype/model.ts` (`SCENARIOS`), `view` is
`control | logs | connection | settings`, `theme` is `dark | light`. The
`Prototype` strip under the header exposes the same scenario switch.

## Design control plane

[`ui-contract.md`](ui-contract.md) is the cross-screen consistency contract:
tokens, spacing/type scales, component invariants, status semantics, cited UX
rules and anti-slop guardrails. `ui-contract.tokens.json` is the shadcn preset's
upstream DTCG snapshot kept for provenance; the live token authority is
`src/index.css`.

## Review harness (the TEST step)

The bundled `score_mockup` tool cannot import Playwright from this repo, so the
loop uses two scripts with the same rubric plus the rendered a11y gate:

```sh
# 88 screenshots across 22 states × 2 themes × 3 widths + axe-core
LD_LIBRARY_PATH=/tmp/chromedeps/usr/lib node scripts/capture.mjs --url http://localhost:4173
# focus visibility, target size, reduced-motion, zoom — checks axe cannot make
LD_LIBRARY_PATH=/tmp/chromedeps/usr/lib node scripts/gate-checks.mjs --url http://localhost:4173
```

`install-browser-deps.sh` fetches the browser system libraries this host lacks,
without root (Playwright's Chromium needs libatk/libxkbcommon/etc., and this
Arch box has no `atk` and no passwordless sudo). On a normal dev machine,
`npx playwright install-deps` is the equivalent.

Last recorded run:

- `capture.mjs` — **0 axe violations** (WCAG 2.0/2.1/2.2 A+AA) and **0 console errors** across 22 states × 2 themes × 3 widths.
- `gate-checks.mjs` — **7/7**: zoom enabled, primary action 44px, visible focus on all 18 focusable elements, no target < 24px, reduced-motion collapses transitions, both themes applied.
- shadcn L1 token-lint and L2 contrast gates — PASS (`validate_mockup{system:"shadcn"}`).

## Layout

```
src/
  index.css                 theme tokens (authority) + semantic status tokens
  prototype/
    model.ts                vocabulary + real fixtures + scenario builders
    store.tsx               simulated device, verbs, legal sequencing
  components/
    ui/                     shadcn components (generated)
    app-*.tsx               shell: sidebar, header, mobile nav
    status.tsx              status pills (icon + word, never colour alone)
    run-card.tsx            the one dominant action per view
    *-picker.tsx            macro / amiibo pickers
    amiibo-lock.tsx         the three amiibo unavailability states
  views/                    control, logs, connection, settings
scripts/
  capture.mjs               screenshots + axe
  gate-checks.mjs           deterministic a11y floor
  install-browser-deps.sh   local browser libs, no root
```
