# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import date, timedelta
import math

class ForecastSnapshot(models.Model):
    _name = 'smart.reorder.forecast.snapshot'
    _description = 'Forecast Snapshot for Back-testing'
    _order = 'snapshot_date desc, id desc'

    company_id = fields.Many2one('res.company', string='Company', required=True, readonly=True, ondelete='cascade')
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', required=True, readonly=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', string='Product', required=True, readonly=True, ondelete='cascade')
    snapshot_date = fields.Date(string='Snapshot Date', required=True, readonly=True, default=fields.Date.context_today)
    
    forecast_demand = fields.Float(string='Forecast Demand (Monthly)', readonly=True)
    confidence = fields.Float(string='Forecast Confidence', readonly=True)
    lead_time_days = fields.Integer(string='Lead Time (Days)', readonly=True)
    abc_class = fields.Selection([('A', 'A'), ('B', 'B'), ('C', 'C')], string='ABC Class', readonly=True)
    
    actual_sales = fields.Float(string='Actual Sales (since)', readonly=True)
    absolute_error_pct = fields.Float(string='MAPE (%)', readonly=True, digits=(16, 2))
    evaluated = fields.Boolean(string='Evaluated', default=False, index=True)

    @api.model
    def _score_snapshots(self):
        """
        Scheduled or on-demand scorer comparing snapshots older than one lead
        time against actual sales in that period.

        Performance: previously issued one SQL query per unevaluated snapshot.
        Now issues a single aggregate query for ALL snapshots' intervals, then
        applies MAPE in Python — O(1 query) regardless of snapshot count.
        """
        today = date.today()
        snapshots = self.search([('evaluated', '=', False)])
        to_evaluate = snapshots.filtered(
            lambda s: s.snapshot_date + timedelta(days=s.lead_time_days) <= today
        )
        if not to_evaluate:
            return

        # Build one aggregate query: SUM(qty_delivered) per
        # (company_id, warehouse_id, product_id, date_start, date_end).
        # We use a VALUES list to join against — each row is one snapshot's window.
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

        for snap in to_evaluate:
            actual_sales = actuals_by_snap.get(snap.id, 0.0)
            forecasted_qty = snap.forecast_demand * (snap.lead_time_days / 30.0)

            if forecasted_qty > 0.0:
                ape = abs(forecasted_qty - actual_sales) / forecasted_qty
            elif actual_sales > 0.0:
                ape = 1.0
            else:
                ape = 0.0

            snap.write({
                'actual_sales': actual_sales,
                'absolute_error_pct': min(9999.0, ape * 100.0),
                'evaluated': True,
            })

    def action_evaluate(self):
        self._score_snapshots()
        return True

