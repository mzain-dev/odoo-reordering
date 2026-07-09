from odoo import models, fields, api, _, Command
from odoo.exceptions import AccessError, UserError
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from psycopg2 import IntegrityError
import logging
import time

from ..utils.access import require_group

_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'
_USER_GROUP = 'smart_reorder_advisor.group_smart_reorder_user'

_logger = logging.getLogger(__name__)


class SmartReorderSuggestion(models.Model):
    """
    Smart Reorder Suggestion — one record per product per warehouse.

    FIXES IN THIS VERSION (v2.1):
    ─────────────────────────────────────────────────────────────────
    FIX 1  — urgency_rank NameError: variable is now assigned before use
    FIX 2  — attrs= removed: all views use Odoo 17 inline boolean syntax
    FIX 3  — Vendor info batched: one read_group before loop, O(1) lookup
    FIX 4  — Trend & seasonal batched: two read_groups before loop
    FIX 5  — Upsert batched: existing records pre-fetched into dict before loop
    FIX 6  — Stale data purged: old records deleted before fresh write
    FIX 7  — last_sale read_group fixed: uses max_date__order workaround
    FIX 8  — Vendor perf removed from loop: available via on-demand button only
    ─────────────────────────────────────────────────────────────────
    """
    _name = 'smart.reorder.suggestion'
    _inherit = ['mail.thread']
    _description = 'Smart Reorder Suggestion'
    _order = 'urgency_rank asc, reorder_value desc'
    _rec_name = 'product_id'

    _sql_constraints = [
        ('product_warehouse_company_uniq',
         'UNIQUE(product_id, warehouse_id, company_id)',
         'A suggestion already exists for this product/warehouse/company combination.'),
    ]

    # ── Identity ──────────────────────────────────────────────────────────────
    active = fields.Boolean(default=True, help='Set to false instead of deleting, to preserve suggestion history.')
    is_stale = fields.Boolean(
        string='Stale Data?', default=False, readonly=True, index=True,
        help='True when the last batch for this warehouse failed (query/DB error) and '
             'this record was left untouched instead of being refreshed or removed. '
             'Cleared automatically the next time a batch for this warehouse succeeds.'
    )
    stale_reason = fields.Char(
        string='Stale Reason', readonly=True,
        help='Plain-language explanation of why this record could not be refreshed.'
    )
    company_id = fields.Many2one(
        'res.company', string='Company / Branch',
        required=True, index=True, default=lambda self: self.env.company,
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse', required=True, index=True,
    )
    product_id = fields.Many2one(
        'product.product', string='Product', required=True, index=True,
    )
    product_categ_id = fields.Many2one(
        related='product_id.categ_id', string='Category', store=True,
    )
    default_code = fields.Char(
        related='product_id.default_code', string='Part Number', store=True,
    )
    product_cost = fields.Float(
        string='Unit Cost', digits=(16, 3), readonly=True,
    )

    # ── Analysis Window ───────────────────────────────────────────────────────
    analysis_date   = fields.Date(string='Analysis Date', default=fields.Date.today, readonly=True)
    analysis_months = fields.Integer(string='Analysis Window (Months)', readonly=True)
    date_from       = fields.Date(string='Period From', readonly=True)
    date_to         = fields.Date(string='Period To', readonly=True)

    # ── Monthly Demand ────────────────────────────────────────────────────────
    total_qty_sold      = fields.Float(string='Total Qty Sold (Period)', digits=(16, 2), readonly=True)
    avg_monthly_demand  = fields.Float(string='Avg Monthly Demand',      digits=(16, 2), readonly=True,
                                        help='Monthly average with spike rejection forecast — not a plain average. '
                                             'See Outlier Months Excluded / Demand Forecast Note.')
    excluded_outlier_months = fields.Integer(
        string='Outlier Months Excluded', readonly=True,
        help='How many of the monthly sales buckets were excluded as statistical outliers '
             '(value above the median by more than 3× the median absolute deviation) before forecasting.'
    )
    demand_forecast_note = fields.Char(
        string='Demand Forecast Note', readonly=True,
        help='Plain-language explanation of the monthly average with spike rejection for this forecast.'
    )
    confidence = fields.Float(
        string='Confidence Score', digits=(16, 1), readonly=True, index=True,
        help='0-100 score for how much to trust this forecast, based on data quality: '
             'zero-sales months, outlier months excluded, and how little sales history '
             'the product has overall. Starts at 100 and deducts points per weakness.'
    )
    lead_time_months    = fields.Float(string='Lead Time (Months)',       digits=(16, 2), readonly=True)
    safety_buffer_months= fields.Float(string='Safety Buffer (Months)',   digits=(16, 2), readonly=True)
    order_cycle_months  = fields.Float(string='Order Cycle (Months)',     digits=(16, 2), readonly=True,
                                        help='Snapshot of the order cycle config at analysis time.')
    min_stock_level     = fields.Float(string='Min Stock Level',          digits=(16, 0), readonly=True,
                                        help='Min Stock Level (Reorder Point): average monthly demand × (lead time months + safety buffer months).')
    max_stock_level     = fields.Float(string='Max Stock Level',          digits=(16, 0), readonly=True,
                                        help='Max Stock Level (Order-up-to Level): Min Stock Level + average monthly demand × order cycle.')
    moq                 = fields.Float(string='Min Order Qty (MOQ)',      digits=(16, 0), readonly=True)

    # ── Trend (Phase 2) ───────────────────────────────────────────────────────
    prev_period_qty_sold = fields.Float(string='Prev Period Qty Sold', digits=(16, 2), readonly=True)
    trend_comparison_months = fields.Integer(
        string='Trend Comparison Window (Months)', readonly=True,
        help='How many months prev_period_qty_sold spans — a snapshot of '
             'smart.reorder.config.trend_comparison_months at analysis time, '
             'so reports can show a true prior-period monthly average without '
             're-querying the config.'
    )
    demand_trend = fields.Selection([
        ('up',     '↑ Rising'),
        ('stable', '→ Stable'),
        ('down',   '↓ Falling'),
        ('new',    '★ New / No History'),
    ], string='Demand Trend', readonly=True, index=True)
    trend_pct = fields.Float(string='Trend Change %', digits=(16, 1), readonly=True)

    # ── Seasonal (Phase 3) ────────────────────────────────────────────────────
    same_period_last_year_qty = fields.Float(string='Same Period Last Year', digits=(16, 2), readonly=True)
    seasonal_note             = fields.Char(string='Seasonal Note', readonly=True)

    # ── Stock Position ────────────────────────────────────────────────────────
    qty_on_hand  = fields.Float(string='On Hand Qty',   digits=(16, 0), readonly=True)
    qty_incoming = fields.Float(string='Incoming (PO)', digits=(16, 0), readonly=True)
    qty_outgoing = fields.Float(string='Outgoing (Reserved)', digits=(16, 0), readonly=True)
    qty_available = fields.Float(
        string='Net Available', digits=(16, 0), readonly=True,
        compute='_compute_qty_available', store=True,
    )
    months_of_stock = fields.Float(
        string='Months of Stock', digits=(16, 1), readonly=True,
        compute='_compute_months_of_stock', store=True,
    )

    # ── Dead Stock ────────────────────────────────────────────────────────────
    is_dead_stock         = fields.Boolean(string='Dead Stock?',           readonly=True, index=True)
    last_sale_date        = fields.Date(   string='Last Sale Date',         readonly=True)
    months_since_last_sale= fields.Integer(string='Months Since Last Sale', readonly=True)

    # ── Suggestion Output ─────────────────────────────────────────────────────
    suggested_reorder_qty = fields.Float(   string='Suggested Reorder Qty',        digits=(16, 0), readonly=True, tracking=True)
    raw_reorder_qty       = fields.Float(   string='Raw Qty (before MOQ rounding)', digits=(16, 2), readonly=True)
    prior_suggested_qty   = fields.Float(
        string='Prior Suggested Qty', digits=(16, 0), readonly=True,
        help='suggested_reorder_qty from the previous analysis run, for comparison.'
    )
    delta_pct              = fields.Float(
        string='Change vs Prior Run (%)', digits=(16, 1), readonly=True, index=True,
        help='Percentage change in Suggested Reorder Qty since the last run. '
             'A brand-new suggestion (or one with no prior quantity) shows as +100%.'
    )

    # ── Needs-Review Auto-Triage (T-26) ──────────────────────────────────────
    needs_review = fields.Boolean(
        string='Needs Review?', default=False, readonly=True, index=True,
        help='Auto-flagged when this suggestion swung wildly vs the prior run, is '
             'driven by negative stock with no real demand behind it, or contradicts '
             'itself (dead stock with a positive reorder qty) — worth a human look '
             'before trusting the numbers.'
    )
    needs_review_reason = fields.Char(
        string='Needs Review Reason', readonly=True,
        help='Plain-language explanation of which trigger(s) fired.'
    )

    reorder_value         = fields.Monetary(string='Reorder Value', currency_field='currency_id',
                                            readonly=True, compute='_compute_reorder_value', store=True)
    currency_id           = fields.Many2one('res.currency', related='company_id.currency_id', store=True)
    reorder_needed        = fields.Boolean( string='Reorder Needed?', readonly=True, tracking=True)

    # ── Budget ────────────────────────────────────────────────────────────────
    within_budget = fields.Boolean(string='Within Budget?',       readonly=True)
    budget_rank   = fields.Integer(string='Budget Priority Rank', readonly=True)

    # ── Classification ────────────────────────────────────────────────────────
    abc_class = fields.Selection([
        ('A', 'A — Fast Mover'),
        ('B', 'B — Medium Mover'),
        ('C', 'C — Slow Mover'),
    ], string='ABC Class', readonly=True, index=True)

    sales_pattern = fields.Selection([
        ('regular', 'Sells Regularly'),
        ('sometimes', 'Sells Sometimes'),
        ('big_order_mixed', 'Big Order Mixed In'),
        ('one_time_big_order', 'One-Time Big Order Only'),
        ('new', 'New — No Sales History'),
    ], string='Sales Pattern', readonly=True, index=True)

    reorder_behavior = fields.Selection(
        related='product_id.product_tmpl_id.reorder_behavior',
        string='How to Order This Part',
        readonly=False,
    )

    has_bulk_concentration = fields.Boolean(
        string='Has Bulk Concentration',
        readonly=True,
    )

    urgency = fields.Selection([
        ('critical', '🔴 Critical — Negative Stock'),
        ('urgent',   '🟠 Urgent — Stock < Lead Time'),
        ('normal',   '🟡 Normal — Reorder Recommended'),
        ('dead',     '💀 Dead Stock — No Movement'),
        ('ok',       '🟢 OK — Sufficient Stock'),
    ], string='Urgency', readonly=True, index=True, tracking=True)

    # FIX 1: urgency_rank is a stored computed field — always in sync with urgency
    urgency_rank = fields.Integer(compute='_compute_urgency_rank', store=True)

    superseded_by_id = fields.Many2one(
        'product.template',
        related='product_id.product_tmpl_id.superseded_by_id',
        string='Superseded By',
        readonly=True,
        store=True,
    )

    months_of_stock_after_order = fields.Float(
        string='Months Left After Order',
        compute='_compute_months_of_stock_after_order',
        store=True,
        digits=(16, 1),
    )

    is_overstocked = fields.Boolean(
        string='Overstocked',
        compute='_compute_months_of_stock_after_order',
        store=True,
    )

    is_provisional = fields.Boolean(
        string='Provisional Suggestion',
        default=False,
        help='Provisional suggestions are created immediately when stock goes negative between analysis runs.',
    )

    vendor_price = fields.Float(
        string='Vendor Price',
        readonly=True,
        help='Supplier pricelist price converted to company currency at the analysis run rate.',
    )

    estimated_purchase_value = fields.Monetary(
        string='Est. Purchase Value',
        currency_field='currency_id',
        compute='_compute_purchase_value',
        store=True,
        help='Suggested quantity multiplied by vendor price (converted to company currency).',
    )

    # ── Vendor ────────────────────────────────────────────────────────────────
    vendor_id              = fields.Many2one('res.partner', string='Primary Vendor', readonly=True)
    vendor_stated_lead_days= fields.Integer(string='Vendor Stated Lead (Days)', readonly=True)
    vendor_actual_avg_days = fields.Float(  string='Vendor Actual Avg Lead (Days)', digits=(16, 1), readonly=True)
    vendor_performance_note= fields.Char(   string='Vendor Performance', readonly=True)

    alt_vendor_id          = fields.Many2one('res.partner', string='Fastest Alternative Vendor', readonly=True)
    alt_vendor_lead_days   = fields.Integer(string='Alt. Vendor Lead (Days)', readonly=True)

    transfer_source_warehouse_id = fields.Many2one('stock.warehouse', string='Transfer Source Warehouse', readonly=True)
    transfer_lead_time_days = fields.Integer(
        string='Transfer Lead Time (Days)', readonly=True,
        help='From a smart.reorder.transfer.lane record for this specific '
             'source/destination pair if one exists, otherwise the company '
             'default lead time (Configuration → Company Settings).'
    )
    transfer_suggested_qty = fields.Float(
        string='Transfer Suggested Qty',
        digits=(16, 2),
        readonly=True,
        help='The capped transfer quantity suggested from the donor warehouse.'
    )
    po_ids = fields.Many2many(
        'purchase.order',
        'sra_suggestion_purchase_order_rel',  # explicit junction table
        'suggestion_id',
        'purchase_order_id',
        string='Drafted Orders',
        readonly=True,
    )
    draft_po_ref = fields.Char(
        string='Draft PO',
        compute='_compute_draft_po_ref',
        help='Reference of any linked purchase order still in draft state. '
             'While a draft PO is open, this suggestion holds off on '
             're-proposing a quantity or re-arming Reorder Needed — a note '
             'in the Calculation Breakdown explains why instead.'
    )

    notes = fields.Text(string='Calculation Breakdown', readonly=True)

    @api.depends('po_ids', 'po_ids.state', 'po_ids.name')
    def _compute_draft_po_ref(self):
        for rec in self:
            drafts = rec.po_ids.filtered(lambda po: po.state == 'draft')
            rec.draft_po_ref = ', '.join(drafts.mapped('name')) if drafts else False

    # ── Snooze ────────────────────────────────────────────────────────────────
    snoozed_until = fields.Date(
        string='Snoozed Until', readonly=False,
        help='Suppress reorder alerts for this product until this date.'
    )
    snoozed_note = fields.Char(string='Snooze Reason', readonly=False)
    is_snoozed = fields.Boolean(
        string='Snoozed?', compute='_compute_is_snoozed', store=True
    )

    # ══════════════════════════════════════════════════════════════════════════
    # COMPUTED FIELDS
    # ══════════════════════════════════════════════════════════════════════════

    @api.depends('qty_on_hand', 'qty_incoming', 'qty_outgoing')
    def _compute_qty_available(self):
        for rec in self:
            rec.qty_available = rec.qty_on_hand + rec.qty_incoming - rec.qty_outgoing

    @api.depends('qty_available', 'avg_monthly_demand')
    def _compute_months_of_stock(self):
        for rec in self:
            rec.months_of_stock = self._calc_months_of_stock(rec.qty_available, rec.avg_monthly_demand)

    @api.depends('suggested_reorder_qty', 'product_cost')
    def _compute_reorder_value(self):
        for rec in self:
            rec.reorder_value = rec.suggested_reorder_qty * rec.product_cost

    @api.depends('suggested_reorder_qty', 'vendor_price')
    def _compute_purchase_value(self):
        for rec in self:
            rec.estimated_purchase_value = rec.suggested_reorder_qty * rec.vendor_price

    @api.depends('snoozed_until')
    def _compute_is_snoozed(self):
        today = date.today()
        for rec in self:
            rec.is_snoozed = bool(rec.snoozed_until and rec.snoozed_until >= today)

    @api.depends('urgency')
    def _compute_urgency_rank(self):
        # FIX 1: rank map defined here, not after the loop
        rank_map = {'critical': 1, 'urgent': 2, 'normal': 3, 'dead': 4, 'ok': 5}
        for rec in self:
            rec.urgency_rank = rank_map.get(rec.urgency, 5)

    @api.depends('qty_available', 'suggested_reorder_qty', 'avg_monthly_demand', 'company_id')
    def _compute_months_of_stock_after_order(self):
        # One config lookup per distinct company — avoids N+1 on large suggestion sets.
        company_ids = self.mapped('company_id.id')
        configs = self.env['smart.reorder.config'].sudo().search(
            [('company_id', 'in', company_ids)]
        )
        ceiling_by_company = {c.company_id.id: c.overstock_ceiling_months for c in configs}

        for rec in self:
            ceiling = ceiling_by_company.get(rec.company_id.id, 12.0)

            if rec.avg_monthly_demand > 0:
                rec.months_of_stock_after_order = (
                    (rec.qty_available + rec.suggested_reorder_qty) / rec.avg_monthly_demand
                )
            else:
                rec.months_of_stock_after_order = 0.0

            rec.is_overstocked = bool(ceiling > 0 and rec.months_of_stock_after_order > ceiling)

    # ══════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _calc_months_of_stock(qty_available, avg_monthly_demand):
        from .reorder_engine import calc_months_of_stock
        return calc_months_of_stock(qty_available, avg_monthly_demand)

    @staticmethod
    def _round_to_moq(qty, moq):
        from .reorder_engine import round_to_moq
        return round_to_moq(qty, moq)

    @staticmethod
    def _classify_abc(avg_monthly_demand, config):
        from .reorder_engine import classify_abc
        return classify_abc(avg_monthly_demand, config)

    @staticmethod
    def _determine_urgency(qty_on_hand, months_of_stock, avg_monthly,
                           lead_months, is_dead_stock, suggested_qty):
        from .reorder_engine import determine_urgency
        return determine_urgency(qty_on_hand, months_of_stock, avg_monthly,
                                 lead_months, is_dead_stock, suggested_qty)

    @staticmethod
    def _calc_trend(current_qty, prev_qty, analysis_months, comparison_months):
        from .reorder_engine import calc_trend
        return calc_trend(current_qty, prev_qty, analysis_months, comparison_months)

    @staticmethod
    def _calc_seasonal_note(current_qty, ly_qty):
        from .reorder_engine import calc_seasonal_note
        return calc_seasonal_note(current_qty, ly_qty)

    @staticmethod
    def _compute_robust_monthly_demand(monthly_series, min_spike_size=10.0):
        from .reorder_engine import compute_robust_monthly_demand
        return compute_robust_monthly_demand(monthly_series, min_spike_size=min_spike_size)

    @staticmethod
    def _calc_delta_pct(old_qty, new_qty):
        from .reorder_engine import calc_delta_pct
        return calc_delta_pct(old_qty, new_qty)

    @staticmethod
    def _compute_needs_review(avg_monthly, suggested_qty, is_dead_stock, delta_pct, delta_threshold,
                              mos_after_order=0.0, overstock_ceiling_months=0.0,
                              sales_pattern=False, has_bulk_concentration=False):
        from .reorder_engine import compute_needs_review
        return compute_needs_review(avg_monthly, suggested_qty, is_dead_stock, delta_pct, delta_threshold,
                                    mos_after_order, overstock_ceiling_months,
                                    sales_pattern, has_bulk_concentration)

    @staticmethod
    def _compute_confidence_score(monthly_series, excluded_months, reorder_behavior='system'):
        from .reorder_engine import compute_confidence_score
        return compute_confidence_score(monthly_series, excluded_months, reorder_behavior)

    @staticmethod
    def _build_vendor_perf_note(actual_avg_days, stated_lead_days, with_emoji=False):
        from .reorder_engine import build_vendor_perf_note
        return build_vendor_perf_note(actual_avg_days, stated_lead_days, with_emoji)

    # ══════════════════════════════════════════════════════════════════════════
    # MAIN ENGINE — generate_suggestions()
    # ALL bulk queries happen BEFORE the product loop.
    # The loop does only in-memory dict lookups (O(1) per product).
    # ══════════════════════════════════════════════════════════════════════════

    @api.model
    @require_group(_MANAGER_GROUP)
    def generate_suggestions(self, company_ids=None, warehouse_ids=None,
                             include_zero_demand=False,  # ← NEW param
                             _cron_start=None,           # ← T-19: safety cap
                             trigger_type='cron'):       # ← T-22: 'cron' or 'manual'
        """
        Query plan for N products across W warehouses:
        ───────────────────────────────────
        Per warehouse (8 bulk SQL queries total, regardless of N):
          Q1  — current period sales aggregation
          Q2  — on-hand stock (quant)
          Q2b — negative stock detection (quant)
          Q3  — incoming PO qty
          Q4  — product standard_price + template_id
          Q5  — last sale date per product
          Q6  — vendor supplierinfo (batch, keyed by tmpl_id)
          Q7  — previous period sales aggregation (trend)
          Q8  — last year same period sales aggregation (seasonal)
        Pre-fetch existing suggestions → dict (no search inside loop)
        Loop: pure Python, O(1) dict lookups only
        Bulk write/create after loop

        Run-lock (T-21/T-22): each company's analysis is wrapped in a per-company
        lock so a slow/crashed company never blocks another company's run, and a
        manual wizard trigger can't double-run concurrently with the cron. The
        lock's source of truth is a smart.reorder.cron.log record in 'running'
        status (auditable run history), not a bare Boolean — config.is_running
        is just a computed view onto that table. See _generate_for_company().
        """
        URGENCY_RANK = {'critical': 1, 'urgent': 2, 'normal': 3, 'dead': 4, 'ok': 5}

        # T-19: 45-minute safety cap — stop processing before Odoo.sh 60-min hard timeout
        CRON_TIMEOUT_SECS = 45 * 60   # 45 minutes
        # T-21: a lock held longer than this is treated as crashed/stuck, not "in progress"
        STUCK_LOCK_TIMEOUT_SECS = 60 * 60   # 60 minutes
        t_start = _cron_start or time.time()
        CronLog = self.env['smart.reorder.cron.log'].sudo()

        # BUG 2 FIX: Build location→warehouse map with ONE SQL query instead of
        # N child_of ORM searches (one per warehouse). Uses parent_path to find
        # all descendants of each warehouse root location in a single round trip.
        all_warehouses = self.env['stock.warehouse'].sudo().search([])
        warehouse_by_location = {}  # location_id (int) → stock.warehouse record
        if all_warehouses:
            root_paths = {wh.lot_stock_id.id: wh for wh in all_warehouses if wh.lot_stock_id}
            self.env.cr.execute("""
                SELECT sl.id, sl.parent_path
                  FROM stock_location sl
                 WHERE sl.usage = 'internal'
                   AND sl.active = true
            """)
            for loc_id, parent_path in self.env.cr.fetchall():
                if not parent_path:
                    continue
                for anc_id in (int(x) for x in parent_path.strip('/').split('/') if x):
                    if anc_id in root_paths:
                        warehouse_by_location[loc_id] = root_paths[anc_id]
                        break

        # Clear the provisional flag for suggestions in the analyzed scope (Feature 14)
        prov_domain = []
        if company_ids:
            prov_domain.append(('company_id', 'in', company_ids))
        if warehouse_ids:
            prov_domain.append(('warehouse_id', 'in', warehouse_ids))
        self.sudo().with_context(active_test=False).search(prov_domain).write({'is_provisional': False})

        if company_ids:
            companies = self.env['res.company'].sudo().browse(company_ids)
        else:
            companies = self.env['res.company'].sudo().search([])

        total_created = 0
        total_critical = 0

        for company in companies:
            try:
                config = self._get_config(company.id)
            except UserError as e:
                _logger.warning(
                    'SmartReorder: Skipping company %s — no config found. '
                    'Go to Reorder Advisor → Configuration to set it up. (%s)',
                    company.name, e
                )
                continue

            # T-21/T-22: Per-company run lock — refuse to overlap two runs on the
            # same company (cron + manual wizard, or two overlapping crons), but
            # never let one company's lock block any other company's analysis.
            # The lock's source of truth is any cron.log record still 'running'.
            now = fields.Datetime.now()
            running_log = CronLog.search([
                ('company_id', '=', company.id),
                ('status', '=', 'running'),
            ], order='started_at desc', limit=1)

            if running_log:
                elapsed_lock = (
                    (now - running_log.started_at).total_seconds()
                    if running_log.started_at else STUCK_LOCK_TIMEOUT_SECS
                )
                if elapsed_lock < STUCK_LOCK_TIMEOUT_SECS:
                    _logger.warning(
                        'SmartReorder: Skipping %s — an analysis run is already in progress '
                        '(started %s, %.0fs ago). Will retry next run.',
                        company.name, running_log.started_at, elapsed_lock
                    )
                    continue
                _logger.warning(
                    'SmartReorder: %s — lock held since %s is stale (older than %ds). '
                    'Treating as a crashed/stuck run and overriding it.',
                    company.name, running_log.started_at, STUCK_LOCK_TIMEOUT_SECS
                )
                running_log.write({
                    'status': 'aborted',
                    'finished_at': now,
                    'error_notes': 'Auto-aborted: superseded after the lock went stale '
                                    f'(held longer than {STUCK_LOCK_TIMEOUT_SECS}s).',
                })

            # The check above is check-then-create, so two workers can race
            # past it together; the partial unique index on (company_id)
            # WHERE status='running' (see SmartReorderCronLog.init) makes the
            # second INSERT fail here instead of double-running the company.
            try:
                with self.env.cr.savepoint():
                    log = CronLog.create({
                        'company_id': company.id,
                        'started_at': now,
                        'trigger_type': trigger_type,
                        'status': 'running',
                    })
            except IntegrityError:
                _logger.warning(
                    'SmartReorder: Skipping %s — another worker started a run '
                    'concurrently. Will retry next run.', company.name
                )
                continue
            try:
                comp_include_zero = config.cron_include_zero_demand if trigger_type == 'cron' else include_zero_demand
                created, critical, had_errors = self._generate_for_company(
                    company, config, warehouse_ids, comp_include_zero,
                    t_start, CRON_TIMEOUT_SECS, warehouse_by_location, URGENCY_RANK, log,
                )
                total_created  += created
                total_critical += critical
                log.write({
                    'finished_at':         fields.Datetime.now(),
                    'status':               'completed_with_errors' if had_errors else 'completed',
                    'products_processed':  created,
                    'critical_count':      critical,
                })
            except Exception as e:
                # No finally needed: the T-19 timeout cap is a plain `break` inside
                # _generate_for_company(), not an exception, so it returns normally
                # through the try branch above (status 'completed'). Only a genuine
                # exception reaches here, and both branches always close the log —
                # so the lock (any log row still 'running') can never be left stuck.
                log.write({
                    'finished_at': fields.Datetime.now(),
                    'status': 'aborted',
                    'error_notes': str(e),
                })
                raise

        _logger.info('SmartReorder: Done. %d records. %d critical.', total_created, total_critical)
        return {'created': total_created, 'critical': total_critical}

    # UoM-safe conversion expression: sale.order.line.qty_delivered is stored
    # in the LINE's UoM, not the product's. qty_in_product_uom =
    # qty / line_uom.factor * product_uom.factor (same math as
    # uom.uom._compute_quantity). Summing raw qty_delivered understates a
    # dozen-sold/unit-stocked product by 12x.
    _SALE_QTY_EXPR = "SUM(sol.qty_delivered / lu.factor * pu.factor)"
    _SALE_QTY_JOINS = """
          JOIN sale_order so ON so.id = sol.order_id
          JOIN product_product pp ON pp.id = sol.product_id
          JOIN product_template pt ON pt.id = pp.product_tmpl_id
          JOIN uom_uom lu ON lu.id = sol.product_uom
          JOIN uom_uom pu ON pu.id = pt.uom_id
    """

    def _sales_qty_by_product(self, company_id, warehouse_id, date_from, date_to_excl,
                              product_ids=None, storable_only=False):
        """Confirmed sales per product over [date_from, date_to_excl), summed
        in the PRODUCT's UoM. Returns {product_id: qty}."""
        params = [company_id, warehouse_id, str(date_from), str(date_to_excl)]
        extra = ''
        if storable_only:
            extra += (" AND pt.type = 'product'"
                      " AND COALESCE(pt.exclude_from_reorder_advisor, false) = false")
        if product_ids is not None:
            if not product_ids:
                return {}
            extra += " AND sol.product_id = ANY(%s)"
            params.append(list(product_ids))
        self.env.cr.execute(f"""
            SELECT sol.product_id, {self._SALE_QTY_EXPR}
              FROM sale_order_line sol
              {self._SALE_QTY_JOINS}
             WHERE so.state = 'sale'
               AND so.company_id = %s
               AND so.warehouse_id = %s
               AND so.date_order >= %s
               AND so.date_order < %s
               {extra}
             GROUP BY sol.product_id
        """, tuple(params))
        return {row[0]: row[1] or 0.0 for row in self.env.cr.fetchall()}

    def _fetch_warehouse_data(self, company, config, warehouse, dates, include_zero_demand,
                              tmpl_to_prod, prod_by_id, warehouse_by_location, warehouses):
        """
        Gathers all the raw inventory, sales, and vendor parameters from Odoo's database
        for a single warehouse, returning them as a dictionary of plain Python mappings.
        No calculations are performed here.
        """
        # ── Upsert map ──
        existing_records = self.sudo().with_context(active_test=False).search([
            ('company_id',   '=', company.id),
            ('warehouse_id', '=', warehouse.id),
        ])
        existing_map = {r.product_id.id: r for r in existing_records}

        # ── Q1: Current period sales ──
        date_from = dates['date_from']
        date_to = dates['date_to']
        prev_date_from = dates['prev_date_from']
        prev_date_to = dates['prev_date_to']
        ly_date_from = dates['ly_date_from']
        ly_date_to = dates['ly_date_to']
        analysis_months = dates['analysis_months']
        comparison_months = dates['comparison_months']

        qty_map = self._sales_qty_by_product(
            company.id, warehouse.id, date_from, date_to + timedelta(days=1),
            storable_only=True,
        )
        product_ids_with_sales = list(qty_map.keys())

        # ── Q2: On-hand stock ──
        location = warehouse.lot_stock_id
        quant_data = self.env['stock.quant'].sudo().read_group(
            domain=[
                ('product_id',   'in', product_ids_with_sales),
                ('location_id',  'child_of', location.id),
            ],
            fields=['product_id', 'quantity:sum'],
            groupby=['product_id'],
        ) if product_ids_with_sales else []
        onhand_map = {r['product_id'][0]: r['quantity'] for r in quant_data}

        # ── Q2b: Negative stock ──
        neg_data = self.env['stock.quant'].sudo().read_group(
            domain=[
                ('location_id', 'child_of', location.id),
                ('quantity',    '<', 0),
                ('product_id.type', '=', 'product'),
                ('product_id.product_tmpl_id.exclude_from_reorder_advisor', '=', False),
            ],
            fields=['product_id', 'quantity:sum'],
            groupby=['product_id'],
        )
        for r in neg_data:
            pid = r['product_id'][0]
            if pid not in onhand_map:
                onhand_map[pid] = r['quantity']
            if pid not in qty_map:
                qty_map[pid] = 0.0
                product_ids_with_sales.append(pid)

        # Q2c: Zero-demand products
        if include_zero_demand:
            zero_demand_quants = self.env['stock.quant'].sudo().read_group(
                domain=[
                    ('location_id',          'child_of', location.id),
                    ('quantity',             '>',  0),
                    ('product_id.type',      '=',  'product'),
                    ('product_id.product_tmpl_id.exclude_from_reorder_advisor', '=', False),
                    ('product_id',           'not in', product_ids_with_sales),
                ],
                fields=['product_id', 'quantity:sum'],
                groupby=['product_id'],
            )
            for r in zero_demand_quants:
                pid = r['product_id'][0]
                onhand_map.setdefault(pid, r['quantity'])
                qty_map.setdefault(pid, 0.0)
                product_ids_with_sales.append(pid)

        # Successor/Predecessor chains logic
        additional_pids = []
        for pid in product_ids_with_sales:
            prod = prod_by_id.get(pid)
            if not prod:
                continue
            curr_tmpl = prod.product_tmpl_id
            depth = 0
            while curr_tmpl.superseded_by_id and depth < 10:
                next_tmpl = curr_tmpl.superseded_by_id
                if next_tmpl.exclude_from_reorder_advisor:
                    break
                succ_pid = tmpl_to_prod.get(next_tmpl.id)
                if succ_pid:
                    if succ_pid not in product_ids_with_sales and succ_pid not in additional_pids:
                        additional_pids.append(succ_pid)
                curr_tmpl = next_tmpl
                depth += 1
        product_ids_with_sales.extend(additional_pids)

        for pid in additional_pids:
            qty_map.setdefault(pid, 0.0)
        missing_onhand_pids = [pid for pid in additional_pids if pid not in onhand_map]
        if missing_onhand_pids:
            missing_quants = self.env['stock.quant'].sudo().read_group(
                domain=[
                    ('product_id',   'in', missing_onhand_pids),
                    ('location_id',  'child_of', location.id),
                ],
                fields=['product_id', 'quantity:sum'],
                groupby=['product_id'],
            )
            for r in missing_quants:
                onhand_map[r['product_id'][0]] = r['quantity']

        # Archive orphans. When zero-demand products are EXCLUDED from this
        # run (manual wizard default), products with stock but no sales are
        # legitimately absent from the scope — archiving their suggestions
        # here made every quick manual refresh empty the Dead Stock view
        # until the next full cron. In that case only archive suggestions
        # whose product is genuinely gone from the advisor's universe
        # (archived, non-storable, or flagged excluded — i.e. not in
        # prod_by_id); a zero-demand-inclusive run still archives all orphans.
        scope_set = set(product_ids_with_sales)
        orphan_ids = [
            r.id for pid, r in existing_map.items()
            if pid not in scope_set and r.active
            and (include_zero_demand or pid not in prod_by_id)
        ]
        if orphan_ids:
            self.sudo().browse(orphan_ids).write({'active': False})

        if not product_ids_with_sales:
            return {}

        # ── Q1b: Monthly sales breakdown ──
        month_starts = dates['month_starts']
        month_index = {m: i for i, m in enumerate(month_starts)}
        monthly_qty_by_product = {
            pid: [0.0] * analysis_months for pid in product_ids_with_sales
        }
        self.env.cr.execute(f"""
            SELECT sol.product_id,
                   date_trunc('month', so.date_order)::date AS sale_month,
                   {self._SALE_QTY_EXPR}
              FROM sale_order_line sol
              {self._SALE_QTY_JOINS}
             WHERE so.state         = 'sale'
               AND so.company_id     = %s
               AND so.warehouse_id   = %s
               AND so.date_order    >= %s
               AND so.date_order    < %s
               AND sol.product_id    = ANY(%s)
             GROUP BY sol.product_id, sale_month
        """, (company.id, warehouse.id, str(month_starts[-1]), str(date_to + timedelta(days=1)), product_ids_with_sales))
        for pid, sale_month, qty in self.env.cr.fetchall():
            idx = month_index.get(sale_month)
            if idx is not None and pid in monthly_qty_by_product:
                monthly_qty_by_product[pid][idx] = qty

        original_monthly_qty = {pid: list(series) for pid, series in monthly_qty_by_product.items()}
        predecessors_map = {}
        predecessor_names_map = {}

        for pid in product_ids_with_sales:
            prod = prod_by_id.get(pid)
            if not prod:
                continue
            tmpl = prod.product_tmpl_id
            if not tmpl.superseded_by_id:
                continue

            curr_tmpl = tmpl
            path = [curr_tmpl.id]
            depth = 0
            while curr_tmpl.superseded_by_id and depth < 10:
                next_tmpl = curr_tmpl.superseded_by_id
                if next_tmpl.id in path:
                    break
                path.append(next_tmpl.id)
                curr_tmpl = next_tmpl
                depth += 1

            pred_pid = prod.id
            pred_name = prod.default_code or prod.name
            for succ_tmpl_id in path[1:]:
                succ_pid = tmpl_to_prod.get(succ_tmpl_id)
                if succ_pid and succ_pid in monthly_qty_by_product:
                    pred_series = original_monthly_qty.get(pred_pid)
                    if pred_series:
                        for i in range(analysis_months):
                            monthly_qty_by_product[succ_pid][i] += pred_series[i]

                    predecessors_map.setdefault(succ_pid, set()).add(pred_pid)
                    if pred_name not in predecessor_names_map.setdefault(succ_pid, []):
                        predecessor_names_map[succ_pid].append(pred_name)

        # ── Q1c: Current month sales ──
        current_month_start = date.today().replace(day=1)
        current_month_sales_map = self._sales_qty_by_product(
            company.id, warehouse.id, current_month_start, date.today() + timedelta(days=1),
            product_ids=product_ids_with_sales,
        )

        # ── Q3: Incoming PO qty ──
        po_lines = self.env['purchase.order.line'].sudo().search_read(
            domain=[
                ('order_id.state',      'in', ['purchase', 'done']),
                ('order_id.company_id', '=',  company.id),
                ('order_id.picking_type_id.warehouse_id', '=', warehouse.id),
                ('product_id',          'in', product_ids_with_sales),
            ],
            fields=['product_id', 'product_qty', 'qty_received', 'date_planned', 'product_uom']
        )
        # product_qty / qty_received are in the PO line's UoM — convert to the
        # product's UoM before mixing with stock quantities.
        line_uom_ids = {l['product_uom'][0] for l in po_lines if l.get('product_uom')}
        uoms_by_id = {u.id: u for u in self.env['uom.uom'].sudo().browse(list(line_uom_ids))}
        incoming_map = {}
        overdue_map = {}
        for line in po_lines:
            pid = line['product_id'][0]
            qty_ordered = line['product_qty'] or 0.0
            qty_received = line['qty_received'] or 0.0
            net_qty = max(0.0, qty_ordered - qty_received)
            if net_qty > 0.0:
                prod = prod_by_id.get(pid)
                line_uom = uoms_by_id.get(line['product_uom'][0]) if line.get('product_uom') else None
                if prod and line_uom and line_uom != prod.uom_id:
                    net_qty = line_uom._compute_quantity(net_qty, prod.uom_id, round=False)
                incoming_map[pid] = incoming_map.get(pid, 0.0) + net_qty
                planned_dt = line['date_planned']
                if planned_dt and planned_dt.date() < date.today():
                    overdue_map.setdefault(pid, []).append((net_qty, planned_dt))

        # ── Q3c: Incoming internal transfers (confirmed, not yet done) ──
        # A transfer already on its way from a donor warehouse must count
        # toward incoming quantity here too, or the next run can suggest
        # buying stock that is already en route. `product_qty` on a
        # non-done move is the still-undelivered remainder (Odoo splits off
        # a done move for whatever portion has already arrived), same
        # convention as Q3b below — no separate subtraction needed.
        transfer_in_data = self.env['stock.move'].sudo().read_group(
            domain=[
                ('state',            'in', ['confirmed', 'waiting', 'assigned', 'partially_available']),
                ('location_dest_id', 'child_of', warehouse.lot_stock_id.id),
                '!', ('location_id', 'child_of', warehouse.lot_stock_id.id),
                ('location_id.usage', '=', 'internal'),
                ('product_id',       'in', product_ids_with_sales),
                ('company_id',       '=', company.id),
            ],
            fields=['product_id', 'product_qty:sum'],
            groupby=['product_id'],
        )
        for r in transfer_in_data:
            qty = r.get('product_qty') or 0.0
            if qty > 0.0:
                pid = r['product_id'][0]
                incoming_map[pid] = incoming_map.get(pid, 0.0) + qty

        # ── Q3b: Outgoing customer reservations ──
        outgoing_data = self.env['stock.move'].sudo().read_group(
            domain=[
                ('state',               'in', ['confirmed', 'waiting', 'assigned', 'partially_available']),
                ('location_id',         'child_of', warehouse.lot_stock_id.id),
                ('location_dest_id.usage', '=', 'customer'),
                ('product_id',          'in', product_ids_with_sales),
                ('company_id',          '=', company.id),
            ],
            fields=['product_id', 'product_qty:sum'],
            groupby=['product_id'],
        )
        outgoing_map = {
            r['product_id'][0]: r['product_qty']
            for r in outgoing_data
            if r.get('product_qty')
        }

        # ── Q4: Product cost + template_id ──
        # standard_price is a company-dependent property field — it must be read
        # within the context of the company being analyzed, or it silently
        # resolves to whichever company's cost record the ORM defaults to
        # (wrong price and reorder value in a multi-company setup).
        products = self.env['product.product'].with_company(company).sudo().browse(product_ids_with_sales)
        product_lookup = {p.id: p for p in products}
        cost_map = {p.id: p.standard_price for p in products}
        tmpl_map = {p.id: p.product_tmpl_id.id for p in products}

        # ── Q5: Last sale date per product ──
        self.env.cr.execute("""
            SELECT sol.product_id, MAX(so.date_order)::date
              FROM sale_order_line sol
              JOIN sale_order so ON sol.order_id = so.id
             WHERE so.state       = 'sale'
               AND so.warehouse_id = %s
               AND sol.product_id  = ANY(%s)
             GROUP BY sol.product_id
        """, (warehouse.id, product_ids_with_sales))
        last_sale_map = {
            row[0]: row[1]
            for row in self.env.cr.fetchall()
            if row[1] is not None
        }

        # ── Q6: Batch vendor info ──
        all_tmpl_ids = list(set(tmpl_map.values()))
        supplier_data = self.env['product.supplierinfo'].sudo().search_read(
            domain=[
                ('product_tmpl_id', 'in', all_tmpl_ids),
                '|', ('company_id', '=', False), ('company_id', '=', company.id)
            ],
            fields=['product_tmpl_id', 'partner_id', 'delay', 'min_qty', 'price', 'currency_id'],
            order='product_tmpl_id asc, sequence asc',
        )
        template_suppliers_map = {}
        vendor_map = {}
        for r in supplier_data:
            tid = r['product_tmpl_id'][0]
            template_suppliers_map.setdefault(tid, []).append(r)
            if tid not in vendor_map:
                vendor_map[tid] = (
                    r['partner_id'][0] if r.get('partner_id') else False,
                    r['delay'] or (config.default_lead_time_months * 30),
                    r['min_qty'] or 1.0,
                    r['price'] or 0.0,
                    r['currency_id'][0] if r.get('currency_id') else False,
                )

        # ── Q6b: Batch vendor actual lead times ──
        actual_lead_map = {}
        if config.track_vendor_performance and vendor_map:
            vendor_partner_ids = list({v[0] for v in vendor_map.values() if v[0]})
            prod_ids_for_perf = list(product_ids_with_sales)
            self.env.cr.execute("""
                SELECT pol.product_id,
                       po.partner_id,
                       AVG(po.effective_date::date - po.date_approve::date)
                  FROM purchase_order_line pol
                  JOIN purchase_order po ON pol.order_id = po.id
                 WHERE po.state         IN ('purchase', 'done')
                   AND po.partner_id    = ANY(%s)
                   AND po.date_approve IS NOT NULL
                   AND po.effective_date IS NOT NULL
                   AND pol.product_id   = ANY(%s)
                 GROUP BY pol.product_id, po.partner_id
            """, (vendor_partner_ids, prod_ids_for_perf))
            for pid, partner_id, avg_days in self.env.cr.fetchall():
                if avg_days is not None:
                    actual_lead_map[(pid, partner_id)] = round(float(avg_days), 1)

        # ── Q7: Batch previous period sales ──
        prev_qty_map = self._sales_qty_by_product(
            company.id, warehouse.id, prev_date_from, prev_date_to + timedelta(days=1),
            product_ids=product_ids_with_sales,
        )

        # ── Q8: Batch last-year same period ──
        ly_qty_map = self._sales_qty_by_product(
            company.id, warehouse.id, ly_date_from, ly_date_to + timedelta(days=1),
            product_ids=product_ids_with_sales,
        )

        # ── Q2c: Global stock map ──
        global_quant_data = self.env['stock.quant'].sudo().read_group(
            domain=[
                ('product_id',          'in', product_ids_with_sales),
                ('location_id.usage',   '=',  'internal'),
                ('location_id.company_id', '=', company.id),
            ],
            fields=['product_id', 'location_id', 'quantity:sum'],
            groupby=['product_id', 'location_id'],
            lazy=False,
        )
        global_stock_map = {}
        for r in global_quant_data:
            pid = r['product_id'][0]
            loc_id = r['location_id'][0]
            qty = r['quantity']
            wh = warehouse_by_location.get(loc_id)
            if wh:
                global_stock_map.setdefault(pid, {}).setdefault(wh.id, 0.0)
                global_stock_map[pid][wh.id] += qty

        # ── Global sales ──
        self.env.cr.execute(f"""
            SELECT sol.product_id, so.warehouse_id, {self._SALE_QTY_EXPR}
              FROM sale_order_line sol
              {self._SALE_QTY_JOINS}
             WHERE so.state        = 'sale'
               AND so.company_id    = %s
               AND so.date_order   >= %s
               AND so.date_order    < %s
               AND sol.product_id   = ANY(%s)
             GROUP BY sol.product_id, so.warehouse_id
        """, (company.id, str(date_from), str(date_to + timedelta(days=1)), product_ids_with_sales))
        qty_map_global = {}
        for row in self.env.cr.fetchall():
            if row[0] is not None and row[1] is not None:
                qty_map_global[(row[0], row[1])] = row[2]

        # ── Transfer lanes ──
        lane_data = self.env['smart.reorder.transfer.lane'].sudo().search_read(
            domain=[
                ('source_warehouse_id', 'in', warehouses.ids),
                ('dest_warehouse_id', '=', warehouse.id),
            ],
            fields=['source_warehouse_id', 'lead_time_days'],
        )
        lane_lead_time_map = {
            r['source_warehouse_id'][0]: r['lead_time_days'] for r in lane_data
        }

        # ── Partner Names ──
        partner_ids = [v[0] for v in vendor_map.values() if v[0]]
        for lines in template_suppliers_map.values():
            for r in lines:
                if r.get('partner_id'):
                    partner_ids.append(r['partner_id'][0])
        partner_names_map = {
            p.id: p.name for p in self.env['res.partner'].sudo().browse(list(set(partner_ids)))
        }

        return {
            'existing_map': existing_map,
            'qty_map': qty_map,
            'product_ids_with_sales': product_ids_with_sales,
            'onhand_map': onhand_map,
            'monthly_qty_by_product': monthly_qty_by_product,
            'predecessors_map': predecessors_map,
            'predecessor_names_map': predecessor_names_map,
            'current_month_sales_map': current_month_sales_map,
            'incoming_map': incoming_map,
            'overdue_map': overdue_map,
            'outgoing_map': outgoing_map,
            'cost_map': cost_map,
            'tmpl_map': tmpl_map,
            'last_sale_map': last_sale_map,
            'template_suppliers_map': template_suppliers_map,
            'vendor_map': vendor_map,
            'actual_lead_map': actual_lead_map,
            'prev_qty_map': prev_qty_map,
            'ly_qty_map': ly_qty_map,
            'global_stock_map': global_stock_map,
            'qty_map_global': qty_map_global,
            'lane_lead_time_map': lane_lead_time_map,
            'partner_names_map': partner_names_map,
            'product_lookup': product_lookup,
        }

    def _calculate_product_suggestion(
        self, product_id, product_code, product_name, tmpl_id, is_superseded, successor_display_name,
        company_id, warehouse_id, dates, config_data, warehouses_list,
        monthly_series, qty_on_hand, qty_incoming, qty_outgoing, cost,
        last_sale, current_month_sales, prev_qty, ly_qty,
        tmpl_suppliers, primary_vendor_info, actual_avg_days,
        overdue_lines, predecessors, predecessor_names,
        global_stocks, global_sales, lane_lead_times,
        partner_names_map, currency_convert_fn, reorder_behavior='system'
    ):
        from .reorder_engine import calculate_product_suggestion
        return calculate_product_suggestion(
            product_id, product_code, product_name, tmpl_id, is_superseded, successor_display_name,
            company_id, warehouse_id, dates, config_data, warehouses_list,
            monthly_series, qty_on_hand, qty_incoming, qty_outgoing, cost,
            last_sale, current_month_sales, prev_qty, ly_qty,
            tmpl_suppliers, primary_vendor_info, actual_avg_days,
            overdue_lines, predecessors, predecessor_names,
            global_stocks, global_sales, lane_lead_times,
            partner_names_map, currency_convert_fn, reorder_behavior
        )

    def _generate_for_company(self, company, config, warehouse_ids, include_zero_demand,
                              t_start, CRON_TIMEOUT_SECS, warehouse_by_location, URGENCY_RANK, log):
        """
        Process one company's warehouses end to end: data-fetch, loop with pure suggestion calculation,
        budget pass, and upsert.
        """
        total_created = 0
        total_critical = 0
        had_errors = False

        analysis_months    = int(config.analysis_period)
        comparison_months  = config.trend_comparison_months
        date_to            = date.today().replace(day=1) - timedelta(days=1)
        date_from          = (date_to + timedelta(days=1)) - relativedelta(months=analysis_months)
        prev_date_to       = date_from - timedelta(days=1)
        prev_date_from     = (prev_date_to + timedelta(days=1)) - relativedelta(months=comparison_months)
        ly_date_from       = date_from - relativedelta(years=1)
        ly_date_to         = date_to   - relativedelta(years=1)

        month_starts = []
        _cursor_month = date_to.replace(day=1)
        for _ in range(analysis_months):
            month_starts.append(_cursor_month)
            _cursor_month = _cursor_month - relativedelta(months=1)

        dates = {
            'date_from': date_from,
            'date_to': date_to,
            'prev_date_from': prev_date_from,
            'prev_date_to': prev_date_to,
            'ly_date_from': ly_date_from,
            'ly_date_to': ly_date_to,
            'analysis_months': analysis_months,
            'comparison_months': comparison_months,
            'month_starts': month_starts,
        }

        if warehouse_ids:
            warehouses = self.env['stock.warehouse'].sudo().browse(warehouse_ids).filtered(
                lambda w: w.company_id.id == company.id
            )
        else:
            warehouses = self.env['stock.warehouse'].sudo().search(
                [('company_id', '=', company.id)]
            )

        all_products = self.env['product.product'].sudo().search([
            ('active', '=', True),
            ('type', '=', 'product'),
            ('product_tmpl_id.exclude_from_reorder_advisor', '=', False),
        ])
        tmpl_to_prod = {p.product_tmpl_id.id: p.id for p in all_products}
        prod_by_id = {p.id: p for p in all_products}

        for warehouse in warehouses:
            elapsed = time.time() - t_start
            if elapsed > CRON_TIMEOUT_SECS:
                _logger.warning(
                    'SmartReorder: 45-minute safety cap reached (%.0fs elapsed). '
                    'Stopping before Odoo.sh hard timeout. '
                    'Remaining warehouses skipped — will run on next cron.',
                    elapsed
                )
                break

            _logger.info(
                'SmartReorder: %s → %s (%d months)',
                company.name, warehouse.name, analysis_months
            )

            try:
                with self.env.cr.savepoint():
                    data = self._fetch_warehouse_data(
                        company=company,
                        config=config,
                        warehouse=warehouse,
                        dates=dates,
                        include_zero_demand=include_zero_demand,
                        tmpl_to_prod=tmpl_to_prod,
                        prod_by_id=prod_by_id,
                        warehouse_by_location=warehouse_by_location,
                        warehouses=warehouses
                    )
                    if not data:
                        _logger.info('SmartReorder: No products found for %s/%s', company.name, warehouse.name)
                        continue

                    existing_map = data['existing_map']
                    product_ids_with_sales = data['product_ids_with_sales']
                    product_lookup = data['product_lookup']
                    tmpl_map = data['tmpl_map']
                    vendor_map = data['vendor_map']
                    monthly_qty_by_product = data['monthly_qty_by_product']
                    onhand_map = data['onhand_map']
                    incoming_map = data['incoming_map']
                    overdue_map  = data['overdue_map']
                    outgoing_map = data['outgoing_map']
                    cost_map = data['cost_map']
                    last_sale_map = data['last_sale_map']
                    current_month_sales_map = data['current_month_sales_map']
                    prev_qty_map = data['prev_qty_map']
                    ly_qty_map = data['ly_qty_map']
                    template_suppliers_map = data['template_suppliers_map']
                    actual_lead_map = data['actual_lead_map']
                    predecessors_map = data['predecessors_map']
                    predecessor_names_map = data['predecessor_names_map']
                    global_stock_map = data['global_stock_map']
                    qty_map_global = data['qty_map_global']
                    lane_lead_time_map = data['lane_lead_time_map']
                    partner_names_map = data['partner_names_map']

                    suggestion_values = []
                    
                    config_data = {
                        'default_lead_time_months': config.default_lead_time_months,
                        'track_vendor_performance': config.track_vendor_performance,
                        'dead_stock_months': config.dead_stock_months,
                        'flag_dead_stock': config.flag_dead_stock,
                        'safety_buffer_months': config.safety_buffer_months,
                        'order_cycle_months': config.order_cycle_months,
                        'alt_vendor_lead_margin_days': config.alt_vendor_lead_margin_days,
                        'abc_a_threshold': config.abc_a_threshold,
                        'abc_b_threshold': config.abc_b_threshold,
                        'min_spike_size': config.min_spike_size,
                        'overstock_ceiling_months': config.overstock_ceiling_months,
                        'transfer_surplus_threshold': config.transfer_surplus_threshold,
                        'default_internal_transfer_days': config.default_internal_transfer_days,
                    }
                    
                    warehouses_list = [{'id': wh.id, 'name': wh.name} for wh in warehouses]

                    def currency_convert_fn(price, from_currency_id):
                        if price <= 0.0 or not from_currency_id or from_currency_id == company.currency_id.id:
                            return price
                        currency_obj = self.env['res.currency'].browse(from_currency_id)
                        return currency_obj._convert(
                            price,
                            company.currency_id,
                            company,
                            date.today(),
                        )

                    for product_id in product_ids_with_sales:
                        product = product_lookup.get(product_id)
                        if not product:
                            continue

                        tmpl_id = tmpl_map.get(product_id)
                        is_superseded = bool(product.product_tmpl_id.superseded_by_id)
                        successor_display_name = product.product_tmpl_id.superseded_by_id.display_name if is_superseded else False
                        
                        monthly_series = monthly_qty_by_product.get(product_id, [0.0] * analysis_months)
                        qty_on_hand = onhand_map.get(product_id, 0.0)
                        qty_incoming = incoming_map.get(product_id, 0.0)
                        qty_outgoing = outgoing_map.get(product_id, 0.0)
                        cost = cost_map.get(product_id, 0.0)
                        last_sale = last_sale_map.get(product_id)
                        current_month_sales = current_month_sales_map.get(product_id, 0.0)
                        prev_qty = prev_qty_map.get(product_id, 0.0)
                        ly_qty = ly_qty_map.get(product_id, 0.0)
                        tmpl_suppliers = template_suppliers_map.get(tmpl_id, [])
                        primary_vendor_info = vendor_map.get(tmpl_id)
                        
                        v_partner_id = primary_vendor_info[0] if primary_vendor_info else False
                        actual_avg_days = actual_lead_map.get((product_id, v_partner_id), 0.0) if v_partner_id else 0.0
                        
                        overdue_lines = overdue_map.get(product_id, [])
                        predecessors = predecessors_map.get(product_id, set())
                        predecessor_names = predecessor_names_map.get(product_id, [])
                        global_stocks = global_stock_map.get(product_id, {})
                        
                        global_sales = {}
                        for wh_other in warehouses:
                            global_sales[wh_other.id] = qty_map_global.get((product_id, wh_other.id), 0.0)

                        vals = self._calculate_product_suggestion(
                            product_id=product_id,
                            product_code=product.default_code,
                            product_name=product.name,
                            tmpl_id=tmpl_id,
                            is_superseded=is_superseded,
                            successor_display_name=successor_display_name,
                            company_id=company.id,
                            warehouse_id=warehouse.id,
                            dates=dates,
                            config_data=config_data,
                            warehouses_list=warehouses_list,
                            monthly_series=monthly_series,
                            qty_on_hand=qty_on_hand,
                            qty_incoming=qty_incoming,
                            qty_outgoing=qty_outgoing,
                            cost=cost,
                            last_sale=last_sale,
                            current_month_sales=current_month_sales,
                            prev_qty=prev_qty,
                            ly_qty=ly_qty,
                            tmpl_suppliers=tmpl_suppliers,
                            primary_vendor_info=primary_vendor_info,
                            actual_avg_days=actual_avg_days,
                            overdue_lines=overdue_lines,
                            predecessors=predecessors,
                            predecessor_names=predecessor_names,
                            global_stocks=global_stocks,
                            global_sales=global_sales,
                            lane_lead_times=lane_lead_time_map,
                            partner_names_map=partner_names_map,
                            currency_convert_fn=currency_convert_fn,
                            reorder_behavior=product.reorder_behavior
                        )

                        u_rank = URGENCY_RANK.get(vals['urgency'], 5)
                        suggestion_values.append((product_id, vals['estimated_purchase_value'], u_rank, vals))

                        if vals['urgency'] == 'critical':
                            total_critical += 1

                    suggestion_values.sort(key=lambda x: (x[2], -x[1]))
                    budget        = config.budget_cap
                    running_total = 0.0

                    for rank, (pid, rv, _, vals) in enumerate(suggestion_values, 1):
                        vals['budget_rank'] = rank
                        if budget > 0 and vals['reorder_needed']:
                            running_total         += rv
                            vals['within_budget']  = running_total <= budget
                        else:
                            vals['within_budget'] = True

                    to_create = []
                    to_write  = []
                    for _, _, _, vals in suggestion_values:
                        pid = vals['product_id']
                        if pid in existing_map:
                            existing_rec = existing_map[pid]
                            old_qty = existing_rec.suggested_reorder_qty
                            new_qty = vals['suggested_reorder_qty']
                            write_vals = dict(vals)
                            write_vals['prior_suggested_qty'] = old_qty
                            write_vals['delta_pct']           = self._calc_delta_pct(old_qty, new_qty)

                            # Draft PO guard: a draft PO already created from this
                            # suggestion (action_create_draft_po) is still open.
                            # Don't silently re-arm reorder_needed or re-propose
                            # the same quantity from scratch — that's what invited
                            # a second draft PO for one shortage. Surface it
                            # instead: suppress the qty/flag and name the draft PO.
                            open_draft_pos = existing_rec.po_ids.filtered(lambda po: po.state == 'draft')
                            draft_po_note = False
                            if open_draft_pos:
                                draft_lines = []
                                for po in open_draft_pos:
                                    po_qty = sum(po.order_line.filtered(
                                        lambda l: l.product_id.id == pid
                                    ).mapped('product_qty'))
                                    draft_lines.append(f'{po.name} ({po_qty:.0f} units)')
                                draft_po_note = (
                                    'Draft PO already created: ' + ', '.join(draft_lines) +
                                    '. Confirm or cancel it before a new suggestion is proposed.'
                                )
                                write_vals['suggested_reorder_qty'] = 0.0
                                write_vals['raw_reorder_qty']       = 0.0
                                write_vals['reorder_needed']        = False

                            # Skip bulk-concentration flag when buyer confirmed bulk-regular
                            _bulk_conc = write_vals.get('has_bulk_concentration', False)
                            if write_vals.get('reorder_behavior') == 'bulk_regular':
                                _bulk_conc = False
                            write_vals['needs_review'], write_vals['needs_review_reason'] = (
                                self._compute_needs_review(
                                    write_vals['avg_monthly_demand'], write_vals['suggested_reorder_qty'],
                                    write_vals['is_dead_stock'], write_vals['delta_pct'],
                                    config.needs_review_delta_threshold,
                                    write_vals.get('months_of_stock_after_order', 0.0),
                                    config.overstock_ceiling_months,
                                    write_vals.get('sales_pattern', False),
                                    _bulk_conc,
                                )
                            )
                            if draft_po_note:
                                write_vals['needs_review'] = True
                                write_vals['needs_review_reason'] = (
                                    draft_po_note + ' ' + (write_vals['needs_review_reason'] or '')
                                ).strip()
                                write_vals['notes'] = f'⚠️ {draft_po_note}\n\n' + (write_vals.get('notes') or '')
                            write_vals['active'] = True
                            if existing_rec.snoozed_until:
                                write_vals.pop('snoozed_until', None)
                                write_vals.pop('snoozed_note', None)
                                if existing_rec.is_snoozed:
                                    write_vals['reorder_needed'] = False
                            to_write.append((existing_rec, write_vals))
                        else:
                            vals['prior_suggested_qty'] = 0.0
                            vals['delta_pct'] = self._calc_delta_pct(0.0, vals['suggested_reorder_qty'])
                            # Skip bulk-concentration flag when buyer confirmed bulk-regular
                            _bulk_conc_new = vals.get('has_bulk_concentration', False)
                            if vals.get('reorder_behavior') == 'bulk_regular':
                                _bulk_conc_new = False
                            vals['needs_review'], vals['needs_review_reason'] = self._compute_needs_review(
                                vals['avg_monthly_demand'], vals['suggested_reorder_qty'],
                                vals['is_dead_stock'], vals['delta_pct'],
                                config.needs_review_delta_threshold,
                                vals.get('months_of_stock_after_order', 0.0),
                                config.overstock_ceiling_months,
                                vals.get('sales_pattern', False),
                                _bulk_conc_new,
                            )
                            to_create.append(vals)

                    # These keys are needed in-run (budget ranking, needs-review
                    # triage) but are stored COMPUTED fields on the model with
                    # no inverse — persisting them is at best a no-op the ORM
                    # recomputes identically, so strip them before writing.
                    _computed_keys = (
                        'estimated_purchase_value',
                        'months_of_stock_after_order',
                        'is_overstocked',
                    )
                    # tracking_disable: these are machine-refreshed advisory
                    # rows updated in bulk every run; per-field chatter tracking
                    # here generated thousands of mail messages per cron on a
                    # large catalog. The run log is the audit trail.
                    Sugg = self.sudo().with_context(tracking_disable=True)
                    if to_create:
                        for vals in to_create:
                            for key in _computed_keys:
                                vals.pop(key, None)
                        Sugg.create(to_create)
                        total_created += len(to_create)
                    for rec, vals in to_write:
                        for key in _computed_keys:
                            vals.pop(key, None)
                        rec.sudo().with_context(tracking_disable=True).write(vals)
                        total_created += 1

                    Snapshot = self.env['smart.reorder.forecast.snapshot'].sudo()
                    
                    # Skip-if-pending rule: fetch product IDs that have unevaluated snapshots for this company/warehouse
                    existing_pending_pids = set(Snapshot.search([
                        ('company_id', '=', company.id),
                        ('warehouse_id', '=', warehouse.id),
                        ('evaluated', '=', False)
                    ]).mapped('product_id.id'))

                    snapshots_to_create = []
                    for _, _, _, vals in suggestion_values:
                        pid = vals['product_id']
                        if pid in existing_pending_pids:
                            continue

                        # Snapshot scope filtering
                        scope = getattr(config, 'snapshot_scope', 'ab_only')
                        if scope == 'ab_only' and vals.get('abc_class') not in ('A', 'B'):
                            continue
                        elif scope == 'reorder_only' and not vals.get('reorder_needed'):
                            continue

                        snapshots_to_create.append({
                            'company_id':       company.id,
                            'warehouse_id':     warehouse.id,
                            'product_id':       pid,
                            'snapshot_date':    date.today(),
                            'forecast_demand':  vals['avg_monthly_demand'],
                            'confidence':       vals['confidence'],
                            'lead_time_days':   max(1, int(vals['lead_time_months'] * 30)),
                            'abc_class':        vals['abc_class'],
                            'evaluated':        False,
                        })
                    if snapshots_to_create:
                        Snapshot.create(snapshots_to_create)

                    if config.snapshot_retention_months > 0:
                        limit_date = date.today() - relativedelta(months=config.snapshot_retention_months)
                        Snapshot.search([
                            ('company_id', '=', company.id),
                            ('snapshot_date', '<', limit_date)
                        ]).unlink()

                    _logger.info('SmartReorder: %d created / %d updated for %s/%s',
                        len(to_create), len(to_write), company.name, warehouse.name)
            except Exception as e:
                # The savepoint above rolled back everything this warehouse's
                # batch did (e.g. orphan archiving) and restored a clean
                # transaction, so the search/write calls below are guaranteed
                # to work even if the failure was a genuine DB error, not just
                # a Python exception.
                had_errors = True
                _logger.exception(
                    'SmartReorder: Batch failed for %s/%s — falling back to existing '
                    'data instead of refreshing or removing it. %s',
                    company.name, warehouse.name, e
                )
                stale_records = self.sudo().search([
                    ('company_id',   '=', company.id),
                    ('warehouse_id', '=', warehouse.id),
                ])
                if stale_records:
                    last_known_date = max(
                        (r.analysis_date for r in stale_records if r.analysis_date),
                        default=None,
                    )
                    shown_from = f'showing data from {last_known_date}' if last_known_date \
                        else 'showing previously saved data'
                    stale_records.write({
                        'is_stale': True,
                        'stale_reason': (
                            f'Could not refresh — {type(e).__name__} during last run on '
                            f'{date.today()} ({shown_from}).'
                        ),
                    })
                log.sudo().write({
                    'error_count': log.error_count + 1,
                    'error_notes': (
                        (log.error_notes or '') +
                        f'\n[{date.today()}] {warehouse.name}: '
                        f'{type(e).__name__} — {e}'
                    ).strip(),
                })
                continue

        # Notifications + email report
        self._send_notifications(company, config, total_critical)
        if config.send_email_report:
            had_errors = had_errors or (self._send_email_report(company, config) is False)

        return total_created, total_critical, had_errors

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    @api.model
    def _get_config(self, company_id):
        config = self.env['smart.reorder.config'].sudo().search(
            [('company_id', '=', company_id)], limit=1
        )
        if not config:
            company = self.env['res.company'].sudo().browse(company_id)
            raise UserError(_(
                'No Reorder Advisor configuration found for company "%s".\n'
                'Go to Reorder Advisor → Configuration → Company Settings and save a config first.'
            ) % company.name)
        return config

    # ── Vendor Performance — on-demand only (FIX 8: removed from bulk loop) ──

    def action_refresh_vendor_performance(self):
        """
        Calculates actual vs stated vendor lead time for THIS product only.
        Called from the form view button — never runs in bulk loop.
        """
        self.ensure_one()
        if not self.vendor_id:
            raise UserError(_('No vendor set for this product.'))

        pos = self.env['purchase.order'].sudo().search([
            ('partner_id', '=', self.vendor_id.id),
            ('state', '=', 'done'),
            ('date_approve', '!=', False),
        ], order='date_approve desc', limit=10)

        po_lines = self.env['purchase.order.line'].sudo().search([
            ('product_id', '=', self.product_id.id),
            ('order_id', 'in', pos.ids),
        ]).sorted(key=lambda l: l.order_id.date_approve, reverse=True)

        if len(po_lines) < 2:
            self.sudo().write({
                'vendor_performance_note': 'Not enough PO history (need ≥ 2 completed POs).'
            })
            return

        total_days, count = 0, 0
        for line in po_lines:
            if line.order_id.date_approve and line.date_planned:
                delta = (line.date_planned.date() - line.order_id.date_approve.date()).days
                if delta > 0:
                    total_days += delta
                    count += 1

        if count == 0:
            self.sudo().write({'vendor_performance_note': 'Could not calculate from available data.'})
            return

        actual_avg = round(total_days / count, 1)
        note = self._build_vendor_perf_note(actual_avg, self.vendor_stated_lead_days, with_emoji=True)

        self.sudo().write({
            'vendor_actual_avg_days':  actual_avg,
            'vendor_performance_note': note,
        })

    # ── Notifications ─────────────────────────────────────────────────────────

    @api.model
    def _send_notifications(self, company, config, critical_count, warehouse_id=None):
        from .reorder_notifier import send_notifications
        return send_notifications(self, company, config, critical_count, warehouse_id=warehouse_id)

    # ── Email PDF Report ──────────────────────────────────────────────────────

    @api.model
    def _send_email_report(self, company, config):
        from .reorder_notifier import send_email_report
        return send_email_report(self, company, config)

    # ── Phase 3: Auto-flag on negative delivery ───────────────────────────────

    @api.model
    def _flag_negative_stock_product(self, product_id, warehouse_id, company_id):
        # Private (underscore) on purpose: this creates/overwrites suggestions
        # via sudo with caller-chosen product/warehouse/company, so it must not
        # be reachable over RPC. It is only called from the stock.picking
        # validation hook, which runs for whichever user validates a delivery.
        config = self._get_config(company_id)
        if not config.auto_flag_on_negative:
            return

        # standard_price is company-dependent — must be read in the context of
        # the company being flagged, or it can resolve to the wrong company's cost.
        product = self.env['product.product'].with_company(company_id).sudo().browse(product_id)
        location  = self.env['stock.warehouse'].sudo().browse(warehouse_id).lot_stock_id
        quants    = self.env['stock.quant'].sudo().search([
            ('product_id',  '=', product_id),
            ('location_id', 'child_of', location.id),
        ])
        qty_on_hand = sum(quants.mapped('quantity'))

        # Search for primary supplierinfo to get standard MOQ / price / lead time
        supplierinfo = self.env['product.supplierinfo'].sudo().search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            '|', ('company_id', '=', False), ('company_id', '=', company_id)
        ], order='sequence asc', limit=1)

        moq = supplierinfo.min_qty or 1.0 if supplierinfo else 1.0

        round_to_moq = self._round_to_moq
        abs_neg_qty = abs(qty_on_hand) if qty_on_hand < 0 else 0.0
        
        existing = self.sudo().with_context(active_test=False).search([
            ('product_id',   '=', product_id),
            ('warehouse_id', '=', warehouse_id),
            ('company_id',   '=', company_id),
        ], limit=1)

        # Retrieve supplier pricelist currency-converted vendor price
        vendor_price = 0.0
        vendor_id = False
        stated_lead_days = config.default_lead_time_months * 30
        if supplierinfo:
            vendor_id = supplierinfo.partner_id.id
            stated_lead_days = supplierinfo.delay or stated_lead_days
            price = supplierinfo.price or 0.0
            currency_id = supplierinfo.currency_id.id if supplierinfo.currency_id else False
            company_rec = self.env['res.company'].browse(company_id)
            if price > 0.0 and currency_id and currency_id != company_rec.currency_id.id:
                currency_obj = self.env['res.currency'].browse(currency_id)
                vendor_price = currency_obj._convert(
                    price,
                    company_rec.currency_id,
                    company_rec,
                    date.today(),
                )
            else:
                vendor_price = price
        else:
            vendor_price = product.standard_price

        cost = product.standard_price
        lead_months = stated_lead_days / 30.0

        if existing:
            # Reuse the last known monthly demand if suggestion exists
            avg_monthly = existing.avg_monthly_demand
            qty_incoming = existing.qty_incoming
            qty_outgoing = existing.qty_outgoing
            qty_available = qty_on_hand + qty_incoming - qty_outgoing
            
            lead_months_to_use = existing.lead_time_months or lead_months
            safety_buffer = existing.safety_buffer_months
            order_cycle = existing.order_cycle_months
            moq_to_use = existing.moq or moq
            
            min_level = avg_monthly * (lead_months_to_use + safety_buffer)
            max_level = min_level + avg_monthly * order_cycle
            is_triggered = (qty_available < min_level) or (qty_on_hand < 0)
            raw_qty = max(0.0, max_level - qty_available) if is_triggered else 0.0
            suggested_qty = round_to_moq(raw_qty, moq_to_use)
            suggested_qty = max(suggested_qty, round_to_moq(abs_neg_qty, moq_to_use))
            
            cost = existing.product_cost or cost
            vendor_price = existing.vendor_price or vendor_price
            vendor_id = existing.vendor_id.id if existing.vendor_id else vendor_id
            stated_lead_days = existing.vendor_stated_lead_days or stated_lead_days
            moq = moq_to_use
        else:
            suggested_qty = round_to_moq(abs_neg_qty, moq)

        notes = (
            f'⚠️ AUTO-FLAGGED (PROVISIONAL): Stock went negative to {qty_on_hand:.2f} on {date.today()}.\n'
            f'Provisional emergency suggestion computed: rounded to MOQ={moq:.0f}.\n'
            f'Run full analysis to recalculate complete suggestion.'
        )

        # qty_available, reorder_value and estimated_purchase_value are stored
        # computed fields — the ORM derives them from the plain fields written
        # below, so they are deliberately absent from this vals dict.
        vals = {
            'company_id':               company_id,
            'warehouse_id':             warehouse_id,
            'product_id':               product_id,
            'qty_on_hand':              qty_on_hand,
            'suggested_reorder_qty':    suggested_qty,
            'urgency':                  'critical',
            'reorder_needed':           True,
            'is_provisional':           True,
            'analysis_date':            date.today(),
            'active':                   True,
            'notes':                    notes,
            'product_cost':             cost,
            'vendor_price':             vendor_price,
            'vendor_id':                vendor_id,
            'vendor_stated_lead_days':  stated_lead_days,
            'moq':                      moq,
        }

        if existing:
            existing.sudo().write(vals)
        else:
            self.sudo().create(vals)

        self._send_notifications(
            self.env['res.company'].browse(company_id), config, 1, warehouse_id=warehouse_id
        )

    # ── Phase 3: Draft PO ─────────────────────────────────────────────────────

    def action_create_draft_po(self):
        self.ensure_one()
        # ── Security guard ─────────────────────────────────────────────
        if not self.env.user.has_group(
                'smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can create Draft POs.'))
        if self.company_id.id not in self.env.user.company_ids.ids:
            raise UserError(_('You do not have access to company %s and cannot create purchase orders for it.') % self.company_id.name)
        config = self._get_config(self.company_id.id)
        if not config.allow_draft_po:
            raise UserError(_('Draft PO creation is disabled. Enable in Configuration → Company Settings.'))
        if not self.reorder_needed:
            raise UserError(_('This product does not need reordering.'))

        vendor = self.vendor_id or config.default_vendor_id
        if not vendor:
            raise UserError(_(
                'No vendor for %s. Set a vendor on the product or configure a Default Vendor in settings.'
            ) % self.product_id.display_name)

        seller = self.product_id._select_seller(partner_id=vendor, quantity=self.suggested_reorder_qty)
        po_price = seller.price if seller else self.product_cost

        picking_type = self.env['stock.picking.type'].sudo().search([
            ('code', '=', 'incoming'),
            ('warehouse_id', '=', self.warehouse_id.id),
        ], limit=1)
        if not picking_type:
            raise UserError(_('No incoming operation type found for warehouse %s.') % self.warehouse_id.name)

        po = self.env['purchase.order'].sudo().create({
            'partner_id': vendor.id,
            'company_id': self.company_id.id,
            'picking_type_id': picking_type.id,
            'order_line': [Command.create({
                'product_id':  self.product_id.id,
                'product_qty': self.suggested_reorder_qty,
                'price_unit':  po_price,
                'name': (
                    f'{self.product_id.display_name} — Reorder Advisor '
                    f'{date.today()} (avg {self.avg_monthly_demand:.1f}/month)'
                ),
            })],
            'notes': (
                f'Auto-generated by Smart Reorder Advisor\n'
                f'Analysis: {self.analysis_date} | Urgency: {self.urgency}\n'
                f'Avg monthly demand: {self.avg_monthly_demand:.2f}'
            ),
        })
        self.sudo().write({
            'po_ids': [Command.link(po.id)],
            'reorder_needed': False,
        })
        return {
            'type':      'ir.actions.act_window',
            'name':      'Draft Purchase Order',
            'res_model': 'purchase.order',
            'res_id':    po.id,
            'view_mode': 'form',
            'target':    'current',
        }

    def action_create_internal_transfer(self):
        self.ensure_one()
        # ── Security guard ─────────────────────────────────────────────
        if not self.env.user.has_group(
                'smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can create Internal Transfers.'))
        if self.company_id.id not in self.env.user.company_ids.ids:
            raise UserError(_('You do not have access to company %s and cannot create internal transfers for it.') % self.company_id.name)
        if not self.transfer_source_warehouse_id:
            raise UserError(_('No transfer source warehouse recommended.'))
        if not self.reorder_needed:
            raise UserError(_('This suggestion does not require any replenishment.'))
        # Use the donor-surplus-capped quantity, NOT the full reorder qty —
        # transferring the full reorder qty could strip the donor warehouse
        # below its own lead-time demand, which the cap exists to prevent.
        transfer_qty = self.transfer_suggested_qty
        if transfer_qty <= 0:
            raise UserError(_(
                'No transferable surplus quantity is recorded for this suggestion. '
                'Re-run the analysis to refresh the transfer recommendation.'
            ))

        # Find internal transfer picking type for destination warehouse
        picking_type = self.env['stock.picking.type'].sudo().search([
            ('warehouse_id', '=', self.warehouse_id.id),
            ('code', '=', 'internal')
        ], limit=1)
        if not picking_type:
            # Fallback to any internal picking type in company
            picking_type = self.env['stock.picking.type'].sudo().search([
                ('company_id', '=', self.company_id.id),
                ('code', '=', 'internal')
            ], limit=1)
        if not picking_type:
            raise UserError(_("No internal transfer operation type found for company %s.") % self.company_id.name)

        picking_vals = {
            'picking_type_id': picking_type.id,
            'location_id': self.transfer_source_warehouse_id.lot_stock_id.id,
            'location_dest_id': self.warehouse_id.lot_stock_id.id,
            'company_id': self.company_id.id,
            'origin': f'Reorder Advisor — {self.product_id.display_name}',
            'move_ids': [Command.create({
                'name': self.product_id.display_name,
                'product_id': self.product_id.id,
                'product_uom_qty': transfer_qty,
                'product_uom': self.product_id.uom_id.id,
                'location_id': self.transfer_source_warehouse_id.lot_stock_id.id,
                'location_dest_id': self.warehouse_id.lot_stock_id.id,
            })]
        }
        # BUG 5 FIX: Leave in DRAFT so manager reviews before confirming.
        picking = self.env['stock.picking'].sudo().create(picking_vals)

        return {
            'type':      'ir.actions.act_window',
            'name':      'Internal Transfer',
            'res_model': 'stock.picking',
            'res_id':    picking.id,
            'view_mode': 'form',
            'target':    'current',
        }

    # ── Cron entry point ──────────────────────────────────────────────────────

    @api.model
    def action_run_weekly_cron(self):
        t0 = time.time()
        _logger.info('SmartReorder CRON: Starting...')
        # T-19: Pass cron start time so the safety cap inside generate_suggestions()
        # measures total elapsed wall-clock time from the very beginning of this job.
        result = self.generate_suggestions(_cron_start=t0)
        elapsed = round(time.time() - t0, 1)
        _logger.info(
            'SmartReorder CRON: Done in %.1fs — %d records, %d critical',
            elapsed, result.get('created', 0), result.get('critical', 0)
        )
        # Score forecast snapshots during cron run (Feature 15)
        self.env['smart.reorder.forecast.snapshot'].sudo()._score_snapshots()

    @api.model
    def get_dashboard_data(self, warehouse_id=None):
        """
        Single RPC call for the OWL dashboard. Returns all KPI data at once.

        T-24: counts and sums are computed DB-side (read_group / search_count)
        instead of search_read()-ing every field of every suggestion into Python
        and summing in a loop. At 7,000+ SKUs across multiple warehouses, the old
        approach loaded the whole table into memory on every dashboard page view —
        progressively slower as the catalog grows, with real timeout/OOM risk.
        Only the small, bounded "top 10" lists still fetch actual rows — each is
        its own scoped search_read() with an explicit order and limit.
        """
        # Scope every query in this method to the calling user's allowed
        # companies, matching controllers/main.py's banner pattern. This method
        # used to run everything under sudo() with no company filter at all —
        # any Reorder User could pull KPIs, top-10 lists, and back-test accuracy
        # across every company in the database, bypassing the module's own
        # record rules (rule_suggestion_company etc.). Sudo is kept only where a
        # Reorder User genuinely lacks read access to the underlying model
        # (stock.warehouse) — never as a substitute for the company filter,
        # which is applied unconditionally below regardless of sudo.
        company_ids = self.env.companies.ids
        Suggestion = self  # no sudo: rely on the model's own ACL + record rule,
                           # reinforced by the explicit company filter below
        base_domain = [('company_id', 'in', company_ids)]
        if warehouse_id:
            base_domain.append(('warehouse_id', '=', warehouse_id))

        # ── Counts, grouped DB-side (one SQL GROUP BY each, no record loading) ──
        urgency_counts = {
            g['urgency']: g.get('__count') or g.get('urgency_count') or 0
            for g in Suggestion.read_group(base_domain, ['urgency'], ['urgency'])
            if g['urgency']
        }
        abc_counts = {
            g['abc_class']: g.get('__count') or g.get('abc_class_count') or 0
            for g in Suggestion.read_group(base_domain, ['abc_class'], ['abc_class'])
            if g['abc_class']
        }
        trend_counts = {
            g['demand_trend']: g.get('__count') or g.get('demand_trend_count') or 0
            for g in Suggestion.read_group(base_domain, ['demand_trend'], ['demand_trend'])
            if g['demand_trend']
        }
        dead_cnt = Suggestion.search_count(base_domain + [('is_dead_stock', '=', True)])

        # ── Sales pattern counts (the OWL dashboard's pattern KPI tiles) ──
        pattern_counts = {
            g['sales_pattern']: g.get('__count') or g.get('sales_pattern_count') or 0
            for g in Suggestion.read_group(base_domain, ['sales_pattern'], ['sales_pattern'])
            if g['sales_pattern']
        }

        # ── Reorder value + within-budget value/count — grouped sums, converted
        # per-currency: in a multi-company view the records can carry different
        # currencies, and summing raw amounts across currencies produced a
        # meaningless headline number. Everything is expressed in the current
        # company's currency. ──
        target_currency = self.env.company.currency_id

        def _to_target(amount, currency_id):
            if not amount:
                return 0.0
            if not currency_id or currency_id == target_currency.id:
                return amount
            return self.env['res.currency'].sudo().browse(currency_id)._convert(
                amount, target_currency, self.env.company, fields.Date.today()
            )

        budget_groups = Suggestion.read_group(
            base_domain + [('reorder_needed', '=', True)],
            ['estimated_purchase_value:sum'], ['within_budget', 'currency_id'],
            lazy=False,
        )
        total_rv = 0.0
        budget_rv = 0.0
        budget_cnt = 0
        for g in budget_groups:
            cid = g['currency_id'][0] if g.get('currency_id') else False
            amount = _to_target(g.get('estimated_purchase_value') or 0.0, cid)
            total_rv += amount
            if g.get('within_budget'):
                budget_rv += amount
                budget_cnt += g.get('__count') or 0
        currency = target_currency.name

        # ── Last analysis date — single small lookup, not a full scan ──
        date_group = Suggestion.read_group(base_domain, ['analysis_date:max'], [])
        last_date = (
            str(date_group[0]['analysis_date'])
            if date_group and date_group[0].get('analysis_date') else None
        )

        # ── Top 10 lists — already small, bounded result sets: dedicated
        # search_read() per list with an explicit domain/order/limit, same
        # sort criteria and field selection as before. ──────────────────────
        top = {
            'critical': Suggestion.search_read(
                base_domain + [('urgency', '=', 'critical')],
                ['default_code', 'product_id', 'qty_on_hand', 'suggested_reorder_qty', 'estimated_purchase_value'],
                order='qty_on_hand asc', limit=10,
            ),
            'urgent': Suggestion.search_read(
                base_domain + [('urgency', '=', 'urgent')],
                ['default_code', 'product_id', 'months_of_stock', 'avg_monthly_demand', 'suggested_reorder_qty'],
                order='months_of_stock asc', limit=10,
            ),
            'dead': Suggestion.search_read(
                base_domain + [('is_dead_stock', '=', True)],
                ['default_code', 'product_id', 'qty_on_hand', 'months_since_last_sale', 'last_sale_date'],
                order='months_since_last_sale desc', limit=10,
            ),
            'rising': Suggestion.search_read(
                base_domain + [('demand_trend', '=', 'up'), ('reorder_needed', '=', True)],
                ['default_code', 'product_id', 'avg_monthly_demand', 'trend_pct', 'estimated_purchase_value'],
                order='trend_pct desc', limit=8,
            ),
            'falling': Suggestion.search_read(
                base_domain + [('demand_trend', '=', 'down')],
                ['default_code', 'product_id', 'avg_monthly_demand', 'trend_pct', 'estimated_purchase_value'],
                order='trend_pct asc', limit=8,
            ),
        }

        # Backtest forecast scoring aggregation (Feature 15). No company record
        # rule exists on smart.reorder.forecast.snapshot, so the company filter
        # must be explicit here — it is not a safety net, it is the only guard.
        Snapshot = self.env['smart.reorder.forecast.snapshot']
        backtest_domain = [('evaluated', '=', True), ('company_id', 'in', company_ids)]
        if warehouse_id:
            backtest_domain.append(('warehouse_id', '=', warehouse_id))

        abc_groups = Snapshot.read_group(
            backtest_domain,
            ['absolute_error_pct:avg'],
            ['abc_class']
        )
        mape_by_abc = {
            (g['abc_class'] or 'Unknown'): round(g['absolute_error_pct'] or 0.0, 1)
            for g in abc_groups
        }

        wh_groups = Snapshot.read_group(
            [('evaluated', '=', True), ('company_id', 'in', company_ids)],
            ['absolute_error_pct:avg'],
            ['warehouse_id']
        )
        mape_by_warehouse = {
            (g['warehouse_id'][1] if g['warehouse_id'] else 'Unknown'): round(g['absolute_error_pct'] or 0.0, 1)
            for g in wh_groups
        }

        overall_group = Snapshot.read_group(
            backtest_domain,
            ['absolute_error_pct:avg'],
            []
        )
        overall_mape = round(overall_group[0]['absolute_error_pct'] or 0.0, 1) if overall_group and overall_group[0].get('absolute_error_pct') else 0.0
        
        backtest_data = {
            'mape_by_abc': mape_by_abc,
            'mape_by_warehouse': mape_by_warehouse,
            'overall_mape': overall_mape,
        }

        # A Reorder User may lack direct Inventory access to stock.warehouse,
        # so sudo is kept here for the read itself — but the company filter
        # still applies unconditionally, so it can never surface a warehouse
        # from a company outside the caller's allowed set.
        warehouses = self.env['stock.warehouse'].sudo().search_read(
            [('company_id', 'in', company_ids)], ['id', 'name', 'company_id'], limit=50)
        return {
            'urgency': urgency_counts, 'abc': abc_counts, 'trend': trend_counts,
            'sales_pattern': pattern_counts,
            'total_reorder_value': total_rv, 'within_budget_value': budget_rv,
            'budget_count': budget_cnt, 'dead_count': dead_cnt,
            'currency': currency, 'last_analysis_date': last_date,
            'top': top, 'warehouses': warehouses,
            'backtest': backtest_data,
        }

    # ── Utility actions ───────────────────────────────────────────────────────

    def _check_snooze_access(self):
        """Snoozing is a buyer (Reorder User) capability. The snooze actions
        write via sudo() because the User group is otherwise read-only on this
        model, so the group must be checked explicitly here — without it, ANY
        authenticated user could suppress reorder flags over RPC."""
        if not self.env.user.has_group(_USER_GROUP):
            raise AccessError(_(
                'Snoozing suggestions requires the Reorder Advisor User group.'
            ))
        allowed_company_ids = set(self.env.user.company_ids.ids)
        for rec in self:
            if rec.company_id.id not in allowed_company_ids:
                raise AccessError(_(
                    'You do not have access to company %s.'
                ) % rec.company_id.name)

    # T-17: Snooze actions
    def action_snooze(self):
        """Open a wizard-like dialog via context to let the buyer pick snooze duration."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Snooze — {self.product_id.display_name}',
            'res_model': 'smart.reorder.suggestion',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'flags': {'mode': 'edit'},
            'context': {'snooze_mode': True},
        }

    def action_snooze_7(self):
        """Snooze this suggestion for 7 days. Records audit note automatically."""
        self.ensure_one()
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until': date.today() + timedelta(days=7),
            'snoozed_note':  f'Snoozed 7 days by {self.env.user.name} on {date.today()}',
            'reorder_needed': False,
        })

    def action_snooze_30(self):
        """Snooze this suggestion for 30 days. Records audit note automatically."""
        self.ensure_one()
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until': date.today() + timedelta(days=30),
            'snoozed_note':  f'Snoozed 30 days by {self.env.user.name} on {date.today()}',
            'reorder_needed': False,
        })

    def action_unsnooze(self):
        """Clear the snooze — the next cron run will re-evaluate this product."""
        self.ensure_one()
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until': False,
            'snoozed_note': False,
            'reorder_needed': self.suggested_reorder_qty > 0 or self.qty_on_hand < 0,
        })

    # ── Bulk Snooze (T-33) ────────────────────────────────────────────────────
    # Triggered from the list view's Action menu when one or more rows are
    # checkbox-selected (see the ir.actions.server bindings in the view file) —
    # lets a buyer clear a batch of noise (e.g. right after a shipment lands and
    # several SKUs are known to be covered) in one click instead of opening each
    # suggestion individually.

    def action_bulk_snooze_7(self):
        self._bulk_snooze(7)

    def action_bulk_snooze_30(self):
        self._bulk_snooze(30)

    def _bulk_snooze(self, days):
        if not self:
            return
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until':  date.today() + timedelta(days=days),
            'snoozed_note':   f'Bulk-snoozed {days} days by {self.env.user.name} on '
                               f'{date.today()} ({len(self)} suggestions).',
            'reorder_needed': False,
        })

    def action_open_product(self):
        self.ensure_one()
        return {
            'type':      'ir.actions.act_window',
            'res_model': 'product.product',
            'res_id':    self.product_id.id,
            'view_mode': 'form',
            'target':    'current',
        }

    def action_open_stock_moves(self):
        self.ensure_one()
        return {
            'type':      'ir.actions.act_window',
            'name':      f'Stock Moves — {self.product_id.display_name}',
            'res_model': 'stock.move',
            'view_mode': 'list,form',
            'domain': [
                ('product_id', '=', self.product_id.id),
                ('picking_id.picking_type_id.warehouse_id', '=', self.warehouse_id.id),
                ('state', '=', 'done'),
            ],
        }

    def action_mark_one_time_order(self):
        """Manager-only. Writes the behavior flag to the product and updates ONLY
        this suggestion record in place — no full company/warehouse regeneration.
        A synchronous full re-run from a form button froze the screen for minutes
        on large catalogs, mislabeled Run History as a scheduled run, and could be
        silently skipped if the weekly cron held the per-company lock."""
        self.ensure_one()
        if not self.env.user.has_group('smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can change how this part is ordered.'))

        self.product_id.product_tmpl_id.reorder_behavior = 'against_order'
        self.message_post(body=_("Marked as 'Order Only Against Customer Order'. Suggestion suppressed."))

        urgency = self._determine_urgency(
            self.qty_on_hand, self.months_of_stock, self.avg_monthly_demand,
            self.lead_time_months, self.is_dead_stock, 0.0,
        )
        self.write({
            'raw_reorder_qty':       0.0,
            'suggested_reorder_qty': 0.0,
            'reorder_needed':        self.qty_on_hand < 0,
            'urgency':               urgency,
            'needs_review':          False,
            'needs_review_reason':   _(
                'Cleared: marked as one-time order by %(user)s on %(date)s.'
            ) % {'user': self.env.user.name, 'date': date.today()},
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title':   _('Suggestion Updated'),
                'message': _('Reorder quantity suppressed for this part.'),
                'type':    'success',
                'sticky':  False,
            },
        }

    def action_mark_regular_order(self):
        """Manager-only. Recomputes THIS product's forecast/qty locally from figures
        already stored on the record (total_qty_sold, analysis_months, lead/safety/
        order-cycle months, MOQ, stock position) — the same plain-average formula
        the engine uses for reorder_behavior == 'bulk_regular' — instead of
        triggering a full company/warehouse regeneration. See action_mark_one_time_order."""
        self.ensure_one()
        if not self.env.user.has_group('smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can change how this part is ordered.'))

        self.product_id.product_tmpl_id.reorder_behavior = 'bulk_regular'
        self.message_post(body=_(
            "Marked as 'Customer Buys in Bulk Regularly'. Forecast recalculated using "
            "the plain monthly average (outlier rejection bypassed)."
        ))

        avg_monthly = (self.total_qty_sold / self.analysis_months) if self.analysis_months else 0.0
        min_level = avg_monthly * (self.lead_time_months + self.safety_buffer_months)
        max_level = min_level + avg_monthly * self.order_cycle_months
        is_triggered = (self.qty_available < min_level) or (self.qty_on_hand < 0)
        raw_qty = max(0.0, max_level - self.qty_available) if is_triggered else 0.0
        suggested_qty = self._round_to_moq(raw_qty, self.moq)
        months_of_stock = self._calc_months_of_stock(self.qty_available, avg_monthly)
        urgency = self._determine_urgency(
            self.qty_on_hand, months_of_stock, avg_monthly,
            self.lead_time_months, self.is_dead_stock, suggested_qty,
        )

        self.write({
            'avg_monthly_demand':      avg_monthly,
            'excluded_outlier_months': 0,
            'demand_forecast_note':    _(
                'Plain monthly average — outlier rejection bypassed '
                '(Customer Buys in Bulk Regularly).'
            ),
            'min_stock_level':      min_level,
            'max_stock_level':      max_level,
            'raw_reorder_qty':      raw_qty,
            'suggested_reorder_qty': suggested_qty,
            'reorder_needed':       suggested_qty > 0 or self.qty_on_hand < 0,
            'urgency':              urgency,
            'sales_pattern':        'regular',
            'needs_review':         False,
            'needs_review_reason':  _(
                'Cleared: marked as regular bulk order by %(user)s on %(date)s.'
            ) % {'user': self.env.user.name, 'date': date.today()},
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title':   _('Forecast Updated'),
                'message': _('Recalculated using the plain monthly average. Suggested reorder qty: %s.') % suggested_qty,
                'type':    'success',
                'sticky':  False,
            },
        }
