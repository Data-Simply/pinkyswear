"""Verification engine: run verifiers, roll up pass/fail, and render reports.

Rollup is a pure AND: a node passes only if all of its own verifiers pass and all of
its children pass. Verdicts are binary. The "why" of a failure lives in each verifier's
captured ``detail`` rather than in a distinct verdict state.
"""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .model import Node, PromiseSet, Verifier

# Exit code reported when a verifier command exceeds its timeout (the conventional
# shell exit code for a timed-out command).
TIMEOUT_EXIT_CODE = 124

# Exit code reported when a verifier command cannot be launched at all (e.g. its cwd
# does not exist). Conventional shell code for "command not executable".
LAUNCH_FAILED_EXIT_CODE = 126

# Maximum length of a captured ``detail`` string before truncation.
MAX_DETAIL_LENGTH = 500

# Upper bound on worker threads when running verifiers concurrently, used when the
# caller does not specify ``max_workers``. Verifiers are subprocess-bound, so this is
# an I/O-style concurrency limit rather than a CPU one.
DEFAULT_MAX_WORKERS = min(8, (os.cpu_count() or 4) * 2)

PASS = "pass"
FAIL = "fail"


@dataclass
class VerifierResult:
    """Outcome of running a single verifier."""

    run: str
    scope: str
    blocking: bool
    verdict: str
    exit_code: int
    duration_ms: int
    detail: str


@dataclass
class NodeResult:
    """Rolled-up outcome for a single node."""

    node: Node
    verdict: str
    verifier_results: list[VerifierResult] = field(default_factory=list)
    failing_leaves: list[str] = field(default_factory=list)
    breaks: list[str] = field(default_factory=list)


@dataclass
class VerifyReport:
    """The full result of a ``verify`` run over a (sub)tree."""

    root_id: str | None
    ran_at: str
    results: dict[str, NodeResult]
    order: list[str]
    fail_on: str = "blocking"
    wall_ms: int = 0

    @property
    def verdict(self) -> str:
        """Overall verdict: ``fail`` if any node failed, else ``pass``."""
        return FAIL if any(r.verdict == FAIL for r in self.results.values()) else PASS

    @property
    def exit_code(self) -> int:
        """Process exit code, gated by ``fail_on``.

        With ``fail_on == "any"`` any failure yields ``1``. Otherwise (the default,
        ``"blocking"``) only a failing blocking verifier yields ``1``.
        """
        if self.fail_on == "any":
            return 1 if self.verdict == FAIL else 0
        for result in self.results.values():
            for vr in result.verifier_results:
                if vr.verdict == FAIL and vr.blocking:
                    return 1
        return 0

    @property
    def counts(self) -> dict[str, int]:
        """Count of passing and failing nodes in the verified scope."""
        passed = sum(1 for r in self.results.values() if r.verdict == PASS)
        failed = sum(1 for r in self.results.values() if r.verdict == FAIL)
        return {"pass": passed, "fail": failed}

    def root_cause_failures(self) -> list[NodeResult]:
        """Return failing nodes whose own verifiers failed (the root causes).

        Internal nodes that merely relay a child's failure are excluded.

        Returns:
            Node results in document order whose own verifiers produced a failure.
        """
        causes: list[NodeResult] = []
        for node_id in self.order:
            result = self.results[node_id]
            if any(vr.verdict == FAIL for vr in result.verifier_results):
                causes.append(result)
        return causes


def _resolve_cwd(verifier: Verifier, repo_root: Path) -> Path:
    """Resolve a verifier's working directory against the repo root.

    Args:
        verifier: The verifier whose ``cwd`` to resolve.
        repo_root: The repository root directory.

    Returns:
        The absolute working directory for the command.
    """
    if verifier.cwd is None:
        return repo_root
    cwd = Path(verifier.cwd)
    return cwd if cwd.is_absolute() else repo_root / cwd


def _extract_detail(stdout: str, stderr: str, exit_code: int) -> str:
    """Derive a concise one-line detail from captured command output.

    Args:
        stdout: Captured standard output.
        stderr: Captured standard error.
        exit_code: The command's exit code.

    Returns:
        The last non-empty output line (stderr preferred), truncated, or a fallback.
    """
    for stream in (stderr, stdout):
        lines = [line.strip() for line in stream.splitlines() if len(line.strip()) > 0]
        if len(lines) > 0:
            detail = lines[-1]
            return detail if len(detail) <= MAX_DETAIL_LENGTH else detail[:MAX_DETAIL_LENGTH] + "..."
    return f"exited {exit_code} with no output"


def run_verifier(verifier: Verifier, repo_root: Path) -> VerifierResult:
    """Execute a verifier command and always capture its outcome as a result.

    The command runs through the shell (like a test runner invoking ``make`` or
    ``pytest``), with stdout/stderr captured and a timeout enforced. Any failure to
    even launch the command (e.g. a missing ``cwd``) is recorded as a FAIL result with
    an explanatory ``detail`` rather than raised, so one bad verifier never aborts the
    rest of the run.

    Args:
        verifier: The verifier to run.
        repo_root: The repository root, used to resolve ``cwd``.

    Returns:
        A ``VerifierResult`` describing the outcome.
    """
    cwd = _resolve_cwd(verifier, repo_root)
    start = datetime.now(UTC)
    try:
        completed = subprocess.run(  # noqa: S602 - executing repo-declared checks is the point
            verifier.run,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=verifier.timeout,
            check=False,
        )
        exit_code = completed.returncode
        detail = _extract_detail(completed.stdout, completed.stderr, exit_code)
    except subprocess.TimeoutExpired:
        exit_code = TIMEOUT_EXIT_CODE
        detail = f"timed out after {verifier.timeout}s"
    except OSError as exc:
        # The command could not be launched at all (missing cwd, permission denied,
        # un-spawnable shell). Treat it as a failed verifier, not a crash.
        exit_code = LAUNCH_FAILED_EXIT_CODE
        detail = f"could not run verifier: {exc}"
    duration_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)

    return VerifierResult(
        run=verifier.run,
        scope=verifier.scope,
        blocking=verifier.blocking,
        verdict=PASS if exit_code == 0 else FAIL,
        exit_code=exit_code,
        duration_ms=duration_ms,
        detail=detail,
    )


def verify(
    promises: PromiseSet,
    root_ref: str | None = None,
    fail_on: str = "blocking",
    max_workers: int | None = None,
) -> VerifyReport:
    """Run all verifiers in a (sub)tree and roll up verdicts.

    Verifiers are independent and run concurrently on a thread pool (they are
    subprocess-bound). Rollup is deterministic regardless of completion order.

    Args:
        promises: The promise document.
        root_ref: Optional reference to the subtree root. When ``None``, the whole
            document is verified.
        fail_on: Exit-code policy, ``"blocking"`` (default) or ``"any"``.
        max_workers: Maximum concurrent verifier processes. Defaults to
            ``DEFAULT_MAX_WORKERS``; pass ``1`` to force serial execution.

    Returns:
        A ``VerifyReport`` with per-node results, ``failing_leaves`` (down), and
        ``breaks`` (up) populated.
    """
    if root_ref is not None:
        root = promises.resolve(root_ref)
        scope_nodes = [root, *promises.descendants(root.id)]
        root_id: str | None = root.id
    else:
        scope_nodes = promises.nodes
        root_id = None

    # 1. Run every verifier concurrently. Each task is keyed by (node_id, index) so
    #    results can be re-collated into per-node order after completion.
    repo_root = _repo_root_for(promises)
    tasks: list[tuple[str, int, Verifier]] = []
    for node in scope_nodes:
        for index, verifier in enumerate(node.verifiers):
            tasks.append((node.id, index, verifier))

    workers = max(1, min(max_workers if max_workers is not None else DEFAULT_MAX_WORKERS, len(tasks)))

    wall_start = datetime.now(UTC)
    completed: dict[tuple[str, int], VerifierResult] = {}
    if len(tasks) > 0:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_verifier, verifier, repo_root): (node_id, index) for node_id, index, verifier in tasks
            }
            for future in futures:
                node_id, index = futures[future]
                # run_verifier never raises, but guard the future itself so an
                # unexpected pool/thread error degrades one verifier to a FAIL rather
                # than discarding every other verifier's result.
                try:
                    completed[(node_id, index)] = future.result()
                except Exception as exc:  # noqa: BLE001 - defensive: never drop the whole run
                    verifier = next(v for nid, i, v in tasks if nid == node_id and i == index)
                    completed[(node_id, index)] = VerifierResult(
                        run=verifier.run,
                        scope=verifier.scope,
                        blocking=verifier.blocking,
                        verdict=FAIL,
                        exit_code=LAUNCH_FAILED_EXIT_CODE,
                        duration_ms=0,
                        detail=f"verifier raised: {exc}",
                    )
    wall_ms = int((datetime.now(UTC) - wall_start).total_seconds() * 1000)

    own_verdict: dict[str, str] = {}
    verifier_results: dict[str, list[VerifierResult]] = {}
    for node in scope_nodes:
        results_list = [completed[(node.id, i)] for i in range(len(node.verifiers))]
        verifier_results[node.id] = results_list
        own_verdict[node.id] = FAIL if any(r.verdict == FAIL for r in results_list) else PASS

    # The children relation restricted to the verified scope, built once.
    scoped_children: dict[str, list[str]] = {n.id: [] for n in scope_nodes}
    for node in scope_nodes:
        if node.parent is not None and node.parent in scoped_children:
            scoped_children[node.parent].append(node.id)

    # 2. Roll up verdicts bottom-up over the scope. A node fails if its own verifiers
    #    fail or any scoped child fails. Iterative + ``seen``-guarded so a cyclic
    #    promises.yaml degrades to a finite result instead of a RecursionError.
    node_verdict: dict[str, str] = {}
    failing_leaves_map: dict[str, list[str]] = {}

    def resolve_node(node_id: str) -> None:
        # Post-order DFS using an explicit stack; ``state`` marks the second visit.
        stack: list[tuple[str, bool]] = [(node_id, False)]
        on_path: set[str] = set()
        while len(stack) > 0:
            current, processed = stack.pop()
            if processed:
                on_path.discard(current)
                verdict = own_verdict[current]
                leaves: list[str] = []
                if len(verifier_results[current]) > 0 and own_verdict[current] == FAIL:
                    leaves.append(current)
                for child_id in scoped_children[current]:
                    if node_verdict.get(child_id) == FAIL:
                        verdict = FAIL
                    leaves.extend(failing_leaves_map.get(child_id, []))
                node_verdict[current] = verdict
                failing_leaves_map[current] = leaves
                continue
            if current in node_verdict or current in on_path:
                # Already resolved, or a cycle back-edge: skip to avoid looping.
                continue
            on_path.add(current)
            stack.append((current, True))
            for child_id in scoped_children[current]:
                stack.append((child_id, False))

    for node in scope_nodes:
        if node.id not in node_verdict:
            resolve_node(node.id)

    # 3. Assemble results. ``failing_leaves`` comes from the bottom-up pass; ``breaks``
    #    is the failing node's ancestor chain (ancestors() is already cycle-safe).
    results: dict[str, NodeResult] = {}
    order: list[str] = []
    for node in scope_nodes:
        order.append(node.id)
        breaks = [a.id for a in promises.ancestors(node.id)] if node_verdict[node.id] == FAIL else []
        results[node.id] = NodeResult(
            node=node,
            verdict=node_verdict[node.id],
            verifier_results=verifier_results[node.id],
            failing_leaves=failing_leaves_map[node.id],
            breaks=breaks,
        )

    return VerifyReport(
        root_id=root_id,
        ran_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        results=results,
        order=order,
        fail_on=fail_on,
        wall_ms=wall_ms,
    )


def _repo_root_for(promises: PromiseSet) -> Path:
    """Return the repo root used for verifier execution.

    Args:
        promises: The promise document (carries the source path when loaded from disk).

    Returns:
        The directory used as the default working directory for verifiers.
    """
    source = getattr(promises, "source_path", None)
    return Path(source).resolve().parent if source is not None else Path.cwd()
