-- ============================================================================
-- risk-control DDL smoke test  (PostgreSQL 14+)
--
--   NOTE: comments in this file are intentionally ASCII-only. psql derives the
--   client encoding from the Windows console code page, and a UTF-8 file with CJK
--   comments can blow up with `invalid byte sequence for encoding "GBK"` before a
--   single statement runs. Do not "helpfully" translate these comments.
--   (The DDL itself does contain Chinese comments and does need
--    PGCLIENTENCODING=UTF8 -- see the run instructions below.)
--
-- WHY THIS FILE EXISTS
--   tools/verify_risk_config.py is a STATIC gate: it can prove a CHECK constraint
--   is still written in the DDL, but it cannot prove the constraint actually
--   rejects anything. A constraint whose expression is subtly wrong -- `<= 1`
--   typo'd to `< 1`, or a trailing `OR TRUE`, or an enum list missing a member --
--   passes every regex while enforcing nothing. This file is the TRIGGER TEST:
--   every database-level guard is fed an input that MUST be rejected, plus
--   boundary inputs that MUST be accepted (to catch constraints that are
--   over-tight and reject legal values).
--
--   Together: the static gate protects the constraint inventory, this test
--   protects the constraint semantics. Neither one alone is sufficient.
--
-- HOW TO RUN  (nothing is persisted -- the whole thing rolls back)
--   Windows PowerShell:
--     $env:PGCLIENTENCODING = 'UTF8'
--     psql -v ON_ERROR_STOP=1 -U postgres -d quan -f db/risk_control.sql
--     psql -v ON_ERROR_STOP=1 -U postgres -d quan -f db/risk_control.smoke.sql
--     Remove-Item Env:PGCLIENTENCODING      # do not leave it set globally
--
--   Result:
--     exit code 0 + "SMOKE PASS: all N ..." -> every guard is effective
--     exit code 3 + "SMOKE FAIL: ..."       -> at least one guard is a paper guard
--
-- STATUS: written 2026-09-23, EXECUTED 2026-09-23 -- SMOKE PASS, 23 passed / 0 failed
--   on postgres:17 (PostgreSQL 17.11, Debian) via tools/run_sql_smoke.py. Evidence,
--   including the image digest: tools/sql-smoke-report.txt.
--
--   What that green does NOT cover:
--     * only that one image has ever been run -- "PostgreSQL 14+" is untested;
--     * B2..B10 and B12..B19 assert the SQLSTATE only. (B3, B5 and B9 are the
--       must-be-ACCEPTED boundary samples and carry no exception branch at all; B11
--       asserts not_null_violation, not a named CHECK.) For each refusing sample
--       exactly one constraint CAN fire -- hand-verified and enforced by
--       tools/verify_risk_config.py -- but those samples do not prove WHICH
--       constraint refused the row. Only B1 asserts the constraint name. This
--       asymmetry is deliberate and is not coverage.
--     * the 3 non-CHECK guards (1 primary key that the singleton samples ride on, and
--       the not-null on risk_rule.threshold) are NOT falsified by tools/falsify_smoke.py;
--       that tool covers the 14 named CHECK constraints only.
--
--   Proof that the B half has teeth (a green that is never falsified is not evidence):
--   tools/falsify_smoke.py widens one constraint at a time and re-runs this file. It
--   currently runs 31 cases over both DDLs, covering all 30 named CHECK constraints
--   (ck_risk_rule_ratio_range appears twice, once per bound). Each case must turn
--   EXACTLY one sample red, which is the signature that the mutation was narrow.
--   Widening ck_risk_rule_ratio_range's upper bound, for instance, makes this file
--   report 22 passed / 1 failed (B1 FAIL  RATIO=10 was accepted) instead of 23/0.
-- ============================================================================

BEGIN;

DO $smoke$
DECLARE
  n_pass    int := 0;
  n_fail    int := 0;
  v_missing text;
  v_cnt     int;
  v_thr     numeric;
  v_cname   text;
BEGIN

  -- =========================================================================
  -- A. schema inventory and seed integrity
  -- =========================================================================

  -- A1: all nine tables must be creatable and present in the search_path.
  SELECT count(*) INTO v_cnt
  FROM unnest(ARRAY[
         'risk_rule', 'risk_config_version', 'risk_rule_audit', 'risk_blacklist',
         'risk_intercept_log', 'risk_decision_log', 'risk_breaker_state',
         'risk_equity_peak', 'risk_switch_state']) AS t(name)
  WHERE to_regclass(t.name) IS NULL;
  IF v_cnt = 0 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A1 PASS  all 9 tables exist';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A1 FAIL  % table(s) missing from the schema', v_cnt;
  END IF;

  -- A2: the constraint inventory as the database actually sees it. This is the
  -- dynamic twin of checker C14: C14 reads the DDL text, A2 reads pg_constraint,
  -- so an edit that never got applied (stale database) is caught here.
  SELECT string_agg(e.tbl || '.' || e.cname, ', ') INTO v_missing
  FROM (VALUES
      ('risk_rule',           'ck_risk_rule_scope'),
      ('risk_rule',           'ck_risk_rule_unit'),
      ('risk_rule',           'ck_risk_rule_comparison'),
      ('risk_rule',           'ck_risk_rule_ratio_range'),
      ('risk_rule',           'ck_risk_rule_global_key'),
      ('risk_rule',           'ck_risk_rule_nonneg'),
      ('risk_rule',           'ck_risk_rule_count_int'),
      ('risk_config_version', 'ck_risk_config_version_singleton'),
      ('risk_rule_audit',     'ck_risk_rule_audit_direction'),
      ('risk_intercept_log',  'ck_risk_intercept_action'),
      ('risk_decision_log',   'ck_risk_decision_action'),
      ('risk_decision_log',   'ck_risk_decision_run_state'),
      ('risk_breaker_state',  'ck_risk_breaker_state'),
      ('risk_switch_state',   'ck_risk_switch_state_singleton')
  ) AS e(tbl, cname)
  WHERE NOT EXISTS (
      SELECT 1
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      WHERE c.conname = e.cname
        AND c.contype = 'c'
        AND t.relname = e.tbl
  );
  IF v_missing IS NULL THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A2 PASS  all 14 required CHECK constraints present';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A2 FAIL  missing CHECK constraint(s): %', v_missing;
  END IF;

  -- A3: the six GLOBAL threshold rows, compared numerically (not textually).
  -- An ON CONFLICT DO NOTHING seed that never actually inserted anything would
  -- leave the table empty and every downstream guard silently unenforced.
  SELECT string_agg(e.rule_id, ', ') INTO v_missing
  FROM (VALUES
      ('max_position_pct',      0.10, 'RATIO'),
      ('max_daily_trades',      3,    'COUNT'),
      ('strategy_drawdown_pct', 0.15, 'RATIO'),
      ('account_drawdown_pct',  0.20, 'RATIO'),
      ('max_order_amount_pct',  0.10, 'RATIO'),
      ('max_sector_pct',        0.30, 'RATIO')
  ) AS e(rule_id, want_threshold, want_unit)
  WHERE NOT EXISTS (
      SELECT 1 FROM risk_rule r
      WHERE r.rule_id = e.rule_id
        AND r.scope = 'GLOBAL'
        AND r.scope_key = '*'
        AND r.threshold = e.want_threshold
        AND r.unit = e.want_unit
        AND r.enabled
  );
  SELECT count(*) INTO v_cnt FROM risk_rule WHERE scope = 'GLOBAL';
  IF v_missing IS NULL AND v_cnt = 6 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A3 PASS  6 GLOBAL seed rows with the expected thresholds';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A3 FAIL  wrong/unenabled/missing rows: [%], GLOBAL row count = %',
                 coalesce(v_missing, 'none'), v_cnt;
  END IF;

  -- A4: idempotency. Re-running the seed must NOT overwrite an operator-tuned
  -- value. If DO NOTHING were ever "upgraded" to DO UPDATE, this catches it.
  INSERT INTO risk_rule
    (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
     version, updated_by, updated_at, description)
  VALUES
    ('max_position_pct', 'GLOBAL', '*', 0.99, 'RATIO', 'LTE', TRUE,
     99, 'smoke', CURRENT_TIMESTAMP(3), 'must not overwrite')
  ON CONFLICT (rule_id, scope, scope_key) DO NOTHING;

  SELECT count(*), max(threshold) INTO v_cnt, v_thr
  FROM risk_rule
  WHERE rule_id = 'max_position_pct' AND scope = 'GLOBAL' AND scope_key = '*';
  IF v_cnt = 1 AND v_thr = 0.10 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A4 PASS  seed is idempotent (0.10 survived a 0.99 re-seed)';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A4 FAIL  re-seed overwrote the tuned value: count=%, threshold=%',
                 v_cnt, v_thr;
  END IF;

  -- =========================================================================
  -- B. constraint behaviour -- every guard gets an input that must be refused
  -- =========================================================================

  -- B1: D3, the 100x accident. Store 10 meaning "10%" and you must be stopped.
  -- This one also asserts WHICH constraint refused the row. Asserting only the
  -- SQLSTATE class would let any other constraint satisfy the test -- e.g. a
  -- well-meaning catch-all added later would keep B1 green while the actual D3
  -- guard rotted away. The remaining B-tests assert the SQLSTATE only; each was
  -- written so that exactly one constraint *can* fire (checked by hand against the
  -- other five constraints on risk_rule), so they cannot be satisfied by the wrong
  -- guard. B1 now prints PASS (2026-09-23), so this construct IS proven and could be
  -- rolled out to the other samples -- that rollout is a known, deliberately open
  -- item, not an oversight. Until it happens, "which constraint bit?" is asserted
  -- exactly once per file and the SQLSTATE-only samples cannot be told apart from a
  -- catch-all.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b1', 'GLOBAL', '*', 10, 'RATIO', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B1 FAIL  RATIO=10 was accepted (D3 guard ineffective)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_risk_rule_ratio_range' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B1 PASS  RATIO=10 rejected by ck_risk_rule_ratio_range';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B1 FAIL  RATIO=10 rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B2: RATIO lower bound is exclusive -- 0 would mean "no position ever".
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b2', 'GLOBAL', '*', 0, 'RATIO', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B2 FAIL  RATIO=0 was accepted (lower bound must be exclusive)';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B2 PASS  RATIO=0 rejected';
  END;

  -- B3: boundary that must be ACCEPTED. A guard written `threshold < 1` instead
  -- of `<= 1` would pass every static check and reject the legal maximum.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b3', 'ACCOUNT', 'SMOKE-A', 1, 'RATIO', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_pass := n_pass + 1;
    RAISE NOTICE 'B3 PASS  RATIO=1 accepted (upper bound is inclusive)';
  EXCEPTION WHEN others THEN
    n_fail := n_fail + 1;
    RAISE NOTICE 'B3 FAIL  RATIO=1 rejected -- the legal maximum is refused (%)',
                 SQLERRM;
  END;

  -- B4: D2. GLOBAL is the fallback layer; its scope_key is pinned to '*'.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b4', 'GLOBAL', 'SMOKE-A', 0.10, 'RATIO', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B4 FAIL  GLOBAL row with scope_key=''SMOKE-A'' was accepted (D2)';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B4 PASS  GLOBAL row with a non-* scope_key rejected (D2)';
  END;

  -- B5: the same non-* scope_key is legal on a real scope, otherwise B4 would be
  -- tautological (rejecting everything rather than rejecting the wrong layer).
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b5', 'ACCOUNT', 'SMOKE-A', 0.10, 'RATIO', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_pass := n_pass + 1;
    RAISE NOTICE 'B5 PASS  ACCOUNT row with a real scope_key accepted';
  EXCEPTION WHEN others THEN
    n_fail := n_fail + 1;
    RAISE NOTICE 'B5 FAIL  ACCOUNT row rejected (%) -- B4 is therefore tautological',
                 SQLERRM;
  END;

  -- B6: unit enum.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b6', 'GLOBAL', '*', 0.10, 'PERCENT', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B6 FAIL  unit=''PERCENT'' was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B6 PASS  unit=''PERCENT'' rejected';
  END;

  -- B7: comparison enum.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b7', 'GLOBAL', '*', 0.10, 'RATIO', 'LT', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B7 FAIL  comparison=''LT'' was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B7 PASS  comparison=''LT'' rejected';
  END;

  -- B8: contract 2.2 says COUNT is a non-negative INTEGER. numeric(18,8) alone
  -- would happily take 3.5, and "at most 3.5 trades" is nonsense.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b8', 'ACCOUNT', 'SMOKE-A', 3.5, 'COUNT', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B8 FAIL  COUNT=3.5 was accepted (must be integral)';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B8 PASS  COUNT=3.5 rejected';
  END;

  -- B9: integrality must not reject legal integers.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b9', 'ACCOUNT', 'SMOKE-A', 3, 'COUNT', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_pass := n_pass + 1;
    RAISE NOTICE 'B9 PASS  COUNT=3 accepted';
  EXCEPTION WHEN others THEN
    n_fail := n_fail + 1;
    RAISE NOTICE 'B9 FAIL  COUNT=3 rejected -- legal integer refused (%)', SQLERRM;
  END;

  -- B10: nothing may be negative.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b10', 'SYMBOL', '600000.SH', -1, 'ABSOLUTE', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B10 FAIL threshold=-1 was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B10 PASS threshold=-1 rejected';
  END;

  -- B11: NULL thresholds. A NULL read back as 0.0 silently rejects every order.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b11', 'GLOBAL', '*', NULL, 'RATIO', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B11 FAIL threshold=NULL was accepted';
  EXCEPTION WHEN not_null_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B11 PASS threshold=NULL rejected';
  END;

  -- B12: intercept log action enum.
  BEGIN
    INSERT INTO risk_intercept_log
      (decision_id, rule_id, rule_version, threshold, observed, severity, action, created_at)
    VALUES ('__smoke_b12', 'max_position_pct', 1, 0.10, 0.20, 'WARN', 'SELL',
            CURRENT_TIMESTAMP(3));
    n_fail := n_fail + 1;
    RAISE NOTICE 'B12 FAIL intercept action=''SELL'' was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B12 PASS intercept action=''SELL'' rejected';
  END;

  -- B13: decision log action enum.
  BEGIN
    INSERT INTO risk_decision_log
      (decision_id, request_id, is_open, quantity, adjusted_quantity, action,
       run_state, rule_version, created_at)
    VALUES ('__smoke_b13', '__smoke_req', TRUE, 100, 100, 'SKIP', 'NORMAL', 1,
            CURRENT_TIMESTAMP(3));
    n_fail := n_fail + 1;
    RAISE NOTICE 'B13 FAIL decision action=''SKIP'' was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B13 PASS decision action=''SKIP'' rejected';
  END;

  -- B14: decision log run_state enum. HALF_OPEN must NOT exist: a run state that
  -- is not in this list would be treated as "not KILLED, not BREAKER_TRIPPED" and
  -- fall through to the permissive branch.
  BEGIN
    INSERT INTO risk_decision_log
      (decision_id, request_id, is_open, quantity, adjusted_quantity, action,
       run_state, rule_version, created_at)
    VALUES ('__smoke_b14', '__smoke_req', TRUE, 100, 100, 'PASS', 'HALF_OPEN', 1,
            CURRENT_TIMESTAMP(3));
    n_fail := n_fail + 1;
    RAISE NOTICE 'B14 FAIL run_state=''HALF_OPEN'' was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B14 PASS run_state=''HALF_OPEN'' rejected';
  END;

  -- B15: D7 -- the breaker has exactly two states. Implicit recovery is the exact
  -- failure mode D7 exists to forbid.
  BEGIN
    INSERT INTO risk_breaker_state (breaker_key, state)
    VALUES ('STRATEGY:__smoke', 'HALF_OPEN');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B15 FAIL breaker state=''HALF_OPEN'' was accepted (D7)';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B15 PASS breaker state=''HALF_OPEN'' rejected (D7)';
  END;

  -- B16: the kill switch is a single row. A second row would make "is the switch
  -- on?" depend on which row you happened to read.
  BEGIN
    INSERT INTO risk_switch_state (id, active, activated_by)
    VALUES (2, TRUE, 'smoke');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B16 FAIL kill-switch row id=2 was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B16 PASS kill-switch row id=2 rejected (single row)';
  END;

  -- B17: same for the config version row.
  BEGIN
    INSERT INTO risk_config_version (id, version, updated_at)
    VALUES (2, 1, CURRENT_TIMESTAMP(3));
    n_fail := n_fail + 1;
    RAISE NOTICE 'B17 FAIL config-version row id=2 was accepted';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B17 PASS config-version row id=2 rejected (single row)';
  END;

  -- B18: the scope enum. Added 2026-09-23: ck_risk_rule_scope was present in the
  -- DDL and listed by A2, but NO sample ever fed it a value it had to refuse -- so
  -- the constraint was inventory-checked and never behaviour-checked. 'ACCOUNTS' is
  -- the realistic shape of the mistake (a near-miss on 'ACCOUNT'). Exactly one
  -- constraint can fire: scope_key is '*' so ck_risk_rule_global_key is satisfied
  -- even though scope is not 'GLOBAL'.
  BEGIN
    INSERT INTO risk_rule
      (rule_id, scope, scope_key, threshold, unit, comparison, enabled,
       version, updated_by, updated_at, description)
    VALUES ('__smoke_b18', 'ACCOUNTS', '*', 0.10, 'RATIO', 'LTE', TRUE,
            1, 'smoke', CURRENT_TIMESTAMP(3), '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B18 FAIL  scope=''ACCOUNTS'' was accepted (not a declared member)';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B18 PASS  unknown scope rejected';
  END;

  -- B19: the audit-trail direction enum. Same story as B18 -- the constraint existed
  -- and was inventoried, but risk_rule_audit was never inserted into by this file at
  -- all, so nothing proved it rejects anything. 'WIDEN' is a plausible word that is
  -- NOT a declared member; a row that is not TIGHTEN/RELAX/'' would be read back by
  -- the change-report as neither, silently losing the direction of a threshold change.
  BEGIN
    INSERT INTO risk_rule_audit
      (rule_id, scope, scope_key, old_threshold, new_threshold, change_direction,
       applied, version, changed_by, reason, created_at)
    VALUES ('max_position_pct', 'GLOBAL', '*', '0.10', '0.12', 'WIDEN',
            TRUE, 2, 'smoke', '', CURRENT_TIMESTAMP(3));
    n_fail := n_fail + 1;
    RAISE NOTICE 'B19 FAIL  change_direction=''WIDEN'' was accepted (not a declared member)';
  EXCEPTION WHEN check_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'B19 PASS  unknown change_direction rejected';
  END;

  -- =========================================================================
  -- verdict
  -- =========================================================================
  RAISE NOTICE '---- smoke summary: % passed, % failed ----', n_pass, n_fail;
  IF n_fail > 0 THEN
    RAISE EXCEPTION
      'SMOKE FAIL: % of % database-level guards are ineffective',
      n_fail, n_pass + n_fail;
  END IF;
  RAISE NOTICE 'SMOKE PASS: all % database-level guards are effective', n_pass;

END
$smoke$;

ROLLBACK;
