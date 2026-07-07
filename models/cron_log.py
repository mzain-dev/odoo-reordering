from datetime import datetime, timedelta
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


class SmartReorderObservabilityDashboard(models.TransientModel):
    _name = 'smart.reorder.observability.dashboard'
    _description = 'Reorder Observability Dashboard'

    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)
    failure_rate_30_days = fields.Float(string='Failure Rate (Last 30 Days)', compute='_compute_stats')
    avg_duration = fields.Float(string='Average Run Duration (s)', compute='_compute_stats')
    total_runs = fields.Integer(string='Total Runs (Last 30 Days)', compute='_compute_stats')
    failed_runs = fields.Integer(string='Failed Runs (Last 30 Days)', compute='_compute_stats')
    picking_hook_errors = fields.Integer(string='Total Picking Hook Errors', compute='_compute_stats')

    def _compute_stats(self):
        for rec in self:
            limit_date = datetime.now() - timedelta(days=30)
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

