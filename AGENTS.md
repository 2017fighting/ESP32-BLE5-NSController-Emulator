# AGENTS.md

## Agent skills

### Issue tracker

Issues live as GitHub issues on our fork (`2017fighting/ESP32-BLE5-NSController-Emulator`, tracked as the `origin` remote); always pass `--repo`, since several remotes are configured. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary, each label string equal to its role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` plus `docs/adr/` at the repo root, created lazily. See `docs/agents/domain.md`.

### External references

The external repos this effort reads — canonical, context and untrusted — are listed and pinned in `docs/references.md`. Cite them by alias (e.g. `switch2_controller_research/commands.md:47-100`), never by absolute path; local clones resolve under `$REFERENCE_ROOT` (default `~/clone`).
