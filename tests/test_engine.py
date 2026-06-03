"""Tests for the verification engine: running verifiers and rolling up verdicts."""

from pathlib import Path

from pinkyswear.engine import FAIL, PASS, run_verifier, verify
from pinkyswear.model import Node, PromiseSet, Verifier


class TestRunVerifier:
    def test_passing_command(self, tmp_path):
        result = run_verifier(Verifier(run="true", scope="repo", blocking=True), tmp_path)
        assert result.verdict == PASS
        assert result.exit_code == 0

    def test_failing_command(self, tmp_path):
        result = run_verifier(Verifier(run="false", scope="repo", blocking=True), tmp_path)
        assert result.verdict == FAIL
        assert result.exit_code != 0

    def test_detail_captures_output(self, tmp_path):
        result = run_verifier(
            Verifier(run="echo 'corruption detected' >&2; exit 1", scope="repo", blocking=True),
            tmp_path,
        )
        assert result.verdict == FAIL
        assert "corruption detected" in result.detail

    def test_timeout_marks_failure(self, tmp_path):
        result = run_verifier(Verifier(run="sleep 5", scope="repo", blocking=True, timeout=1), tmp_path)
        assert result.verdict == FAIL
        assert "timed out" in result.detail

    def test_cwd_defaults_to_repo_root(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here")
        result = run_verifier(Verifier(run="test -f marker.txt", scope="repo", blocking=True), tmp_path)
        assert result.verdict == PASS

    def test_missing_cwd_records_failure_not_crash(self, tmp_path):
        # A verifier whose cwd does not exist must degrade to a FAIL result, never raise.
        result = run_verifier(
            Verifier(run="true", scope="repo", blocking=True, cwd="does/not/exist"),
            tmp_path,
        )
        assert result.verdict == FAIL
        assert "could not run verifier" in result.detail


class TestCycleSafety:
    """A malformed (cyclic) document must not crash verify with RecursionError."""

    def test_two_node_cycle_terminates(self, tmp_path):
        promises = PromiseSet(
            [
                Node(id="a", title="A", parent="b", verifiers=[Verifier(run="true", scope="repo", blocking=True)]),
                Node(id="b", title="B", parent="a", verifiers=[Verifier(run="true", scope="repo", blocking=True)]),
            ]
        )
        promises.source_path = tmp_path / "promises.yaml"
        report = verify(promises)
        assert set(report.order) == {"a", "b"}
        assert report.results["a"].verdict == PASS

    def test_self_parent_cycle_terminates(self, tmp_path):
        promises = PromiseSet(
            [Node(id="a", title="A", parent="a", verifiers=[Verifier(run="false", scope="repo", blocking=True)])]
        )
        promises.source_path = tmp_path / "promises.yaml"
        report = verify(promises)
        assert report.results["a"].verdict == FAIL


class TestRollup:
    def test_failing_leaf_propagates_to_ancestors(self, todo_promises):
        report = verify(todo_promises)
        assert report.results["concurrent"].verdict == FAIL
        assert report.results["data-safe"].verdict == FAIL
        assert report.results["root"].verdict == FAIL

    def test_passing_branch_stays_green(self, todo_promises):
        report = verify(todo_promises)
        assert report.results["rollback"].verdict == PASS
        assert report.results["deps"].verdict == PASS

    def test_breaks_lists_ancestors_of_failing_node(self, todo_promises):
        report = verify(todo_promises)
        assert report.results["concurrent"].breaks == ["data-safe", "root"]

    def test_passing_node_breaks_nothing(self, todo_promises):
        report = verify(todo_promises)
        assert report.results["rollback"].breaks == []

    def test_failing_leaves_identifies_root_cause(self, todo_promises):
        report = verify(todo_promises)
        assert report.results["root"].failing_leaves == ["concurrent"]

    def test_root_cause_failures_excludes_relaying_internal_nodes(self, todo_promises):
        report = verify(todo_promises)
        cause_ids = [r.node.id for r in report.root_cause_failures()]
        assert cause_ids == ["concurrent"]

    def test_summary_verdict_and_counts(self, todo_promises):
        report = verify(todo_promises)
        assert report.verdict == FAIL
        assert report.counts == {"pass": 3, "fail": 3}


class TestExitCodePolicy:
    def test_blocking_failure_yields_exit_one(self, todo_promises):
        report = verify(todo_promises)
        assert report.exit_code == 1

    def test_non_blocking_failure_does_not_gate(self, tmp_path):
        path = tmp_path / "p.yaml"
        promises = PromiseSet(
            [
                Node(id="r", title="Root", verifiers=[]),
                Node(
                    id="slow",
                    title="Latency budget",
                    parent="r",
                    verifiers=[Verifier(run="false", scope="runtime", blocking=False)],
                ),
            ]
        )
        promises.source_path = path
        report = verify(promises)
        assert report.verdict == FAIL
        assert report.exit_code == 0

    def test_fail_on_any_gates_on_non_blocking_failure(self, tmp_path):
        path = tmp_path / "p.yaml"
        promises = PromiseSet(
            [
                Node(id="r", title="Root", verifiers=[]),
                Node(
                    id="slow",
                    title="Latency budget",
                    parent="r",
                    verifiers=[Verifier(run="false", scope="runtime", blocking=False)],
                ),
            ]
        )
        promises.source_path = path
        report = verify(promises, fail_on="any")
        assert report.exit_code == 1


class TestSubtreeVerification:
    def test_verify_subtree_only_runs_descendants(self, todo_promises):
        report = verify(todo_promises, root_ref="data-safe")
        assert set(report.order) == {"data-safe", "rollback", "concurrent"}
        assert "fast" not in report.results


class TestConcurrency:
    """Parallel execution must not change verdicts or per-node verifier ordering."""

    def test_parallel_and_serial_agree(self, todo_promises):
        serial = verify(todo_promises, max_workers=1)
        parallel = verify(todo_promises, max_workers=8)
        assert {k: v.verdict for k, v in serial.results.items()} == {k: v.verdict for k, v in parallel.results.items()}

    def test_verifier_results_stay_in_node_order(self, tmp_path):
        # A node with two verifiers: the first passes, the second fails. Order must hold
        # regardless of which thread finishes first.
        promises = PromiseSet(
            [
                Node(
                    id="multi",
                    title="Two checks, fixed order",
                    verifiers=[
                        Verifier(run="true", scope="repo", blocking=True),
                        Verifier(run="false", scope="repo", blocking=True),
                    ],
                )
            ]
        )
        promises.source_path = tmp_path / "promises.yaml"
        report = verify(promises, max_workers=4)
        verdicts = [vr.verdict for vr in report.results["multi"].verifier_results]
        assert verdicts == [PASS, FAIL]

    def test_concurrency_actually_overlaps(self, tmp_path):
        # Three 1-second sleeps run concurrently should finish well under 3 seconds.
        import time

        promises = PromiseSet(
            [Node(id="root", title="Root", verifiers=[])]
            + [
                Node(
                    id=f"s{i}",
                    title=f"Sleeper {i}",
                    parent="root",
                    verifiers=[Verifier(run="sleep 1", scope="repo", blocking=True)],
                )
                for i in range(3)
            ]
        )
        promises.source_path = tmp_path / "promises.yaml"
        start = time.monotonic()
        verify(promises, max_workers=3)
        elapsed = time.monotonic() - start
        assert elapsed < 2.5


class TestDeletionDetection:
    """The core thesis: deleting the evidence a promise points at turns it red."""

    def _single_promise(self, tmp_path: Path) -> PromiseSet:
        promises = PromiseSet(
            [
                Node(
                    id="rollback",
                    title="A failed update doesn't corrupt the list",
                    verifiers=[Verifier(run="test -f test_rollback.py", scope="repo", blocking=True)],
                )
            ]
        )
        promises.source_path = tmp_path / "promises.yaml"
        return promises

    def test_promise_green_while_evidence_present(self, tmp_path):
        (tmp_path / "test_rollback.py").write_text("def test_rollback(): assert True")
        report = verify(self._single_promise(tmp_path))
        assert report.results["rollback"].verdict == PASS

    def test_promise_red_after_evidence_deleted(self, tmp_path):
        report = verify(self._single_promise(tmp_path))
        assert report.results["rollback"].verdict == FAIL
