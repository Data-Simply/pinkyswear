"""Static impact analysis: which promises name a given file or concept.

``impact`` performs a purely static match of a query against every node's ``affects``
tokens (and its verifiers' ``run``/``cwd`` strings). Path-shaped tokens are normalised
to repo-relative POSIX form on both sides before matching, the way pre-commit normalises
paths; non-path "concept" tokens match case-insensitively.

Known limitation: this matches only the literal strings authors record. It does not
trace the implementation files a test depends on (there is no coverage analysis in v1);
an agent that needs that precision can run coverage itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import PromiseSet


@dataclass
class ImpactMatch:
    """A node matched by an impact query and the tokens that matched it."""

    node_id: str
    title: str
    matched: list[str] = field(default_factory=list)


def _is_path_like(token: str) -> bool:
    """Return whether a token looks like a file path rather than a concept.

    A token is path-like when it has no whitespace and either contains a ``/`` or ends
    in a file extension (``.ext``). This deliberately excludes prose-with-dots concepts.

    Args:
        token: The token to classify.

    Returns:
        ``True`` for path-shaped tokens.
    """
    if len(token.split()) != 1:
        return False
    return "/" in token or bool(re.search(r"\.\w+$", token))


def _segments(path: str) -> list[str]:
    """Split a normalised POSIX path into its non-empty segments."""
    return [seg for seg in path.split("/") if len(seg) > 0]


def _is_contiguous_sublist(needle: list[str], haystack: list[str]) -> bool:
    """Return whether ``needle`` appears as a contiguous run within ``haystack``."""
    if len(needle) == 0 or len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return True
    return False


def _normalize_path(token: str, repo_root: Path | None) -> str:
    """Normalise a path-like token to repo-relative POSIX form.

    Args:
        token: The path-like token.
        repo_root: The repository root, used to relativise absolute paths.

    Returns:
        A normalised POSIX path string (leading ``./`` stripped, backslashes converted).
    """
    text = token.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if repo_root is not None:
        candidate = Path(token)
        if candidate.is_absolute():
            try:
                text = candidate.resolve().relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                text = candidate.as_posix()
    return text.rstrip("/")


def _tokens_match(query: str, token: str, repo_root: Path | None) -> bool:
    """Return whether a query matches a single token.

    Path-like pairs match by substring on their normalised POSIX form (in either
    direction, so ``storage.py`` matches ``todo-app/storage.py``). Otherwise the match
    is a case-insensitive substring comparison.

    Args:
        query: The user's query string.
        token: A single ``affects``/``run``/``cwd`` token.
        repo_root: The repository root for path normalisation.

    Returns:
        ``True`` if the query matches the token.
    """
    if _is_path_like(query) and _is_path_like(token):
        # Match on whole path segments, not raw substrings: one path's segments must be
        # a contiguous run of the other's. So `storage.py` matches `todo-app/storage.py`
        # and the directory `todo-app/` matches files under it, but `.py` does not match
        # `storage.py` and `auth.s` does not match `auth.session`.
        q_segments = _segments(_normalize_path(query, repo_root))
        t_segments = _segments(_normalize_path(token, repo_root))
        return _is_contiguous_sublist(q_segments, t_segments) or _is_contiguous_sublist(t_segments, q_segments)
    q = query.lower().strip()
    t = token.lower().strip()
    return q in t or t in q


def impact(promises: PromiseSet, query: str, repo_root: Path | None = None) -> list[ImpactMatch]:
    """Return promises whose ``affects`` or verifier strings match a query.

    Args:
        promises: The promise document.
        query: The file path or concept to search for.
        repo_root: Optional repo root for path normalisation; defaults to the document's
            source directory when available.

    Returns:
        Matching nodes in document order, each with the tokens that matched.
    """
    if repo_root is None and promises.source_path is not None:
        repo_root = Path(promises.source_path).resolve().parent

    matches: list[ImpactMatch] = []
    for node in promises.nodes:
        matched: list[str] = []
        for token in node.affects:
            if _tokens_match(query, token, repo_root):
                matched.append(token)
        for verifier in node.verifiers:
            if _tokens_match(query, verifier.run, repo_root):
                matched.append(verifier.run)
            if verifier.cwd is not None and _tokens_match(query, verifier.cwd, repo_root):
                matched.append(verifier.cwd)
        if len(matched) > 0:
            matches.append(ImpactMatch(node_id=node.id, title=node.title, matched=matched))
    return matches
