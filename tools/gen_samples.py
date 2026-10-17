# -*- coding: utf-8 -*-
"""Synthetic sample corpus generator with planted truth.

Everything under ``data/samples/`` is produced by this file. There are three
rules, and each one is enforced rather than documented:

1. **Determinism.** A self-contained splitmix64 generator is used instead of
   ``random`` (whose sequence is not promised across Python versions). No system
   clock, no environment, no filesystem ordering leaks into the output; two runs
   with the same seed are byte-identical. ``--check`` proves it.
2. **Truth is input-only.** ``truth/manifest.json`` records group membership and
   the intended difference kind. It never records a similarity score or any
   ``observed_*`` field, because the engine must compute those at run time.
3. **No real data.** Every organisation, project and person comes from the
   whitelist in ``data/README.md``; ``_assert_entities_whitelisted`` fails the
   build if a document names anything else.

Layout (``plan/04`` section 4)::

    data/samples/
      G001_项目方案/S0001_a.docx ...
      negative/N001_....md
      skipped/S9001_scanonly.pdf
      truth/manifest.json

Usage::

    py -X utf8 tools/gen_samples.py            # (re)generate data/samples
    py -X utf8 tools/gen_samples.py --check    # verify frozen bytes
    py -X utf8 tools/gen_samples.py --bulk DIR --bulk-count 1000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docxutil  # noqa: E402
import pdfutil  # noqa: E402

GENERATOR_VERSION = 1
DEFAULT_SEED = 20261017

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "data" / "samples"
MANIFEST_REL = "truth/manifest.json"

TIER_VALUES = ("exact", "near", "template", "negative")
VARIANT_VALUES = (
    "original",
    "rewrite",
    "truncate",
    "reformat",
    "template",
    "fullwidth",
    "header_footer",
    "no_text_layer",
    "encrypted",
    "noise",
)
SKIP_VARIANTS = {"no_text_layer", "encrypted"}

ORG_A = "示例科技有限公司A"
ORG_B = "示例科技有限公司B"
ORG_C = "示例建设集团C"
PROJECTS = ("示例项目甲", "示例项目乙", "示例标段一")
PERSONS = ("张三", "李四", "王五", "赵六")

WHITELIST_ORGS = (ORG_A, ORG_B, ORG_C)
WHITELIST_PROJECTS = PROJECTS
WHITELIST_PERSONS = PERSONS


# ---------------------------------------------------------------------------
# Deterministic RNG (splitmix64)
# ---------------------------------------------------------------------------


class SplitMix64:
    """The reference splitmix64 sequence, wrapped in the helpers we need."""

    _MASK = (1 << 64) - 1
    _GOLDEN = 0x9E3779B97F4A7C15

    def __init__(self, seed: int) -> None:
        self.state = seed & self._MASK

    def next_u64(self) -> int:
        self.state = (self.state + self._GOLDEN) & self._MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self._MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self._MASK
        return (z ^ (z >> 31)) & self._MASK

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ValueError("upper bound must be positive")
        # Rejection sampling keeps the distribution unbiased; the loop is bounded
        # in practice because the rejection probability is below 1/2.
        limit = (self._MASK // upper) * upper
        while True:
            value = self.next_u64()
            if value < limit:
                return value % upper

    def choice(self, items: Sequence[str]) -> str:
        if not items:
            raise ValueError("cannot choose from an empty sequence")
        return items[self.randbelow(len(items))]

    def sample(self, items: Sequence[str], count: int) -> List[str]:
        pool = list(items)
        picked: List[str] = []
        for _ in range(min(count, len(pool))):
            index = self.randbelow(len(pool))
            picked.append(pool.pop(index))
        return picked

    def int_in(self, low: int, high: int) -> int:
        return low + self.randbelow(high - low + 1)


def _seed_for(*parts: str) -> int:
    """Derive a stable per-group seed from labels, independent of ``hash()``."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


# ---------------------------------------------------------------------------
# Prose construction
# ---------------------------------------------------------------------------


TOPICS: Dict[str, Dict[str, object]] = {
    "investment": {
        "title": "投资可行性研究",
        "terms": ["总投资额", "建设期", "财务内部收益率", "投资回收期", "流动资金", "资本金", "折旧年限", "敏感性分析"],
        "verbs": ["测算", "复核", "论证", "估算"],
        "nouns": ["资金来源", "收益测算", "成本结构", "风险敞口"],
    },
    "procurement": {
        "title": "采购与招标安排",
        "terms": ["评标办法", "投标保证金", "履约担保", "供货周期", "验收标准", "报价清单", "资格条件", "合同价款"],
        "verbs": ["核定", "比对", "确认", "登记"],
        "nouns": ["供应商名录", "采购批次", "到货计划", "质量证明"],
    },
    "safety": {
        "title": "安全生产管理",
        "terms": ["安全交底", "临边防护", "特种作业", "应急预案", "隐患排查", "教育培训", "事故演练", "责任清单"],
        "verbs": ["排查", "落实", "交底", "复查"],
        "nouns": ["作业班组", "风险分级", "整改闭环", "监护措施"],
    },
    "environment": {
        "title": "环境保护措施",
        "terms": ["扬尘控制", "噪声监测", "废水沉淀", "固废分类", "植被恢复", "环境监理", "排放限值", "监测频次"],
        "verbs": ["监测", "控制", "记录", "整改"],
        "nouns": ["施工现场", "敏感点位", "环保台账", "治理设施"],
    },
    "schedule": {
        "title": "进度计划说明",
        "terms": ["关键线路", "里程碑节点", "总工期", "资源投入", "流水段划分", "雨季施工", "交叉作业", "进度偏差"],
        "verbs": ["排定", "复核", "调整", "跟踪"],
        "nouns": ["施工组织", "阶段目标", "资源计划", "纠偏措施"],
    },
    "quality": {
        "title": "质量管理体系",
        "terms": ["隐蔽验收", "材料复检", "实测实量", "质量交底", "样板引路", "不合格品", "检验批", "整改通知"],
        "verbs": ["抽检", "验收", "复核", "标识"],
        "nouns": ["质量控制点", "验收记录", "责任主体", "处理流程"],
    },
    "training": {
        "title": "人员培训方案",
        "terms": ["培训课时", "考核方式", "师资安排", "课程大纲", "参训名单", "实操演练", "结业标准", "复训周期"],
        "verbs": ["组织", "安排", "考核", "记录"],
        "nouns": ["培训计划", "教学资源", "培训档案", "效果评估"],
    },
    "audit": {
        "title": "内部审计要点",
        "terms": ["审计范围", "抽样比例", "凭证附件", "审批流程", "资金流向", "合同履行", "整改期限", "复核意见"],
        "verbs": ["抽查", "核对", "追溯", "复核"],
        "nouns": ["审计底稿", "问题清单", "责任部门", "整改结果"],
    },
    "logistics": {
        "title": "物流与仓储组织",
        "terms": ["到货批次", "库容规划", "装卸机械", "运输路线", "库存周转", "出入库单据", "盘点周期", "损耗控制"],
        "verbs": ["调度", "盘点", "核对", "安排"],
        "nouns": ["仓储区域", "运输车辆", "交接记录", "作业窗口"],
    },
    "design": {
        "title": "设计深化说明",
        "terms": ["设计输入", "专业接口", "图纸会审", "变更流程", "计算书", "构造做法", "材料选型", "设计交底"],
        "verbs": ["深化", "会审", "确认", "归档"],
        "nouns": ["设计文件", "专业协调", "版本控制", "技术条件"],
    },
    "acceptance": {
        "title": "竣工验收组织",
        "terms": ["验收条件", "资料清单", "实体检查", "功能测试", "整改闭环", "验收结论", "移交手续", "缺陷责任期"],
        "verbs": ["组织验收", "核查", "确认", "移交"],
        "nouns": ["验收小组", "竣工资料", "遗留问题", "移交清单"],
    },
    "maintenance": {
        "title": "运行维护安排",
        "terms": ["巡检周期", "保养计划", "备件库存", "故障响应", "运行记录", "能耗指标", "检修窗口", "责任分工"],
        "verbs": ["巡检", "保养", "记录", "响应"],
        "nouns": ["运维班组", "设备台账", "服务时限", "处置流程"],
    },
}

SENTENCE_TEMPLATES = (
    "本阶段围绕{term_a}开展{verb}，{term_b}按既定口径取值，并在{m_noun}中留档。",
    "经{verb}，{term_a}较上一版调整{n1}个百分点，{term_b}保持稳定，{m_noun}同步更新。",
    "结合{term_a}与{term_b}的实际条件，{verb}结论为：{m_noun}满足当前阶段要求。",
    "在{m_noun}层面，{term_a}按{n2}个工作日的节奏推进，{term_b}由责任小组逐项确认。",
    "{verb}{term_a}时需同步核对{term_b}，偏差超过{n1}个百分点即启动纠偏程序。",
    "针对{term_a}，本文件明确了{m_noun}的归属与接口，{term_b}作为复核依据一并归档。",
    "上一周期{term_a}累计{n3}项，其中与{term_b}相关的记录已纳入{m_noun}。",
    "{m_noun}显示，{term_a}与{term_b}的匹配度满足约定，后续按{n2}日周期复评。",
)

BOILERPLATE = (
    "本文件由{org} 编制，仅用于{project} 的内部讨论，未经许可不得对外提供。",
    "文件版本以档首页记录为准，修订内容由编制单位统一登记，历史版本按编号归档。",
    "文中涉及的金额、工期与数量均为示例数据，不代表任何真实合同的约定。",
    "本文件的分发范围限于项目组成员与受邀评审人员，收到后请勿二次转发。",
    "如对本文内容有异议，请在评审会上提出并形成书面意见，由编制单位统一答复。",
)


def _topic_paragraphs(rng: SplitMix64, topic_key: str, count: int) -> List[str]:
    topic = TOPICS[topic_key]
    terms = list(topic["terms"])  # type: ignore[arg-type]
    verbs = list(topic["verbs"])  # type: ignore[arg-type]
    nouns = list(topic["nouns"])  # type: ignore[arg-type]

    paragraphs: List[str] = []
    for index in range(count):
        template = SENTENCE_TEMPLATES[index % len(SENTENCE_TEMPLATES)]
        chosen_terms = rng.sample(terms, 2)
        paragraph = template.format(
            term_a=chosen_terms[0],
            term_b=chosen_terms[1],
            verb=rng.choice(verbs),
            m_noun=rng.choice(nouns),
            n1=rng.int_in(1, 9),
            n2=rng.int_in(3, 20),
            n3=rng.int_in(12, 90),
        )
        paragraphs.append(paragraph)
    return paragraphs


def _base_document(rng: SplitMix64, topic_key: str, heading: str, paragraph_count: int = 10) -> List[str]:
    title = "%s · %s" % (rng.choice(WHITELIST_PROJECTS), TOPICS[topic_key]["title"])
    paragraphs = [title, "编制单位：%s" % rng.choice(WHITELIST_ORGS), "编制人：%s" % rng.choice(WHITELIST_PERSONS)]
    paragraphs.extend(_topic_paragraphs(rng, topic_key, paragraph_count))
    paragraphs.append(BOILERPLATE[0].format(org=rng.choice(WHITELIST_ORGS), project=rng.choice(WHITELIST_PROJECTS)))
    if heading:
        paragraphs.insert(3, heading)
    return paragraphs


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def _require_change(before: str, after: str, label: str) -> None:
    """Fail loudly when an injector silently produced the original text.

    A ``str.replace`` whose anchor no longer matches returns the input unchanged,
    which would plant a "variant" that is byte-identical to its base and quietly
    turn a recall test into a tautology.
    """
    if before == after:
        raise AssertionError("mutation %r did not change the text" % label)


SYNONYMS: Tuple[Tuple[str, str], ...] = (
    ("本阶段", "现阶段"),
    ("按既定口径", "依统一口径"),
    ("逐项确认", "逐条核定"),
    ("同步更新", "一并修订"),
    ("满足当前阶段要求", "符合本阶段需要"),
    ("启动纠偏程序", "进入纠偏流程"),
    ("作为复核依据", "作为核对依据"),
    ("纳入", "归入"),
    ("编制单位", "编撰单位"),
    ("示例数据", "演示数据"),
    ("责任小组", "责任班组"),
    ("复核依据", "核对依据"),
    ("累计", "合计"),
    ("保持稳定", "维持稳定"),
    ("统一登记", "集中登记"),
    ("统一答复", "集中答复"),
)


def _rewrite(paragraphs: Sequence[str], rng: SplitMix64, paragraphs_to_touch: int = 3) -> List[str]:
    """Replace a few phrases on a few paragraphs, keeping the structure intact.

    The rewrite is deliberately light: a near-duplicate should stay above the
    similarity floor while no longer being byte-identical. Heavy rewriting would
    make the recall benchmark measure the threshold instead of the algorithm.
    """
    result = list(paragraphs)
    indices = sorted(rng.randbelow(len(result)) for _ in range(paragraphs_to_touch))
    changed = False
    for index in indices:
        text = result[index]
        for before, after in SYNONYMS:
            if before in text and rng.randbelow(2) == 0:
                result[index] = text.replace(before, after, 1)
                changed = True
                break
    if not changed:
        # Deterministic fallback so the variant always differs from its base.
        before, after = SYNONYMS[rng.randbelow(len(SYNONYMS))]
        for index, text in enumerate(result):
            if before in text:
                result[index] = text.replace(before, after, 1)
                changed = True
                break
    _require_change("\n".join(paragraphs), "\n".join(result), "rewrite")
    return result


def _truncate(paragraphs: Sequence[str], where: str, drop: int = 2) -> List[str]:
    """Drop ``drop`` consecutive paragraphs from the head, middle or tail."""
    body = list(paragraphs)
    if len(body) < drop + 4:
        raise ValueError("document too short to truncate")
    if where == "head":
        start = 3  # keep title and attribution lines
    elif where == "middle":
        start = max(3, (len(body) - drop) // 2)
    elif where == "tail":
        start = len(body) - drop
    else:
        raise ValueError("unknown truncation position %r" % where)
    result = body[:start] + body[start + drop :]
    _require_change("\n".join(body), "\n".join(result), "truncate-%s" % where)
    return result


def _fullwidth_text(text: str) -> str:
    """ASCII letters, digits and the hyphen become their fullwidth forms."""
    out = []
    for char in text:
        code = ord(char)
        if 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        elif char == " ":
            out.append("\u3000")
        else:
            out.append(char)
    return "".join(out)


def _fullwidth(paragraphs: Sequence[str]) -> List[str]:
    """Convert the whole document and prove it actually changed.

    The change check runs on the joined document, not per line: a line made only
    of Chinese characters has no ASCII to convert, which is expected, while the
    document as a whole always contains digits or Latin text.
    """
    before = "\n".join(paragraphs)
    converted = [_fullwidth_text(line) for line in paragraphs]
    _require_change(before, "\n".join(converted), "fullwidth")
    return converted


def _as_html(paragraphs: Sequence[str], title: str) -> bytes:
    rows = []
    for index, paragraph in enumerate(paragraphs):
        if index == 0:
            rows.append("<h1>%s</h1>" % _escape(paragraph))
        elif len(paragraph) < 30:
            rows.append("<p class=\"meta\">%s</p>" % _escape(paragraph))
        else:
            rows.append("<p>%s</p>" % _escape(paragraph))
    body = "\n".join(rows)
    document = (
        "<!DOCTYPE html>\n"
        "<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<title>%s</title>\n</head>\n<body>\n%s\n</body>\n</html>\n" % (_escape(title), body)
    )
    return document.encode("utf-8")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _as_markdown(paragraphs: Sequence[str]) -> bytes:
    lines = ["# %s" % paragraphs[0], ""]
    for paragraph in paragraphs[1:]:
        lines.append(paragraph)
        lines.append("")
    return ("\n".join(lines)).encode("utf-8")


def _as_text(paragraphs: Sequence[str]) -> bytes:
    return ("\n".join(paragraphs) + "\n").encode("utf-8")


def _as_docx(paragraphs: Sequence[str], *, with_table: bool = False) -> bytes:
    import io

    from docx import Document

    document = Document()
    docxutil.pin_core_properties(document, title=paragraphs[0][:60], author=ORG_A)
    for index, paragraph in enumerate(paragraphs):
        if index == 0:
            document.add_heading(paragraph, level=1)
        else:
            document.add_paragraph(paragraph)
    if with_table:
        table = document.add_table(rows=3, cols=2)
        table.cell(0, 0).text = "字段"
        table.cell(0, 1).text = "取值"
        table.cell(1, 0).text = "合同价款"
        table.cell(1, 1).text = "1234.56 万元"
        table.cell(2, 0).text = "履约期限"
        table.cell(2, 1).text = "18 个月"
    buffer = io.BytesIO()
    document.save(buffer)
    return docxutil.normalise_zip(buffer.getvalue())


def _as_pdf(
    paragraphs: Sequence[str],
    *,
    per_page: int = 8,
    header: str = "",
    footer_template: str = "",
) -> bytes:
    pages: List[List[str]] = []
    for start in range(0, len(paragraphs), per_page):
        pages.append(list(paragraphs[start : start + per_page]))
    return pdfutil.build_text_pdf(pages, header=header, footer_template=footer_template)


# ---------------------------------------------------------------------------
# Corpus assembly
# ---------------------------------------------------------------------------


class Corpus:
    """Accumulates files and truth records, then serialises once."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.files: Dict[str, bytes] = {}
        self.groups: List[dict] = []
        self.skipped: List[dict] = []
        self.unsupported: List[dict] = []
        self._sample_counter = 0

    def add(self, rel_path: str, payload: bytes) -> None:
        if rel_path in self.files:
            raise AssertionError("duplicate sample path %r" % rel_path)
        self.files[rel_path] = payload

    def next_sample_id(self) -> str:
        self._sample_counter += 1
        return "S%04d" % self._sample_counter

    # -- group helpers -----------------------------------------------------

    def add_group(
        self,
        group_id: str,
        dir_name: str,
        tier: str,
        members: Sequence[Tuple[str, str, str, bytes]],
        *,
        base: str,
        diff_kind: str,
    ) -> str:
        """Register one truth group.

        ``members`` is ``(file_name, variant, diff_kind, payload)``; the file is
        stored under ``<dir_name>/<file_name>`` and the manifest records that
        relative path so a consumer never has to guess the directory.

        The group's own ``variant`` is the base member's variant. Using the base
        keeps the field inside the documented enum even when the group mixes
        rewrites and truncations; the per-member ``variant`` carries the detail.
        """
        if tier not in TIER_VALUES:
            raise AssertionError("unknown tier %r" % tier)
        sample_id = self.next_sample_id()
        rel_dir = dir_name
        recorded = []
        base_variant = None
        for file_name, variant, member_diff, payload in members:
            if variant not in VARIANT_VALUES:
                raise AssertionError("unknown variant %r" % variant)
            rel_path = "%s/%s" % (rel_dir, file_name)
            self.add(rel_path, payload)
            recorded.append({"file": rel_path, "variant": variant, "diff_kind": member_diff})
            if file_name == base:
                base_variant = variant
        base_path = "%s/%s" % (rel_dir, base)
        if base_variant is None:
            raise AssertionError("base %r is not a member of %s" % (base, group_id))
        self.groups.append(
            {
                "group_id": group_id,
                "sample_id": sample_id,
                "tier": tier,
                "variant": base_variant,
                "base": base_path,
                "files": [item["file"] for item in recorded],
                "diff_kind": diff_kind,
                "dir": rel_dir,
                "members": recorded,
            }
        )
        return sample_id

    def add_skipped(self, rel_path: str, variant: str, status: str, note: str, payload: bytes) -> None:
        if variant not in SKIP_VARIANTS:
            raise AssertionError("skipped sample must use a skip variant, got %r" % variant)
        self.add(rel_path, payload)
        self.skipped.append(
            {"file": rel_path, "variant": variant, "extract_status": status, "note": note}
        )

    def add_unsupported(self, rel_path: str, status: str, note: str, payload: bytes) -> None:
        """Files the scanner must reject without pretending they were empty.

        These are kept out of ``groups`` and out of ``by_variant``: the variant
        enum in ``plan/04`` 2.3 names extraction outcomes, not "a format we do
        not read". The scanner still has to recognise them.
        """
        self.add(rel_path, payload)
        self.unsupported.append({"file": rel_path, "extract_status": status, "note": note})

    def manifest(self) -> dict:
        by_tier: Dict[str, int] = {name: 0 for name in TIER_VALUES}
        by_variant: Dict[str, int] = {name: 0 for name in VARIANT_VALUES}
        by_expectation: Dict[str, int] = {"ok": 0}
        for group in self.groups:
            by_tier[group["tier"]] += 1
            for member in group["members"]:
                by_variant[member["variant"]] += 1
                by_expectation["ok"] += 1
        for item in self.skipped:
            by_variant[item["variant"]] += 1
            status = item["extract_status"]
            by_expectation[status] = by_expectation.get(status, 0) + 1
        for item in self.unsupported:
            status = item["extract_status"]
            by_expectation[status] = by_expectation.get(status, 0) + 1
        return {
            "generator_version": GENERATOR_VERSION,
            "seed": self.seed,
            "counts": {
                "groups": len(self.groups),
                "files": len(self.files),
                "by_tier": by_tier,
                "by_variant": by_variant,
                "by_expectation": by_expectation,
            },
            "groups": self.groups,
            "skipped": self.skipped,
            "unsupported": self.unsupported,
        }


# --- group specifications --------------------------------------------------

# Each near group is (id, directory label, topic, member specs). A member spec is
# (suffix, format, mutation) where mutation is one of:
#   original | rewrite | truncate:head | truncate:middle | truncate:tail
#   fullwidth | header_footer
NEAR_GROUPS: Tuple[Tuple[str, str, str, Tuple[Tuple[str, str, str], ...]], ...] = (
    ("G001", "项目方案", "investment", (("a", "docx", "original"), ("b", "md", "rewrite"), ("c", "txt", "rewrite"))),
    ("G002", "评审纪要", "audit", (("a", "docx", "original"), ("b", "docx", "truncate:head"), ("c", "docx", "truncate:middle"), ("d", "md", "truncate:tail"))),
    ("G003", "采购说明", "procurement", (("a", "docx", "original"), ("b", "pdf", "rewrite"), ("c", "txt", "truncate:middle"))),
    ("G004", "安全交底", "safety", (("a", "md", "original"), ("b", "docx", "rewrite"), ("c", "pdf", "truncate:tail"))),
    ("G005", "跨格式一致性", "design", (("a", "docx", "original"), ("b", "md", "reformat"), ("c", "txt", "reformat"), ("d", "html", "reformat"), ("e", "pdf", "reformat"), ("f", "txt", "reformat"))),
    ("G006", "进度说明", "schedule", (("a", "docx", "original"), ("b", "docx", "rewrite"), ("c", "md", "truncate:head"))),
    ("G007", "质量管理", "quality", (("a", "pdf", "original"), ("b", "docx", "rewrite"), ("c", "txt", "truncate:middle"))),
    ("G008", "培训方案", "training", (("a", "docx", "original"), ("b", "md", "rewrite"), ("c", "pdf", "rewrite"))),
    ("G009", "环境措施", "environment", (("a", "txt", "original"), ("b", "docx", "rewrite"), ("c", "md", "truncate:tail"))),
    ("G010", "全半角混排", "procurement", (("a", "txt", "original"), ("b", "txt", "fullwidth"), ("c", "docx", "fullwidth"))),
    ("G011", "页眉页脚与页码", "quality", (("a", "docx", "original"), ("b", "pdf", "header_footer"))),
    ("G012", "物流组织", "logistics", (("a", "docx", "original"), ("b", "pdf", "rewrite"), ("c", "md", "rewrite"))),
    ("G013", "竣工验收", "acceptance", (("a", "docx", "original"), ("b", "docx", "truncate:middle"), ("c", "txt", "rewrite"))),
    ("G014", "运维安排", "maintenance", (("a", "md", "original"), ("b", "docx", "rewrite"), ("c", "pdf", "truncate:head"))),
    ("G015", "投资复核", "investment", (("a", "docx", "original"), ("b", "docx", "rewrite"), ("c", "md", "rewrite"), ("d", "txt", "truncate:tail"))),
    ("G016", "审计整改", "audit", (("a", "pdf", "original"), ("b", "md", "rewrite"), ("c", "docx", "truncate:middle"))),
    ("G017", "设计变更", "design", (("a", "docx", "original"), ("b", "txt", "rewrite"), ("c", "pdf", "rewrite"))),
    ("G018", "安全演练", "safety", (("a", "md", "original"), ("b", "docx", "truncate:head"), ("c", "pdf", "rewrite"))),
    ("G019", "质量抽检", "quality", (("a", "docx", "original"), ("b", "html", "reformat"), ("c", "md", "rewrite"))),
    ("G020", "进度纠偏", "schedule", (("a", "docx", "original"), ("b", "docx", "rewrite"), ("c", "pdf", "truncate:tail"))),
)

# Exact-duplicate groups hold the same content under different file names. The
# formats stay identical inside a group: "byte-identical after normalization"
# is the exact tier's definition, and a markdown heading marker would otherwise
# differ from a plain-text heading.
EXACT_GROUPS: Tuple[Tuple[str, str, str, Tuple[Tuple[str, str], ...]], ...] = (
    ("G101", "完全重复-培训", "training", (("a", "md"), ("b", "md"), ("c", "md"))),
    ("G102", "完全重复-环保", "environment", (("a", "txt"), ("b", "txt"), ("c", "txt"))),
    ("G103", "完全重复-物流", "logistics", (("a", "txt"), ("b", "txt"))),
    ("G104", "完全重复-运维", "maintenance", (("a", "md"), ("b", "md"))),
)

TEMPLATE_GROUPS: Tuple[Tuple[str, str, Tuple[Tuple[str, str, str, str], ...]], ...] = (
    ("G201", "模板合同", (("a", "示例科技有限公司A", "示例项目甲", "128.60"), ("b", "示例科技有限公司B", "示例项目乙", "96.40"), ("c", "示例建设集团C", "示例标段一", "215.75"), ("d", "示例科技有限公司A", "示例标段一", "77.30"))),
    ("G202", "模板周报", (("a", "示例科技有限公司A", "示例项目甲", "3"), ("b", "示例科技有限公司B", "示例项目乙", "5"), ("c", "示例建设集团C", "示例标段一", "4"))),
    ("G203", "模板发票摘要", (("a", "示例科技有限公司A", "示例项目甲", "42.10"), ("b", "示例科技有限公司B", "示例项目乙", "18.90"), ("c", "示例建设集团C", "示例标段一", "63.25"))),
)

NEGATIVE_TOPICS = (
    ("N001", "investment", "成本测算附件"),
    ("N002", "procurement", "报价清单附表"),
    ("N003", "safety", "事故案例汇编"),
    ("N004", "environment", "排放监测台账"),
    ("N005", "schedule", "资源投入说明"),
    ("N006", "quality", "材料复检记录"),
    ("N007", "training", "课程大纲附录"),
    ("N008", "audit", "问题清单说明"),
    ("N009", "logistics", "盘点差异说明"),
    ("N010", "acceptance", "竣工资料索引"),
)

# Negatives share the domain vocabulary on purpose (that is what makes them a
# false-positive risk) but each one is written from its own angle, with its own
# sentence pool and record shape. A single shared pool would make every negative
# a near-duplicate of every other negative, which would defeat the point: the M2
# benchmark has to show that same-domain documents are *not* clustered.
NEGATIVE_SHAPES = (
    (
        "登记表",
        (
            "{term}的登记值按季度汇总，单位与正文不同，引用时需另行换算。",
            "第{n}行仅记录{term}的原始读数，未做任何口径转换。",
            "若{term}一栏为空，表示当期未采集，不能按零处理。",
        ),
    ),
    (
        "问题清单",
        (
            "问题{n}：{term}相关记录缺失，已交责任部门补录。",
            "问题{n}的整改期限未定，需在下次例会上确认。",
            "{term}的复核结论为待补充，暂不计入结论。",
        ),
    ),
    (
        "培训签到",
        (
            "第{n}次课签到 {n2} 人，课程主题与{term}无关。",
            "缺席人员另行安排补课，补课内容不涉及{term}。",
            "签到表仅作考勤凭证，不作为{term}的验收依据。",
        ),
    ),
    (
        "外部函件",
        (
            "来函第{n}号仅询问{term}的名词定义，未涉及具体数值。",
            "复函说明{term}的适用边界，并请对方另行确认。",
            "本函不构成承诺，{term}以正式合同为准。",
        ),
    ),
    (
        "图片目录",
        (
            "IMG_{n:04d}.jpg 为{term}相关现场照片，未附文字说明。",
            "目录中的文件按拍摄日期排序，与{term}章节无对应关系。",
            "照片原图另存于移动硬盘，本目录仅保留索引。",
        ),
    ),
)


def _negative_paragraphs(rng: SplitMix64, key: str, shape_index: int, count: int = 9) -> List[str]:
    terms = list(TOPICS[key]["terms"])  # type: ignore[arg-type]
    shape_name, sentences = NEGATIVE_SHAPES[shape_index % len(NEGATIVE_SHAPES)]
    paragraphs = ["%s · %s" % (rng.choice(WHITELIST_PROJECTS), shape_name)]
    for index in range(count):
        term = terms[(index * 7 + rng.randbelow(len(terms))) % len(terms)]
        paragraphs.append(
            sentences[index % len(sentences)].format(
                term=term,
                n=rng.int_in(1, 40),
                n2=rng.int_in(3, 30),
            )
        )
    return paragraphs


def _template_document(org: str, project: str, amount: str, person: str, template_kind: str) -> List[str]:
    if template_kind == "contract":
        return [
            "%s 编号 HT-%s" % (project, amount.replace(".", "")),
            "甲方：%s" % org,
            "乙方：%s" % ("示例科技有限公司B" if org != "示例科技有限公司B" else ORG_A),
            "项目名称：%s" % project,
            "合同价款：%s 万元" % amount,
            "履约期限：自签订之日起 18 个月",
            "付款方式：按进度分三期支付，每期以验收单为准。",
            "违约责任：逾期交付按日计收违约金，累计不超过合同价款的百分之五。",
            "争议解决：双方协商不成的，提交项目所在地有管辖权的人民法院。",
            "签署人：%s" % person,
            "本模板仅用于示例，条款不构成任何真实约定。",
        ]
    if template_kind == "weekly":
        return [
            "%s 第 %s 周工作周报" % (project, amount.split(".")[0]),
            "报送单位：%s" % org,
            "本周完成：示例标段一 的巡检与资料整理，共 %s 项。" % amount.split(".")[0],
            "下周计划：继续推进示例项目甲的复核工作。",
            "存在问题：部分台账更新滞后，已通知责任小组。",
            "资源需求：需增派 1 名资料员，周期约 5 个工作日。",
            "填报人：%s" % person,
            "本表为示例模板，数据不具真实含义。",
        ]
    return [
        "%s 费用摘要" % project,
        "开票单位：%s" % org,
        "项目归属：%s" % project,
        "价税合计：%s 万元" % amount,
        "费用类别：技术服务费",
        "开票日期：2026 年 3 月 15 日",
        "备注：本摘要由系统生成，仅用于示例演示。",
        "经办人：%s" % person,
    ]


def _assert_entities_whitelisted(text: str) -> None:
    """Reject any organisation or project name outside the documented whitelist.

    The check strips every whitelisted name first and then looks for the
    remainder of a naming pattern. A regex that tried to capture the name
    directly would grab the preceding characters ("本文件由示例建设集团C") and
    report a false positive, so subtraction is both simpler and correct.
    """
    remaining = unicodedata.normalize("NFKC", text)
    for name in WHITELIST_ORGS + WHITELIST_PROJECTS:
        remaining = remaining.replace(name, " ")
    for marker, label in (("有限公司", "organisation"), ("集团", "group")):
        if marker in remaining:
            index = remaining.index(marker)
            raise AssertionError(
                "non-whitelisted %s name near %r" % (label, remaining[max(0, index - 10) : index + 4])
            )
    # Only the literal "示例X" prefixes are checked; a bare "项目" is an ordinary
    # word ("项目方案") and must not trip the guard.
    for prefix, allowed, label in (
        ("示例项目", {"甲", "乙"}, "project"),
        ("示例标段", {"一"}, "lot"),
    ):
        for match in re.finditer(re.escape(prefix) + "([\u4e00-\u9fa5]?)", remaining):
            suffix = match.group(1)
            if suffix and suffix not in allowed:
                raise AssertionError("non-whitelisted %s suffix %r" % (label, suffix))


def _apply_mutation(
    paragraphs: Sequence[str],
    mutation: str,
    rng: SplitMix64,
) -> Tuple[List[str], str]:
    """Return the mutated paragraphs plus the manifest ``diff_kind``."""
    if mutation == "original":
        return list(paragraphs), "none"
    if mutation == "rewrite":
        return _rewrite(paragraphs, rng), "rewrite"
    if mutation.startswith("truncate:"):
        where = mutation.split(":", 1)[1]
        return _truncate(paragraphs, where), "truncate"
    if mutation == "fullwidth":
        return _fullwidth(paragraphs), "fullwidth"
    if mutation == "reformat":
        return list(paragraphs), "reformat"
    if mutation == "header_footer":
        return list(paragraphs), "header_footer"
    raise ValueError("unknown mutation %r" % mutation)


def _render(
    paragraphs: Sequence[str],
    fmt: str,
    *,
    mutation: str = "original",
    with_table: bool = False,
) -> bytes:
    if fmt == "md":
        return _as_markdown(paragraphs)
    if fmt == "txt":
        return _as_text(paragraphs)
    if fmt == "html":
        return _as_html(paragraphs, paragraphs[0])
    if fmt == "docx":
        return _as_docx(paragraphs, with_table=with_table)
    if fmt == "pdf":
        if mutation == "header_footer":
            header = "%s · 内部资料" % ORG_A
            return _as_pdf(paragraphs, header=header, footer_template="第 {page} 页")
        return _as_pdf(paragraphs)
    raise ValueError("unknown format %r" % fmt)


def build_corpus(seed: int = DEFAULT_SEED) -> Corpus:
    corpus = Corpus(seed)

    # -- near-duplicate groups -------------------------------------------
    for group_id, label, topic, members in NEAR_GROUPS:
        group_rng = SplitMix64(_seed_for(str(seed), group_id))
        base_paragraphs = _base_document(group_rng, topic, heading=label)
        rendered: List[Tuple[str, str, str, bytes]] = []
        base_name = ""
        for suffix, fmt, mutation in members:
            name = "%s_%s.%s" % (group_id, suffix, fmt)
            # The first member of a cross-format group is the base document in its
            # own format; the others are the same text re-encoded, so only the base
            # may be labelled "original".
            effective = "original" if (mutation == "reformat" and not base_name) else mutation
            paragraphs, diff_kind = _apply_mutation(base_paragraphs, effective, group_rng)
            # One twin in the cross-format group is written as GBK, so UTF-8
            # detection alone would fail on it; the encoding probe has to work.
            if group_id == "G005" and suffix == "f":
                payload = _as_text(paragraphs).decode("utf-8").encode("gbk")
            else:
                payload = _render(paragraphs, fmt, mutation=mutation)
            variant = "original" if effective == "original" else effective.split(":")[0]
            _assert_entities_whitelisted("\n".join(paragraphs))
            if effective == "original":
                base_name = name
            rendered.append((name, variant, diff_kind, payload))
        corpus.add_group(
            group_id,
            "%s_%s" % (group_id, label),
            "near",
            rendered,
            base=base_name,
            diff_kind=_summarise_diff([item[2] for item in rendered]),
        )

    # -- exact duplicates -------------------------------------------------
    for group_id, label, topic, members in EXACT_GROUPS:
        group_rng = SplitMix64(_seed_for(str(seed), group_id))
        paragraphs = _base_document(group_rng, topic, heading=label)
        _assert_entities_whitelisted("\n".join(paragraphs))
        rendered = []
        for suffix, fmt in members:
            name = "%s_%s.%s" % (group_id, suffix, fmt)
            rendered.append((name, "original", "none", _render(paragraphs, fmt)))
        corpus.add_group(
            group_id,
            "%s_%s" % (group_id, label),
            "exact",
            rendered,
            base=rendered[0][0],
            diff_kind="none",
        )

    # -- template families ------------------------------------------------
    for group_id, label, rows in TEMPLATE_GROUPS:
        kind = {"模板合同": "contract", "模板周报": "weekly", "模板发票摘要": "invoice"}[label]
        rendered = []
        for suffix, org, project, amount in rows:
            person = PERSONS[suffix_index(suffix) % len(PERSONS)]
            paragraphs = _template_document(org, project, amount, person, kind)
            _assert_entities_whitelisted("\n".join(paragraphs))
            name = "%s_%s.docx" % (group_id, suffix)
            rendered.append((name, "template", "template", _as_docx(paragraphs, with_table=kind == "contract")))
        corpus.add_group(
            group_id,
            "%s_%s" % (group_id, label),
            "template",
            rendered,
            base=rendered[0][0],
            diff_kind="template",
        )

    # -- negatives ---------------------------------------------------------
    for position, (group_id, topic, label) in enumerate(NEGATIVE_TOPICS):
        group_rng = SplitMix64(_seed_for(str(seed), group_id))
        paragraphs = _negative_paragraphs(group_rng, topic, position)
        _assert_entities_whitelisted("\n".join(paragraphs))
        name = "%s_%s.md" % (group_id, label)
        corpus.add_group(
            group_id,
            "negative",
            "negative",
            [(name, "noise", "noise", _as_markdown(paragraphs))],
            base=name,
            diff_kind="noise",
        )

    # -- documents the scanner must skip -----------------------------------
    corpus.add_skipped(
        "skipped/S9001_scanonly.pdf",
        "no_text_layer",
        "skipped:no_text_layer",
        "两页纯图形页面，没有任何文本操作符",
        pdfutil.build_no_text_pdf(2),
    )
    corpus.add_skipped(
        "skipped/S9002_scanonly.pdf",
        "no_text_layer",
        "skipped:no_text_layer",
        "单页空白文本层",
        pdfutil.build_no_text_pdf(1),
    )
    corpus.add_skipped(
        "skipped/S9003_encrypted.pdf",
        "encrypted",
        "skipped:encrypted",
        "需要非空用户口令才能打开",
        pdfutil.build_encrypted_pdf(
            [["加密样本 · 示例项目乙", "本文件设置了用户口令，未提供口令的抽取器必须显式跳过。"]]
        ),
    )
    corpus.add_skipped(
        "skipped/S9004_encrypted.pdf",
        "encrypted",
        "skipped:encrypted",
        "两页加密样本，正文不可读",
        pdfutil.build_encrypted_pdf(
            [
                ["加密样本 · 示例标段一", "第一页正文，仅用于验证加密路径。"],
                ["加密样本 · 示例标段一", "第二页正文，同样不可在无口令时读取。"],
            ],
            user_password=b"another-user",
        ),
    )

    # -- formats the scanner must reject -----------------------------------
    corpus.add_unsupported(
        "unsupported/S9101_legacy.doc",
        "skipped:unsupported",
        "旧版二进制 Word，DOCX 解析器不适用",
        _legacy_doc_stub(),
    )
    corpus.add_unsupported(
        "unsupported/S9102_fake.pdf",
        "skipped:unsupported",
        "扩展名是 .pdf，但魔数不是 PDF",
        "这不是一个真正的 PDF 文件，仅用于验证魔数与扩展名双识别。\n".encode("utf-8"),
    )
    corpus.add_unsupported(
        "unsupported/S9103_notes.rtf",
        "skipped:unsupported",
        "RTF 不在首期支持列表内",
        b"{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 SimSun;}}\\f0 Sample body.}\n",
    )

    return corpus


def suffix_index(suffix: str) -> int:
    """Map ``a``/``b``/``c``... to 0/1/2...; enough to vary the fake signer."""
    return sum(ord(char) for char in suffix)


def _legacy_doc_stub() -> bytes:
    """A minimal OLE2 compound-file header: enough to be recognised, not parsed."""
    header = bytes.fromhex("D0CF11E0A1B11AE1") + bytes(504)
    return header + b"Legacy Word document stub, unsupported by design.\x00" * 4


def _summarise_diff(diff_kinds: Sequence[str]) -> str:
    unique = []
    for kind in diff_kinds:
        if kind != "none" and kind not in unique:
            unique.append(kind)
    if not unique:
        return "none"
    if len(unique) == 1:
        return unique[0]
    return "mixed:" + "+".join(unique)


# ---------------------------------------------------------------------------
# Bulk tier (generated on demand, never committed)
# ---------------------------------------------------------------------------


def build_bulk_corpus(seed: int, count: int) -> Dict[str, bytes]:
    """Return ``count`` mixed-format documents for the scanner scale test.

    The committed corpus is deliberately small: it is the *truth* set and every
    byte is frozen in git. The 1000-file scale test only needs volume, so this
    tier is generated into a temporary directory on demand and stays out of the
    repository.
    """
    files: Dict[str, bytes] = {}
    topic_keys = list(TOPICS.keys())
    for index in range(count):
        rng = SplitMix64(_seed_for(str(seed), "bulk", str(index)))
        topic = topic_keys[index % len(topic_keys)]
        paragraphs = _base_document(rng, topic, heading="批量样本", paragraph_count=6 + index % 6)
        _assert_entities_whitelisted("\n".join(paragraphs))
        selector = index % 10
        if selector < 4:
            fmt = "md"
        elif selector < 7:
            fmt = "txt"
        elif selector < 9:
            fmt = "docx"
        elif selector == 9 and index % 20 == 19:
            payload = pdfutil.build_no_text_pdf(1)
            files["bulk/doc_%05d.pdf" % index] = payload
            continue
        else:
            fmt = "pdf"
        files["bulk/doc_%05d.%s" % (index, fmt)] = _render(paragraphs, fmt)
    return files


# ---------------------------------------------------------------------------
# Writing and verification
# ---------------------------------------------------------------------------


def _dump_manifest(manifest: dict) -> bytes:
    text = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2)
    return (text + "\n").encode("utf-8")


def full_corpus(seed: int = DEFAULT_SEED) -> Dict[str, bytes]:
    corpus = build_corpus(seed)
    payloads = dict(corpus.files)
    payloads[MANIFEST_REL] = _dump_manifest(corpus.manifest())
    return payloads


def write_corpus(destination: Path, payloads: Dict[str, bytes]) -> None:
    for rel_path in sorted(payloads):
        target = destination / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payloads[rel_path])


def _iter_tree(root: Path) -> Dict[str, bytes]:
    found: Dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def check(destination: Path = SAMPLES_DIR, seed: int = DEFAULT_SEED) -> int:
    """Regenerate into a temporary directory and compare byte-for-byte."""
    if not destination.is_dir():
        print("FAIL %s does not exist; run without --check first" % destination, file=sys.stderr)
        return 1

    expected = full_corpus(seed)
    actual = _iter_tree(destination)

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])

    if missing or extra or changed:
        for name in missing:
            print("MISSING %s" % name, file=sys.stderr)
        for name in extra:
            print("UNEXPECTED %s" % name, file=sys.stderr)
        for name in changed:
            print(
                "CHANGED %s (expected %d bytes, found %d bytes)"
                % (name, len(expected[name]), len(actual[name])),
                file=sys.stderr,
            )
        print(
            "samples are not frozen: %d missing, %d unexpected, %d changed"
            % (len(missing), len(extra), len(changed)),
            file=sys.stderr,
        )
        return 1

    print("SAMPLES_FROZEN_OK (%d files, seed=%d)" % (len(expected), seed))
    return 0


def generate(destination: Path = SAMPLES_DIR, seed: int = DEFAULT_SEED) -> int:
    payloads = full_corpus(seed)
    write_corpus(destination, payloads)
    print("wrote %d files to %s (seed=%d)" % (len(payloads), destination, seed))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and verify the synthetic sample corpus.")
    parser.add_argument("--out", default=str(SAMPLES_DIR), help="output directory (default: data/samples)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed (default: %d)" % DEFAULT_SEED)
    parser.add_argument("--check", action="store_true", help="verify the frozen corpus instead of writing it")
    parser.add_argument("--bulk", default=None, metavar="DIR", help="write a bulk corpus for the scale test")
    parser.add_argument("--bulk-count", type=int, default=1000, help="number of bulk documents (default: 1000)")
    args = parser.parse_args(argv)

    if args.bulk:
        payloads = build_bulk_corpus(args.seed, args.bulk_count)
        write_corpus(Path(args.bulk), payloads)
        print("wrote %d bulk documents to %s" % (len(payloads), args.bulk))
        return 0

    destination = Path(args.out)
    if args.check:
        return check(destination, args.seed)
    return generate(destination, args.seed)


if __name__ == "__main__":
    sys.exit(main())
