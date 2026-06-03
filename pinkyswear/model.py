"""Data model for promises: nodes, verifiers, and the on-disk YAML document.

A promise set is a flat list of nodes stored under a top-level ``nodes:`` key in a
single ``promises.yaml`` file. Each node optionally references a parent node by id,
forming a tree (or forest). The CLI is the safe write path: it owns id generation
and reference integrity, while the YAML stays human-readable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The fixed set of verifier scopes. Scope is metadata only in v1: it never affects a
# verdict, it only seeds the default ``blocking`` bit (see ``DEFAULT_BLOCKING``).
VALID_SCOPES = ("repo", "artifact", "deploy", "runtime", "external")

# Default blocking policy per scope. ``external`` is intentionally absent: an external
# verifier must declare ``blocking`` explicitly, since there is no sensible default.
DEFAULT_BLOCKING: dict[str, bool] = {
    "repo": True,
    "artifact": True,
    "deploy": False,
    "runtime": False,
}

# Default per-verifier timeout in seconds when a verifier does not specify one.
DEFAULT_TIMEOUT_SECONDS = 300


class PromiseError(Exception):
    """Raised when a promise document is invalid or a reference cannot be resolved."""


@dataclass
class Verifier:
    """A single mechanical check backing a promise.

    Attributes:
        run: Shell command to execute. Exit code 0 means pass, any non-zero means fail.
        scope: One of ``VALID_SCOPES``. Metadata only; seeds the default ``blocking`` bit.
        blocking: Whether a failure of this verifier should affect the process exit code.
        cwd: Working directory for the command, relative to the repo root. Defaults to
            the repo root when ``None``.
        timeout: Maximum run time in seconds. Defaults to ``DEFAULT_TIMEOUT_SECONDS``.
    """

    run: str
    scope: str
    blocking: bool
    cwd: str | None = None
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Verifier:
        """Build a ``Verifier`` from a raw mapping, validating types and constraints.

        Args:
            data: Mapping parsed from YAML for a single verifier.

        Returns:
            A validated ``Verifier`` instance.

        Raises:
            PromiseError: If required fields are missing or values are invalid.
        """
        if not isinstance(data, dict):
            msg = f"verifier must be a mapping, got {type(data).__name__}"
            raise PromiseError(msg)

        run = data.get("run")
        if not isinstance(run, str) or len(run.strip()) == 0:
            msg = "verifier 'run' must be a non-empty string"
            raise PromiseError(msg)

        scope = data.get("scope")
        if scope not in VALID_SCOPES:
            msg = f"verifier 'scope' must be one of {VALID_SCOPES}, got {scope!r}"
            raise PromiseError(msg)

        if "blocking" in data:
            blocking = data["blocking"]
            if not isinstance(blocking, bool):
                msg = f"verifier 'blocking' must be a boolean, got {blocking!r}"
                raise PromiseError(msg)
        elif scope in DEFAULT_BLOCKING:
            blocking = DEFAULT_BLOCKING[scope]
        else:
            msg = f"verifier with scope {scope!r} must set 'blocking' explicitly"
            raise PromiseError(msg)

        cwd = data.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            msg = f"verifier 'cwd' must be a string, got {cwd!r}"
            raise PromiseError(msg)

        timeout = data.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            msg = f"verifier 'timeout' must be a positive integer, got {timeout!r}"
            raise PromiseError(msg)

        return cls(run=run, scope=scope, blocking=blocking, cwd=cwd, timeout=timeout)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a mapping suitable for YAML round-tripping.

        Returns:
            A mapping with ``cwd`` omitted when unset.
        """
        data: dict[str, Any] = {"run": self.run, "scope": self.scope, "blocking": self.blocking}
        if self.cwd is not None:
            data["cwd"] = self.cwd
        data["timeout"] = self.timeout
        return data


@dataclass
class Node:
    """A single promise the codebase makes.

    A node with no verifiers is an internal node whose verdict is purely the rollup of
    its children. A node with verifiers contributes its own pass/fail to that rollup.

    Attributes:
        id: Stable UUID identifying the node.
        title: Prose statement of the promise.
        parent: Id of the parent node, or ``None`` for a root.
        affects: Free-form tokens (file paths or concepts) used only by ``impact``.
        verifiers: Mechanical checks backing this promise.
    """

    id: str
    title: str
    parent: str | None = None
    affects: list[str] = field(default_factory=list)
    verifiers: list[Verifier] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        """Build a ``Node`` from a raw mapping, validating types and constraints.

        Args:
            data: Mapping parsed from YAML for a single node.

        Returns:
            A validated ``Node`` instance.

        Raises:
            PromiseError: If required fields are missing or values are invalid.
        """
        if not isinstance(data, dict):
            msg = f"node must be a mapping, got {type(data).__name__}"
            raise PromiseError(msg)

        node_id = data.get("id")
        if not isinstance(node_id, str) or len(node_id.strip()) == 0:
            msg = "node 'id' must be a non-empty string"
            raise PromiseError(msg)

        title = data.get("title")
        if not isinstance(title, str) or len(title.strip()) == 0:
            msg = f"node {node_id!r} 'title' must be a non-empty string"
            raise PromiseError(msg)

        parent = data.get("parent")
        if parent is not None and not isinstance(parent, str):
            msg = f"node {node_id!r} 'parent' must be a string or null, got {parent!r}"
            raise PromiseError(msg)

        affects_raw = data.get("affects", []) or []
        if not isinstance(affects_raw, list) or not all(isinstance(a, str) for a in affects_raw):
            msg = f"node {node_id!r} 'affects' must be a list of strings"
            raise PromiseError(msg)

        verifiers_raw = data.get("verifiers", []) or []
        if not isinstance(verifiers_raw, list):
            msg = f"node {node_id!r} 'verifiers' must be a list"
            raise PromiseError(msg)
        verifiers = [Verifier.from_dict(v) for v in verifiers_raw]

        return cls(id=node_id, title=title, parent=parent, affects=list(affects_raw), verifiers=verifiers)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a mapping suitable for YAML round-tripping.

        Returns:
            A mapping mirroring the on-disk node shape.
        """
        return {
            "id": self.id,
            "title": self.title,
            "parent": self.parent,
            "affects": list(self.affects),
            "verifiers": [v.to_dict() for v in self.verifiers],
        }


class PromiseSet:
    """An in-memory promise document with tree navigation and reference resolution."""

    def __init__(self, nodes: list[Node]) -> None:
        """Initialise from a list of nodes and validate id uniqueness.

        Args:
            nodes: The nodes making up the document.

        Raises:
            PromiseError: If two nodes share an id.
        """
        self._nodes: dict[str, Node] = {}
        for node in nodes:
            if node.id in self._nodes:
                msg = f"duplicate node id {node.id!r}"
                raise PromiseError(msg)
            self._nodes[node.id] = node
        # Path the document was loaded from, used to anchor verifier working dirs.
        self.source_path: Path | None = None
        # Lazily built ``parent id -> [child id]`` index, so children()/roots() and the
        # tree walks built on them are O(1) lookups rather than O(n) scans. Invalidated
        # whenever the topology changes (add/remove/set_parent).
        self._children_index: dict[str | None, list[str]] | None = None

    def _child_map(self) -> dict[str | None, list[str]]:
        """Return the parent->children id index, building it once on demand."""
        if self._children_index is None:
            index: dict[str | None, list[str]] = {}
            for node in self._nodes.values():
                index.setdefault(node.parent, []).append(node.id)
            self._children_index = index
        return self._children_index

    def _invalidate_index(self) -> None:
        """Drop the cached children index after a topology change."""
        self._children_index = None

    @property
    def nodes(self) -> list[Node]:
        """Return all nodes in insertion order."""
        return list(self._nodes.values())

    def get(self, node_id: str) -> Node:
        """Return the node with the given exact id.

        Args:
            node_id: Exact node id.

        Returns:
            The matching node.

        Raises:
            PromiseError: If no node has that id.
        """
        if node_id not in self._nodes:
            msg = f"no node with id {node_id!r}"
            raise PromiseError(msg)
        return self._nodes[node_id]

    def has(self, node_id: str) -> bool:
        """Return whether a node with the given exact id exists."""
        return node_id in self._nodes

    def children(self, node_id: str) -> list[Node]:
        """Return the direct children of a node, in document order."""
        return [self._nodes[cid] for cid in self._child_map().get(node_id, [])]

    def roots(self) -> list[Node]:
        """Return all nodes whose parent is ``None``."""
        return [self._nodes[cid] for cid in self._child_map().get(None, [])]

    def ancestors(self, node_id: str) -> list[Node]:
        """Return ancestors of a node, nearest parent first.

        Stops if a cycle or dangling parent is encountered (those are surfaced by
        ``check`` rather than raised here).

        Args:
            node_id: Id of the node whose ancestors to walk.

        Returns:
            Ancestor nodes from immediate parent upward.
        """
        result: list[Node] = []
        seen: set[str] = {node_id}
        current = self._nodes[node_id].parent
        while current is not None and current in self._nodes and current not in seen:
            seen.add(current)
            node = self._nodes[current]
            result.append(node)
            current = node.parent
        return result

    def descendants(self, node_id: str) -> list[Node]:
        """Return all descendants of a node (excluding itself) via breadth-first walk.

        Args:
            node_id: Id of the subtree root.

        Returns:
            Descendant nodes.
        """
        result: list[Node] = []
        queue = self.children(node_id)
        seen: set[str] = {node_id}
        while len(queue) > 0:
            node = queue.pop(0)
            if node.id in seen:
                continue
            seen.add(node.id)
            result.append(node)
            queue.extend(self.children(node.id))
        return result

    def resolve(self, ref: str) -> Node:
        """Resolve a CLI reference to a single node.

        A reference matches by, in order: exact id, unique id prefix, or unique
        case-insensitive title substring.

        Args:
            ref: The user-supplied reference string.

        Returns:
            The single matching node.

        Raises:
            PromiseError: If the reference matches zero or multiple nodes.
        """
        if ref in self._nodes:
            return self._nodes[ref]

        prefix_matches = [n for n in self._nodes.values() if n.id.startswith(ref)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            ids = ", ".join(n.id for n in prefix_matches)
            msg = f"reference {ref!r} is an ambiguous id prefix matching: {ids}"
            raise PromiseError(msg)

        ref_lower = ref.lower()
        title_matches = [n for n in self._nodes.values() if ref_lower in n.title.lower()]
        if len(title_matches) == 1:
            return title_matches[0]
        if len(title_matches) > 1:
            titles = "; ".join(f"{n.id[:8]} {n.title!r}" for n in title_matches)
            msg = f"reference {ref!r} matches multiple titles: {titles}"
            raise PromiseError(msg)

        msg = f"reference {ref!r} matched no node"
        raise PromiseError(msg)

    def add(self, node: Node) -> None:
        """Add a node, enforcing id uniqueness and parent existence.

        Args:
            node: The node to add.

        Raises:
            PromiseError: If the id already exists or the parent is unknown.
        """
        if node.id in self._nodes:
            msg = f"duplicate node id {node.id!r}"
            raise PromiseError(msg)
        if node.parent is not None and node.parent not in self._nodes:
            msg = f"parent {node.parent!r} does not exist"
            raise PromiseError(msg)
        self._nodes[node.id] = node
        self._invalidate_index()

    def remove(self, node_id: str) -> None:
        """Remove a node by exact id.

        Args:
            node_id: Exact id of the node to remove.

        Raises:
            PromiseError: If the node does not exist.
        """
        if node_id not in self._nodes:
            msg = f"no node with id {node_id!r}"
            raise PromiseError(msg)
        del self._nodes[node_id]
        self._invalidate_index()

    def set_parent(self, node_id: str, new_parent: str | None) -> None:
        """Reparent a node, keeping the children index consistent.

        Args:
            node_id: Id of the node to move.
            new_parent: New parent id, or ``None`` to make it a root.

        Raises:
            PromiseError: If the node or the new parent does not exist.
        """
        node = self.get(node_id)
        if new_parent is not None and new_parent not in self._nodes:
            msg = f"parent {new_parent!r} does not exist"
            raise PromiseError(msg)
        node.parent = new_parent
        self._invalidate_index()

    def would_create_cycle(self, node_id: str, new_parent: str) -> bool:
        """Return whether reparenting ``node_id`` under ``new_parent`` forms a cycle.

        Walks upward from ``new_parent`` following parent pointers; a cycle would form
        iff ``node_id`` is encountered on that chain.

        Args:
            node_id: The node being moved.
            new_parent: The proposed new parent id.

        Returns:
            ``True`` if ``new_parent`` is ``node_id`` itself or one of its descendants.
        """
        current: str | None = new_parent
        seen: set[str] = set()
        while current is not None and current in self._nodes and current not in seen:
            if current == node_id:
                return True
            seen.add(current)
            current = self._nodes[current].parent
        return False

    @classmethod
    def load(cls, path: str | Path) -> PromiseSet:
        """Load and validate a promise document from disk.

        Args:
            path: Path to the ``promises.yaml`` file.

        Returns:
            The parsed and validated ``PromiseSet``.

        Raises:
            PromiseError: If the file is missing or structurally invalid.
        """
        path = Path(path)
        if not path.exists():
            msg = f"promise file not found: {path}"
            raise PromiseError(msg)

        raw = yaml.safe_load(path.read_text()) or {}
        if not isinstance(raw, dict):
            msg = "promise file must be a mapping with a top-level 'nodes' key"
            raise PromiseError(msg)

        nodes_raw = raw.get("nodes", []) or []
        if not isinstance(nodes_raw, list):
            msg = "'nodes' must be a list"
            raise PromiseError(msg)

        promise_set = cls([Node.from_dict(n) for n in nodes_raw])
        promise_set.source_path = path
        return promise_set

    def save(self, path: str | Path) -> None:
        """Write the document to disk as YAML.

        Args:
            path: Destination path.
        """
        path = Path(path)
        data = {"nodes": [n.to_dict() for n in self._nodes.values()]}
        path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))


def new_id() -> str:
    """Return a fresh node id.

    Returns:
        A random UUID4 hex-and-dash string.
    """
    return str(uuid.uuid4())
