"""Tests for the promise data model: verifiers, nodes, and document navigation."""

import pytest

from pinkyswear.model import (
    DEFAULT_TIMEOUT_SECONDS,
    Node,
    PromiseError,
    PromiseSet,
    Verifier,
    new_id,
)


class TestVerifierValidation:
    def test_repo_scope_defaults_to_blocking(self):
        verifier = Verifier.from_dict({"run": "pytest tests/", "scope": "repo"})
        assert verifier.blocking is True

    def test_runtime_scope_defaults_to_non_blocking(self):
        verifier = Verifier.from_dict({"run": "check_latency.sh", "scope": "runtime"})
        assert verifier.blocking is False

    def test_external_scope_requires_explicit_blocking(self):
        with pytest.raises(PromiseError, match="must set 'blocking' explicitly"):
            Verifier.from_dict({"run": "dependabot_gate.sh", "scope": "external"})

    def test_external_scope_accepts_explicit_blocking(self):
        verifier = Verifier.from_dict({"run": "dependabot_gate.sh", "scope": "external", "blocking": True})
        assert verifier.blocking is True

    def test_explicit_blocking_overrides_scope_default(self):
        verifier = Verifier.from_dict({"run": "pytest tests/", "scope": "repo", "blocking": False})
        assert verifier.blocking is False

    def test_default_timeout_applied(self):
        verifier = Verifier.from_dict({"run": "pytest tests/", "scope": "repo"})
        assert verifier.timeout == DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            ({"run": "", "scope": "repo"}, "non-empty string"),
            ({"scope": "repo"}, "non-empty string"),
            ({"run": "pytest", "scope": "bogus"}, "must be one of"),
            ({"run": "pytest", "scope": "repo", "timeout": 0}, "positive integer"),
            ({"run": "pytest", "scope": "repo", "timeout": -5}, "positive integer"),
            ({"run": "pytest", "scope": "repo", "blocking": "yes"}, "must be a boolean"),
        ],
    )
    def test_invalid_verifier_rejected(self, data, match):
        with pytest.raises(PromiseError, match=match):
            Verifier.from_dict(data)

    def test_round_trip_preserves_fields(self):
        original = Verifier.from_dict({"run": "pytest -q", "scope": "repo", "blocking": False, "timeout": 30})
        restored = Verifier.from_dict(original.to_dict())
        assert restored == original


class TestNodeValidation:
    def test_minimal_node(self):
        node = Node.from_dict({"id": "abc", "title": "Data is safe"})
        assert node.parent is None
        assert node.affects == []
        assert node.verifiers == []

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            ({"title": "x"}, "'id' must be a non-empty string"),
            ({"id": "abc"}, "'title' must be a non-empty string"),
            ({"id": "abc", "title": "x", "parent": 5}, "'parent' must be a string"),
            ({"id": "abc", "title": "x", "affects": "nope"}, "'affects' must be a list"),
        ],
    )
    def test_invalid_node_rejected(self, data, match):
        with pytest.raises(PromiseError, match=match):
            Node.from_dict(data)


class TestPromiseSetNavigation:
    def test_duplicate_ids_rejected(self):
        with pytest.raises(PromiseError, match="duplicate node id"):
            PromiseSet([Node(id="x", title="A"), Node(id="x", title="B")])

    def test_children_and_roots(self, todo_promises):
        assert {c.id for c in todo_promises.children("data-safe")} == {"rollback", "concurrent"}
        assert {r.id for r in todo_promises.roots()} == {"root"}

    def test_ancestors_nearest_first(self, todo_promises):
        assert [a.id for a in todo_promises.ancestors("concurrent")] == ["data-safe", "root"]

    def test_descendants(self, todo_promises):
        assert {d.id for d in todo_promises.descendants("root")} == {
            "data-safe",
            "rollback",
            "concurrent",
            "fast",
            "deps",
        }

    def test_would_create_cycle_for_descendant(self, todo_promises):
        assert todo_promises.would_create_cycle("root", "concurrent") is True

    def test_would_create_cycle_for_self(self, todo_promises):
        assert todo_promises.would_create_cycle("root", "root") is True

    def test_no_cycle_for_unrelated_move(self, todo_promises):
        assert todo_promises.would_create_cycle("fast", "data-safe") is False

    def test_add_requires_existing_parent(self, todo_promises):
        with pytest.raises(PromiseError, match="does not exist"):
            todo_promises.add(Node(id="orphan", title="X", parent="ghost"))

    def test_set_parent_updates_children_both_sides(self, todo_promises):
        # The cached children index must stay consistent after a reparent.
        todo_promises.set_parent("fast", "data-safe")
        assert "fast" not in {c.id for c in todo_promises.children("root")}
        assert "fast" in {c.id for c in todo_promises.children("data-safe")}

    def test_add_then_children_reflects_new_node(self, todo_promises):
        todo_promises.add(Node(id="backup", title="Backups exist", parent="data-safe"))
        assert "backup" in {c.id for c in todo_promises.children("data-safe")}

    def test_remove_then_children_drops_node(self, todo_promises):
        todo_promises.remove("deps")
        assert "deps" not in {c.id for c in todo_promises.children("root")}


class TestReferenceResolution:
    def test_resolve_exact_id(self, todo_promises):
        assert todo_promises.resolve("rollback").id == "rollback"

    def test_resolve_title_substring(self, todo_promises):
        assert todo_promises.resolve("instant").id == "fast"

    def test_resolve_title_is_case_insensitive(self, todo_promises):
        assert todo_promises.resolve("INSTANT").id == "fast"

    def test_ambiguous_title_rejected(self, todo_promises):
        with pytest.raises(PromiseError, match="multiple titles"):
            todo_promises.resolve("todo")

    def test_unknown_reference_rejected(self, todo_promises):
        with pytest.raises(PromiseError, match="matched no node"):
            todo_promises.resolve("nonexistent-thing")

    def test_resolve_unique_id_prefix(self):
        promises = PromiseSet([Node(id="abc123", title="A"), Node(id="xyz789", title="B")])
        assert promises.resolve("abc").id == "abc123"


class TestPersistence:
    def test_round_trip_through_disk(self, todo_promises, tmp_path):
        out = tmp_path / "out.yaml"
        todo_promises.save(out)
        reloaded = PromiseSet.load(out)
        assert {n.id for n in reloaded.nodes} == {n.id for n in todo_promises.nodes}
        assert reloaded.get("concurrent").verifiers[0].run == "false"

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(PromiseError, match="not found"):
            PromiseSet.load(tmp_path / "absent.yaml")

    def test_new_id_is_unique(self):
        assert new_id() != new_id()
