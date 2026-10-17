# HANDOFF-M1 · 数据先行与扫描层

- 写于：2026-10-17（M0 收尾）
- 用法：新对话读本文件即可续接。启动命令由用户侧拼接绝对路径：
  `/goal 读取 "<项目绝对路径>\plan\HANDOFF-M1.md" 继续完成任务`
- 上一棒：M0（计划先行 + 骨架），已完成并提交
- 下一棒：**M1（数据先行 + 模块① 扫描与格式适配层）**

## 一、当前进度与证据

M0 已完成，仓库处于"可安装、可运行、可测试"的状态，**不含任何业务实现**。

| 交付物 | 位置 | 证据 |
|---|---|---|
| 计划文档 00–06 | `plan/00-README总览.md` … `plan/06-交付对标.md` | `check_topic.py` 无 FAIL（见 §五） |
| 题面（已定稿） | `plan/01-题目文档.md` | 十段式齐全，含招牌动词表 |
| 数据台账 | `data/README.md` | 依据三态 + 数据源可获取性 + 数值口径唯一出处 |
| 可运行骨架 | `src/docdedupe/{__init__,__main__,cli,config,store}.py` | `doc-dedupe --version` → `doc-dedupe 0.1.0`；`selftest` → exit 0 |
| SQLite schema | `src/docdedupe/store.py` | 表：`meta/documents/fingerprints/pairs/clusters/cluster_members/quarantine_batches` |
| 测试 | `tests/test_cli_contract.py`、`tests/test_settings_and_store.py`、`tests/test_repo_invariants.py` | 3.8 与 3.12 双解释器全绿 |
| CI 配置 | `.github/workflows/ci.yml` | 4 矩阵（ubuntu/windows × 3.8/3.12）；**未接远端，从未实跑** |
| 许可 | `LICENSE`（MIT） | 与 `pyproject.toml` 的 license 字段一致 |

**诚实口径**：CI 从未运行过。所有"绿"都是本地等价校验，CI 待首推验证。

本地等价校验清单（M0 实际跑过的）：
- `py -3.12 -m venv .venv312` + `pip install -e ".[dev]"` → `pytest -q` 全绿
- `py -3.8 -m venv .venv38` + 升级 pip 到 24.3.1 + `pip install -e ".[dev]"` → `pytest -q` 全绿
- `doc-dedupe.exe selftest` → `selftest OK (doc-dedupe 0.1.0)`，退出码 0
- `python -m docdedupe --version` → `doc-dedupe 0.1.0`

不可本机实跑的通路：CI 四矩阵、`pip install .` 的 wheel 安装面（本机用 editable）。
→ 这两项标注"待首推验证"。

## 二、下一棒待办（M1）

顺序按依赖排列，不要跳步：

1. **`tools/gen_samples.py`**：合成样本生成器
   - 自实现 splitmix64 确定性 RNG（不用 stdlib `random`）
   - 固定 seed，产物无当前时间戳
   - 覆盖 `plan/04-数据计划.md` §2.2 全部形态
   - 输出 `data/samples/truth/manifest.json`（只存输入与期望，**禁 `observed_*` 字段**）
   - `--check`：重新生成到临时目录 → 与 `data/samples/` 逐字节比对 → `SAMPLES_FROZEN_OK`
   - 变异注入器自带 `_require_change`（`str.replace` 未命中即抛错）
2. **`tools/docxutil.py`**：DOCX 落盘后重写 zip（固定 `(1980,1,1,0,0,0)` + `create_system=0`），
   否则 `python-docx` 会把当前时间写进条目，`--check` 跨秒必红
3. **`src/docdedupe/extract.py`**：各格式抽取
   - `pypdf` 文本层（加密 → `skipped:encrypted`；无文本层 → `skipped:no_text_layer`）
   - `python-docx` 段落 + 表格
   - 标准库 MD/HTML/TXT（HTML 用 `html.parser`；TXT 编码探测：utf-8-sig → utf-8 → gbk → latin-1）
   - 扩展名 + 魔数双识别；未知格式 → `skipped:unsupported`
4. **`src/docdedupe/store.py` 扩展**：扫描写入路径
   - `upsert_document()`：按 `path_norm` 唯一键 + `cache_key` 判断是否需要重新抽取
   - 遍历排序键 = `path_norm`（不依赖文件系统枚举顺序）
5. **`src/docdedupe/cli.py`**：接通 `scan`
   - 从 `STAGE_NOT_READY` 移除 `"scan"`
   - 汇总输出：总文件数 / 各状态计数 / 缓存命中数 / 耗时
   - 退出码：全部文件不可用 → exit 2；有 skipped → 仍 exit 0 但在汇总里显式计数
6. **测试**：抽取单测（含 skipped 三态）、缓存失效（改 mtime）、1000 份扫描冒烟、
   二次扫描耗时下降（这是**性能门槛**，见 K 清单 K6）

## 三、既定口径清单（K 编号，动了会打挂测试）

| 编号 | 口径 | 位置 | 变了会怎样 |
|---|---|---|---|
| K1 | 数值口径唯一出处 = `data/README.md` §四 | 文档 | 代码里出现第二份阈值即违反纪律 |
| K2 | 缓存键六字段：路径 / size / mtime_ns / extractor_version / normalize_version / n-gram 参数 | `store.cache_key()` | 改字段即需递增对应版本常量，否则旧缓存被误用 |
| K3 | 归一化顺序：NFKC → 去空白与不可见字符 → 去页码行 → 去页眉页脚 → 拉丁小写 | `data/README.md` §四.3 | 顺序重排会改变指纹，缓存与基准同时失效 |
| K4 | shingle 一律**字符** 3-gram，中英同路径，**禁止空格分词** | `config.py` 校验器 | 改即违反技术要求 |
| K5 | skipped 三态字面量：`skipped:no_text_layer` / `skipped:encrypted` / `skipped:unsupported` | `store.py` 常量 | 改字面量会打挂 `test_status_vocabulary_*` |
| K6 | 二次扫描门槛 ≥ 80% 下降；召回率门槛 ≥ 90%；两者只在 `data/README.md` §四.7/4.9 定义 | 文档 + 基准 | 代码里写死门槛 = 第二份真相 |
| K7 | 退出码 0/1/2/3 语义 | `cli.py` 表 + 测试 | 改语义须四处同步（表 / 分派 / 文档 / 测试） |
| K8 | `STAGE_NOT_READY` 逐项退役：模块落地时才删对应条目，全部落地后整项删除 | `cli.py` | 提前删 = 未实现命令变成默认成功 |
| K9 | 簇 id 由簇内最小规范化路径派生（sha1 前 12 位），不依赖遍历顺序 | `plan/02` §五 | 改会让报告 diff 永远有噪声 |
| K10 | 报告 JSON：`sort_keys=True, ensure_ascii=False, indent=2` + LF + 结尾换行 | `plan/02` §五 | 双跑逐字节一致会红 |
| K11 | 真值只存输入与期望；出现 `observed_*` 字段即缺陷 | `plan/04` §2.3 | 基准退化成"比对两份预先算好的答案" |
| K12 | 程序不得删除文件：无 `os.remove`/`unlink`/`rmtree` | `tests/test_repo_invariants.py` | 静态断言当场红 |

## 四、本机环境坑（实测过）

1. **`python` 不可用**（商店占位符）：一律 `py -3.8` / `py -3.12`；
   venv 内的可执行文件用 `./.venv312/Scripts/python.exe` 相对路径。
2. **`py -3.8 -m venv` 的 ensurepip 装的是 pip 20.2.3**，它不认 PEP 660 editable install，
   会报 `File "setup.py" not found`。处置：先
   `pip install -U "pip<25"`（24.3.1 可用），再 `pip install -e ".[dev]"`。
3. **pip 被注册表系统代理污染**：命令前加 `NO_PROXY="*" no_proxy="*"`，
   并加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。
4. **`pyc` 偶发损坏**：全程 `PYTHONDONTWRITEBYTECODE=1`；出怪错先清 `__pycache__` 再重跑。
5. **中文控制台**：跑命令加 `PYTHONUTF8=1`（CI 里也设了该 env）。
6. **终端不写 `-X utf8` 也不能崩**：`cli._ensure_utf8_stdio()` 已按 `stream.encoding` 探针处理；
   测试里用无 `reconfigure` 的 `StringIO` 覆盖该分支。
7. **`.venv312/`、`.venv38/` 已被 `.gitignore` 的 `.venv*/` 覆盖**（原本只写 `.venv/`，
   会漏掉带后缀的 venv，已修）。
8. **Git Bash 下 `rmdir` 空目录**可用，但删除非空目录要用 `rm -rf`；
   不要用 `rm` 去删仓库内文件（本项目的静态断言也不允许代码这么做）。

## 五、DoD checklist（M1 验收，逐项打勾）

- [ ] `data/samples/` 覆盖 `plan/04` §2.2 全部十类形态
- [ ] `truth/manifest.json` 无 `observed_*` 字段；`tier`/`variant` 取值在枚举内
- [ ] `py -X utf8 tools/gen_samples.py --check` 输出 `SAMPLES_FROZEN_OK`
- [ ] 生成器改坏一处 → `--check` 必红（变异验证，不是只看它绿）
- [ ] 1000 份混合格式扫描 exit 0，无未捕获异常
- [ ] 二次扫描耗时下降 ≥ 80%（同机同进程各跑一次，记录实测数字）
- [ ] 无文本层 PDF / 加密 PDF 分别落 `skipped:no_text_layer` / `skipped:encrypted`，并在汇总里点名
- [ ] 改 mtime 后重扫，该文件缓存失效（重新抽取）
- [ ] 双解释器（3.8 / 3.12）全量 pytest 绿
- [ ] `python -m docdedupe scan <dir>` 与裸 `doc-dedupe scan <dir>` 两条通路都跑通
- [ ] `plan/00` 台账对应行翻 ✅、`plan/05` M1 DoD 勾选、`plan/HANDOFF-M2.md` 落盘

## 六、关键命令速查（均在本轮实跑过或明确标注待验证）

```bash
# 环境（Windows + Git Bash）
py -3.12 -m venv .venv312
./.venv312/Scripts/python.exe -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple

py -3.8 -m venv .venv38
./.venv38/Scripts/python.exe -m pip install -U "pip<25" -i https://pypi.tuna.tsinghua.edu.cn/simple
./.venv38/Scripts/python.exe -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 测试（双解释器）
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv312/Scripts/python.exe -m pytest -q
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv38/Scripts/python.exe  -m pytest -q

# 冒烟
PYTHONUTF8=1 ./.venv312/Scripts/doc-dedupe.exe selftest
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe --version

# 题面定稿门禁（路径按本机 skill 安装位置替换）
py -3.12 "<ai-tool-topic-definer>/scripts/check_topic.py" .
```

**待验证（本机没有的东西，别在 M1 里假装绿）**：

```bash
# CI —— 仓库尚无远端，这些都没跑过
gh run list --limit 5
# wheel 安装面 —— 本机一直用 editable，没验证过 pip install 出来的包
pip install . && doc-dedupe selftest
```
