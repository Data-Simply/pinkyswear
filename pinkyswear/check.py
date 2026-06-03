"""Static linting of a promise document.

``check`` validates structural invariants that loading alone does not guarantee:
dangling parent references, parent-chain cycles, and nodes with no backing (no
verifiers and no children). A node with no backing can never be meaningfully verified,
so it is treated as an authoring error.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import PromiseSet

DANGLING_PARENT = "dangling_parent"
CYCLE = "cycle"
NO_BACKING = "no_backing"


@dataclass
class CheckIssue:
    """A single problem found by ``check``."""

    node_id: str
    kind: str
    message: str


def check(promises: PromiseSet) -> list[CheckIssue]:
    """Return all structural problems in a promise document.

    Args:
        promises: The promise document to lint.

    Returns:
        Issues in document order. An empty list means the document is well-formed.
    """
    issues: list[CheckIssue] = []
    for node in promises.nodes:
        if node.parent is not None and not promises.has(node.parent):
            issues.append(
                CheckIssue(
                    node_id=node.id,
                    kind=DANGLING_PARENT,
                    message=f"parent {node.parent!r} does not exist",
                )
            )

        if _in_cycle(promises, node.id):
            issues.append(
                CheckIssue(
                    node_id=node.id,
                    kind=CYCLE,
                    message="node is part of a parent-chain cycle",
                )
            )

        if len(node.verifiers) == 0 and len(promises.children(node.id)) == 0:
            issues.append(
                CheckIssue(
                    node_id=node.id,
                    kind=NO_BACKING,
                    message="node has no verifiers and no children, so it can never be verified",
                )
            )

    return issues


def _in_cycle(promises: PromiseSet, node_id: str) -> bool:
    """Return whether following a node's parent chain loops back to the node itself.

    Only nodes that are genuinely on the cycle are reported; a node that merely points
    into a cycle further up the chain is not.

    Args:
        promises: The promise document.
        node_id: The node whose parent chain to walk.

    Returns:
        ``True`` if the chain returns to ``node_id``.
    """
    seen: set[str] = set()
    current: str | None = promises.get(node_id).parent
    while current is not None and promises.has(current):
        if current == node_id:
            return True
        if current in seen:
            return False
        seen.add(current)
        current = promises.get(current).parent
    return False
