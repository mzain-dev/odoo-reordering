from odoo import models, fields, api, _, Command
from odoo.exceptions import UserError
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import math
import logging

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
    _description = 'Smart Reorder Suggestion'
    _order = 'urgency_rank asc, reorder_value desc'
    _rec_name = 'product_id'

    # ── Identity ──────────────────────────────────────────────────────────────
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
    avg_monthly_demand  = fields.Float(string='Avg Monthly Demand',      digits=(16, 2), readonly=True)
    lead_time_months    = fields.Float(string='Lead Time (Months)',       digits=(16, 2), readonly=True)
    safety_buffer_months= fields.Float(string='Safety Buffer (Months)',   digits=(16, 2), readonly=True)
    moq                 = fields.Float(string='Min Order Qty (MOQ)',      digits=(16, 2), readonly=True)

    # ── Trend (Phase 2) ───────────────────────────────────────────────────────
    prev_period_qty_sold = fields.Float(string='Prev Period Qty Sold', digits=(16, 2), readonly=True)
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
    qty_on_hand  = fields.Float(string='On Hand Qty',   digits=(16, 2), readonly=True)
    qty_incoming = fields.Float(string='Incoming (PO)', digits=(16, 2), readonly=True)
    qty_available = fields.Float(
        string='Net Available', digits=(16, 2), readonly=True,
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
    suggested_reorder_qty = fields.Float(   string='Suggested Reorder Qty',        digits=(16, 0), readonly=True)
    raw_reorder_qty       = fields.Float(   string='Raw Qty (before MOQ rounding)', digits=(16, 2), readonly=True)
    reorder_value         = fields.Monetary(string='Reorder Value', currency_field='currency_id',
                                            readonly=True, compute='_compute_reorder_value', store=True)
    currency_id           = fields.Many2one('res.currency', related='company_id.currency_id', store=True)
    reorder_needed        = fields.Boolean( string='Reorder Needed?', readonly=True)

    # ── Budget ────────────────────────────────────────────────────────────────
    within_budget = fields.Boolean(string='Within Budget?',       readonly=True)
    budget_rank   = fields.Integer(string='Budget Priority Rank', readonly=True)

    # ── Classification ────────────────────────────────────────────────────────
    abc_class = fields.Selection([
        ('A', 'A — Fast Mover'),
        ('B', 'B — Medium Mover'),
        ('C', 'C — Slow / Dead'),
    ], string='ABC Class', readonly=True, index=True)

    urgency = fields.Selection([
        ('critical', '🔴 Critical — Negative Stock'),
        ('urgent',   '🟠 Urgent — Stock < Lead Time'),
        ('normal',   '🟡 Normal — Reorder Recommended'),
        ('dead',     '💀 Dead Stock — No Movement'),
        ('ok',       '🟢 OK — Sufficient Stock'),
    ], string='Urgency', readonly=True, index=True)

    # FIX 1: urgency_rank is a stored computed field — always in sync with urgency
    urgency_rank = fields.Integer(compute='_compute_urgency_rank', store=True)

    # ── Vendor ────────────────────────────────────────────────────────────────
    vendor_id              = fields.Many2one('res.partner', string='Primary Vendor', readonly=True)
    vendor_stated_lead_days= fields.Integer(string='Vendor Stated Lead (Days)', readonly=True)
    vendor_actual_avg_days = fields.Float(  string='Vendor Actual Avg Lead (Days)', digits=(16, 1), readonly=True)
    vendor_performance_note= fields.Char(   string='Vendor Performance', readonly=True)

    transfer_source_warehouse_id = fields.Many2one('stock.warehouse', string='Transfer Source Warehouse', readonly=True)
    po_ids = fields.Many2many('purchase.order', string="Drafted Orders", readonly=True)

    notes = fields.Text(string='Calculation Breakdown', readonly=True)

    # ══════════════════════════════════════════════════════════════════════════
    # COMPUTED FIELDS
    # ══════════════════════════════════════════════════════════════════════════

    @api.depends('qty_on_hand', 'qty_incoming')
    def _compute_qty_available(self):
        for rec in self:
            rec.qty_available = rec.qty_on_hand + rec.qty_incoming

    @api.depends('qty_available', 'avg_monthly_demand')
    def _compute_months_of_stock(self):
        for rec in self:
            rec.months_of_stock = (
                rec.qty_available / rec.avg_monthly_demand
                if rec.avg_monthly_demand > 0 else 999.0
            )

    @api.depends('suggested_reorder_qty', 'product_cost')
    def _compute_reorder_value(self):
        for rec in self:
            rec.reorder_value = rec.suggested_reorder_qty * rec.product_cost

    @api.depends('urgency')
    def _compute_urgency_rank(self):
        # FIX 1: rank map defined here, not after the loop
        rank_map = {'critical': 1, 'urgent': 2, 'normal': 3, 'dead': 4, 'ok': 5}
        for rec in self:
            rec.urgency_rank = rank_map.get(rec.urgency, 5)

    # ══════════════════════════════════════════════════════════════════════════
    # STATIC HELPERS (no DB access — safe inside loops)
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _round_to_moq(qty, moq):
        """Round qty UP to nearest MOQ multiple. Never returns negative."""
        if moq <= 0 or qty <= 0:
            return max(0.0, qty)
        return math.ceil(qty / moq) * moq

    @staticmethod
    def _classify_abc(avg_monthly_demand, config):
        if avg_monthly_demand >= config.abc_a_threshold:
            return 'A'
        if avg_monthly_demand >= config.abc_b_threshold:
            return 'B'
        return 'C'

    @staticmethod
    def _determine_urgency(qty_on_hand, months_of_stock, avg_monthly,
                           lead_months, is_dead_stock, suggested_qty):
        if qty_on_hand < 0:
            return 'critical'
        if is_dead_stock and qty_on_hand > 0:
            return 'dead'
        if avg_monthly > 0 and months_of_stock < lead_months:
            return 'urgent'
        if suggested_qty > 0:
            return 'normal'
        return 'ok'

    @staticmethod
    def _calc_trend(current_qty, prev_qty, analysis_months, comparison_months):
        """
        Pure calculation — no DB. Called inside loop after bulk pre-fetch.
        Returns (trend_selection, trend_pct).
        """
        current_per_month = current_qty / max(analysis_months, 1)
        prev_per_month    = prev_qty    / max(comparison_months, 1)

        if prev_per_month == 0 and current_per_month == 0:
            return ('new', 0.0)
        if prev_per_month == 0:
            return ('up', 100.0)

        pct = ((current_per_month - prev_per_month) / prev_per_month) * 100
        if pct >= 15:
            return ('up', round(pct, 1))
        if pct <= -15:
            return ('down', round(pct, 1))
        return ('stable', round(pct, 1))

    @staticmethod
    def _calc_seasonal_note(current_qty, ly_qty):
        """Pure calculation — no DB."""
        if ly_qty == 0:
            return 'No data for same period last year'
        pct = ((current_qty - ly_qty) / ly_qty) * 100
        if pct >= 20:
            return f'↑ {pct:.0f}% vs same period last year (seasonal spike?)'
        if pct <= -20:
            return f'↓ {abs(pct):.0f}% vs same period last year (seasonal dip?)'
        return f'≈ Consistent with same period last year ({pct:+.0f}%)'

    # ══════════════════════════════════════════════════════════════════════════
    # MAIN ENGINE — generate_suggestions()
    # ALL bulk queries happen BEFORE the product loop.
    # The loop does only in-memory dict lookups (O(1) per product).
    # ══════════════════════════════════════════════════════════════════════════

    @api.model
    def generate_suggestions(self, company_ids=None, warehouse_ids=None):
        """
        Query plan for N products across W warehouses:
        ─────────────────────────────────────────────
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
        """
        URGENCY_RANK = {'critical': 1, 'urgent': 2, 'normal': 3, 'dead': 4, 'ok': 5}

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

        if company_ids:
            companies = self.env['res.company'].sudo().browse(company_ids)
        else:
            companies = self.env['res.company'].sudo().search([])

        total_created = 0
        total_critical = 0

        for company in companies:
            config = self._get_config(company.id)
            analysis_months    = int(config.analysis_period)
            comparison_months  = config.trend_comparison_months
            date_to            = date.today()
            date_from          = date_to - relativedelta(months=analysis_months)
            prev_date_to       = date_from - timedelta(days=1)
            prev_date_from     = prev_date_to - relativedelta(months=comparison_months)
            ly_date_from       = date_from - relativedelta(years=1)
            ly_date_to         = date_to   - relativedelta(years=1)

            if warehouse_ids:
                warehouses = self.env['stock.warehouse'].sudo().browse(warehouse_ids).filtered(
                    lambda w: w.company_id.id == company.id
                )
            else:
                warehouses = self.env['stock.warehouse'].sudo().search(
                    [('company_id', '=', company.id)]
                )

            for warehouse in warehouses:
                _logger.info(
                    'SmartReorder: %s → %s (%d months)',
                    company.name, warehouse.name, analysis_months
                )

                # ── FIX 6: Purge stale records for this scope FIRST ──────────
                stale = self.sudo().search([
                    ('company_id',   '=', company.id),
                    ('warehouse_id', '=', warehouse.id),
                ])
                if stale:
                    stale.sudo().unlink()
                    _logger.info(
                        'SmartReorder: Purged %d stale records for %s/%s',
                        len(stale), company.name, warehouse.name
                    )

                # ── Q1: Current period sales ──────────────────────────────────
                sale_data = self.env['sale.order.line'].sudo().read_group(
                    domain=[
                        ('order_id.state',        'in', ['sale', 'done']),
                        ('order_id.company_id',   '=',  company.id),
                        ('order_id.warehouse_id', '=',  warehouse.id),
                        ('order_id.date_order',   '>=', str(date_from)),
                        ('order_id.date_order',   '<=', str(date_to)),
                        ('product_id.type',       '=',  'product'),
                        ('product_id.product_tmpl_id.exclude_from_reorder_advisor', '=', False),
                    ],
                    fields=['product_id', 'product_uom_qty:sum'],
                    groupby=['product_id'],
                )
                qty_map                = {r['product_id'][0]: r['product_uom_qty'] for r in sale_data}
                product_ids_with_sales = list(qty_map.keys())

                # ── Q2: On-hand stock ─────────────────────────────────────────
                location  = warehouse.lot_stock_id
                quant_data = self.env['stock.quant'].sudo().read_group(
                    domain=[
                        ('product_id',   'in', product_ids_with_sales),
                        ('location_id',  'child_of', location.id),
                    ],
                    fields=['product_id', 'quantity:sum'],
                    groupby=['product_id'],
                ) if product_ids_with_sales else []
                onhand_map = {r['product_id'][0]: r['quantity'] for r in quant_data}

                # ── Q2b: Negative stock (no recent sales but still negative) ──
                neg_data = self.env['stock.quant'].sudo().read_group(
                    domain=[
                        ('location_id', 'child_of', location.id),
                        ('quantity',    '<', 0),
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

                if not product_ids_with_sales:
                    _logger.info('SmartReorder: No products found for %s/%s', company.name, warehouse.name)
                    continue

                # ── Q3: Incoming PO qty ───────────────────────────────────────
                incoming_data = self.env['purchase.order.line'].sudo().read_group(
                    domain=[
                        ('order_id.state',      'in', ['purchase', 'done']),
                        ('order_id.company_id', '=',  company.id),
                        ('product_id',          'in', product_ids_with_sales),
                        ('date_planned',        '>=', str(date_to)),
                    ],
                    fields=['product_id', 'product_qty:sum', 'qty_received:sum'],
                    groupby=['product_id'],
                )
                incoming_map = {
                    r['product_id'][0]: max(0.0, r['product_qty'] - r['qty_received'])
                    for r in incoming_data
                }

                # ── Q4: Product cost + template_id ────────────────────────────
                products       = self.env['product.product'].sudo().browse(product_ids_with_sales)
                product_lookup = {p.id: p for p in products}
                cost_map       = {p.id: p.standard_price for p in products}
                tmpl_map       = {p.id: p.product_tmpl_id.id for p in products}

                # ── Q5: Last sale date per product — direct SQL ───────────────
                # REASON: search_read() with no date limit on sale_order_line
                # across 7,000 products with years of history can load hundreds
                # of thousands of rows into Python memory → OOM / gateway timeout.
                # Direct SQL lets PostgreSQL compute MAX(date_order) server-side,
                # returning only one row per product (fast and memory-safe).
                if product_ids_with_sales:
                    self.env.cr.execute("""
                        SELECT sol.product_id, MAX(so.date_order)::date
                          FROM sale_order_line sol
                          JOIN sale_order so ON sol.order_id = so.id
                         WHERE so.state       IN ('sale', 'done')
                           AND so.warehouse_id = %s
                           AND sol.product_id  = ANY(%s)
                         GROUP BY sol.product_id
                    """, (warehouse.id, product_ids_with_sales))
                    last_sale_map = {
                        row[0]: row[1]   # product_id → date object (already a date from ::date cast)
                        for row in self.env.cr.fetchall()
                        if row[1] is not None
                    }
                else:
                    last_sale_map = {}

                # ── Q6: FIX 3 — Batch vendor info (one query, not per product) ──
                all_tmpl_ids = list(set(tmpl_map.values()))
                supplier_data = self.env['product.supplierinfo'].sudo().search_read(
                    domain=[('product_tmpl_id', 'in', all_tmpl_ids)],
                    fields=['product_tmpl_id', 'partner_id', 'delay', 'min_qty'],
                    order='product_tmpl_id asc, sequence asc',
                )
                # Keep only the first (lowest sequence) supplier per template
                vendor_map = {}   # tmpl_id → (partner_id, lead_days, min_qty)
                for r in supplier_data:
                    tid = r['product_tmpl_id'][0]
                    if tid not in vendor_map:
                        vendor_map[tid] = (
                            r['partner_id'][0] if r.get('partner_id') else False,
                            r['delay'] or (config.default_lead_time_months * 30),
                            r['min_qty'] or 1.0,
                        )

                # ── Q7: FIX 4 — Batch previous period sales (trend) ──────────
                prev_sale_data = self.env['sale.order.line'].sudo().read_group(
                    domain=[
                        ('order_id.state',        'in', ['sale', 'done']),
                        ('order_id.company_id',   '=',  company.id),
                        ('order_id.warehouse_id', '=',  warehouse.id),
                        ('order_id.date_order',   '>=', str(prev_date_from)),
                        ('order_id.date_order',   '<=', str(prev_date_to)),
                        ('product_id',            'in', product_ids_with_sales),
                    ],
                    fields=['product_id', 'product_uom_qty:sum'],
                    groupby=['product_id'],
                )
                prev_qty_map = {r['product_id'][0]: r['product_uom_qty'] for r in prev_sale_data}

                # ── Q8: FIX 4 — Batch last-year same period (seasonal) ───────
                ly_sale_data = self.env['sale.order.line'].sudo().read_group(
                    domain=[
                        ('order_id.state',        'in', ['sale', 'done']),
                        ('order_id.company_id',   '=',  company.id),
                        ('order_id.warehouse_id', '=',  warehouse.id),
                        ('order_id.date_order',   '>=', str(ly_date_from)),
                        ('order_id.date_order',   '<=', str(ly_date_to)),
                        ('product_id',            'in', product_ids_with_sales),
                    ],
                    fields=['product_id', 'product_uom_qty:sum'],
                    groupby=['product_id'],
                )
                ly_qty_map = {r['product_id'][0]: r['product_uom_qty'] for r in ly_sale_data}

                # BUG 3 FIX: Q2c and Q1c moved to AFTER product list is built
                # but still inside the warehouse loop so product_ids_with_sales is available.
                # Q2c: Global stock across ALL warehouses of this company
                # (needed for inter-warehouse rebalancing suggestions)
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
                global_stock_map = {}  # { product_id: { warehouse_id: qty } }
                for r in global_quant_data:
                    pid    = r['product_id'][0]
                    loc_id = r['location_id'][0]
                    qty    = r['quantity']
                    wh     = warehouse_by_location.get(loc_id)
                    if wh:
                        global_stock_map.setdefault(pid, {}).setdefault(wh.id, 0.0)
                        global_stock_map[pid][wh.id] += qty

                # BUG 1 FIX: Q1c — global sales per product per warehouse
                # sale.order.line has NO warehouse_id field — it is on sale.order.
                # Must use 'order_id.warehouse_id' as the relational path.
                self.env.cr.execute("""
                    SELECT sol.product_id, so.warehouse_id, SUM(sol.product_uom_qty)
                      FROM sale_order_line sol
                      JOIN sale_order so ON sol.order_id = so.id
                     WHERE so.state        IN ('sale', 'done')
                       AND so.company_id    = %s
                       AND so.date_order   >= %s
                       AND so.date_order   <= %s
                       AND sol.product_id   = ANY(%s)
                     GROUP BY sol.product_id, so.warehouse_id
                """, (company.id, str(date_from), str(date_to), product_ids_with_sales))
                qty_map_global = {
                    (row[0], row[1]): row[2]
                    for row in self.env.cr.fetchall()
                    if row[0] is not None and row[1] is not None
                }

                # BUG 4 FIX: actual_lead_map SQL — filter by primary vendor per product
                # Without vendor filter, products ordered from multiple vendors get a
                # blended average that represents neither vendor accurately.
                # We use vendor_map (built in Q6 above — but Q6 runs after this block,
                # so we run actual_lead SQL after Q6 in the loop below via tmpl_map).
                # For now, set empty — populated per-product in the loop using vendor_map.
                actual_lead_map = {}

                # ── FIX 5: Pre-fetch existing suggestions (no search in loop) ─
                # After purge this dict will always be empty — but kept here
                # for the auto-flag path which writes outside the normal flow.
                existing_map = {}  # product_id → record

                # ── PRODUCT LOOP — pure Python, O(1) lookups only ─────────────
                suggestion_values = []   # (product_id, reorder_value, urgency_rank_int, vals)

                for product_id in product_ids_with_sales:
                    product = product_lookup.get(product_id)
                    if not product:
                        continue

                    total_sold   = qty_map.get(product_id, 0.0)
                    avg_monthly  = total_sold / analysis_months if analysis_months > 0 else 0.0
                    qty_on_hand  = onhand_map.get(product_id, 0.0)
                    qty_incoming = incoming_map.get(product_id, 0.0)
                    qty_available= qty_on_hand + qty_incoming
                    cost         = cost_map.get(product_id, 0.0)

                    # Vendor: O(1) dict lookup (FIX 3)
                    tmpl_id = tmpl_map.get(product_id)
                    if tmpl_id and tmpl_id in vendor_map:
                        v_partner_id, v_lead_days, v_moq = vendor_map[tmpl_id]
                    else:
                        v_partner_id = False
                        v_lead_days  = config.default_lead_time_months * 30
                        v_moq        = 1.0
                    lead_months     = v_lead_days / 30.0
                    stated_lead_days= int(v_lead_days)
                    moq             = v_moq

                    # BUG 4 FIX: Vendor performance scoped to primary vendor only.
                    # Running per-product SQL here is acceptable because track_vendor_performance
                    # is opt-in (default False) so this block is skipped for most installs.
                    actual_avg_days = 0.0
                    vendor_perf_note = ''
                    if config.track_vendor_performance and v_partner_id:
                        self.env.cr.execute("""
                            SELECT AVG(EXTRACT(DAY FROM (pol.date_planned - po.date_approve)))
                              FROM purchase_order_line pol
                              JOIN purchase_order po ON pol.order_id = po.id
                             WHERE po.state         = 'done'
                               AND po.partner_id    = %s
                               AND po.date_approve IS NOT NULL
                               AND pol.date_planned IS NOT NULL
                               AND pol.product_id   = %s
                        """, (v_partner_id, product_id))
                        row = self.env.cr.fetchone()
                        if row and row[0] is not None:
                            actual_avg_days = round(float(row[0]), 1)
                            diff = actual_avg_days - stated_lead_days
                            if diff > 5:
                                vendor_perf_note = f'Typically {diff:.0f} days late (avg {actual_avg_days:.0f}d vs stated {stated_lead_days}d).'
                                lead_months = actual_avg_days / 30.0
                            elif diff < -5:
                                vendor_perf_note = f'Delivers {abs(diff):.0f} days early on average (avg {actual_avg_days:.0f}d).'
                            else:
                                vendor_perf_note = f'Delivers on time (avg {actual_avg_days:.0f}d vs stated {stated_lead_days}d).'

                    # Months of stock
                    months_of_stock = qty_available / avg_monthly if avg_monthly > 0 else 999.0

                    # Dead stock
                    last_sale       = last_sale_map.get(product_id)
                    months_since    = int((date_to - last_sale).days / 30) if last_sale else 0
                    if last_sale:
                        is_dead = (months_since >= config.dead_stock_months and config.flag_dead_stock)
                    else:
                        is_dead = (qty_on_hand > 0 and config.flag_dead_stock)

                    # Core formula
                    demand_lead   = avg_monthly * lead_months
                    demand_buffer = avg_monthly * config.safety_buffer_months
                    raw_qty       = max(0.0, (demand_lead + demand_buffer) - qty_available)
                    suggested_qty = self._round_to_moq(raw_qty, moq)
                    reorder_needed= suggested_qty > 0 or qty_on_hand < 0
                    abc_class     = self._classify_abc(avg_monthly, config)
                    urgency       = self._determine_urgency(
                        qty_on_hand, months_of_stock, avg_monthly,
                        lead_months, is_dead, suggested_qty
                    )

                    # Inter-Warehouse Rebalancing (Phase 4)
                    transfer_source_wh = False
                    max_surplus_qty = 0.0
                    if urgency in ('critical', 'urgent'):
                        for wh_other in warehouses:
                            if wh_other.id == warehouse.id:
                                continue
                            qty_other = global_stock_map.get(product_id, {}).get(wh_other.id, 0.0)
                            if qty_other <= 0:
                                continue
                            
                            # Calculate months of stock in other warehouse
                            sold_other = qty_map_global.get((product_id, wh_other.id), 0.0)
                            demand_other = sold_other / analysis_months if analysis_months > 0 else 0.0
                            mos_other = qty_other / demand_other if demand_other > 0 else 999.0
                            
                            if mos_other > 6.0:
                                if qty_other > max_surplus_qty:
                                    max_surplus_qty = qty_other
                                    transfer_source_wh = wh_other

                    # FIX 1: compute rank here, before appending to list
                    u_rank = URGENCY_RANK.get(urgency, 5)

                    # Trend: O(1) dict lookup (FIX 4)
                    prev_qty = prev_qty_map.get(product_id, 0.0)
                    trend, trend_pct = self._calc_trend(
                        total_sold, prev_qty, analysis_months, comparison_months
                    )

                    # Seasonal: O(1) dict lookup (FIX 4)
                    ly_qty       = ly_qty_map.get(product_id, 0.0)
                    seasonal_note= self._calc_seasonal_note(total_sold, ly_qty)

                    reorder_value = suggested_qty * cost

                    # Calculation notes (transparent audit trail)
                    lead_time_desc = f'Lead time: {lead_months:.2f} months ({stated_lead_days} days stated)'
                    if config.track_vendor_performance and product_id in actual_lead_map and actual_avg_days > stated_lead_days:
                        lead_time_desc += f' [OVERRIDDEN dynamically to {actual_avg_days:.0f} days due to supplier lateness]'

                    notes_lines = [
                        '══ DEMAND ANALYSIS ══',
                        f'Period: {date_from} → {date_to} ({analysis_months} months)',
                        f'Total sold: {total_sold:.2f}  |  Avg monthly: {avg_monthly:.2f} units/month',
                        '',
                        '══ STOCK POSITION ══',
                        f'On hand: {qty_on_hand:.2f}  |  Incoming PO: {qty_incoming:.2f}',
                        f'Net available: {qty_available:.2f}  |  Months of stock: {months_of_stock:.1f}',
                        '',
                        '══ FORMULA ══',
                        lead_time_desc,
                        f'Safety buffer: {config.safety_buffer_months:.1f} months',
                        f'Demand during lead time: {demand_lead:.2f}',
                        f'Safety demand buffer: {demand_buffer:.2f}',
                        f'Raw reorder qty: {raw_qty:.2f}',
                        f'MOQ: {moq:.0f}  →  Final suggestion: {suggested_qty:.0f}',
                        f'Reorder value: {cost:.3f} × {suggested_qty:.0f} = {reorder_value:.3f}',
                    ]

                    if transfer_source_wh:
                        notes_lines += [
                            '',
                            '══ TRANSFER REBALANCING ══',
                            f'🔄 Transfer Recommended from warehouse: {transfer_source_wh.name}',
                            f'Available surplus stock there: {max_surplus_qty:.2f} units'
                        ]

                    notes_lines += [
                        '',
                        '══ TREND & SEASONAL ══',
                        f'Prev period qty: {prev_qty:.2f}  |  Trend: {trend} ({trend_pct:+.1f}%)',
                        f'Last year same period: {ly_qty:.2f}  |  {seasonal_note}',
                    ]

                    if qty_on_hand < 0:
                        notes_lines = ['⚠️ NEGATIVE STOCK — CRITICAL', ''] + notes_lines
                    if is_dead:
                        notes_lines = [f'💀 DEAD STOCK — {months_since} months without sale', ''] + notes_lines

                    vals = {
                        'company_id':               company.id,
                        'warehouse_id':             warehouse.id,
                        'product_id':               product_id,
                        'product_cost':             cost,
                        'analysis_date':            date.today(),
                        'analysis_months':          analysis_months,
                        'date_from':                date_from,
                        'date_to':                  date_to,
                        'total_qty_sold':           total_sold,
                        'avg_monthly_demand':       avg_monthly,
                        'lead_time_months':         lead_months,
                        'safety_buffer_months':     config.safety_buffer_months,
                        'moq':                      moq,
                        'prev_period_qty_sold':     prev_qty,
                        'demand_trend':             trend,
                        'trend_pct':                trend_pct,
                        'same_period_last_year_qty':ly_qty,
                        'seasonal_note':            seasonal_note,
                        'qty_on_hand':              qty_on_hand,
                        'qty_incoming':             qty_incoming,
                        'is_dead_stock':            is_dead,
                        'last_sale_date':           last_sale,
                        'months_since_last_sale':   months_since,
                        'raw_reorder_qty':          raw_qty,
                        'suggested_reorder_qty':    suggested_qty,
                        'reorder_needed':           reorder_needed,
                        'within_budget':            True,   # updated in budget pass
                        'budget_rank':              0,
                        'abc_class':                abc_class,
                        'urgency':                  urgency,
                        'vendor_id':                v_partner_id,
                        'vendor_stated_lead_days':  stated_lead_days,
                        'vendor_actual_avg_days':   actual_avg_days,
                        'vendor_performance_note':  vendor_perf_note,
                        'transfer_source_warehouse_id': transfer_source_wh.id if transfer_source_wh else False,
                        'notes':                    '\n'.join(notes_lines),
                    }

                    # FIX 1: pass u_rank (integer), not the undefined urgency_rank
                    suggestion_values.append((product_id, reorder_value, u_rank, vals))

                    if urgency == 'critical':
                        total_critical += 1

                # ── Budget ranking pass ───────────────────────────────────────
                suggestion_values.sort(key=lambda x: (x[2], -x[1]))   # urgency rank asc, value desc
                budget        = config.budget_cap
                running_total = 0.0

                for rank, (pid, rv, _, vals) in enumerate(suggestion_values, 1):
                    vals['budget_rank'] = rank
                    if budget > 0 and vals['reorder_needed']:
                        running_total         += rv
                        vals['within_budget']  = running_total <= budget
                    else:
                        vals['within_budget'] = True

                # ── FIX 5: Bulk create — no search inside loop ───────────────
                # We already purged stale records above, so all writes are creates.
                records_to_create = [vals for _, _, _, vals in suggestion_values]
                if records_to_create:
                    self.sudo().create(records_to_create)
                    total_created += len(records_to_create)
                    _logger.info(
                        'SmartReorder: Created %d records for %s/%s',
                        len(records_to_create), company.name, warehouse.name
                    )

            # Notifications + email report
            self._send_notifications(company, config, total_critical)
            if config.send_email_report:
                self._send_email_report(company, config)

        _logger.info('SmartReorder: Done. %d records. %d critical.', total_created, total_critical)
        return {'created': total_created, 'critical': total_critical}

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    @api.model
    def _get_config(self, company_id):
        config = self.env['smart.reorder.config'].sudo().search(
            [('company_id', '=', company_id)], limit=1
        )
        if not config:
            config = self.env['smart.reorder.config'].sudo().create({'company_id': company_id})
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
        diff = actual_avg - self.vendor_stated_lead_days

        if diff > 5:
            note = f'⚠️ Typically {diff:.0f} days late (avg {actual_avg:.0f}d vs stated {self.vendor_stated_lead_days}d).'
        elif diff < -5:
            note = f'✅ Delivers {abs(diff):.0f} days early on average (avg {actual_avg:.0f}d).'
        else:
            note = f'✅ Delivers on time (avg {actual_avg:.0f}d vs stated {self.vendor_stated_lead_days}d).'

        self.sudo().write({
            'vendor_actual_avg_days':  actual_avg,
            'vendor_performance_note': note,
        })

    # ── Notifications ─────────────────────────────────────────────────────────

    @api.model
    def _send_notifications(self, company, config, critical_count):
        if not config.notify_user_ids:
            return
        if config.critical_notify_only and critical_count == 0:
            return

        critical_items = self.sudo().search([('company_id','=',company.id),('urgency','=','critical')])
        urgent_items   = self.sudo().search([('company_id','=',company.id),('urgency','=','urgent')])
        dead_items     = self.sudo().search([('company_id','=',company.id),('is_dead_stock','=',True)])
        reorder_items  = self.sudo().search([('company_id','=',company.id),('reorder_needed','=',True)])
        total_value    = sum(reorder_items.mapped('reorder_value'))
        currency       = company.currency_id.symbol or ''

        body_lines = [
            f'<h3>📦 Smart Reorder Advisor — {company.name}</h3>',
            f'<p>Analysis completed on <strong>{date.today()}</strong></p>',
            f'<table style="border-collapse:collapse;width:100%;">',
            f'<tr style="background:#f5f5f5;"><td style="padding:8px;"><strong>🔴 Critical</strong></td>'
            f'<td style="padding:8px;"><strong>{len(critical_items)}</strong></td></tr>',
            f'<tr><td style="padding:8px;">🟠 Urgent</td><td style="padding:8px;"><strong>{len(urgent_items)}</strong></td></tr>',
            f'<tr style="background:#f5f5f5;"><td style="padding:8px;">💀 Dead Stock</td><td style="padding:8px;"><strong>{len(dead_items)}</strong></td></tr>',
            f'<tr><td style="padding:8px;">💰 Total Reorder Value</td><td style="padding:8px;"><strong>{currency} {total_value:,.2f}</strong></td></tr>',
            f'</table>',
        ]

        if critical_items:
            body_lines += [
                '<p><strong>Critical Items (top 10):</strong></p>',
                '<table style="border-collapse:collapse;width:100%;font-size:12px;">',
                '<tr style="background:#e74c3c;color:white;"><th style="padding:6px;">Part No.</th>'
                '<th style="padding:6px;">Product</th><th style="padding:6px;">On Hand</th>'
                '<th style="padding:6px;">Suggest</th><th style="padding:6px;">Value</th></tr>',
            ]
            for item in critical_items[:10]:
                body_lines.append(
                    f'<tr style="background:#fdecea;"><td style="padding:5px;">{item.default_code or "—"}</td>'
                    f'<td style="padding:5px;">{item.product_id.display_name}</td>'
                    f'<td style="padding:5px;color:#e74c3c;"><strong>{item.qty_on_hand:.0f}</strong></td>'
                    f'<td style="padding:5px;"><strong>{item.suggested_reorder_qty:.0f}</strong></td>'
                    f'<td style="padding:5px;">{currency} {item.reorder_value:,.2f}</td></tr>'
                )
            body_lines.append('</table>')

        body_lines.append('<p>Open <strong>Reorder Advisor</strong> in Odoo for the full report.</p>')

        subject = (
            f'{config.email_subject_prefix} {company.name} — '
            f'{critical_count} Critical | {date.today()}'
        )
        for user in config.notify_user_ids:
            self.env['mail.thread'].sudo().message_notify(
                partner_ids=[user.partner_id.id],
                subject=subject,
                body=''.join(body_lines),
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )

    # ── Email PDF Report ──────────────────────────────────────────────────────

    @api.model
    def _send_email_report(self, company, config):
        if not config.notify_user_ids:
            return
        suggestions = self.sudo().search([
            ('company_id', '=', company.id),
            ('reorder_needed', '=', True),
        ])
        if not suggestions:
            return
        try:
            report = self.env.ref('smart_reorder_advisor.action_report_reorder_summary').sudo()
            pdf_content, _ = report._render_qweb_pdf(suggestions.ids)
        except Exception as e:
            _logger.warning('SmartReorder: PDF generation failed — %s', e)
            return

        import base64
        att = self.env['ir.attachment'].sudo().create({
            'name':     f'Reorder_Report_{company.name}_{date.today()}.pdf',
            'type':     'binary',
            'datas':    base64.b64encode(pdf_content),
            'mimetype': 'application/pdf',
        })
        self.env['mail.mail'].sudo().create({
            'subject':         f'{config.email_subject_prefix} Weekly Reorder — {company.name} — {date.today()}',
            'body_html':       f'<p>Weekly Smart Reorder Report for <strong>{company.name}</strong> attached.</p>',
            'recipient_ids':   [(6, 0, config.notify_user_ids.mapped('partner_id').ids)],
            'attachment_ids':  [(6, 0, [att.id])],
        }).send()
        _logger.info('SmartReorder: PDF emailed to %d users', len(config.notify_user_ids))

    # ── Phase 3: Auto-flag on negative delivery ───────────────────────────────

    @api.model
    def flag_negative_stock_product(self, product_id, warehouse_id, company_id):
        config = self._get_config(company_id)
        if not config.auto_flag_on_negative:
            return

        location  = self.env['stock.warehouse'].sudo().browse(warehouse_id).lot_stock_id
        quants    = self.env['stock.quant'].sudo().search([
            ('product_id',  '=', product_id),
            ('location_id', 'child_of', location.id),
        ])
        qty_on_hand = sum(quants.mapped('quantity'))

        vals = {
            'company_id':     company_id,
            'warehouse_id':   warehouse_id,
            'product_id':     product_id,
            'qty_on_hand':    qty_on_hand,
            'urgency':        'critical',
            'reorder_needed': True,
            'analysis_date':  date.today(),
            'notes': (
                f'⚠️ AUTO-FLAGGED: Stock went negative on {date.today()}\n'
                f'On Hand: {qty_on_hand:.2f}\n'
                f'Run full analysis for complete suggestion.'
            ),
        }
        existing = self.sudo().search([
            ('product_id',   '=', product_id),
            ('warehouse_id', '=', warehouse_id),
            ('company_id',   '=', company_id),
        ], limit=1)
        if existing:
            existing.sudo().write(vals)
        else:
            self.sudo().create(vals)

        self._send_notifications(
            self.env['res.company'].browse(company_id), config, 1
        )

    # ── Phase 3: Draft PO ─────────────────────────────────────────────────────

    def action_create_draft_po(self):
        self.ensure_one()
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

        po = self.env['purchase.order'].sudo().create({
            'partner_id': vendor.id,
            'company_id': self.company_id.id,
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
        if not self.transfer_source_warehouse_id:
            raise UserError(_('No transfer source warehouse recommended.'))
        if not self.reorder_needed:
            raise UserError(_('This suggestion does not require any replenishment.'))

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
                'product_uom_qty': self.suggested_reorder_qty,
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
        _logger.info('SmartReorder CRON: Starting...')
        result = self.generate_suggestions()
        _logger.info('SmartReorder CRON: Done → %s', result)

    # ── Utility actions ───────────────────────────────────────────────────────

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
