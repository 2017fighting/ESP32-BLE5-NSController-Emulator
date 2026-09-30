# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Target repo

This clone has four remotes; `origin` is **upstream**, not where we track work:

| Remote    | Repo                                              | Role                          |
| --------- | ------------------------------------------------- | ----------------------------- |
| `origin`  | `zhantss/ESP32-BLE5-NSController-Emulator`        | upstream (do not target)      |
| `mine`    | `2017fighting/ESP32-BLE5-NSController-Emulator`   | **our fork — target this**    |
| `fix`     | `xioxin/ESP32-BLE5-NSController-Emulator`         | reference fork                |
| `easycon` | `EasyConNS/EasyMCU_ESP32C61`                      | related project               |

**Always pass `--repo 2017fighting/ESP32-BLE5-NSController-Emulator`** to `gh`. Do not rely on
`gh`'s automatic inference from `git remote -v`: with `origin` tracked and pointing at upstream,
inference silently targets the wrong repository.

Export once per session to keep commands short:

```sh
export GH_REPO=2017fighting/ESP32-BLE5-NSController-Emulator
```

## Conventions

- **Create an issue**: `gh issue create --repo "$GH_REPO" --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --repo "$GH_REPO" --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --repo "$GH_REPO" --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --repo "$GH_REPO" --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --repo "$GH_REPO" --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --repo "$GH_REPO" --comment "..."`

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --repo "$GH_REPO" --comments` and `gh pr diff <number> --repo "$GH_REPO"` for the diff.
- **List external PRs for triage**: `gh pr list --repo "$GH_REPO" --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close` (all with `--repo "$GH_REPO"`).

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42 --repo "$GH_REPO"` and fall back to `gh issue view 42 --repo "$GH_REPO"`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue on `$GH_REPO` (the `mine` fork).

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --repo "$GH_REPO" --comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets. All commands
target `$GH_REPO`.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --repo "$GH_REPO" --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies**, the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/$GH_REPO/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/$GH_REPO/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only, the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --repo "$GH_REPO" --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --repo "$GH_REPO" --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment <n> --repo "$GH_REPO" --body "<answer>"`, then `gh issue close <n> --repo "$GH_REPO"`, then append a context pointer (gist + link) to the map's Decisions-so-far.
