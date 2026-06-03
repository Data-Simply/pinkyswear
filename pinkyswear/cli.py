"""Command-line interface for the ``promise`` tool.

The CLI is the safe write path for ``promises.yaml``: it owns id generation and
reference integrity. Commands: ``add``, ``rm``, ``mv``, ``edit``, ``check``,
``verify`` and ``impact``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .check import check
from .engine import verify
from .impact import impact
from .model import VALID_SCOPES, Node, PromiseError, PromiseSet, Verifier, new_id
from .render import render_compact, render_full, render_junit, render_text

DEFAULT_FILE = "promises.yaml"


def _load(path: str) -> PromiseSet:
    """Load a promise document or exit with a clear error.

    Args:
        path: Path to the promise file.

    Returns:
        The loaded document.
    """
    try:
        return PromiseSet.load(path)
    except PromiseError as exc:
        _fail(str(exc))


def _load_or_empty(path: str) -> PromiseSet:
    """Load a promise document, or return an empty one if the file is absent.

    Args:
        path: Path to the promise file.

    Returns:
        The loaded or freshly created document.
    """
    if Path(path).exists():
        return _load(path)
    empty = PromiseSet([])
    empty.source_path = Path(path)
    return empty


def _fail(message: str) -> None:
    """Print an error to stderr and exit with status 2.

    Args:
        message: The error message.
    """
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _resolve_or_fail(promises: PromiseSet, ref: str) -> Node:
    """Resolve a node reference or exit with a clear error.

    Args:
        promises: The promise document.
        ref: A node id, id-prefix, or title substring.

    Returns:
        The resolved node.
    """
    try:
        return promises.resolve(ref)
    except PromiseError as exc:
        _fail(str(exc))


def _cmd_add(args: argparse.Namespace) -> int:
    """Add a new promise node.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    promises = _load_or_empty(args.file)

    parent_id: str | None = None
    if args.parent is not None:
        parent_id = _resolve_or_fail(promises, args.parent).id

    verifiers: list[Verifier] = []
    if args.verify is not None:
        if args.scope is None:
            _fail("--scope is required when --verify is given")
        verifier_data: dict[str, object] = {"run": args.verify, "scope": args.scope}
        if args.blocking is not None:
            verifier_data["blocking"] = args.blocking
        if args.timeout is not None:
            verifier_data["timeout"] = args.timeout
        try:
            verifiers.append(Verifier.from_dict(verifier_data))
        except PromiseError as exc:
            _fail(str(exc))

    node = Node(
        id=new_id(),
        title=args.title,
        parent=parent_id,
        affects=list(args.affects),
        verifiers=verifiers,
    )
    try:
        promises.add(node)
    except PromiseError as exc:
        _fail(str(exc))
    promises.save(args.file)
    print(node.id)
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    """Remove a promise node (and optionally its subtree).

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    promises = _load(args.file)
    node = _resolve_or_fail(promises, args.ref)

    children = promises.children(node.id)
    if len(children) > 0 and not args.cascade:
        names = ", ".join(c.id[:8] for c in children)
        _fail(f"node {node.id[:8]} has {len(children)} child(ren) ({names}); pass --cascade to remove them too")

    ancestors = promises.ancestors(node.id)
    to_remove = [node, *promises.descendants(node.id)] if args.cascade else [node]
    for target in to_remove:
        promises.remove(target.id)
    promises.save(args.file)

    print(f"removed {len(to_remove)} node(s)")
    if len(ancestors) > 0:
        chain = " → ".join(a.title for a in ancestors)
        print(f"warning: this may weaken the backing of: {chain}", file=sys.stderr)
    return 0


def _cmd_mv(args: argparse.Namespace) -> int:
    """Reparent a promise node, guarding against cycles.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    promises = _load(args.file)
    node = _resolve_or_fail(promises, args.ref)

    new_parent: str | None = None
    if args.parent.lower() not in ("none", "null", "root"):
        new_parent = _resolve_or_fail(promises, args.parent).id
        if promises.would_create_cycle(node.id, new_parent):
            _fail(f"moving {node.id[:8]} under {new_parent[:8]} would create a cycle")

    promises.set_parent(node.id, new_parent)
    promises.save(args.file)
    print(f"moved {node.id[:8]} under {'root' if new_parent is None else new_parent[:8]}")
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    """Edit a node's title and/or affects list.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    promises = _load(args.file)
    node = _resolve_or_fail(promises, args.ref)

    if args.title is None and args.affects is None:
        _fail("nothing to edit; pass --title and/or --affects")
    if args.title is not None:
        node.title = args.title
    if args.affects is not None:
        node.affects = list(args.affects)
    promises.save(args.file)
    print(f"updated {node.id[:8]}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Lint the promise document for structural problems.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code (1 if any issue found).
    """
    promises = _load(args.file)
    issues = check(promises)
    if len(issues) == 0:
        print("ok: no issues found")
        return 0
    for issue in issues:
        print(f"{issue.node_id[:8]}  [{issue.kind}] {issue.message}")
    print(f"\n{len(issues)} issue(s) found", file=sys.stderr)
    return 1


def _cmd_verify(args: argparse.Namespace) -> int:
    """Run verifiers and roll up verdicts.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code from the verify report.
    """
    if args.full and not args.json:
        _fail("--full only applies with --json")
    if args.workers is not None and args.workers < 1:
        _fail("--workers must be a positive integer")

    promises = _load(args.file)
    try:
        report = verify(promises, root_ref=args.ref, fail_on=args.fail_on, max_workers=args.workers)
    except PromiseError as exc:
        _fail(str(exc))

    if args.junit is not None:
        Path(args.junit).write_text(render_junit(report, promises))

    # JSON is the machine format and stands alone; otherwise always emit the human
    # verdict line (even alongside --junit, so an interactive run is never silent).
    if args.json and args.full:
        print(json.dumps(render_full(report, promises), indent=2))
    elif args.json:
        print(json.dumps(render_compact(report, promises), indent=2))
    else:
        print(render_text(report, promises))
    return report.exit_code


def _cmd_impact(args: argparse.Namespace) -> int:
    """Report which promises name a given file or concept.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code (0 always; absence of matches is not an error).
    """
    promises = _load(args.file)
    matches = impact(promises, args.query)
    if args.json:
        payload = [{"id": m.node_id, "title": m.title, "matched": m.matched} for m in matches]
        print(json.dumps(payload, indent=2))
        return 0
    if len(matches) == 0:
        print(f"no promises reference {args.query!r}")
        return 0
    for match in matches:
        print(f"{match.node_id[:8]}  {match.title}")
        print(f"      matched: {', '.join(match.matched)}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser with all subcommands.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(prog="promise", description=__doc__)
    parser.add_argument("-f", "--file", default=DEFAULT_FILE, help="path to promises.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add a promise")
    p_add.add_argument("title", help="prose statement of the promise")
    p_add.add_argument("--parent", help="parent node reference (id prefix or title)")
    p_add.add_argument("--verify", help="shell command backing this promise")
    p_add.add_argument("--scope", choices=VALID_SCOPES, help="verifier scope (required with --verify)")
    p_add.add_argument(
        "--blocking",
        dest="blocking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="whether a failure affects the exit code (defaults per scope)",
    )
    p_add.add_argument("--timeout", type=int, help="verifier timeout in seconds")
    p_add.add_argument("--affects", action="append", default=[], help="file path or concept (repeatable)")
    p_add.set_defaults(func=_cmd_add)

    p_rm = sub.add_parser("rm", help="remove a promise")
    p_rm.add_argument("ref", help="node reference (id prefix or title)")
    p_rm.add_argument("--cascade", action="store_true", help="also remove descendants")
    p_rm.set_defaults(func=_cmd_rm)

    p_mv = sub.add_parser("mv", help="reparent a promise")
    p_mv.add_argument("ref", help="node reference (id prefix or title)")
    p_mv.add_argument("--parent", required=True, help="new parent reference, or 'root' for a top-level promise")
    p_mv.set_defaults(func=_cmd_mv)

    p_edit = sub.add_parser("edit", help="edit a promise's title or affects")
    p_edit.add_argument("ref", help="node reference (id prefix or title)")
    p_edit.add_argument("--title", help="new title")
    p_edit.add_argument("--affects", action="append", help="replacement affects token (repeatable)")
    p_edit.set_defaults(func=_cmd_edit)

    p_check = sub.add_parser("check", help="lint the promise document")
    p_check.set_defaults(func=_cmd_check)

    p_verify = sub.add_parser("verify", help="run verifiers and roll up verdicts")
    p_verify.add_argument("ref", nargs="?", help="optional subtree root reference")
    p_verify.add_argument("--json", action="store_true", help="emit JSON")
    p_verify.add_argument("--full", action="store_true", help="with --json, emit the full tree")
    p_verify.add_argument(
        "--fail-on",
        choices=["blocking", "any"],
        default="blocking",
        help="exit non-zero on blocking failures only (default) or any failure",
    )
    p_verify.add_argument("--junit", metavar="PATH", help="write a JUnit XML report to PATH")
    p_verify.add_argument(
        "--workers",
        type=int,
        default=None,
        help="max concurrent verifier processes (default: auto; 1 forces serial)",
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_impact = sub.add_parser("impact", help="find promises that name a file or concept")
    p_impact.add_argument("query", help="file path or concept to search for")
    p_impact.add_argument("--json", action="store_true", help="emit JSON")
    p_impact.set_defaults(func=_cmd_impact)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
