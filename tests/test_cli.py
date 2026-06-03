"""Tests for the command-line interface."""

import json
from xml.etree import ElementTree as ET

import pytest

from pinkyswear.cli import main
from pinkyswear.model import PromiseSet


def run(argv: list[str]) -> int:
    """Invoke the CLI and return its exit code."""
    return main(argv)


class TestAdd:
    def test_add_creates_file_and_node(self, tmp_path, capsys):
        path = tmp_path / "new.yaml"
        code = run(["-f", str(path), "add", "Data is never lost"])
        assert code == 0
        new_id = capsys.readouterr().out.strip()
        promises = PromiseSet.load(path)
        assert promises.get(new_id).title == "Data is never lost"

    def test_add_with_verifier(self, tmp_path, capsys):
        path = tmp_path / "new.yaml"
        run(["-f", str(path), "add", "Tests pass", "--verify", "pytest -q", "--scope", "repo"])
        new_id = capsys.readouterr().out.strip()
        verifier = PromiseSet.load(path).get(new_id).verifiers[0]
        assert verifier.run == "pytest -q"
        assert verifier.blocking is True

    def test_add_under_parent(self, todo_file, capsys):
        run(["-f", str(todo_file), "add", "Backups exist", "--parent", "data-safe"])
        new_id = capsys.readouterr().out.strip()
        assert PromiseSet.load(todo_file).get(new_id).parent == "data-safe"

    def test_external_verifier_without_blocking_errors(self, tmp_path):
        path = tmp_path / "new.yaml"
        with pytest.raises(SystemExit) as exc:
            run(["-f", str(path), "add", "No CVEs", "--verify", "scan.sh", "--scope", "external"])
        assert exc.value.code == 2

    def test_verify_without_scope_errors(self, tmp_path):
        path = tmp_path / "new.yaml"
        with pytest.raises(SystemExit) as exc:
            run(["-f", str(path), "add", "X", "--verify", "pytest"])
        assert exc.value.code == 2


class TestRemove:
    def test_rm_refuses_node_with_children(self, todo_file):
        with pytest.raises(SystemExit) as exc:
            run(["-f", str(todo_file), "rm", "data-safe"])
        assert exc.value.code == 2

    def test_rm_cascade_removes_subtree(self, todo_file, capsys):
        run(["-f", str(todo_file), "rm", "data-safe", "--cascade"])
        capsys.readouterr()
        promises = PromiseSet.load(todo_file)
        assert not promises.has("data-safe")
        assert not promises.has("rollback")
        assert not promises.has("concurrent")

    def test_rm_leaf_succeeds(self, todo_file):
        assert run(["-f", str(todo_file), "rm", "deps"]) == 0
        assert not PromiseSet.load(todo_file).has("deps")


class TestMove:
    def test_mv_into_descendant_is_blocked(self, todo_file):
        with pytest.raises(SystemExit) as exc:
            run(["-f", str(todo_file), "mv", "root", "--parent", "concurrent"])
        assert exc.value.code == 2

    def test_mv_to_root(self, todo_file):
        assert run(["-f", str(todo_file), "mv", "data-safe", "--parent", "root"]) == 0
        run(["-f", str(todo_file), "mv", "concurrent", "--parent", "root"])
        assert PromiseSet.load(todo_file).get("concurrent").parent is None

    def test_mv_reparents(self, todo_file):
        run(["-f", str(todo_file), "mv", "fast", "--parent", "data-safe"])
        assert PromiseSet.load(todo_file).get("fast").parent == "data-safe"


class TestEdit:
    def test_edit_title(self, todo_file):
        run(["-f", str(todo_file), "edit", "deps", "--title", "No vulnerable deps, ever"])
        assert PromiseSet.load(todo_file).get("deps").title == "No vulnerable deps, ever"

    def test_edit_affects_replaces(self, todo_file):
        run(["-f", str(todo_file), "edit", "deps", "--affects", "requirements.txt"])
        assert PromiseSet.load(todo_file).get("deps").affects == ["requirements.txt"]

    def test_edit_nothing_errors(self, todo_file):
        with pytest.raises(SystemExit) as exc:
            run(["-f", str(todo_file), "edit", "deps"])
        assert exc.value.code == 2


class TestCheck:
    def test_clean_document_passes(self, todo_file):
        assert run(["-f", str(todo_file), "check"]) == 0

    def test_broken_document_fails(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("nodes:\n  - id: lonely\n    title: Backed by nothing\n")
        assert run(["-f", str(path), "check"]) == 1


class TestVerify:
    def test_text_output_reports_failure(self, todo_file, capsys):
        code = run(["-f", str(todo_file), "verify"])
        out = capsys.readouterr().out
        assert code == 1
        assert out.startswith("FAIL")
        assert "Concurrent writes don't clobber each other" in out

    def test_compact_json_lists_root_cause_and_breaks(self, todo_file, capsys):
        run(["-f", str(todo_file), "verify", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "fail"
        assert payload["exit"] == 1
        assert len(payload["failed"]) == 1
        failed = payload["failed"][0]
        assert failed["title"] == "Concurrent writes don't clobber each other"
        assert failed["breaks"] == ["Data is never silently lost", "Users can trust the todo app"]

    def test_full_json_includes_every_node(self, todo_file, capsys):
        run(["-f", str(todo_file), "verify", "--json", "--full"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema"] == "promise.verify/v1"
        assert {n["id"] for n in payload["nodes"]} == {
            "root",
            "data-safe",
            "rollback",
            "concurrent",
            "fast",
            "deps",
        }
        assert payload["summary"]["counts"] == {"pass": 3, "fail": 3}

    def test_fail_on_any_changes_exit_code(self, tmp_path, capsys):
        path = tmp_path / "p.yaml"
        path.write_text(
            "nodes:\n"
            "  - id: r\n    title: Root\n    verifiers: []\n"
            "  - id: slow\n    title: Latency budget\n    parent: r\n"
            "    verifiers:\n      - run: 'false'\n        scope: runtime\n        blocking: false\n"
        )
        assert run(["-f", str(path), "verify"]) == 0
        capsys.readouterr()
        assert run(["-f", str(path), "verify", "--fail-on", "any"]) == 1

    def test_junit_output_written(self, todo_file, tmp_path, capsys):
        out = tmp_path / "results.xml"
        code = run(["-f", str(todo_file), "verify", "--junit", str(out)])
        capsys.readouterr()
        assert code == 1
        tree = ET.parse(out)
        suite = tree.getroot()
        assert suite.tag == "testsuite"
        # Only nodes with verifiers become testcases (root and data-safe are internal).
        names = {tc.get("name") for tc in suite.findall("testcase")}
        assert "Concurrent writes don't clobber each other" in names
        assert "Users can trust the todo app" not in names
        # The failing concurrent-writes promise carries a <failure> naming what it breaks.
        failing = next(tc for tc in suite.findall("testcase") if tc.get("name").startswith("Concurrent"))
        failure = failing.find("failure")
        assert failure is not None
        assert "Data is never silently lost" in failure.text

    def test_workers_one_forces_serial_same_result(self, todo_file, capsys):
        assert run(["-f", str(todo_file), "verify", "--workers", "1"]) == 1
        capsys.readouterr()

    def test_junit_only_still_prints_verdict(self, todo_file, tmp_path, capsys):
        # --junit must not silence the human verdict line on stdout.
        run(["-f", str(todo_file), "verify", "--junit", str(tmp_path / "r.xml")])
        out = capsys.readouterr().out
        assert out.startswith("FAIL")

    def test_full_without_json_errors(self, todo_file):
        with pytest.raises(SystemExit) as exc:
            run(["-f", str(todo_file), "verify", "--full"])
        assert exc.value.code == 2

    def test_nonpositive_workers_errors(self, todo_file):
        with pytest.raises(SystemExit) as exc:
            run(["-f", str(todo_file), "verify", "--workers", "0"])
        assert exc.value.code == 2

    def test_cyclic_document_does_not_crash(self, tmp_path, capsys):
        path = tmp_path / "cycle.yaml"
        path.write_text(
            "nodes:\n"
            "  - id: a\n    title: A\n    parent: b\n"
            "    verifiers:\n      - run: 'true'\n        scope: repo\n"
            "  - id: b\n    title: B\n    parent: a\n"
            "    verifiers:\n      - run: 'true'\n        scope: repo\n"
        )
        code = run(["-f", str(path), "verify"])
        assert code == 0
        assert capsys.readouterr().out.startswith("PASS")


class TestImpact:
    def test_impact_text_lists_matches(self, todo_file, capsys):
        run(["-f", str(todo_file), "impact", "storage.py"])
        out = capsys.readouterr().out
        assert "Data is never silently lost" in out

    def test_impact_json(self, todo_file, capsys):
        run(["-f", str(todo_file), "impact", "SOC2 compliance", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert {m["id"] for m in payload} == {"deps"}

    def test_impact_no_match(self, todo_file, capsys):
        run(["-f", str(todo_file), "impact", "unrelated/thing.py"])
        assert "no promises reference" in capsys.readouterr().out
