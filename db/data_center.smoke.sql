-- ============================================================================
-- data-center DDL smoke test  (PostgreSQL 14+)
--
--   NOTE: comments in this file are intentionally ASCII-only. psql derives the
--   client encoding from the Windows console code page, and a UTF-8 file with CJK
--   comments can blow up with `invalid byte sequence for encoding "GBK"` before a
--   single statement runs. Do not "helpfully" translate these comments.
--   (db/data_center.sql does contain Chinese comments and therefore does need
--    PGCLIENTENCODING=UTF8 -- see the run instructions below.)
--
-- WHY THIS FILE EXISTS
--   tools/verify_data_center.py is a STATIC gate. It proves every CHECK
--   constraint is documented in contract section 3.6.1, is declared inside a
--   CREATE TABLE body, and that the DDL still carries its NOT-YET-EXECUTED banner.
--   It cannot prove the constraint actually REJECTS anything. `roe >= -1 AND
--   roe <= 5` typo'd to `<= 0.5`, an enum list missing a member, an inverted
--   comparison, a constraint renamed out from under a test -- every one of those
--   passes all of the static checks while enforcing nothing.
--
--   This file is the TRIGGER TEST. Every database-level guard gets:
--     * a sample that MUST be rejected, and the test asserts WHICH constraint
--       refused it, by name (GET STACKED DIAGNOSTICS ... CONSTRAINT_NAME), so a
--       well-meaning catch-all added later cannot keep the test green while the
--       real guard rots away; and
--     * boundary samples that MUST be accepted, because an over-tight constraint
--       is a harder bug to notice than a missing one -- everything still looks
--       guarded, and the data simply stops loading one day.
--
--   Together: the static gate protects the constraint INVENTORY, this test
--   protects the constraint SEMANTICS. Neither one alone is sufficient.
--
-- CONVENTION THIS FILE MUST KEEP (enforced by gate check C7)
--   Every constraint name declared in db/data_center.sql must be compared for
--   EQUALITY against a quoted literal here, i.e. the line must read
--   `= 'ck_dc_...'` (the shape used throughout section B). Two weaker forms are
--   deliberately rejected by C7:
--     * a name that appears only inside a comment -- that lets the constraint be
--       dropped or renamed while this file still "mentions" it; and
--     * a name that appears only as a bare quoted literal in a list -- such as the
--       A2 inventory in this very file. A2 has to name all 16 constraints, so if C7
--       accepted bare literals the whole of section B could be deleted and the gate
--       would still report full coverage. Asserting the name AFTER a rejection, in
--       the equality form, is what makes it a trigger test.
--
-- HOW TO RUN  (nothing is persisted -- the whole thing rolls back)
--   Windows PowerShell:
--     $env:PGCLIENTENCODING = 'UTF8'
--     psql -v ON_ERROR_STOP=1 -U postgres -d quan -f db/data_center.sql
--     psql -v ON_ERROR_STOP=1 -U postgres -d quan -f db/data_center.smoke.sql
--     Remove-Item Env:PGCLIENTENCODING      # do not leave it set globally
--
--   Result:
--     exit code 0 + "SMOKE PASS: ..."  -> every guard is effective
--     non-zero   + "SMOKE FAIL: ..."   -> at least one guard is a paper guard
--
-- STATUS: written 2026-09-23, EXECUTED 2026-09-23 -- SMOKE PASS, 42 passed / 0 failed
--   on postgres:17 (PostgreSQL 17.11, Debian) via tools/run_sql_smoke.py. Evidence,
--   including the image digest: tools/sql-smoke-report.txt.
--
--   What that green does NOT cover:
--     * only that one image has ever been run -- "PostgreSQL 14+" is untested;
--     * the denominator is not self-asserted: a section that silently stopped
--       executing would shrink n_pass without turning anything red. What keeps the
--       B half present is C7 in tools/verify_data_center.py (16/16 asserted by name).
--     * falsification is per-CONSTRAINT, one case at a time: each case must turn
--       EXACTLY one sample red (n_fail == 1). That signature is what indirectly
--       covers "every sample value really falls inside the reject region"; it is not
--       a per-value proof. The 3 guards that are not named CHECKs (the two NOT NULLs
--       and the partial unique index) are covered by C7, not by the falsifier.
--
--   A6 is RESOLVED: GET STACKED DIAGNOSTICS ... = CONSTRAINT_NAME DOES return
--   uq_dc_data_version_active for a partial unique INDEX violation, so the fallback
--   documented below (relax A6 to the SQLSTATE only) is NOT needed -- A6 stays strict.
--
--   Proof that the B half has teeth (a green that is never falsified is not evidence):
--   tools/falsify_smoke.py relaxes ONE named CHECK at a time in the DDL (expression
--   only -- the constraint is never dropped), re-runs this file, and requires exactly
--   one sample to go red. Coverage is now every constraint, not a sample: 31 cases /
--   31 CAUGHT / 30 named CHECKs (this file's 16 + risk control's 14). Per-case
--   evidence: tools/falsify-report.txt. Example: widening ck_dc_fin_roe_range's upper
--   bound from 5 to 1000 makes this file report 41 passed / 1 failed
--   (B10 FAIL  roe = 600 was accepted) instead of 42 / 0.
--
--   A6 asked whether PostgreSQL populates CONSTRAINT_NAME for a violation raised by a
--   unique INDEX. Measured answer on 17.11: yes, it does -- A6 PASSED, so the planned
--   fallback (relax that one assertion to the SQLSTATE only) was NOT needed. That
--   question is the perfect example of what no static check can answer: this file
--   could only state it as a hypothesis until the day it finally ran.
-- ============================================================================

BEGIN;

DO $smoke$
DECLARE
  n_pass    int := 0;
  n_fail    int := 0;
  v_ok      int;
  v_missing text;
  v_cnt     int;
  v_val     numeric;
  v_date    date;
  v_cname   text;
  v_flag    text;
BEGIN

  -- ========================================================================
  -- A. schema inventory, seed integrity, cross-row invariant
  -- ========================================================================

  -- A1: all nine tables must be present in the search_path.
  SELECT count(*) INTO v_cnt
  FROM unnest(ARRAY[
         'dc_data_version', 'dc_trading_calendar', 'dc_symbol', 'dc_daily_bar',
         'dc_adjust_factor', 'dc_financial_report', 'dc_index_member',
         'dc_ingest_run', 'dc_quality_issue']) AS t(name)
  WHERE to_regclass(t.name) IS NULL;
  IF v_cnt = 0 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A1 PASS  all 9 tables exist';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A1 FAIL  % table(s) missing from the schema', v_cnt;
  END IF;

  -- A2: the constraint inventory as the DATABASE sees it. This is the dynamic twin
  -- of static check C4: C4 reads the DDL text, A2 reads pg_constraint. A DDL edit
  -- that was never applied to a live database (stale schema, missed migration) is
  -- caught here and only here.
  SELECT string_agg(e.tbl || '.' || e.cname, ', ') INTO v_missing
  FROM (VALUES
      ('dc_data_version',     'ck_dc_version_format'),
      ('dc_symbol',           'ck_dc_symbol_delist_after_list'),
      ('dc_daily_bar',        'ck_dc_bar_price_positive'),
      ('dc_daily_bar',        'ck_dc_bar_ohlc_order'),
      ('dc_daily_bar',        'ck_dc_bar_volume_nonneg'),
      ('dc_adjust_factor',    'ck_dc_factor_positive'),
      ('dc_financial_report', 'ck_dc_fin_report_type'),
      ('dc_financial_report', 'ck_dc_fin_announce_after_period'),
      ('dc_financial_report', 'ck_dc_fin_available_ge_announce'),
      ('dc_financial_report', 'ck_dc_fin_roe_range'),
      ('dc_index_member',     'ck_dc_member_effective_range'),
      ('dc_index_member',     'ck_dc_member_weight_range'),
      ('dc_ingest_run',       'ck_dc_ingest_status'),
      ('dc_ingest_run',       'ck_dc_ingest_priority'),
      ('dc_quality_issue',    'ck_dc_quality_flag'),
      ('dc_quality_issue',    'ck_dc_quality_severity')
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
    RAISE NOTICE 'A2 PASS  all 16 required CHECK constraints present';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A2 FAIL  missing/misplaced CHECK constraint(s): %', v_missing;
  END IF;

  -- A3: the initial data version seed. An ON CONFLICT DO NOTHING seed that never
  -- actually inserted a row would leave the table empty, and then every version
  -- binding downstream is silently unenforced.
  SELECT count(*), max(CASE WHEN is_active THEN 1 ELSE 0 END) INTO v_cnt, v_val
  FROM dc_data_version
  WHERE data_version = 'v2026.09.23' AND as_of_date = DATE '2026-09-23';
  IF v_cnt = 1 AND v_val = 1 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A3 PASS  seed version v2026.09.23 is present, dated, and active';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A3 FAIL  seed row wrong: matching rows=%, active flag=%', v_cnt, v_val;
  END IF;

  -- A4: idempotency and immutability (contract D8). Re-running the seed must NOT
  -- overwrite a published version. If ON CONFLICT DO NOTHING were ever "upgraded"
  -- to DO UPDATE, this catches it.
  INSERT INTO dc_data_version (data_version, as_of_date, is_active, notes)
  VALUES ('v2026.09.23', DATE '1999-01-01', FALSE, 'must not overwrite')
  ON CONFLICT (data_version) DO NOTHING;

  SELECT count(*), max(as_of_date) INTO v_cnt, v_date
  FROM dc_data_version WHERE data_version = 'v2026.09.23';
  IF v_cnt = 1 AND v_date = DATE '2026-09-23' THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A4 PASS  seed is idempotent (as_of_date 2026-09-23 survived a re-seed)';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A4 FAIL  re-seed overwrote the version: rows=%, as_of_date=%', v_cnt, v_date;
  END IF;

  -- A5: "at most one is_active = TRUE row" is a CROSS-ROW invariant, so no CHECK
  -- constraint can express it -- the DDL uses a partial unique index instead.
  -- Static check C4 only ever reads CHECK constraints, so this invariant is
  -- invisible to the gate and this is the ONLY place it is ever tested.
  SELECT count(*) INTO v_cnt
  FROM pg_indexes
  WHERE tablename = 'dc_data_version' AND indexname = 'uq_dc_data_version_active';
  IF v_cnt = 1 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'A5 PASS  partial unique index uq_dc_data_version_active exists';
  ELSE
    n_fail := n_fail + 1;
    RAISE NOTICE 'A5 FAIL  partial unique index uq_dc_data_version_active is missing';
  END IF;

  -- A6: ... and it actually bites. Existence is not enforcement.
  BEGIN
    INSERT INTO dc_data_version (data_version, as_of_date, is_active, notes)
    VALUES ('v2026.09.24', DATE '2026-09-24', TRUE, 'second active row');
    n_fail := n_fail + 1;
    RAISE NOTICE 'A6 FAIL  a SECOND is_active = TRUE row was accepted -- the partial unique index is not enforcing';
  EXCEPTION WHEN unique_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'uq_dc_data_version_active' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'A6 PASS  second active row rejected by uq_dc_data_version_active';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'A6 FAIL  second active row rejected, but by the WRONG guard: %', v_cname;
    END IF;
  END;

  -- ========================================================================
  -- B. one sample per CHECK constraint -- every sample MUST be rejected
  -- ========================================================================
  -- Each sample is built so that exactly ONE constraint can fire (checked by hand
  -- against the other constraints on the same table), and the name assertion makes
  -- that explicit rather than assumed.

  -- B1: version format. A version without the leading 'v' must be refused.
  -- NOTE: the regex is a FORMAT check only and accepts v2026.13.45 -- [0-9]{2} does
  -- not validate a month/day RANGE. That is a deliberate limit of the constraint,
  -- not something this test can cover; real calendar validity comes from
  -- dc_trading_calendar (contract section 3.5).
  BEGIN
    INSERT INTO dc_data_version (data_version, as_of_date, is_active, notes)
    VALUES ('2026.09.23', DATE '2026-09-23', FALSE, '');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B1 FAIL  a version with no leading v was accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_version_format' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B1 PASS  malformed version rejected by ck_dc_version_format';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B1 FAIL  malformed version rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B2: delist_date must be strictly AFTER list_date. Equal is refused -- a symbol
  -- listed and delisted the same day is a data error, not a real event.
  BEGIN
    INSERT INTO dc_symbol (symbol, exchange, list_date, delist_date, available_date, data_version)
    VALUES ('__smoke_sym_range', 'SSE', DATE '2026-01-01', DATE '2026-01-01',
            DATE '2026-01-01', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B2 FAIL  delist_date = list_date was accepted (bound must be strict)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_symbol_delist_after_list' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B2 PASS  equal list/delist dates rejected by ck_dc_symbol_delist_after_list';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B2 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B3: prices must be strictly positive. All four set to 0 so that the OHLC
  -- ORDER constraint (which is satisfied by equality) cannot fire instead.
  BEGIN
    INSERT INTO dc_daily_bar (symbol, trade_date, open, high, low, close, volume, amount, data_version)
    VALUES ('__smoke_bar_price', DATE '2026-01-05', 0, 0, 0, 0, 100, 1000, 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B3 FAIL  a zero price was accepted (prices must be > 0)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_bar_price_positive' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B3 PASS  zero price rejected by ck_dc_bar_price_positive';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B3 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B4: high must be the highest. high < low is refused. Prices stay positive so
  -- that B3's constraint cannot fire instead.
  BEGIN
    INSERT INTO dc_daily_bar (symbol, trade_date, open, high, low, close, volume, amount, data_version)
    VALUES ('__smoke_bar_ohlc', DATE '2026-01-05', 10.5, 10, 11, 10.5, 100, 1000, 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B4 FAIL  high < low was accepted (OHLC ordering not enforced)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_bar_ohlc_order' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B4 PASS  high < low rejected by ck_dc_bar_ohlc_order';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B4 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B5: volume/amount must be non-negative. Prices equal and positive so that
  -- neither price constraint can fire.
  BEGIN
    INSERT INTO dc_daily_bar (symbol, trade_date, open, high, low, close, volume, amount, data_version)
    VALUES ('__smoke_bar_vol', DATE '2026-01-05', 10, 10, 10, 10, -1, 1000, 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B5 FAIL  volume = -1 was accepted (volume must be >= 0)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_bar_volume_nonneg' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B5 PASS  negative volume rejected by ck_dc_bar_volume_nonneg';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B5 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B6: the cumulative adjustment factor must be strictly positive. Exactly one
  -- constraint exists on this table, so this cannot be satisfied by the wrong guard.
  BEGIN
    INSERT INTO dc_adjust_factor (symbol, trade_date, adjust_factor, data_version)
    VALUES ('__smoke_factor', DATE '2026-01-05', 0, 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B6 FAIL  adjust_factor = 0 was accepted (must be > 0)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_factor_positive' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B6 PASS  adjust_factor = 0 rejected by ck_dc_factor_positive';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B6 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B7: report_type must be a declared member. Dates are left consistent so that
  -- the two date-order constraints cannot fire instead.
  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, data_version)
    VALUES ('__smoke_fin_type', 'QUARTERLY', DATE '2026-03-31', DATE '2026-04-28',
            DATE '2026-04-28', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B7 FAIL  report_type = QUARTERLY was accepted (not a declared member)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_fin_report_type' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B7 PASS  unknown report_type rejected by ck_dc_fin_report_type';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B7 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B8: a report cannot be ANNOUNCED before its period ends (contract D4 -- the
  -- whole point of the PIT design). available_date is set equal to announce_date so
  -- that the availability constraint is satisfied and cannot fire instead.
  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, data_version)
    VALUES ('__smoke_fin_ann', 'INCOME', DATE '2026-03-31', DATE '2026-03-01',
            DATE '2026-03-01', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B8 FAIL  announce_date < period_end was accepted (D4 look-ahead leak)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_fin_announce_after_period' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B8 PASS  announce before period end rejected by ck_dc_fin_announce_after_period';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B8 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B9: available_date is the PIT filter key and may not precede announce_date.
  -- announce_date still respects period_end, so B8's constraint is satisfied.
  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, data_version)
    VALUES ('__smoke_fin_avail', 'INCOME', DATE '2026-03-31', DATE '2026-04-28',
            DATE '2026-04-01', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B9 FAIL  available_date < announce_date was accepted (data visible before publication)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_fin_available_ge_announce' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B9 PASS  availability before announcement rejected by ck_dc_fin_available_ge_announce';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B9 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B10: roe is a RATIO bound (-1 .. 5), NOT a percentage. 600 must be refused: that
  -- is the shape a 6.00 ratio takes if a source hands over "600%" and the adapter
  -- forgets to divide by 100. A slipped unit is the classic silent corruption here,
  -- and it is exactly what this bound exists to catch.
  -- NOTE: the COMMENT on this column still says 0~1 while the CHECK says -1..5. The
  -- two disagree and the disagreement is unresolved -- see the reconciliation item in
  -- docs/开工前缺口清单.md. The CHECK is what runs; these two accept samples (C1a/C1b)
  -- pin down the behaviour the database actually has until that is settled.
  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, roe, data_version)
    VALUES ('__smoke_fin_roe', 'INDICATOR', DATE '2026-03-31', DATE '2026-04-28',
            DATE '2026-04-28', 600, 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B10 FAIL  roe = 600 was accepted (percentage typed as a ratio)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_fin_roe_range' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B10 PASS  out-of-range roe rejected by ck_dc_fin_roe_range';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B10 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B11: the membership interval must be strictly empty-free: an effective_to equal
  -- to effective_from covers no trading day at all and is refused.
  BEGIN
    INSERT INTO dc_index_member
      (index_code, symbol, effective_from, effective_to, weight, data_version)
    VALUES ('000300.SH', '__smoke_mem_range', DATE '2026-01-01', DATE '2026-01-01',
            0.5, 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B11 FAIL  effective_to = effective_from was accepted (bound must be strict)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_member_effective_range' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B11 PASS  empty membership interval rejected by ck_dc_member_effective_range';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B11 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B12: index weight is a 0..1 RATIO, not a percentage. 1.5 must be refused.
  BEGIN
    INSERT INTO dc_index_member
      (index_code, symbol, effective_from, effective_to, weight, data_version)
    VALUES ('000300.SH', '__smoke_mem_weight', DATE '2026-01-01', NULL,
            1.5, 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B12 FAIL  weight = 1.5 was accepted (percentage typed as a ratio)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_member_weight_range' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B12 PASS  weight = 1.5 rejected by ck_dc_member_weight_range';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B12 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B13: ingest status must be a declared member. run_id is an identity column, so
  -- no key needs to be supplied and no other constraint can fire.
  BEGIN
    INSERT INTO dc_ingest_run (adapter, status, data_version)
    VALUES ('__smoke_ingest', 'DONE', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B13 FAIL  status = DONE was accepted (not a declared member)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_ingest_status' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B13 PASS  unknown status rejected by ck_dc_ingest_status';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B13 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B14: ingest priority must be PRIMARY or FALLBACK. A valid status is supplied so
  -- that B13's constraint cannot fire instead.
  BEGIN
    INSERT INTO dc_ingest_run (adapter, priority, status, data_version)
    VALUES ('__smoke_ingest', 'SECONDARY', 'SUCCESS', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B14 FAIL  priority = SECONDARY was accepted (not a declared member)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_ingest_priority' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B14 PASS  unknown priority rejected by ck_dc_ingest_priority';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B14 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B15: quality flag must be a declared member. A valid severity is supplied so
  -- that B16's constraint cannot fire instead, and the symbol/date are unique to
  -- this sample so that uq_dc_quality_issue cannot fire with a unique_violation
  -- (which is a DIFFERENT SQLSTATE and would escape the handler).
  BEGIN
    INSERT INTO dc_quality_issue (symbol, trade_date, flag, severity, data_version)
    VALUES ('__smoke_qi_flag', DATE '2026-01-05', 'HALTED', 'WARNING', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B15 FAIL  flag = HALTED was accepted (not a declared member)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_quality_flag' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B15 PASS  unknown quality flag rejected by ck_dc_quality_flag';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B15 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- B16: severity must be a declared member. Valid flag, unique key again.
  BEGIN
    INSERT INTO dc_quality_issue (symbol, trade_date, flag, severity, data_version)
    VALUES ('__smoke_qi_sev', DATE '2026-01-05', 'SUSPENDED', 'ERROR', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'B16 FAIL  severity = ERROR was accepted (not a declared member)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    IF v_cname = 'ck_dc_quality_severity' THEN
      n_pass := n_pass + 1;
      RAISE NOTICE 'B16 PASS  unknown severity rejected by ck_dc_quality_severity';
    ELSE
      n_fail := n_fail + 1;
      RAISE NOTICE 'B16 FAIL  rejected by the WRONG constraint: %', v_cname;
    END IF;
  END;

  -- ========================================================================
  -- C. boundary samples -- every sample here MUST be ACCEPTED
  -- ========================================================================
  -- Without these, an over-tight constraint is invisible: the B tests would all
  -- pass and the first sign of trouble would be real data failing to load.

  -- C1: the roe bound is CLOSED (-1 and 5 are legal, NULL is legal because the
  -- column is nullable). All four report_type members are exercised at the same
  -- time, which also proves the enum list is not missing a member.
  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, roe, data_version)
    VALUES ('__smoke_ok_fin', 'BALANCE', DATE '2026-03-31', DATE '2026-04-28',
            DATE '2026-04-28', -1, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C1a PASS  roe = -1 accepted (lower bound is inclusive)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C1a FAIL  legal roe = -1 rejected by % -- the bound is too tight', v_cname;
  END;

  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, roe, data_version)
    VALUES ('__smoke_ok_fin', 'INCOME', DATE '2026-03-31', DATE '2026-04-28',
            DATE '2026-04-28', 5, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C1b PASS  roe = 5 accepted (upper bound is inclusive)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C1b FAIL  legal roe = 5 rejected by % -- the bound is too tight', v_cname;
  END;

  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, roe, data_version)
    VALUES ('__smoke_ok_fin', 'CASHFLOW', DATE '2026-03-31', DATE '2026-04-28',
            DATE '2026-04-28', NULL, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C1c PASS  roe = NULL accepted (column is nullable by design)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C1c FAIL  NULL roe rejected by % -- the constraint forgot the IS NULL arm', v_cname;
  END;

  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, roe, data_version)
    VALUES ('__smoke_ok_fin', 'INDICATOR', DATE '2026-03-31', DATE '2026-04-28',
            DATE '2026-04-28', 0, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C1d PASS  roe = 0 accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C1d FAIL  legal roe = 0 rejected by %', v_cname;
  END;

  -- C2: announce_date equal to period_end is legal (some filings are same-day), and
  -- available_date equal to announce_date is legal (the common case).
  BEGIN
    INSERT INTO dc_financial_report
      (symbol, report_type, period_end, announce_date, available_date, data_version)
    VALUES ('__smoke_ok_fin_eq', 'INCOME', DATE '2026-03-31', DATE '2026-03-31',
            DATE '2026-03-31', 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C2 PASS  announce = period_end and available = announce accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C2 FAIL  equal dates rejected by % -- both bounds must be non-strict', v_cname;
  END;

  -- C3: membership boundaries. weight 0 and weight 1 are legal, NULL weight is
  -- legal, and effective_to IS NULL means "still a member" and must be accepted --
  -- that last one matters most, because a naive `effective_to > effective_from`
  -- without the IS NULL arm would reject every currently-indexed symbol.
  BEGIN
    INSERT INTO dc_index_member
      (index_code, symbol, effective_from, effective_to, weight, data_version)
    VALUES ('000300.SH', '__smoke_ok_mem_a', DATE '2026-01-01', NULL, 0, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C3a PASS  open interval with weight = 0 accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C3a FAIL  weight = 0 rejected by % -- lower bound must be inclusive', v_cname;
  END;

  BEGIN
    INSERT INTO dc_index_member
      (index_code, symbol, effective_from, effective_to, weight, data_version)
    VALUES ('000300.SH', '__smoke_ok_mem_b', DATE '2026-01-01', NULL, 1, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C3b PASS  single-member index with weight = 1 accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C3b FAIL  weight = 1 rejected by % -- upper bound must be inclusive', v_cname;
  END;

  BEGIN
    INSERT INTO dc_index_member
      (index_code, symbol, effective_from, effective_to, weight, data_version)
    VALUES ('000300.SH', '__smoke_ok_mem_c', DATE '2026-01-01', NULL, NULL, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C3c PASS  NULL weight accepted (source published no weight)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C3c FAIL  NULL weight rejected by % -- constraint forgot the IS NULL arm', v_cname;
  END;

  BEGIN
    INSERT INTO dc_index_member
      (index_code, symbol, effective_from, effective_to, weight, data_version)
    VALUES ('000300.SH', '__smoke_ok_mem_d', DATE '2026-01-01', DATE '2026-02-01',
            0.5, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C3d PASS  closed interval with effective_to > effective_from accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C3d FAIL  legal closed interval rejected by %', v_cname;
  END;

  -- C4: symbol date boundaries. Both dates NULL (a fund or index leg with no
  -- listing record) must be accepted, and a normal listed-then-delisted symbol too.
  -- The two IS NULL arms are what make this constraint safe to apply at all.
  BEGIN
    INSERT INTO dc_symbol (symbol, exchange, list_date, delist_date, available_date, data_version)
    VALUES ('__smoke_ok_sym_a', 'SSE', NULL, NULL, DATE '2026-01-05', 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C4a PASS  NULL list_date and delist_date accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C4a FAIL  NULL dates rejected by % -- constraint forgot the IS NULL arms', v_cname;
  END;

  BEGIN
    INSERT INTO dc_symbol (symbol, exchange, list_date, delist_date, available_date, data_version)
    VALUES ('__smoke_ok_sym_b', 'SSE', DATE '2020-01-01', DATE '2026-01-01',
            DATE '2026-01-05', 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C4b PASS  delist_date after list_date accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C4b FAIL  legal delisting rejected by %', v_cname;
  END;

  -- C5: daily-bar boundaries. A one-price day (high = low = open = close) is the
  -- normal shape for a suspended or limit-locked symbol, zero volume is legal, and
  -- amount = 0 with a non-zero volume happens when a source omits turnover. All
  -- three are routine data, all three must load.
  BEGIN
    INSERT INTO dc_daily_bar (symbol, trade_date, open, high, low, close, volume, amount, data_version)
    VALUES ('__smoke_ok_bar', DATE '2026-01-05', 9.99, 9.99, 9.99, 9.99, 0, 0, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C5a PASS  suspended one-price day with zero volume accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C5a FAIL  legal one-price/suspended day rejected by %', v_cname;
  END;

  BEGIN
    INSERT INTO dc_daily_bar (symbol, trade_date, open, high, low, close, volume, amount, data_version)
    VALUES ('__smoke_ok_bar', DATE '2026-01-06', 10, 10.5, 9.8, 10.2, 1000, 10200, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C5b PASS  ordinary trading day accepted';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C5b FAIL  ordinary bar rejected by %', v_cname;
  END;

  BEGIN
    INSERT INTO dc_daily_bar (symbol, trade_date, open, high, low, close, volume, amount, data_version)
    VALUES ('__smoke_ok_bar', DATE '2026-01-07', 10, 10, 10, 10, 100, 0, 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C5c PASS  volume > 0 with amount = 0 accepted (source omitted turnover)';
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
    n_fail := n_fail + 1;
    RAISE NOTICE 'C5c FAIL  legal amount = 0 rejected by %', v_cname;
  END;

  -- C6: the enum lists are COMPLETE, not merely non-empty. Each declared member is
  -- inserted; a missing member shows up as a rejected legal value, which is the
  -- over-tight failure mode that no B test can detect.
  v_ok := 0;
  FOR v_flag IN SELECT unnest(ARRAY['SUSPENDED', 'LIMIT_UP', 'LIMIT_DOWN', 'ST',
                                    'NEW_LISTING', 'DELISTED', 'VOLUME_ANOMALY', 'MISSING'])
  LOOP
    BEGIN
      INSERT INTO dc_quality_issue (symbol, trade_date, flag, severity, data_version)
      VALUES ('__smoke_ok_qi', DATE '2026-01-05', v_flag, 'INFO', 'v2026.09.23');
      v_ok := v_ok + 1;
    EXCEPTION WHEN check_violation THEN
      GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
      n_fail := n_fail + 1;
      RAISE NOTICE 'C6a FAIL  declared flag % rejected by % -- the enum list is too tight',
                   v_flag, v_cname;
    END;
  END LOOP;
  IF v_ok = 8 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'C6a PASS  all 8 declared quality flags accepted';
  END IF;

  -- NOTE: uq_dc_quality_issue is UNIQUE (symbol, trade_date, flag, data_version) and
  -- does NOT include severity. Reusing one symbol/date/flag across the three
  -- severities would therefore raise unique_violation, not check_violation, and this
  -- block would report three failures for reasons that have nothing to do with the
  -- severity list. The symbol is varied instead so each tuple stays distinct.
  v_ok := 0;
  FOR v_flag IN SELECT unnest(ARRAY['CRITICAL', 'WARNING', 'INFO'])
  LOOP
    BEGIN
      INSERT INTO dc_quality_issue (symbol, trade_date, flag, severity, data_version)
      VALUES ('__smoke_ok_sev_' || v_flag, DATE '2026-01-05', 'MISSING', v_flag,
              'v2026.09.23');
      v_ok := v_ok + 1;
    EXCEPTION WHEN check_violation THEN
      GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
      n_fail := n_fail + 1;
      RAISE NOTICE 'C6b FAIL  declared severity % rejected by % -- the enum list is too tight',
                   v_flag, v_cname;
    END;
  END LOOP;
  IF v_ok = 3 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'C6b PASS  all 3 declared severities accepted';
  END IF;

  v_ok := 0;
  FOR v_flag IN SELECT unnest(ARRAY['RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED'])
  LOOP
    BEGIN
      INSERT INTO dc_ingest_run (adapter, status, data_version)
      VALUES ('__smoke_ok_run', v_flag, 'v2026.09.23');
      v_ok := v_ok + 1;
    EXCEPTION WHEN check_violation THEN
      GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
      n_fail := n_fail + 1;
      RAISE NOTICE 'C6c FAIL  declared status % rejected by % -- the enum list is too tight',
                   v_flag, v_cname;
    END;
  END LOOP;
  IF v_ok = 4 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'C6c PASS  all 4 declared ingest statuses accepted';
  END IF;

  v_ok := 0;
  FOR v_flag IN SELECT unnest(ARRAY['PRIMARY', 'FALLBACK'])
  LOOP
    BEGIN
      INSERT INTO dc_ingest_run (adapter, priority, status, data_version)
      VALUES ('__smoke_ok_run', v_flag, 'SUCCESS', 'v2026.09.23');
      v_ok := v_ok + 1;
    EXCEPTION WHEN check_violation THEN
      GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;
      n_fail := n_fail + 1;
      RAISE NOTICE 'C6d FAIL  declared priority % rejected by % -- the enum list is too tight',
                   v_flag, v_cname;
    END;
  END LOOP;
  IF v_ok = 2 THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'C6d PASS  both declared ingest priorities accepted';
  END IF;

  -- C8: the trading calendar accepts an ordinary row, and a repeated
  -- (exchange, trade_date, data_version) is refused as a duplicate rather than
  -- quietly duplicated. Not a CHECK constraint, but it is the table the whole
  -- "Nth trading day" contract rests on (section 3.5), and the DDL seeds nothing
  -- into it, so a first insert succeeding is itself worth proving.
  BEGIN
    INSERT INTO dc_trading_calendar (exchange, trade_date, data_version)
    VALUES ('SSE', DATE '2026-01-05', 'v2026.09.23');
    n_pass := n_pass + 1;
    RAISE NOTICE 'C8a PASS  ordinary trading-day row accepted';
  EXCEPTION WHEN unique_violation THEN
    n_fail := n_fail + 1;
    RAISE NOTICE 'C8a FAIL  a fresh trading-day row was rejected as a duplicate -- the calendar already holds it, so this test is not testing a first insert';
  END;

  BEGIN
    INSERT INTO dc_trading_calendar (exchange, trade_date, data_version)
    VALUES ('SSE', DATE '2026-01-05', 'v2026.09.23');
    n_fail := n_fail + 1;
    RAISE NOTICE 'C8b FAIL  a duplicate calendar day was accepted (primary key not enforcing)';
  EXCEPTION WHEN unique_violation THEN
    n_pass := n_pass + 1;
    RAISE NOTICE 'C8b PASS  duplicate calendar day rejected (unique_violation)';
  END;

  -- ========================================================================
  -- verdict
  -- ========================================================================
  RAISE NOTICE '---- smoke summary: % passed, % failed ----', n_pass, n_fail;
  IF n_fail > 0 THEN
    RAISE EXCEPTION
      'SMOKE FAIL: % of % database-level checks are ineffective',
      n_fail, n_pass + n_fail;
  END IF;
  RAISE NOTICE 'SMOKE PASS: all % database-level checks are effective', n_pass;

END
$smoke$;

ROLLBACK;
