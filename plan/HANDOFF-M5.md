# HANDOFF-M5 · 隔离与回滚

- 写于：2026-10-17（M4 收尾）
- 用法：新对话读本文件即可续接。启动命令由用户侧拼接绝对路径：
  `/goal 读取 "<项目绝对路径>\plan\HANDOFF-M5.md" 继续完成任务`
- 上一棒：M4（模块④ 差异复核界面），已完成并提交
- 下一棒：**M5（模块⑤ 安全执行与回滚）**

## 一、当前进度与证据

M4 已完成：`serve` 起本地单页复核界面（簇列表 + 双栏差异高亮 + 摘要 + 保留选择），
界面只读 `clusters` 表、零外链、零网络、无构建步骤；`diffing.py` 落地段落级 diff 与唯一的摘要函数；
两个破坏性 POST 接口校验 `confirm` 后**显式返回 501**（执行层留给 M5）。
模块⑤ 起尚未实现——`quarantine` / `undo` 仍在 `STAGE_NOT_READY`。

| 交付物 | 位置 | 证据 |
|---|---|---|
| 段落级 diff + 字符级高亮 + 摘要 | `src/docdedupe/diffing.py` | B5 逐对点名 **63/63**；`tests/test_diffing.py` 33 项 |
| 本地复核服务 + 只读读模型 | `src/docdedupe/web/server.py` | `tests/test_web.py` 31 项（三方一致、零外链、只读、边界） |
| 单页内联界面 | `src/docdedupe/web/static/index.html` | 唯一前端产物；`web/` 下无 `.js`/`.css` |
| 只读索引连接 | `store.connect_readonly` | `mode=ro`；写入被 SQLite 拒绝 |
| B5 基准 + 探活 | `tools/bench_diff.py` | 63/63；三种注入各 FAIL |
| socket 拦截审计 | `tests/test_no_network.py` | 全链路零 socket 尝试；HTTP 侧零非回环连接 |
| CLI `serve` | `src/docdedupe/cli.py` | 已从 `STAGE_NOT_READY` 退役；缺 extra → exit 3 |
| 出口门截图 | `plan/media/M4-review-page.png` | 浏览器实操（27 簇、近重复双栏高亮 4 处 mark） |
| 测试 | 全量 | 3.8 与 3.12 各 **394 项**全绿 |

**诚实口径**：CI 仍未跑过（仓库无远端）。以下"绿"都是本地等价校验。
`plan/05` M4 的原 DoD 写"无命令行完成…触发隔离全流程"，但隔离执行层属 M5（`plan/03` 模块⑤），
M4 交付的是复核 UI + 诚实占位接口；该口径变更已写进 `plan/05` M4 节，未改模块划分。

M4 实测数字（同机）：

| 项 | 数字 |
|---|---|
| 真实服务 | `serve --db C:/demo/index.db --port 8791`；`/` 200（16432 B，外链命中 **0**）、`/api/clusters` 200、`/{id}/diff` 200、`/docs` 与 `/openapi.json` **404** |
| 三方一致 | `clusters` 表 / 页面 API / `report --format json`：27 簇、84 份，cluster_id·tier·keep·reason·成员路径逐项一致 |
| B5 | **63/63 = 100%**（no-change 12 / container-only 3 / deletion 14 / replacement 28 / crafted 6） |
| B5 探活 | `count-drift` → 6/63 FAIL；`container-blind` → 60/63 FAIL（恰好 G010×2+G011）；`all-signature` → 18/63 FAIL |
| 摘要分布 | 正文逐段一致 12、差异仅来自版式或编码 3、仅差 2 段 14、仅差 4/6/8/10/12 段 28；**"差一个署名" 合成语料 0 例**（见下） |
| 浏览器实操 | 27 张卡片全渲染；近重复簇 `cdf6c9a258711` 摘要"仅差 4 段"、统计"新增 0 / 删除 0 / 替换 2（差异段 4）"、字符级 `mark` 4 处（`本`/`现`、`按既定`/`依统一`） |
| 隐私审计 | `scan`→`report`→读模型→渲染 零 socket 尝试；HTTP 侧零非回环连接；`serve` 收到 `127.0.0.1`；非回环在 CLI 入口 exit 2 |
| 回归 | B3 仍 `B3 OK`（20/20）；B4 仍 `27/27`；`gen_samples.py --check` → `SAMPLES_FROZEN_OK` |
| 顺带修复 | `cluster._others_clause` 两成员簇输出"其余 1 份份正文等长"（重复量词）→ 改为"其余 1 份正文等长"。**理由字符串进报告，故 M3 记录的 JSON 摘要 `7f0026ae…` 作废**：重算后同机两次运行与跨解释器逐字节一致，新摘要 `333192d0fb3448ad…`；档位/成员/保留建议/打分口径未变（B3/B4 回归绿；测试 298 → 394 是新增用例，不是改判据） |

## 二、下一棒待办（M5）

顺序按依赖排列：

1. **`src/docdedupe/quarantine.py`**：manifest 状态机 + `shutil.move` + undo + 冲突处理（`plan/03` 模块⑤）
   - 形状见 `plan/03` 模块⑤ 的 manifest 示例（`batch_id / created_at / cluster_id / keep / items[]`，
     item 含 `status / original_path / quarantine_path / sha256 / size / moved_at`）
   - **先写后动**：manifest 落盘（`pending`）后才移动，移动成功改 `moved`；中断后可续做
   - Windows 长路径 `\\?\` 前缀 + 句柄重试；**只隔离、不删除**（AST 门已存在，勿触）
   - undo 前校验 `sha256`，移回后逐字节一致，失败即中止并报告
2. **`cli.py`：`quarantine` / `undo` / `reports` 三个子命令**从 `STAGE_NOT_READY` 退役
   - `STAGE_NOT_READY` 清空后按 `cli.py` 顶部注释删掉该常量与 `_not_ready`，并同步改
     `tests/test_cli_contract.py` 的 `parametrize(sorted(STAGE_NOT_READY))`（会变成空参数化，必须处理）
   - `cli.py` 顶部退出码表、README、`selftest` 里引用 `STAGE_NOT_READY` 的检查一并改
3. **`serve` 的两个 POST 接到执行层**：`web/server.py` 里 `/api/quarantine`、`/api/undo` 现在是
   `_confirm(...)` 之后返回 501；M5 换成调用执行层，**返回体与 CLI 同源**（别在 Web 层重写一遍状态机）。
   注意 `plan/03` 模块④ 的"只读索引"不变量：执行层自己开写连接，复核的读连接保持 `mode=ro`。
4. **B6 基准 + 探活**：撤销后路径与内容逐字节一致（sha256 断言）；中断恢复用例；
   注入（如"undo 顺序反了 / 少校验 sha256 / pending 项被跳过"）后必须报未达标。
5. **端到端出口门**：扫描 → 报告 → 隔离 → undo 复原；隔离区与索引库不得进 `git status`。

## 三、既定口径清单（K 编号，动了会打挂测试）

M4 新增的编号从 K46 起；K1–K45 原样保留。

| 编号 | 口径 | 位置 | 变了会怎样 |
|---|---|---|---|
| K1–K12 | 见 `plan/HANDOFF-M1.md` §三 | — | 原样有效 |
| K13–K21 | 见 `plan/HANDOFF-M2.md` §三 | — | 原样有效 |
| K22–K32 | 见 `plan/HANDOFF-M3.md` §三 | — | 原样有效 |
| K33–K45 | 见 `plan/HANDOFF-M4.md` §三 | — | 原样有效 |
| K46 | 摘要只有四句话，全部由 `diffing.summarize` 产出；`diffing.py` 是唯一出现这四句话的模块 | `diffing.SUMMARY_*` / `COUNT_TEMPLATE` | 页面或基准各自拼文案会漂移；`tests/test_diffing.py::test_the_summary_sentences_are_composed_in_exactly_one_module` 与 `::test_the_page_contains_no_summary_wording_of_its_own` 会红 |
| K47 | 摘要判定顺序：一致 → 归一化等价（版式/编码）→ 差一个署名 → 计数。"版式"必须在"计数"之前 | `diffing.summarize` | 顺序颠倒会让全半角 twins 显示"仅差 18 段"，与同一索引的 `exact` 档位自相矛盾；B5 的 container-only 门立刻红 |
| K48 | `仅差 X 段` 的 X = 新增段数 + 删除段数 + 2×替换对数；X 必须等于本次 diff 的差异段数 | `diffing.DiffStats.differing` | 摘要与自己概括的块不一致就是漂移；B5 gate 0 逐对断言 |
| K49 | 差异按**抽取文本**（`documents.text`）逐段算，段落 = 去空行后的行；**不用归一化文本** | `diffing.paragraphs` / `diff_documents` | 用归一化文本会丢掉行结构与页眉页脚，复核看不到差异；模板判据（K35）同因 |
| K50 | **"差一页" 不实现**：无页级切分依据，不编阈值 | `data/README.md` §四.10 / `plan/03` §六 | `tests/test_diffing.py::test_no_page_level_claim_can_be_made`（配置里不得出现 page 阈值、摘要不得含页级断言）会红 |
| K51 | `差一个署名` 要求"恰好一对替换段 + 两侧都是短署名行 + 没有别的增删改" | `diffing._is_signature_only` / `is_signature_line` | 放宽会把"署名变了 + 正文也变了"报成一处差异，藏起另一个差异 |
| K52 | Web 层零判定：tier/keep/reason/scores 一律读 `clusters` 表（含 `scores_json`），不重算 | `web/server.clusters_view` | 静态 AST 门（不得调用 `build_clusters`/`clusters_from_index`/`scores_json`/`shingle_set`）+ 三方一致测试会红 |
| K53 | 复核只用 `store.connect_readonly`（`mode=ro`）；写连接不得出现在 `web/server.py` | `store.connect_readonly` / `web/server.py` | 静态门（AST 调用名）+ 运行时门（把 `store.connect` 换成抛异常跑请求）+ `sqlite3.OperationalError` 断言三处会红 |
| K54 | 未 `report` 的索引：页面返回 `status=empty` + 提示先跑 `report`，**不自动聚类、不写库** | `web/server.EMPTY_HINT` / `clusters_view` | 自动聚类会违反 K53；测试断言此时 `clusters` 仍 0 行 |
| K55 | 零外链在**生成出口**常驻校验（`render_page`），不只靠测试；`/docs`、`/redoc`、`/openapi.json` 关闭 | `web/server.assert_no_external_resources` / `create_app` | 加一个 `<link>` 或任何 `<script src`/`<img`/`<iframe`/`@import` 即抛 `ExternalResourceError`；`web/` 下多出 `.js`/`.css` 也会被测试抓到 |
| K56 | 页面按 `kind` 用两个游标对齐两侧 `segments`，**不重算对齐**；字符级 `spans` 由服务端下发 | `web/static/index.html` 的 `buildRows` / `diffing.changed_spans` | 页面自己算高亮＝第二套口径（测试断言页面不含 `shingle`/`jaccard`） |
| K57 | Web 栈的导入被限制在 `web/server.py`，且缺依赖时 `serve` exit 3（真让 `import fastapi` 失败来验） | `web/__init__.MissingWebDependency` / `cli.cmd_serve` | 把 fastapi 提到其他模块会让"缺 extras 也能 scan/report"的承诺失效 |
| K58 | 两成员簇的理由文案是"其余 1 份正文等长"，不是"其余 1 份份正文等长" | `cluster._others_clause` | 量词重复；B4 只数维度词，改坏了要人工看报告/页面才发现 |
| K59 | 隔离执行层不在 M4 抢跑：两个 POST 校验 `confirm` 后返回 **501 + M5 说明**，不移动文件 | `web/server.QUARANTINE_NOT_IMPLEMENTED` | M5 把它们接到执行层时**只需替换 501 分支**；测试比对请求前后语料目录逐项一致 |

## 四、本机环境坑（M4 实测新增）

M0–M3 的坑仍然有效，新增：

1. **`fastapi`/`uvicorn` 不在两套 venv 里**（M4 开始时实测缺失）。已装进 `.venv312`（fastapi 0.143 /
   uvicorn 0.54）与 `.venv38`（fastapi 0.124 / uvicorn 0.33）。换机器要 `pip install -e ".[web]"`，
   否则 `tests/test_web.py` 的 HTTP 用例会跳过、`tests/test_no_network.py` 的 HTTP 用例会 ImportError。
2. **`starlette.testclient` 在 3.12 上会发 `StarletteDeprecationWarning`**（"install httpx2"）。
   `pyproject.toml` 的 `filterwarnings` 只把 `docdedupe.*` 的 DeprecationWarning 变成 error，
   所以是"仅警告"；不要把过滤放宽成全局 `-W error`，否则整个 web 测试会红。
3. **`sqlite3` 只读 URI 的写法**：`Path(db).resolve().as_uri() + "?mode=ro"`（→ `file:///D:/x.db?mode=ro`）
   可用；`file://D:/x.db` 会报 `invalid uri authority: D:`。只读连接上写操作抛
   `OperationalError: attempt to write a readonly database`——这正是模块④只读的强制手段。
4. **`report` 有簇时退出码是 1**（M3 坑，仍然有效）：`serve` 的前置脚本里
   `report ... && serve ...` 会在有重复时中断，要按码分支（`serve` 自身仍返回 0/2/3）。
5. **`store.cluster_rows` 的列集在 M4 扩了一列 `built_at`**（页面要显示聚类时间）。
   改这行 SELECT 时注意 `web/server.py` 依赖它；`test_web.py` 的成员对齐测试用 `zip` 迭代，
   丢列不会报错但会静默错位——加列没问题，删列/换序要同步改 `_member_records`。
6. **IAB 里 Playwright 的 `click` 对滚动容器内元素会超时**（本机实测，元素 visible、`count()==1` 也超时，
   3 s 预算内没等到可操作性）。改用 `getByText(...).evaluate(el => el.scrollIntoView(...))` 取
   `getBoundingClientRect()`，再用 `tab.cua.click({x, y})` 点坐标；**量坐标与点击必须在同一个
   JS cell 里**，否则列表滚动会让坐标失效（M4 实操踩过一次：点到了别的簇）。
7. **`plan/media/` 里的截图会把界面上的文字连同路径一起拍进去**：截图前把索引放到中性路径
   （用 `C:/demo/index.db` 而不是 `%LOCALAPPDATA%\Temp\...`），否则画面里带着本机用户名路径，
   M6 的脱敏扫描会（合理地）挑出来。

## 五、DoD checklist（M5 验收，逐项打勾）

- [ ] `quarantine` 把显式列出的成员移进 `.dedupe-quarantine/<batch-id>/` 并写 manifest（`pending → moved`）
- [ ] 无 `--confirm` 时 exit 2 且**不触碰任何文件**（有断言）
- [ ] `undo` 前校验 sha256，移回后与原文件逐字节一致；校验失败即中止并报告
- [ ] 中断恢复：`pending` 项重跑可续做；已在隔离区的项补齐状态
- [ ] 破坏性开关语义是"动哪些"（`--move` 显式列举），组外文件不动的测试
- [ ] `reports` 子命令产出 CSV/JSON：`batch_id / cluster_id / tier / keep / moved_path / original_path / sha256 / status`
- [ ] Web 的两个 POST 从 501 接通执行层，返回体与 CLI 同源
- [ ] 代码层无 `os.remove`/`unlink`（已有 AST 门，扩展后仍绿）
- [ ] 隔离区与索引库不在 `git status` 里
- [ ] B6 基准 + 探活（注入后必须报未达标）
- [ ] 双解释器全量 pytest 绿
- [ ] `plan/00` 台账对应行翻 ✅、`plan/05` M5 DoD 勾选、`plan/HANDOFF-M6.md` 落盘

## 六、关键命令速查

```bash
# 测试（双解释器；web 用例需要 .[web]）
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv312/Scripts/python.exe -m pytest -p no:cacheprovider
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv38/Scripts/python.exe  -m pytest -p no:cacheprovider

# 样本冻结门禁
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/gen_samples.py --check

# 扫描 + 三个基准（注意用 Windows 临时目录或中性路径，见 §四.7）
D="$LOCALAPPDATA/Temp/m5run"; rm -rf "$D"; mkdir -p "$D"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe scan data/samples --db "$D/i.db"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_recall.py --db "$D/i.db"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_tiers.py  --db "$D/i.db"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_diff.py   --db "$D/i.db"

# 起复核页（退出码 0/2/3）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe report --db "$D/i.db"   # 有簇时退出码 1
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe serve  --db "$D/i.db" --port 8791

# 库里的 clusters（模块④的输入，模块⑤的快照来源）
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

**待验证（别在 M5 里假装绿）**：CI 四矩阵、wheel 安装面。仓库仍无远端。

## 七、给 M5 的三条硬提醒

1. **执行层是唯一的写者。** 复核页保持 `mode=ro`（K53），隔离/撤销自己开写连接并只经
   `quarantine.py`；不要让 `web/server.py` 出现写连接，也不要在 Web 层重写一遍 manifest 状态机
   ——两个入口各写一套，"撤销后逐字节一致"这条承诺就没法证明。
2. **先写 manifest 再动文件。** 顺序反了（先 move 再写 manifest）在中断时就丢了"哪一份去了哪里"，
   undo 只能靠猜；`plan/03` 模块⑤ 的 `pending → moved` 状态机与"从 manifest 恢复"验收都建立在这个顺序上。
3. **验收要能失败。** 撤销后 sha256 一致、中断恢复、无 `--confirm` 不动文件——每条都要有注入/反例
   （例如故意跳过 sha256 校验、故意逆序 undo、故意在 `pending` 时中断），否则是"恰好跑通一次"，
   不是门。M4 的 B5 用的是同一套做法（三个注入各 FAIL 才算数）。
