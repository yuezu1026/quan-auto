-- =============================================================================
-- 智能量化交易平台 · 数据中心 · PostgreSQL 14+ 建表脚本
-- 契约出处: docs/智能量化交易平台-数据中心接口契约文档.md v1.0
-- 对应 PRD 模块: 模块 1（数据中心）
-- 生成日期: 2026-09-23
--
-- STATUS: written 2026-09-23, EXECUTED 2026-09-23, re-run on a version ladder 2026-09-24.
--
-- PG-VERIFIED-ON: postgres:14 postgres:15 postgres:16 postgres:17
--
--         逐版本证据（每个镜像一次独立运行，各写一份快照，互不覆盖）:
--           postgres:14 -- PostgreSQL 14.24, tools/sql-smoke-report-pg14.txt (42/42 PASS)
--           postgres:15 -- PostgreSQL 15.18, tools/sql-smoke-report-pg15.txt (42/42 PASS)
--           postgres:16 -- PostgreSQL 16.15, tools/sql-smoke-report-pg16.txt (42/42 PASS)
--           postgres:17 -- PostgreSQL 17.11, tools/sql-smoke-report.txt      (42/42 PASS)
--         上面那行 PG-VERIFIED-ON 是本文件关于「在哪些镜像上验过」的**唯一**主张，
--         它的字面内容由 tools/verify_data_center.py 的 C6 与 tools/sql-smoke-report*.txt
--         记录的镜像名**双向**核对（多写一个没跑过的版本会红，少写一个跑过的也会红）。
--
--         能说与不能说：可以说「这两个 smoke 套件在 14.24 / 15.18 / 16.15 / 17.11 四个
--         镜像上都通过」。**不能**说「PostgreSQL 14+ 全都成立」—— 四条大版本之间还有无数
--         小版本与发行版，实测只覆盖这四个 tag。改 CHECK 表达式、或改了 DDL 未迁移时，
--         下面的约束会重新退回「纸面防线」—— 每次改 db/*.sql 都必须重跑
--         tools/run_sql_smoke.py（要另存证据就加 --report=，别覆盖上一轮）。
--
-- 落地约定（与 db/risk_control.sql 一致，全平台单一方言）:
--   1. 标识符一律小写、不加双引号（PostgreSQL 折叠规则）
--   2. 时间列一律 timestamptz(3)，日期列一律 date；禁止无时区的 timestamp
--   3. 价格/金额 numeric(18,4)，比率/权重 numeric(10,6)；禁止 double precision
--   4. 布尔用 boolean；自增用 GENERATED ALWAYS AS IDENTITY
--   5. 关键不变量下沉为 CHECK 约束（见契约 §3.6.1）
--   6. 写入用 INSERT ... ON CONFLICT DO UPDATE（幂等，见契约 D10）
--   7. 分区只能建表时声明；MVP 用单表 + 定期归档
--
-- 执行:
--   $env:PGCLIENTENCODING = 'UTF8'
--   psql -v ON_ERROR_STOP=1 -U postgres -d quan -f db/data_center.sql
--
-- 注意: 本文件注释含中文，故执行前必须设置 PGCLIENTENCODING=UTF8。
--       （db/risk_control.smoke.sql 的注释为纯 ASCII，是另一回事。）
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. dc_data_version — 数据版本（不可变，单行 active）
--    契约 D8: 版本号追加不修改；修正历史数据 = 产生新版本
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_data_version (
    data_version    text            NOT NULL,
    as_of_date      date            NOT NULL,
    created_at      timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    is_active       boolean         NOT NULL DEFAULT FALSE,
    notes           text            NOT NULL DEFAULT '',

    CONSTRAINT pk_dc_data_version PRIMARY KEY (data_version),
    CONSTRAINT ck_dc_version_format
        CHECK (data_version ~ '^v[0-9]{4}\.[0-9]{2}\.[0-9]{2}$')
);

COMMENT ON TABLE  dc_data_version IS '数据版本。不可变，追加不修改；回测结果必须绑定版本号（契约 D8）。';
COMMENT ON COLUMN dc_data_version.data_version IS '版本号，格式 vYYYY.MM.DD。';
COMMENT ON COLUMN dc_data_version.as_of_date   IS '该版本涵盖到哪个交易日。';
COMMENT ON COLUMN dc_data_version.is_active    IS '是否当前生效版本。由部分唯一索引保证全表最多一行为 TRUE。';

-- 「同时只有一行 is_active」是跨行约束，CHECK 做不到，必须用部分唯一索引。
CREATE UNIQUE INDEX IF NOT EXISTS uq_dc_data_version_active
    ON dc_data_version (is_active) WHERE is_active;

-- -----------------------------------------------------------------------------
-- 2. dc_trading_calendar — 交易日历
--    契约 §3.5: 所有「第 N 个交易日」语义必须由本表驱动，禁止自然日加减
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_trading_calendar (
    exchange        text            NOT NULL DEFAULT 'SSE',
    trade_date      date            NOT NULL,
    source          text            NOT NULL DEFAULT '',
    ingested_at     timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_trading_calendar PRIMARY KEY (exchange, trade_date, data_version)
);

COMMENT ON TABLE  dc_trading_calendar IS '交易日历。只存交易日；非交易日即「不在表中」。';
COMMENT ON COLUMN dc_trading_calendar.exchange IS '交易所：SSE / SZSE。两市日历在 MVP 阶段一致。';

CREATE INDEX IF NOT EXISTS idx_dc_calendar_date ON dc_trading_calendar (trade_date);

-- -----------------------------------------------------------------------------
-- 3. dc_symbol — 标的基础信息
--    契约 §四 局限 6: ST 历史为 PIT 的，字段级设计待补（MVP 只存当前值）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_symbol (
    symbol          text            NOT NULL,
    name            text            NOT NULL DEFAULT '',
    exchange        text            NOT NULL DEFAULT '',
    list_date       date,
    delist_date     date,
    is_st           boolean         NOT NULL DEFAULT FALSE,
    available_date  date            NOT NULL,
    source          text            NOT NULL DEFAULT '',
    ingested_at     timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_symbol PRIMARY KEY (symbol, data_version),
    CONSTRAINT ck_dc_symbol_delist_after_list
        CHECK (delist_date IS NULL OR list_date IS NULL OR delist_date > list_date)
);

COMMENT ON TABLE  dc_symbol IS '标的基础信息。契约 §四 局限 6 已记录：ST 历史需要 PIT，当前仅存单一时点值。';
COMMENT ON COLUMN dc_symbol.available_date IS 'PIT 可见日。查询一律 available_date <= as_of_date（契约 D2）。';
COMMENT ON COLUMN dc_symbol.symbol IS '统一格式 600000.SH / 000001.SZ，禁止混用裸代码。';

CREATE INDEX IF NOT EXISTS idx_dc_symbol_exchange ON dc_symbol (exchange);

-- -----------------------------------------------------------------------------
-- 4. dc_daily_bar — 日线行情（不复权）
--    契约 D6: 只存不复权价；复权在读取时按 as_of_date 现算
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_daily_bar (
    symbol          text            NOT NULL,
    trade_date      date            NOT NULL,
    open            numeric(18,4)   NOT NULL,
    high            numeric(18,4)   NOT NULL,
    low             numeric(18,4)   NOT NULL,
    close           numeric(18,4)   NOT NULL,
    volume          numeric(20,4)   NOT NULL,
    amount          numeric(20,4)   NOT NULL,
    source          text            NOT NULL DEFAULT '',
    ingested_at     timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_daily_bar PRIMARY KEY (symbol, trade_date, data_version),
    CONSTRAINT ck_dc_bar_price_positive
        CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CONSTRAINT ck_dc_bar_ohlc_order
        CHECK (high >= low AND high >= open AND high >= close
               AND low <= open AND low <= close),
    CONSTRAINT ck_dc_bar_volume_nonneg
        CHECK (volume >= 0 AND amount >= 0)
);

COMMENT ON TABLE  dc_daily_bar IS '日线行情，不复权原始价。复权因子另存 dc_adjust_factor（契约 D6）。';
COMMENT ON COLUMN dc_daily_bar.volume IS '成交量，单位【股】——不是手。源若给手必须由适配器 ×100（契约 §2.3）。';
COMMENT ON COLUMN dc_daily_bar.amount IS '成交额，单位元。';
COMMENT ON COLUMN dc_daily_bar.trade_date IS '数据所属日期。行情收盘后即可见，故 available_date = trade_date（契约 D4）。';

CREATE INDEX IF NOT EXISTS idx_dc_bar_date ON dc_daily_bar (trade_date);

-- -----------------------------------------------------------------------------
-- 5. dc_adjust_factor — 复权因子
--    契约 D6: 只存原始因子，不存预计算复权价
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_adjust_factor (
    symbol          text            NOT NULL,
    trade_date      date            NOT NULL,
    adjust_factor   numeric(18,8)   NOT NULL,
    source          text            NOT NULL DEFAULT '',
    ingested_at     timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_adjust_factor PRIMARY KEY (symbol, trade_date, data_version),
    CONSTRAINT ck_dc_factor_positive CHECK (adjust_factor > 0)
);

COMMENT ON TABLE  dc_adjust_factor IS '复权因子（累乘）。契约 D6：库中不存预计算复权价，复权是读取时的视图行为。';
COMMENT ON COLUMN dc_adjust_factor.adjust_factor IS '累乘因子，必须 > 0。前复权因子本身含未来信息，故禁止回测使用 QFQ。';

CREATE INDEX IF NOT EXISTS idx_dc_factor_date ON dc_adjust_factor (trade_date);

-- -----------------------------------------------------------------------------
-- 6. dc_financial_report — 财务三表
--    契约 D4: announce_date 是唯一合法的可见性依据
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_financial_report (
    symbol          text            NOT NULL,
    report_type     text            NOT NULL,
    period_end      date            NOT NULL,
    announce_date   date            NOT NULL,
    available_date  date            NOT NULL,
    revenue         numeric(20,4),
    net_profit      numeric(20,4),
    total_assets    numeric(20,4),
    total_equity    numeric(20,4),
    roe             numeric(10,6),
    source          text            NOT NULL DEFAULT '',
    ingested_at     timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_financial_report
        PRIMARY KEY (symbol, report_type, period_end, data_version),
    CONSTRAINT ck_dc_fin_report_type
        CHECK (report_type IN ('BALANCE', 'INCOME', 'CASHFLOW', 'INDICATOR')),
    CONSTRAINT ck_dc_fin_announce_after_period
        CHECK (announce_date >= period_end),
    CONSTRAINT ck_dc_fin_available_ge_announce
        CHECK (available_date >= announce_date),
    CONSTRAINT ck_dc_fin_roe_range
        CHECK (roe IS NULL OR (roe >= -1 AND roe <= 5))
);

COMMENT ON TABLE  dc_financial_report IS '财务数据。契约 D4：查询过滤键是 available_date，不是 period_end。';
COMMENT ON COLUMN dc_financial_report.period_end    IS '报告期截止日 = 数据所属日期。';
COMMENT ON COLUMN dc_financial_report.announce_date IS '披露日期 = 数据可获取日期。源未提供时适配器必须抛异常，禁止用 period_end 冒充（契约 D4）。';
COMMENT ON COLUMN dc_financial_report.available_date IS '可用日期 = announce_date 或其后首个交易日。★ 所有 PIT 查询的过滤键。';
COMMENT ON COLUMN dc_financial_report.roe           IS '净资产收益率 = 净利润/净资产，小数比率——不是百分数。允许负值（亏损公司必然为负），区间 -1~5 由 ck_dc_fin_roe_range 强制；不适用契约 §2.3 的 0~1 通用比率口径。';

CREATE INDEX IF NOT EXISTS idx_dc_fin_pit
    ON dc_financial_report (symbol, report_type, available_date, period_end DESC);

-- -----------------------------------------------------------------------------
-- 7. dc_index_member — 指数成分股 PIT 区间快照
--    契约 D5: 区间表而非逐日快照；effective_to IS NULL = 至今仍在成分内
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_index_member (
    index_code      text            NOT NULL,
    symbol          text            NOT NULL,
    effective_from  date            NOT NULL,
    effective_to    date,
    weight          numeric(10,6),
    source          text            NOT NULL DEFAULT '',
    ingested_at     timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_index_member
        PRIMARY KEY (index_code, symbol, effective_from, data_version),
    CONSTRAINT ck_dc_member_effective_range
        CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT ck_dc_member_weight_range
        CHECK (weight IS NULL OR (weight >= 0 AND weight <= 1))
);

COMMENT ON TABLE  dc_index_member IS '指数成分股 PIT 区间快照。契约 D5：禁止用当前名单回溯历史（幸存者偏差）。';
COMMENT ON COLUMN dc_index_member.effective_to IS '调出生效日（不含）。NULL 表示至今仍在成分内——查询必须显式处理 NULL，否则 a <= x < NULL 恒为 FALSE。';
COMMENT ON COLUMN dc_index_member.weight       IS '权重，0~1 小数——不是百分数。';

CREATE INDEX IF NOT EXISTS idx_dc_member_pit
    ON dc_index_member (index_code, effective_from, effective_to);

-- -----------------------------------------------------------------------------
-- 8. dc_ingest_run — 采集批次日志
--    契约 D10: 幂等与可追溯
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_ingest_run (
    run_id          bigint          GENERATED ALWAYS AS IDENTITY,
    adapter         text            NOT NULL,
    priority        text            NOT NULL DEFAULT 'PRIMARY',
    start_date      date,
    end_date        date,
    symbol_count    integer         NOT NULL DEFAULT 0,
    row_count       integer         NOT NULL DEFAULT 0,
    status          text            NOT NULL,
    error_message   text            NOT NULL DEFAULT '',
    started_at      timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    finished_at     timestamptz(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_ingest_run PRIMARY KEY (run_id),
    CONSTRAINT ck_dc_ingest_status
        CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED')),
    CONSTRAINT ck_dc_ingest_priority
        CHECK (priority IN ('PRIMARY', 'FALLBACK'))
);

COMMENT ON TABLE  dc_ingest_run IS '采集批次日志。契约 D10：失败后重跑整段即可修复，不需人工挑数据。';
COMMENT ON COLUMN dc_ingest_run.status IS 'PARTIAL = 主备源都取不到部分标的数据，批次未完整。';

CREATE INDEX IF NOT EXISTS idx_dc_ingest_started ON dc_ingest_run (started_at DESC);

-- -----------------------------------------------------------------------------
-- 9. dc_quality_issue — 数据质量问题留痕
--    契约 §3.8: 停牌/涨跌停标记是回测撮合正确性的输入，不只是告警
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dc_quality_issue (
    issue_id        bigint          GENERATED ALWAYS AS IDENTITY,
    symbol          text            NOT NULL,
    trade_date      date            NOT NULL,
    flag            text            NOT NULL,
    detail          text            NOT NULL DEFAULT '',
    severity        text            NOT NULL,
    created_at      timestamptz(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    data_version    text            NOT NULL,

    CONSTRAINT pk_dc_quality_issue PRIMARY KEY (issue_id),
    CONSTRAINT uq_dc_quality_issue UNIQUE (symbol, trade_date, flag, data_version),
    CONSTRAINT ck_dc_quality_flag
        CHECK (flag IN ('SUSPENDED', 'LIMIT_UP', 'LIMIT_DOWN', 'ST',
                        'NEW_LISTING', 'DELISTED', 'VOLUME_ANOMALY', 'MISSING')),
    CONSTRAINT ck_dc_quality_severity
        CHECK (severity IN ('CRITICAL', 'WARNING', 'INFO'))
);

COMMENT ON TABLE  dc_quality_issue IS '数据质量问题留痕。契约 §3.8：不阻断入库，但停牌/涨跌停标记必须被撮合引擎消费。';
COMMENT ON COLUMN dc_quality_issue.flag IS 'SUSPENDED=停牌，LIMIT_UP/LIMIT_DOWN=涨跌停，VOLUME_ANOMALY=异常成交量，MISSING=数据缺失。';

CREATE INDEX IF NOT EXISTS idx_dc_quality_date ON dc_quality_issue (trade_date);
CREATE INDEX IF NOT EXISTS idx_dc_quality_symbol ON dc_quality_issue (symbol, trade_date);

-- -----------------------------------------------------------------------------
-- 种子数据：初始数据版本
--   幂等：重复执行不覆盖已有版本（契约 D8 版本不可变）
-- -----------------------------------------------------------------------------
INSERT INTO dc_data_version (data_version, as_of_date, is_active, notes)
VALUES ('v2026.09.23', DATE '2026-09-23', TRUE, '初始版本（占位：覆盖范围由首次采集填充）')
ON CONFLICT (data_version) DO NOTHING;

COMMIT;

-- =============================================================================
-- 后续说明
--
-- 1. 【已执行四个版本，不是「14+ 全部成立」】2026-09-23 首次用 tools/run_sql_smoke.py 在
--    临时容器 postgres:17（PostgreSQL 17.11, Debian）上执行本文件 + db/data_center.smoke.sql，
--    结果 42/42 通过；2026-09-24 用同一脚本（`--pg-image=postgres:NN` + `--report=...`
--    另存）在 postgres:14 / postgres:15 / postgres:16 上各跑一次，也都是 42/42。证据四份：
--      tools/sql-smoke-report-pg14.txt (14.24)
--      tools/sql-smoke-report-pg15.txt (15.18)
--      tools/sql-smoke-report-pg16.txt (16.15)
--      tools/sql-smoke-report.txt      (17.11, 2026-09-23 那份，未被覆盖)
--    每份都含镜像 digest、server/client encoding。
--    **但**「在四个 tag 上通过」仍不等于 DDL 标题里那句「PostgreSQL 14+」——
--    四条大版本之间还有别的 tag 与发行版，从未跑过。本段不得被读成后者。
--
-- 2. 触发测试的**效力**已逐条证伪（2026-09-23，不再是抽样）：`python tools/falsify_smoke.py`
--    把两份 DDL 里的每一条命名 CHECK **单独**放宽（只放松表达式，不删约束）后重跑对应
--    触发测试，要求**正好 1 条样本变红**（`n_fail == 1`）。当前 **31 个案例 / 31 CAUGHT**，
--    覆盖全部 30 条命名 CHECK（本文件的 16 条 + 风控 14 条；ck_risk_rule_ratio_range 上下界各一个案例）。
--    逐案例证据：tools/falsify-report.txt。举例：把本文件的 ck_dc_fin_roe_range 上界 5 放宽成 1000，
--    触发测试立刻由 42/42 变成 41 passed / 1 failed（B10 FAIL  roe = 600 was accepted），说明 B 段不是摆设。
--    但要说清它证明的**是**什么：它证明「B 段会红」，**不是**「每条样本值都真的落在拒绝区间内」——
--    那是靠 `n_fail == 1`（每次只该红一条）这个签名间接兜住的，不是逐值证明。
--    tools/verify_data_center.py 的 C7 只能核对「触发用例还在、16/16 覆盖齐、
--    且每个用例都断言了是哪条约束拒绝的」。
--    B 段每条样本都写了它针对哪个 CHECK、为何挑这个值；改样本时请一并改注释。
--
-- 3. 分区：dc_daily_bar 未来按 trade_date 做 RANGE 分区以加速归档。
--    PostgreSQL 的分区只能在建表时声明，无法对已有表补加，故届时需重建表 + 迁移。
--
-- 4. 数据保留：行情明细按「N 年热数据 + 更早归档到 Parquet」策略执行；
--    归档后仍必须可从权威源重建，且 Parquet 只是缓存（契约 D1）。
-- =============================================================================
