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
| 第 3 轮 | `0a76678`（已被重写） | **4/4 成功** | 同上 |
| 第 4 轮 | `3ae7059`（已被重写） | **4/4 失败** | 本文件自己把抹掉的字面量抄了回来；见 1.3 末段 |
| 第 5 轮 | 收尾提交（含本文件） | 见第二节 | tag 就打在它上面 |

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
   身份"那条断言可红，写死了一个本机用户目录形状的路径与一个 QQ 邮箱地址
   （两处字面量在下表按形状描述，本文件不再原样复写它们——复写就等于又把它们带回来）。
   脱敏门在同一个提交里把 worktree 与 git 历史两个门都判红。门是对的，写测试的方式不对。
2. **`test_normalize_path_is_absolute_and_uses_forward_slashes` 是 Windows-only 写法**：
   断言 `endswith("/") or ":" in normalized`，而 POSIX 绝对路径（形如 `/home/<user>/...`）
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
| 1 | M0 提交 `HANDOFF-M1.md` 里的本机用户目录路径（`C:/Users/<name>/.agents/skills/...`） | M1 已在工作树修掉，但 blob 留在历史里 |
| 2 | `tests/test_check_release.py` 里一个本机用户目录形状的路径（`C:/Users/<name>/...`） | 见 1.3 第 1 条 |
| 3 | `tests/test_check_release.py` 里一个 QQ 邮箱地址（`<name>@<provider>` 形式）及其断言中的同一域名 | M6 DoD 要求 QQ 邮箱 0 命中，而门当时只认"QQ 号前缀"的形式 |
| 4 | 本文件（`plan/RELEASE-M6.md`）在 1.3/1.6 两节里原样复写的上述字面量 | 留档把抹掉的东西抄了一遍，等于带回来；已改为按形状描述 |

第 2、3 次之后各重跑一次全量门禁。重写用 `git filter-repo --replace-text`（逐 blob 改写，
不做 checkout，因此 `data/samples` 的样本字节不受影响：样本 blob 摘要前后一致）。

顺带修正的无效口径：`tests/test_repo_invariants.py` 与 `tools/check_release.py` 的 QQ 模式
从"必须带 QQ 号前缀"（`\d{5,12}` 加在域名前）收紧为任意 QQ 域名——原模式漏掉了不带
QQ 号的地址形式，而 DoD 说的是"QQ 邮箱 0 命中"，不是"QQ 号邮箱 0 命中"。

**这份留档自己也被门拦过一次**（第 4 轮 CI，四作业同时红）：1.3 与 1.6 两节当初把抹掉的
字面量原样写了出来，于是 `test_no_machine_absolute_paths_in_tracked_sources` 与
`..._in_git_history` 双双报红。正确的写法是**按形状描述**（`C:/Users/<name>/...`、
`<name>@<provider>`），而不是把抹掉的东西再抄一遍——抄一遍就等于把它们带回来。
这也是"门禁必须能红"的又一次正面例子：它对留档文档一视同仁。

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

### 2.1 tag 指向哪一次提交、那次提交有哪些 CI

| 项 | 值 |
|---|---|
| tag 对象 | `8ddf3ec7c09780f6aa1e99b5cbd3ae4f7838d7e4`（`git cat-file -t` → `tag`，annotated） |
| tag 目标提交 | `5958987fa1263e90739707eaffede4ea5958c7ab` |
| branch push 的运行 | [39040282695](https://github.com/yuluo554/10-17-doc-dedupe/actions/runs/39040282695) `event=push branch=main` **success**，4/4 作业 |
| tag push 的运行 | [39040585083](https://github.com/yuluo554/10-17-doc-dedupe/actions/runs/39040585083) `event=push branch=v0.1.0` **success**，4/4 作业 |

两条运行打在**同一个提交**上，这正是 DoD 要的"tag push 与 branch push 各一条"：
`gh run list --commit 5958987` 只返回两条，`gh run view` 显示 `headSha` 相同。
第二次（tag push）的四个作业同样是 481 点 / 0 非点。

### 2.2 产物与 digest

产物由 tag 树构建（构建前 `git status` 干净且 `HEAD == v0.1.0^{commit}`），
本地副本在 `release/`（`.gitignore` 里，不入仓），同一份挂在 Release 上：

```yaml
sha256: 092aaa5971ec4aff9568251ee10b03d2a4f7eb061b5d7a9333776591e6b833c6  doc_dedupe-0.1.0.tar.gz
sha256: 80ce98d3067388b891e444f1bd13d3204c8c8b7693737fe0247ee9b3b13e6270  doc_dedupe-0.1.0-py3-none-any.whl
```

复验（把上面两行原样喂给 `sha256sum -c` 即可；Windows 用 `Get-FileHash`）：

```bash
sha256sum -c <<'SUMS'
092aaa5971ec4aff9568251ee10b03d2a4f7eb061b5d7a9333776591e6b833c6  doc_dedupe-0.1.0.tar.gz
80ce98d3067388b891e444f1bd13d3204c8c8b7693737fe0247ee9b3b13e6270  doc_dedupe-0.1.0-py3-none-any.whl
SUMS
```

Release：<https://github.com/yuluo554/10-17-doc-dedupe/releases/tag/v0.1.0>，
两个附件（`doc_dedupe-0.1.0-py3-none-any.whl` 106,227 B、`doc_dedupe-0.1.0.tar.gz` 191,615 B），
不是 draft、不是 prerelease。`tools/check_release.py` 会用 `gh release download` 把附件真的拉下来
再算一次 sha256，与上表比对——"挂在 Release 上"不是靠肉眼看的。

### 2.3 安装面实测（M5 收尾时这一项还是"未实跑"）

| 装法 | 结果 |
|---|---|
| 全新 venv 装 **wheel** | 装得上；`doc-dedupe --version` → `0.1.0`；`selftest` → `selftest OK` |
| 从 wheel 跑全流程（用仓库语料作输入） | `scan` 0 / `report` 1（27 个簇，exact+near+template 三档齐全）/ `reports` 0 |
| 干净 clone + `pip install -e ".[dev]"` | 见 §1.4 的退出码表 |
| 干净 clone + `pip install -e ".[web]"` 后 `serve` | `GET /` 200、`GET /api/clusters` 200（27 个簇） |

### 2.4 发布门禁的最终输出

`python tools/check_release.py .`（联网，含 gh 校验）：

```
== release gate: 10-17-doc-dedupe (tag=v0.1.0) ==
[PASS] TAG_ANNOTATED        tag v0.1.0 is annotated
[WARN] TAG_COMMIT           no commit recorded in the facts block; the tag target is 5958987fa126
[PASS] TAGGER_EMAIL         tagger email is noreply
[PASS] DOC_IN_TAG           plan/RELEASE-M6.md is inside the v0.1.0 tree
[WARN] DOC_UNCHANGED        the release document changed after the tag; the post-tag additions
                            must be a separate, clearly marked section
[PASS] ARTIFACT_DIGEST      2 artifact digest(s) recorded after the tag
[PASS] ARTIFACT_LOCAL       release/doc_dedupe-0.1.0.tar.gz matches the recorded sha256
[PASS] ARTIFACT_LOCAL       release/doc_dedupe-0.1.0-py3-none-any.whl matches the recorded sha256
[PASS] ARTIFACT_REMOTE      downloaded doc_dedupe-0.1.0.tar.gz from yuluo554/10-17-doc-dedupe; sha256 matches
[PASS] ARTIFACT_REMOTE      downloaded doc_dedupe-0.1.0-py3-none-any.whl from yuluo554/10-17-doc-dedupe; sha256 matches
[PASS] RELEASE_ASSETS       2 asset(s): doc_dedupe-0.1.0-py3-none-any.whl, doc_dedupe-0.1.0.tar.gz
[PASS] CI_GREEN             2 successful run(s) on 5958987fa126, covering main, v0.1.0
[PASS] PLAN00               all 3 M6 status row(s) are flipped
[PASS] README_ARTIFACTS     every artifact the README names is shipped: doc_dedupe-0.1.0-py3-none-any.whl, doc_dedupe-0.1.0.tar.gz
[PASS] README_LICENSE       LICENSE exists and the README names it
[PASS] README_EXIT_CODES    report/quarantine/undo/reports each document exit code 1 on their command line
[PASS] LEAKS                no machine-specific path or QQ mailbox in 117 scanned file(s)
[PASS] IDENTITIES           1 unique commit identity/identities, all noreply
[PASS] CLEAN_TREE           the working tree is clean
[PASS] REMOTE               remote configured: origin
[PASS] REMOTE_TAG           the remote tag matches the local one (5958987fa126)

0 FAIL / 2 WARN / 19 PASS
```

这一段是**照抄实跑输出**（`README_ARTIFACTS` 那条是 §2.5 新增的门）。
两条 WARN 都是设计如此，不是待办：`TAG_COMMIT` 因为没有把提交哈希写进 tag 树里的
facts 块（写进去会改变那棵树），`DOC_UNCHANGED` 因为本节就是 tag 后的追加。
"0 FAIL"是硬要求，"0 WARN"不是。

### 2.5 tag 之后改了什么（按 §二 的规矩分栏记录）

tag 树里的仓库状态与本文件 §一 所写一致；下列改动只出现在 tag 之后，不影响 tag 内容：

| 改动 | 为什么 |
|---|---|
| README 的复验片段改用**真实产物名**（`doc_dedupe-…` 下划线，含两个真实 sha256） | README 原先写的是连字符 `doc-dedupe-0.1.0.tar.gz`——setuptools 规范化后实际是下划线。DoD 要求"README 的附件名与实物一致"，这是唯一一处不一致 |
| `check_release.py` 新增 `README_ARTIFACTS` 门 | 让上面那类漂移不可能再悄悄发生：README 里出现的任何 `doc[_-]dedupe-*.tar.gz/whl` 必须真的在 Release 里；加了两条测试（提到未发布的名字必须红、提到真实名字必须绿） |
| 测试数 481 → **483** | 就是上面那两条测试。tag 提交（`5958987`）仍是 481 项；这两个数字各自都说的是自己那次提交 |
| `plan/HANDOFF-M6.md` §五的 10 项与 `plan/05` M6 节的 9 项 DoD 复选框打勾 | 这两处都写着"逐项打勾"；打勾要等对应事实都出来（tag、Release、CI 对账、0 FAIL），所以只能在 tag 之后做，tag 树里仍是空框 |
| `ci.yml` 的 `actions/checkout` 加 `fetch-depth: 0`，并加一条非空跑守卫 | 补一个**覆盖面**漏洞：默认浅克隆只取 1 个提交，而历史脱敏门是"遍历所有 ref 可达的 blob"。本机全量扫描过 1100 个 blob，CI 里却只扫到 tip 的 172 个——门还在，覆盖悄悄缩水了。新守卫 `test_the_history_scan_covers_more_than_the_tip_commit` 在浅克隆下**报红**（实测：`git clone --depth 1` 的副本里跑 → `only 1 revision(s) reachable`），在完整克隆下通过 |
| 测试数 483 → **484** | 就是上面那条守卫 |

tag 之后 `main` 的提交因此与 tag 指向的提交不同，这是 §二 存在的理由——
`DOC_UNCHANGED` 报 WARN 正是它在如实说明这件事。

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
