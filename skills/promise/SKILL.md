---
name: promise
description: >-
  Track and verify the promises a codebase makes, backed by runnable evidence. Use
  this skill BEFORE changing code to learn what a file or behavior is responsible for,
  and AFTER changing code to confirm you haven't broken a guarantee. Especially: before
  deleting or weakening a test, run `promise impact <file>` and `promise verify` to see
  which higher-level promises that test backs. Triggers: a repo contains a
  `promises.yaml`; the user mentions "promises", "guarantees", "what does this break",
  "is this still backed"; you are about to remove/modify a test or a load-bearing file.
---

# promise

`promise` tracks the guarantees a codebase makes ("a failed update doesn't corrupt the
list", "checkout is fast", "no insecure dependencies") and backs each with a **runnable
verifier** — a shell command. A promise is only as real as the evidence behind it, so if
you delete the test backing a promise, the promise goes red and `promise verify` tells
you exactly which higher-level promises that breaks.

The guarantees live in a single `promises.yaml` at the repo root. Always edit it through
the CLI, never by hand — the CLI owns id generation and reference integrity.

## When to reach for it

- **Before editing a file**: `promise impact <path>` → which promises name this file.
- **Before deleting/weakening a test**: `promise impact <test-file>`, then `promise
  verify` — if the test backs a promise, removing it will turn that promise (and its
  ancestors) red. Decide whether that is acceptable, or tell the user.
- **After any change**: `promise verify` → confirm every promise still holds.
- **Authoring a new guarantee**: `promise add` a promise and back it with a verifier.

## Core workflow for a code change

1. `promise impact src/path/you/will/touch.py` — see affected promises.
2. Make your change.
3. `promise verify --json` — read `verdict`. If `fail`, each entry in `failed` names the
   promise, its `detail` (why), and `breaks` (the higher-level promises now unfulfillable).
4. Fix until `verdict` is `pass`, or surface the broken promise to the user with its
   `breaks` chain if breaking it is intentional.

## Commands

```bash
promise verify [REF] [--json] [--full] [--junit PATH] [--fail-on blocking|any] [--workers N]
promise impact <file-or-concept> [--json]
promise check
promise add "<title>" [--parent REF] [--verify "<cmd>" --scope SCOPE [--blocking|--no-blocking]] [--affects TOKEN ...]
promise rm REF [--cascade]
promise mv REF --parent REF|root
promise edit REF [--title ...] [--affects TOKEN ...]
```

`REF` resolves by exact id, unique id-prefix, or unique title substring — so
`promise verify "data is never lost"` works.

### verify

- `promise verify` — human summary: one status line plus root-cause failures.
- `promise verify --json` — **use this as an agent.** Compact: `verdict`, `exit`, and a
  `failed` list of only the root-cause promises, each with `breaks` (as titles) and
  `detail`. No tree-walking needed.
- `promise verify --json --full` — the entire `promise.verify/v1` tree (every node, its
  verifiers, `failing_leaves` down, `breaks` up) when you need to inspect deeply.
- `promise verify --junit results.xml` — JUnit XML for CI dashboards.

Compact JSON after a backing test was deleted:

```json
{
  "verdict": "fail",
  "exit": 1,
  "failed": [
    {
      "id": "4c9f1a77-...",
      "title": "A failed update doesn't corrupt the list",
      "breaks": ["A user's data is never silently lost", "Users can trust the app"],
      "detail": "ERROR: file or directory not found: tests/test_storage_rollback.py"
    }
  ]
}
```

Read `breaks` first: it is the direct answer to "what guarantee did I just break?"

### Exit codes (key off these)

`promise verify` exits `0` when nothing failed and `1` when something failed. By default
only **blocking** verifiers gate the exit code; `--fail-on any` makes every failure gate.
The JSON `exit` field mirrors the process exit code.

### impact

`promise impact <query>` statically lists promises whose `affects` (or verifier
commands) name a file or concept. Path queries are normalised, so `promise impact
storage.py` matches a promise whose `affects` lists `todo-app/storage.py`.

Limitation: `impact` only matches the literal paths/concepts authors recorded — it does
**not** trace implementation files a test merely depends on. So `impact` is a fast
pre-flight hint, not a guarantee. The hard guarantee is post-hoc: make the change, run
`promise verify`, and trust the red. If you need precise file→test mapping, run the
project's coverage tool yourself.

## The model in one screen

A node is one promise:

| field       | meaning |
|-------------|---------|
| `id`        | stable UUID; reference by id-prefix or title substring |
| `title`     | the promise, in prose |
| `parent`    | parent promise id, or null for a root |
| `affects`   | free-form tokens: file paths or concepts (e.g. `"SOC2 compliance"`) |
| `verifiers` | shell checks backing the promise |

A node with no verifiers is internal: its verdict is the **AND** of its children. A node
passes only if all its own verifiers pass and all its children pass.

A verifier:

| field      | meaning |
|------------|---------|
| `run`      | shell command; exit 0 = pass, non-zero = fail |
| `scope`    | `repo` · `artifact` · `deploy` · `runtime` · `external` (metadata; seeds `blocking`) |
| `blocking` | whether failure gates exit code (default: repo/artifact true, deploy/runtime false, external explicit) |
| `cwd`      | working dir relative to repo root (default: repo root) |
| `timeout`  | seconds before the command is killed and failed (default 300) |

## Authoring good promises

The single most important rule: **scope each verifier narrowly to the evidence it
asserts**, so deletion is detectable. `--verify "pytest tests/test_rollback.py"` goes red
when that file is deleted; `--verify "pytest tests/"` would stay green because the rest of
the suite still passes. Good signal out requires precise verifiers in.

Build a small tree by adding a high-level promise, then children backed by concrete
commands:

```bash
promise add "Users can trust the todo app"
promise add "Data is never silently lost" --parent "trust the todo app"
promise add "A failed update doesn't corrupt the list" \
    --parent "never silently lost" --scope repo \
    --verify "pytest tests/test_storage_rollback.py -q" \
    --affects storage.py --affects tests/test_storage_rollback.py
promise verify
```

## Cautions

- `promise rm` refuses to delete a node with children unless you pass `--cascade`; it
  warns which promises the removal may weaken. Read that warning.
- `promise mv` refuses moves that would create a cycle.
- Run `promise check` after manual edits or large restructures: it flags dangling parent
  references, cycles, and nodes with no backing (no verifiers and no children).
- `promise verify` executes shell commands from `promises.yaml`. That is the same trust
  level as running the repo's test suite — fine in a repo you already run, but be aware
  on an untrusted clone.
