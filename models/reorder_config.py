from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare
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
    order_cycle_is_manual = fields.Boolean(
        string='Order Cycle Manually Set',
        default=False,
        help='Set automatically when a manager types a custom Order Cycle value. '
             'While checked, changing the Analysis Frequency no longer overwrites '
             'the order cycle — even if the manual value happens to match one of '
             'the auto-derived defaults (0.25 / 0.5 / 1.0). Untick to return to '
             'automatic derivation.'
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
        default=True,
        help='After each cron run, automatically email the Weekly Order List '
             "PDF to the notify users (subject to 'Notify Only for Critical / "
             "Urgent Items' below — Task 4)."
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
    stale_draft_po_days = fields.Integer(
        string='Stale Draft PO Alert Threshold (Days)',
        default=7,
        help='Flag a suggestion when a Draft PO it created has sat unconfirmed '
             'for longer than this many days. Set to 0 to disable.'
    )

    # ── Boss's Weekly Order Report (Task 3) ────────────────────────────────────
    temp_vendor_ids = fields.Many2many(
        'res.partner', 'smart_reorder_config_temp_vendor_rel', 'config_id', 'partner_id',
        string='Temporary/Placeholder Vendors',
        help='Vendors used as a formal placeholder meaning "no real vendor assigned '
             'yet" (e.g. a "Temporary Supplier" record) — not real suppliers. '
             "Suggestions still tagged with one of these are excluded from the boss's "
             'weekly order report and routed to its "Needs Attention" section instead, '
             'the same as a suggestion with no vendor at all.'
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
        help='Products selling >= this qty/month are B (Medium). Below = C (Slow Mover).'
    )

    # ── One-Time Spike Detection ──────────────────────────────────────────────
    min_spike_size = fields.Float(
        string='Minimum One-Time Spike Size',
        default=10.0,
        help='A single month of sales can only be excluded as a one-time bulk-order '
             'spike if its quantity is at least this many units. Prevents a lone small '
             'sale (e.g. a part that sold once in the window) from being zeroed out and '
             'flagged as "One-Time Big Order Only" just because every other month is zero. '
             'Raise this for catalogs with large one-off orders; lower it if genuine small '
             'spikes are being averaged in and inflating the forecast.'
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

    snapshot_scope = fields.Selection([
        ('all', 'All Products'),
        ('ab_only', 'A+B Class Only'),
        ('reorder_only', 'Reorder-Needed Only'),
    ], string='Back-testing Snapshot Scope', default='ab_only', required=True,
       help='Which products to create back-testing snapshots for. '
            'A+B Class Only is recommended to reduce database volume.')

    snapshot_retention_months = fields.Integer(
        string='Snapshot Retention (Months)',
        default=6,
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


    cron_include_zero_demand = fields.Boolean(
        string='Include Zero-Sales in Scheduled Runs',
        default=True,
        help='If checked, weekly scheduled runs will also analyse products with NO sales (needed to detect dead stock automatically).'
    )

    # ── Scheduling (T-32) ──────────────────────────────────────────────────────
    cron_frequency = fields.Selection([
        ('weekly',   'Weekly'),
        ('biweekly', 'Bi-Weekly (Every 2 Weeks)'),
        ('monthly',  'Monthly'),
    ], string='Analysis Frequency', default='weekly', required=True,
       help='System-wide setting, shared by all companies. '
            'How often the scheduled cron job re-runs the full reorder analysis. '
            'Saving this updates the frequency for all company configurations '
            'and synchronizes the "Smart Reorder: Weekly Analysis" scheduled action.')

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
        # One batched search for all configs instead of one query per record.
        CronLog = self.env['smart.reorder.cron.log'].sudo()
        running_by_company = {}
        for log in CronLog.search([
            ('company_id', 'in', self.mapped('company_id').ids),
            ('status', '=', 'running'),
        ], order='started_at desc'):
            running_by_company.setdefault(log.company_id.id, log)
        for rec in self:
            running = running_by_company.get(rec.company_id.id)
            rec.is_running = bool(running)
            rec.run_started_at = running.started_at if running else False

    _CYCLE_DEFAULTS = {'weekly': 0.25, 'biweekly': 0.5, 'monthly': 1.0}

    @api.depends('cron_frequency')
    def _compute_order_cycle_months(self):
        for rec in self:
            # A manual value is authoritative — tracked via an explicit flag,
            # not by sniffing whether the value "looks like" a default (which
            # silently destroyed a deliberate manual 0.25/0.5/1.0).
            if rec.order_cycle_is_manual and rec.order_cycle_months:
                rec.order_cycle_months = rec.order_cycle_months
            else:
                rec.order_cycle_months = self._CYCLE_DEFAULTS.get(rec.cron_frequency, 1.0)

    @api.onchange('cron_frequency')
    def _onchange_cron_frequency_warn_shared(self):
        """Task 8 / Finding 8: cron_frequency LOOKS per-company (it lives on
        this per-company config record) but is actually system-wide — saving
        it here overwrites every other company's config and the one shared
        scheduled action. The static help text already explained this but was
        easy to skim past; a real onchange warning forces an acknowledgment
        at the moment the value is actually changed, without hard-blocking
        the (legitimate) global change."""
        if not self.cron_frequency:
            return
        origin_id = self._origin.id if self._origin else False
        other_configs = self.env['smart.reorder.config'].search([
            ('id', '!=', origin_id),
            ('cron_frequency', '!=', self.cron_frequency),
        ])
        if other_configs:
            company_names = ', '.join(other_configs.mapped('company_id.name'))
            return {
                'warning': {
                    'title': _('Analysis Frequency is shared across ALL companies'),
                    'message': _(
                        'This is not a per-company setting even though it lives on '
                        'this company\'s configuration. Saving it will also change it '
                        'for: %s — and the single shared "Smart Reorder: Weekly '
                        'Analysis" scheduled action, since it only supports one '
                        'interval for every company.'
                    ) % company_names,
                }
            }

    _sql_constraints = [
        ('company_unique', 'UNIQUE(company_id)', 'Only one configuration allowed per company.'),
    ]

    @api.constrains('safety_buffer_months', 'default_lead_time_months', 'order_cycle_months', 'overstock_ceiling_months', 'alt_vendor_lead_margin_days', 'snapshot_retention_months', 'transfer_surplus_threshold', 'default_internal_transfer_days', 'stale_draft_po_days')
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
            if rec.stale_draft_po_days < 0:
                raise ValidationError(_('Stale Draft PO Alert Threshold cannot be negative.'))

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

    @api.constrains('min_spike_size')
    def _check_min_spike_size(self):
        for rec in self:
            if rec.min_spike_size < 0:
                raise ValidationError(_('Minimum One-Time Spike Size cannot be negative.'))

    _CRON_FREQUENCY_INTERVALS = {
        'weekly':   (1, 'weeks'),
        'biweekly': (2, 'weeks'),
        'monthly':  (1, 'months'),
    }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # An explicit order-cycle value at creation is a manual choice —
            # unless it simply equals the derived default (the web client can
            # echo the computed value back on save).
            if 'order_cycle_months' in vals and 'order_cycle_is_manual' not in vals:
                cycle = vals['order_cycle_months']
                freq = vals.get('cron_frequency', 'weekly')
                default_cycle = self._CYCLE_DEFAULTS.get(freq, 0.25)
                if float_compare(cycle, default_cycle, precision_digits=4) != 0:
                    vals['order_cycle_is_manual'] = True
        records = super().create(vals_list)

        freqs = [v.get('cron_frequency') for v in vals_list if v.get('cron_frequency')]
        if freqs:
            new_freq = freqs[0]
            other_configs = self.sudo().search([
                ('id', 'not in', records.ids),
                ('cron_frequency', '!=', new_freq)
            ])
            if other_configs:
                other_configs.write({'cron_frequency': new_freq})
            if len(set(freqs)) > 1 or records.filtered(lambda r: r.cron_frequency != new_freq):
                records.write({'cron_frequency': new_freq})

        records._sync_cron_frequency()
        return records

    def write(self, vals):
        # A typed custom order cycle becomes authoritative (manual); writing a
        # value that equals the derived default returns the field to automatic
        # derivation. The flag comparison must happen per record because the
        # derived default depends on each record's (possibly changing) frequency.
        if 'order_cycle_months' in vals and 'order_cycle_is_manual' not in vals:
            cycle = vals['order_cycle_months']
            res = True
            for rec in self:
                freq = vals.get('cron_frequency', rec.cron_frequency)
                default_cycle = self._CYCLE_DEFAULTS.get(freq, 0.25)
                rec_vals = dict(vals, order_cycle_is_manual=(
                    float_compare(cycle, default_cycle, precision_digits=4) != 0
                ))
                res = super(SmartReorderConfig, rec).write(rec_vals) and res
        else:
            res = super().write(vals)

        if 'cron_frequency' in vals:
            new_freq = vals['cron_frequency']
            other_configs = self.sudo().search([
                ('id', 'not in', self.ids),
                ('cron_frequency', '!=', new_freq)
            ])
            if other_configs:
                other_configs.write({'cron_frequency': new_freq})
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
