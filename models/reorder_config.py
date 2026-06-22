from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


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

    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('company_unique', 'UNIQUE(company_id)', 'Only one configuration allowed per company.'),
    ]

    @api.constrains('safety_buffer_months', 'default_lead_time_months')
    def _check_positive_months(self):
        for rec in self:
            if rec.safety_buffer_months < 0:
                raise ValidationError(_('Safety buffer cannot be negative.'))
            if rec.default_lead_time_months <= 0:
                raise ValidationError(_('Default lead time must be at least 0.1 months.'))

    @api.constrains('abc_a_threshold', 'abc_b_threshold')
    def _check_abc_thresholds(self):
        for rec in self:
            if rec.abc_a_threshold <= rec.abc_b_threshold:
                raise ValidationError(_('A-class threshold must be higher than B-class threshold.'))
