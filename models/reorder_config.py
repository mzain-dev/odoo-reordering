from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from ..utils.access import require_group

_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'


class SmartReorderConfig(models.Model):
    """
    Per-company configuration for the Smart Reorder Advisor.
    ─────────────────────────────────────────────────────────
    PHASE 1: Monthly demand, MOQ, reorder value, dead stock
    PHASE 2: Demand trend, email delivery, budget cap, branch coverage
    PHASE 3: Draft PO, auto-flag on delivery, seasonal, vendor tracker
    """
    _name = 'smart.reorder.config'
    _description = 'Smart Reorder Advisor Configuration'
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', string='Company / Branch',
        required=True, default=lambda self: self.env.company, ondelete='cascade',
    )

    # ── PHASE 1: Analysis Window (now in MONTHS) ──────────────────────────────
    analysis_period = fields.Selection([
        ('3',  'Last 3 Months'),
        ('6',  'Last 6 Months'),
        ('9',  'Last 9 Months'),
        ('12', 'Last 12 Months'),
    ], string='Sales Analysis Period', default='6', required=True,
       help='How many months of sales history to use for demand calculation. '
            '6 months is recommended for spare parts with irregular demand.')

    # ── PHASE 1: Safety Buffer (now in MONTHS) ────────────────────────────────
    safety_buffer_months = fields.Float(
        string='Safety Buffer (Months)',
        default=1.0,
        help='Extra months of stock to hold as safety buffer. '
             'Example: 1.0 = keep one extra month of stock beyond lead time demand.'
    )
    order_cycle_months = fields.Float(
        string='Order Cycle (Months)',
        compute='_compute_order_cycle_months',
        store=True,
        readonly=False,
        help='Typical frequency of ordering from vendors in months. '
             'Auto-derived from Analysis Frequency when not manually overridden (Weekly -> 0.25, Bi-Weekly -> 0.5, Monthly -> 1.0). '
             'Used to calculate the maximum stock level.'
    )
    default_lead_time_months = fields.Float(
        string='Default Lead Time (Months)',
        default=1.5,
        help='Used when a product has no vendor lead time set. '
             'For spare parts imported from abroad, 1.5–2 months is typical.'
    )

    # ── PHASE 1: Dead Stock Detection ─────────────────────────────────────────
    dead_stock_months = fields.Integer(
        string='Dead Stock Threshold (Months)',
        default=6,
        help='Products with ZERO sales for this many months are flagged as Dead Stock.'
    )
    flag_dead_stock = fields.Boolean(
        string='Flag Dead Stock',
        default=True,
        help='When enabled, products with no sales movement are flagged separately.'
    )

    # ── PHASE 1: Budget Cap ───────────────────────────────────────────────────
    budget_cap = fields.Monetary(
        string='Weekly Reorder Budget Cap',
        currency_field='currency_id',
        default=0.0,
        help='Maximum total reorder value per analysis run. '
             'Set to 0 to disable. When set, suggestions are ranked by priority '
             'until the budget is exhausted.'
    )
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id,
    )

    # ── PHASE 2: Email Delivery ────────────────────────────────────────────────
    send_email_report = fields.Boolean(
        string='Email PDF Report After Analysis',
        default=False,
        help='After each cron run, automatically email the summary PDF report '
             'to the notify users.'
    )
    email_subject_prefix = fields.Char(
        string='Email Subject Prefix',
        default='[Reorder Advisor]',
    )

    # ── PHASE 2: Demand Trend ─────────────────────────────────────────────────
    trend_comparison_months = fields.Integer(
        string='Trend Comparison Period (Months)',
        default=3,
        help='Compare latest N months vs previous N months to detect trend direction.'
    )

    # ── PHASE 3: Draft PO Creation ────────────────────────────────────────────
    allow_draft_po = fields.Boolean(
        string='Allow One-Click Draft PO Creation',
        default=False,
        help='Enables a button on suggestions to push items to a Draft Purchase Order. '
             'Requires manager approval before confirming.'
    )
    default_vendor_id = fields.Many2one(
        'res.partner',
        string='Default Vendor (for Draft POs)',
        help='Used when no vendor is set on the product.'
    )

    # ── PHASE 3: Auto-Flag on Delivery ────────────────────────────────────────
    auto_flag_on_negative = fields.Boolean(
        string='Auto-Flag When Stock Goes Negative',
        default=True,
        help='When a delivery validation causes stock to go negative, '
             'immediately create/update a Critical suggestion without waiting for the cron.'
    )

    # ── Notifications ─────────────────────────────────────────────────────────
    notify_user_ids = fields.Many2many(
        'res.users', 'smart_reorder_config_user_rel', 'config_id', 'user_id',
        string='Notify Users',
        help='Receive Odoo inbox alerts + email reports after each analysis.'
    )
    critical_notify_only = fields.Boolean(
        string='Notify Only for Critical / Urgent Items',
        default=True,
    )

    # ── ABC Thresholds (now monthly) ──────────────────────────────────────────
    abc_a_threshold = fields.Float(
        string='A-Class: Min Monthly Demand',
        default=5.0,
        help='Products selling >= this qty/month are A (Fast Movers).'
    )
    abc_b_threshold = fields.Float(
        string='B-Class: Min Monthly Demand',
        default=1.0,
        help='Products selling >= this qty/month are B (Medium). Below = C (Slow/Dead).'
    )

    # ── Vendor Performance (Phase 3) ──────────────────────────────────────────
    track_vendor_performance = fields.Boolean(
        string='Track Vendor Delivery Performance',
        default=False,
        help='Automatically add observed late-delivery days to lead time calculations.'
    )

    # ── Needs-Review Auto-Triage (T-26) ───────────────────────────────────────
    needs_review_delta_threshold = fields.Float(
        string='Needs-Review Swing Threshold (%)',
        default=50.0,
        help='A suggestion whose Suggested Reorder Qty changed by more than this '
             'percentage vs the prior run is auto-flagged "Needs Review". '
             'Set to 0 to disable this particular trigger (the demand/dead-stock '
             'contradiction checks always remain active).'
    )

    overstock_ceiling_months = fields.Float(
        string='Overstock Ceiling (Months)',
        default=12.0,
        help='Ceiling in months for post-order stock coverage. '
             'If the months of stock after ordering exceeds this ceiling (e.g. due to vendor MOQ), '
             'the suggestion is flagged for review. Set to 0 to disable.'
    )

    alt_vendor_lead_margin_days = fields.Integer(
        string='Alt. Vendor Lead Margin (Days)',
        default=5,
        help='Minimum lead time difference in days to suggest an alternative vendor. Set to 0 to disable.'
    )

    snapshot_retention_months = fields.Integer(
        string='Snapshot Retention (Months)',
        default=12,
        help='Purge forecast snapshots older than this many months. Set to 0 to keep forever.'
    )

    transfer_surplus_threshold = fields.Float(
        string='Transfer Surplus Threshold (Months)',
        default=6.0,
        help='A branch must have more than this many months of stock left to be eligible as a transfer donor.'
    )

    default_internal_transfer_days = fields.Integer(
        string='Default Internal Transfer Lead Time (Days)',
        default=3,
        help='Used when recommending an internal transfer and no specific transfer lane is configured between the warehouses.'
    )


    # ── Scheduling (T-32) ──────────────────────────────────────────────────────
    cron_frequency = fields.Selection([
        ('weekly',   'Weekly'),
        ('biweekly', 'Bi-Weekly (Every 2 Weeks)'),
        ('monthly',  'Monthly'),
    ], string='Analysis Frequency', default='weekly', required=True,
       help='How often the scheduled cron job re-runs the full reorder analysis. '
            'Saving this writes directly to the "Smart Reorder: Weekly Analysis" '
            'scheduled action — no need to open Settings → Technical → Scheduled '
            'Actions. That cron processes every company in a single run, so this '
            'setting is effectively system-wide: whichever company\'s config was '
            'saved most recently wins.')

    active = fields.Boolean(default=True)

    picking_hook_error_count = fields.Integer(
        string='Picking Hook Error Count',
        default=0,
        readonly=True,
        help='Total number of errors encountered during real-time picking hooks (auto-flag / receipt-refresh).'
    )

    # ── Run Lock (T-21 / T-22) ─────────────────────────────────────────────────
    # Derived from smart.reorder.cron.log — that table is the source of truth
    # for "is a run in progress", not a bare Boolean that can drift out of sync.
    is_running = fields.Boolean(
        string='Analysis Running?', compute='_compute_run_status',
        help='True when a smart.reorder.cron.log record for this company is '
             'still in "Running" status. Prevents the cron and a manual wizard '
             'run from overlapping.'
    )
    run_started_at = fields.Datetime(
        string='Run Started At', compute='_compute_run_status',
        help='Start time of the currently-running log record for this company, if any. '
             'A lock older than 60 minutes is treated as crashed and auto-overridden.'
    )

    def _compute_run_status(self):
        CronLog = self.env['smart.reorder.cron.log'].sudo()
        for rec in self:
            running = CronLog.search([
                ('company_id', '=', rec.company_id.id),
                ('status', '=', 'running'),
            ], order='started_at desc', limit=1)
            rec.is_running = bool(running)
            rec.run_started_at = running.started_at if running else False

    @api.depends('cron_frequency')
    def _compute_order_cycle_months(self):
        defaults = {'weekly': 0.25, 'biweekly': 0.5, 'monthly': 1.0}
        for rec in self:
            # If the value is not set or matches one of the defaults, update it to the new default
            if not rec.order_cycle_months or rec.order_cycle_months in (0.0, 0.25, 0.5, 1.0):
                rec.order_cycle_months = defaults.get(rec.cron_frequency, 1.0)
            else:
                rec.order_cycle_months = rec.order_cycle_months

    _sql_constraints = [
        ('company_unique', 'UNIQUE(company_id)', 'Only one configuration allowed per company.'),
    ]

    @api.constrains('safety_buffer_months', 'default_lead_time_months', 'order_cycle_months', 'overstock_ceiling_months', 'alt_vendor_lead_margin_days', 'snapshot_retention_months', 'transfer_surplus_threshold', 'default_internal_transfer_days')
    def _check_positive_months(self):
        for rec in self:
            if rec.safety_buffer_months < 0:
                raise ValidationError(_('Safety buffer cannot be negative.'))
            if rec.default_lead_time_months <= 0:
                raise ValidationError(_('Default lead time must be at least 0.1 months.'))
            if rec.order_cycle_months < 0:
                raise ValidationError(_('Order cycle cannot be negative.'))
            if rec.overstock_ceiling_months < 0:
                raise ValidationError(_('Overstock ceiling cannot be negative.'))
            if rec.alt_vendor_lead_margin_days < 0:
                raise ValidationError(_('Alternative vendor lead margin cannot be negative.'))
            if rec.snapshot_retention_months < 0:
                raise ValidationError(_('Snapshot retention months cannot be negative.'))
            if rec.transfer_surplus_threshold < 0:
                raise ValidationError(_('Transfer surplus threshold cannot be negative.'))
            if rec.default_internal_transfer_days < 0:
                raise ValidationError(_('Default internal transfer lead time (Days) cannot be negative.'))

    @api.constrains('needs_review_delta_threshold')
    def _check_needs_review_delta_threshold(self):
        for rec in self:
            if rec.needs_review_delta_threshold < 0:
                raise ValidationError(_('Needs-Review Swing Threshold cannot be negative.'))

    @api.constrains('abc_a_threshold', 'abc_b_threshold')
    def _check_abc_thresholds(self):
        for rec in self:
            if rec.abc_a_threshold <= rec.abc_b_threshold:
                raise ValidationError(_('A-class threshold must be higher than B-class threshold.'))

    _CRON_FREQUENCY_INTERVALS = {
        'weekly':   (1, 'weeks'),
        'biweekly': (2, 'weeks'),
        'monthly':  (1, 'months'),
    }

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_cron_frequency()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'cron_frequency' in vals:
            self._sync_cron_frequency()
        return res

    def _sync_cron_frequency(self):
        """T-32: push cron_frequency onto the single shared 'Smart Reorder: Weekly
        Analysis' scheduled action, so a manager never has to open Settings →
        Technical → Scheduled Actions to change the run schedule."""
        cron = self.env.ref('smart_reorder_advisor.cron_smart_reorder_weekly', raise_if_not_found=False)
        if not cron:
            return
        for rec in self:
            interval_number, interval_type = self._CRON_FREQUENCY_INTERVALS.get(
                rec.cron_frequency, (1, 'weeks')
            )
            cron.sudo().write({
                'interval_number': interval_number,
                'interval_type': interval_type,
            })

    @require_group(_MANAGER_GROUP)
    def action_clear_lock(self):
        """Manually release a stuck run lock without waiting for the 60-minute
        auto-override. Manager-only — this is meant for diagnosing a crashed cron.
        Marks the open smart.reorder.cron.log record(s) as aborted; is_running
        is derived from that table, so it flips to False as soon as none remain."""
        self.ensure_one()
        running_logs = self.env['smart.reorder.cron.log'].sudo().search([
            ('company_id', '=', self.company_id.id),
            ('status', '=', 'running'),
        ])
        running_logs.write({
            'status': 'aborted',
            'finished_at': fields.Datetime.now(),
            'error_notes': _('Manually cleared by %s.') % self.env.user.name,
        })
