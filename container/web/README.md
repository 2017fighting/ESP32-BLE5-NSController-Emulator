# The container's web UI

The four screens of the container console — **Control**, **Logs**,
**Connection**, **Settings** — served as static assets by `container/ns2web`
(spec §8.2, §8.7, §8.8).

This directory is the **product** UI, promoted from the throwaway prototype at
[`prototype/web-ui/`](../../prototype/web-ui) (issue #29's raised decision).
That prototype is where the design was measured — 0 axe violations across 22
states × 2 themes × 3 widths, 7/7 on the deterministic gate checks, shadcn L1/L2
token gates PASS — and it stays where it is as the historical artifact, with
its own scenario harness. What moved here is the source, with the
prototype-only affordances removed: the scenario selector, the
`?scenario`/`?theme` deep-links and the theme toggle. The shipped app follows
the OS theme (§8.8).

Stack: Vite + React 19 + TypeScript (strict) + Tailwind v4 + shadcn/ui
(`radix-nova`, neutral base, Geist), plus Noto Sans SC for the real Chinese
macro filenames.

## The one contract that matters

`src/lib/model.ts`'s `DeviceState` mirrors
`container/ns2container/state.py:Controller.snapshot()` key for key. If the two
drift, one of them is a bug — the state is sent whole (§8.7) and the client
does no merging of device truth.

The store (`src/app/store.tsx`) holds exactly two things the server does not:
the picker selection (nothing on the wire carries it) and the log ring. Every
verb goes through `src/lib/api.ts`, which is the browser's only door to the
container; the browser never talks to the device.

## Run it

```sh
npm ci
npm run dev            # http://localhost:5173, proxies nothing — point it at the container
# or
npm run build          # tsc -b && vite build -> dist/, which the container serves
```

`npm run build` is what the Dockerfile runs (stage 1); a type error fails the
image.

## Design control plane

[`ui-contract.md`](ui-contract.md) is the cross-screen consistency contract —
tokens, spacing/type scales, component invariants, status semantics, cited UX
rules and anti-slop guardrails. `ui-contract.tokens.json` is the shadcn
preset's upstream DTCG snapshot kept for provenance; the live token authority
is `src/index.css`.

## Layout

```
src/
  lib/
    model.ts       the TypeScript side of GET /api/state (§8.7)
    api.ts         fetch + SSE; the browser's only door to the container
    mounts.ts      the mount lines the Settings screen prints verbatim
  app/store.tsx    state + verbs, one SSE subscription
  components/      shell, status pills, pickers, the one dominant action
  components/ui/   shadcn components
  views/           control, logs, connection, settings
```
