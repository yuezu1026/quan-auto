-- ============================================================================
-- 智能量化交易平台 — 风控层建表脚本（PostgreSQL 14+）
-- 对应契约: docs/智能量化交易平台-风控层接口契约文档.md  §3.6
-- 目标: PostgreSQL 14+
-- 说明: 本脚本同时承担「默认阈值种子」职责，种子行由 tools/verify_risk_config.py 强制校验。
--       RATIO 单位一律存 0~1 小数（契约 D3），禁止写 10 表示 10%。
-- 约定: ① 标识符一律不加双引号、全小写。PostgreSQL 会把未加引号的标识符折叠为小写，
--          而加过引号的标识符终身区分大小写 —— 混用是「表不存在」类事故的常见根因。
--       ② 时间列一律 timestamptz(3)，存绝对时刻；展示口径由应用层统一为 Asia/Shanghai。
--       ③ 关键不变量（RATIO 范围、GLOBAL 键、单行表、状态枚举）一律下沉为 CHECK 约束，
--          不依赖应用层自觉：绕过风控进程手工 UPDATE 时的最后一道防线。
--       ④ 种子使用 ON CONFLICT DO NOTHING，重复执行不会覆盖运维已调过的阈值。
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 权威阈值配置表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_rule (
  rule_id      varchar(64)    NOT NULL,
  scope        varchar(16)    NOT NULL,
  scope_key    varchar(64)    NOT NULL,
  threshold    numeric(18,8)  NOT NULL,
  unit         varchar(16)    NOT NULL,
  comparison   varchar(8)     NOT NULL DEFAULT 'LTE',
  enabled      boolean        NOT NULL DEFAULT TRUE,
  version      bigint         NOT NULL,
  updated_by   varchar(64)    NOT NULL,
  updated_at   timestamptz(3) NOT NULL,
  description  varchar(255)   NOT NULL DEFAULT '',
  PRIMARY KEY (rule_id, scope, scope_key),
  CONSTRAINT ck_risk_rule_scope      CHECK (scope IN ('GLOBAL', 'ACCOUNT', 'STRATEGY', 'SYMBOL')),
  CONSTRAINT ck_risk_rule_unit       CHECK (unit IN ('RATIO', 'COUNT', 'ABSOLUTE')),
  CONSTRAINT ck_risk_rule_comparison CHECK (comparison IN ('LTE', 'GTE')),
  -- D3 防线: RATIO 必须落在 (0, 1]。即便有人绕过风控进程手工 UPDATE 也拦得住。
  CONSTRAINT ck_risk_rule_ratio_range CHECK (unit <> 'RATIO' OR (threshold > 0 AND threshold <= 1)),
  -- D2 防线: GLOBAL 是兜底层，其作用域键必须固定为 *。
  CONSTRAINT ck_risk_rule_global_key  CHECK (scope <> 'GLOBAL' OR scope_key = '*'),
  -- 单位口径防线（契约 §2.2）: COUNT / ABSOLUTE 不得为负。
  CONSTRAINT ck_risk_rule_nonneg      CHECK (threshold >= 0),
  -- 单位口径防线（契约 §2.2）: COUNT 必须是整数，"最多 3.5 次" 无意义。
  -- 少了这条，unit=COUNT 时误写 3.5 会一路拖到运行时才表现为行为古怪。
  CONSTRAINT ck_risk_rule_count_int   CHECK (unit <> 'COUNT' OR threshold = trunc(threshold))
);

COMMENT ON TABLE  risk_rule           IS '风控阈值权威配置表（分层覆盖，契约 D2）';
COMMENT ON COLUMN risk_rule.threshold IS '阈值；unit=RATIO 时必须为 0~1 小数';
COMMENT ON COLUMN risk_rule.enabled   IS 'FALSE 表示该层视为不存在，继续向下一层查找';

CREATE INDEX IF NOT EXISTS idx_risk_rule_version ON risk_rule (version);

-- ---------------------------------------------------------------------------
-- 2. 配置版本号（单行，供风控进程低成本轮询，契约 D1）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_config_version (
  id          smallint       NOT NULL DEFAULT 1,
  version     bigint         NOT NULL DEFAULT 0,
  updated_at  timestamptz(3) NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT ck_risk_config_version_singleton CHECK (id = 1)
);

COMMENT ON TABLE  risk_config_version         IS '风控配置版本号（单行）';
COMMENT ON COLUMN risk_config_version.version IS '全局单调递增版本号';

-- ---------------------------------------------------------------------------
-- 3. 变更审计表（只追加，契约 D6/D10）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_rule_audit (
  audit_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_id          varchar(64)    NOT NULL,
  scope            varchar(16)    NOT NULL,
  scope_key        varchar(64)    NOT NULL,
  old_threshold    varchar(64)    NOT NULL DEFAULT '',
  new_threshold    varchar(64)    NOT NULL DEFAULT '',
  change_direction varchar(16)    NOT NULL DEFAULT '',
  applied          boolean        NOT NULL DEFAULT TRUE,
  version          bigint         NOT NULL,
  changed_by       varchar(64)    NOT NULL,
  reason           varchar(255)   NOT NULL DEFAULT '',
  created_at       timestamptz(3) NOT NULL,
  CONSTRAINT ck_risk_rule_audit_direction CHECK (change_direction IN ('', 'TIGHTEN', 'RELAX'))
);

COMMENT ON TABLE  risk_rule_audit            IS '风控阈值变更审计（只追加）';
COMMENT ON COLUMN risk_rule_audit.applied    IS 'TRUE=立即生效，FALSE=落为 PENDING';
COMMENT ON COLUMN risk_rule_audit.changed_by IS '操作人（对应 RuleChangeRequest.operator）';

CREATE INDEX IF NOT EXISTS idx_risk_rule_audit_created ON risk_rule_audit (created_at);
CREATE INDEX IF NOT EXISTS idx_risk_rule_audit_rule    ON risk_rule_audit (rule_id, scope, scope_key);

-- ---------------------------------------------------------------------------
-- 4. 禁止开仓标的名单（名单型规则，不存 risk_rule，契约 3.6.3）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_blacklist (
  symbol   varchar(32)    NOT NULL,
  reason   varchar(255)   NOT NULL DEFAULT '',
  added_by varchar(64)    NOT NULL,
  added_at timestamptz(3) NOT NULL,
  enabled  boolean        NOT NULL DEFAULT TRUE,
  PRIMARY KEY (symbol)
);

COMMENT ON TABLE risk_blacklist IS '禁止开仓标的名单（仅拦截开仓，允许平仓）';

-- ---------------------------------------------------------------------------
-- 5. 拦截留痕（只追加，异步批量写入，契约 3.6.2）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_intercept_log (
  intercept_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  decision_id  varchar(64)    NOT NULL,
  account_id   varchar(64)    NOT NULL DEFAULT '',
  strategy_id  varchar(64)    NOT NULL DEFAULT '',
  symbol       varchar(32)    NOT NULL DEFAULT '',
  rule_id      varchar(64)    NOT NULL,
  rule_scope   varchar(16)    NOT NULL DEFAULT '',
  scope_key    varchar(64)    NOT NULL DEFAULT '',
  rule_version bigint         NOT NULL,
  threshold    numeric(18,8)  NOT NULL,
  observed     numeric(18,8)  NOT NULL,
  severity     varchar(16)    NOT NULL,
  action       varchar(16)    NOT NULL,
  message      varchar(512)   NOT NULL DEFAULT '',
  created_at   timestamptz(3) NOT NULL,
  CONSTRAINT ck_risk_intercept_action CHECK (action IN ('REDUCE', 'REJECT', 'HALT'))
);

COMMENT ON TABLE  risk_intercept_log              IS '风控拦截留痕（只追加）';
COMMENT ON COLUMN risk_intercept_log.rule_version IS '生效规则版本号（契约 D9）';

CREATE INDEX IF NOT EXISTS idx_risk_intercept_created  ON risk_intercept_log (created_at);
CREATE INDEX IF NOT EXISTS idx_risk_intercept_strategy ON risk_intercept_log (strategy_id, created_at);

-- ---------------------------------------------------------------------------
-- 6. 交易决策日志（含 rule_version，契约 D9）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_decision_log (
  decision_id       varchar(64)    NOT NULL,
  request_id        varchar(64)    NOT NULL,
  account_id        varchar(64)    NOT NULL DEFAULT '',
  strategy_id       varchar(64)    NOT NULL DEFAULT '',
  symbol            varchar(32)    NOT NULL DEFAULT '',
  side              varchar(8)     NOT NULL DEFAULT '',
  is_open           boolean        NOT NULL,
  quantity          bigint         NOT NULL,
  adjusted_quantity bigint         NOT NULL,
  action            varchar(16)    NOT NULL,
  run_state         varchar(24)    NOT NULL,
  rule_version      bigint         NOT NULL,
  created_at        timestamptz(3) NOT NULL,
  PRIMARY KEY (decision_id),
  CONSTRAINT ck_risk_decision_action    CHECK (action IN ('PASS', 'REDUCE', 'REJECT', 'HALT')),
  CONSTRAINT ck_risk_decision_run_state CHECK (run_state IN ('NORMAL', 'DEGRADED', 'BREAKER_TRIPPED', 'KILLED'))
);

COMMENT ON TABLE  risk_decision_log              IS '交易决策日志（只追加）';
COMMENT ON COLUMN risk_decision_log.rule_version IS '判定依据的规则版本号（契约 D9）';

CREATE INDEX IF NOT EXISTS idx_risk_decision_created ON risk_decision_log (created_at);
CREATE INDEX IF NOT EXISTS idx_risk_decision_account ON risk_decision_log (account_id, created_at);

-- ---------------------------------------------------------------------------
-- 7. 熔断状态（必须跨重启保留，契约 D7）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_breaker_state (
  breaker_key    varchar(96)    NOT NULL,
  state          varchar(16)    NOT NULL,
  triggered_at   timestamptz(3) NULL,
  trigger_reason varchar(255)   NOT NULL DEFAULT '',
  resumed_at     timestamptz(3) NULL,
  resumed_by     varchar(64)    NOT NULL DEFAULT '',
  resume_reason  varchar(255)   NOT NULL DEFAULT '',
  PRIMARY KEY (breaker_key),
  CONSTRAINT ck_risk_breaker_state CHECK (state IN ('NORMAL', 'TRIPPED'))
);

COMMENT ON TABLE  risk_breaker_state              IS '熔断状态（不得隐式恢复）';
COMMENT ON COLUMN risk_breaker_state.breaker_key  IS 'ACCOUNT:<id> 或 STRATEGY:<id>';
COMMENT ON COLUMN risk_breaker_state.triggered_at IS '触发时间；未触发为 NULL';

-- ---------------------------------------------------------------------------
-- 8. 权益峰值（必须跨重启保留，契约 D8）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_equity_peak (
  peak_key   varchar(96)    NOT NULL,
  peak_value numeric(20,4)  NOT NULL,
  updated_at timestamptz(3) NOT NULL,
  PRIMARY KEY (peak_key)
);

COMMENT ON TABLE  risk_equity_peak             IS '权益峰值（缺失将导致回撤熔断静默失效）';
COMMENT ON COLUMN risk_equity_peak.peak_key    IS 'ACCOUNT:<id> 或 STRATEGY:<id>';
COMMENT ON COLUMN risk_equity_peak.peak_value  IS '历史峰值权益（元）';

-- ---------------------------------------------------------------------------
-- 9. Kill Switch 状态（另存本地文件冗余，契约 3.4.1）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_switch_state (
  id             smallint       NOT NULL DEFAULT 1,
  active         boolean        NOT NULL DEFAULT FALSE,
  activated_at   timestamptz(3) NULL,
  activated_by   varchar(64)    NOT NULL DEFAULT '',
  reason         varchar(255)   NOT NULL DEFAULT '',
  deactivated_at timestamptz(3) NULL,
  deactivated_by varchar(64)    NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  CONSTRAINT ck_risk_switch_state_singleton CHECK (id = 1)
);

COMMENT ON TABLE risk_switch_state IS 'Kill Switch 状态（单行）';

-- ============================================================================
-- 种子数据
-- ============================================================================

-- GLOBAL 层默认阈值（契约 3.1.1）。
-- 关系约束: strategy_drawdown_pct(0.15) 必须严于生命周期退役门槛(0.20)，
--           熔断是「先刹车再诊断」，退役是「确诊后清退」。
-- 幂等: ON CONFLICT DO NOTHING —— 重复执行【不会】覆盖运维已经调过的阈值。
INSERT INTO risk_rule
  (rule_id, scope, scope_key, threshold, unit, comparison, enabled, version, updated_by, updated_at, description)
VALUES
  ('max_position_pct',      'GLOBAL', '*', 0.10, 'RATIO', 'LTE', TRUE, 1, 'system', CURRENT_TIMESTAMP(3), '单票仓位上限: 该标的持仓市值 / 账户总资产'),
  ('max_daily_trades',      'GLOBAL', '*', 3,    'COUNT', 'LTE', TRUE, 1, 'system', CURRENT_TIMESTAMP(3), '单日单票交易次数上限: 同一交易日同一标的买卖笔数合计'),
  ('strategy_drawdown_pct', 'GLOBAL', '*', 0.15, 'RATIO', 'LTE', TRUE, 1, 'system', CURRENT_TIMESTAMP(3), '单策略回撤熔断阈值: (策略权益峰值 - 当前) / 峰值'),
  ('account_drawdown_pct',  'GLOBAL', '*', 0.20, 'RATIO', 'LTE', TRUE, 1, 'system', CURRENT_TIMESTAMP(3), '全账户回撤熔断阈值: (账户总资产峰值 - 当前) / 峰值'),
  ('max_order_amount_pct',  'GLOBAL', '*', 0.10, 'RATIO', 'LTE', TRUE, 1, 'system', CURRENT_TIMESTAMP(3), '单笔委托成交额占比上限: 委托金额 / 标的当日日均成交额'),
  ('max_sector_pct',        'GLOBAL', '*', 0.30, 'RATIO', 'LTE', TRUE, 1, 'system', CURRENT_TIMESTAMP(3), '单行业集中度上限: 单行业持仓市值 / 账户总资产')
ON CONFLICT (rule_id, scope, scope_key) DO NOTHING;

INSERT INTO risk_config_version (id, version, updated_at)
VALUES (1, 1, CURRENT_TIMESTAMP(3))
ON CONFLICT (id) DO NOTHING;

COMMIT;

-- ============================================================================
-- 附: 数据保留（MVP 用单表 + 索引；数据量上来后再改 PostgreSQL 声明式分区）
-- ============================================================================
-- 待启用（需先 REVOKE 或改造为 INSERT 触发），仅作设计记录:
--   ALTER TABLE risk_intercept_log PARTITION BY RANGE (created_at);
--   ALTER TABLE risk_decision_log  PARTITION BY RANGE (created_at);
-- PostgreSQL 的声明式分区必须在建表时指定 PARTITION BY，无法后加；
-- 因此 MVP 先保持单表，保留 1 年由定时任务 DELETE 并 VACUUM 处理。
