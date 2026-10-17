# HANDOFF-M4 · 差异复核界面

- 写于：2026-10-17（M3 收尾）
- 用法：新对话读本文件即可续接。启动命令由用户侧拼接绝对路径：
  `/goal 读取 "<项目绝对路径>\plan\HANDOFF-M4.md" 继续完成任务`
- 上一棒：M3（模块③ 聚类与保留建议），已完成并提交
- 下一棒：**M4（模块④ 差异复核界面）**

## 一、当前进度与证据

M3 已完成：`report` 从 `pairs` 整表聚簇、判三档、给保留建议、导出 md/csv/json，
并把结果落进 `clusters` / `cluster_members` 两张表（模块④ 直接读这两张表即可）。
模块④ 起尚未实现。

| 交付物 | 位置 | 证据 |
|---|---|---|
| 聚类与三档判定 | `src/docdedupe/cluster.py` | B4 逐组点名 27/27；`tests/test_cluster.py` 32 项 |
| 报告三轨渲染 | `src/docdedupe/report.py` | `tests/test_report.py` 16 项（含 AST 零判定门） |
| B4 基准 + 探活 | `tools/bench_tiers.py` | 逐组点名；三种注入 |
| 存储扩展 | `src/docdedupe/store.py` | `cluster_document_rows` / `document_texts` / `replace_clusters` / `cluster_rows` |
| CLI | `src/docdedupe/cli.py` | `report` 已从 `STAGE_NOT_READY` 退役 |
| 测试 | `tests/test_cluster.py`、`test_report.py`、`test_bench_tiers.py`、`test_cli_contract.py` | 3.8 与 3.12 各 **298 项**全绿 |

**诚实口径**：CI 仍未跑过（仓库无远端）。以下"绿"都是本地等价校验。

M3 实测数字（同机）：

| 项 | 数字 |
|---|---|
| B4 分档（逐组点名） | **27/27 = 100%**（near 20 / exact 4 / template 3）；跨真值组污染 0 |
| 簇规模 | 27 簇 / 84 份文件；`exact 7 / near 17 / template 3` |
| 模板族 | G201/G202/G203 全落 `template`，`keep=None`；G202 首对 0.801 仍判模板 |
| 三轨一致 | md/csv/json 的簇 id、成员、保留建议逐项一致 |
| JSON 可复现 | 两次运行 sha256 一致 → `7f0026ae4448fa0e0af4d2ba20f0bd2b…`；跨解释器同字节 |
| 规模 | 1000 份：扫描 9.34s、聚类 0.42s、1000 份 6732 对 → 0 簇（无模板假阳） |
| 探活 | `template-as-near` → 24/27 FAIL；`near-as-template` → 10/27 FAIL；`--near-threshold 0.5` → 污染 1 簇 FAIL |
| 回归 | B3 仍 `B3 OK`（20/20）；`gen_samples.py --check` → `SAMPLES_FROZEN_OK` |

## 二、下一棒待办（M4）

顺序按依赖排列：

1. **`src/docdedupe/diffing.py`**：段落级 diff + 摘要统计（`data/README.md` §四.10）
   - 三句文案（"仅差 X 段"/"差一个署名"/"差一页"）由**同一个统计函数**产出，不得各写一套
   - 输入是两篇文档的**抽取文本**（段落级），不是归一化文本——归一化抹掉了行结构
   - 摘要阈值（页码阈值、署名长度上限、页级相似度阈值）按 `plan/03` §六 的扩展项处理：
     首期只落"仅差 X 段"与"差一个署名"，"差一页"缺页级切分依据，**要么补口径要么降级**，
     别在没定义的情况下硬写一个阈值
2. **`src/docdedupe/web/server.py`** + `web/static/index.html`（FastAPI + 原生 JS 单文件）
   - 只读索引；`GET /api/clusters` 直接读 `clusters` + `cluster_members`（M3 已写入）
   - `reason` / `tier` / `keep` 从库里读，**不要在 Web 层重算**（`plan/03` 模块④ 不变量）
   - `k nearest` 的单页零外链、断网可用；`serve` 的 host 校验已在 M0 落在 CLI 层
3. **`cli.py`：`serve` 子命令**从 `STAGE_NOT_READY` 退役
   - 同步改 `cli.py` 顶部退出码表、README、`tests/test_cli_contract.py` 的 staging 断言
4. **B5 基准 + 探活**：差异段号与摘要文案对真值；注入后必须报未达标
5. **隐私动态审计**：socket 拦截测试（`plan/06` §四 的"socket 拦截动态审计待 M4"）

## 三、既定口径清单（K 编号，动了会打挂测试）

M3 新增的编号从 K33 起；K1–K32 原样保留。

| 编号 | 口径 | 位置 | 变了会怎样 |
|---|---|---|---|
| K1–K12 | 见 `plan/HANDOFF-M1.md` §三 | — | 原样有效 |
| K13–K21 | 见 `plan/HANDOFF-M2.md` §三 | — | 原样有效 |
| K22–K32 | 见 `plan/HANDOFF-M3.md` §三 | — | 原样有效 |
| K33 | 聚类输入是 `pairs` **整表**（≥ `pair_store_threshold`），不是只取 ≥ 近重复阈值 | `cluster.build_clusters` / `cluster.load_inputs` | 先按 0.80 过滤会让 `template` 档永远为空；把 0.55 当判定阈值连边会把 G001+G015 并成一簇 |
| K34 | 三档由**边分类**决定：exact（`norm_sha256` 同）→ template（结构判据）→ near（≥ 阈值）；`near` 优先级最高 | `cluster._edge_kind` / `cluster._build_one` | 顺序颠倒会让 G202 的 0.801 对把模板族判成近重复 |
| K35 | 模板判据 = 行数相同 + 差异行 ≤ `template_field_diff_max` + 差异只在同标签字段位（标题按字段位）+ 掩码骨架 ≥ `template_threshold` | `cluster.template_evidence` | 改成纯分数阈值必然打挂 G201–G203（实测 0.575–0.801 与近重复区间重叠） |
| K36 | 模板族不产出保留建议（`keep=None`） | `cluster._build_one` | 会让"永不自动处置模板族"的承诺失效 |
| K37 | 打分公式与理由字符串只在 `cluster.py`；`report.py` 零判定 | `cluster.SCORE_WEIGHTS` / `report.py` | `test_report.py` 的 AST 门会红（阈值常量、浮点比较、导入 scorer 三种都查） |
| K38 | 完整度与文件名副本标记是**门槛**不是权重 | `cluster._build_one` | 退回纯加权和会推荐截断副本（实测 7 簇命中） |
| K39 | 簇 id = 成员 `path_norm` 排序后 sha256 前 12 位（前缀 `c`）；成员按 `path_norm` 升序 | `cluster._cluster_id` | 依赖并查集遍历顺序会让两次运行结果漂移 |
| K40 | `clusters` / `cluster_members` 整表替换，不做增量合并 | `store.replace_clusters` | 陈旧簇会留在库里，`serve` 会继续给已修好的重复推荐隔离 |
| K41 | 报告三轨（md/csv/json）来自同一个 `Report` 值；JSON 键排序 + 无时间戳 + 末尾换行 | `report.build_report` / `render_json` | 各写一套渲染会漂移；加时间戳会让逐字节比较永远红 |
| K42 | CLI 输出报告走二进制缓冲，不经过平台换行转换 | `cli._write_payload` | Windows 管道下 `\n`→`\r\n`，JSON 逐字节可复现不成立 |
| K43 | `report` 的 `--tier` 只改**显示**，写入索引的是未过滤的全量聚类 | `cli.cmd_report` | 写过滤后的集合会让 `serve` 与用户刚读的报告不一致 |
| K44 | 真值 `tier` 是生成类别；期望档位由 manifest 的 `variant` 标签按 §四.5 推导 | `tools/bench_tiers.expected_tier` | 直接比对 truth tier 会把 G005/G010/G011 判成 FAIL（它们的差异是容器级的，归一化后应判 `exact`） |
| K45 | B4 必须检查**跨真值组污染**，不能只看每组档位标签 | `tools/bench_tiers.find_contamination` | 阈值降到 0.5 时每组档位仍"正确"，只有污染门能抓到（M3 实现期实测踩过） |

## 四、本机环境坑（M3 实测新增）

M0–M2 的坑仍然有效，新增：

1. **`report` 有簇时退出码是 1**，不是 0。在 shell 里 `report ... && next` 会在有重复时中断——
   这是既定语义（检出结果 = 需要人看一眼），不是故障。写脚本要按码分支。
2. **Python 文本模式 stdout 会把 `\n` 换成 `\r\n`**（Windows）。`compare` 字节时必须走
   `sys.stdout.buffer` 或 `--out`（内部用 `newline=""`）；`print()` 出来的字节不可比。
   CLI 已经用 `_write_payload` 收口，新增输出路径请复用它。
3. **`posixpath.commonpath` 在不同盘符上抛 `ValueError`**（`C:\` vs `D:\`）。
   `cluster._common_parent` 已兜底为退回 basename；给报告加新路径字段时注意。
4. **模板判据依赖提取文本的行结构**，不能用归一化文本（归一化把行都接成一串）。
   所以 `store.document_texts` 只为"出现在 pairs 里的文档"读原文——全量读会让内存与语料成正比。
5. **`min([...] + [0])` 恒为 0**。M3 实现期真的写错过一次（基准的维度计数），
   表现是"所有组都报 0 个维度"而不是报错。取最小值时用 `min(gen, default=0)`。
6. **`template_field_diff_max` 是紧的**：G201 有一对正好 6 个字段位差异（等于上限）。
   把它调小到 5 会让 G201 掉出模板档。改这个值必须跑 B4。
7. **`tools/` 已在 `tests/conftest.py` 的 `sys.path` 里**，测试可直接 `import bench_tiers`；
   基准与测试必须共用 `cluster.REASON_DIMENSIONS`，别各写一套维度词（会立刻漂移）。

## 五、DoD checklist（M4 验收，逐项打勾）

- [ ] `serve` 起本地服务，断网可用、零外链、无构建步骤
- [ ] 簇列表三档标签与保留建议直接读 `clusters` 表，Web 层不重算分数或档位
- [ ] 双栏差异高亮 + 摘要文案由 `diffing.py` 单一实现产出
- [ ] 摘要文案对真值（B5），且注入缺陷后必须报未达标
- [ ] `serve` 从 `STAGE_NOT_READY` 退役，退出码契约同步更新
- [ ] socket 拦截动态审计（零网络）落成测试
- [ ] 双解释器（3.8 / 3.12）全量 pytest 绿
- [ ] `plan/00` 台账对应行翻 ✅、`plan/05` M4 DoD 勾选、`plan/HANDOFF-M5.md` 落盘

## 六、关键命令速查

```bash
# 测试（双解释器）
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv312/Scripts/python.exe -m pytest -p no:cacheprovider
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv38/Scripts/python.exe  -m pytest -p no:cacheprovider

# 样本冻结门禁
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/gen_samples.py --check

# 扫描 + B3 + B4（注意用 Windows 临时目录，见 HANDOFF-M3 §四.1）
D="$LOCALAPPDATA/Temp/m4run"; rm -rf "$D"; mkdir -p "$D"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe scan data/samples --db "$D/i.db"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_recall.py --db "$D/i.db"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_tiers.py  --db "$D/i.db"

# 报告三轨（有簇时退出码 1）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe report --db "$D/i.db" --format md   --out "$D/r.md"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe report --db "$D/i.db" --format json --out "$D/r.json"

# 看库里的 clusters（模块④ 的输入）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe - <<PY
import sqlite3
conn = sqlite3.connect(r"$D/i.db")
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT cluster_id, tier, keep_id, reason FROM clusters ORDER BY cluster_id LIMIT 3"):
    print(dict(row))
print("clusters:", conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0])
PY
```

**不要在仓库内建临时目录跑 bulk**：`--bulk` 的产物不受 `.gitignore` 白名单保护。

**待验证（别在 M4 里假装绿）**：CI 四矩阵、wheel 安装面。仓库仍无远端。

## 七、给 M4 的三条硬提醒

1. **模块④ 只读 `clusters` / `cluster_members`，不得重算档位与分数。** 表由 `report` 写好；
   若 `serve` 自己聚类，用户可能看到"报告说留 A、页面说留 B"。表的字段见
   `store._SCHEMA`，读接口见 `store.cluster_rows`。
2. **`serve` 必须能跑在"刚 scan 完、还没 report 过"的索引上**——那种库里 `clusters` 是空的。
   要么显式提示"先跑 report"，要么在只读前提下调用一次 `cluster.clusters_from_index`
   （**但那样就必须同时落库**，否则又回到上面第 1 条的不一致）。选一个并写进 README。
3. **差异摘要需要段落级结构，只有原文有。** `documents.text` 是抽取文本、`norm_text` 是
   归一化后的一整串；`diffing.py` 要的是前者。`store.document_texts` 已在 M3 加好。
