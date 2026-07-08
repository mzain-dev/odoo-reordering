# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_round
from odoo.tools.sql import create_index
from datetime import date, timedelta


class ForecastSnapshot(models.Model):
    _name = 'smart.reorder.forecast.snapshot'
    _description = 'Forecast Snapshot for Back-testing'
    _order = 'snapshot_date desc, id desc'

    company_id = fields.Many2one('res.company', string='Company', required=True, readonly=True, ondelete='cascade', index=True)
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', required=True, readonly=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Product', required=True, readonly=True, ondelete='cascade', index=True)
    snapshot_date = fields.Date(string='Snapshot Date', required=True, readonly=True, default=fields.Date.context_today, index=True)

    forecast_demand = fields.Float(string='Forecast Demand (Monthly)', readonly=True)
    confidence = fields.Float(string='Forecast Confidence', readonly=True)
    lead_time_days = fields.Integer(string='Lead Time (Days)', readonly=True)
    abc_class = fields.Selection([('A', 'A'), ('B', 'B'), ('C', 'C')], string='ABC Class', readonly=True)
    
    actual_sales = fields.Float(string='Actual Sales (since)', readonly=True)
    absolute_error_pct = fields.Float(string='MAPE (%)', readonly=True, digits=(16, 2))
    evaluated = fields.Boolean(string='Evaluated', default=False, readonly=True, index=True)

    evaluable_from = fields.Date(
        string='Evaluable From',
        compute='_compute_evaluable_from',
        store=True,
        index=True,
    )

    def init(self):
        # Composite index matching the actual access patterns: the scorer
        # filters/searches by evaluated status (_score_snapshots), and the
        # dashboard's back-test aggregation filters by evaluated + company
        # (and often warehouse/product) in the same query (get_dashboard_data).
        # The single-column indexes on each field individually don't give the
        # planner a combined index scan for that filter set — at the intended
        # volume (tens of thousands of snapshots) that's the difference
        # between an index scan and repeated table scans.
        super().init()
        create_index(
            self.env.cr,
            'smart_reorder_forecast_snapshot_eval_scope_idx',
            self._table,
            ['evaluated', 'company_id', 'warehouse_id', 'product_id'],
        )

    @api.depends('snapshot_date', 'lead_time_days')
    def _compute_evaluable_from(self):
        for snap in self:
            if snap.snapshot_date and snap.lead_time_days is not None:
                snap.evaluable_from = snap.snapshot_date + timedelta(days=snap.lead_time_days)
            else:
                snap.evaluable_from = False

    @api.model
    def _score_snapshots(self, snapshots=None):
        """
        Scheduled or on-demand scorer comparing snapshots older than one lead
        time against actual sales in that period.

        Performance: previously issued one SQL query per unevaluated snapshot
        to read actuals, then one write() per snapshot to store the result.
        Now issues a single aggregate query per batch for ALL snapshots'
        intervals, computes MAPE in Python, and writes the whole batch back
        with one bulk UPDATE — O(1) read + O(1) write per batch of up to
        `batch_size`, regardless of snapshot count.
        """
        today = date.today()
        if snapshots is None:
            snapshots = self.search([('evaluated', '=', False)])
        
        to_evaluate_all = snapshots.filtered(
            lambda s: not s.evaluated and s.snapshot_date + timedelta(days=s.lead_time_days) <= today
        )
        if not to_evaluate_all:
            return

        # Process in batches of 1000 to prevent large parameter queries
        batch_size = 1000
        for idx in range(0, len(to_evaluate_all), batch_size):
            to_evaluate = to_evaluate_all[idx:idx + batch_size]
            
            intervals = [
                (
                    snap.id,
                    snap.company_id.id,
                    snap.warehouse_id.id,
                    snap.product_id.id,
                    str(snap.snapshot_date),
                    str(snap.snapshot_date + timedelta(days=snap.lead_time_days)),
                )
                for snap in to_evaluate
            ]

            # Build a temporary unnested VALUES clause
            values_sql = ', '.join(
                self.env.cr.mogrify(
                    '(%s::int, %s::int, %s::int, %s::int, %s::date, %s::date)',
                    row
                ).decode()
                for row in intervals
            )

            self.env.cr.execute(f"""
                SELECT
                    iv.snap_id,
                    COALESCE(SUM(sol.qty_delivered), 0.0) AS actual_qty
                FROM (
                    VALUES {values_sql}
                ) AS iv (snap_id, company_id, warehouse_id, product_id, date_start, date_end)
                LEFT JOIN sale_order so
                    ON  so.company_id   = iv.company_id
                    AND so.warehouse_id = iv.warehouse_id
                    AND so.state        = 'sale'
                    AND so.date_order  >= iv.date_start
                    AND so.date_order  <= iv.date_end
                LEFT JOIN sale_order_line sol
                    ON  sol.order_id   = so.id
                    AND sol.product_id = iv.product_id
                GROUP BY iv.snap_id
            """)
            actuals_by_snap = {row[0]: row[1] for row in self.env.cr.fetchall()}

            update_rows = []
            for snap in to_evaluate:
                actual_sales = actuals_by_snap.get(snap.id, 0.0)
                lead_days = snap.lead_time_days or 30
                forecasted_qty = snap.forecast_demand * (lead_days / 30.0)

                if forecasted_qty > 0.0:
                    ape = abs(forecasted_qty - actual_sales) / forecasted_qty
                elif actual_sales > 0.0:
                    ape = 1.0
                else:
                    ape = 0.0

                # absolute_error_pct has digits=(16, 2) — round explicitly since
                # a raw SQL write skips the ORM's write-time rounding for
                # digits-constrained Float fields.
                error_pct = float_round(min(9999.0, ape * 100.0), precision_digits=2)
                update_rows.append((snap.id, actual_sales, error_pct))

            # Bulk-write the whole batch in one round trip instead of one
            # write() per snapshot — on a backlog of tens of thousands of
            # pending snapshots, per-record writes were thousands of needless
            # round trips. Same VALUES-clause style as the read above.
            update_values_sql = ', '.join(
                self.env.cr.mogrify(
                    '(%s::int, %s::double precision, %s::double precision)', row
                ).decode()
                for row in update_rows
            )
            self.env.cr.execute(f"""
                UPDATE smart_reorder_forecast_snapshot AS t
                SET actual_sales        = v.actual_sales,
                    absolute_error_pct  = v.absolute_error_pct,
                    evaluated           = TRUE
                FROM (VALUES {update_values_sql}) AS v(id, actual_sales, absolute_error_pct)
                WHERE t.id = v.id
            """)
            to_evaluate.invalidate_recordset(['actual_sales', 'absolute_error_pct', 'evaluated'])

    def action_evaluate(self):
        # Restriction: Manager Group Only
        if not self.env.user.has_group('smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_("Only managers can manually trigger snapshot evaluation."))

        today = date.today()
        unevaluated = self.filtered(lambda s: not s.evaluated)
        not_ready = unevaluated.filtered(
            lambda s: s.snapshot_date + timedelta(days=s.lead_time_days) > today
        )
        ready = unevaluated - not_ready

        # Only refuse outright when NOTHING in the selection can be scored —
        # a mixed selection must still score whatever is mature instead of
        # erroring out on the whole batch because of one immature snapshot.
        if not ready:
            if not_ready:
                lines = []
                for snap in not_ready[:5]:
                    ready_date = snap.snapshot_date + timedelta(days=snap.lead_time_days)
                    lines.append(_("- %s: Evaluable from %s (Snapshot: %s, Lead Time: %d days)") % (
                        snap.product_id.display_name, ready_date, snap.snapshot_date, snap.lead_time_days
                    ))
                if len(not_ready) > 5:
                    lines.append(_("- ... and %d more snapshots") % (len(not_ready) - 5))
                raise UserError(_("Some selected snapshots are not yet scoreable:\n%s") % "\n".join(lines))
            raise UserError(_("All selected snapshots have already been evaluated."))

        self._score_snapshots(ready)

        # Show success notification: how many were scored, and — if any
        # immature snapshots were skipped rather than blocking the batch —
        # how many, and the earliest date at which they'll become evaluable.
        message = _('Successfully evaluated %d snapshot(s).') % len(ready)
        if not_ready:
            earliest_ready_date = min(
                snap.snapshot_date + timedelta(days=snap.lead_time_days) for snap in not_ready
            )
            message += ' ' + _(
                '%(count)d snapshot(s) skipped — not yet evaluable '
                '(earliest: %(date)s).'
            ) % {'count': len(not_ready), 'date': earliest_ready_date}

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Evaluation Completed'),
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }
