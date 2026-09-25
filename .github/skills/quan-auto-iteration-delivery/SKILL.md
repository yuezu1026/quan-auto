---
name: quan-auto-iteration-delivery
description: "quan-auto 仓库「一轮迭代 / 一个子步收工」的固定动作：拿总判据、读报告而不是重跑、回填会变假的状态陈述、按证据快照抬头钉的东西决定提交粒度、提交后刷新以 git ls-files 为范围的门禁证据、走代理推送并等一次 CI 真跑。Use when the user says 收工 / 交付 / 提交这一轮 / 迭代收尾 / 刷门禁报告 / 推送 / 等 CI / wrap up the iteration, or asks whether an evidence snapshot is still current."
---

# quan-auto 迭代 / 子步收工

## 何时用

- 一轮迭代或一个子步（S1/S2/S3 这类）改完，要收工提交。
- 有人问「现在门禁什么状态」「这份证据还算不算数」「能不能推」。
- 改了产物之后要判断哪些**旁边那句话**同时变成假话。

## 先记住三条

1. **解释器只有一个**：本仓库的 `python` 是 anaconda base（没有 pytest/pandas/psycopg）。
   一律 `.\.venv\Scripts\python.exe -X utf8 ...`。`-X utf8` 不能省：控制台是 cp936，
   stdout 出现 GBK 之外的字符会让子进程**整个死掉**而不是轻微乱码。
2. **看结果读报告，不要为了看结果重跑门禁**。总判据在 `tools/gates-report.txt`
   （UTF-8 无 BOM，必须按 UTF-8 读；PS 5.1 裸 `Get-Content` 会按 GBK 解码，中文显示成乱码
   —— 那是**读法**问题，不是文件坏了）。
3. **门禁数量、测试条数、提交数一律现取**：`tools/run_all_gates.py --list` /
   `python -m pytest -q` / `git log --oneline`。任何不带日期的写死数字都会过期，
   而且没人会在改动时想起它。

## 步骤

1. **拿总判据**
   - 一次调用：`.\.venv\Scripts\python.exe tools/dev.py check`（ASCII 总表，省掉 PS 的编码税）；
     要细节用 `python tools/run_all_gates.py`，只跑一个用 `--gate=NAME`。
   - 退出码：`0` 全绿 / `1` 有门禁失败或 harness 自身无法完成检查 / `2` harness 自测失败。
   - 单个门禁的**必填参数**在 `CONTEXT.md` §4 的表里（少一个 `--extra=` 会虚增基线、掩盖真漂移）。

2. **回填会变假的句子**（这一步最容易漏，且**没有任何门禁兜底**）
   - 常见落点：`CONTEXT.md`、`docs/迭代计划.md`、`docs/开工前缺口清单.md`、
     `.github/copilot-instructions.md`、以及各契约文末的追加小节。
   - 判据是「这句话是否自称当前状态」：**带日期的收工快照**是历史记录，不回改；
     **自称现取的副本**一被重跑就成假话，必须同批回填。
   - 改完中文文案要**回读核对**：门禁只看得见结构，看不见错字/乱码（实测把形近字提交上去，
     全部探测器照样绿）。

3. **决定提交粒度**（看证据快照的**抬头钉了什么**，不是一刀切）
   - 抬头钉了 `commit:` + `dirty:` 的（如 `tools/ci-dryrun-report.txt`）⇒ 它只对它自己那次
     运行成立 ⇒ **必须自成一笔「证据快照」提交**（先提交代码，再跑，再提交快照）。
   - 抬头只有 `SCOPE: ...` 的（如 `tools/gates-report.txt`）⇒ 描述的是工作树内容 ⇒
     与代码同批提交是诚实的。
   - 子步可以走轻量：不刷这两份快照**不会**让任何门禁变红（`R5-EVIDENCE-CURRENT` 只比对
     `tools/verify_backtest_reproducibility.py` 里 `EVIDENCE_PLAN` 那几份
     `.rounds/i1/*.json`）。但不刷新 ≠ 过期：文档里**不得**把它当成「当前最新证据」来引。

4. **提交后重跑以 `git ls-files` 为范围的门禁**
   - `tools/verify_appendix_refs.py` 的文件清单取自 `git ls-files`（拿不到就**拒判**）。
     新增/删除任何被跟踪的文件都会改变它的扫描面，而**提交前那次绿不算数**
     —— untracked 的新文件还没进清单。
   - 所以：**先提交，再重跑，再单独提交刷新后的 `tools/gates-report.txt`**，
     并确认第二次跑逐字节相同（幂等）。这条本身是实测出来的：扫描面走过
     77 → 78 → 81 → 82 → 83 → 88 → 89 → 90。

5. **推送与等 CI**
   - `git -c http.proxy=http://127.0.0.1:7890 push origin main`（默认直接走代理；
     失败了再探测，不要每次先探测）。
   - 提交信息要真 UTF-8 无 BOM：写成文件后用
     `[System.IO.File]::WriteAllText(path, text, UTF8Encoding($false))` + `git commit -F`。
     想核验就读原始字节（`git cat-file commit HEAD`），**不要看终端回显** —— 控制台是 GBK，
     回显乱码与文件编码无关。
   - `gh` 需要 `$env:HTTPS_PROXY='http://127.0.0.1:7890'`（它不读系统代理）。
     结论用退出码：`gh run watch <id> --exit-status`。
   - `.github/workflows/ci.yml` 只有两条 `run:`，**禁止 `|| true`** 吞退出码。

## 边界

- 本 skill 只给「动作与次序」，判据本身在 `docs/开发工作流规范.md`（§3 漂移控制、
  §4 Token 消耗控制、§5 红线清单、§6 一次改动的完整清单）。
- 它**不**证明任何东西已经跑过：跑过的凭证是 `tools/*-report*.txt` 那一批快照，
  各自只对它抬头写的那次运行成立。
