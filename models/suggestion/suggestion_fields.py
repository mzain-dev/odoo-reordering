from odoo import models, fields, api, _, Command
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from psycopg2 import IntegrityError
import logging
import time

from ...utils.access import require_group
from ..reorder_engine import (
    calc_months_of_stock,
    round_to_moq,
    classify_abc,
    determine_urgency,
    calc_trend,
    calc_seasonal_note,
    compute_robust_monthly_demand,
    calc_delta_pct,
    compute_needs_review,
    compute_confidence_score,
    build_vendor_perf_note,
)

_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'
_USER_GROUP = 'smart_reorder_advisor.group_smart_reorder_user'
# Recommendation: field-level access for vendor cost/pricing data — a plain
# Reorder User no longer sees these by default; Cost Viewer or Manager does.
# Enforced by the ORM itself (fields_get/read), not just hidden in a view.
_COST_GROUPS = 'smart_reorder_advisor.group_smart_reorder_cost_viewer,smart_reorder_advisor.group_smart_reorder_manager'

_logger = logging.getLogger(__name__)


class SmartReorderSuggestion(models.Model):
    """
    Smart Reorder Suggestion — one record per product per warehouse.
    """
    _name = 'smart.reorder.suggestion'
    _inherit = ['mail.thread', 'mail.activity.mixin']
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
        string='Unit Cost', digits=(16, 3), readonly=True, groups=_COST_GROUPS,
    )

    # ── Analysis Window ───────────────────────────────────────────────────────
    analysis_date   = fields.Date(string='Analysis Date', default=fields.Date.today, readonly=True)
    analysis_months = fields.Integer(string='Analysis Window (Months)', readonly=True)
    date_from       = fields.Date(string='Period From', readonly=True)
    date_to         = fields.Date(string='Period To', readonly=True)

    # ── Monthly Demand ────────────────────────────────────────────────────────
    total_qty_sold      = fields.Float(string='Total Qty Sold (Period)', digits='Product Unit of Measure', readonly=True)
    avg_monthly_demand  = fields.Float(string='Avg Monthly Demand',      digits='Product Unit of Measure', readonly=True,
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
    min_stock_level     = fields.Float(string='Min Stock Level',          digits='Product Unit of Measure', readonly=True,
                                        help='Min Stock Level (Reorder Point): average monthly demand × (lead time months + safety buffer months).')
    max_stock_level     = fields.Float(string='Max Stock Level',          digits='Product Unit of Measure', readonly=True,
                                        help='Max Stock Level (Order-up-to Level): Min Stock Level + average monthly demand × order cycle.')
    moq                 = fields.Float(string='Min Order Qty (MOQ)',      digits='Product Unit of Measure', readonly=True,
                                        help='Minimum quantity required by the vendor for a purchase.')

    # ── Trend (Phase 2) ───────────────────────────────────────────────────────
    prev_period_qty_sold = fields.Float(string='Prev Period Qty Sold', digits='Product Unit of Measure', readonly=True)
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
    same_period_last_year_qty = fields.Float(string='Same Period Last Year', digits='Product Unit of Measure', readonly=True)
    seasonal_note             = fields.Char(string='Seasonal Note', readonly=True)

    # ── Stock Position ────────────────────────────────────────────────────────
    qty_on_hand  = fields.Float(string='On Hand Qty',   digits='Product Unit of Measure', readonly=True)
    qty_incoming = fields.Float(string='Incoming (PO)', digits='Product Unit of Measure', readonly=True)
    qty_outgoing = fields.Float(string='Outgoing (Reserved)', digits='Product Unit of Measure', readonly=True)
    qty_available = fields.Float(
        string='Net Available', digits='Product Unit of Measure', readonly=True,
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
    suggested_reorder_qty = fields.Integer(  string='Suggested Reorder Qty',        readonly=True, tracking=True)
    raw_reorder_qty       = fields.Float(   string='Raw Qty (before MOQ rounding)', digits='Product Unit of Measure', readonly=True)
    prior_suggested_qty   = fields.Integer(
        string='Prior Suggested Qty', readonly=True,
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
                                            readonly=True, compute='_compute_reorder_value', store=True,
                                            groups=_COST_GROUPS)
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
        groups=_COST_GROUPS,
        help='Supplier pricelist price converted to company currency at the analysis run rate.',
    )

    estimated_purchase_value = fields.Monetary(
        string='Est. Purchase Value',
        currency_field='currency_id',
        compute='_compute_purchase_value',
        store=True,
        groups=_COST_GROUPS,
        help='Suggested quantity multiplied by vendor price (converted to company currency).',
    )

    # ── Vendor ────────────────────────────────────────────────────────────────
    vendor_id              = fields.Many2one('res.partner', string='Primary Vendor', readonly=True)
    vendor_stated_lead_days= fields.Integer(string='Vendor Stated Lead (Days)', readonly=True)
    vendor_actual_avg_days = fields.Float(  string='Vendor Actual Avg Lead (Days)', digits=(16, 1), readonly=True)
    vendor_performance_note= fields.Char(   string='Vendor Performance', readonly=True)

    # ── Last Purchase Cost (T-Task1) ─────────────────────────────────────────
    last_purchase_cost = fields.Float(
        string='Last Purchase Cost', digits=(16, 3), readonly=True, groups=_COST_GROUPS,
        help='Unit price actually paid on the most recent confirmed purchase order '
             '(state Purchase Order or Done) for this product, converted to the '
             "product's unit of measure and company currency. Blank/0 means there is "
             'no purchase history yet.'
    )
    last_purchase_date = fields.Date(
        string='Last Purchase Date', readonly=True,
        help='Order date of the purchase order that last_purchase_cost was taken from.'
    )
    last_purchase_vendor_id = fields.Many2one(
        'res.partner', string='Last Purchase Vendor', readonly=True,
        help='Vendor on the most recent confirmed purchase order for this product.'
    )
    effective_unit_cost = fields.Float(
        string='Effective Unit Cost', digits=(16, 3), readonly=True, groups=_COST_GROUPS,
        help='Best available cost for ordering decisions, in priority order: '
             'Last Purchase Cost (what was actually paid) if known, otherwise '
             'Vendor Price (pricelist), otherwise Standard Cost.'
    )

    # ── Price Discrepancy Flag (Task 2) ───────────────────────────────────────
    has_price_discrepancy = fields.Boolean(
        string='Price Discrepancy?', readonly=True, index=True, groups=_COST_GROUPS,
        help='True when the vendor pricelist price differs from the last actual '
             'purchase cost by more than 15%. Both a Vendor Price and a Last '
             'Purchase Cost must be on record for this comparison to run — '
             'never flagged when either is missing.'
    )
    price_discrepancy_pct = fields.Float(
        string='Price Discrepancy (%)', digits=(16, 1), readonly=True, groups=_COST_GROUPS,
        help='Vendor Price vs. Last Purchase Cost, as a percentage change from '
             'Last Purchase Cost. Positive means the pricelist price is higher '
             'than what was actually paid.'
    )

    # ── Data Cleanup Triage (Recommendation) ─────────────────────────────────
    needs_vendor_assignment = fields.Boolean(
        string='Needs Vendor Assignment?', readonly=True, index=True,
        help='True when this suggestion needs reordering but has no vendor, or '
             'only a Temporary/Placeholder Vendor (Configuration → Company '
             'Settings). Feeds the "Needs Vendor Assignment" triage list and the '
             "boss report's Needs Attention section."
    )
    dead_stock_critical_review = fields.Boolean(
        string='Dead Stock + Critical — Needs Review?', readonly=True, index=True,
        help='True when this item is both flagged Dead Stock and shows Critical '
             'urgency — a real but misleading combination (negative/promised '
             'stock forces a positive reorder qty even though nothing has sold '
             'in months). Needs a human judgment call, not an automatic urgent '
             'flag. Feeds the "Dead Stock — Needs Review" triage list.'
    )

    # ── Stock Correction Candidate (Recommendation) ──────────────────────────
    # Purely computed and displayed — this module never writes to stock.quant
    # or any inventory record. Closes a gap in is_dead: a product that has
    # NEVER sold (no last_sale at all) and is negative was never flagged dead
    # before, even though that's the pattern most likely to be a data/timing
    # error (e.g. a delayed purchase receipt) rather than genuine demand.
    stock_correction_candidate = fields.Boolean(
        string='Stock Correction Candidate?', readonly=True, index=True,
        help='True when stock is negative, there is no real forecasted demand '
             'behind it, and the product has never sold (or not sold in longer '
             'than the Dead Stock Threshold). Likely a data/timing error — e.g. '
             'a delayed purchase receipt — rather than genuine demand worth '
             'reordering for. A judgment call for a human, not an automatic action.'
    )
    suggested_resolution = fields.Selection([
        ('reorder', 'Genuine Shortfall — Reorder'),
        ('stock_correction', 'Likely Data Error — Consider Adjustment'),
    ], string='Suggested Resolution', readonly=True,
       help='Only set when on-hand is negative. Plain-language read of '
            'stock_correction_candidate for the triage list.')
    monthly_sales_history = fields.Char(
        string='Monthly Sales History', readonly=True,
        help='Comma-separated units sold per month, oldest to newest (e.g. '
             '"0,0,3,0,7,6"), over the current Sales Analysis Period. Same '
             'underlying data as the Calculation Breakdown\'s per-month '
             'listing, in a compact form for scanning across many rows.'
    )

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
        'sra_suggestion_purchase_order_rel',
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

    # ── Stale Draft PO Alert (Task 8) ────────────────────────────────────────
    # Refreshed during the weekly generate_suggestions() run (like is_stale /
    # is_overstocked), not a live compute — a "days since creation" flag needs
    # a periodic re-check, not a reactive one, since nothing about the PO
    # itself changes as time passes.
    draft_po_stale_days = fields.Integer(
        string='Draft PO Age (Days)', readonly=True,
        help='Days since the oldest linked Draft PO still in draft state was created.'
    )
    is_draft_po_stale = fields.Boolean(
        string='Draft PO Stale?', readonly=True, index=True,
        help='True when a linked Draft PO has sat unconfirmed for longer than '
             "the configured Stale Draft PO Alert Threshold (Configuration → "
             'Company Settings). Refreshed on each analysis run.'
    )

    # ── Action Center (Recommendation) ───────────────────────────────────────
    # Purely derived from the six flags above — a standard reactive compute
    # (not engine-computed) is the right tool here, since nothing outside this
    # one record is needed to work it out, and it needs to stay correct the
    # instant any of those six flags changes for any reason (a wizard clearing
    # needs_vendor_assignment, a snooze, etc.), not just after the next
    # generate_suggestions() run.
    attention_flag_count = fields.Integer(
        string='Attention Flags', compute='_compute_attention_summary', store=True, index=True,
        help='How many of the six triage flags (Needs Review, Stale Data, Stale '
             'Draft PO, Needs Vendor Assignment, Dead Stock + Critical, Price '
             'Discrepancy) are currently set on this suggestion.'
    )
    attention_reasons = fields.Char(
        string='Why Flagged', compute='_compute_attention_summary', store=True,
        help='Plain-language list of which triage flag(s) are currently set.'
    )

    notes = fields.Text(string='Calculation Breakdown', readonly=True)

    @api.depends('po_ids', 'po_ids.state', 'po_ids.name')
    def _compute_draft_po_ref(self):
        for rec in self:
            drafts = rec.po_ids.filtered(lambda po: po.state == 'draft')
            rec.draft_po_ref = ', '.join(drafts.mapped('name')) if drafts else False

    @api.depends('needs_review', 'is_stale', 'is_draft_po_stale', 'needs_vendor_assignment',
                'dead_stock_critical_review', 'has_price_discrepancy')
    def _compute_attention_summary(self):
        for rec in self:
            reasons = []
            if rec.needs_review:
                reasons.append(_('Needs Review'))
            if rec.is_stale:
                reasons.append(_('Stale Data'))
            if rec.is_draft_po_stale:
                reasons.append(_('Stale Draft PO'))
            if rec.needs_vendor_assignment:
                reasons.append(_('Needs Vendor Assignment'))
            if rec.dead_stock_critical_review:
                reasons.append(_('Dead Stock + Critical'))
            if rec.has_price_discrepancy:
                reasons.append(_('Price Discrepancy'))
            rec.attention_flag_count = len(reasons)
            rec.attention_reasons = ', '.join(reasons) if reasons else False

    # ── Snooze ────────────────────────────────────────────────────────────────
    snoozed_until = fields.Date(
        string='Snoozed Until', readonly=False, tracking=True,
        help='Suppress reorder alerts for this product until this date.'
    )
    snoozed_note = fields.Char(string='Snooze Reason', readonly=False, tracking=True)
    is_snoozed = fields.Boolean(
        string='Snoozed?', compute='_compute_is_snoozed', search='_search_is_snoozed', store=False
    )

    # ── Mark as Ordered (Task 5) ─────────────────────────────────────────────
    is_marked_ordered = fields.Boolean(
        string='Marked as Ordered?', default=False, readonly=True, index=True, tracking=True,
        help='The boss has already placed this order directly (email/vendor site) '
             'and someone has confirmed it in Odoo before the purchase is logged. '
             'Suppresses this suggestion for exactly the next analysis run — after '
             'that, the flag clears automatically so a genuinely delayed order '
             'still resurfaces instead of being silently forgotten.'
    )
    marked_ordered_at = fields.Datetime(string='Marked Ordered At', readonly=True)
    marked_ordered_by_id = fields.Many2one(
        'res.users', string='Marked Ordered By', readonly=True,
        help='The Odoo user who clicked Mark as Ordered.'
    )
    marked_ordered_confirmer_name = fields.Char(
        string='Confirmed By (Name)', readonly=False,
        help='Optional free-text name of whoever actually confirmed the order was '
             'placed, if different from the Odoo user who clicked the button '
             '(e.g. relaying what the boss said over the phone).'
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

    def _search_is_snoozed(self, operator, value):
        today = date.today()
        if operator not in ('=', '!='):
            raise NotImplementedError(_('Unsupported search operator on snooze: %s') % operator)
        if (operator == '=' and value) or (operator == '!=' and not value):
            return [('snoozed_until', '>=', today)]
        else:
            return ['|', ('snoozed_until', '=', False), ('snoozed_until', '<', today)]

    @api.depends('urgency')
    def _compute_urgency_rank(self):
        rank_map = {'critical': 1, 'urgent': 2, 'normal': 3, 'dead': 4, 'ok': 5}
        for rec in self:
            rec.urgency_rank = rank_map.get(rec.urgency, 5)

    @api.depends('qty_available', 'suggested_reorder_qty', 'avg_monthly_demand', 'company_id')
    def _compute_months_of_stock_after_order(self):
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
        return calc_months_of_stock(qty_available, avg_monthly_demand)

    @staticmethod
    def _round_to_moq(qty, moq):
        return round_to_moq(qty, moq)

    @staticmethod
    def _classify_abc(avg_monthly_demand, config):
        return classify_abc(avg_monthly_demand, config)

    @staticmethod
    def _determine_urgency(qty_on_hand, months_of_stock, avg_monthly,
                           lead_months, is_dead_stock, suggested_qty):
        return determine_urgency(qty_on_hand, months_of_stock, avg_monthly,
                                 lead_months, is_dead_stock, suggested_qty)

    @staticmethod
    def _calc_trend(current_qty, prev_qty, analysis_months, comparison_months):
        return calc_trend(current_qty, prev_qty, analysis_months, comparison_months)

    @staticmethod
    def _calc_seasonal_note(current_qty, ly_qty):
        return calc_seasonal_note(current_qty, ly_qty)

    @staticmethod
    def _compute_robust_monthly_demand(monthly_series, min_spike_size=10.0,
                                       spike_dominance_pct=50.0, spike_multiplier=4.0):
        return compute_robust_monthly_demand(
            monthly_series, min_spike_size=min_spike_size,
            spike_dominance_pct=spike_dominance_pct, spike_multiplier=spike_multiplier,
        )

    @staticmethod
    def _calc_delta_pct(old_qty, new_qty):
        return calc_delta_pct(old_qty, new_qty)

    @staticmethod
    def _compute_needs_review(avg_monthly, suggested_qty, is_dead_stock, delta_pct, delta_threshold,
                              mos_after_order=0.0, overstock_ceiling_months=0.0,
                              sales_pattern=False, has_bulk_concentration=False):
        return compute_needs_review(avg_monthly, suggested_qty, is_dead_stock, delta_pct, delta_threshold,
                                    mos_after_order, overstock_ceiling_months,
                                    sales_pattern, has_bulk_concentration)

    @staticmethod
    def _compute_confidence_score(monthly_series, excluded_months, reorder_behavior='system'):
        return compute_confidence_score(monthly_series, excluded_months, reorder_behavior)

    @staticmethod
    def _build_vendor_perf_note(actual_avg_days, stated_lead_days, with_emoji=False):
        return build_vendor_perf_note(actual_avg_days, stated_lead_days, with_emoji)

    _SALE_QTY_EXPR = "SUM(sol.qty_delivered / lu.factor * pu.factor)"
    _SALE_QTY_JOINS = """
          JOIN sale_order so ON so.id = sol.order_id
          JOIN product_product pp ON pp.id = sol.product_id
          JOIN product_template pt ON pt.id = pp.product_tmpl_id
          JOIN uom_uom lu ON lu.id = sol.product_uom
          JOIN uom_uom pu ON pu.id = pt.uom_id
    """
