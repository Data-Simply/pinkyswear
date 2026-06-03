"""Tests for the static linter."""

from pinkyswear.check import CYCLE, DANGLING_PARENT, NO_BACKING, check
from pinkyswear.model import Node, PromiseSet, Verifier


class TestCheck:
    def test_clean_document_has_no_issues(self, todo_promises):
        assert check(todo_promises) == []

    def test_dangling_parent_detected(self):
        promises = PromiseSet([Node(id="child", title="Orphaned", parent="ghost", verifiers=[])])
        # A childless, verifier-less node would also flag no_backing; assert the dangling kind is present.
        kinds = {issue.kind for issue in check(promises)}
        assert DANGLING_PARENT in kinds

    def test_no_backing_detected_for_empty_leaf(self):
        promises = PromiseSet([Node(id="empty", title="Backed by nothing")])
        issues = check(promises)
        assert [i.kind for i in issues] == [NO_BACKING]

    def test_internal_node_with_children_is_not_no_backing(self, todo_promises):
        kinds = {i.kind for i in check(todo_promises)}
        assert NO_BACKING not in kinds

    def test_cycle_detected(self):
        promises = PromiseSet(
            [
                Node(id="a", title="A", parent="b", verifiers=[]),
                Node(id="b", title="B", parent="a", verifiers=[]),
            ]
        )
        cycle_nodes = {i.node_id for i in check(promises) if i.kind == CYCLE}
        assert cycle_nodes == {"a", "b"}

    def test_node_leading_into_cycle_is_not_flagged_as_cycle(self):
        # tail -> a <-> b ; tail points into the cycle but is not itself on it.
        promises = PromiseSet(
            [
                Node(
                    id="tail", title="Tail", parent="a", verifiers=[Verifier(run="true", scope="repo", blocking=True)]
                ),
                Node(id="a", title="A", parent="b", verifiers=[]),
                Node(id="b", title="B", parent="a", verifiers=[]),
            ]
        )
        cycle_nodes = {i.node_id for i in check(promises) if i.kind == CYCLE}
        assert "tail" not in cycle_nodes
        assert cycle_nodes == {"a", "b"}
