from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from odoo import models, fields, api


class SmartReorderCronLog(models.Model):
    """
    One record per generate_suggestions() run (cron or manual), per company.

    This is the auditable source of truth for the run-lock (T-21): instead of
    a bare Boolean flag that can drift out of sync with reality, the lock check
    searches for any record still in 'running' status. smart.reorder.config's
    is_running/run_started_at are derived from this table for display.
    """
    _name = 'smart.reorder.cron.log'
    _description = 'Smart Reorder Analysis Run Log'
    _order = 'started_at desc'
    _rec_name = 'started_at'

    company_id = fields.Many2one(
        'res.company', string='Company / Branch',
        required=True, index=True, default=lambda self: self.env.company,
    )
    started_at = fields.Datetime(string='Started At', required=True, default=fields.Datetime.now, index=True)
    finished_at = fields.Datetime(string='Finished At')

    trigger_type = fields.Selection([
        ('cron',   'Scheduled Cron'),
        ('manual', 'Manual'),
    ], string='Triggered By', required=True, default='cron')

    status = fields.Selection([
        ('running',               'Running'),
        ('completed',             'Completed'),
        ('completed_with_errors', 'Completed with Errors'),
        ('aborted',               'Aborted'),
    ], string='Status', required=True, default='running', index=True)

    products_processed = fields.Integer(string='Products Processed')
    critical_count = fields.Integer(string='Critical Items Found')
    error_notes = fields.Text(string='Error Notes')
    error_count = fields.Integer(
        string='Warehouse Batch Errors', default=0,
        help='Incremented each time a warehouse batch failed during this run and fell '
             'back to leaving its existing suggestions in place instead of refreshing '
             'or removing them (see is_stale on smart.reorder.suggestion).'
    )

    duration_seconds = fields.Float(
        string='Duration (s)', compute='_compute_duration', store=True,
        help='Wall-clock time from started_at to finished_at. Blank while still running.'
    )

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                rec.duration_seconds = (rec.finished_at - rec.started_at).total_seconds()
            else:
                rec.duration_seconds = 0.0

    def init(self):
        super().init()
        # The run-lock in generate_suggestions() is check-then-create; two
        # workers can race past the check together. This partial unique index
        # guarantees at most ONE 'running' log per company at the DB level —
        # the loser's INSERT fails and that company's run is skipped.
        # Dedupe first so index creation can't fail on a DB that already has
        # several stuck 'running' rows for one company.
        self.env.cr.execute("""
            UPDATE smart_reorder_cron_log
               SET status = 'aborted',
                   error_notes = COALESCE(error_notes, '') ||
                       ' Auto-aborted: duplicate running row cleaned up on module update.'
             WHERE status = 'running'
               AND id NOT IN (
                    SELECT MAX(id)
                      FROM smart_reorder_cron_log
                     WHERE status = 'running'
                     GROUP BY company_id
               )
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS smart_reorder_cron_log_running_uniq
                ON smart_reorder_cron_log (company_id)
             WHERE status = 'running'
        """)

    @api.model
    def _purge_old_logs(self):
        """Recommendation: Run History has no retention today, unlike forecast
        snapshots — it grows one row per company per run forever. Mirrors
        ForecastSnapshot._score_snapshots()'s retention purge, called from
        action_run_weekly_cron() alongside it. Never purges a 'running' row,
        even if it looks old (a stuck lock is a separate problem — see
        action_clear_lock — not something a retention purge should paper over)."""
        configs = self.env['smart.reorder.config'].sudo().search(
            [('cron_log_retention_months', '>', 0)]
        )
        today = date.today()
        for config in configs:
            limit_date = today - relativedelta(months=config.cron_log_retention_months)
            expired = self.sudo().search([
                ('company_id', '=', config.company_id.id),
                ('started_at', '<', limit_date),
                ('status', '!=', 'running'),
            ])
            if expired:
                expired.unlink()


# Recommendation: cron missed-run detection. Expected gap between successful
# runs per configured frequency, used both for the dashboard's passive
# "is this overdue" display and the active heartbeat check below.
CRON_FREQUENCY_DAYS = {'weekly': 7, 'biweekly': 14, 'monthly': 30}
# A run finishing a bit late (server load, long product catalog) shouldn't
# immediately read as "broken" — only flag once it's meaningfully overdue.
CRON_OVERDUE_GRACE_MULTIPLIER = 1.5


class SmartReorderObservabilityDashboard(models.TransientModel):
    _name = 'smart.reorder.observability.dashboard'
    _description = 'Reorder Observability Dashboard'

    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)
    failure_rate_30_days = fields.Float(string='Failure Rate (Last 30 Days)', compute='_compute_stats')
    avg_duration = fields.Float(string='Average Run Duration (s)', compute='_compute_stats')
    total_runs = fields.Integer(string='Total Runs (Last 30 Days)', compute='_compute_stats')
    failed_runs = fields.Integer(string='Failed Runs (Last 30 Days)', compute='_compute_stats')
    picking_hook_errors = fields.Integer(string='Total Picking Hook Errors', compute='_compute_stats')

    cron_active = fields.Boolean(string='Weekly Cron Enabled?', compute='_compute_stats')
    last_successful_run_at = fields.Datetime(string='Last Successful Run', compute='_compute_stats')
    days_since_last_run = fields.Integer(string='Days Since Last Successful Run', compute='_compute_stats')
    expected_interval_days = fields.Integer(string='Expected Interval (Days)', compute='_compute_stats')
    is_run_overdue = fields.Boolean(string='Run Overdue?', compute='_compute_stats')

    def _compute_stats(self):
        cron = self.env.ref('smart_reorder_advisor.cron_smart_reorder_weekly', raise_if_not_found=False)
        for rec in self:
            limit_date = fields.Datetime.now() - timedelta(days=30)
            logs = self.env['smart.reorder.cron.log'].search([
                ('company_id', '=', rec.company_id.id),
                ('started_at', '>=', limit_date)
            ])
            total = len(logs)
            failed = len(logs.filtered(lambda l: l.status in ('completed_with_errors', 'aborted')))
            rate = (failed / total) if total > 0 else 0.0

            completed_logs = logs.filtered(lambda l: l.status in ('completed', 'completed_with_errors') and l.duration_seconds)
            avg_dur = sum(completed_logs.mapped('duration_seconds')) / len(completed_logs) if completed_logs else 0.0

            config = self.env['smart.reorder.config'].search([('company_id', '=', rec.company_id.id)], limit=1)
            hook_errors = config.picking_hook_error_count if config else 0

            rec.total_runs = total
            rec.failed_runs = failed
            rec.failure_rate_30_days = rate
            rec.avg_duration = avg_dur
            rec.picking_hook_errors = hook_errors

            rec.cron_active = bool(cron and cron.active)
            expected_days = CRON_FREQUENCY_DAYS.get(config.cron_frequency if config else 'weekly', 7)
            rec.expected_interval_days = expected_days
            last_log = self.env['smart.reorder.cron.log'].search([
                ('company_id', '=', rec.company_id.id),
                ('status', 'in', ('completed', 'completed_with_errors')),
            ], order='finished_at desc', limit=1)
            rec.last_successful_run_at = last_log.finished_at if last_log else False
            if last_log and last_log.finished_at:
                rec.days_since_last_run = (fields.Datetime.now() - last_log.finished_at).days
                rec.is_run_overdue = bool(
                    rec.cron_active
                    and rec.days_since_last_run > expected_days * CRON_OVERDUE_GRACE_MULTIPLIER
                )
            else:
                rec.days_since_last_run = 0
                # No successful run ever recorded — only "overdue" if the
                # cron already missed its own next scheduled fire time, not
                # merely because a fresh install hasn't had its first chance
                # to run yet (nextcall still in the future).
                rec.is_run_overdue = bool(
                    rec.cron_active and cron and cron.nextcall
                    and cron.nextcall < fields.Datetime.now()
                )

    @api.model
    def action_open_dashboard(self):
        dashboard = self.create({'company_id': self.env.company.id})
        return {
            'name': 'Observability Dashboard',
            'type': 'ir.actions.act_window',
            'res_model': 'smart.reorder.observability.dashboard',
            'res_id': dashboard.id,
            'view_mode': 'form',
            'target': 'current',
        }

