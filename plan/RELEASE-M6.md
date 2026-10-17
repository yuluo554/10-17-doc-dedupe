# RELEASE-M6 · 脱敏发布留档

- 仓库：<https://github.com/yuluo554/10-17-doc-dedupe>（public）
- 版本：v0.1.0
- 本文件分两栏，这是刻意的：**一、tag 时点事实**在打 tag 之前写好、随 tag 进树
  （`git cat-file -e <tag>:plan/RELEASE-M6.md` 应当成立）；**二、tag 后追加**只放
  打 tag 之后才可能算出的事实（产物 sha256、tag 提交自己的 CI、Release 附件）。
  留档若在 tag 之后"补写"，`git cat-file` 一查就露，那样的留档不可信。

```yaml
# release-facts
tag: v0.1.0
artifact: doc_dedupe-0.1.0.tar.gz, doc_dedupe-0.1.0-py3-none-any.whl
```

产物名先在这里定下（tag 时点可知），**digest 不在这一栏**：产物是从 tag 树构建出来的，
把 digest 写进 tag 树会改变这棵树，也就改变了 digest。digest 记录在第二节，
`tools/check_release.py` 两处都读（见该脚本 `read_facts`）。

## 一、tag 时点事实

### 1.1 发布三件套

| 项 | 落在哪 | 可复验方式 |
|---|---|---|
| ① git tag | v0.1.0，annotated，打在 CI 全绿的收尾提交上 | `git cat-file -t v0.1.0` → `tag`；`gh release view v0.1.0` |
| ② Release 附产物 | sdist + wheel 两个附件，本地留 `release/` 副本 | 第二节的 sha256 行 + `sha256sum -c` |
| ③ 发布留档 | 本文件，进 tag 树 | `git cat-file -e v0.1.0:plan/RELEASE-M6.md` |

### 1.2 CI 四矩阵（本仓库的 CI 首跑）

首跑就是**失败**的，四个作业全红，这在 M5 收尾的"待验证"里正是最该先跑的东西。
两个提交合计三轮，全部记录在案：

| 轮次 | 提交 | 结论 | 说明 |
|---|---|---|---|
| 第 1 轮 | `9094506`（已被重写） | **4/4 失败** | 4 个作业各报同一批问题；见 1.3 |
| 第 2 轮 | `caa2043`（已被重写） | **4/4 成功** | 481 项 / 0 跳过 / 0 失败，四作业一致 |
| 第 3 轮 | 收尾提交（含本文件） | 见第二节 | tag 就打在它上面 |

第 2 轮的读法值得写下来：结论行之外还核了两件事——**用例数**与**没有跳过**。
把每个作业的 pytest 进度行抽出来数：`481` 个字符，全部是 `.`，非点字符 `0`
（跳过会显示成 `s`，失败 `F`，错误 `E`）。四个作业都是 481 点 / 0 非点，
与本机 3.8 与 3.12 的 481 项一致。只看"绿"是不够的：

- `ci.yml` 的安装行是 `-e ".[dev,web,extract]"`。缺 `web` 会让 `tests/test_web.py`
  与 `test_no_network.py` 的 HTTP 用例**跳过**（`pytest.importorskip("fastapi")`），
  跳过不是绿。
- 每个作业还跑 `tools/gen_samples.py --check`（语料位级冻结）与 `doc-dedupe selftest`。

### 1.3 首跑抓到的三个平台问题（本机永远暴露不了）

本机只有 Windows，M4/M5 的验收也只在 Windows 上做过，所以这三个问题一直藏着：

1. **门禁把自己的测试文件拦下来了**。`tests/test_check_release.py` 为了让"非 noreply
   身份"那条断言可红，写死了 `user.email=<name>@<provider>` 与 `C:/Users/<name>/notes.md`
   两个字面量——脱敏门在同一个提交里把 worktree 与 git 历史两个门都判红。
   门是对的，写测试的方式不对。
2. **`test_normalize_path_is_absolute_and_uses_forward_slashes` 是 Windows-only 写法**：
   断言 `endswith("/") or ":" in normalized`，而 POSIX 绝对路径 `/home/<user>/...`
   两条都不满足。
3. **socket 审计的非空断言依赖平台**：`test_the_http_surface_sends_nothing_anywhere`
   用 `guard.loopback` 非空来证明"上面的 remote == [] 不是空跑"，理由是"测试客户端走真实
   回环传输"。ubuntu-latest 上 ASGI test client 是进程内的、一个 socket 都不开，
   于是那句话变成了在断言 httpx 的行为，而它在 Windows 上靠别的路径凑巧非空。

处置：1 改为运行时拼接（源文件里不再出现该形状）；2 改为 `startswith("/") or ":" in normalized`；
3 改为在同一 guard 内**主动发起一次真实回环连接**来证明允许路径有效——"非空"从此永远是在
证明这个 guard，而不是证明某个第三方库。

顺带在收尾复跑时抓到第四个问题，不是平台差异而是**概率性红灯**：
`test_the_batch_id_is_sortable_and_unique_enough` 断言"50 次生成的批次 id 两两不同"，
而 `secrets.token_hex(2)` 只有 65,536 种后缀，50 次抽样按生日问题算至少撞一次的
概率约 **1.9%**——也就是说这个测试大约每 54 次跑就会红一次，CI 四作业并行时每次约 7%。
它在本轮收尾复跑时正好红了。撞了也不是数据损坏：同 id 换请求会被 `_existing_batch`
拒绝（K62 有自己的测试）。改法是钉住**后缀的形状**（4 位小写十六进制、非常量、
时间戳前缀可排序），并把这个概率写进测试文档串，而不是让"抽 50 次全不同"去赌运气。

### 1.4 干净 clone + 全新 venv 按 README 逐条跑通（退出码语义）

`git clone` 到中性目录 `C:/demo/clone`（HEAD 与本地 `main` 一致），
`py -3.12 -m venv .venv`，按 README 走。**退出码不是"全 0"**，逐条记录：

| 命令 | 退出码 | 含义 |
|---|---|---|
| `pip install -e ".[dev]"` | 0 | README 快速开始那条 |
| `doc-dedupe --version` | 0 | `doc-dedupe 0.1.0` |
| `doc-dedupe selftest` | 0 | `selftest OK (doc-dedupe 0.1.0)` |
| `doc-dedupe scan data/samples --db index.db` | **0** | 正常完成 |
| `doc-dedupe report --db index.db` | **1** | 检出簇——既定语义，不是故障 |
| `doc-dedupe serve --db index.db --port 8799`（未装 web extra） | **3** | 依赖缺失，并打印安装命令 |
| `doc-dedupe reports --db index.db --out out/` | 0 | 此刻没有 `pending` 行（尚未隔离过） |
| `doc-dedupe quarantine … --confirm`（2 份） | 0 | 两项全部移动到位 |
| `doc-dedupe undo --db index.db --batch <id> --confirm` | 0 | 整批校验通过并移回 |
| `doc-dedupe undo --db index.db --batch ""` | **2** | 用法/越界：这个库里没有该批次 |
| `python tools/gen_samples.py --check` | 0 | `SAMPLES_FROZEN_OK (102 files, seed=20261017)` |
| `pip install -e ".[web]"` 后 `serve`，`GET /` 与 `/api/clusters` | 200 / 200 | 27 个簇 |

隔离往返的字节级证据：把 `data/samples` 里除 `.dedupe-quarantine/` 之外的文件全部
`sha256sum` 再取整体摘要，隔离并复原前后都是
`557bb9e151b70d0ae521d8d5d73e317f61f7ab83360a7fe13f2233b7e3e5b56d`，
与另一次全新 clone 的同一摘要**一致**。隔离目录本身按 K70 保留（manifest 是记录，不是残留）。

### 1.5 脱敏四步

| 步骤 | 做法 | 结果 |
|---|---|---|
| 源码 | 全部跟踪文件按字节扫描（二进制跳过）：本机用户名目录、ProgramData 工作目录、临时目录、QQ 邮箱 | **0 命中**（171 个跟踪文件，114 个按文本扫） |
| 文档 | README 与 plan 的命令一律相对路径或中性路径（`C:/demo`、`data/samples`）；M4/M5 截图用 `C:/demo` | 复核通过 |
| 历史 | 全部 ref 可达的 blob 逐个扫描（870 个 blob / 11 个提交）| **0 命中**（见 1.6 的重写记录）|
| 产物 | `plan/media/*.png` 逐像素看图复核（文字都烧进像素，扫描器看不见） | 只有 `C:/demo/...` 中性路径，无用户名 |

`plan/media/M4-review-page.png` 与 `M5-quarantine-page.png` 里的路径文字分别是
`C:/demo/index.db`、`C:\demo\docs\...`，没有用户名目录。

### 1.6 历史重写记录（三处，都在首次发布之前）

`git log -p` 里的历史无法"事后修改"，所以发现即改。三次都发生在仓库创建后的最初几分钟内，
**当时没有任何 tag、没有任何 Release、没有任何外部克隆**，之后 `--force` 覆盖远端：

| 次序 | 抹掉什么 | 原因 |
|---|---|---|
| 1 | M0 提交 `HANDOFF-M1.md` 里的 `C:/Users/<本机用户名>/.agents/skills/...` | M1 已在工作树修掉，但 blob 留在历史里 |
| 2 | `tests/test_check_release.py` 里的 `C:/Users/<name>/` | 见 1.3 第 1 条 |
| 3 | `tests/test_check_release.py` 里的 `<name>@<provider>` 与其断言里的 `<qq-domain>` | M6 DoD 要求 QQ 邮箱 0 命中，而门当时只认"QQ 号前缀"的形式 |

第 2、3 次之后各重跑一次全量门禁。重写用 `git filter-repo --replace-text`（逐 blob 改写，
不做 checkout，因此 `data/samples` 的样本字节不受影响：样本 blob 摘要前后一致）。

顺带修正的无效口径：`tests/test_repo_invariants.py` 与 `tools/check_release.py` 的 QQ 模式
从 `\d{5,12}@qq[.]com` 收紧为任意 `@qq[.]com`——原模式漏掉了不带 QQ 号的地址形式，
而 DoD 说的是"QQ 邮箱 0 命中"，不是"QQ 号邮箱 0 命中"。

### 1.7 四个基准与探活（同机复跑）

| 基准 | 结果 |
|---|---|
| B3 召回 | `B3 OK`，逐组点名 **20/20 = 100%**（门槛 90%），negative 零误报 |
| B4 分档 | **27/27 = 100%**（20 near + 4 exact + 3 template），跨真值组污染 0 |
| B5 差异 | **63/63**（no-change 12 / container-only 3 / deletion 14 / replacement 28 / crafted 6） |
| B6 隔离回滚 | **58/58 = 100%**（batches 30、items 57、同名冲突确定性改名） |
| 语料冻结 | `SAMPLES_FROZEN_OK (102 files, seed=20261017)` |

探活（每条"修好之前必须红"的证据）：

| 注入 | 期望 | 实测 |
|---|---|---|
| B6 `skip-sha-check` | 报未达标 | 1/58 项 FAIL（篡改副本被当作完好恢复） |
| B6 `skip-pending` | 报未达标 | 57/58 项 FAIL |
| B6 `copy-instead-of-move` | 报未达标 | 57/58 项 FAIL |
| B3 `drop-above` / `drop-random` / `shift-groups` | 报未达标 | G005+G010+G011 漏召回 / G002 漏召回 / 20 组全漏 |

M6 新增的"语料错配提示"也在同一次复跑里验过：把 `--samples` 指到不是索引扫描的那份语料时，
B3/B4/B5 各打印一行 `HINT: no truth-manifest file is present in the index …`，
后面的红字（0/20、0/27、无 container-only pair）因此可读；提示只解释、不改判定，
退出码仍是 1。正常语料下不打印任何提示（`tests/test_bench_hints.py` 两侧都钉住）。

### 1.8 发布门禁与测试

| 项 | 数字 |
|---|---|
| 测试 | 本机 3.8 / 3.12 各 **481** 项全绿，0 跳过；CI 四作业同为 481 项 |
| 发布门禁 | `python tools/check_release.py .`：见第二节（tag 之后才有意义） |
| 基准数 | B3 20/20、B4 27/27、B5 63/63、B6 58/58 |

`tools/check_release.py` 是本仓库自带的发布门禁，把 M6 DoD 里可机械判定的部分做成硬门：
tag 存在且 annotated、留档在 tag 树内、产物 sha256 与留档一致（本地副本与 Release 附件各验一次）、
tag 提交的 CI 全绿且 branch push 与 tag push 各有一条、`plan/00` 三行台账已翻转、
README 的退出码语义、跟踪文件里没有本机路径、工作树干净、远端与远端 tag 同源。
`--no-gh` 时网络项报 SKIP（不是 PASS）。

## 二、tag 后追加

> 本栏在 tag 打出、产物构建完成之后填写。tag 树里的本文件到"## 二"这一行为止，
> 因此 `DOC_UNCHANGED` 门会（正确地）报一条 WARN 而不是 PASS。

（待填：tag 提交 sha 与它的 CI 两条运行、两个产物的 sha256 与复验命令、
Release 入口、`check_release.py` 的最终输出。）

## 三、诚实口径

- **CI 在本机之外跑过，但只跑过这一个矩阵**：ubuntu/windows × 3.8/3.12。
  macOS、3.9–3.11 未在 CI 覆盖，只在 `pyproject` 里声明兼容。
- **本机网络到 `github.com` 不稳定**：`git push` 首次连接超时，`api.github.com`
  与 `codeload.github.com` 始终可用。push 最终走 HTTPS 成功；若日后复验失败，
  先怀疑网络而不是仓库。为此 `check_release.py` 的远端 tag 校验在 `ls-remote`
  失败时回退到 `gh api`。
- **wheel 安装面**：见第二节的安装复验（M5 收尾时这一项还是"未实跑"）。
- **`plan/00` 的三行台账**在本文件同级提交里翻 ✅，因此 tag 树里台账也已是完成态。
- **历史重写了三次**（1.6）。这不改变"当前树"的任何字节（除被抹掉的那两处字面量），
  但意味着 tag 之前的提交哈希与仓库最初几分钟的不同；发布物只在 tag 上。
