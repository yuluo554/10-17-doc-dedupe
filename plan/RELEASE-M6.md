# RELEASE-M6 · 脱敏发布留档

- 仓库：<https://github.com/yuluo554/10-17-doc-dedupe>（public）
- 版本：v0.1.0
- **本 tag 与收尾提交重合**：`git rev-list -n1 v0.1.0` == `git rev-parse HEAD`，
  `git log --oneline v0.1.0..HEAD` 为空。本文件因此不再分"tag 时点 / tag 之后"两栏——
  能写进 tag 树的事实**全部**写在这里，且工作树里的本文件与 tag 树里的**逐字节相同**
  （`git cat-file -e v0.1.0:plan/RELEASE-M6.md` 成立；门仍会报一条 `DOC_UNCHANGED` WARN，
  原因在门自己身上，见 §2.3）。
- **写不进来的只有两件**：tag 对象自己的 sha，和它自己那次 tag push 的 CI——这两样在
  tag 存在之前不存在，写进 tag 树就是自指（§三 有证明）。它们交给
  `tools/check_release.py` 的 `TAG_ANNOTATED` / `CI_GREEN` / `REMOTE_TAG` 三门去核：
  让机器去核，比在这里抄一遍更难伪造。

```yaml
# release-facts
tag: v0.1.0
artifact: doc_dedupe-0.1.0.tar.gz, doc_dedupe-0.1.0-py3-none-any.whl
```

产物名与 digest 都记在本文件里（§二）。**digest 可以进 tag 树**：本仓的 `plan/` 不在
sdist / wheel 里（本版实测：sdist 55 个条目、wheel 23 个条目，都没有 `plan/`），
把 digest 写进本文件不会改变产物字节——读者拿 tag 树里的这一份记录就能校验产物。
`tools/check_release.py` 的 `read_facts` 从本文件读产物名与 digest。

## 一、tag 时点事实

### 1.1 发布三件套

| 项 | 落在哪 | 可复验方式 |
|---|---|---|
| ① git tag | v0.1.0，annotated，打在 CI 全绿的提交上，且与 `HEAD` 重合 | `git cat-file -t v0.1.0` → `tag`；`gh release view v0.1.0`；CI 是否全绿由 `check_release.py` 的 `CI_GREEN` 核；落点自己核：`git rev-list -n1 v0.1.0` |
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
| 第 5 轮 | `5958987`（M6 首个 tag 位置） | **4/4 成功** | 481 项；M6 首版 `v0.1.0` 就打在这里。**本版把这个 tag 移走**，落到 §1.9 那 6 个修正及其后的发布线改动上（落点自己核：`git rev-list -n1 v0.1.0`），原因见 §1.9 |

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
| 历史 | 全部 ref 可达的 blob 逐个扫描（本版实测 19 个提交 / 258 个去重 blob；旧记录写的 870 / 1100 已复算不出，见 §1.9）| **0 命中**，由 `test_no_machine_absolute_paths_in_git_history` 逐 blob 复核 |
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
| 测试（本机 3.12） | **`484 passed`**，退出码 0（`-o addopts="" -q`，约 79s），0 跳过 |
| 测试（本机 3.8） | **`484 passed`**，退出码 0（`.venv38`，约 106s），0 跳过 |
| 发布门禁 | `python tools/check_release.py .`：复验请自己跑，期望与理由写在 §2.3 |
| 基准数 | B3 20/20、B4 27/27、B5 63/63、B6 58/58 |

`tools/check_release.py` 是本仓库自带的发布门禁，把 M6 DoD 里可机械判定的部分做成硬门：
tag 存在且 annotated、留档在 tag 树内、产物 sha256 与留档一致（本地副本与 Release 附件各验一次）、
tag 提交的 CI 全绿且 branch push 与 tag push 各有一条、`plan/00` 三行台账已翻转、
README 的退出码语义、跟踪文件里没有本机路径、工作树干净、远端与远端 tag 同源。
`--no-gh` 时网络项报 SKIP（不是 PASS）。

### 1.9 本版相对 M6 首个 tag 位置的修正（现已并入本 tag）

M6 首个 tag（`v0.1.0` 原位 `5958987`）之后、本版 tag 之前，`main` 上又落了 6 个提交。
它们不再是"tag 之后的追加"，而是**本 tag 的一部分**，逐条留档：

| 提交 | 改动 | 为什么 |
|---|---|---|
| `d3c32d2` | 追加：tag 指向的提交、它的两条 CI、产物 sha256、Release 与门禁输出 | 把"tag 之后才知道的事实"单独成栏，不混进 tag 树 |
| `dfeaf7f` | README 复验片段改用**真实产物名**（`doc_dedupe-…` 下划线，含真实 sha256）；`check_release.py` 新增 `README_ARTIFACTS` 门 | README 原写的是连字符 `doc-dedupe-0.1.0.tar.gz`，而 setuptools 规范化后是下划线——DoD 要求"README 的附件名与实物一致"，这是当时唯一一处不一致。加了门就不可能再悄悄漂移（另加两条测试：提到未发布的名字必须红、提到真实名字必须绿） |
| `8f08988` | 照抄实跑的门禁输出（新增 `README_ARTIFACTS` 后是 19 PASS） | 让读者能逐条对上，而不是只看"19 PASS"这个总数 |
| `3ab9ced` | `plan/HANDOFF-M6.md` §五 10 项 + `plan/05` M6 节 9 项 DoD 逐项打勾 | 这两处明写"逐项打勾"；打勾要等对应事实都出来（tag、Release、CI 对账、0 FAIL） |
| `004ad27` | `ci.yml` 的 `actions/checkout` 加 **`fetch-depth: 0`** | 补一个**覆盖面**漏洞：默认浅克隆只取 1 个提交，而历史脱敏门是"遍历所有 ref 可达的 blob"。**门还在，覆盖悄悄缩水了**——tip 树有 172 个文件，浅克隆下却只有 1 个提交可达 |
| `8919034` | 历史门加非空跑守卫 `test_the_history_scan_covers_more_than_the_tip_commit` | 让上面那条不可能再退化：浅克隆下**报红**（实测：`git clone --depth 1` 的副本里跑 → `only 1 revision(s) reachable`），完整克隆下通过 |

**这就是本轮把 tag 移过来的原因**：`004ad27` 与 `8919034` 让 CI 里的历史脱敏门真的扫全历史。
没有它们，tag 上那条 CI 的历史门是**浅克隆空验**的，而 README 声称"跟踪文件与 git 历史里
没有本机路径、真实文档名、私人邮箱"——门在，却只覆盖它声称的一小部分。
测试数因此 481 → 483（`README_ARTIFACTS` 两条）→ **484**（守卫一条）。

**一处数字更正（本版实测）**：旧记录里的"本机全量扫描过 870 / 1100 个 blob"
**现在复算不出来**。按门自己的口径（`rev-list --all` 的全部 ref、`ls-tree -r` 去重）本机
是 **19 个提交 / 258 个 blob**；对象库里的 blob 也正好 258 个，没有不可达残留。
旧数字应是历史重写之后、`gc` 剪除之前数到的（那时被抹掉的旧 blob 还在对象库里）。
数字变小不改变那条结论：**门的覆盖面取决于可达提交数**，浅克隆下它必然只剩 1 个提交。

## 二、产物、安装面与可复验的口径

> 这一栏只放**打 tag 之前就能算出**的事实，所以它与 tag 树里的本文件逐字节相同。
> 只有两样东西写不进来（tag 对象自己的 sha、它自己那次 tag push 的 CI），理由与证明见 §三；
> 它们由 `tools/check_release.py` 的 `TAG_ANNOTATED` / `CI_GREEN` / `REMOTE_TAG` 三门机械复核。

### 2.1 产物与 digest

产物构建于本版提交之前的最后一次干净检出。它与 tag 树的差别**只有本文件里的这几行 digest**：
产物的输入是 LICENSE、README.md、pyproject.toml、setup.cfg、`src/`、`tests/`，而 `plan/`、
`tools/`、`.github/` 都不在其中（本版实测：sdist 55 个条目、wheel 23 个条目）。所以 digest 行
不影响产物字节；并且从 tag 树重新构建一次、逐个文件比过 sha256：**23 个 wheel 条目与 48 个
sdist 文件全部逐字节相同**——上传的那一份，就是 tag 树能构建出来的那一份内容。
本地副本在 `release/`（`.gitignore` 里，不入仓），同一份挂在 Release 上：

```yaml
sha256: 4b7b7ba753187088923c87782dd5bcf31be5c13114b63206d870e8aa6eec099e  doc_dedupe-0.1.0.tar.gz
sha256: 9acfd74f92cba9762fe143bf1aa57fa9fe5497c28d0f06a1842bd376d0c2903e  doc_dedupe-0.1.0-py3-none-any.whl
```

**为什么 digest 只写在这里、不抄进 README**：README 会被嵌进 sdist，也会进 wheel 的元数据
（`long_description`）。把 digest 写进 README，就等于让"产物的摘要"出现在产物内部——那个环
只能差一步收敛，产物里嵌的那份 README 永远指着上一批产物。这不是假想：M6 首版把两个 digest
抄在了 README 里（§1.9 的 `dfeaf7f` 那行），本轮重打产物之后它们立刻与 Release 上的实物不符。
本文件不在任何产物里，所以它是唯一能稳定收敛的出处；README 只负责给产物名。

复验（把上面两行原样喂给 `sha256sum -c` 即可；Windows 用 `Get-FileHash`）：

```bash
sha256sum -c <<'SUMS'
4b7b7ba753187088923c87782dd5bcf31be5c13114b63206d870e8aa6eec099e  doc_dedupe-0.1.0.tar.gz
9acfd74f92cba9762fe143bf1aa57fa9fe5497c28d0f06a1842bd376d0c2903e  doc_dedupe-0.1.0-py3-none-any.whl
SUMS
```

Release：<https://github.com/yuluo554/10-17-doc-dedupe/releases/tag/v0.1.0>，
两个附件（`doc_dedupe-0.1.0-py3-none-any.whl` 106,636 B、`doc_dedupe-0.1.0.tar.gz` 193,173 B），
不是 draft、不是 prerelease。`tools/check_release.py` 会用 `gh release download` 把附件真的拉下来
再算一次 sha256 与上表比对——"挂在 Release 上"不是靠肉眼看的。

相对 M6 首版，两个产物的字节**都变了**：本版的 tag 树含 §1.9 那 6 个提交，其中 README 与
`tests/` 的改动都会进 sdist，所以旧附件不是本 tag 树的产物，必须重打。

### 2.2 安装面实测（全部从**本版 wheel**起，全新 venv）

| 装法 | 结果（退出码） |
|---|---|
| 全新 venv `pip install doc_dedupe-0.1.0-py3-none-any.whl` | 0 |
| `doc-dedupe --version` | 0 → `doc-dedupe 0.1.0` |
| `doc-dedupe selftest` | 0 → `selftest OK (doc-dedupe 0.1.0)` |
| `doc-dedupe scan data/samples --db <tmp>/i.db` | 0 |
| `doc-dedupe report --db <tmp>/i.db` | **1**（检出 27 簇 / 84 份文件；既定语义，不是故障） |
| `doc-dedupe reports --db <tmp>/i.db --out <tmp>/out/` | 0 |
| `doc-dedupe serve --db <tmp>/i.db --port 8799`（未装 `web` extra） | **3**，并打印 `pip install -e ".[web]"` |
| 干净 clone + `pip install -e ".[dev]"`（源码态） | 见 §1.4 的退出码表 |
| 干净 clone + `pip install -e ".[web]"` 后 `serve` | `GET /` 200、`GET /api/clusters` 200（27 个簇） |

wheel 里带 Web 静态页（`docdedupe/web/static/index.html`，23 个条目）——这是"装完能 serve"的前提。
`plan/`、`tools/`、`.github/` 都不在 wheel 与 sdist 里（sdist 55 个条目）。

### 2.3 门禁与远端状态怎么复核

本文件**不贴** `check_release.py` 的最终输出，这不是省略，是它**写不进来**：门禁的 `CI_GREEN`
要 tag push 触发的那次 CI，而那次 CI 只有在 tag 推出去之后才存在；把结论抄进 tag 树里的本文件
就是自指（§三）。所以复核方式是跑门禁本身：

```bash
python tools/check_release.py      # 期望 0 FAIL / 2 WARN / 19 PASS，exit 0
```

**为什么剩下 2 条 WARN 都不是待办**：

- **`TAG_COMMIT`**：要 facts 块记一个提交哈希、且必须等于 tag 的目标提交；本文件在 tag 树里，
  记自己的哈希就是自指，数学上无解（§三）。它正是 `CI_GREEN` 存在的理由：tag 是不是打在
  CI 全绿的提交上，由机器在那个提交上核，而不是由留档自述。
- **`DOC_UNCHANGED`**：本文件与 tag 树里的它**其实逐字节相同**（tag 与收尾提交重合、
  工作树干净），门却仍报 WARN——因为门取 tag 侧内容时经过了 `run()` 的 `.strip()`
  （见 `tools/check_release.py` 的 `run()` / `git()`），**文件末尾那个换行被削掉了**，
  而工作树侧是 `read_text()` 的原文，没有削。实测：`tagged == on_disk.strip()` 为真、
  `tagged == on_disk` 为假，差的正是那一个换行。**这条门的 PASS 分支对任何以换行结尾的
  文件都不可达**，它现在只能报 WARN——也就报不出"留档真的被改过"这件事了。
  修法只有一行：取 tag 侧内容时不要 `.strip()`（或两侧对称地保留换行），再补一条
  "相同必须 PASS / 改过必须 WARN" 的两侧测试。
  **本轮没有改门**（纪律禁止把 `check_release.py` 的 WARN 直接改成 PASS），也没有为了
  迁就它去掉文件末尾的换行——这两件事都该由人来定，不该顺手做。

## 三、诚实口径

- **CI 在本机之外跑过，但只跑过这一个矩阵**：ubuntu/windows × 3.8/3.12。
  macOS、3.9–3.11 未在 CI 覆盖，只在 `pyproject` 里声明兼容。
- **本机网络到 `github.com` 不稳定**：`git push` 首次连接超时，`api.github.com`
  与 `codeload.github.com` 始终可用。push 最终走 HTTPS 成功；若日后复验失败，
  先怀疑网络而不是仓库。为此 `check_release.py` 的远端 tag 校验在 `ls-remote`
  失败时回退到 `gh api`。
- **两条 WARN 的真正原因（本轮实测；两条都不是"留档不老实"）**：
  - `DOC_UNCHANGED`：本文件与 tag 树里的自己**逐字节相同**，WARN 出在门自己身上——
    `run()` 对 git 输出做了 `.strip()`，tag 侧被削掉末尾换行、工作树侧没有，两边永远差
    一个换行。细节与修法写在 §2.3；**这条门的 PASS 分支不可达**，它只能当"提醒"看。
  - `TAG_COMMIT`：要 PASS 就得在 tag 树里的本文件写下 tag 自己的提交哈希——一个提交的
    哈希由包含该哈希的树算出，是自指，数学上无解。把哈希写进 facts 块、留在 tag 之后是
    另一条路，代价是 tag 之外多一个提交，也就回到了"tag 落后 HEAD"——**本轮要修的正是它**。
  - 所以在"不改门 + 不制造后置提交"的前提下，本版的最优成绩是 **0 FAIL / 2 WARN**。
    本版选择让 tag 与收尾提交重合：留档在 tag 树里就是终态，tag 之后没有任何改动。
- **产物 digest 的唯一出处是本文件 §2.1，不在 README 里**：README 会被嵌进 sdist 与 wheel
  元数据，把 digest 写进去等于让产物的摘要出现在产物内部，那个环只能差一步收敛。首版 README
  里的两个 digest 在本轮重打产物后立刻失效（已从 README 移走），所以这类漂移不再可能。
- **license 元数据仍是弃用写法，这是刻意的、有实测依据的**：
  `project.license = { text = "MIT" }` 与 `License :: OSI Approved :: MIT License`
  在 setuptools 84 上各报一条 `SetuptoolsDeprecationWarning`（原文含
  **"By 2027-Feb-18, you need to update your project and remove deprecated calls or your
  builds will no longer be supported."**）。**唯一的改法**是换成 SPDX 字符串
  `license = "MIT"`（+ `license-files`），而那条路要求 `setuptools>=77`：
  实测 `setuptools>=75.4.0` 已要求 Python >=3.9，能跑在 3.8 上最新的 `75.3.4` **拒收**
  该写法（`ValueError: invalid pyproject.toml config: project.license`）。
  也就是说"SPDX license"与"`requires-python >=3.8`"**不可兼得**：本版保留 3.8
  （CI 四作业含 3.8，本机 `.venv38` 也跑过全量 484 项），把这条挂到 2027-02-18 的期限上。
- **wheel 安装面**：见 §2.2（全部从本版 wheel 起，全新 venv 实测）。
- **`plan/00` 的三行台账**在本文件同级提交里翻 ✅，因此 tag 树里台账也已是完成态。
- **历史重写了三次**（1.6）。这不改变"当前树"的任何字节（除被抹掉的那两处字面量），
  但意味着 tag 之前的提交哈希与仓库最初几分钟的不同；发布物只在 tag 上。
