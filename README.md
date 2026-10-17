# DocDedupe · 本地文档内容级去重整理器

给本机文档做**正文级查重**：跨 PDF / Word / Markdown / TXT / HTML，找出"同一份内容的多个版本"，
用可读的差异对比告诉你该留哪一份，并且**只隔离、不删除**。

现有去重工具基本止步于哈希或图像相似；"跨格式正文级近重复 + 保留建议 + 可解释差异"是空缺。
中文没有空格分词、页眉页脚污染、标书 / 论文 / 合同的模板族判定，是英文工具的结构性盲区。

- **纯本地**：无服务器、无账号、无上传、无遥测。
- **可逆**：任何"收走"都进 `.dedupe-quarantine/` 并写 manifest，一条命令原样移回。
- **可解释**：每一簇都给出人类可读的保留理由，不给裸分数。

## 状态

**M6 · 脱敏发布**。五个模块全部落地，仓库公开发布：源码脱敏（跟踪文件与 git 历史里没有本机路径、
真实文档名、私人邮箱），四个基准分别在 CI 上跑通，发布产物带 sha256 留档可复验。

复核之后可以真的动手，而且每一步都可逆：把选中的成员移进
`.dedupe-quarantine/<batch-id>/`，**先写 manifest 再移动**；`undo` 复原时逐个校验 sha256，
校验通过才按逆序移回，任一项不对就整批中止、不碰任何文件。代码里没有删除路径（静态断言）。

| 命令 | 状态 |
|---|---|
| `doc-dedupe --help` / `version` / `selftest` | ✅ 可用 |
| `scan` | ✅ 可用（M1 扫描与格式适配；M2 归一化 + 指纹 + 候选召回） |
| `report` | ✅ 可用（M3 聚类与保留建议，md/csv/json 报告） |
| `serve` | ✅ 可用（M4 差异复核界面；隔离按钮已接通执行层） |
| `quarantine` / `undo` / `reports` | ✅ 可用（M5 隔离、复原与隔离记录导出） |

里程碑与验收标准见 [`plan/05-里程碑与发布.md`](plan/05-里程碑与发布.md)。

## 快速开始

需要 Python 3.8 及以上。

```bash
git clone https://github.com/yuluo554/10-17-doc-dedupe.git && cd 10-17-doc-dedupe
python -m venv .venv
# Windows (Git Bash)
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
# Linux / macOS
./.venv/bin/python -m pip install -e ".[dev]"

doc-dedupe --version
doc-dedupe selftest
```

装齐全部能力（含复核页与测试所需的 Web 栈）用 `pip install -e ".[dev,web,extract]"`：
缺 `web` 时 `serve` 返回 3，且 `tests/test_web.py` 与 `test_no_network.py` 的 HTTP 用例会**跳过**——
跳过不是通过，CI 装的就是这套 extras。

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
- 抽取 PDF 文本层 / DOCX 段落与表格 / Markdown / TXT / HTML（Markdown 的 `#`、列表符等语法标记按容器语法剥离）；
- 把**读不出正文**的文件显式落库为 `skipped:no_text_layer` / `skipped:encrypted` /
  `skipped:unsupported`，在汇总里逐条点名，**不当成"空文档"混进候选集**；
- 归一化正文（NFKC → 去空白与不可见字符 → 去页码行 → 去跨页高频短行 → 拉丁小写），
  让全半角、页眉页脚、页码噪声和跨格式差异在比较前消失；
- 生成字符 3-gram 指纹（64-bit SimHash 预筛 + MinHash(128) + LSH 32×4 召回），
  对候选对用精确 Jaccard 打分，写入 `pairs`（`score_kind = exact_jaccard`，
  同时留 `jaccard_estimate` 供审计）；
- 写 SQLite 索引并按缓存键复用结果：文件未变时第二次扫描不再打开文件，
  指纹与候选对也不重算（跳过条件由两个摘要证明，见下）。

汇总示例：

```
scanned 101 file(s): 101 indexed (0 from cache, 101 extracted) in 0.99s
  ok                       94
  skipped:encrypted        2
  skipped:no_text_layer    2
  skipped:unsupported      3
fingerprinted 94 document(s) this pass (0 rebuilt from stored text); 94 fingerprint row(s) in the index
recall: 94 document(s) compared, 877 band candidate(s), 455 rejected by simhash, 324 below the 0.55 retention floor, 98 stored pair(s), 81 at or above 0.80
index: .../index.db
```

第二次扫描（目录未变）：

```
scanned 101 file(s): 101 indexed (101 from cache, 0 extracted) in 0.03s
...
recall: not recomputed (no fingerprint changed and the stored pair table still matches its recorded digest); 98 pair(s), 81 at or above 0.80
```

`pairs` 是派生状态：每次重算都整表替换，所以被编辑过的文件不会留下陈旧边。
跳过的条件是两个摘要同时成立——召回输入摘要（文档集合、归一化文本哈希、全部指纹、算法参数）
与结果摘要（`pairs` 表内容），任一不符即重算。

### 召回基准（B3）

```bash
python tools/bench_recall.py --db <索引库> --samples data/samples
```

逐组点名输出 PASS/FAIL，并分别报告两个门：near 组召回率（门槛见 `data/README.md` §四.7）
与 `negative` 组是否零误报。任一门不达标即退出码 1。基准本身可被证伪：

```bash
python tools/bench_recall.py --db <索引库> --inject-edges drop-above    # 必须 FAIL
python tools/bench_recall.py --db <索引库> --inject-edges drop-random   # 必须 FAIL
python tools/bench_recall.py --db <索引库> --inject-groups shift-groups # 必须 FAIL
```

模板族（G201–G203）在基准里以 `INFO` 行呈现：它们按设计落在近重复阈值以下，
档位由模块③按"同构骨架 + 字段位差异"判定，不属 M2 的召回门。

### 聚类与保留建议（`report`）

```bash
doc-dedupe report [--db 索引库路径] [--format md|csv|json] [--out 输出文件] [--tier exact|near|template]
```

`report` 读同一份索引，把候选对聚成簇并判定三档：

- **完全重复**（`exact`）：归一化正文逐字节一致（或内容哈希一致）→ 建议保留 1 份；
- **近重复**（`near`）：正文相似度达到近重复阈值、且不属于模板族 → 建议保留 1 份，差异可复核；
- **模板族**（`template`）：同构骨架 + 差异仅出现在字段位（合同 / 周报 / 发票摘要）
  → **单列一档，不产生"建议保留"**，只提示人工裁决。

关键纪律：

- **输入是 `pairs` 整表，不只是 ≥ 阈值的那部分。** 模板族成员天然落在 0.55–0.80，
  若在这里先按近重复阈值过滤，`template` 档会永远是空的。低于阈值、又不符合模板判据的对
  （如两个不同组的 0.561 对）只是证据：它们**不连边**，不会把两个真值组合并。
- **打分与理由字符串只在 `cluster.py` 一处实现**，五维打分（正文完整度 / 修改时间 / 路径层级 /
  文件名 / 目录归属）。其中"正文完整度"与"文件名无副本标记"是**门槛**而非权重：
  截断副本通常 mtime 最新，纯加权和会把它当成最佳保留对象；带 `(1)` 的文件按定义就是副本。
- **每簇理由至少含两个判定维度**，禁止只输出裸分数。测试对每簇逐条断言。
- **簇 id 与成员顺序由规范化路径排序派生**，不依赖并查集遍历顺序，两次运行逐字段一致。
- **报告层零判定**：`report.py` 只做渲染，三个格式是同一个 `Report` 的三种序列化；
  测试用 AST 断言该模块不含阈值常量、不含与浮点数的比较。

退出码：**有簇 → 1，无簇 → 0，索引缺失或为空 → 2**。报告的默认路径是
`./.dedupe-cache/index.db`（与 `scan .` 写出的位置一致）。

报告示例（节选）：

```
# 文档重复报告

共 27 簇、84 份文件。本报告只给建议：程序永不删除文件，任何处置都需要人工确认。

| 档位 | 簇数 | 说明 |
|---|---|---|
| 完全重复 | 7 | 归一化正文逐字节一致，建议保留 1 份 |
| 近重复 | 17 | 正文相似度达到近重复阈值，建议保留 1 份并复核差异 |
| 模板族 | 3 | 同构骨架 + 字段位差异，单列一档，需人工裁决 |

## 模板族 · cfc6e66f42b92（4 份）

- 建议保留：无（模板族只提示人工裁决，不建议批量处置）
- 理由：模板族：4 份成员共享同构骨架（14 行、字段位序列一致），差异仅出现在 6 个字段位；不给出保留建议，需人工裁决。
```

`--format json` 逐字节可复现（无时间戳、无随机序，键排序固定），可直接被下游脚本解析：

```json
{
  "schema": "doc-dedupe-report/1",
  "summary": {"clusters": 27, "members": 84, "by_tier": {"exact": 7, "near": 17, "template": 3}},
  "clusters": [{"cluster_id": "...", "tier": "template", "keep": null, "reason": "...", "members": []}]
}
```

### 分档基准（B4）

```bash
python tools/bench_tiers.py --db <索引库> --samples data/samples
```

逐组点名输出 PASS/FAIL：每组必须落进文档口径推出的档位、全部成员同簇、非模板簇必须有保留建议
且理由含至少两个判定维度、模板簇必须**没有**保留建议；另外检查没有任何簇混入两个真值组。
任一不达标即退出码 1。基准同样可被证伪：

```bash
python tools/bench_tiers.py --db <索引库> --inject-clusters template-as-near   # 必须 FAIL
python tools/bench_tiers.py --db <索引库> --inject-clusters near-as-template   # 必须 FAIL
python tools/bench_tiers.py --db <索引库> --near-threshold 0.5                 # 必须 FAIL
```

`--near-threshold 0.5` 会把 G001 与 G015 通过它们 0.561 的对并成一个簇：这条注入正是
"跨真值组污染"门的由来——只看每组档位标签会漏掉它（每组仍报着"应有"的档位），
必须直接检查簇里的成员来自哪些真值组。

模板族在真值 manifest 里记为 `near`（生成类别），但它们的差异全是**容器级**
（换格式 / 全半角 / 页眉页脚），归一化后正文逐字节一致，按 `data/README.md` §四.5
应落 `exact`。基准的期望档位由 manifest 自己记录的 `variant` 标签推导，**不读引擎输出**，
所以不可能"按构造"与引擎一致。

### 复核界面（`serve`）

```bash
doc-dedupe serve [--db 索引库路径] [--port 8765] [--host 127.0.0.1] [--open]
```

打开 `http://127.0.0.1:8765/`（CLI 会把地址打印出来）：

- **左侧**：簇列表，带三档标签、推荐保留项与理由字符串；
- **右侧**：所选簇的双栏差异——段落级增删改 + 改动字符的高亮——以及一句摘要
  （"仅差 3 段 / 差一个署名 / 差异仅来自版式或编码 / 正文逐段一致"）；
- **下方**：成员打分表（完整度 / 修改时间 / 路径层级 / 文件名 / 目录）与"保留哪一份"单选；
- **隔离按钮**与左侧的**隔离批次列表**：点击按钮会先把要移动的文件逐个列在确认框里，确认后把
  "除保留项之外"的成员移进 `.dedupe-quarantine/<batch-id>/`；批次列表显示每一批"已隔离 / 已复原 /
  待处理"，并有"复原这一批"按钮（复原前同样先确认）。

几条写进不变量、并有测试守着的纪律：

- **只读索引**：连接以 SQLite 的 `mode=ro` 打开，写操作由数据库直接拒绝；页面展示的档位、保留建议、
  理由与分数**全部读 `clusters` 表**（`report` 写入的那张表），Web 层不重算——否则会出现
  "报告说留 A、页面说留 B"而无从判断谁错。测试把"表 / 页面 API / `report --format json`"
  三方逐簇逐项对齐（27 簇）。
- **零外链、零构建**：整个界面是 `web/static/index.html` 一个文件（内联 CSS/JS，原生 JS），
  仓库里没有 `package.json`/`node_modules`/`.js`/`.css`；生成出口**每次都**校验页面不含
  `<link>`/外链脚本/图片/`@import`/CDN，测试用七种 CDN 片段证明这道门会红；
  `/docs`、`/redoc`、`/openapi.json` 一律 404（Swagger UI 会引 CDN）。
- **零网络**：socket 拦截动态审计跑完 `scan` → `report` → 页面渲染 → HTTP 请求，断言对外连接为 0；
  服务只接受回环地址，非回环在 CLI 入口就 exit 2。
- **刚 `scan` 完还没 `report`**：页面能打开，并提示先运行 `report`——**不会自动聚类**。
  自动聚类要么写库（页面就成了写入方），要么算而不写（页面与表不一致），两条都比提示更糟。
- **摘要口径只有一处**：`diffing.py` 是唯一产出这四句话的模块（静态断言），页面只显示接口给的字符串。
  其中"差一页"首期**不实现**：页级相似度需要页边界，而抽取层只给一条纯文本流；缺定义就不编阈值，
  故降级为扩展项（`plan/03` §六），基准每次运行都打印这条降级。

### 隔离与回滚（`quarantine` / `undo` / `reports`）

```bash
# 把某一簇里除保留项之外的成员移进隔离区（--move 逐个列举要动的文件）
doc-dedupe quarantine --db index.db --cluster <簇 ID> \
    --keep "D:/docs/B/方案.docx" --move "D:/docs/A/方案(1).docx" --confirm

# 复原某一批（校验 sha256 后按逆序移回原路径）
doc-dedupe undo --db index.db --batch 20261017T101500Z-7f3a --confirm

# 导出隔离记录（一行一个被移动的文件）
doc-dedupe reports --db index.db --format csv --out out/
```

隔离区就落在**这些成员共同父目录**下的 `.dedupe-quarantine/<batch-id>/`：`manifest.json` 加被移动的文件。
manifest 是唯一的状态机（`pending → moved → restored`），命令与页面都读它，不各写一套：

```json
{
  "schema": "doc-dedupe-quarantine/1",
  "batch_id": "20261017T101500Z-7f3a",
  "created_at": "2026-10-17T10:15:00Z",
  "cluster_id": "c1a2b3c4d5e6",
  "keep": "D:/docs/B/方案.docx",
  "items": [
    {
      "status": "moved",
      "original_path": "D:/docs/A/方案(1).docx",
      "quarantine_path": "D:/docs/.dedupe-quarantine/20261017T101500Z-7f3a/方案(1).docx",
      "sha256": "…",
      "size": 12345,
      "moved_at": "2026-10-17T10:15:01Z"
    }
  ]
}
```

**纪律（每条都有测试或基准守着）**：

- **先写后动**：manifest 落盘（全部 `pending`）之后才开始移动，每成功一份改写为 `moved`。
  中途中断，manifest 仍记着"哪一份要去哪里"；`--batch-id` 重跑即续做：`pending` 项现在移，
  已在隔离区的项补齐状态，**不重复搬动**。
- **只隔离、不删除**：包内不存在 `os.remove`/`unlink`/`rmtree` 调用（静态 AST 断言）；
  隔离目录即使复原后也保留，因为它是"当时动了什么"的记录。
- **先校验后移动**：`undo` 先整批校验——每个隔离副本的 sha256 与大小必须与 manifest 一致，
  原路径必须空着（或已是同一份内容，于是 `undo` 可以重复执行）——任何一项不对就整批中止，
  一个文件都不动。逐项移回后再算一次 sha256，"逐字节一致"是量出来的。
- **破坏性开关写"动哪些"**：只移动 `--move` 逐项列举的成员；空列表是用法错误（exit 2），
  绝不当成"全部"。`--keep` 与 `--move` 的每一项都必须是该簇成员（读 `clusters` 表判定），
  且文件必须在扫描后未被改动（大小 / mtime 与索引一致）——复核的必须是磁盘上那一份。
- **同名不覆盖**：同一批里两个成员同文件名时，按 `path_norm` 顺序确定性改名（`方案__2.txt`），
  永远不覆盖隔离区里已有文件。
- **导出字段固定**：`batch_id / cluster_id / tier / keep / moved_path / original_path / sha256 / status`，
  其中 `tier` 读 `clusters` 表，不重算。

### 隔离基准（B6）

```bash
python tools/bench_quarantine.py --db <索引库> --samples data/samples --work <临时目录>
```

B6 在一份语料的副本上跑**完整的往返**：移动 → 逐字节核对（sha256 由基准自己算，不读 manifest）→
复原 → 再逐字节核对；另外造三个场景：中断后续做（在移动缝隙注入故障）、隔离副本被改动、
原路径被别的文件占用。基准同样可证伪：

```bash
python tools/bench_quarantine.py ... --inject skip-sha-check      # 必须 FAIL
python tools/bench_quarantine.py ... --inject skip-pending        # 必须 FAIL
python tools/bench_quarantine.py ... --inject overwrite-on-restore # 必须 FAIL
```

### 差异基准（B5）

```bash
python tools/bench_diff.py --db <索引库> --samples data/samples
```

逐对点名：**差异段号**（改动的究竟是第几段）与**摘要文案**都要对真值，真值由工具自己从两份抽取文本
现算（段落差几个、哪些位置不同、归一化后是否一致），不读引擎输出；另有 6 条 crafted 用例覆盖
合成语料里没有的"差一个署名"，含两条"像署名但不是"的反例。任一不达标即退出码 1。基准同样可证伪：

```bash
python tools/bench_diff.py --db <索引库> --inject-summary count-drift     # 必须 FAIL
python tools/bench_diff.py --db <索引库> --inject-summary container-blind # 必须 FAIL
python tools/bench_diff.py --db <索引库> --inject-summary all-signature   # 必须 FAIL
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
pip install -e ".[web]"       # fastapi + uvicorn，serve 需要（缺它时 serve 退出 3 并给出这条命令）
```

`serve` 之外的一切（`scan` / `report` / 三个基准）在**不含 `web` extra** 的安装上照常工作：
读模型与差异引擎只依赖标准库，Web 栈的导入被限制在 `web/server.py`，缺依赖时只在构建
应用那一步报错，并在 CLI 层翻译成退出码 3。

## 退出码语义

| 码 | 含义 |
|---|---|
| 0 | 正常完成，且没有需要处置的结果 |
| 1 | 正常完成，但**检出了结果**（如存在近重复簇），或基准门槛未达标，或有项没做完（隔离/复原残留待处理） |
| 2 | 用法错误、输入路径不存在、缺少 `--confirm`、请求越界（非本簇成员 / 文件已被改动 / 原路径被占用），或输入全部不可用 |
| 3 | 依赖缺失（缺 `web` extra、缺 PDF/DOCX 抽取依赖） |

`report` 检出结果返回 1 是既定语义，不是故障——脚本化使用时按码分支即可：
`0` 表示"没有重复"，`1` 表示"有重复，去看报告"。
`scan` 在有文件被跳过时**仍返回 0**（跳过的文件在汇总里逐条列出并计数）；
只有在**没有任何文件产出正文**或**一个候选文件都没找到**时才返回 2——那意味着索引没法用于后续步骤，
返回 0 会让脚本误以为扫描成功。

`quarantine` / `undo` 的 1 同样不是故障：它表示"这一批还有项没到位"（移动失败、校验不过、原路径被占用），
原因逐条打印，manifest 记着剩下什么；`quarantine` 用同一个 `--batch-id` 重跑即可续做。
`reports` 只要还有 `pending` 项就返回 1——那正是需要人看一眼的状态。

`serve` 的退出码描述的是**起服务这件事**，不是复核结果：服务正常停止返回 0；没有可用索引
（文件不存在 / 库里没有文档）返回 2；缺 `web` extra 返回 3 并给出安装命令。索引里还没有聚类结果
（刚 `scan` 完还没 `report`）**不影响退出码**——服务照常起，页面与 stderr 都会提示先跑 `report`。

```bash
doc-dedupe scan data/samples --db index.db   # 0
doc-dedupe report --db index.db              # 1：检出簇；0：没有重复
doc-dedupe serve  --db index.db              # 0：服务停止；2：索引不可用；3：缺 web extra
doc-dedupe quarantine --db index.db --cluster C --keep K --move M --confirm   # 0；1：还有项没到位
doc-dedupe undo --db index.db --batch B --confirm                             # 0；1：整批未复原
doc-dedupe reports --db index.db --out out/                                   # 0；1：有 pending 项
```

## 隐私

- 只读本地文件，**不发起任何网络请求**；Web 复核页只绑定 `127.0.0.1`（非回环地址在 CLI 入口即被拒绝）。
- 页面零外链（无 CDN、无外部字体、无外链脚本/图片），断网可用；这条断言常驻在页面生成出口，
  不是只在测试里验一次。
- 复核页对索引**只读**：连接以 SQLite `mode=ro` 打开，写操作被数据库拒绝。页面**会移动文件**，
  但只在点击确认之后，且只移动选中且属于该簇、扫描后未改动的成员；隔离动作要显式 `confirm`
  （页面是确认框，CLI 是 `--confirm`），随时可用 `undo` 原样移回。
- 有一条 socket 拦截审计：把 `socket.socket.connect`、`create_connection`、`getaddrinfo` 换成会抛异常并
  计数的桩，再跑完 `scan` → `report` → 页面渲染 → HTTP 请求，断言**对外连接为 0**（回环流量单独计数，
  只证明测试客户端自己确实用了本地传输）。
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
| [`plan/media/M4-review-page.png`](plan/media/M4-review-page.png) | M4 出口门截图留档（合成数据，浏览器实操） |
| [`plan/media/M5-quarantine-page.png`](plan/media/M5-quarantine-page.png) | M5 出口门截图留档（隔离批次列表 + 按钮接通） |
| [`plan/RELEASE-M6.md`](plan/RELEASE-M6.md) | M6 发布留档：tag、产物 sha256、出口门实测 |
| [`data/README.md`](data/README.md) | 依据查证、数据源可获取性、**数值口径唯一出处** |
| [`data/samples/truth/manifest.json`](data/samples/truth/manifest.json) | 合成样本的植入真值（只记输入与期望分组） |

## 发布与复验

发布留档在 [`plan/RELEASE-M6.md`](plan/RELEASE-M6.md)：tag、产物名与每个产物的 sha256，
以及 tag 时点事实与 tag 后追加事实（分栏）。产物同时挂在
[GitHub Release](https://github.com/yuluo554/10-17-doc-dedupe/releases/tag/v0.1.0) 上，
下载后这样复验（`sha256sum -c` 直接跑；Windows 用 `Get-FileHash` 对照）：

```bash
sha256sum -c <<'SUMS'
092aaa5971ec4aff9568251ee10b03d2a4f7eb061b5d7a9333776591e6b833c6  doc_dedupe-0.1.0.tar.gz
80ce98d3067388b891e444f1bd13d3204c8c8b7693737fe0247ee9b3b13e6270  doc_dedupe-0.1.0-py3-none-any.whl
SUMS
```

两个产物名由 setuptools 规范化，是**下划线**（`doc_dedupe-...`），不是包名里的连字符。

仓库自带的发布门禁把可机械判定的那一部分做成硬门（tag 是 annotated 且在 CI 之上、
留档在 tag 树内、产物 sha256 与留档一致（本地副本与 Release 附件各验一次）、
README 提到的产物名必须真的在 Release 里、`plan/00` 台账已翻转、工作树干净、
跟踪文件与 git 历史里没有本机路径）：

```bash
python tools/check_release.py .          # 0 = 无 FAIL；1 = 有 FAIL；2 = 用法错误
python tools/check_release.py . --no-gh  # 离线时跳过需要网络的那几项（报 SKIP，不报 PASS）
```

## 相似度不是抄袭判断

相似度是机械测量值，报告与界面只陈述"哪些文件相似、差在哪、建议留哪份"，
**不构成抄袭或侵权判断**。所有处置都只是建议，且永远可逆。

## 许可

MIT，见 [`LICENSE`](LICENSE)。
