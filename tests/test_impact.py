"""Tests for static impact analysis."""

import pytest

from pinkyswear.impact import impact


class TestImpactMatching:
    def test_bare_filename_matches_nested_path(self, todo_promises):
        matches = impact(todo_promises, "storage.py")
        assert {m.node_id for m in matches} == {"data-safe", "rollback", "concurrent"}

    def test_full_path_matches(self, todo_promises):
        # The root node's affects is the directory "todo-app/", so a file under it
        # legitimately matches root as well as the storage-specific promises.
        matches = impact(todo_promises, "todo-app/storage.py")
        assert {m.node_id for m in matches} == {"root", "data-safe", "rollback", "concurrent"}

    def test_leading_dot_slash_is_normalized(self, todo_promises):
        matches = impact(todo_promises, "./todo-app/storage.py")
        assert {m.node_id for m in matches} == {"root", "data-safe", "rollback", "concurrent"}

    def test_concept_token_matches_case_insensitively(self, todo_promises):
        matches = impact(todo_promises, "soc2 COMPLIANCE")
        assert {m.node_id for m in matches} == {"deps"}

    def test_query_reports_which_tokens_matched(self, todo_promises):
        matches = impact(todo_promises, "storage.py")
        rollback = next(m for m in matches if m.node_id == "rollback")
        assert "todo-app/storage.py" in rollback.matched

    def test_no_match_returns_empty(self, todo_promises):
        assert impact(todo_promises, "nonexistent/module.py") == []

    def test_bare_extension_does_not_match_files(self, todo_promises):
        # A loose query like ".py" must not match every .py path (segment-aware matching).
        assert impact(todo_promises, ".py") == []

    def test_partial_segment_does_not_match_path(self, todo_promises):
        # "stora" is not a whole path segment of "todo-app/storage.py".
        assert impact(todo_promises, "storage.p") == []

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("uv.lock", {"deps"}),
            ("checkout latency", {"fast"}),
            ("todo-app/api.py", {"fast", "root"}),
        ],
    )
    def test_various_queries(self, todo_promises, query, expected):
        assert {m.node_id for m in impact(todo_promises, query)} == expected
