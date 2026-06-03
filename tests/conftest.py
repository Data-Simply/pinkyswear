"""Shared fixtures for the promise CLI test suite."""

from pathlib import Path

import pytest

from pinkyswear.model import PromiseSet

# A deterministic todo-app promise tree. ``concurrent`` fails (repo scope => blocking),
# dragging down its ancestors ``data-safe`` and ``root``; ``fast`` passes; ``deps``
# passes. Verifiers use ``true``/``false`` so they run in any environment.
TODO_YAML = """
nodes:
  - id: root
    title: "Users can trust the todo app"
    parent: null
    affects: ["todo-app/"]
    verifiers: []
  - id: data-safe
    title: "Data is never silently lost"
    parent: root
    affects: ["todo-app/storage.py"]
    verifiers: []
  - id: rollback
    title: "A failed update doesn't corrupt the list"
    parent: data-safe
    affects: ["todo-app/storage.py", "tests/test_storage_rollback.py"]
    verifiers:
      - run: "true"
        scope: repo
  - id: concurrent
    title: "Concurrent writes don't clobber each other"
    parent: data-safe
    affects: ["todo-app/storage.py"]
    verifiers:
      - run: "false"
        scope: repo
  - id: fast
    title: "Saving a todo feels instant"
    parent: root
    affects: ["todo-app/api.py", "checkout latency"]
    verifiers:
      - run: "true"
        scope: runtime
        blocking: false
  - id: deps
    title: "We ship no known-vulnerable dependencies"
    parent: root
    affects: ["uv.lock", "SOC2 compliance"]
    verifiers:
      - run: "true"
        scope: external
        blocking: true
"""


@pytest.fixture
def todo_file(tmp_path: Path) -> Path:
    """Write the todo promise document to a temp file and return its path."""
    path = tmp_path / "promises.yaml"
    path.write_text(TODO_YAML)
    return path


@pytest.fixture
def todo_promises(todo_file: Path) -> PromiseSet:
    """Load the todo promise document."""
    return PromiseSet.load(todo_file)
