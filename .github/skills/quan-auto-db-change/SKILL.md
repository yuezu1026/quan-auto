---
name: quan-auto-db-change
description: "quan-auto 仓库改动 db/*.sql 或 db/*.smoke.sql 时要做的事：在容器 PostgreSQL 上真跑、按镜像另存快照、维护 DDL 头部「验过哪些镜像」的唯一主张、证伪那两份绿、记住改过就作废的边界。Use when the user asks to 改 DDL / 加约束 / 改触发测试 / smoke 跑一下 / 约束有没有生效 / change db/*.sql or the smoke tests."
---

# quan-auto 数据库产物改动

## 何时用

- 改 `db/risk_control.sql` / `db/data_center.sql`（表、约束、索引、种子）。
- 改 `db/*.smoke.sql`（约束触发测试）。
- 有人问「这条约束真的会拒绝吗」「在哪些 PostgreSQL 版本上验过」。

## 本机约束（先看，否则会得出错误结论）

- **没有本地 PostgreSQL 实例**：`psql` 不在 PATH、无服务、无安装目录。
- **容器通道可用**，且是**唯一**能证明「约束真的会拒绝」的手段。
  不要再说「从未执行」，也不要反过来说「约束已验证」——
  正确说法只有一种：**在哪个镜像上、什么时候跑过、结果多少**。

## 步骤

1. **改 DDL / 触测**（`tools/` 只用标准库这条不适用；这里改的是 SQL）。
2. **真跑**：`.\.venv\Scripts\python.exe tools/run_sql_smoke.py`
   - 起一次性容器 → **先验空库** → 跑两份 DDL + 两份触测 → 落报告 → 销毁。
   - 空库守卫不可省：`db/*.sql` 全是 `CREATE TABLE IF NOT EXISTS`，在非空库上会**静默什么都不做**
     ⇒ DDL 与触测会一起假绿。想验证这条守卫有效，就先手工建一张表，必须看到 `GATE FAIL`。
   - 换版本用 `--pg-image=`，另存快照用 `--report=PATH`（**fail-closed**：路径不可写就不写，
     绝不退回默认路径，否则会把上一版快照覆盖掉）。失败留容器排查用 `--keep`。
   - 结论行是 `SMOKE PASS` / `SMOKE FAIL`；同时出现两者、或两者都没有（提取为空）、
     或报了 PASS 但退出码非 0 —— 都判 `GUARD_FAIL`，**绝不返回 0**。
   - 退出码：`0` 两份都 PASS / `1` 有触测失败（**约束没拦住**）/ `2` 空库守卫或脚本自检失败 / `3` 无 docker。
3. **维护 DDL 头部那句唯一主张**：每个 DDL 头部有一行 `-- PG-VERIFIED-ON:` 声明验过哪些版本。
   它由 `tools/verify_data_center.py` 的 C6 与 `tools/verify_risk_config.py` 的 C17 拿
   `tools/sql-smoke-report*.txt` 里的镜像名**双向**核对 —— 多写一个没跑过的版本会红，
   少写一个跑过的也红。所以「跑了新镜像」与「改这一行」必须同批。
4. **证伪那两份绿**：`.\.venv\Scripts\python.exe tools/falsify_smoke.py`
   - 手法是**逐条放宽一条 `CHECK` 的表达式**（不删约束）：删约束会让「约束存在性」检查一起红，
     就分不清是「有牙」还是「文件被破坏了」；放宽只让对应样本变红，期望特征是
     `n_pass = 总数-1, n_fail = 1`。
   - 每条案例前 `DROP SCHEMA public CASCADE` 重建：不清库的话第二条案例改的 DDL 会静默空转
     （表已存在）⇒「变异没送进数据库」会被读成「触测没用」。
   - 退出码：`0` 每个案例都被抓到 / `1` 有案例没被抓到（触测是摆设）/ `2` 自 assert 失败。
   - 它只在 `postgres:17` 上做过；另几个镜像只有冒烟层面的通过。

## 边界（每一条都会让结论作废）

- 绿色本身**不会告诉你有牙**。`SMOKE PASS` 只证明「这次跑对了」，
  「约束真的会拒绝」要靠证伪器那一步。
- **改过任何 `db/*.sql` 后，所有快照即作废**（`tools/sql-smoke-report*.txt`、
  `tools/falsify-report.txt` 都是快照）。
- `docker-compose.dev.yml`（发布 127.0.0.1:55432、带命名卷）**不是证据通道**：
  它跑出来的任何结果都不许被门禁 / DoD / 契约引用。证据只出自 `docker-compose.smoke.yml`。
  在里面重跑 DDL 会静默空转（库非空），改了约束却看不到变化时先 `down -v` 重置。
- `run_sql_smoke.py` **不在** `run_all_gates.py` 里：没 docker 的机器上注册进去要么报环境性 FAIL、
  要么被静默 SKIP 后仍打印绿色。要接进来必须先做到「无 docker ⇒ 明确 SKIPPED 且计入跳过数」。
- 非 `CHECK` 的守门（`NOT NULL`、部分唯一索引）不在证伪范围内，靠 `C7` 核对触发测试覆盖。
