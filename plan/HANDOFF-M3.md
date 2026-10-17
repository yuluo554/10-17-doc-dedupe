# HANDOFF-M3 · 聚类与保留建议

- 写于：2026-10-17（M2 收尾）
- 用法：新对话读本文件即可续接。启动命令由用户侧拼接绝对路径：
  `/goal 读取 "<项目绝对路径>\plan\HANDOFF-M3.md" 继续完成任务`
- 上一棒：M2（模块② 中文友好指纹与召回），已完成并提交
- 下一棒：**M3（模块③ 聚类与保留建议）**

## 一、当前进度与证据

M2 已完成：`scan` 从"只抽正文"变成"抽正文 → 归一化 → 指纹 → 召回 → 打分 → 落库"的完整链路，
`pairs` 表可被模块③直接消费。模块③ 起尚未实现。

| 交付物 | 位置 | 证据 |
|---|---|---|
| 归一化（五步定序） | `src/docdedupe/normalize.py` | `STAGE_ORDER` 可断言；`tests/test_normalize.py` 33 项 |
| 指纹（3-gram / SimHash / MinHash / LSH） | `src/docdedupe/fingerprint.py` | `tests/test_fingerprint.py` 37 项 |
| 真值比对（B3 唯一实现） | `src/docdedupe/recall.py` | 逐组点名 + 双边门 |
| 召回基准工具 | `tools/bench_recall.py` | 逐组点名；`--inject-*` 三种探活 |
| 扫描接通 | `src/docdedupe/scanner.py` | 归一化/指纹/召回写入 + 摘要式跳过 |
| 存储扩展 | `src/docdedupe/store.py` | `fingerprints.shingle_hashes`、`pairs.jaccard_estimate` |
| 测试 | `tests/test_normalize.py`、`test_fingerprint.py`、`test_recall.py` | 3.8 与 3.12 各 **220 项**全绿 |

**诚实口径**：CI 仍未跑过（仓库无远端）。以下"绿"都是本地等价校验。

M2 实测数字（同机同进程）：

| 项 | 数字 |
|---|---|
| B3 召回（逐组点名） | **20/20 = 100%**（门槛 90%）；`negative` 10 组零误报 |
| 合成集扫描（101 份） | 首次 0.99s，二次 0.03s；94 指纹行；98 条 pairs（81 条 ≥ 0.80） |
| 1000 份混合格式扫描 | 首次 9.28s，二次 0.25s → **下降 97.3%** |
| 精确 vs 估计 | \|exact − minhash\| 均值 0.020、最大 0.107（`pairs` 全表 98 条） |
| 探活 | `drop-above` → 17/20；`drop-random` → 按组点名；`shift-groups` → 0/20 |

## 二、下一棒待办（M3）

顺序按依赖排列：

1. **`src/docdedupe/cluster.py`**
   - 输入 `pairs`（**所有已存对，不只 ≥ 0.80**）：模板族成员落在 0.55–0.80，必须看得见
   - 并查集建簇；簇 id 由规范化路径排序派生（不得依赖遍历顺序）
   - 三档判定：`exact`（`norm_sha256` 相同）/ `near`（≥ `near_duplicate_threshold`）/ `template`（同构骨架 + 字段位差异）
   - 打分与理由字符串**只在本模块一处实现**；理由必须含至少两个判定维度，禁止裸分数
   - 模板族**不得**与近重复混算，且不产生"建议保留"
2. **`src/docdedupe/report.py`**
   - md / csv / json 三轨共用同一渲染源；报告层零判定（AST 守门）
   - 无时间戳、无随机序；JSON 逐字节可复现
3. **`cli.py`：`report` 子命令**
   - 从 `STAGE_NOT_READY` 退役；退出码语义：检出结果 → 1，无结果 → 0
   - 同步改 `cli.py` 顶部退出码表、README、`tests/test_cli_contract.py` 的 staging 断言
4. **B4 基准 + 探活**
   - 三档标签逐组对真值；模板族（G201–G203）必须落 `template` 档而不是 `near`
   - 注入缺陷（如把 `near` 阈值改到 0.5、或把模板族并入 near）后必须报未达标

## 三、既定口径清单（K 编号，动了会打挂测试）

M2 新增的编号从 K22 起；K1–K21 原样保留。

| 编号 | 口径 | 位置 | 变了会怎样 |
|---|---|---|---|
| K1–K12 | 见 `plan/HANDOFF-M1.md` §三 | — | 原样有效 |
| K13–K21 | 见 `plan/HANDOFF-M2.md` §三 | — | 原样有效（K21 见本文件 §五） |
| K22 | 归一化五步顺序 = `STAGE_ORDER`，且只有 `normalize.py` 一处实现 | `normalize.STAGE_ORDER` | 重排或复制会被 `test_stage_order_matches_the_documented_sequence` / `test_normalization_has_exactly_one_implementation` 打红 |
| K23 | 归一化版本字面量只在 `config.py` | `config.NORMALIZE_VERSION` | `test_normalize_version_literal_lives_only_in_config` 会红 |
| K24 | shingle = 字符 3-gram，**禁止空格分词** | `fingerprint.shingles` | 中文会被切碎，G005/G010 跨格式一致性立刻崩 |
| K25 | 指纹哈希用 BLAKE2b，不用内建 `hash()` | `fingerprint._hash64` | 换回 `hash()` 会让签名随进程变化，`test_signature_is_stable_across_processes` 打红 |
| K26 | **MinHash/LSH 只召回，判定用精确 Jaccard**；`pairs.score_kind = exact_jaccard` | `fingerprint.build_candidates` | 改回用估计值判定，20 组里 4 组会被采样噪声拆散（实测） |
| K27 | `pairs` 整表替换，不做增量合并 | `store.replace_pairs` | 合并会留下陈旧边，`test_pair_rows_are_replaced_not_merged` 打红 |
| K28 | `pairs` 保留下限 = `pair_store_threshold`（0.55），低于近重复阈值 | `config.Settings` | 抬到 0.80 会让模板族对模块③ 不可见 |
| K29 | 召回跳过需"输入摘要 + 结果摘要"同时成立 | `scanner._run_recall` | 任一摘要不校验，被手改的 `pairs` 会被永久信任 |
| K30 | SimHash 落 SQLite 需转有符号 64 位 | `store.simhash_to_signed` | 直接存无符号会 `OverflowError` |
| K31 | 模板族不参与 B3 召回门（`INFO` 行呈现） | `tools/bench_recall.py` | 把模板族算进 near 门会让基准永远红，且掩盖真正的召回问题 |
| K32 | Markdown 语法标记由抽取层剥离，不留给归一化 | `extract._extract_markdown` | 留给归一化会让"正文归一"混入容器语法，且 `EXTRACTOR_VERSION` 语义失真 |

## 四、本机环境坑（M2 实测新增）

M0/M1 的坑仍然有效，新增：

1. **Git Bash 的 `/tmp` 与 Python 的 `/tmp` 不是同一个目录**（前者映射到 `C:/Users/<user>/AppData\Local\Temp`，
   后者在 `D:\tmp`）。传 `--db /tmp/x.db` 给 Python 时，SQLite 会另建一个库，读到的计数全是 0，
   看起来像"扫描没写入"。**用 `$LOCALAPPDATA/Temp/...` 或显式绝对路径。**
2. **`path_norm` 在 Windows 上按文件系统语义折叠大小写**（`G001_a.docx` → `g001_a.docx`）。
   拿真值相对路径与索引绝对路径比对时必须过同一套 `store.normalize_path`，
   否则每个组都显示"拆成 N 个组件"（看着像召回全崩，其实是键对不上）。
3. **SQLite INTEGER 是有符号的**：64-bit SimHash 高位为 1 时直接插入会 `OverflowError`。
   读写都要过 `simhash_to_signed` / `simhash_from_signed`。
4. **`pypdf`/`python-docx` 的抽取耗时只占小头**：1000 份样本里抽取约 2.9s，指纹约 3.4s，
   候选对打分约 2.2s。优化指纹前先量，别猜。
5. **`min(map(salt.__xor__, hashes))` 比生成器表达式快约 40%**；`frozenset` 求交比 Python 归并快约 3 倍。
   但**不要**为了快改用近似算法——判定必须可复算（K26）。
6. **`tools/` 已在 `tests/conftest.py` 的 `sys.path` 里**，测试可以直接 `import bench_recall`。
   基准工具与测试必须共用 `recall.py`，别各写一套真值比对。

## 五、DoD checklist（M3 验收，逐项打勾）

- [ ] 三档标签与真值逐组一致；`template` 不与 `near` 混同（G201–G203 必须落 `template`）
- [ ] 每簇 `reason` 非空且含至少两个判定维度（禁止裸分数）
- [ ] 打分公式只在 `cluster.py` 一处；报告层零判定（AST 守门）
- [ ] md / csv / json 三轨内容一致（同一渲染源）+ JSON 逐字节可复现
- [ ] 报告无时间戳、无随机序
- [ ] `report` 从 `STAGE_NOT_READY` 退役，退出码契约同步更新
- [ ] B4 基准 + 探活（注入缺陷后必须报未达标）
- [ ] 双解释器（3.8 / 3.12）全量 pytest 绿
- [ ] `plan/00` 台账对应行翻 ✅、`plan/05` M3 DoD 勾选、`plan/HANDOFF-M4.md` 落盘

## 六、关键命令速查

```bash
# 测试（双解释器）
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv312/Scripts/python.exe -m pytest -p no:cacheprovider
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv38/Scripts/python.exe  -m pytest -p no:cacheprovider

# 样本冻结门禁
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/gen_samples.py --check

# 扫描 + B3 基准（注意用 Windows 临时目录，见 §四.1）
D="$LOCALAPPDATA/Temp/m3run"; rm -rf "$D"; mkdir -p "$D"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe scan data/samples --db "$D/i.db"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_recall.py --db "$D/i.db"

# 探活（三条都必须 FAIL）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_recall.py --db "$D/i.db" --inject-edges drop-above
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_recall.py --db "$D/i.db" --inject-edges drop-random
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_recall.py --db "$D/i.db" --inject-groups shift-groups

# 大样本量（不入仓，生成到仓库外的临时目录）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/gen_samples.py --bulk "$D/bulk" --bulk-count 1000

# 看库里的 pairs
PYTHONUTF8=1 ./.venv312/Scripts/python.exe - <<PY
import sqlite3
conn = sqlite3.connect(r"$D/i.db")
for row in conn.execute("SELECT COUNT(*), SUM(jaccard>=0.80) FROM pairs"):
    print("pairs, above threshold:", row)
PY
```

**不要在仓库内建临时目录跑 bulk**：`--bulk` 的产物不受 `.gitignore` 白名单保护。

**待验证（别在 M3 里假装绿）**：CI 四矩阵、wheel 安装面。仓库仍无远端。

## 七、给 M3 的三条硬提醒

1. **`pairs` 里不只有 ≥ 0.80 的对。** 保留下限是 0.55（`pair_store_threshold`），
   模板族（G201–G203）落在 0.575–0.801。若在 `cluster.py` 里先按 0.80 过滤，
   模板档将永远为空，而 B4 会红。**先看数据再定过滤。**
2. **阈值只在 `data/README.md` §四 定义，代码从 `config.DEFAULT_SETTINGS` 读。**
   M1/M2 的测试已用 `DEFAULT_SETTINGS.near_duplicate_threshold` 而非字面量，照这个写法继续。
3. **簇 id 与成员顺序由规范化路径排序派生**，不得依赖并查集遍历顺序或 SQLite 行序；
   `test_two_scans_produce_identical_fingerprints_and_pairs` 是这一条的现有样板。
