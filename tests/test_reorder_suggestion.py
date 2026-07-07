import base64
import io
from datetime import date, datetime, timedelta
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from openpyxl import load_workbook

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestReorderCalculations(TransactionCase):
    """Pure-function unit tests for the static calculation helpers.
    No DB fixtures needed beyond a config record for ABC thresholds.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.env.company.id,
            'abc_a_threshold': 5.0,
            'abc_b_threshold': 1.0,
        })

    def test_round_to_moq(self):
        m = self.Suggestion._round_to_moq
        self.assertEqual(m(0, 10), 0.0)
        self.assertEqual(m(5, 10), 10.0)
        self.assertEqual(m(10, 10), 10.0)
        self.assertEqual(m(11, 10), 20.0)
        self.assertEqual(m(5, 0), 5.0, 'MOQ <= 0 should pass qty through unrounded')
        self.assertEqual(m(-5, 10), 0.0, 'negative qty must never round to a negative number')

    def test_calc_months_of_stock(self):
        m = self.Suggestion._calc_months_of_stock
        # Rule 1: Net available <= 0 returns 0.0
        self.assertEqual(m(-5.0, 10.0), 0.0)
        self.assertEqual(m(0.0, 10.0), 0.0)
        self.assertEqual(m(-1.0, 0.0), 0.0)
        # Rule 2: Net available > 0 with positive demand returns net available / average monthly demand
        self.assertEqual(m(10.0, 2.0), 5.0)
        self.assertEqual(m(5.0, 2.0), 2.5)
        # Rule 3: Net available > 0 with zero demand returns 999.0
        self.assertEqual(m(10.0, 0.0), 999.0)
        self.assertEqual(m(10.0, -1.0), 999.0)

    def test_classify_abc(self):
        m = self.Suggestion._classify_abc
        self.assertEqual(m(10, self.config), 'A')
        self.assertEqual(m(5, self.config), 'A')
        self.assertEqual(m(3, self.config), 'B')
        self.assertEqual(m(1, self.config), 'B')
        self.assertEqual(m(0.5, self.config), 'C')

    def test_determine_urgency(self):
        m = self.Suggestion._determine_urgency
        # negative on-hand always wins, regardless of every other input
        self.assertEqual(
            m(qty_on_hand=-1, months_of_stock=999, avg_monthly=0,
              lead_months=1, is_dead_stock=False, suggested_qty=0),
            'critical',
        )
        self.assertEqual(
            m(qty_on_hand=5, months_of_stock=999, avg_monthly=0,
              lead_months=1, is_dead_stock=True, suggested_qty=0),
            'dead',
        )
        self.assertEqual(
            m(qty_on_hand=1, months_of_stock=0.5, avg_monthly=2,
              lead_months=1, is_dead_stock=False, suggested_qty=3),
            'urgent',
        )
        self.assertEqual(
            m(qty_on_hand=10, months_of_stock=5, avg_monthly=2,
              lead_months=1, is_dead_stock=False, suggested_qty=3),
            'normal',
        )
        self.assertEqual(
            m(qty_on_hand=10, months_of_stock=5, avg_monthly=2,
              lead_months=1, is_dead_stock=False, suggested_qty=0),
            'ok',
        )

    def test_calc_trend(self):
        m = self.Suggestion._calc_trend
        self.assertEqual(m(0, 0, 3, 3), ('new', 0.0))
        self.assertEqual(m(30, 0, 3, 3), ('up', 100.0))
        trend, pct = m(115, 100, 1, 1)
        self.assertEqual(trend, 'up')
        self.assertGreaterEqual(pct, 15)
        trend, pct = m(80, 100, 1, 1)
        self.assertEqual(trend, 'down')
        self.assertLessEqual(pct, -15)
        trend, pct = m(105, 100, 1, 1)
        self.assertEqual(trend, 'stable')

    def test_calc_seasonal_note(self):
        m = self.Suggestion._calc_seasonal_note
        self.assertIn('No data', m(10, 0))
        self.assertIn('spike', m(150, 100))
        self.assertIn('dip', m(50, 100))
        self.assertIn('Consistent', m(102, 100))

    def test_robust_demand_identical_months_no_outliers(self):
        forecast, excluded, direction, flags = self.Suggestion._compute_robust_monthly_demand(
            [5.0, 5.0, 5.0, 5.0, 5.0]
        )
        self.assertEqual(forecast, 5.0)
        self.assertEqual(excluded, 0)
        self.assertIsNone(direction)
        self.assertEqual(flags, [False] * 5)

    def test_robust_demand_normal_variation_no_outliers(self):
        # Some natural month-to-month variance. Expecting the true average of all months.
        forecast, excluded, direction, flags = self.Suggestion._compute_robust_monthly_demand(
            [4.0, 5.0, 6.0, 5.0, 4.0]
        )
        self.assertEqual(forecast, 4.8)
        self.assertEqual(excluded, 0)
        self.assertIsNone(direction)
        self.assertEqual(flags, [False] * 5)

    def test_robust_demand_rejects_high_bulk_order_outlier(self):
        # One freak 500-unit bulk-order month against a 4-6 baseline is excluded.
        # Expecting the mean of clean months (4.8).
        forecast, excluded, direction, flags = self.Suggestion._compute_robust_monthly_demand(
            [4.0, 5.0, 6.0, 5.0, 4.0, 500.0]
        )
        self.assertEqual(forecast, 4.8)
        self.assertEqual(excluded, 1)
        self.assertEqual(direction, 'high')
        self.assertEqual(flags, [False, False, False, False, False, True],
                          'only the 500-unit month must be flagged as excluded')

    def test_robust_demand_keeps_low_months_no_outliers(self):
        # Low months/zeros must never be excluded as outliers.
        # Expecting the true average of all months (42.0).
        forecast, excluded, direction, flags = self.Suggestion._compute_robust_monthly_demand(
            [50.0, 49.0, 51.0, 52.0, 50.0, 0.0]
        )
        self.assertEqual(forecast, 42.0)
        self.assertEqual(excluded, 0)
        self.assertIsNone(direction)
        self.assertEqual(flags, [False] * 6)

    def test_robust_demand_zero_mad_fallback(self):
        # [0, 0, 0, 5, 0] has median 0.0 and MAD 0.0.
        # It must return the true mathematical average (1.0) instead of the median (0.0).
        forecast, excluded, direction, flags = self.Suggestion._compute_robust_monthly_demand(
            [0.0, 0.0, 0.0, 5.0, 0.0]
        )
        self.assertEqual(forecast, 1.0)
        self.assertEqual(excluded, 0)
        self.assertIsNone(direction)
        self.assertEqual(flags, [False] * 5)

    def test_robust_demand_safety_net(self):
        # Clean months [-2.0, 0.0, 2.0] average to 0.0, but total sales (100.0) is > 0.0.
        # It must fall back to the true average of all months (25.0) instead of returning 0.0.
        forecast, excluded, direction, flags = self.Suggestion._compute_robust_monthly_demand(
            [-2.0, 0.0, 2.0, 100.0]
        )
        self.assertEqual(forecast, 25.0)
        self.assertEqual(excluded, 1)
        self.assertEqual(direction, 'high')
        self.assertEqual(flags, [False, False, False, True])

    def test_robust_demand_empty_series(self):
        forecast, excluded, direction, flags = self.Suggestion._compute_robust_monthly_demand([])
        self.assertEqual(forecast, 0.0)
        self.assertEqual(excluded, 0)
        self.assertIsNone(direction)
        self.assertEqual(flags, [])

    def test_calc_delta_pct(self):
        m = self.Suggestion._calc_delta_pct
        self.assertEqual(m(0, 5), 100.0, 'brand-new suggestion must read as +100%, not divide by zero')
        self.assertEqual(m(0, 0), 0.0, 'no prior qty and no new qty is genuinely no change')
        self.assertEqual(m(10, 20), 100.0)
        self.assertEqual(m(20, 10), -50.0)
        self.assertEqual(m(10, 10), 0.0)
        self.assertEqual(m(4, 12), 200.0)

    def test_compute_needs_review_large_swing(self):
        m = self.Suggestion._compute_needs_review
        flagged, reason = m(avg_monthly=5, suggested_qty=10, is_dead_stock=False,
                             delta_pct=80.0, delta_threshold=50.0)
        self.assertTrue(flagged)
        self.assertIn('80%', reason)

        not_flagged, reason = m(avg_monthly=5, suggested_qty=10, is_dead_stock=False,
                                 delta_pct=30.0, delta_threshold=50.0)
        self.assertFalse(not_flagged)
        self.assertEqual(reason, '')

    def test_compute_needs_review_swing_trigger_disabled_at_zero_threshold(self):
        flagged, reason = self.Suggestion._compute_needs_review(
            avg_monthly=5, suggested_qty=10, is_dead_stock=False,
            delta_pct=500.0, delta_threshold=0,
        )
        self.assertFalse(flagged, 'threshold 0 must disable the swing trigger entirely')
        self.assertEqual(reason, '')

    def test_compute_needs_review_zero_demand_with_positive_suggestion(self):
        flagged, reason = self.Suggestion._compute_needs_review(
            avg_monthly=0, suggested_qty=8, is_dead_stock=False,
            delta_pct=0.0, delta_threshold=50.0,
        )
        self.assertTrue(flagged)
        self.assertIn('No real monthly demand', reason)

    def test_compute_needs_review_dead_stock_contradiction(self):
        flagged, reason = self.Suggestion._compute_needs_review(
            avg_monthly=2, suggested_qty=5, is_dead_stock=True,
            delta_pct=0.0, delta_threshold=50.0,
        )
        self.assertTrue(flagged)
        self.assertIn('dead stock', reason)

    def test_compute_needs_review_normal_dead_stock_not_flagged(self):
        # Dead stock with NO suggested reorder (the common, non-contradictory case).
        flagged, reason = self.Suggestion._compute_needs_review(
            avg_monthly=0, suggested_qty=0, is_dead_stock=True,
            delta_pct=0.0, delta_threshold=50.0,
        )
        self.assertFalse(flagged)
        self.assertEqual(reason, '')

    def test_compute_needs_review_multiple_triggers_combine(self):
        flagged, reason = self.Suggestion._compute_needs_review(
            avg_monthly=0, suggested_qty=10, is_dead_stock=True,
            delta_pct=200.0, delta_threshold=50.0,
        )
        self.assertTrue(flagged)
        self.assertIn('changed', reason)
        self.assertIn('No real monthly demand', reason)
        self.assertIn('dead stock', reason)

    def test_confidence_score_clean_history_is_100(self):
        score, reason = self.Suggestion._compute_confidence_score(
            monthly_series=[4.0, 5.0, 6.0, 5.0, 4.0, 5.0], excluded_months=0
        )
        self.assertEqual(score, 100.0)
        self.assertEqual(reason, 'Full clean history — no deductions.')

    def test_confidence_score_deducts_per_zero_month(self):
        score, reason = self.Suggestion._compute_confidence_score(
            monthly_series=[0.0, 0.0, 5.0, 4.0, 5.0, 4.0], excluded_months=0
        )
        self.assertEqual(score, 90.0, '2 zero months x 5-point penalty = -10')
        self.assertIn('2 of 6 months had zero sales (-10)', reason)

    def test_confidence_score_deducts_per_outlier_month(self):
        score, reason = self.Suggestion._compute_confidence_score(
            monthly_series=[4.0, 5.0, 6.0, 5.0, 4.0, 500.0], excluded_months=1
        )
        self.assertEqual(score, 85.0, '1 outlier month x 15-point penalty = -15')
        self.assertIn('1 month(s) excluded as an outlier (-15)', reason)

    def test_confidence_score_deducts_for_very_new_product(self):
        score, reason = self.Suggestion._compute_confidence_score(
            monthly_series=[0.0, 0.0, 0.0, 0.0, 0.0, 5.0], excluded_months=0
        )
        # 5 zero months (-25) + very-limited-history, only 1 non-zero month (-10) = 65
        self.assertEqual(score, 65.0)
        self.assertIn('5 of 6 months had zero sales (-25)', reason)
        self.assertIn('Very limited sales history — only 1 month(s) with any sales (-10)', reason)

    def test_confidence_score_floors_at_zero(self):
        score, _ = self.Suggestion._compute_confidence_score(
            monthly_series=[0.0] * 6, excluded_months=10
        )
        self.assertEqual(score, 0.0, 'must never go negative')

    def test_confidence_score_caps_at_100(self):
        score, _ = self.Suggestion._compute_confidence_score(
            monthly_series=[4.0, 5.0, 6.0, 5.0, 4.0, 5.0], excluded_months=0
        )
        self.assertLessEqual(score, 100.0)

    def test_config_order_cycle_validation(self):
        with self.assertRaises(ValidationError):
            self.config.write({'order_cycle_months': -0.5})


@tagged('post_install', '-at_install')
class TestReorderGenerateSuggestions(TransactionCase):
    """Integration tests for generate_suggestions() — the main engine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company

        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
            'analysis_period': '6',
            'safety_buffer_months': 1.0,
            'default_lead_time_months': 1.0,
            'dead_stock_months': 6,
            'auto_flag_on_negative': True,
        })

        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.env['stock.warehouse'].create({
                'name': 'Test Warehouse',
                'code': 'TWH',
                'company_id': cls.company.id,
            })

        cls.product = cls.env['product.product'].create({
            'name': 'Test Reorder Widget',
            'type': 'product',
            'standard_price': 10.0,
        })

    def _set_quant(self, qty_delta):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, qty_delta
        )

    def test_generate_suggestions_flags_negative_stock_critical(self):
        self._set_quant(-5.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        suggestion = self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(len(suggestion), 1)
        self.assertEqual(suggestion.urgency, 'critical')
        self.assertTrue(suggestion.reorder_needed)
        self.assertTrue(suggestion.active)

    def test_orphan_suggestion_is_archived_not_deleted(self):
        # Run 1: negative stock creates an active critical suggestion.
        self._set_quant(-5.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        suggestion = self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(len(suggestion), 1)
        original_id = suggestion.id

        # Run 2: bring stock back to zero (out of scope — no sales, no negative qty).
        self._set_quant(5.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        archived = self.Suggestion.with_context(active_test=False).search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(len(archived), 1, 'orphan must be archived, never hard-deleted')
        self.assertEqual(archived.id, original_id, 'archiving must reuse the same record')
        self.assertFalse(archived.active)

    def test_reactivated_suggestion_reuses_archived_record(self):
        # Create, archive, then bring the product back into scope.
        self._set_quant(-5.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        original_id = self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
        ]).id

        self._set_quant(5.0)  # archive it
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )

        self._set_quant(-5.0)  # bring it back into scope (negative again)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )

        all_records = self.Suggestion.with_context(active_test=False).search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(
            len(all_records), 1,
            'reactivation must reuse the archived record, not violate the unique constraint '
            'by creating a duplicate'
        )
        self.assertEqual(all_records.id, original_id)
        self.assertTrue(all_records.active)
        self.assertEqual(all_records.urgency, 'critical')

    def test_generate_suggestions_min_max_policy_with_demand(self):
        self.config.write({
            'safety_buffer_months': 1.0,
            'default_lead_time_months': 1.5,
            'order_cycle_months': 2.0,
        })
        self._set_quant(5.0)  # qty_on_hand = 5.0, qty_available = 5.0

        # Patch _compute_robust_monthly_demand to return average monthly demand of 10.0
        with patch.object(self.Suggestion, '_compute_robust_monthly_demand', return_value=(10.0, 0, None, [False]*6)):
            self.Suggestion.generate_suggestions(
                company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
            )
        suggestion = self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
        ])
        # Min Level = 10.0 * (1.5 + 1.0) = 25.0
        # Max Level = 25.0 + 10.0 * 2.0 = 45.0
        # qty_available = 5.0 < Min Level (25.0) -> Triggered!
        # raw_qty = Max Level - qty_available = 45.0 - 5.0 = 40.0
        # MOQ = 1.0 -> suggested_qty = 40.0
        self.assertEqual(suggestion.min_stock_level, 25.0)
        self.assertEqual(suggestion.max_stock_level, 45.0)
        self.assertEqual(suggestion.suggested_reorder_qty, 40.0)

    def test_generate_suggestions_min_max_policy_not_triggered(self):
        self.config.write({
            'safety_buffer_months': 1.0,
            'default_lead_time_months': 1.5,
            'order_cycle_months': 2.0,
        })
        self._set_quant(30.0)  # qty_on_hand = 30.0, qty_available = 30.0

        # Patch _compute_robust_monthly_demand to return average monthly demand of 10.0
        with patch.object(self.Suggestion, '_compute_robust_monthly_demand', return_value=(10.0, 0, None, [False]*6)):
            self.Suggestion.generate_suggestions(
                company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
            )
        suggestion = self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
        ])
        # Min Level = 10.0 * (1.5 + 1.0) = 25.0
        # Max Level = 25.0 + 10.0 * 2.0 = 45.0
        # qty_available = 30.0 >= Min Level (25.0) -> Not triggered!
        # raw_qty = 0.0 -> suggested_qty = 0.0
        self.assertEqual(suggestion.min_stock_level, 25.0)
        self.assertEqual(suggestion.max_stock_level, 45.0)
        self.assertEqual(suggestion.suggested_reorder_qty, 0.0)


@tagged('post_install', '-at_install')
class TestReorderSuggestionActions(TransactionCase):
    """Snooze / unsnooze behavior on an existing suggestion."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Test Snooze Widget',
            'type': 'product',
            'standard_price': 5.0,
        })
        cls.suggestion = cls.env['smart.reorder.suggestion'].create({
            'company_id': cls.company.id,
            'warehouse_id': cls.warehouse.id,
            'product_id': cls.product.id,
            'qty_on_hand': -3.0,
            'suggested_reorder_qty': 10.0,
            'urgency': 'critical',
            'reorder_needed': True,
        })

    def test_snooze_30_suppresses_reorder_needed(self):
        self.suggestion.action_snooze_30()
        self.assertFalse(self.suggestion.reorder_needed)
        self.assertTrue(self.suggestion.is_snoozed)
        self.assertTrue(self.suggestion.snoozed_until)

    def test_unsnooze_recomputes_reorder_needed(self):
        self.suggestion.action_snooze_30()
        self.suggestion.action_unsnooze()
        self.assertFalse(self.suggestion.snoozed_until)
        self.assertFalse(self.suggestion.is_snoozed)
        self.assertTrue(
            self.suggestion.reorder_needed,
            'on-hand is still negative, so unsnoozing must re-flag it as needing reorder'
        )


@tagged('post_install', '-at_install')
class TestGenerateSuggestionsWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['smart.reorder.config'].create({'company_id': cls.env.company.id})

    def test_warehouse_scope_without_selection_raises(self):
        wizard = self.env['smart.reorder.wizard'].create({
            'scope': 'warehouse',
            'company_ids': [],
            'warehouse_ids': [],
        })
        with self.assertRaises(UserError):
            wizard.action_generate()

    def test_all_scope_runs_without_error(self):
        wizard = self.env['smart.reorder.wizard'].create({'scope': 'all'})
        result = wizard.action_generate()
        self.assertEqual(result.get('res_model'), 'smart.reorder.suggestion')


@tagged('post_install', '-at_install')
class TestReorderRunLock(TransactionCase):
    """Per-company run lock (T-21/T-22) — backed by smart.reorder.cron.log as the
    auditable source of truth (any 'running' record IS the lock). Cron and the
    manual wizard both funnel through generate_suggestions(), so locking it there
    covers both triggers without a separate check in each caller. config.is_running
    is just a computed view onto the log table, not an independent flag.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.CronLog = cls.env['smart.reorder.cron.log']
        cls.company = cls.env.company
        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
        })
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Test Lock Widget',
            'type': 'product',
            'standard_price': 1.0,
        })

    def _set_quant(self, qty_delta):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, qty_delta
        )

    def _search_suggestion(self):
        return self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])

    def _open_running_logs(self):
        return self.CronLog.search([
            ('company_id', '=', self.company.id),
            ('status', '=', 'running'),
        ])

    def test_lock_released_after_successful_run(self):
        self._set_quant(-1.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        self.assertFalse(self.config.is_running)
        self.assertFalse(self._open_running_logs())
        log = self.CronLog.search([('company_id', '=', self.company.id)], limit=1)
        self.assertEqual(log.status, 'completed')
        self.assertEqual(log.trigger_type, 'cron')
        self.assertTrue(log.finished_at)
        self.assertGreaterEqual(log.duration_seconds, 0.0)

    def test_concurrent_run_is_skipped(self):
        self.CronLog.create({
            'company_id': self.company.id,
            'started_at': fields.Datetime.now(),
            'trigger_type': 'cron',
            'status': 'running',
        })
        self._set_quant(-1.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        self.assertFalse(self._search_suggestion(), 'a fresh lock must skip the company entirely')
        self.assertTrue(self.config.is_running, 'skip path must not touch an in-progress lock')
        self.assertEqual(len(self._open_running_logs()), 1, 'skip path must not create a second log row')

    def test_stuck_lock_is_overridden(self):
        stale_log = self.CronLog.create({
            'company_id': self.company.id,
            'started_at': fields.Datetime.now() - timedelta(minutes=61),
            'trigger_type': 'cron',
            'status': 'running',
        })
        self._set_quant(-1.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        self.assertTrue(self._search_suggestion(), 'a stale (>60min) lock must be overridden, not honored')
        self.assertFalse(self.config.is_running, 'lock must be released once the overriding run completes')
        self.assertEqual(stale_log.status, 'aborted', 'the stale lock itself must be marked aborted')
        completed_logs = self.CronLog.search([
            ('company_id', '=', self.company.id),
            ('status', '=', 'completed'),
        ])
        self.assertEqual(len(completed_logs), 1, 'overriding must create a fresh log, not reuse the stale one')

    def test_lock_released_even_if_company_processing_raises(self):
        self._set_quant(-1.0)
        with patch.object(
            type(self.Suggestion), '_send_notifications',
            side_effect=RuntimeError('boom'),
        ):
            with self.assertRaises(RuntimeError):
                self.Suggestion.generate_suggestions(
                    company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
                )
        self.assertFalse(self.config.is_running, 'the log must be closed out even on exception')
        log = self.CronLog.search([('company_id', '=', self.company.id)], limit=1)
        self.assertEqual(log.status, 'aborted')
        self.assertIn('boom', log.error_notes)

    def test_clear_lock_action(self):
        self.CronLog.create({
            'company_id': self.company.id,
            'started_at': fields.Datetime.now(),
            'trigger_type': 'cron',
            'status': 'running',
        })
        self.assertTrue(self.config.is_running)
        self.config.action_clear_lock()
        self.assertFalse(self.config.is_running)
        self.assertFalse(self._open_running_logs())

    def test_manual_trigger_recorded_as_manual(self):
        wizard = self.env['smart.reorder.wizard'].create({'scope': 'all'})
        wizard.action_generate()
        log = self.CronLog.search(
            [('company_id', '=', self.company.id)], limit=1, order='started_at desc'
        )
        self.assertEqual(log.trigger_type, 'manual')

    def test_email_report_failure_marks_completed_with_errors(self):
        self.config.write({'send_email_report': True})
        self._set_quant(-1.0)
        with patch.object(type(self.Suggestion), '_send_email_report', return_value=False):
            self.Suggestion.generate_suggestions(
                company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
            )
        log = self.CronLog.search([('company_id', '=', self.company.id)], limit=1)
        self.assertEqual(log.status, 'completed_with_errors')


@tagged('post_install', '-at_install')
class TestPoWizardSecurityGuard(TransactionCase):
    """action_confirm_consolidation() must reject non-managers before touching
    active_ids, mirroring the guard on action_create_draft_po()."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['smart.reorder.config'].create({'company_id': cls.env.company.id})
        user_group = cls.env.ref('smart_reorder_advisor.group_smart_reorder_user')
        cls.basic_user = cls.env['res.users'].create({
            'name': 'Basic Reorder User',
            'login': 'basic_reorder_user_test',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id, user_group.id])],
        })

    def test_non_manager_cannot_confirm_consolidation(self):
        wizard = self.env['smart.reorder.po.wizard'].with_user(self.basic_user).create({})
        with self.assertRaises(UserError):
            wizard.action_confirm_consolidation()


@tagged('post_install', '-at_install')
class TestRobustDemandForecast(TransactionCase):
    """End-to-end: generate_suggestions() must forecast off the robust average + MAD
    monthly breakdown, not a plain total/N average, so one freak bulk-order
    month doesn't inflate the reorder qty, value, budget rank, and urgency."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company

        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
            'analysis_period': '6',
            'safety_buffer_months': 0.0,
            'default_lead_time_months': 0.1,
        })

        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.env['stock.warehouse'].create({
                'name': 'Test Warehouse',
                'code': 'TWH2',
                'company_id': cls.company.id,
            })
        cls.partner = cls.env['res.partner'].create({'name': 'Test Robust Demand Customer'})
        cls.product = cls.env['product.product'].create({
            'name': 'Test Robust Demand Widget',
            'type': 'product',
            'standard_price': 1.0,
        })

        # 6 calendar-month buckets, most-recent-first, anchored the same way
        # generate_suggestions() anchors them (today's month, then back by 1).
        today = fields.Date.today()
        cls.month_starts = [
            (today.replace(day=1) - relativedelta(months=i)) for i in range(6)
        ]
        month_starts = cls.month_starts
        # Most recent month (i=0) is a 500-unit outlier; the other 5 months
        # have a normal 4-6 baseline with some natural variance.
        monthly_qtys = [500.0, 4.0, 5.0, 6.0, 5.0, 4.0]
        for month_start, qty in zip(month_starts, monthly_qtys):
            order_date = datetime.combine(month_start + timedelta(days=4), datetime.min.time())
            order = cls.env['sale.order'].create({
                'partner_id': cls.partner.id,
                'company_id': cls.company.id,
                'warehouse_id': cls.warehouse.id,
                'date_order': order_date,
                'order_line': [(0, 0, {
                    'product_id': cls.product.id,
                    'product_uom_qty': qty,
                    'price_unit': 10.0,
                })],
            })
            order.action_confirm()
            order.order_line.qty_delivered = qty

    def test_forecast_uses_average_with_spike_rejection(self):
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        suggestion = self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(len(suggestion), 1)
        self.assertEqual(suggestion.total_qty_sold, 524.0, 'display total must stay the raw sum')
        self.assertEqual(
            suggestion.avg_monthly_demand, 4.8,
            'forecast must be the average of the clean months, not 524/6 ≈ 87.3'
        )
        self.assertEqual(suggestion.excluded_outlier_months, 1)
        self.assertIn('Excluded 1', suggestion.demand_forecast_note)
        self.assertIn('unusually high', suggestion.demand_forecast_note)

        # T-27: the notes audit trail must list every month by name/year, and
        # tag only the actual outlier month — so a buyer can see at a glance
        # which months fed into the number and what got filtered out.
        outlier_label = self.month_starts[0].strftime('%B %Y')
        normal_label = self.month_starts[1].strftime('%B %Y')
        self.assertIn('Per-month breakdown:', suggestion.notes)
        self.assertIn(f'{outlier_label}: 500 units [excluded as outlier]', suggestion.notes)
        self.assertIn(f'{normal_label}: 4 units', suggestion.notes)
        self.assertNotIn(f'{normal_label}: 4 units [excluded as outlier]', suggestion.notes)

        # T-28: confidence score — all 6 months have sales (no zero-month penalty),
        # 1 month excluded as an outlier (-15) → 85/100.
        self.assertEqual(suggestion.confidence, 85.0)
        self.assertIn('Confidence: 85/100', suggestion.notes)
        self.assertIn('1 month(s) excluded as an outlier (-15)', suggestion.notes)

    def test_sales_today_are_included(self):
        product_today = self.env['product.product'].create({
            'name': 'Test Today Sale Widget',
            'type': 'product',
            'standard_price': 1.0,
        })
        today_datetime = datetime.combine(fields.Date.today(), datetime.min.time()) + timedelta(hours=14)
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'date_order': today_datetime,
            'order_line': [(0, 0, {
                'product_id': product_today.id,
                'product_uom_qty': 10.0,
                'price_unit': 10.0,
            })],
        })
        order.action_confirm()
        order.order_line.qty_delivered = 10.0

        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        suggestion = self.Suggestion.search([
            ('product_id', '=', product_today.id),
            ('warehouse_id', '=', self.warehouse.id),
        ])
        self.assertEqual(len(suggestion), 1)
        self.assertEqual(suggestion.total_qty_sold, 10.0, "Today's sales must be fully included")


@tagged('post_install', '-at_install')
class TestStaleDataCarryForward(TransactionCase):
    """T-23: a single warehouse's batch failure (query/DB error) must never wipe
    or silently skip its suggestions. Existing records are left untouched and
    flagged stale instead, the company run still completes (not aborted), and
    the cron log's error_count makes the failure visible without reading logs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.CronLog = cls.env['smart.reorder.cron.log']
        cls.company = cls.env.company
        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
        })
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Test Stale Widget',
            'type': 'product',
            'standard_price': 1.0,
        })

    def _set_quant(self, qty_delta):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, qty_delta
        )

    def _search_suggestion(self):
        return self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])

    def test_failed_batch_leaves_existing_records_untouched_and_flags_stale(self):
        # Baseline: a clean run creates a critical suggestion.
        self._set_quant(-5.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        baseline = self._search_suggestion()
        self.assertEqual(len(baseline), 1)
        self.assertFalse(baseline.is_stale)
        baseline_id = baseline.id

        # Second run: the per-warehouse batch blows up partway through (deep
        # inside the product loop, well past the point where it would otherwise
        # have archived/recreated suggestions).
        with patch.object(
            type(self.Suggestion), '_round_to_moq',
            side_effect=RuntimeError('simulated DB blip'),
        ):
            self.Suggestion.generate_suggestions(
                company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
            )  # must not raise — the failure is caught and handled internally

        # The company run must be reported as completed-with-errors, not aborted,
        # and the error must be counted on the log record.
        log = self.CronLog.search([('company_id', '=', self.company.id)], limit=1)
        self.assertEqual(log.status, 'completed_with_errors')
        self.assertEqual(log.error_count, 1)

        # The existing suggestion must survive untouched, just flagged stale.
        still_there = self._search_suggestion()
        self.assertEqual(len(still_there), 1)
        self.assertEqual(still_there.id, baseline_id, 'must not delete/archive and recreate')
        self.assertTrue(still_there.is_stale)
        self.assertIn('RuntimeError', still_there.stale_reason)
        self.assertEqual(still_there.urgency, 'critical', 'old data must be preserved, not wiped')

    def test_is_stale_clears_on_next_successful_run(self):
        self._set_quant(-5.0)
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        suggestion = self._search_suggestion()
        suggestion.write({'is_stale': True, 'stale_reason': 'Pretend a previous run failed.'})

        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        self.assertFalse(suggestion.is_stale, 'a successful refresh must clear the stale flag')
        self.assertFalse(suggestion.stale_reason)


@tagged('post_install', '-at_install')
class TestDashboardData(TransactionCase):
    """T-24: get_dashboard_data() must compute counts/sums DB-side (read_group /
    search_count) and return the same shape/values it always did — verified here
    against hand-built records rather than loading everything in Python."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.env['stock.warehouse'].create({
                'name': 'Test Warehouse', 'code': 'DWH1', 'company_id': cls.company.id,
            })
        cls.warehouse2 = cls.env['stock.warehouse'].create({
            'name': 'Second Test Warehouse', 'code': 'DWH2', 'company_id': cls.company.id,
        })

        def make_product(name):
            return cls.env['product.product'].create({
                'name': name, 'type': 'product', 'standard_price': 1.0,
            })

        cls.p_critical = make_product('Dash Critical Widget')
        cls.p_urgent   = make_product('Dash Urgent Widget')
        cls.p_dead     = make_product('Dash Dead Widget')
        cls.p_fallingA = make_product('Dash Falling A Widget')
        cls.p_fallingB = make_product('Dash Falling B Widget')
        cls.p_other_wh = make_product('Dash Other Warehouse Widget')

        common = {'company_id': cls.company.id, 'warehouse_id': cls.warehouse.id}
        cls.Suggestion.create([
            dict(common, product_id=cls.p_critical.id, urgency='critical', abc_class='A',
                 demand_trend='down', trend_pct=-10.0, is_dead_stock=False,
                 reorder_needed=True, within_budget=True, reorder_value=100.0,
                 qty_on_hand=-5.0, suggested_reorder_qty=20.0),
            dict(common, product_id=cls.p_urgent.id, urgency='urgent', abc_class='B',
                 demand_trend='up', trend_pct=30.0, is_dead_stock=False,
                 reorder_needed=True, within_budget=False, reorder_value=200.0,
                 months_of_stock=0.5, avg_monthly_demand=10.0, suggested_reorder_qty=15.0),
            dict(common, product_id=cls.p_dead.id, urgency='dead', abc_class='C',
                 demand_trend='stable', is_dead_stock=True, reorder_needed=False,
                 within_budget=True, reorder_value=0.0,
                 months_since_last_sale=8, qty_on_hand=3.0),
            dict(common, product_id=cls.p_fallingA.id, urgency='ok', abc_class='A',
                 demand_trend='down', trend_pct=-15.0, is_dead_stock=False,
                 reorder_needed=False, within_budget=True, reorder_value=0.0),
            dict(common, product_id=cls.p_fallingB.id, urgency='ok', abc_class='A',
                 demand_trend='down', trend_pct=-40.0, is_dead_stock=False,
                 reorder_needed=False, within_budget=True, reorder_value=0.0),
        ])
        # A record in a DIFFERENT warehouse — must be excluded when filtering by cls.warehouse.
        cls.Suggestion.create(dict(
            common, warehouse_id=cls.warehouse2.id, product_id=cls.p_other_wh.id,
            urgency='critical', abc_class='A', demand_trend='down',
            is_dead_stock=False, reorder_needed=True, within_budget=True,
            reorder_value=999.0, qty_on_hand=-99.0,
        ))

    def test_counts_and_sums(self):
        data = self.Suggestion.get_dashboard_data(warehouse_id=self.warehouse.id)
        self.assertEqual(data['urgency'], {'critical': 1, 'urgent': 1, 'dead': 1, 'ok': 2})
        self.assertEqual(data['abc'], {'A': 3, 'B': 1, 'C': 1})
        self.assertEqual(data['trend'], {'down': 3, 'up': 1, 'stable': 1})
        self.assertEqual(data['dead_count'], 1)
        self.assertEqual(data['total_reorder_value'], 300.0)
        self.assertEqual(data['within_budget_value'], 100.0)
        self.assertEqual(data['budget_count'], 1)

    def test_top_lists_ordering_and_scope(self):
        data = self.Suggestion.get_dashboard_data(warehouse_id=self.warehouse.id)
        self.assertEqual(len(data['top']['critical']), 1)
        self.assertEqual(data['top']['critical'][0]['product_id'][0], self.p_critical.id)
        self.assertEqual(len(data['top']['urgent']), 1)
        self.assertEqual(data['top']['urgent'][0]['product_id'][0], self.p_urgent.id)
        self.assertEqual(len(data['top']['dead']), 1)
        self.assertEqual(data['top']['dead'][0]['product_id'][0], self.p_dead.id)
        self.assertEqual(len(data['top']['rising']), 1)
        self.assertEqual(data['top']['rising'][0]['product_id'][0], self.p_urgent.id)
        # falling: two candidates, most negative trend_pct sorted first
        falling_ids = [r['product_id'][0] for r in data['top']['falling']]
        self.assertEqual(falling_ids, [self.p_fallingB.id, self.p_fallingA.id])

    def test_warehouse_filter_excludes_other_warehouses(self):
        data = self.Suggestion.get_dashboard_data(warehouse_id=self.warehouse.id)
        all_top_product_ids = {
            r['product_id'][0] for lst in data['top'].values() for r in lst
        }
        self.assertNotIn(self.p_other_wh.id, all_top_product_ids)
        self.assertEqual(data['urgency']['critical'], 1, 'must not include the other warehouse record')

    def test_no_warehouse_filter_includes_all_warehouses(self):
        data = self.Suggestion.get_dashboard_data()
        self.assertEqual(data['urgency']['critical'], 2, 'unfiltered call must include both warehouses')


@tagged('post_install', '-at_install')
class TestDeltaTracking(TransactionCase):
    """T-25: prior_suggested_qty/delta_pct must track suggested_reorder_qty
    changes across successive generate_suggestions() runs, so buyers can spot
    a suggestion that swung dramatically since last week before approving a PO."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
            'safety_buffer_months': 0.0,
            'default_lead_time_months': 0.1,
        })
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Test Delta Widget',
            'type': 'product',
            'standard_price': 1.0,
        })

    def _set_quant(self, qty_delta):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, qty_delta
        )

    def _run(self):
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        return self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])

    def test_first_run_is_treated_as_100_percent_increase(self):
        self._set_quant(-5.0)
        suggestion = self._run()
        self.assertEqual(suggestion.suggested_reorder_qty, 5.0)
        self.assertEqual(suggestion.prior_suggested_qty, 0.0)
        self.assertEqual(suggestion.delta_pct, 100.0)

    def test_delta_tracks_increase_then_decrease_across_runs(self):
        # Run 1: -5 on hand → suggested qty 5.
        self._set_quant(-5.0)
        suggestion = self._run()
        self.assertEqual(suggestion.suggested_reorder_qty, 5.0)

        # Run 2: push further negative (-20 total) → suggested qty 20.
        self._set_quant(-15.0)
        suggestion = self._run()
        self.assertEqual(suggestion.suggested_reorder_qty, 20.0)
        self.assertEqual(suggestion.prior_suggested_qty, 5.0)
        self.assertEqual(suggestion.delta_pct, 300.0)

        # Run 3: still negative (-2), just less severe — stays in scope (no sales
        # history means it would otherwise be archived as an orphan the moment
        # stock turns non-negative) — suggested qty drops to 2.
        self._set_quant(18.0)
        suggestion = self._run()
        self.assertEqual(suggestion.suggested_reorder_qty, 2.0)
        self.assertEqual(suggestion.prior_suggested_qty, 20.0)
        self.assertEqual(suggestion.delta_pct, -90.0)


@tagged('post_install', '-at_install')
class TestNeedsReviewTriage(TransactionCase):
    """T-26: generate_suggestions() must auto-flag suggestions worth a human
    look — wild swings vs the prior run, negative-stock-only "demand", and
    dead-stock/positive-suggestion contradictions — using the configurable
    delta threshold from smart.reorder.config."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
            'safety_buffer_months': 0.0,
            'default_lead_time_months': 0.1,
            'needs_review_delta_threshold': 50.0,
        })
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Test Review Widget',
            'type': 'product',
            'standard_price': 1.0,
        })

    def _set_quant(self, qty_delta):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, qty_delta
        )

    def _run(self):
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )
        return self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.warehouse.id),
            ('company_id', '=', self.company.id),
        ])

    def test_negative_stock_with_no_real_demand_is_flagged(self):
        # No sales history at all — the suggestion is driven purely by negative
        # stock, not real demand. Must be flagged regardless of the delta swing.
        self._set_quant(-5.0)
        suggestion = self._run()
        self.assertEqual(suggestion.avg_monthly_demand, 0.0)
        self.assertGreater(suggestion.suggested_reorder_qty, 0.0)
        self.assertTrue(suggestion.needs_review)
        self.assertIn('No real monthly demand', suggestion.needs_review_reason)

    def test_large_swing_is_flagged_on_second_run(self):
        self._set_quant(-2.0)
        first = self._run()
        self.assertEqual(first.suggested_reorder_qty, 2.0)

        # Jump to -20 → suggested qty 20, a +900% swing — well past the 50% threshold.
        self._set_quant(-18.0)
        second = self._run()
        self.assertEqual(second.suggested_reorder_qty, 20.0)
        self.assertEqual(second.delta_pct, 900.0)
        self.assertTrue(second.needs_review)
        self.assertIn('changed', second.needs_review_reason)

    def test_swing_threshold_is_configurable(self):
        self.config.write({'needs_review_delta_threshold': 0})
        self._set_quant(-2.0)
        self._run()  # establish a prior suggested qty to diff against
        self._set_quant(-18.0)
        second = self._run()
        self.assertEqual(second.delta_pct, 900.0)
        # The swing trigger is disabled, but the "no real demand" trigger still
        # fires (it always does for a pure negative-stock suggestion) — confirms
        # the threshold only switches off the swing check, not the whole feature.
        self.assertTrue(second.needs_review)
        self.assertNotIn('changed', second.needs_review_reason)
        self.assertIn('No real monthly demand', second.needs_review_reason)


@tagged('post_install', '-at_install')
class TestPdfReportTrendDetail(TransactionCase):
    """T-29: the per-product PDF report must show the numbers behind the trend
    label — current vs previous period monthly average, % change, and the
    same-period-last-year comparison — not just "↑ Rising (+25%)"."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Test Trend Report Widget',
            'type': 'product',
            'standard_price': 1.0,
        })
        cls.suggestion = cls.Suggestion.create({
            'company_id': cls.company.id,
            'warehouse_id': cls.warehouse.id,
            'product_id': cls.product.id,
            'analysis_months': 6,
            'avg_monthly_demand': 10.0,
            'prev_period_qty_sold': 18.0,
            'trend_comparison_months': 3,
            'demand_trend': 'up',
            'trend_pct': 25.0,
            'same_period_last_year_qty': 30.0,
            'seasonal_note': '↑ 20% vs same period last year (seasonal spike?)',
            'urgency': 'normal',
            'reorder_needed': False,
        })

    def test_trend_detail_table_renders_with_correct_numbers(self):
        # Render to HTML (not PDF) so the test doesn't need wkhtmltopdf installed.
        html, _ = self.env['ir.actions.report']._render_qweb_html(
            'smart_reorder_advisor.action_report_reorder_suggestion',
            self.suggestion.ids,
        )
        html = html.decode('utf-8')
        self.assertIn('Demand Trend Detail', html)
        self.assertIn('10.00 units/mo', html, 'current-period avg must show')
        self.assertIn('6.00 units/mo', html, 'prev-period avg = 18 / 3 months')
        self.assertIn('+25.0%', html)
        self.assertIn('30.00 units total', html, 'same-period-last-year total')
        self.assertIn('5.00 units/mo)', html, 'same-period-last-year avg = 30 / 6 months')
        self.assertIn('seasonal spike', html)

    def test_trend_detail_handles_no_prior_data_gracefully(self):
        suggestion = self.Suggestion.create({
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'product_id': self.env['product.product'].create({
                'name': 'Test Trend Report Widget — No History',
                'type': 'product',
                'standard_price': 1.0,
            }).id,
            'analysis_months': 6,
            'avg_monthly_demand': 0.0,
            'prev_period_qty_sold': 0.0,
            'trend_comparison_months': 0,
            'demand_trend': 'new',
            'trend_pct': 0.0,
            'same_period_last_year_qty': 0.0,
            'seasonal_note': 'No data for same period last year',
            'urgency': 'ok',
            'reorder_needed': False,
        })
        html, _ = self.env['ir.actions.report']._render_qweb_html(
            'smart_reorder_advisor.action_report_reorder_suggestion',
            suggestion.ids,
        )
        html = html.decode('utf-8')
        self.assertIn('No data for same period last year', html)
        self.assertIn('★ New / No History', html)


@tagged('post_install', '-at_install')
class TestExportSuggestionsWizard(TransactionCase):
    """T-30: the toolbar Excel export must respect whatever scope the list
    view's header button passed in (current filter via active_domain, or an
    explicit row selection via active_ids), and produce a real .xlsx with
    the documented columns."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['smart.reorder.export.wizard']
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        cls.vendor = cls.env['res.partner'].create({'name': 'Test Export Vendor'})
        cls.product_a = cls.env['product.product'].create({
            'name': 'Export Widget A', 'type': 'product', 'standard_price': 2.0,
            'default_code': 'EXP-A',
        })
        cls.product_b = cls.env['product.product'].create({
            'name': 'Export Widget B', 'type': 'product', 'standard_price': 3.0,
            'default_code': 'EXP-B',
        })
        common = {'company_id': cls.company.id, 'warehouse_id': cls.warehouse.id}
        cls.sugg_a = cls.Suggestion.create(dict(
            common, product_id=cls.product_a.id, urgency='critical', abc_class='A',
            demand_trend='up', qty_on_hand=-5.0, avg_monthly_demand=4.0,
            lead_time_months=1.0, suggested_reorder_qty=10.0, reorder_value=20.0,
            budget_rank=1, vendor_id=cls.vendor.id,
            last_sale_date=fields.Date.today(),
        ))
        cls.sugg_b = cls.Suggestion.create(dict(
            common, product_id=cls.product_b.id, urgency='ok', abc_class='C',
            demand_trend='stable', qty_on_hand=20.0, avg_monthly_demand=1.0,
            lead_time_months=1.0, suggested_reorder_qty=0.0, reorder_value=0.0,
            budget_rank=0,
        ))

    def test_export_domain_prefers_active_domain(self):
        wizard = self.Wizard.with_context(
            active_domain=[('id', '=', self.sugg_a.id)],
            active_ids=[self.sugg_a.id, self.sugg_b.id],
        ).create({})
        self.assertEqual(wizard._get_export_domain(), [('id', '=', self.sugg_a.id)])

    def test_export_domain_falls_back_to_active_ids(self):
        wizard = self.Wizard.with_context(active_ids=[self.sugg_b.id]).create({})
        self.assertEqual(wizard._get_export_domain(), [('id', 'in', [self.sugg_b.id])])

    def test_export_domain_defaults_to_everything(self):
        wizard = self.Wizard.create({})
        self.assertEqual(wizard._get_export_domain(), [])

    def test_record_count_reflects_domain(self):
        wizard = self.Wizard.with_context(
            active_domain=[('id', '=', self.sugg_a.id)]
        ).create({})
        self.assertEqual(wizard.record_count, 1)

    def test_export_produces_valid_xlsx_with_expected_columns_and_rows(self):
        wizard = self.Wizard.with_context(
            active_domain=[('id', 'in', [self.sugg_a.id, self.sugg_b.id])]
        ).create({})
        action = wizard.action_export()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertIn('download=true', action['url'])

        attachment_id = int(action['url'].split('/web/content/')[1].split('?')[0])
        attachment = self.env['ir.attachment'].browse(attachment_id)
        self.assertTrue(attachment.exists())
        self.assertEqual(
            attachment.mimetype,
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        wb = load_workbook(io.BytesIO(base64.b64decode(attachment.datas)))
        ws = wb.active
        header_row = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
        self.assertEqual(header_row, [
            'Part Number', 'Product Name', 'Category', 'Warehouse',
            'On Hand Qty', 'Avg Monthly Demand', 'Lead Time (Months)',
            'Suggested Reorder Qty', 'Reorder Value', 'Urgency', 'ABC Class',
            'Demand Trend', 'Budget Rank', 'Vendor', 'Last Sale Date',
        ])
        self.assertEqual(ws.max_row, 3, 'header + 2 data rows')

        data_rows = list(ws.iter_rows(min_row=2, max_row=3, values_only=True))
        part_numbers = {row[0] for row in data_rows}
        self.assertEqual(part_numbers, {'EXP-A', 'EXP-B'})

        row_a = next(row for row in data_rows if row[0] == 'EXP-A')
        self.assertEqual(row_a[1], 'Export Widget A')
        self.assertEqual(row_a[4], -5.0)
        self.assertEqual(row_a[9], 'Critical — Negative Stock')
        self.assertEqual(row_a[13], 'Test Export Vendor')

    def test_export_with_no_matching_records_raises(self):
        wizard = self.Wizard.with_context(active_domain=[('id', '=', -1)]).create({})
        with self.assertRaises(UserError):
            wizard.action_export()


@tagged('post_install', '-at_install')
class TestTransferLane(TransactionCase):
    """T-31: inter-warehouse transfer recommendations must use a per-pair lane's
    lead time when one is configured, and fall back to the company default
    (in days) when it isn't — instead of one global lead time for every pair."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Lane = cls.env['smart.reorder.transfer.lane']
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
            'default_lead_time_months': 2.0,   # → 60-day fallback
        })
        cls.wh_a = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        if not cls.wh_a:
            cls.wh_a = cls.env['stock.warehouse'].create({
                'name': 'Lane Test WH A', 'code': 'LWHA', 'company_id': cls.company.id,
            })
        cls.wh_b = cls.env['stock.warehouse'].create({
            'name': 'Lane Test WH B', 'code': 'LWHB', 'company_id': cls.company.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Test Transfer Lane Widget', 'type': 'product', 'standard_price': 1.0,
        })

    def _set_quant(self, warehouse, qty_delta):
        self.env['stock.quant']._update_available_quantity(
            self.product, warehouse.lot_stock_id, qty_delta
        )

    def _run(self):
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.wh_a.id, self.wh_b.id]
        )
        return self.Suggestion.search([
            ('product_id', '=', self.product.id),
            ('warehouse_id', '=', self.wh_a.id),
            ('company_id', '=', self.company.id),
        ])

    # ── Model-level constraints ──────────────────────────────────────────

    def test_lane_rejects_identical_source_and_destination(self):
        with self.assertRaises(ValidationError):
            self.Lane.create({
                'source_warehouse_id': self.wh_a.id,
                'dest_warehouse_id': self.wh_a.id,
                'lead_time_days': 3,
            })

    def test_lane_rejects_non_positive_lead_time(self):
        with self.assertRaises(ValidationError):
            self.Lane.create({
                'source_warehouse_id': self.wh_a.id,
                'dest_warehouse_id': self.wh_b.id,
                'lead_time_days': 0,
            })

    def test_lane_rejects_duplicate_pair(self):
        self.Lane.create({
            'source_warehouse_id': self.wh_a.id,
            'dest_warehouse_id': self.wh_b.id,
            'lead_time_days': 3,
        })
        with self.assertRaises(ValidationError):
            self.Lane.create({
                'source_warehouse_id': self.wh_a.id,
                'dest_warehouse_id': self.wh_b.id,
                'lead_time_days': 5,
            })

    # ── generate_suggestions() integration ───────────────────────────────

    def test_transfer_uses_lane_lead_time_when_configured(self):
        self.Lane.create({
            'source_warehouse_id': self.wh_b.id,
            'dest_warehouse_id': self.wh_a.id,
            'lead_time_days': 3,
        })
        self._set_quant(self.wh_a, -5.0)   # WH A: negative stock → critical
        self._set_quant(self.wh_b, 50.0)   # WH B: surplus, no sales → mos = 999

        suggestion = self._run()
        self.assertEqual(suggestion.urgency, 'critical')
        self.assertEqual(suggestion.transfer_source_warehouse_id, self.wh_b)
        self.assertEqual(suggestion.transfer_lead_time_days, 3, 'must use the lane, not the company default')
        self.assertIn('lane: Lane Test WH B', suggestion.notes)

    def test_transfer_falls_back_to_company_default_without_a_lane(self):
        # No lane created for this pair.
        self._set_quant(self.wh_a, -5.0)
        self._set_quant(self.wh_b, 50.0)

        suggestion = self._run()
        self.assertEqual(suggestion.transfer_source_warehouse_id, self.wh_b)
        self.assertEqual(
            suggestion.transfer_lead_time_days, 3,
            'no lane configured — must fall back to default_internal_transfer_days (3)'
        )
        self.assertIn('company default — no specific lane configured', suggestion.notes)

    def test_transfer_custom_internal_lead_time_fallback(self):
        # Set custom fallback
        self.config.write({
            'default_internal_transfer_days': 8
        })
        self._set_quant(self.wh_a, -5.0)
        self._set_quant(self.wh_b, 50.0)

        suggestion = self._run()
        self.assertEqual(suggestion.transfer_lead_time_days, 8)

    def test_donor_surplus_cap(self):
        """
        Donor warehouse has 20 units on hand, sells 10/month with a 1-month
        lead time → protected need = 10; safe surplus = 10.
        Destination needs 25 units.
        The transfer suggestion must be capped at min(10, 25) = 10.
        """
        self.config.write({
            'analysis_period': '6',
            'default_lead_time_months': 1.0,
            'transfer_surplus_threshold': 1.0,
        })
        self._set_quant(self.wh_a, -25.0)   # destination needs 25
        self._set_quant(self.wh_b, 20.0)   # donor has 20

        # Create sales history: 60 units in the last month on WH B (so demand = 10/mo)
        import datetime as _dt
        last_month = fields.Date.today().replace(day=1) - relativedelta(months=1)
        order_date = _dt.datetime.combine(last_month, _dt.time(12, 0))
        so = self.env['sale.order'].create({
            'partner_id': self.env['res.partner'].create({'name': 'WH-B Customer'}).id,
            'company_id': self.env.company.id,
            'warehouse_id': self.wh_b.id,
            'date_order': order_date,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 60.0,
                'price_unit': 1.0,
            })],
        })
        so.action_confirm()

        suggestion = self._run()
        self.assertEqual(
            suggestion.transfer_source_warehouse_id, self.wh_b,
            'WH-B should be chosen as the donor'
        )
        self.assertEqual(
            suggestion.transfer_suggested_qty, 10.0,
            'Transfer suggested qty should be capped at donor surplus of 10.0'
        )
        self.assertIn('══ INTERNAL TRANSFER ══', suggestion.notes)
        self.assertIn('Transfer recommended: 10.00 units from donor warehouse "Lane Test WH B"', suggestion.notes)


@tagged('post_install', '-at_install')
class TestCronFrequencySync(TransactionCase):
    """T-32: saving smart.reorder.config.cron_frequency must update the shared
    'Smart Reorder: Weekly Analysis' scheduled action's interval directly, so a
    manager never has to open Settings → Technical → Scheduled Actions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Config = cls.env['smart.reorder.config']
        cls.company = cls.env.company
        cls.cron = cls.env.ref('smart_reorder_advisor.cron_smart_reorder_weekly')

    def test_create_with_default_frequency_syncs_weekly(self):
        # Detune the cron first so the assertion proves the sync actually ran,
        # not that it just happened to already match.
        self.cron.write({'interval_number': 99, 'interval_type': 'months'})
        config = self.Config.create({'company_id': self.company.id})
        self.assertEqual(config.cron_frequency, 'weekly')
        self.assertEqual(self.cron.interval_number, 1)
        self.assertEqual(self.cron.interval_type, 'weeks')

    def test_write_biweekly_updates_cron_interval(self):
        config = self.Config.create({'company_id': self.company.id})
        config.write({'cron_frequency': 'biweekly'})
        self.assertEqual(self.cron.interval_number, 2)
        self.assertEqual(self.cron.interval_type, 'weeks')

    def test_write_monthly_updates_cron_interval(self):
        config = self.Config.create({'company_id': self.company.id})
        config.write({'cron_frequency': 'monthly'})
        self.assertEqual(self.cron.interval_number, 1)
        self.assertEqual(self.cron.interval_type, 'months')

    def test_unrelated_field_write_does_not_touch_cron(self):
        config = self.Config.create({'company_id': self.company.id})
        config.write({'cron_frequency': 'monthly'})
        self.cron.write({'interval_number': 1, 'interval_type': 'months'})  # known baseline

        config.write({'safety_buffer_months': 2.0})  # unrelated field

        self.assertEqual(self.cron.interval_number, 1)
        self.assertEqual(self.cron.interval_type, 'months')


@tagged('post_install', '-at_install')
class TestBulkSnooze(TransactionCase):
    """T-33: bulk snooze (triggered from the list view's Action menu on a
    checkbox selection) must snooze every selected record in one write, the
    same way the single-record buttons do for one record at a time."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )
        common = {'company_id': cls.company.id, 'warehouse_id': cls.warehouse.id}
        cls.suggestions = cls.Suggestion.create([
            dict(common, product_id=cls.env['product.product'].create({
                'name': f'Bulk Snooze Widget {i}', 'type': 'product', 'standard_price': 1.0,
            }).id, urgency='critical', qty_on_hand=-5.0, suggested_reorder_qty=5.0,
                 reorder_needed=True)
            for i in range(3)
        ])

    def test_bulk_snooze_7_sets_all_selected_records(self):
        self.suggestions.action_bulk_snooze_7()
        expected_until = fields.Date.today() + timedelta(days=7)
        for rec in self.suggestions:
            self.assertEqual(rec.snoozed_until, expected_until)
            self.assertFalse(rec.reorder_needed)
            self.assertIn('Bulk-snoozed 7 days', rec.snoozed_note)
            self.assertIn('3 suggestions', rec.snoozed_note)
        self.assertTrue(all(self.suggestions.mapped('is_snoozed')))

    def test_bulk_snooze_30_sets_all_selected_records(self):
        self.suggestions.action_bulk_snooze_30()
        expected_until = fields.Date.today() + timedelta(days=30)
        for rec in self.suggestions:
            self.assertEqual(rec.snoozed_until, expected_until)
            self.assertIn('Bulk-snoozed 30 days', rec.snoozed_note)

    def test_bulk_snooze_on_partial_selection_leaves_others_untouched(self):
        selected = self.suggestions[:2]
        untouched = self.suggestions[2]
        selected.action_bulk_snooze_7()
        self.assertTrue(all(selected.mapped('is_snoozed')))
        self.assertFalse(untouched.is_snoozed)
        self.assertTrue(untouched.reorder_needed)

    def test_bulk_snooze_on_empty_recordset_does_not_raise(self):
        empty = self.Suggestion.browse()
        empty.action_bulk_snooze_7()  # must be a silent no-op


@tagged('post_install', '-at_install')
class TestLastRunBanner(TransactionCase):
    """T-34: the 'Last Analysis' banner above the suggestions list (banner_route)
    must prefer the cron log (Feature 1.2) for an accurate run timestamp/status,
    and fall back gracefully when there's no run history yet."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ..controllers.main import SmartReorderBannerController
        cls.build_html = SmartReorderBannerController._build_banner_html
        cls.CronLog = cls.env['smart.reorder.cron.log']
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1
        )

    def test_no_history_at_all(self):
        html = self.build_html(self.env)
        self.assertIn('No analysis has been run yet', html)

    def test_falls_back_to_suggestion_analysis_date_without_cron_log(self):
        product = self.env['product.product'].create({
            'name': 'Banner Test Widget', 'type': 'product', 'standard_price': 1.0,
        })
        self.Suggestion.create({
            'company_id': self.company.id,
            'warehouse_id': self.warehouse.id,
            'product_id': product.id,
            'analysis_date': fields.Date.today(),
        })
        html = self.build_html(self.env)
        self.assertIn('no run history found yet', html)
        self.assertIn(str(fields.Date.today()), html)

    def test_completed_run_shows_info_banner(self):
        now = fields.Datetime.now()
        self.CronLog.create({
            'company_id': self.company.id,
            'started_at': now - timedelta(minutes=5),
            'finished_at': now,
            'trigger_type': 'cron',
            'status': 'completed',
        })
        html = self.build_html(self.env)
        self.assertIn('alert-info', html)
        self.assertIn('Last Analysis', html)
        self.assertNotIn('warnings', html)

    def test_completed_with_errors_shows_warning_banner(self):
        now = fields.Datetime.now()
        self.CronLog.create({
            'company_id': self.company.id,
            'started_at': now - timedelta(minutes=5),
            'finished_at': now,
            'trigger_type': 'cron',
            'status': 'completed_with_errors',
        })
        html = self.build_html(self.env)
        self.assertIn('alert-warning', html)
        self.assertIn('completed with some warnings', html)

    def test_most_recent_completed_run_wins(self):
        now = fields.Datetime.now()
        self.CronLog.create({
            'company_id': self.company.id,
            'started_at': now - timedelta(days=8),
            'finished_at': now - timedelta(days=8),
            'trigger_type': 'cron', 'status': 'completed',
        })
        recent = self.CronLog.create({
            'company_id': self.company.id,
            'started_at': now - timedelta(hours=1),
            'finished_at': now,
            'trigger_type': 'manual', 'status': 'completed',
        })
        html = self.build_html(self.env)
        local_dt = fields.Datetime.context_timestamp(self.env.user, recent.finished_at)
        self.assertIn(local_dt.strftime('%d %b %Y, %H:%M'), html)

    def test_running_status_is_ignored_in_favor_of_last_completed(self):
        now = fields.Datetime.now()
        completed = self.CronLog.create({
            'company_id': self.company.id,
            'started_at': now - timedelta(hours=2),
            'finished_at': now - timedelta(hours=1),
            'trigger_type': 'cron', 'status': 'completed',
        })
        self.CronLog.create({
            'company_id': self.company.id,
            'started_at': now,
            'trigger_type': 'cron', 'status': 'running',
        })
        html = self.build_html(self.env)
        local_dt = fields.Datetime.context_timestamp(self.env.user, completed.finished_at)
        self.assertIn(local_dt.strftime('%d %b %Y, %H:%M'), html)
        self.assertNotIn('No analysis has been run yet', html)


class TestProductSupersession(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.company = cls.env.company
        cls.config = cls.env['smart.reorder.config'].create({
            'company_id': cls.company.id,
            'analysis_period': '3',
            'safety_buffer_months': 0.0,
            'default_lead_time_months': 1.0,
        })
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)

        # Create predecessor and successor templates/products
        cls.pred_tmpl = cls.env['product.template'].create({
            'name': 'Old Part A',
            'type': 'product',
        })
        cls.succ_tmpl = cls.env['product.template'].create({
            'name': 'New Part B',
            'type': 'product',
        })
        cls.pred_tmpl.superseded_by_id = cls.succ_tmpl.id

        cls.pred_prod = cls.pred_tmpl.product_variant_id
        cls.succ_prod = cls.succ_tmpl.product_variant_id

        # Add sales to old part in previous completed month (so it enters the analysis window)
        cls.partner = cls.env['res.partner'].create({'name': 'Supersession Customer'})

        # Last month start
        last_month_start = date.today().replace(day=1) - relativedelta(months=1)
        order_date = datetime.combine(last_month_start + timedelta(days=5), datetime.min.time())

        cls.order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id,
            'company_id': cls.company.id,
            'warehouse_id': cls.warehouse.id,
            'date_order': order_date,
            'order_line': [(0, 0, {
                'product_id': cls.pred_prod.id,
                'product_uom_qty': 10.0,
                'price_unit': 10.0,
            })],
        })
        cls.order.action_confirm()
        cls.order.order_line.qty_delivered = 10.0

    def test_superseded_product_rollup_and_replenishment_exclusion(self):
        self.Suggestion.generate_suggestions(
            company_ids=[self.company.id], warehouse_ids=[self.warehouse.id]
        )

        pred_suggestion = self.Suggestion.search([
            ('product_id', '=', self.pred_prod.id),
            ('warehouse_id', '=', self.warehouse.id),
        ])
        succ_suggestion = self.Suggestion.search([
            ('product_id', '=', self.succ_prod.id),
            ('warehouse_id', '=', self.warehouse.id),
        ])

        self.assertTrue(pred_suggestion, "Predecessor suggestion should exist")
        self.assertTrue(succ_suggestion, "Successor suggestion should exist")

        # Predecessor assertions
        self.assertEqual(pred_suggestion.suggested_reorder_qty, 0.0)
        self.assertFalse(pred_suggestion.reorder_needed)
        self.assertEqual(pred_suggestion.urgency, 'dead')
        self.assertTrue(pred_suggestion.is_dead_stock)
        self.assertIn('SUPERSEDED: This part has been superseded', pred_suggestion.notes)

        # Successor assertions
        self.assertGreater(succ_suggestion.avg_monthly_demand, 0.0)
        self.assertIn('Demand includes rolled-up sales history from superseded parts', succ_suggestion.notes)
        self.assertIn('Old Part A', succ_suggestion.notes)

    def test_supersession_cycle_guard(self):
        # Direct self-reference
        with self.assertRaises(ValidationError):
            self.pred_tmpl.superseded_by_id = self.pred_tmpl.id

        # Loop: A -> B -> A
        with self.assertRaises(ValidationError):
            self.succ_tmpl.superseded_by_id = self.pred_tmpl.id



class TestReorderConfigOrderCycle(TransactionCase):

    def test_order_cycle_derivation_and_manual_authoritativeness(self):
        config = self.env['smart.reorder.config'].create({
            'company_id': self.env.company.id,
            'cron_frequency': 'weekly',
        })
        # Default derived value should be 0.25 (weekly)
        self.assertEqual(config.order_cycle_months, 0.25)

        # Change frequency to biweekly -> updates to 0.5
        config.cron_frequency = 'biweekly'
        self.assertEqual(config.order_cycle_months, 0.5)

        # Change frequency to monthly -> updates to 1.0
        config.cron_frequency = 'monthly'
        self.assertEqual(config.order_cycle_months, 1.0)

        # Set a custom manual value -> manual value survives
        config.order_cycle_months = 2.5
        self.assertEqual(config.order_cycle_months, 2.5)

        # Changing frequency again with custom manual value -> manual value still survives
        config.cron_frequency = 'weekly'
        self.assertEqual(config.order_cycle_months, 2.5)


class TestReorderOverstockGuard(TransactionCase):

    def test_overstock_guard_trigger_and_flagging(self):
        company = self.env.company
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].create({
                'name': 'Test WH',
                'code': 'TWH',
                'company_id': company.id,
            })
            
        product = self.env['product.product'].create({
            'name': 'Overstock Test Part',
            'type': 'product',
        })
        
        config = self.env['smart.reorder.config'].search([('company_id', '=', company.id)], limit=1)
        if not config:
            config = self.env['smart.reorder.config'].create({
                'company_id': company.id,
                'overstock_ceiling_months': 10.0,
            })
        else:
            config.write({'overstock_ceiling_months': 10.0})

        supplier = self.env['res.partner'].create({'name': 'Giant MOQ Vendor'})
        self.env['product.supplierinfo'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'partner_id': supplier.id,
            'min_qty': 100.0,
            'price': 10.0,
        })

        sale_date = date(2026, 6, 15)
        so = self.env['sale.order'].create({
            'partner_id': supplier.id,
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'date_order': sale_date,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 6.0,
                'price_unit': 20.0,
            })],
        })
        so.action_confirm()

        self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=[company.id], warehouse_ids=[warehouse.id]
        )

        suggestion = self.env['smart.reorder.suggestion'].search([
            ('product_id', '=', product.id),
            ('warehouse_id', '=', warehouse.id),
        ])
        
        self.assertTrue(suggestion, "Suggestion should be generated")
        self.assertTrue(suggestion.is_overstocked)
        self.assertTrue(suggestion.needs_review)
        self.assertIn("Vendor MOQ forces", suggestion.needs_review_reason)
        self.assertIn("months of cover", suggestion.needs_review_reason)


class TestReorderBudgetCapVendorPrice(TransactionCase):

    def test_budget_cap_valued_at_vendor_price(self):
        company = self.env.company
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].create({
                'name': 'Test WH',
                'code': 'TWH',
                'company_id': company.id,
            })

        product = self.env['product.product'].create({
            'name': 'Budget Test Part',
            'type': 'product',
            'standard_price': 5.0,
        })

        supplier = self.env['res.partner'].create({'name': 'Vendor Price supplier'})
        self.env['product.supplierinfo'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'partner_id': supplier.id,
            'price': 10.0,
            'min_qty': 1.0,
        })

        config = self.env['smart.reorder.config'].search([('company_id', '=', company.id)], limit=1)
        if not config:
            config = self.env['smart.reorder.config'].create({
                'company_id': company.id,
                'budget_cap': 20.0,
            })
        else:
            config.write({'budget_cap': 20.0})

        sale_date = date(2026, 6, 15)
        so = self.env['sale.order'].create({
            'partner_id': supplier.id,
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'date_order': sale_date,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 6.0,
                'price_unit': 20.0,
            })],
        })
        so.action_confirm()

        self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=[company.id], warehouse_ids=[warehouse.id]
        )

        suggestion = self.env['smart.reorder.suggestion'].search([
            ('product_id', '=', product.id),
            ('warehouse_id', '=', warehouse.id),
        ])
        self.assertTrue(suggestion, "Suggestion should be generated")
        self.assertEqual(suggestion.reorder_value, 15.0)
        self.assertEqual(suggestion.estimated_purchase_value, 30.0)
        self.assertFalse(suggestion.within_budget, "Should exceed budget when valued at vendor price (30.0 > 20.0)")


class TestReorderPriceBreaksAltVendors(TransactionCase):

    def test_price_breaks_and_alt_vendors(self):
        company = self.env.company
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].create({
                'name': 'Test WH',
                'code': 'TWH',
                'company_id': company.id,
            })

        config = self.env['smart.reorder.config'].search([('company_id', '=', company.id)], limit=1)
        if not config:
            config = self.env['smart.reorder.config'].create({
                'company_id': company.id,
                'alt_vendor_lead_margin_days': 5,
            })
        else:
            config.write({'alt_vendor_lead_margin_days': 5})

        product = self.env['product.product'].create({
            'name': 'Price Break Part',
            'type': 'product',
            'standard_price': 5.0,
        })

        primary_vendor = self.env['res.partner'].create({'name': 'Primary Vendor'})
        alt_vendor = self.env['res.partner'].create({'name': 'Alternative Vendor'})

        self.env['product.supplierinfo'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'partner_id': primary_vendor.id,
            'price': 10.0,
            'min_qty': 1.0,
            'delay': 30,
            'sequence': 10,
        })
        self.env['product.supplierinfo'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'partner_id': primary_vendor.id,
            'price': 8.0,
            'min_qty': 50.0,
            'delay': 25,
            'sequence': 10,
        })

        self.env['product.supplierinfo'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'partner_id': alt_vendor.id,
            'price': 12.0,
            'min_qty': 1.0,
            'delay': 15,
            'sequence': 20,
        })

        sale_date = date(2026, 6, 15)
        so = self.env['sale.order'].create({
            'partner_id': primary_vendor.id,
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'date_order': sale_date,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 20.0,
                'price_unit': 20.0,
            })],
        })
        so.action_confirm()

        self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=[company.id], warehouse_ids=[warehouse.id]
        )

        suggestion = self.env['smart.reorder.suggestion'].search([
            ('product_id', '=', product.id),
            ('warehouse_id', '=', warehouse.id),
        ])
        self.assertTrue(suggestion, "Suggestion should be generated")
        self.assertEqual(suggestion.vendor_price, 10.0)
        self.assertEqual(suggestion.alt_vendor_id.id, alt_vendor.id)
        self.assertEqual(suggestion.alt_vendor_lead_days, 15)
        self.assertIn("Alternative vendor available: Alternative Vendor", suggestion.notes)
        self.assertIn("can deliver in 15 days", suggestion.notes)

        so_large = self.env['sale.order'].create({
            'partner_id': primary_vendor.id,
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'date_order': sale_date,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 120.0,
                'price_unit': 20.0,
            })],
        })
        so_large.action_confirm()

        self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=[company.id], warehouse_ids=[warehouse.id]
        )
        suggestion.invalidate_recordset()
        suggestion = self.env['smart.reorder.suggestion'].search([
            ('product_id', '=', product.id),
            ('warehouse_id', '=', warehouse.id),
        ])
        self.assertEqual(suggestion.vendor_price, 8.0)
        self.assertEqual(suggestion.alt_vendor_id.id, alt_vendor.id)
        self.assertEqual(suggestion.alt_vendor_lead_days, 15)

        picking = self.env['stock.picking'].create({
            'picking_type_id': self.env['stock.picking.type'].search([('code', '=', 'incoming'), ('warehouse_id', '=', warehouse.id)], limit=1).id,
            'location_id': self.env.ref('stock.stock_location_suppliers').id,
            'location_dest_id': warehouse.lot_stock_id.id,
            'company_id': company.id,
        })
        move = self.env['stock.move'].create({
            'name': 'Test Move',
            'product_id': product.id,
            'product_uom_qty': 500.0,
            'product_uom': product.uom_id.id,
            'location_id': picking.location_id.id,
            'location_dest_id': picking.location_dest_id.id,
            'picking_id': picking.id,
        })
        picking.action_confirm()
        picking.action_assign()
        move.quantity = 500.0
        picking.button_validate()

        self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=[company.id], warehouse_ids=[warehouse.id]
        )
        suggestion.invalidate_recordset()
        suggestion = self.env['smart.reorder.suggestion'].search([
            ('product_id', '=', product.id),
            ('warehouse_id', '=', warehouse.id),
        ])
        
        self.assertEqual(suggestion.qty_on_hand, 500.0)
        self.assertEqual(suggestion.suggested_reorder_qty, 0.0)
        self.assertNotIn("Alternative vendor available", suggestion.notes or "")


class TestReorderProvisionalNegativeStock(TransactionCase):

    def test_provisional_negative_stock_flag(self):
        company = self.env.company
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].create({
                'name': 'Test WH',
                'code': 'TWH',
                'company_id': company.id,
            })

        config = self.env['smart.reorder.config'].search([('company_id', '=', company.id)], limit=1)
        if not config:
            config = self.env['smart.reorder.config'].create({
                'company_id': company.id,
                'auto_flag_on_negative': True,
            })
        else:
            config.write({'auto_flag_on_negative': True})

        product = self.env['product.product'].create({
            'name': 'Provisional Test Part',
            'type': 'product',
            'standard_price': 5.0,
        })

        supplier = self.env['res.partner'].create({'name': 'Provisional supplier'})
        self.env['product.supplierinfo'].create({
            'product_tmpl_id': product.product_tmpl_id.id,
            'partner_id': supplier.id,
            'price': 10.0,
            'min_qty': 10.0,  # MOQ = 10
            'delay': 30,
        })

        # Set stock negative: let's mock it by editing standard stock quants or manually driving negative.
        # But we can also simulate the flag call directly:
        # Since flag_negative_stock_product reads quants, we create a quant with quantity -3.0
        # In Odoo, negative quants represent negative stock.
        self.env['stock.quant'].create({
            'product_id': product.id,
            'location_id': warehouse.lot_stock_id.id,
            'quantity': -3.0,
        })

        # 1. No prior suggestion exists
        self.env['smart.reorder.suggestion'].flag_negative_stock_product(
            product.id, warehouse.id, company.id
        )

        suggestion = self.env['smart.reorder.suggestion'].search([
            ('product_id', '=', product.id),
            ('warehouse_id', '=', warehouse.id),
        ])
        self.assertTrue(suggestion, "Suggestion should be generated")
        self.assertTrue(suggestion.is_provisional, "Should be marked as provisional")
        self.assertEqual(suggestion.qty_on_hand, -3.0)
        # Suggested qty should be absolute negative qty (3.0) rounded to MOQ (10) -> 10
        self.assertEqual(suggestion.suggested_reorder_qty, 10.0)

        # 2. Existing suggestion exists
        # Update suggestion to simulate a previous analysis run that calculated some demand
        suggestion.write({
            'avg_monthly_demand': 15.0,
            'lead_time_months': 1.0,
            'safety_buffer_months': 1.0,
            'order_cycle_months': 1.0,
            'moq': 10.0,
            'qty_incoming': 0.0,
            'qty_outgoing': 0.0,
            'is_provisional': False,
        })

        # Let's call the hook again with a different negative qty (e.g. -5.0)
        self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', '=', warehouse.lot_stock_id.id),
        ]).write({'quantity': -5.0})

        self.env['smart.reorder.suggestion'].flag_negative_stock_product(
            product.id, warehouse.id, company.id
        )

        suggestion.invalidate_recordset()
        self.assertTrue(suggestion.is_provisional, "Should be flagged provisional again")
        self.assertEqual(suggestion.qty_on_hand, -5.0)
        # Calculations:
        # avg_monthly = 15.0, min_level = 15 * (1 + 1) = 30
        # max_level = 30 + 15 * 1 = 45
        # raw_qty = 45 - (-5) = 50. MOQ = 10, so rounded = 50.
        # Max of 50 and abs(-5) rounded to 10 (10) is 50.
        self.assertEqual(suggestion.suggested_reorder_qty, 50.0)

        # 3. Next full analysis clears the provisional flag
        # We need to run full analysis. Let's create a sale order to simulate sales history first
        so = self.env['sale.order'].create({
            'partner_id': supplier.id,
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'date_order': date(2026, 6, 15),
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 12.0,
                'price_unit': 20.0,
            })],
        })
        so.action_confirm()

        self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=[company.id], warehouse_ids=[warehouse.id]
        )

        suggestion.invalidate_recordset()
        self.assertFalse(suggestion.is_provisional, "Provisional flag should be cleared by full analysis")
        # Numbers should be replaced with actual forecast numbers
        # avg_monthly = 2.0 (since 12 / 6 = 2)
        # Net available = -5.0. Min level = 2 * (1 + 1) = 4
        # max_level = 4 + 2 * 0.25 = 4.5
        # raw_qty = 4.5 - (-5.0) = 9.5 -> rounded to MOQ = 10.
        self.assertEqual(suggestion.suggested_reorder_qty, 10.0)
        self.assertEqual(suggestion.avg_monthly_demand, 2.0)


class TestReorderForecastBacktesting(TransactionCase):

    def test_forecast_backtesting_flow(self):
        company = self.env.company
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        if not warehouse:
            warehouse = self.env['stock.warehouse'].create({
                'name': 'Test WH',
                'code': 'TWH',
                'company_id': company.id,
            })

        # Set retention configuration
        config = self.env['smart.reorder.config'].search([('company_id', '=', company.id)], limit=1)
        if not config:
            config = self.env['smart.reorder.config'].create({
                'company_id': company.id,
                'snapshot_retention_months': 3,
            })
        else:
            config.write({'snapshot_retention_months': 3})

        product = self.env['product.product'].create({
            'name': 'Backtest Test Product',
            'type': 'product',
            'standard_price': 10.0,
        })

        # 1. Create snapshots manually to simulate old runs (older than 1 lead time)
        # Snapshot date: 35 days ago, lead time: 30 days
        snap_date_old = date.today() - timedelta(days=35)
        old_snap = self.env['smart.reorder.forecast.snapshot'].create({
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'product_id': product.id,
            'snapshot_date': snap_date_old,
            'forecast_demand': 10.0,  # 10 units/month forecast
            'confidence': 85.0,
            'lead_time_days': 30,
            'abc_class': 'A',
            'evaluated': False,
        })

        # Snapshot date: 15 days ago, lead time: 30 days (too new to evaluate)
        snap_date_new = date.today() - timedelta(days=15)
        new_snap = self.env['smart.reorder.forecast.snapshot'].create({
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'product_id': product.id,
            'snapshot_date': snap_date_new,
            'forecast_demand': 5.0,
            'confidence': 90.0,
            'lead_time_days': 30,
            'abc_class': 'B',
            'evaluated': False,
        })

        # Snapshot date: 120 days ago (older than 3 months retention)
        snap_date_expired = date.today() - timedelta(days=120)
        expired_snap = self.env['smart.reorder.forecast.snapshot'].create({
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'product_id': product.id,
            'snapshot_date': snap_date_expired,
            'forecast_demand': 20.0,
            'confidence': 70.0,
            'lead_time_days': 30,
            'abc_class': 'C',
            'evaluated': True,
        })

        # 2. Record sales during the lead time period for the old snapshot
        # Period: [today-35, today-5]
        # Let's say we sold 8 units
        so = self.env['sale.order'].create({
            'partner_id': self.env['res.partner'].create({'name': 'Test Cust'}).id,
            'company_id': company.id,
            'warehouse_id': warehouse.id,
            'date_order': date.today() - timedelta(days=20),
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 8.0,
                'price_unit': 15.0,
            })],
        })
        so.action_confirm()
        # Deliver the units
        for line in so.order_line:
            line.qty_delivered = 8.0

        # 3. Score Snapshots
        self.env['smart.reorder.forecast.snapshot']._score_snapshots()

        old_snap.invalidate_recordset()
        new_snap.invalidate_recordset()

        self.assertTrue(old_snap.evaluated, "Old snapshot should be evaluated")
        self.assertFalse(new_snap.evaluated, "New snapshot should not be evaluated yet")

        # Calculations:
        # Forecast demand = 10.0 monthly. Period = 30 days -> forecasted qty = 10 * (30/30) = 10.0 units.
        # Actual sales = 8.0 units.
        # APE = abs(10.0 - 8.0) / 10.0 = 0.2 -> 20.0%
        self.assertEqual(old_snap.actual_sales, 8.0)
        self.assertEqual(old_snap.absolute_error_pct, 20.0)

        # 4. Verify retention settings pruning on generate suggestions
        # Create suggestion to run generation
        self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=[company.id], warehouse_ids=[warehouse.id]
        )

        # The expired snapshot (120 days ago) should be deleted by the retention purge
        still_exists = self.env['smart.reorder.forecast.snapshot'].search([('id', '=', expired_snap.id)])
        self.assertFalse(still_exists, "Expired snapshot should have been deleted")

        # 5. Dashboard aggregation check
        dashboard_data = self.env['smart.reorder.suggestion'].get_dashboard_data(warehouse_id=warehouse.id)
        self.assertIn('backtest', dashboard_data)
        backtest_res = dashboard_data['backtest']
        self.assertEqual(backtest_res['overall_mape'], 20.0)
        self.assertEqual(backtest_res['mape_by_abc']['A'], 20.0)


@tagged('post_install', '-at_install')
class TestReorderPureFunction(TransactionCase):
    """
    Direct unit tests for the pure per-product calculation function `_calculate_product_suggestion`.
    These pass plain Python datatypes to ensure correct formula application without DB calls.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Suggestion = cls.env['smart.reorder.suggestion']
        cls.config_data = {
            'default_lead_time_months': 1.0,
            'track_vendor_performance': False,
            'dead_stock_months': 6,
            'flag_dead_stock': True,
            'safety_buffer_months': 1.0,
            'order_cycle_months': 2.0,
            'alt_vendor_lead_margin_days': 5,
            'abc_a_threshold': 5.0,
            'abc_b_threshold': 1.0,
            'overstock_ceiling_months': 12.0,
            'transfer_surplus_threshold': 6.0,
            'default_internal_transfer_days': 3,
        }
        cls.dates = {
            'date_from': date.today() - timedelta(days=90),
            'date_to': date.today(),
            'analysis_months': 3.0,
            'comparison_months': 3.0,
            'month_starts': [
                date.today() - timedelta(days=30),
                date.today() - timedelta(days=60),
                date.today() - timedelta(days=90),
            ],
        }

    def test_pure_function_normal_part(self):
        # Normal part: monthly_series = [10.0, 10.0, 10.0] -> avg = 10.0.
        # On hand = 5.0. Incoming = 0. Outgoing = 0.
        # Lead time = 1 month (30 days stated). Safety buffer = 1 month. Order cycle = 2 months.
        # Min level = 10.0 * (1 + 1) = 20.0.
        # Max level = 20.0 + 10.0 * 2 = 40.0.
        # Triggered because qty_available (5.0) < min_level (20.0).
        # Raw reorder qty = 40.0 - 5.0 = 35.0.
        # MOQ = 10.0 -> Suggested qty = 40.0 (rounded up from 35.0 to multiple of 10).
        res = self.Suggestion._calculate_product_suggestion(
            product_id=1,
            product_code='NORM',
            product_name='Normal Part',
            tmpl_id=10,
            is_superseded=False,
            successor_display_name=False,
            company_id=1,
            warehouse_id=1,
            dates=self.dates,
            config_data=self.config_data,
            warehouses_list=[{'id': 1, 'name': 'WH 1'}],
            monthly_series=[10.0, 10.0, 10.0],
            qty_on_hand=5.0,
            qty_incoming=0.0,
            qty_outgoing=0.0,
            cost=1.5,
            last_sale=date.today(),
            current_month_sales=0.0,
            prev_qty=30.0,
            ly_qty=30.0,
            tmpl_suppliers=[],
            primary_vendor_info=(101, 30.0, 10.0, 5.0, False), # partner_id, delay, min_qty, price, currency_id
            actual_avg_days=0.0,
            overdue_lines=[],
            predecessors=set(),
            predecessor_names=[],
            global_stocks={1: 5.0},
            global_sales={1: 30.0},
            lane_lead_times={},
            partner_names_map={101: 'Primary Vendor'},
            currency_convert_fn=lambda price, currency_id: price
        )
        self.assertEqual(res['suggested_reorder_qty'], 40.0)
        self.assertEqual(res['min_stock_level'], 20.0)
        self.assertEqual(res['max_stock_level'], 40.0)
        self.assertEqual(res['urgency'], 'urgent')
        self.assertEqual(res['abc_class'], 'A')

    def test_pure_function_negative_stock_part(self):
        # Negative-stock part: on hand = -5.0.
        # Even with zero demand and zero forecast, negative stock triggers reorder.
        res = self.Suggestion._calculate_product_suggestion(
            product_id=2,
            product_code='NEG',
            product_name='Negative Stock Part',
            tmpl_id=11,
            is_superseded=False,
            successor_display_name=False,
            company_id=1,
            warehouse_id=1,
            dates=self.dates,
            config_data=self.config_data,
            warehouses_list=[{'id': 1, 'name': 'WH 1'}],
            monthly_series=[0.0, 0.0, 0.0],
            qty_on_hand=-5.0,
            qty_incoming=0.0,
            qty_outgoing=0.0,
            cost=1.5,
            last_sale=date.today() - timedelta(days=200),
            current_month_sales=0.0,
            prev_qty=0.0,
            ly_qty=0.0,
            tmpl_suppliers=[],
            primary_vendor_info=False,
            actual_avg_days=0.0,
            overdue_lines=[],
            predecessors=set(),
            predecessor_names=[],
            global_stocks={1: -5.0},
            global_sales={1: 0.0},
            lane_lead_times={},
            partner_names_map={},
            currency_convert_fn=lambda price, currency_id: price
        )
        self.assertEqual(res['urgency'], 'critical')
        self.assertGreater(res['suggested_reorder_qty'], 0.0)

    def test_pure_function_dead_part(self):
        # Dead part: no sales in dates or last_sale is very old.
        # Config dead_stock_months is 6, last_sale is 300 days ago.
        res = self.Suggestion._calculate_product_suggestion(
            product_id=3,
            product_code='DEAD',
            product_name='Dead Part',
            tmpl_id=12,
            is_superseded=False,
            successor_display_name=False,
            company_id=1,
            warehouse_id=1,
            dates=self.dates,
            config_data=self.config_data,
            warehouses_list=[{'id': 1, 'name': 'WH 1'}],
            monthly_series=[0.0, 0.0, 0.0],
            qty_on_hand=10.0,
            qty_incoming=0.0,
            qty_outgoing=0.0,
            cost=1.5,
            last_sale=date.today() - timedelta(days=300),
            current_month_sales=0.0,
            prev_qty=0.0,
            ly_qty=0.0,
            tmpl_suppliers=[],
            primary_vendor_info=False,
            actual_avg_days=0.0,
            overdue_lines=[],
            predecessors=set(),
            predecessor_names=[],
            global_stocks={1: 10.0},
            global_sales={1: 0.0},
            lane_lead_times={},
            partner_names_map={},
            currency_convert_fn=lambda price, currency_id: price
        )
        self.assertTrue(res['is_dead_stock'])
        self.assertEqual(res['urgency'], 'dead')

    def test_pure_function_transfer_eligible_part(self):
        # Transfer eligible part: urgent/critical in WH 1, but WH 2 has surplus stock (mos_other > 6.0).
        # WH 2 stock: 100.0, demand in WH 2: 1.0 (sold_other = 3.0 / 3 months = 1.0 units/month).
        # mos_other = 100.0 / 1.0 = 100.0 (> 6.0).
        # Expect transfer source warehouse to be WH 2.
        res = self.Suggestion._calculate_product_suggestion(
            product_id=4,
            product_code='TRANS',
            product_name='Transfer Eligible Part',
            tmpl_id=13,
            is_superseded=False,
            successor_display_name=False,
            company_id=1,
            warehouse_id=1,
            dates=self.dates,
            config_data=self.config_data,
            warehouses_list=[{'id': 1, 'name': 'WH 1'}, {'id': 2, 'name': 'WH 2'}],
            monthly_series=[10.0, 10.0, 10.0],
            qty_on_hand=5.0,
            qty_incoming=0.0,
            qty_outgoing=0.0,
            cost=1.5,
            last_sale=date.today(),
            current_month_sales=0.0,
            prev_qty=30.0,
            ly_qty=30.0,
            tmpl_suppliers=[],
            primary_vendor_info=False,
            actual_avg_days=0.0,
            overdue_lines=[],
            predecessors=set(),
            predecessor_names=[],
            global_stocks={1: 5.0, 2: 100.0},
            global_sales={1: 30.0, 2: 3.0}, # WH 2 sold 3 units total -> 1.0 avg
            lane_lead_times={2: 5},
            partner_names_map={},
            currency_convert_fn=lambda price, currency_id: price
        )
        self.assertEqual(res['transfer_source_warehouse_id'], 2)
        self.assertEqual(res['transfer_lead_time_days'], 5)

    def test_pure_function_overdue_po_lines(self):
        """
        Regression test for the datetime-vs-date comparison bug fixed in
        _calculate_product_suggestion:  oldest_dt = min(...).date()

        Previously oldest_dt held a datetime.datetime object and
        `date.today() - oldest_dt` raised TypeError.  This test passes an
        overdue PO line (quantity overdue, delivery 10 days in the past) and
        asserts:
          1. The function returns without raising.
          2. The notes string contains the OVERDUE SUPPLY marker.
        """
        overdue_dt = datetime.now() - timedelta(days=10)
        res = self.Suggestion._calculate_product_suggestion(
            product_id=5,
            product_code='OVER',
            product_name='Overdue PO Part',
            tmpl_id=14,
            is_superseded=False,
            successor_display_name=False,
            company_id=1,
            warehouse_id=1,
            dates=self.dates,
            config_data=self.config_data,
            warehouses_list=[{'id': 1, 'name': 'WH 1'}],
            monthly_series=[5.0, 5.0, 5.0],
            qty_on_hand=2.0,
            qty_incoming=8.0,   # an open PO line — overdue
            qty_outgoing=0.0,
            cost=2.0,
            last_sale=date.today(),
            current_month_sales=0.0,
            prev_qty=15.0,
            ly_qty=15.0,
            tmpl_suppliers=[],
            primary_vendor_info=(102, 30.0, 5.0, 4.0, False),
            actual_avg_days=0.0,
            overdue_lines=[(8.0, overdue_dt)],   # <-- triggers the fixed code path
            predecessors=set(),
            predecessor_names=[],
            global_stocks={1: 2.0},
            global_sales={1: 15.0},
            lane_lead_times={},
            partner_names_map={102: 'Overdue Vendor'},
            currency_convert_fn=lambda price, currency_id: price
        )
        # Must not raise; basic sanity checks
        self.assertIsNotNone(res)
        # The overdue block must have been entered and written into notes
        self.assertIn('OVERDUE SUPPLY', res.get('notes', ''))


@tagged('post_install', '-at_install')
class TestReorderObservability(TransactionCase):

    def test_observability_dashboard_and_hook_errors(self):
        company = self.env.company
        config = self.env['smart.reorder.config'].create({
            'company_id': company.id,
        })
        self.assertEqual(config.picking_hook_error_count, 0)

        # Increment picking hook error count
        config.write({'picking_hook_error_count': 5})

        # Create dummy cron log records
        CronLog = self.env['smart.reorder.cron.log']
        
        # Log 1: Completed
        CronLog.create({
            'company_id': company.id,
            'started_at': fields.Datetime.now() - timedelta(hours=2),
            'finished_at': fields.Datetime.now() - timedelta(hours=1, minutes=58),
            'status': 'completed',
        })
        # Log 2: Completed with Errors
        CronLog.create({
            'company_id': company.id,
            'started_at': fields.Datetime.now() - timedelta(hours=1),
            'finished_at': fields.Datetime.now() - timedelta(minutes=58),
            'status': 'completed_with_errors',
        })

        # Compute dashboard stats
        dashboard = self.env['smart.reorder.observability.dashboard'].create({
            'company_id': company.id,
        })
        self.assertEqual(dashboard.total_runs, 2)
        self.assertEqual(dashboard.failed_runs, 1)
        # failure_rate_30_days is 0.5 (1/2) for percentage widget format
        self.assertEqual(dashboard.failure_rate_30_days, 0.5)
        self.assertEqual(dashboard.picking_hook_errors, 5)


@tagged('post_install', '-at_install')
class TestReorderConfigConstraints(TransactionCase):

    def test_negative_constraints(self):
        company = self.env.company
        # Create a new config bypass constraint checks on creation or write
        config = self.env['smart.reorder.config'].create({
            'company_id': company.id,
        })
        
        # Test negative transfer_surplus_threshold raises ValidationError
        with self.assertRaises(ValidationError):
            config.write({'transfer_surplus_threshold': -1.0})
            
        # Test negative default_internal_transfer_days raises ValidationError
        with self.assertRaises(ValidationError):
            config.write({'default_internal_transfer_days': -5})


