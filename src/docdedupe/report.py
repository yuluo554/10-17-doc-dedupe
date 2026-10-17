"""Report rendering for the ``report`` command (module 3, output side).

This module is a *renderer*. It reads :class:`docdedupe.cluster.Cluster`
objects and lays them out as Markdown, CSV or JSON; it never decides a tier,
never scores a member, and never applies a threshold. That split is what makes
the three formats agree: they are three serializations of one
:class:`Report` value, so a cluster that appears in the Markdown file cannot be
missing from the JSON file.

The split is also a testable promise. ``tests/test_report.py`` walks this
module's AST and fails if a decision constant or scoring function appears here,
so "the formula lives in cluster.py only" cannot quietly stop being true.

Determinism
-----------
* No timestamps: a report describes content, and two runs over an unchanged
  index must be byte-identical. When the clustering ran is recorded in the
  index (``clusters.built_at``), not in the exported report.
* No dictionary iteration order leaks into output: every mapping is written
  with sorted keys, and clusters/members arrive already sorted from
  :mod:`docdedupe.cluster`.
* ``json.dumps(..., sort_keys=True)`` plus a fixed separator and a trailing
  newline gives a byte-stable file that ``diff`` can compare across runs.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .cluster import TIER_ORDER, Cluster

# Report schema id. Bumped when a consumer-visible field changes, so a downstream
# parser can refuse a payload it does not understand instead of guessing.
REPORT_SCHEMA = "doc-dedupe-report/1"

FORMAT_MARKDOWN = "md"
FORMAT_CSV = "csv"
FORMAT_JSON = "json"
FORMATS: Tuple[str, ...] = (FORMAT_MARKDOWN, FORMAT_CSV, FORMAT_JSON)

# Column order for the CSV track. Every column is copied from a Cluster field;
# none is computed here.
CSV_COLUMNS: Tuple[str, ...] = (
    "cluster_id",
    "tier",
    "keep",
    "member_path",
    "is_keep",
    "total_score",
    "completeness",
    "recency",
    "path_depth",
    "name_clean",
    "directory",
    "reason",
)

# Dimension columns, in the order the score table stores them.
_SCORE_COLUMNS: Tuple[str, ...] = (
    "completeness",
    "recency",
    "path_depth",
    "name_clean",
    "directory",
)

TIER_LABELS: Mapping[str, str] = {
    "exact": "完全重复",
    "near": "近重复",
    "template": "模板族",
}


@dataclass(frozen=True)
class ReportMember:
    """One member row, copied from a cluster."""

    doc_id: int
    path: str
    kept: bool
    total: float
    scores: Mapping[str, float]


@dataclass(frozen=True)
class ReportCluster:
    """One cluster row, copied from a cluster."""

    cluster_id: str
    tier: str
    keep: Optional[str]
    reason: str
    members: Tuple[ReportMember, ...]


@dataclass(frozen=True)
class Report:
    """The whole report: counts plus clusters, in render order."""

    clusters: Tuple[ReportCluster, ...]

    @property
    def counts(self) -> Dict[str, int]:
        counts = {tier: 0 for tier in TIER_ORDER}
        for cluster in self.clusters:
            counts[cluster.tier] = counts.get(cluster.tier, 0) + 1
        return counts

    @property
    def member_count(self) -> int:
        return sum(len(cluster.members) for cluster in self.clusters)

    @property
    def has_findings(self) -> bool:
        """True when the report contains anything a human should look at.

        Every cluster has at least two members by construction, so any cluster
        at all is a finding. The exit-code contract maps this to code 1.
        """
        return bool(self.clusters)


def build_report(clusters: Iterable[Cluster]) -> Report:
    """Freeze clusters into the render model.

    The conversion copies values; it does not normalize, rank or filter them.
    ``kept`` is derived by comparing a member path with the cluster's ``keep``,
    which is a field lookup rather than a decision.
    """
    rows: List[ReportCluster] = []
    for cluster in clusters:
        members = tuple(
            ReportMember(
                doc_id=doc_id,
                path=display,
                kept=(cluster.keep is not None and path == cluster.keep),
                total=float(cluster.scores.get(path, {}).get("total", 0.0)),
                scores={
                    name: float(cluster.scores.get(path, {}).get(name, 0.0))
                    for name in _SCORE_COLUMNS
                },
            )
            for doc_id, path, display in zip(
                cluster.member_ids, cluster.members, cluster.displays
            )
        )
        rows.append(
            ReportCluster(
                cluster_id=cluster.cluster_id,
                tier=cluster.tier,
                keep=cluster.keep_display,
                reason=cluster.reason,
                members=members,
            )
        )
    return Report(clusters=tuple(rows))


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def report_to_dict(report: Report) -> Dict[str, object]:
    """The JSON shape, built once so ``--format json`` and tests share it."""
    counts = report.counts
    return {
        "schema": REPORT_SCHEMA,
        "summary": {
            "clusters": len(report.clusters),
            "members": report.member_count,
            "by_tier": {tier: counts.get(tier, 0) for tier in TIER_ORDER},
        },
        "clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "tier": cluster.tier,
                "tier_label": TIER_LABELS.get(cluster.tier, cluster.tier),
                "keep": cluster.keep,
                "reason": cluster.reason,
                "members": [
                    {
                        "doc_id": member.doc_id,
                        "path": member.path,
                        "kept": member.kept,
                        "scores": {
                            name: member.scores[name] for name in sorted(member.scores)
                        },
                    }
                    for member in cluster.members
                ],
            }
            for cluster in report.clusters
        ],
    }


def render_json(report: Report) -> str:
    """Byte-stable JSON: sorted keys, fixed indentation, trailing newline."""
    return (
        json.dumps(
            report_to_dict(report),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def render_csv(report: Report) -> str:
    """One row per member; cluster fields repeat on each of its rows.

    ``lineterminator="\\n"`` instead of the platform default: a CSV written on
    Windows and read on POSIX must be the same bytes, and the determinism test
    compares files, not parsed values.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for cluster in report.clusters:
        for member in cluster.members:
            writer.writerow(
                [
                    cluster.cluster_id,
                    cluster.tier,
                    cluster.keep or "",
                    member.path,
                    "true" if member.kept else "false",
                    "%.6f" % member.total,
                    "%.6f" % member.scores["completeness"],
                    "%.6f" % member.scores["recency"],
                    "%.6f" % member.scores["path_depth"],
                    "%.6f" % member.scores["name_clean"],
                    "%.6f" % member.scores["directory"],
                    cluster.reason,
                ]
            )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md_cell(value: str) -> str:
    """Escape the characters that would break a Markdown table cell."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Report) -> str:
    """Human-readable report: a summary, then one section per cluster."""
    counts = report.counts
    lines: List[str] = []
    lines.append("# 文档重复报告")
    lines.append("")
    lines.append(
        "共 %d 簇、%d 份文件。本报告只给建议：程序永不删除文件，"
        "任何处置都需要人工确认。" % (len(report.clusters), report.member_count)
    )
    lines.append("")
    lines.append("| 档位 | 簇数 | 说明 |")
    lines.append("|---|---|---|")
    explanations = {
        "exact": "归一化正文逐字节一致，建议保留 1 份",
        "near": "正文相似度达到近重复阈值，建议保留 1 份并复核差异",
        "template": "同构骨架 + 字段位差异，单列一档，需人工裁决",
    }
    for tier in TIER_ORDER:
        lines.append(
            "| %s | %d | %s |"
            % (TIER_LABELS[tier], counts.get(tier, 0), explanations[tier])
        )
    lines.append("")

    if not report.clusters:
        lines.append("没有发现重复文档。")
        lines.append("")
        return "\n".join(lines)

    for cluster in report.clusters:
        lines.append(
            "## %s · %s（%d 份）"
            % (TIER_LABELS.get(cluster.tier, cluster.tier), cluster.cluster_id, len(cluster.members))
        )
        lines.append("")
        if cluster.keep is None:
            lines.append("- 建议保留：无（模板族只提示人工裁决，不建议批量处置）")
        else:
            lines.append("- 建议保留：`%s`" % cluster.keep)
        lines.append("- 理由：%s" % cluster.reason)
        lines.append("")
        lines.append("| 文件 | 保留 | 综合分 | 完整度 | 修改时间 | 路径层级 | 文件名 | 目录 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for member in cluster.members:
            lines.append(
                "| `%s` | %s | %.3f | %.3f | %.3f | %.3f | %.3f | %.3f |"
                % (
                    _md_cell(member.path),
                    "是" if member.kept else "",
                    member.total,
                    member.scores["completeness"],
                    member.scores["recency"],
                    member.scores["path_depth"],
                    member.scores["name_clean"],
                    member.scores["directory"],
                )
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "相似度是机械测量值，不构成抄袭或侵权判断；所有处置都只是建议，且永远可逆。"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def render(report: Report, fmt: str) -> str:
    """Render one report in one format.

    The only place the three formats are dispatched, so a new format cannot be
    added without deciding how it consumes :class:`Report`.
    """
    if fmt == FORMAT_MARKDOWN:
        return render_markdown(report)
    if fmt == FORMAT_CSV:
        return render_csv(report)
    if fmt == FORMAT_JSON:
        return render_json(report)
    raise ValueError("unknown report format %r" % fmt)


__all__ = [
    "CSV_COLUMNS",
    "FORMATS",
    "FORMAT_CSV",
    "FORMAT_JSON",
    "FORMAT_MARKDOWN",
    "REPORT_SCHEMA",
    "Report",
    "ReportCluster",
    "ReportMember",
    "TIER_LABELS",
    "build_report",
    "render",
    "render_csv",
    "render_json",
    "render_markdown",
    "report_to_dict",
]
