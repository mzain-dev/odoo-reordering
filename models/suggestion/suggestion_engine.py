from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_compare
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from psycopg2 import IntegrityError
import logging
import time
import math

from ...utils.access import require_group

_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'

_logger = logging.getLogger(__name__)

from ..reorder_engine import calculate_replenishment_levels


class SmartReorderSuggestion(models.Model):
    _inherit = 'smart.reorder.suggestion'

    def _sales_qty_by_product(self, company_id, warehouse_id, date_from, date_to_excl,
                              product_ids=None, storable_only=False):
        """Confirmed sales per product over [date_from, date_to_excl), summed
        in the PRODUCT's UoM. Returns {product_id: qty}."""
        company = self.env['res.company'].browse(company_id)
        tz_name = company.partner_id.tz or 'UTC'
        try:
            import pytz
            local_tz = pytz.timezone(tz_name)
        except Exception:
            local_tz = pytz.UTC

        from datetime import datetime, time
        # Convert date to local midnight and translate to UTC naive
        dt_from = datetime.combine(date_from, time.min)
        dt_from_utc = local_tz.localize(dt_from).astimezone(pytz.UTC).replace(tzinfo=None)

        dt_to = datetime.combine(date_to_excl, time.min)
        dt_to_utc = local_tz.localize(dt_to).astimezone(pytz.UTC).replace(tzinfo=None)

        params = [company_id, warehouse_id, dt_from_utc, dt_to_utc]
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
        month_starts = dates['month_starts']

        qty_map = self._sales_qty_by_product(
            company.id, warehouse.id, date_from, date_to + timedelta(days=1),
            storable_only=True,
        )
        product_ids_with_sales = list(qty_map.keys())

        # ── Q2: On-hand stock ──
        location = warehouse.lot_stock_id
        quant_data = self.env['stock.quant'].sudo().read_group(
            domain=[
                ('product_id',  'in', product_ids_with_sales),
                ('location_id', 'child_of', location.id),
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
        month_index = {m: i for i, m in enumerate(month_starts)}
        monthly_qty_by_product = {
            pid: [0.0] * analysis_months for pid in product_ids_with_sales
        }
        tz_name = company.partner_id.tz or 'UTC'
        try:
            import pytz
            local_tz = pytz.timezone(tz_name)
        except Exception:
            local_tz = pytz.UTC

        from datetime import datetime, time
        dt_start = datetime.combine(month_starts[-1], time.min)
        dt_start_utc = local_tz.localize(dt_start).astimezone(pytz.UTC).replace(tzinfo=None)

        dt_end = datetime.combine(date_to + timedelta(days=1), time.min)
        dt_end_utc = local_tz.localize(dt_end).astimezone(pytz.UTC).replace(tzinfo=None)

        self.env.cr.execute(f"""
            SELECT sol.product_id,
                   date_trunc('month', so.date_order AT TIME ZONE 'UTC' AT TIME ZONE %s)::date AS sale_month,
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
        """, (tz_name, company.id, warehouse.id, dt_start_utc, dt_end_utc, product_ids_with_sales))
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

        # ── Q3c: Incoming internal transfers ──
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

        # ── Q3d: Outgoing reserved stock ──
        outgoing_data = self.env['stock.move'].sudo().read_group(
            domain=[
                ('state',            'in', ['confirmed', 'waiting', 'assigned', 'partially_available']),
                ('location_id',      'child_of', warehouse.lot_stock_id.id),
                '!', ('location_dest_id', 'child_of', warehouse.lot_stock_id.id),
                ('product_id',       'in', product_ids_with_sales),
                ('company_id',       '=', company.id),
            ],
            fields=['product_id', 'product_qty:sum'],
            groupby=['product_id'],
        )
        outgoing_map = {r['product_id'][0]: r['product_qty'] for r in outgoing_data if r.get('product_id')}

        # ── Q4: Product costs (read context-specifically in bulk) ──
        products = self.env['product.product'].with_company(company).sudo().browse(product_ids_with_sales)
        product_lookup = {p.id: p for p in products}
        cost_map = {p.id: p.standard_price for p in products}
        tmpl_map = {p.id: p.product_tmpl_id.id for p in products}

        # ── Q5: Last sale date per product ──
        self.env.cr.execute("""
            SELECT sol.product_id, MAX(so.date_order AT TIME ZONE 'UTC' AT TIME ZONE %s)::date
              FROM sale_order_line sol
              JOIN sale_order so ON sol.order_id = so.id
             WHERE so.state       = 'sale'
               AND so.company_id   = %s
               AND so.warehouse_id = %s
               AND sol.product_id  = ANY(%s)
             GROUP BY sol.product_id
        """, (tz_name, company.id, warehouse.id, product_ids_with_sales))
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
                    r['delay'] or 0,
                    r['min_qty'] or 1.0,
                    r['price'] or 0.0,
                    r['currency_id'][0] if r.get('currency_id') else False
                )

        # ── Q7: Historical lead times ──
        po_history_lines = self.env['purchase.order.line'].sudo().read_group(
            domain=[
                ('product_id', 'in', product_ids_with_sales),
                ('order_id.state', 'in', ['purchase', 'done']),
                ('order_id.company_id', '=', company.id),
                ('order_id.date_approve', '!=', False),
                ('order_id.effective_date', '!=', False),
            ],
            fields=['product_id', 'order_id'],
            groupby=['product_id', 'order_id'],
            lazy=False,
        )
        po_ids = list({r['order_id'][0] for r in po_history_lines if r.get('order_id')})
        po_data = self.env['purchase.order'].sudo().search_read(
            [('id', 'in', po_ids)],
            ['id', 'partner_id', 'date_approve', 'effective_date']
        )
        po_by_id = {p['id']: p for p in po_data}
        lead_time_totals = {}
        for r in po_history_lines:
            pid = r['product_id'][0]
            po = po_by_id.get(r['order_id'][0]) if r.get('order_id') else None
            if po and po.get('date_approve') and po.get('effective_date'):
                diff = (po['effective_date'].date() - po['date_approve'].date()).days
                if diff > 0:
                    partner_id = po['partner_id'][0]
                    key = (pid, partner_id)
                    lead_time_totals.setdefault(key, []).append(diff)
        actual_lead_map = {
            key: round(sum(vals) / len(vals), 1)
            for key, vals in lead_time_totals.items()
        }

        # ── Q8: Historical sales totals ──
        prev_qty_map = self._sales_qty_by_product(
            company.id, warehouse.id, prev_date_from, prev_date_to + timedelta(days=1),
            product_ids=product_ids_with_sales,
        )
        ly_qty_map = self._sales_qty_by_product(
            company.id, warehouse.id, ly_date_from, ly_date_to + timedelta(days=1),
            product_ids=product_ids_with_sales,
        )

        # ── Q9: Last purchase cost/date/vendor (Task 1) ──
        # One row per product — the most recent confirmed PO line, DB-side —
        # instead of fetching all history and reducing in Python.
        self.env.cr.execute("""
            SELECT DISTINCT ON (pol.product_id)
                   pol.product_id, pol.price_unit, pol.product_uom,
                   po.date_order, po.partner_id, po.currency_id
              FROM purchase_order_line pol
              JOIN purchase_order po ON po.id = pol.order_id
             WHERE po.state IN ('purchase', 'done')
               AND po.company_id = %s
               AND pol.product_id = ANY(%s)
             ORDER BY pol.product_id, po.date_order DESC, pol.id DESC
        """, (company.id, product_ids_with_sales))
        last_purchase_rows = self.env.cr.fetchall()
        lp_uom_ids = {row[2] for row in last_purchase_rows if row[2]}
        lp_uoms_by_id = {u.id: u for u in self.env['uom.uom'].sudo().browse(list(lp_uom_ids))}
        last_purchase_map = {}
        for pid, price_unit, line_uom_id, order_date, partner_id, currency_id in last_purchase_rows:
            prod = prod_by_id.get(pid)
            price = price_unit or 0.0
            line_uom = lp_uoms_by_id.get(line_uom_id) if line_uom_id else None
            if prod and line_uom and line_uom != prod.uom_id:
                price = line_uom._compute_price(price, prod.uom_id)
            last_purchase_map[pid] = (
                price,
                order_date.date() if order_date else False,
                partner_id,
                currency_id,
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
            'last_purchase_map': last_purchase_map,
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
        partner_names_map, currency_convert_fn, reorder_behavior='system', previously_triggered=False,
        last_purchase_cost=0.0, last_purchase_date=None, last_purchase_vendor_id=False,
    ):
        from ..reorder_engine import calculate_product_suggestion
        return calculate_product_suggestion(
            product_id, product_code, product_name, tmpl_id, is_superseded, successor_display_name,
            company_id, warehouse_id, dates, config_data, warehouses_list,
            monthly_series, qty_on_hand, qty_incoming, qty_outgoing, cost,
            last_sale, current_month_sales, prev_qty, ly_qty,
            tmpl_suppliers, primary_vendor_info, actual_avg_days,
            overdue_lines, predecessors, predecessor_names,
            global_stocks, global_sales, lane_lead_times,
            partner_names_map, currency_convert_fn, reorder_behavior, previously_triggered,
            last_purchase_cost, last_purchase_date, last_purchase_vendor_id,
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
        for _i in range(analysis_months):
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

        all_products_read = self.env['product.product'].sudo().search_read([
            ('active', '=', True),
            ('type', '=', 'product'),
            ('product_tmpl_id.exclude_from_reorder_advisor', '=', False),
        ], ['product_tmpl_id'])
        tmpl_to_prod = {r['product_tmpl_id'][0]: r['id'] for r in all_products_read if r.get('product_tmpl_id')}
        active_prod_ids = {r['id'] for r in all_products_read}
        products = self.env['product.product'].sudo().browse(list(active_prod_ids))
        prod_by_id = {p.id: p for p in products}

        # Clear the provisional flag for suggestions in the analyzed scope (Fix 5)
        prov_domain = [
            ('company_id', '=', company.id),
            ('is_provisional', '=', True),
        ]
        if warehouse_ids:
            prov_domain.append(('warehouse_id', 'in', warehouse_ids))
        self.sudo().with_context(active_test=False).search(prov_domain).write({'is_provisional': False})

        for warehouse in warehouses:
            elapsed = time.time() - t_start
            if elapsed > CRON_TIMEOUT_SECS:
                _logger.warning(
                    'SmartReorder: 45-minute safety cap reached (%.0fs elapsed). '
                    'Stopping before Odoo.sh hard timeout. '
                    'Remaining warehouses skipped — will run on next cron.',
                    elapsed
                )
                had_errors = True
                log.sudo().write({
                    'error_count': log.error_count + 1,
                    'error_notes': ((log.error_notes or '') +
                        f'\n[{date.today()}] TIME CAP: stopped after {elapsed:.0f}s — '
                        f'warehouse "{warehouse.name}" and all remaining warehouses '
                        f'were NOT refreshed this run.').strip(),
                })
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
                    last_purchase_map = data['last_purchase_map']
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
                        'spike_dominance_pct': config.spike_dominance_pct,
                        'spike_multiplier': config.spike_multiplier,
                        'overstock_ceiling_months': config.overstock_ceiling_months,
                        'transfer_surplus_threshold': config.transfer_surplus_threshold,
                        'default_internal_transfer_days': config.default_internal_transfer_days,
                        'temp_vendor_ids': config.temp_vendor_ids.ids,
                    }

                    warehouses_list = [{'id': wh.id, 'name': wh.name} for wh in warehouses]

                    supplier_currency_ids = set()
                    for suppliers in template_suppliers_map.values():
                        for sup in suppliers:
                            if sup.get('currency_id'):
                                supplier_currency_ids.add(sup['currency_id'][0])
                    for _price, _dt, _partner_id, lp_currency_id in last_purchase_map.values():
                        if lp_currency_id:
                            supplier_currency_ids.add(lp_currency_id)

                    rates_map = {}
                    company_currency = company.currency_id
                    for curr_id in supplier_currency_ids:
                        if curr_id == company_currency.id:
                            rates_map[curr_id] = 1.0
                        else:
                            curr_obj = self.env['res.currency'].browse(curr_id)
                            try:
                                rates_map[curr_id] = curr_obj._convert(
                                    1.0,
                                    company_currency,
                                    company,
                                    date.today(),
                                )
                            except Exception:
                                rates_map[curr_id] = 1.0

                    def currency_convert_fn(price, from_currency_id):
                        if price <= 0.0 or not from_currency_id or from_currency_id == company_currency.id:
                            return price
                        rate = rates_map.get(from_currency_id, 1.0)
                        return price * rate

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

                        lp_price, lp_date, lp_vendor_id, lp_currency_id = last_purchase_map.get(
                            product_id, (0.0, False, False, False)
                        )
                        last_purchase_cost = currency_convert_fn(lp_price, lp_currency_id)

                        existing_rec = existing_map.get(product_id)
                        prev_triggered = existing_rec.reorder_needed if existing_rec else False

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
                            reorder_behavior=product.reorder_behavior,
                            previously_triggered=prev_triggered,
                            last_purchase_cost=last_purchase_cost,
                            last_purchase_date=lp_date,
                            last_purchase_vendor_id=lp_vendor_id,
                        )

                        u_rank = URGENCY_RANK.get(vals['urgency'], 5)
                        suggestion_values.append((product_id, vals['estimated_purchase_value'], u_rank, vals))

                        if vals['urgency'] == 'critical':
                            total_critical += 1

                    suggestion_values.sort(key=lambda x: (x[2], -x[1]))
                    budget        = config.budget_cap
                    running_total = 0.0

                    for rank, (_pid, rv, _u_rank, vals) in enumerate(suggestion_values, 1):
                        vals['budget_rank'] = rank
                        if budget > 0 and vals['reorder_needed']:
                            running_total         += rv
                            vals['within_budget']  = running_total <= budget
                        else:
                            vals['within_budget'] = True

                    to_create = []
                    to_write  = []
                    for _pid, _rv, _u_rank, vals in suggestion_values:
                        pid = vals['product_id']
                        if pid in existing_map:
                            existing_rec = existing_map[pid]
                            old_qty = existing_rec.suggested_reorder_qty
                            new_qty = vals['suggested_reorder_qty']
                            write_vals = dict(vals)
                            write_vals['prior_suggested_qty'] = old_qty
                            write_vals['delta_pct']           = self._calc_delta_pct(old_qty, new_qty)

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

                                # Task 8: stale draft PO alert — refreshed each
                                # run, same cadence as is_stale/is_overstocked.
                                stale_threshold = config.stale_draft_po_days
                                oldest_create = min(open_draft_pos.mapped('create_date'))
                                age_days = (fields.Datetime.now() - oldest_create).days
                                write_vals['draft_po_stale_days'] = age_days
                                write_vals['is_draft_po_stale'] = bool(
                                    stale_threshold > 0 and age_days > stale_threshold
                                )
                                if write_vals['is_draft_po_stale']:
                                    draft_po_note += (
                                        f' ⚠️ Sitting unconfirmed for {age_days} days '
                                        f'(alert threshold: {stale_threshold} days).'
                                    )
                            else:
                                write_vals['draft_po_stale_days'] = 0
                                write_vals['is_draft_po_stale'] = False

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
                            if existing_rec.is_marked_ordered:
                                # Task 5: honor exactly one more suppressed run, then
                                # consume the flag — if it's genuinely still needed
                                # after that (order lost/delayed), it resurfaces
                                # normally instead of staying silently hidden.
                                would_still_be_needed = bool(write_vals.get('reorder_needed'))
                                write_vals['reorder_needed'] = False
                                write_vals['is_marked_ordered'] = False
                                marked_note = (
                                    f'✅ Marked as Ordered by '
                                    f'{existing_rec.marked_ordered_by_id.name or "someone"} on '
                                    f'{existing_rec.marked_ordered_at} — suppressed for this run. '
                                    'Will resurface next run if the purchase still has not been logged.'
                                )
                                write_vals['notes'] = f'{marked_note}\n\n' + (write_vals.get('notes') or '')

                                # Recommendation: reconciliation trail — resolve
                                # the pending log entry(ies) for this suggestion
                                # with whichever outcome actually happened.
                                pending_logs = self.env['smart.reorder.mark.ordered.log'].sudo().search([
                                    ('suggestion_id', '=', existing_rec.id),
                                    ('outcome', '=', 'pending'),
                                ])
                                if pending_logs:
                                    pending_logs.write({
                                        'outcome': 'resurfaced' if would_still_be_needed else 'cleared',
                                        'resolved_at': fields.Datetime.now(),
                                    })
                            to_write.append((existing_rec, write_vals))
                        else:
                            vals['prior_suggested_qty'] = 0.0
                            vals['delta_pct'] = self._calc_delta_pct(0.0, vals['suggested_reorder_qty'])
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

                    _computed_keys = (
                        'estimated_purchase_value',
                        'months_of_stock_after_order',
                        'is_overstocked',
                    )
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
                        changes = {}
                        for k, new_val in vals.items():
                            old_val = rec[k]
                            field_type = rec._fields[k].type
                            if field_type == 'many2one':
                                old_id = old_val.id if old_val else False
                                new_id = new_val.id if isinstance(new_val, models.Model) else new_val
                                if old_id != new_id:
                                    changes[k] = new_val
                            elif field_type == 'float':
                                if float_compare(old_val or 0.0, new_val or 0.0, precision_digits=4) != 0:
                                    changes[k] = new_val
                            else:
                                if old_val != new_val:
                                    changes[k] = new_val

                        if changes:
                            rec.sudo().with_context(tracking_disable=True).write(changes)
                        total_created += 1

                    Snapshot = self.env['smart.reorder.forecast.snapshot'].sudo()
                    existing_pending_pids = set(Snapshot.search([
                        ('company_id', '=', company.id),
                        ('warehouse_id', '=', warehouse.id),
                        ('evaluated', '=', False)
                    ]).mapped('product_id.id'))

                    snapshots_to_create = []
                    for _pid, _rv, _u_rank, vals in suggestion_values:
                        pid = vals['product_id']
                        if pid in existing_pending_pids:
                            continue

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
                        })
                    if snapshots_to_create:
                        Snapshot.create(snapshots_to_create)

            except Exception as e:
                # The savepoint above rolled back everything this warehouse's
                # batch did and restored a clean transaction. Do NOT call
                # cr.rollback() here — a full rollback would destroy the run-lock
                # log record and every prior warehouse's/company's writes in this
                # cron transaction.
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

    @api.model
    @require_group(_MANAGER_GROUP)
    def generate_suggestions(self, company_ids=None, warehouse_ids=None,
                             include_zero_demand=False,
                             _cron_start=None,
                             trigger_type='cron'):
        """
        Query plan for N products across W warehouses:
        ───────────────────────────────────
        Per warehouse (8 bulk SQL queries total, regardless of N)
        """
        URGENCY_RANK = {'critical': 1, 'urgent': 2, 'normal': 3, 'dead': 4, 'ok': 5}
        CRON_TIMEOUT_SECS = 45 * 60
        STUCK_LOCK_TIMEOUT_SECS = 60 * 60
        t_start = _cron_start or time.time()
        CronLog = self.env['smart.reorder.cron.log'].sudo()

        all_warehouses = self.env['stock.warehouse'].sudo().search([])
        warehouse_by_location = {}
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
                    'SmartReorder: Skipping company %s — no config found. (%s)',
                    company.name, e
                )
                continue

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
                    'error_notes': 'Auto-aborted: superseded after the lock went stale.',
                })

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
                log.write({
                    'finished_at': fields.Datetime.now(),
                    'status': 'aborted',
                    'error_notes': str(e),
                })
                raise

        _logger.info('SmartReorder: Done. %d records. %d critical.', total_created, total_critical)
        return {'created': total_created, 'critical': total_critical}

    @api.model
    def action_run_weekly_cron(self):
        t0 = time.time()
        _logger.info('SmartReorder CRON: Starting...')
        result = self.generate_suggestions(_cron_start=t0)
        elapsed = round(time.time() - t0, 1)
        _logger.info(
            'SmartReorder CRON: Done in %.1fs — %d records, %d critical',
            elapsed, result.get('created', 0), result.get('critical', 0)
        )
        self.env['smart.reorder.forecast.snapshot'].sudo()._score_snapshots()
        self.env['smart.reorder.cron.log'].sudo()._purge_old_logs()

    @api.model
    def _send_notifications(self, company, config, critical_count, warehouse_id=None):
        from ..reorder_notifier import send_notifications
        return send_notifications(self, company, config, critical_count, warehouse_id=warehouse_id)

    @api.model
    def _send_email_report(self, company, config):
        from ..reorder_notifier import send_email_report
        return send_email_report(self, company, config)

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

    @api.model
    def _flag_negative_stock_product(self, product_id, warehouse_id, company_id, notify=True):
        config = self._get_config(company_id)
        if not config.auto_flag_on_negative:
            return

        product = self.env['product.product'].with_company(company_id).sudo().browse(product_id)
        location  = self.env['stock.warehouse'].sudo().browse(warehouse_id).lot_stock_id
        quants    = self.env['stock.quant'].sudo().search([
            ('product_id',  '=', product_id),
            ('location_id', 'child_of', location.id),
        ])
        qty_on_hand = sum(quants.mapped('quantity'))

        supplierinfo = self.env['product.supplierinfo'].sudo().search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            '|', ('company_id', '=', False), ('company_id', '=', company_id)
        ], order='sequence asc', limit=1)

        moq = supplierinfo.min_qty or 1.0

        round_to_moq = self._round_to_moq
        abs_neg_qty = abs(qty_on_hand) if qty_on_hand < 0 else 0.0

        existing = self.sudo().with_context(active_test=False).search([
            ('product_id',   '=', product_id),
            ('warehouse_id', '=', warehouse_id),
            ('company_id',   '=', company_id),
        ], limit=1)

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
            avg_monthly = existing.avg_monthly_demand
            qty_incoming = existing.qty_incoming
            qty_outgoing = existing.qty_outgoing
            qty_available = qty_on_hand + qty_incoming - qty_outgoing

            lead_months_to_use = existing.lead_time_months or lead_months
            safety_buffer = existing.safety_buffer_months
            order_cycle = existing.order_cycle_months
            moq_to_use = existing.moq or moq

            levels = calculate_replenishment_levels(
                avg_monthly, lead_months_to_use, safety_buffer,
                order_cycle, qty_available, qty_on_hand, moq_to_use,
                previously_triggered=existing.reorder_needed
            )
            suggested_qty = int(math.ceil(max(levels['suggested_reorder_qty'], round_to_moq(abs_neg_qty, moq_to_use))))

            cost = existing.product_cost or cost
            vendor_price = existing.vendor_price or vendor_price
            vendor_id = existing.vendor_id.id if existing.vendor_id else vendor_id
            stated_lead_days = existing.vendor_stated_lead_days or stated_lead_days
            moq = moq_to_use
        else:
            suggested_qty = int(math.ceil(round_to_moq(abs_neg_qty, moq)))

        notes = (
            f'⚠️ AUTO-FLAGGED (PROVISIONAL): Stock went negative to {qty_on_hand:.2f} on {date.today()}.\n'
            f'Provisional emergency suggestion computed: raised to vendor minimum of {moq:.0f}.\n'
            f'Run full analysis to recalculate complete suggestion.'
        )

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

        if notify:
            self._send_notifications(
                self.env['res.company'].browse(company_id), config, 1, warehouse_id=warehouse_id
            )
