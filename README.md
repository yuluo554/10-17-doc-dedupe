# DocDedupe · 本地文档内容级去重整理器

给本机文档做**正文级查重**：跨 PDF / Word / Markdown / TXT / HTML，找出"同一份内容的多个版本"，
用可读的差异对比告诉你该留哪一份，并且**只隔离、不删除**。

现有去重工具基本止步于哈希或图像相似；"跨格式正文级近重复 + 保留建议 + 可解释差异"是空缺。
中文没有空格分词、页眉页脚污染、标书 / 论文 / 合同的模板族判定，是英文工具的结构性盲区。

- **纯本地**：无服务器、无账号、无上传、无遥测。
- **可逆**：任何"收走"都进 `.dedupe-quarantine/` 并写 manifest，一条命令原样移回。
- **可解释**：每一簇都给出人类可读的保留理由，不给裸分数。

## 状态

**M1 · 数据先行与扫描层**。合成样本集（带植入真值）与 `scan` 已落地：跨 PDF / DOCX / MD /
TXT / HTML 抽取正文、SQLite 索引、缓存命中、失败态显式落库。后续里程碑按顺序推进，
**未实现的命令显式退出 3 并说明计划里程碑，不返回空结果**。

| 命令 | 状态 |
|---|---|
| `doc-dedupe --help` / `version` / `selftest` | ✅ 可用 |
| `scan` | ✅ 可用（M1：扫描与格式适配） |
| `report` | ⬜ M3（聚类与保留建议） |
| `serve` | ⬜ M4（差异复核界面） |
| `quarantine` / `undo` | ⬜ M5（隔离与回滚） |

里程碑与验收标准见 [`plan/05-里程碑与发布.md`](plan/05-里程碑与发布.md)。

## 快速开始

需要 Python 3.8 及以上。

```bash
git clone <this-repo> && cd 10-17-doc-dedupe
python -m venv .venv
# Windows (Git Bash)
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
# Linux / macOS
./.venv/bin/python -m pip install -e ".[dev]"

doc-dedupe --version
doc-dedupe selftest
```

`selftest` 是**可证伪的**离线自检：安装元数据与包内版本不一致、设置校验器失效、
缓存键不随 mtime 变化、schema 缺表——任一成立都会返回非 0。

### 扫描一个目录

```bash
doc-dedupe scan <目录> [--db 索引库路径] [--ignore DIR]
```

扫描会：

- 递归遍历（跳过 `.git`、`node_modules`、`.dedupe-quarantine/` 等），**按规范化路径排序处理**，
  结果与文件系统枚举顺序无关；
- 用**扩展名 + 魔数双识别**判断格式：扩展名是 `.pdf` 但内容不是 PDF 的文件不会被硬解析；
- 抽取 PDF 文本层 / DOCX 段落与表格 / Markdown / TXT / HTML；
- 把**读不出正文**的文件显式落库为 `skipped:no_text_layer` / `skipped:encrypted` /
  `skipped:unsupported`，在汇总里逐条点名，**不当成"空文档"混进候选集**；
- 写 SQLite 索引并按缓存键复用结果：文件未变时第二次扫描不再打开文件。

汇总示例：

```
scanned 101 file(s): 101 indexed (0 from cache, 101 extracted) in 0.72s
  ok                       94
  skipped:encrypted        2
  skipped:no_text_layer    2
  skipped:unsupported      3
```

### 合成样本与真值

仓库内的 `data/samples/` 全部由 `tools/gen_samples.py` 程序化生成，**不含任何真实文档**：

```bash
py -X utf8 tools/gen_samples.py            # 重新生成
py -X utf8 tools/gen_samples.py --check    # 位级校验，输出 SAMPLES_FROZEN_OK
```

`--check` 是逐字节比对，不是抽样比对；改坏任意一个样本或改动生成器都会红。
大样本量（如 1000 份）用于规模测试，按需生成、不入仓：

```bash
py -X utf8 tools/gen_samples.py --bulk <临时目录> --bulk-count 1000
```

后续里程碑的依赖按需安装：

```bash
pip install -e ".[extract]"   # 兼容别名；扫描依赖已并入核心安装面
pip install -e ".[web]"       # fastapi + uvicorn（M4 起需要）
```

## 退出码语义

| 码 | 含义 |
|---|---|
| 0 | 正常完成，且没有需要处置的结果 |
| 1 | 正常完成，但**检出了结果**（如存在近重复簇），或基准门槛未达标 |
| 2 | 用法错误、输入路径不存在、缺少 `--confirm`，或输入全部不可用 |
| 3 | 依赖缺失，或该命令尚未在本次构建中实现 |

`report` 之类"检出结果返回 1"是既定语义，不是故障——脚本化使用时按码分支即可。
`scan` 在有文件被跳过时**仍返回 0**（跳过的文件在汇总里逐条列出并计数）；
只有在**没有任何文件产出正文**或**一个候选文件都没找到**时才返回 2——那意味着索引没法用于后续步骤，
返回 0 会让脚本误以为扫描成功。

## 隐私

- 只读本地文件，**不发起任何网络请求**；Web 复核页只绑定 `127.0.0.1`。
- 页面零外链（无 CDN、无外部字体、无外链脚本），断网可用。
- 索引库 `*.db`、隔离区 `.dedupe-quarantine/`、导出报告都在 `.gitignore` 里，永不入仓。
- 仓库内不含任何真实文档；示例与基准数据全部由 `tools/gen_samples.py` 程序化合成。

## 文档

| 文档 | 内容 |
|---|---|
| [`plan/00-README总览.md`](plan/00-README总览.md) | 索引、决策记录、承诺-证据台账、里程碑 |
| [`plan/01-题目文档.md`](plan/01-题目文档.md) | 题目定稿：痛点、模块要求、评分标准、边界 |
| [`plan/02-架构与技术选型.md`](plan/02-架构与技术选型.md) | 分层、技术选型与弃选理由、确定性设计 |
| [`plan/03-模块详设.md`](plan/03-模块详设.md) | 五个模块的接口契约与不变量 |
| [`plan/04-数据计划.md`](plan/04-数据计划.md) | 合成样本、真值语义、基准口径 |
| [`plan/05-里程碑与发布.md`](plan/05-里程碑与发布.md) | M0–M6 的交付物与 DoD |
| [`plan/06-交付对标.md`](plan/06-交付对标.md) | 评分栏与招牌动词的证据映射 |
| [`data/README.md`](data/README.md) | 依据查证、数据源可获取性、**数值口径唯一出处** |
| [`data/samples/truth/manifest.json`](data/samples/truth/manifest.json) | 合成样本的植入真值（只记输入与期望分组） |

## 相似度不是抄袭判断

相似度是机械测量值，报告与界面只陈述"哪些文件相似、差在哪、建议留哪份"，
**不构成抄袭或侵权判断**。所有处置都只是建议，且永远可逆。

## 许可

MIT，见 [`LICENSE`](LICENSE)。
