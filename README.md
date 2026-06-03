# promise

Track the promises a codebase makes and back each one with **runnable evidence**.

The core idea: a promise ("a failed update doesn't corrupt the user's list", "checkout
is fast", "no insecure dependencies") is only as real as the mechanical check behind it.
So if an agent or a human deletes the test backing a promise, the promise visibly goes
**red** — and `verify` tells you exactly which higher-level promises that breaks.

It's language-agnostic (verifiers are just shell commands) and designed to be driven by
coding agents that want to know what they might break before and after a change.

## Install

```bash
uv sync
uv run promise --help
```

## Model

Everything lives in a single, human-readable `promises.yaml`, but the CLI is the safe
write path — it owns id generation and reference integrity.

A **node** is one promise:

| field       | meaning                                                                       |
|-------------|-------------------------------------------------------------------------------|
| `id`        | stable UUID (CLI-generated); reference it by id-prefix or title substring      |
| `title`     | the promise, in prose                                                          |
| `parent`    | id of the parent promise, or null for a root                                  |
| `affects`   | free-form tokens — file paths or concepts (e.g. `"SOC2 compliance"`)          |
| `verifiers` | zero or more shell checks backing the promise                                  |

A node with no verifiers is an internal node; its verdict is purely the rollup of its
children. Rollup is a **pure AND**: a node passes only if all its own verifiers pass and
all its children pass.

A **verifier**:

| field      | meaning                                                                          |
|------------|----------------------------------------------------------------------------------|
| `run`      | shell command; exit `0` = pass, anything else = fail                              |
| `scope`    | `repo` · `artifact` · `deploy` · `runtime` · `external` (see below)               |
| `blocking` | whether a failure affects the exit code (defaults per scope)                      |
| `cwd`      | working dir, relative to the repo root (defaults to the repo root)                |
| `timeout`  | seconds before the command is killed and marked failed (default 300)              |

`scope` is metadata: it never changes a verdict, it only seeds the default `blocking`
bit. `repo`/`artifact` default to blocking; `deploy`/`runtime` default to non-blocking;
`external` must set `blocking` explicitly.

## Verdicts

Binary: **pass** / **fail**. The *why* of a failure (a deleted test, a missing binary,
an assertion) lives in the captured `detail`, not in extra verdict states. Good signal
out requires good verifiers in — point each verifier narrowly at the evidence it asserts.

## Commands

```bash
promise add "<title>" [--parent REF] [--verify "<cmd>" --scope SCOPE [--blocking/--no-blocking]] [--affects TOKEN ...]
promise rm REF [--cascade]
promise mv REF --parent REF|root          # guarded against cycles
promise edit REF [--title ...] [--affects TOKEN ...]
promise check                             # lint: dangling parents, cycles, unbacked nodes
promise verify [REF] [--json] [--full] [--junit PATH] [--fail-on blocking|any] [--workers N]
promise impact <file-or-concept> [--json]
```

`REF` is an id, a unique id-prefix, or a unique title substring.

Verifiers run **concurrently** (they are subprocess-bound); `--workers N` caps the pool
and `--workers 1` forces serial. Rollup is deterministic regardless of completion order.

### verify output formats

- **plain text** (human): a one-line status plus the root-cause failures.
- **`--json`** (default for agents): just `verdict`, `exit`, and the root-cause `failed`
  list — each with the `breaks` it causes (rendered as titles) and a `detail`.
- **`--json --full`**: the complete `promise.verify/v1` tree, every node with its
  verifiers, `failing_leaves` (down) and `breaks` (up).
- **`--junit PATH`**: JUnit XML so promises show up in CI test dashboards. Each
  promise with verifiers is a `<testcase>`; a red one is a `<failure>` carrying the
  `detail` and the `breaks` chain. Internal (verifier-less) nodes are omitted.

Compact example, after a backing test was deleted:

```json
{
  "verdict": "fail",
  "exit": 1,
  "failed": [
    {
      "id": "4c9f1a77-...",
      "title": "A failed update doesn't corrupt the list",
      "breaks": ["A user's data is never silently lost or corrupted",
                 "Users can manage their todo list and trust it"],
      "detail": "ERROR: file or directory not found: tests/test_storage_rollback.py"
    }
  ]
}
```

`breaks` is the headline for agents: it answers *"what higher-level promise did I just
make unfulfillable?"* without walking the tree.

### Exit codes

`verify` reports a fact: `0` = nothing failed, `1` = something failed. *What to do about
it* is policy: by default only **blocking** failures gate (exit 1); `--fail-on any`
widens the gate to every failure.

### impact

`impact` is a fast, static pre-flight: it matches a file or concept against every node's
`affects` (and verifier strings). Path-like tokens are normalised to repo-relative POSIX
form, so `impact storage.py` finds a promise whose `affects` lists `todo-app/storage.py`.

> Limitation: `impact` only matches the literal tokens authors record — it does not trace
> the implementation files a test depends on (there is no coverage analysis). For that,
> rely on post-hoc `verify`, or run coverage yourself.

## Claude Code skill

`skills/promise/SKILL.md` is an installable Claude Code skill that teaches an agent to
use `promise` effectively — chiefly to run `impact` before touching a file and `verify`
before deleting a test. Copy the `skills/promise/` directory into a skills directory
Claude Code reads (e.g. `~/.claude/skills/` or a project's `.claude/skills/`).

## Self-hosted promises

This repo keeps its own `promises.yaml`: every promise is backed by promise-cli's own
test suite and lint. Run `promise verify` here to check the tool still keeps its own
promises — and watch a promise go red if you delete the test behind it.

## Development

```bash
uv run pytest          # tests
uv run ruff check .    # lint
uv run ruff format .   # format
```

This project was built as an experiment under `agent-experiments`.
