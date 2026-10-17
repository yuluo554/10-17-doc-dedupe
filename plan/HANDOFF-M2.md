# HANDOFF-M2 · 指纹与召回

- 写于：2026-10-17（M1 收尾）
- 用法：新对话读本文件即可续接。启动命令由用户侧拼接绝对路径：
  `/goal 读取 "<项目绝对路径>\plan\HANDOFF-M2.md" 继续完成任务`
- 上一棒：M1（数据先行 + 模块① 扫描与格式适配层），已完成并提交
- 下一棒：**M2（模块② 中文友好指纹与召回）**

## 一、当前进度与证据

M1 已完成：仓库可安装、可扫描、可测试，**模块① 全链路可用**，模块② 起尚未实现。

| 交付物 | 位置 | 证据 |
|---|---|---|
| 合成样本生成器 | `tools/gen_samples.py` | `--check` → `SAMPLES_FROZEN_OK (102 files, seed=20261017)` |
| 确定性 PDF 写手 | `tools/pdfutil.py` | 文本/无文本层/加密三种产物；pypdf 实测可读、加密的不可读 |
| DOCX 时间戳收口 | `tools/docxutil.py` | 跨秒两次生成字节一致 |
| 样本集 + 真值 | `data/samples/`（101 文档 / 37 组）+ `truth/manifest.json` | 无 `observed_*`、无 float、variant/tier 全在枚举内 |
| 抽取层 | `src/docdedupe/extract.py` | 五格式抽取 + 三态 skipped；逐样本对上 manifest |
| 扫描编排 | `src/docdedupe/scanner.py` | 排序遍历、缓存命中不读文件、skipped 逐条点名 |
| 索引写入 | `src/docdedupe/store.py` | `upsert_document` / `status_counts` / `skipped_documents` |
| CLI | `src/docdedupe/cli.py` | `scan` 已从 `STAGE_NOT_READY` 退役 |
| 测试 | `tests/test_extract.py`、`test_scanner.py`、`test_samples.py`、`test_repo_invariants.py` | 3.8 与 3.12 全绿 |

**诚实口径**：CI 仍未跑过（仓库无远端）。以下"绿"都是本地等价校验。

M1 实测数字（同机同进程）：

| 项 | 数字 |
|---|---|
| 样本集扫描（101 份） | 首次 0.72s，二次 0.03s |
| 1000 份混合格式扫描 | 首次 3.59s，二次 0.28s → **下降 92.2%** |
| 改单个 mtime 后重扫 | `999 from cache, 1 extracted` |
| 三态 skipped | `ok 94 / encrypted 2 / no_text_layer 2 / unsupported 3` |

## 二、下一棒待办（M2）

顺序按依赖排列：

1. **`src/docdedupe/normalize.py`**
   - 顺序**固定**：NFKC → 去空白与不可见字符 → 去页码行 → 去页眉页脚 → 拉丁小写（`data/README.md` §四.3）
   - 只有一处实现；`NORMALIZE_VERSION` 变更即同步 `data/README.md`
   - 页眉页脚判据：**同文档内跨页高频短行**——注意 PDF 抽取出的页眉页脚是混在正文流里的
     普通行，没有版面信息可用，只能用"跨页重复 + 短"这一条线索
2. **`src/docdedupe/fingerprint.py`**
   - 字符 3-gram（中英同路径，**禁止空格分词**）
   - 64-bit SimHash（只作分桶）
   - MinHash（128 perm）+ LSH banding（32 band × 4 row）
   - 主分数 = MinHash 估计的 Jaccard；`score_kind` 必须落库
3. **接通 `scan` 的归一化与指纹写入**
   - `documents.norm_text` / `norm_sha256` 现在由 `scanner.py` 写成空串，M2 要把它们真正填上
   - `fingerprints` 表按 `fingerprint.py` 写入
   - **注意**：`cache_key` 里已含 `NORMALIZE_VERSION` 与 n-gram 参数，改了自动失效
4. **`pairs` 表写入 + 召回基准 B3**
   - 20 组 near 真值逐组点名（不是只报汇总率）
   - `negative` 组零误报
   - 探活：注入缺陷后基准必须报未达标

## 三、既定口径清单（K 编号，动了会打挂测试）

M1 新增的编号从 K13 起；K1–K12 原样保留。

| 编号 | 口径 | 位置 | 变了会怎样 |
|---|---|---|---|
| K1–K12 | 见 `plan/HANDOFF-M1.md` §三 | — | 原样有效 |
| K13 | 遍历排序键 = `path_norm`（大小写按文件系统语义、正斜杠） | `scanner.iter_files` | 改则两跑行序可能不同，报告 diff 出现噪声 |
| K14 | 缓存命中 = `cache_key` 相同，**且命中时不打开文件** | `scanner.scan` | 命中仍解析文件会让性能门槛失效；`test_a_cache_hit_does_not_open_the_file` 会红 |
| K15 | 格式识别 = 魔数优先、扩展名兜底；两者不一致时以魔数为准 | `extract.detect_format` | 改成只信扩展名，伪装文件会硬解析失败或被误读 |
| K16 | skipped 五态：`no_text_layer` / `encrypted` / `unsupported` / `too_large` / `read_error` | `store.SKIP_STATUSES` | 新增值必须同步 `SKIP_STATUSES` 与汇总渲染 |
| K17 | 扫描只打开文档类后缀（`indexed_suffixes` + `legacy_suffixes`） | `config.scan_candidate_suffixes` | 放开会让 `.json`/`.zip` 混进 skipped 计数，把真正的跳过项埋掉 |
| K18 | 真值 manifest 的 `variant` 枚举只描述抽取结果；不支持的格式单列 `unsupported` 段 | `tools/gen_samples.py` | 把 unsupported 塞进 variant 枚举会破坏 K11 的语义边界 |
| K19 | 加密样本必须用**非空用户口令** | `pdfutil.build_encrypted_pdf` | 空口令会被透明解密，样本走不到 `skipped:encrypted` |
| K20 | 生成器不许用 stdlib `random`、不读时钟；PDF/DOCX 时间戳一律固定 | `tools/gen_samples.py`、`pdfutil`、`docxutil` | 任一引入即 `--check` 跨秒必红 |
| K21 | `scan` 退出码：有 skipped 仍 0；无可用正文或无候选文件 → 2 | `cli.cmd_scan` | 改成语义会让脚本把"扫了个空"当成成功 |

## 四、本机环境坑（M1 实测新增）

M0 的八条仍然有效，新增：

1. **`pypdf` 写出的 PDF 不可用于冻结样本**：`/ID` 取时钟、加密用 `os.urandom`。
   已用 `tools/pdfutil.py` 自写解决；**不要**为了省事改回 `PdfWriter`。
2. **`pypdf` 读取自带 RC4 加密的 PDF 时**，对象密钥必须是
   `md5(key + objnum(3B LE) + gen(2B LE))` 前 `min(len(key)+5, 16)` 字节；
   少了 generation 两字节会报 `Invalid Elementary Object`。
3. **`python-docx` 把当前时间写进 zip 条目**（且 `docProps/core.xml` 里的时间来自打包模板）。
   两者都要收口：条目用 `docxutil.normalise_zip`，属性用 `pin_core_properties`。
4. **`os.walk` 的 `dirnames` 必须原地排序过滤**，`filenames` 也要按后缀过滤，
   否则 `.json`/`.zip` 会进 skipped 计数。
5. **`tests/conftest.py` 把 `tools/` 加进 `sys.path`**：测试与生成器必须用同一份
   PDF/DOCX 写手，否则测试绿而真实样本红。
6. **`argparse` 子命令的 `--db` 要用 `default=argparse.SUPPRESS`**，
   否则会盖掉顶层 `--db` 的值。
7. **`cli.py` 里有一段被注释行"吞掉"的缩进块**（M1 已修）：注释行不会结束 if 块，
   多缩进的语句会被解析进上一分支却看似在顶层。改 `main()` 时留意。

## 五、DoD checklist（M2 验收，逐项打勾）

- [ ] 归一化顺序与 `data/README.md` §四.3 逐条对应，且只有一处实现
- [ ] 归一化用例集：全半角、跨页页眉、页码噪声、中英混排各至少一例
- [ ] 同文不同格式（G005 组六份）归一后一致
- [ ] 20 组 near 真值召回率 ≥ 90%，**逐组点名**
- [ ] `negative` 组零误报（不与其他组同簇）
- [ ] `pairs` 的 `score_kind` 落库，报告不把辅助信号当主分数
- [ ] 同输入两次运行，`fingerprints` / `pairs` 逐字段一致
- [ ] 基准探活：注入缺陷后 B3 必须报未达标
- [ ] 双解释器（3.8 / 3.12）全量 pytest 绿
- [ ] `plan/00` 台账对应行翻 ✅、`plan/05` M2 DoD 勾选、`plan/HANDOFF-M3.md` 落盘

## 六、关键命令速查

```bash
# 环境（Windows + Git Bash；前缀 NO_PROXY 是必须的，见 M0 环境坑）
NO_PROXY="*" no_proxy="*" ./.venv312/Scripts/python.exe -m pip install -e ".[dev]" \
  -i https://pypi.tuna.tsinghua.edu.cn/simple

# 测试（双解释器）
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv312/Scripts/python.exe -m pytest -q
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv38/Scripts/python.exe  -m pytest -q

# 样本冻结门禁
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/gen_samples.py --check

# 大样本量（不入仓，生成到仓库外的临时目录）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/gen_samples.py --bulk <临时目录> --bulk-count 1000

# 扫描冒烟（双入口）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe scan data/samples --db <临时目录>/i.db
PYTHONUTF8=1 ./.venv312/Scripts/doc-dedupe.exe scan data/samples --db <临时目录>/i.db

# 只看 skipped 与状态计数
PYTHONUTF8=1 ./.venv312/Scripts/python.exe - <<'PY'
import sqlite3
conn = sqlite3.connect("<临时目录>/i.db")
for row in conn.execute("SELECT extract_status, COUNT(*) FROM documents GROUP BY 1 ORDER BY 1"):
    print(row)
PY
```

**不要在仓库内建临时目录跑 bulk**：`--bulk` 的产物不受 `.gitignore` 白名单保护
（只有 `data/samples/` 被放行），会把仓库弄脏。

**待验证（别在 M2 里假装绿）**：CI 四矩阵、wheel 安装面。仓库仍无远端。

## 七、给 M2 的两条硬提醒

1. **不要把阈值搬进代码。** 近重复阈值、召回门槛、缓存门槛只在 `data/README.md` §四 定义，
   代码从 `config.DEFAULT_SETTINGS` 读。M1 的测试已经用
   `DEFAULT_SETTINGS.near_duplicate_threshold` 而不是字面量，照这个写法继续。
2. **样本集的相似度地板是 0.54**（不同真值组之间的最高 3-gram Jaccard），
   阈值默认 0.80。改样本或改归一化后，跑
   `test_no_two_truth_groups_sit_near_the_similarity_threshold`；
   如果这行红了，说明数据背景抬升到会压迫判定边界，要先修数据而不是调阈值。
