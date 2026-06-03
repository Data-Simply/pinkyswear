"""Rendering of verify reports at several output formats.

- ``render_text``: a human one-liner plus the root-cause failures.
- ``render_compact``: the default JSON for agents - verdict, exit, and root causes only.
- ``render_full``: the complete ``promise.verify/v1`` tree for deep inspection.
- ``render_junit``: JUnit XML so promises show up in CI test dashboards.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

from .engine import FAIL, VerifyReport
from .model import PromiseSet

SCHEMA_VERSION = "promise.verify/v1"


def _failure_detail(report: VerifyReport, node_id: str) -> str:
    """Join the detail strings of a node's failing verifiers.

    Args:
        report: The verify report.
        node_id: The node whose failing verifier details to collect.

    Returns:
        A semicolon-joined detail string.
    """
    details = [vr.detail for vr in report.results[node_id].verifier_results if vr.verdict == FAIL]
    return "; ".join(details)


def _break_titles(promises: PromiseSet, break_ids: list[str]) -> list[str]:
    """Resolve a node's ``breaks`` ids to promise titles, used by every renderer.

    Falls back to the raw id for any id that no longer resolves, so a stale reference
    degrades gracefully instead of crashing one output format.

    Args:
        promises: The promise document.
        break_ids: Ancestor ids a failing node renders unfulfillable.

    Returns:
        Human-readable titles (or raw ids when unresolvable).
    """
    titles: list[str] = []
    for break_id in break_ids:
        titles.append(promises.get(break_id).title if promises.has(break_id) else break_id)
    return titles


def render_compact(report: VerifyReport, promises: PromiseSet) -> dict[str, Any]:
    """Render the compact, agent-oriented report.

    Args:
        report: The verify report.
        promises: The promise document, used to render ``breaks`` as titles.

    Returns:
        A mapping with ``verdict``, ``exit`` and root-cause ``failed`` entries.
    """
    failed: list[dict[str, Any]] = []
    for result in report.root_cause_failures():
        failed.append(
            {
                "id": result.node.id,
                "title": result.node.title,
                "breaks": _break_titles(promises, result.breaks),
                "detail": _failure_detail(report, result.node.id),
            }
        )
    return {"verdict": report.verdict, "exit": report.exit_code, "failed": failed}


def render_full(report: VerifyReport, promises: PromiseSet) -> dict[str, Any]:
    """Render the full ``promise.verify/v1`` tree.

    Args:
        report: The verify report.
        promises: The promise document (unused beyond report data, accepted for symmetry).

    Returns:
        The complete report mapping.
    """
    nodes: list[dict[str, Any]] = []
    for node_id in report.order:
        result = report.results[node_id]
        nodes.append(
            {
                "id": result.node.id,
                "title": result.node.title,
                "parent": result.node.parent,
                "verdict": result.verdict,
                "verifiers": [
                    {
                        "run": vr.run,
                        "scope": vr.scope,
                        "blocking": vr.blocking,
                        "verdict": vr.verdict,
                        "exit": vr.exit_code,
                        "duration_ms": vr.duration_ms,
                        "detail": vr.detail,
                    }
                    for vr in result.verifier_results
                ],
                "failing_leaves": result.failing_leaves,
                "breaks": result.breaks,
            }
        )
    return {
        "schema": SCHEMA_VERSION,
        "root": report.root_id,
        "ran_at": report.ran_at,
        "summary": {"verdict": report.verdict, "exit": report.exit_code, "counts": report.counts},
        "nodes": nodes,
    }


def render_text(report: VerifyReport, promises: PromiseSet) -> str:
    """Render the human-facing plain-text report.

    Args:
        report: The verify report.
        promises: The promise document, used to render ``breaks`` as titles.

    Returns:
        A multi-line string summarising the run and its root-cause failures.
    """
    counts = report.counts
    total = counts["pass"] + counts["fail"]
    header = f"{report.verdict.upper()}  exit={report.exit_code}  {counts['pass']}/{total} promises green"
    lines = [header]
    for result in report.root_cause_failures():
        lines.append(f"  ✗ {result.node.title}")
        if len(result.breaks) > 0:
            chain = " → ".join(_break_titles(promises, result.breaks))
            lines.append(f"      breaks → {chain}")
        detail = _failure_detail(report, result.node.id)
        if len(detail) > 0:
            lines.append(f"      {detail}")
    return "\n".join(lines)


def render_junit(report: VerifyReport, promises: PromiseSet) -> str:
    """Render a JUnit XML report so promises appear in CI test dashboards.

    Each node that has verifiers becomes a ``<testcase>``; a failing node carries a
    ``<failure>`` whose message is the verifier detail and whose body lists the
    higher-level promises it breaks. Internal (verifier-less) nodes are omitted, since
    they have no evidence of their own to report.

    Args:
        report: The verify report.
        promises: The promise document, used to render ``breaks`` as titles.

    Returns:
        A JUnit XML document as a string.
    """
    testcases = [report.results[node_id] for node_id in report.order if len(report.results[node_id].node.verifiers) > 0]
    failures = sum(1 for r in testcases if r.verdict == FAIL)

    suite = ET.Element(
        "testsuite",
        {
            "name": "promise",
            "tests": str(len(testcases)),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
            # Real wall-clock time of the run; per-verifier durations sum higher than
            # this because verifiers run concurrently.
            "time": f"{report.wall_ms / 1000.0:.3f}",
            "timestamp": report.ran_at,
        },
    )
    for result in testcases:
        node_time = sum(vr.duration_ms for vr in result.verifier_results) / 1000.0
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": "promise", "name": result.node.title, "time": f"{node_time:.3f}"},
        )
        if result.verdict == FAIL:
            detail = _failure_detail(report, result.node.id)
            failure = ET.SubElement(case, "failure", {"message": detail or "promise failed"})
            body = [f"promise: {result.node.title}"]
            if len(result.breaks) > 0:
                chain = " -> ".join(_break_titles(promises, result.breaks))
                body.append(f"breaks: {chain}")
            for vr in result.verifier_results:
                if vr.verdict == FAIL:
                    body.append(f"verifier (exit {vr.exit_code}): {vr.run}")
                    body.append(f"  {vr.detail}")
            failure.text = "\n".join(body)
    ET.indent(suite)
    return ET.tostring(suite, encoding="unicode", xml_declaration=True)
