# HANDOFF-M6 · 脱敏发布

- 写于：2026-10-17（M5 收尾）
- 用法：新对话读本文件即可续接。启动命令由用户侧拼接绝对路径：
  `/goal 读取 "<项目绝对路径>\plan\HANDOFF-M6.md" 继续完成任务`
- 上一棒：M5（模块⑤ 安全执行与回滚），已完成并提交
- 下一棒：**M6（脱敏发布）**——本文件第五节是 DoD

## 一、当前进度与证据

M5 已完成：`quarantine.py` 是包内唯一移动文件的地方；manifest 先写后动（`pending → moved → restored`）；
`undo` 先整批校验 sha256/size/原路径占用、再按逆序移回；CLI 的 `quarantine` / `undo` / `reports`
与复核页的两个 POST 共用同一个执行层与同一个 `BatchResult` 负载。**`STAGE_NOT_READY` 常量与 501 占位接口
一并退役**（八条命令全部有 handler，`selftest` 交叉核对 parser 与 handler 两张表）。

| 交付物 | 位置 | 证据 |
|---|---|---|
| 隔离/回滚执行层 | `src/docdedupe/quarantine.py` | B6 **58/58**；`tests/test_quarantine.py` 37 项 |
| 批次登记（只存 manifest 路径） | `src/docdedupe/store.py` `quarantine_batches` | 行内不含 items；`test_the_manifest_is_registered_in_the_index_and_that_row_is_only_a_pointer` |
| CLI `quarantine` / `undo` / `reports` | `src/docdedupe/cli.py` | 退出码 0/1/2 各有断言；`--format json` 与页面同源 |
| 页面接通执行层 | `src/docdedupe/web/server.py` + `static/index.html` | `tests/test_web.py` 35 项（含"返回体=执行层 as_dict()"） |
| B6 基准 + 探活 | `tools/bench_quarantine.py` | 58/58；三条注入各 FAIL（1 / 57 / 56 项） |
| 出口门截图 | `plan/media/M5-quarantine-page.png` | 浏览器实操（批次列表 + 按钮接通） |
| 测试 | 全量 | 3.8 与 3.12 各 **452** 项全绿 |

M5 实测数字（同机）：

| 项 | 数字 |
|---|---|
| 端到端 | 27 簇 / 84 成员索引上 CLI 隔离 2 批（5+2 份）→ 复原；页面再隔离 5 份（GUI 确认框）→ CLI 复原 |
| 逐字节一致 | 语料 102 文件的 sha256 清单：隔离前 == 全部复原后（`diff` 为空） |
| B6 | **58/58 = 100%**（round-trip 55 / interrupted-resume 1 / tampered-copy 1 / occupied-original 1；batches 30、items 57） |
| B6 探活 | `skip-sha-check` → 1 项 FAIL；`skip-pending` → 57 项 FAIL；`copy-instead-of-move` → 56 项 FAIL |
| 回归 | B3 `B3 OK`（20/20）；B4 `27/27`；B5 `63/63`；`gen_samples.py --check` → `SAMPLES_FROZEN_OK (102 files)` |
| 无 `--confirm` | 参数解析即 exit 2；请求前后 102 项 sha256 清单逐项一致 |
| 隐私 | Web 层仍零写连接（K53 静态 + 运行时门未动）；页面零外链断言未动；socket 审计仍绿 |
| 测试 | 394 → **452** 项（新增：隔离执行层 38、B6 基准 15、契约与页面改写 5） |

**诚实口径**：CI 仍未跑过（仓库无远端），以下"绿"都是本地等价校验；wheel 安装面同样未实跑。
B6 的 `--work` 目录**不做清理**（包与工具都没有删除路径，K71），每次运行会在 `--work` 下留下
4 份语料副本与索引，请给一个临时目录。

## 二、下一棒待办（M6）

顺序按依赖排列（`plan/05` M6 节是权威版）：

1. **脱敏四步**（源码 / 文档 / 历史 / 产物）：
   - 源码：本机绝对路径、用户名、临时目录名——`test_no_machine_absolute_paths_in_tracked_sources`
     已覆盖 `src/ tools/ tests/ plan/` 的 `.py/.md/.json/.toml`，M6 要扩到**全部**跟踪文件与 git 历史；
   - 文档：README 里的证据命令改用相对路径（M1–M5 已做，复核一遍）；
   - 历史：`git log -p` 里不得出现本机路径 / 真实文档名 / 邮箱（当前提交者已是 noreply）；
   - 产物：`plan/media/*.png` 截图里的路径（M4/M5 已用 `C:/demo`，M6 复核像素级）。
2. **CI 首跑**：四矩阵（3.8/3.12 × 两平台）。仓库当前**无远端**，必须先建 GitHub 公开仓库。
   `ci.yml` 的安装行已在 M5 收尾改成 `-e ".[dev,extract,web]"`——**原本只装 `[dev,extract]`**，
   缺 `web` 会让 `tests/test_web.py` 与 `test_no_network.py` 的 HTTP 用例**跳过**
   （`pytest.importorskip("fastapi")`），跳过不是绿。首跑时核对两件事：用例数（本机 452）与
   "没有 skip"，别只看结论行。
3. **发布三件套**（缺一即未达成）：annotated tag 打在 CI 绿的收尾提交上；GitHub Release 附产物
   （源码 zip 或 wheel）**本地留副本 + sha256 留档可复验**；`plan/RELEASE-M6.md` 在 tag
   **之前**写好并进 tag 树（`git cat-file -e <tag>:plan/RELEASE-M6.md` 成立），tag 后的追加**分栏**记录。
4. **干净 clone + 全新 venv 按 README 逐条跑通**，记录**退出码语义**（`report`/`quarantine`/`undo`/
   `reports` 有结果时是 1，不是全 0——照抄"全 0"就是假绿）。
5. **`check_release.py` 无 FAIL**（若仓库内尚无该脚本，按 `plan/05` M6 出口门要求补）。
6. 顺带（非 DoD 但建议）：**B3/B4/B5/B6 对"索引与语料不匹配"给出可读提示**。M5 收尾实测踩过：
   把索引扫在 `C:/demo/docs` 而基准指向仓库的 `data/samples` 时，B3 报"recall 0.0%（0/20）"、
   B4 报"0/27"、B5 报"没有 pair 覆盖 container-only"——数字像真回归，实际是路径错配。
   三个基准各自加一句"没有任何真值组命中索引里的文档，请检查 --samples 是否就是索引扫描的那份语料"
   即可（归属 M2/M3/M4 的收尾，登记在此以免丢失）。

## 三、既定口径清单（K 编号，动了会打挂测试）

M5 新增的编号从 K60 起；K1–K45 原样保留，K46–K59 见 `plan/HANDOFF-M5.md` §三。

| 编号 | 口径 | 位置 | 变了会怎样 |
|---|---|---|---|
| K1–K45 | 见 `plan/HANDOFF-M1.md` / `M2` / `M3` / `M4` §三 | — | 原样有效 |
| K46–K58 | 见 `plan/HANDOFF-M5.md` §三 | — | 原样有效 |
| ~~K59~~ | ~~两个 POST 校验 `confirm` 后返回 501~~ **已于 M5 失效**：501 分支被替换为执行层调用 | `web/server._run_action` | 新口径见 K68；若有人把 501 加回来，`test_the_quarantine_endpoint_moves_the_listed_members_and_undo_puts_them_back` 会红 |
| K60 | `quarantine.py` 是包内**唯一**移动/改名文件的地方；`move_file` 是唯一 OS 边界（含 `\\?\` 前缀与句柄重试），故障注入只走这一个缝 | `quarantine.move_file` / `windows_extended_path` | B6 三条注入与 `test_move_file_retries_a_locked_file` 会红；把 `shutil.move` 写到别处会让"工具只移动文件"这句话失去单一审计点 |
| K61 | **先写 manifest 再移动**：条目先全部 `pending` 落盘，再逐个移动、成功一个改一个 `moved`；中断后 manifest 必须已含全部条目 | `execute_quarantine` 的 `write_state` 顺序 | `test_the_manifest_exists_and_lists_pending_before_the_first_move`、B6 `interrupted-resume` 会红 |
| K62 | **续跑以 manifest 为准，不重新规划**：同一 `--batch-id` 只接受同一 cluster/keep/文件集；`restored` 项不会被再移动（要重做请换新 batch id） | `_existing_batch` / `_assert_same_request` | `test_a_batch_id_is_not_reused_for_a_different_request`、`test_a_restored_batch_is_not_silently_reopened` 会红；改成重新规划会让"文件已搬走"被误判为"原路径缺失" |
| K63 | undo **先整批校验再动第一个文件**（sha256 + size + 原路径占用/同一份内容），任一不符即整批中止、文件不动；通过后逆序移回并复算 sha256 | `execute_undo` 两段式 | `test_undo_aborts_the_whole_batch_when_a_quarantine_copy_changed`、`test_undo_refuses_to_overwrite_a_file_that_appeared_at_the_original_path`、B6 `tampered-copy`/`occupied-original` 会红 |
| K64 | 隔离区 = 成员**共同父目录**下 `.dedupe-quarantine/<batch-id>/`；同批同基名按 `path_norm` 顺序确定性改名 `<stem>__<n><ext>`，**永不覆盖**；跨卷直接拒绝 | `_common_root` / `_free_name` | `test_two_members_with_the_same_name_get_distinct_quarantine_paths` 与 B6 的同名冲突计数会红 |
| K65 | 破坏性输入必须写"动哪些"：`--move` 逐项列举，**空列表是用法错误**；每项都必须是该簇成员，且 size/mtime 与索引一致（扫描后被改过即拒绝） | `plan_batch` | 四个 refusal 测试（组外路径 / 保留项 / 重复项 / 扫描后被改）会红 |
| K66 | `confirm` 是**三道独立关卡**：argparse `required=True`、执行层 `confirm` 检查、HTTP body `"confirm": true` | `build_parser` / `execute_*` / `web.server._confirm` | `test_destructive_commands_refuse_to_run_without_confirm`、`test_the_execution_layer_refuses_without_confirm`、`test_the_quarantine_endpoint_requires_confirmation` 会红 |
| K67 | 批次登记表只存 manifest **路径**，不存条目；报告行由 manifest + `clusters` 表（tier）拼出 | `store.upsert_quarantine_batch` / `quarantine.report_rows` | `test_the_manifest_is_registered_in_the_index_and_that_row_is_only_a_pointer` 会红；存第二份条目就会有两个状态要同步 |
| K68 | Web 返回体与 `quarantine --format json` 是**同一个** `BatchResult.as_dict()`；页面不组装状态、不移动文件 | `web/server._run_action` | `test_the_quarantine_response_is_the_execution_layers_own_payload` 会红 |
| K69 | 退出码：`quarantine`/`undo` 有项没到位 → **1**（不是故障，是"还有待办"）；请求越界 → 2；`reports` 有 `pending` 行 → 1；**3 只表示依赖缺失**（`STAGE_NOT_READY` 已删除，parser 与 `HANDLERS` 两张表由 `selftest` 交叉核对） | `cmd_quarantine` / `cmd_undo` / `cmd_reports` / `HANDLERS` | `tests/test_cli_contract.py` 的退出码与 `selftest` 交叉断言会红 |
| K70 | 隔离目录**复原后仍保留**（manifest 是记录），因此"语料一致"必须断言在 `.dedupe-quarantine` 之外的路径集合上 | `tests/test_quarantine.py` 的 `_documents` / B6 `check_corpus_restored` | 写成"整棵目录树相等"会因遗留的 manifest 而红——那不是缺陷，是记录 |
| K71 | `src/` 与 `tools/` 都不得出现 `os.remove`/`unlink`/`rmtree`/`removedirs`；B6 每个场景**开新目录**而不是删旧目录 | `test_repo_invariants.test_no_delete_calls_in_the_package` + `test_bench_quarantine.test_the_benchmark_never_deletes_anything` | 直接红；"临时目录用 `shutil.rmtree` 清一下"是最容易被顺手写进来的违规 |

## 四、本机环境坑（M5 实测新增）

M0–M4 的坑仍然有效，新增：

1. **`reports --out` 的目录/文件判定**：路径已存在且是目录，**或以分隔符结尾**（`out/`）→ 写入
   `<dir>/quarantine_report.<fmt>`；否则当成文件路径。M5 实测踩过：`--out /c/demo/out`（目录不存在）
   会生成一个名为 `out` 的**文件**。README 与 `--help` 已按此写。
2. **IAB 里 `window.confirm` 会让 `cua.click` 超时 30 s**，但点击其实已经生效、对话框已经弹出：
   `try { await tab.cua.click({x,y}) } catch {}` 之后直接 `const d = await tab.getJsDialog(); if (d?.type === "confirm") await d.accept();`
   再读页面状态即可（M5 实操两次都这样）。坐标仍要"量 + 点在同一次 JS 调用里"，见 `plan/HANDOFF-M5.md` §四.6。
3. **基准的 `--samples` 必须指向索引实际扫描的那份语料**。索引扫在 `C:/demo/docs` 而基准默认指向
   仓库 `data/samples` 时，B3/B4/B5 会报"像真回归"的 FAIL（0/20、0/27、无 pair）。不是缺陷，
   是路径错配；M6 顺带改进见 §二.6。
4. **`quarantine --db` 与 `undo --db` 必须指向同一个索引**：批次登记表在索引里，换库就找不到批次
   （报 `no batch …`，并提示跑 `reports` 看这个库知道哪些批次）。
5. **导出报告不要写进语料目录**：`reports --out` 的输出与 `.dedupe-quarantine/` 都不该出现在
   `scan` 的根目录里（虽然 `config.ignore_dirs` 已忽略 `.dedupe-quarantine`，导出的 csv 会被当成文档候选）。
6. **截图前把索引放中性路径**（`C:/demo/index.db`）：`plan/media/M5-quarantine-page.png` 里出现的
   全部是本机中性路径，没有用户名目录。

## 五、DoD checklist（M6 验收，逐项打勾）

- [x] `plan/RELEASE-M6.md` 在 tag 之前写好并进 tag 树（`git cat-file -e <tag>:plan/RELEASE-M6.md` 成立）
- [x] annotated tag 打在 CI 绿的收尾提交上
- [x] GitHub Release 附产物（源码 zip / wheel 任一），本地留副本 + sha256 留档可复验
- [x] 干净 clone + 全新 venv 按 README 逐条跑通，记录**退出码语义**（非"全 0"）
- [x] CI 四矩阵全绿；`gh run list` 对账（tag push 与 branch push 各一条）
- [x] 脱敏扫描：本机绝对路径、QQ 邮箱、tag tagger 邮箱均 0 命中
- [x] README 的命令、附件名与实物一致（`LICENSE` 真实存在）
- [x] GitHub 仓库 public，description 与 README 定位一致，topics 已补
- [x] `check_release.py` 无 FAIL
- [x] `plan/00` 台账对应行翻 ✅

## 六、关键命令速查

```bash
# 测试（双解释器；web 用例需要 .[web]）
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv312/Scripts/python.exe -m pytest -p no:cacheprovider
PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 ./.venv38/Scripts/python.exe  -m pytest -p no:cacheprovider

# 样本冻结门禁
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/gen_samples.py --check

# 端到端（中性路径；注意 report/quarantine/undo 在"有结果"时退出码是 1，不是 0）
D=C:/demo; rm -rf /c/demo; mkdir -p /c/demo; cp -r data/samples /c/demo/docs
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe scan /c/demo/docs --db "$D/index.db"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe report --db "$D/index.db" --format json --out "$D/report.json"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe quarantine --db "$D/index.db" \
  --cluster <簇 ID> --keep <保留项> --move <要收走的> --move <…> --confirm
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe reports --db "$D/index.db" --format csv --out "$D/out/"
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe undo --db "$D/index.db" --batch <batch-id> --confirm

# 四个基准（--samples 必须指向索引扫描的那份语料）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_recall.py    --db "$D/index.db" --samples /c/demo/docs
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_tiers.py     --db "$D/index.db" --samples /c/demo/docs
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_diff.py      --db "$D/index.db" --samples /c/demo/docs
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_quarantine.py --samples data/samples --work "$D/b6"
# B6 探活（三条都必须报未达标）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_quarantine.py --samples data/samples --inject skip-sha-check
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_quarantine.py --samples data/samples --inject skip-pending
PYTHONUTF8=1 ./.venv312/Scripts/python.exe tools/bench_quarantine.py --samples data/samples --inject copy-instead-of-move

# 复核页（缺 web extra → exit 3）
PYTHONUTF8=1 ./.venv312/Scripts/python.exe -m docdedupe serve --db "$D/index.db" --port 8791
```

**待验证（别在 M6 里假装绿）**：CI 四矩阵、wheel 安装面。仓库仍无远端——这是 M6 的主要工作。

## 七、给 M6 的三条硬提醒

1. **发布纪律**：`plan/RELEASE-M6.md` 必须**先进 tag 树**再打 tag，tag 之后只做**分栏追加**。
   tag 打完再"补写"留档，等于留档不可信（`git cat-file -e` 一查就露）。
2. **CI 必须带 extras**：`pip install -e ".[dev,web,extract]"`。缺 `web` 会让 HTTP 用例**跳过**，
   绿得毫无意义；跳过不是绿，这在 M1 就写进口径。
3. **脱敏扫描要覆盖"全部跟踪文件 + git 历史 + 产物像素"**：`plan/media/*.png` 里的路径文字
   肉眼看不见但确实存在（M4/M5 都用 `C:/demo` 规避），`git log -p` 里的历史无法事后修改，
   所以 M6 开始前先跑一次全量扫描，发现问题就现在改，别等到发布。
