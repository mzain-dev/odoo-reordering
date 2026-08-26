from odoo import models, fields, api, _, Command
from odoo.exceptions import AccessError, UserError
from datetime import date, timedelta
import logging
from ..reorder_engine import calculate_replenishment_levels

_logger = logging.getLogger(__name__)
_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'
_USER_GROUP = 'smart_reorder_advisor.group_smart_reorder_user'


class SmartReorderSuggestion(models.Model):
    _inherit = 'smart.reorder.suggestion'

    def action_refresh_vendor_performance(self):
        """
        Calculates actual vs stated vendor lead time for THIS product only.
        """
        self.ensure_one()
        if not self.vendor_id:
            raise UserError(_('No vendor set for this product.'))

        pos = self.env['purchase.order'].sudo().search([
            ('partner_id', '=', self.vendor_id.id),
            ('state', 'in', ['purchase', 'done']),
            ('date_approve', '!=', False),
            ('effective_date', '!=', False),
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
            po = line.order_id
            if po.date_approve and po.effective_date:
                delta = (po.effective_date.date() - po.date_approve.date()).days
                if delta > 0:
                    total_days += delta
                    count += 1

        if count == 0:
            self.sudo().write({'vendor_performance_note': 'Could not calculate from available data.'})
            return

        actual_avg = round(total_days / count, 1)
        note = self._build_vendor_perf_note(actual_avg, self.vendor_stated_lead_days, with_emoji=True)

        self.sudo().write({
            'vendor_actual_avg_days':  actual_avg,
            'vendor_performance_note': note,
        })

    def action_create_draft_po(self):
        self.ensure_one()
        if not self.env.user.has_group('smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can create Draft POs.'))
        if not self.env.user.has_group('purchase.group_purchase_user'):
            raise AccessError(_('You do not have the required Purchase access rights to create purchase orders.'))
        if self.company_id.id not in self.env.user.company_ids.ids:
            raise UserError(_('You do not have access to company %s and cannot create purchase orders for it.') % self.company_id.name)
        config = self._get_config(self.company_id.id)
        if not config.allow_draft_po:
            raise UserError(_('Draft PO creation is disabled. Enable in Configuration → Company Settings.'))
        if not self.reorder_needed or self.is_snoozed:
            raise UserError(_('This product does not need reordering or is currently snoozed.'))

        # Task 8: a single-click Draft PO must never silently guess a vendor —
        # falling back to the configured Default Vendor without the user
        # noticing risks ordering from the wrong supplier. The bulk "Generate
        # Consolidated POs" wizard is the deliberate place for that fallback,
        # since it already shows a confirmation screen before creating anything.
        if not self.vendor_id:
            if config.default_vendor_id:
                raise UserError(_(
                    'No vendor assigned to %(product)s. A Default Vendor (%(default_vendor)s) is '
                    'configured, but a one-click Draft PO must not silently fall back to it. '
                    'Either set the correct vendor on the product/pricelist, or select this '
                    'suggestion in the list and use "Generate Consolidated POs" instead — it '
                    'shows exactly which items will use the Default Vendor before creating anything.'
                ) % {'product': self.product_id.display_name, 'default_vendor': config.default_vendor_id.name})
            raise UserError(_(
                'No vendor for %s. Set a vendor on the product or configure a Default Vendor in settings.'
            ) % self.product_id.display_name)
        vendor = self.vendor_id

        seller = self.product_id._select_seller(partner_id=vendor, quantity=self.suggested_reorder_qty)
        po_price = seller.price if seller else self.product_cost
        price_currency = seller.currency_id if (seller and seller.currency_id) else self.company_id.currency_id
        po_currency = vendor.property_purchase_currency_id or self.company_id.currency_id
        if price_currency != po_currency:
            po_price = price_currency._convert(
                po_price, po_currency, self.company_id, date.today()
            )

        picking_type = self.env['stock.picking.type'].sudo().search([
            ('code', '=', 'incoming'),
            ('warehouse_id', '=', self.warehouse_id.id),
        ], limit=1)
        if not picking_type:
            raise UserError(_('No incoming operation type found for warehouse %s.') % self.warehouse_id.name)

        po = self.env['purchase.order'].sudo().create({
            'partner_id': vendor.id,
            'company_id': self.company_id.id,
            'picking_type_id': picking_type.id,
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
        if not self.env.user.has_group('smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can create Internal Transfers.'))
        if not self.env.user.has_group('stock.group_stock_user'):
            raise AccessError(_('You do not have the required Inventory access rights to create internal transfers.'))
        if self.company_id.id not in self.env.user.company_ids.ids:
            raise UserError(_('You do not have access to company %s and cannot create internal transfers for it.') % self.company_id.name)
        if not self.transfer_source_warehouse_id:
            raise UserError(_('No transfer source warehouse recommended.'))
        if not self.reorder_needed or self.is_snoozed:
            raise UserError(_('This suggestion does not require any replenishment or is currently snoozed.'))
        transfer_qty = self.transfer_suggested_qty
        if transfer_qty <= 0:
            raise UserError(_(
                'No transferable surplus quantity is recorded for this suggestion. '
                'Re-run the analysis to refresh the transfer recommendation.'
            ))

        picking_type = self.env['stock.picking.type'].sudo().search([
            ('warehouse_id', '=', self.warehouse_id.id),
            ('code', '=', 'internal')
        ], limit=1)
        if not picking_type:
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
                'product_uom_qty': transfer_qty,
                'product_uom': self.product_id.uom_id.id,
                'location_id': self.transfer_source_warehouse_id.lot_stock_id.id,
                'location_dest_id': self.warehouse_id.lot_stock_id.id,
            })]
        }
        picking = self.env['stock.picking'].sudo().create(picking_vals)

        return {
            'type':      'ir.actions.act_window',
            'name':      'Internal Transfer',
            'res_model': 'stock.picking',
            'res_id':    picking.id,
            'view_mode': 'form',
            'target':    'current',
        }

    @api.model
    def get_dashboard_data(self, warehouse_id=None):
        company_ids = self.env.companies.ids
        Suggestion = self
        base_domain = [('company_id', 'in', company_ids)]
        if warehouse_id:
            base_domain.append(('warehouse_id', '=', warehouse_id))

        urgency_counts = {
            g['urgency']: g.get('__count') or g.get('urgency_count') or 0
            for g in Suggestion.read_group(base_domain, ['urgency'], ['urgency'])
            if g['urgency']
        }
        abc_counts = {
            g['abc_class']: g.get('__count') or g.get('abc_class_count') or 0
            for g in Suggestion.read_group(base_domain, ['abc_class'], ['abc_class'])
            if g['abc_class']
        }
        trend_counts = {
            g['demand_trend']: g.get('__count') or g.get('demand_trend_count') or 0
            for g in Suggestion.read_group(base_domain, ['demand_trend'], ['demand_trend'])
            if g['demand_trend']
        }
        dead_cnt = Suggestion.search_count(base_domain + [('is_dead_stock', '=', True)])

        pattern_counts = {
            g['sales_pattern']: g.get('__count') or g.get('sales_pattern_count') or 0
            for g in Suggestion.read_group(base_domain, ['sales_pattern'], ['sales_pattern'])
            if g['sales_pattern']
        }

        target_currency = self.env.company.currency_id

        def _to_target(amount, currency_id):
            if not amount:
                return 0.0
            if not currency_id or currency_id == target_currency.id:
                return amount
            return self.env['res.currency'].sudo().browse(currency_id)._convert(
                amount, target_currency, self.env.company, fields.Date.today()
            )

        budget_groups = Suggestion.read_group(
            base_domain + [('reorder_needed', '=', True)],
            ['estimated_purchase_value:sum'], ['within_budget', 'currency_id'],
            lazy=False,
        )
        total_rv = 0.0
        budget_rv = 0.0
        budget_cnt = 0
        for g in budget_groups:
            cid = g['currency_id'][0] if g.get('currency_id') else False
            amount = _to_target(g.get('estimated_purchase_value') or 0.0, cid)
            total_rv += amount
            if g.get('within_budget'):
                budget_rv += amount
                budget_cnt += g.get('__count') or 0
        currency = target_currency.name

        date_group = Suggestion.read_group(base_domain, ['analysis_date:max'], [])
        last_date = (
            str(date_group[0]['analysis_date'])
            if date_group and date_group[0].get('analysis_date') else None
        )

        top = {
            'critical': Suggestion.search_read(
                base_domain + [('urgency', '=', 'critical')],
                ['default_code', 'product_id', 'qty_on_hand', 'suggested_reorder_qty', 'estimated_purchase_value'],
                order='qty_on_hand asc', limit=10,
            ),
            'urgent': Suggestion.search_read(
                base_domain + [('urgency', '=', 'urgent')],
                ['default_code', 'product_id', 'months_of_stock', 'avg_monthly_demand', 'suggested_reorder_qty'],
                order='months_of_stock asc', limit=10,
            ),
            'dead': Suggestion.search_read(
                base_domain + [('is_dead_stock', '=', True)],
                ['default_code', 'product_id', 'qty_on_hand', 'months_since_last_sale', 'last_sale_date'],
                order='months_since_last_sale desc', limit=10,
            ),
            'rising': Suggestion.search_read(
                base_domain + [('demand_trend', '=', 'up'), ('reorder_needed', '=', True)],
                ['default_code', 'product_id', 'avg_monthly_demand', 'trend_pct', 'estimated_purchase_value'],
                order='trend_pct desc', limit=8,
            ),
            'falling': Suggestion.search_read(
                base_domain + [('demand_trend', '=', 'down')],
                ['default_code', 'product_id', 'avg_monthly_demand', 'trend_pct', 'estimated_purchase_value'],
                order='trend_pct asc', limit=8,
            ),
        }

        Snapshot = self.env['smart.reorder.forecast.snapshot']
        backtest_domain = [('evaluated', '=', True), ('company_id', 'in', company_ids)]
        if warehouse_id:
            backtest_domain.append(('warehouse_id', '=', warehouse_id))

        abc_groups = Snapshot.read_group(
            backtest_domain,
            ['absolute_error_pct:avg'],
            ['abc_class']
        )
        mape_by_abc = {
            (g['abc_class'] or 'Unknown'): round(g['absolute_error_pct'] or 0.0, 1)
            for g in abc_groups
        }

        wh_groups = Snapshot.read_group(
            [('evaluated', '=', True), ('company_id', 'in', company_ids)],
            ['absolute_error_pct:avg'],
            ['warehouse_id']
        )
        mape_by_warehouse = {
            (g['warehouse_id'][1] if g['warehouse_id'] else 'Unknown'): round(g['absolute_error_pct'] or 0.0, 1)
            for g in wh_groups
        }

        overall_group = Snapshot.read_group(
            backtest_domain,
            ['absolute_error_pct:avg'],
            []
        )
        overall_mape = round(overall_group[0]['absolute_error_pct'] or 0.0, 1) if overall_group and overall_group[0].get('absolute_error_pct') else 0.0
        
        backtest_data = {
            'mape_by_abc': mape_by_abc,
            'mape_by_warehouse': mape_by_warehouse,
            'overall_mape': overall_mape,
        }

        warehouses = self.env['stock.warehouse'].sudo().search_read(
            [('company_id', 'in', company_ids)], ['id', 'name', 'company_id'], limit=50)
        return {
            'urgency': urgency_counts, 'abc': abc_counts, 'trend': trend_counts,
            'sales_pattern': pattern_counts,
            'total_reorder_value': total_rv, 'within_budget_value': budget_rv,
            'budget_count': budget_cnt, 'dead_count': dead_cnt,
            'currency': currency, 'last_analysis_date': last_date,
            'top': top, 'warehouses': warehouses,
            'backtest': backtest_data,
        }

    def _check_user_group_and_company_access(self, action_label):
        if not self.env.user.has_group(_USER_GROUP):
            raise AccessError(_(
                '%s requires the Reorder Advisor User group.'
            ) % action_label)
        allowed_company_ids = set(self.env.user.company_ids.ids)
        for rec in self:
            if rec.company_id.id not in allowed_company_ids:
                raise AccessError(_(
                    'You do not have access to company %s.'
                ) % rec.company_id.name)

    def _check_snooze_access(self):
        self._check_user_group_and_company_access(_('Snoozing suggestions'))

    def _check_mark_ordered_access(self):
        self._check_user_group_and_company_access(_('Marking as ordered'))

    def action_snooze(self):
        """Open a wizard-like dialog to let the buyer pick snooze duration and reason."""
        self._check_snooze_access()
        name = _('Snooze Suggestions')
        if len(self) == 1:
            name = f'Snooze — {self.product_id.display_name}'
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'smart.reorder.snooze.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_suggestion_ids': [Command.set(self.ids)],
            },
        }

    def action_snooze_7(self):
        """Snooze this suggestion for 7 days. Records audit note automatically."""
        self.ensure_one()
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until': date.today() + timedelta(days=7),
            'snoozed_note':  f'Snoozed 7 days by {self.env.user.name} on {date.today()}',
            'reorder_needed': False,
        })

    def action_snooze_30(self):
        """Snooze this suggestion for 30 days. Records audit note automatically."""
        self.ensure_one()
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until': date.today() + timedelta(days=30),
            'snoozed_note':  f'Snoozed 30 days by {self.env.user.name} on {date.today()}',
            'reorder_needed': False,
        })

    def action_unsnooze(self):
        """Clear the snooze — the next cron run will re-evaluate this product."""
        self.ensure_one()
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until': False,
            'snoozed_note': False,
            'reorder_needed': self.suggested_reorder_qty > 0 or self.qty_on_hand < 0,
        })

    def _create_mark_ordered_log_entries(self):
        """Recommendation: reconciliation trail — one immutable record per
        Mark as Ordered click, resolved later by _generate_for_company()'s
        existing suppression-consuming step (see suggestion_engine.py)."""
        if not self:
            return
        now = fields.Datetime.now()
        self.env['smart.reorder.mark.ordered.log'].sudo().create([{
            'company_id': rec.company_id.id,
            'warehouse_id': rec.warehouse_id.id,
            'product_id': rec.product_id.id,
            'suggestion_id': rec.id,
            'marked_at': now,
            'marked_by_id': self.env.user.id,
            'suggested_qty_at_mark': rec.suggested_reorder_qty,
        } for rec in self])

    def action_mark_ordered(self):
        """Task 5: the boss already placed this order directly (email/vendor
        site) — no PO, no wizard, just a quick note so it stops nagging until
        someone logs the arrival. No-op fields (analysis numbers etc.) are left
        untouched; only the suppression + audit trail is written here."""
        self.ensure_one()
        self._check_mark_ordered_access()
        self.sudo().write({
            'is_marked_ordered': True,
            'marked_ordered_at': fields.Datetime.now(),
            'marked_ordered_by_id': self.env.user.id,
            'reorder_needed': False,
        })
        self._create_mark_ordered_log_entries()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title':   _('Marked as Ordered'),
                'message': _('This will stay off the next report. Remember to log the '
                              'purchase in Odoo once it arrives.'),
                'type':    'success',
                'sticky':  False,
            },
        }

    def action_unmark_ordered(self):
        """Undo a Mark as Ordered click made by mistake — the next analysis
        run will re-evaluate this product normally."""
        self.ensure_one()
        self._check_mark_ordered_access()
        self.sudo().write({
            'is_marked_ordered': False,
            'marked_ordered_at': False,
            'marked_ordered_by_id': False,
            'marked_ordered_confirmer_name': False,
            'reorder_needed': self.suggested_reorder_qty > 0 or self.qty_on_hand < 0,
        })

    def action_bulk_snooze_7(self):
        self._bulk_snooze(7)

    def action_bulk_snooze_30(self):
        self._bulk_snooze(30)

    def _bulk_snooze(self, days):
        if not self:
            return
        self._check_snooze_access()
        self.sudo().write({
            'snoozed_until':  date.today() + timedelta(days=days),
            'snoozed_note':   f'Bulk-snoozed {days} days by {self.env.user.name} on '
                               f'{date.today()} ({len(self)} suggestions).',
            'reorder_needed': False,
        })

    def action_bulk_mark_ordered(self):
        """List-view bulk version of action_mark_ordered (Task 5) — for when
        several items were ordered together in one call/order to a vendor."""
        if not self:
            return
        self._check_mark_ordered_access()
        self.sudo().write({
            'is_marked_ordered': True,
            'marked_ordered_at': fields.Datetime.now(),
            'marked_ordered_by_id': self.env.user.id,
            'reorder_needed': False,
        })
        self._create_mark_ordered_log_entries()

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

    def action_mark_one_time_order(self):
        self.ensure_one()
        if not self.env.user.has_group('smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can change how this part is ordered.'))

        self.product_id.product_tmpl_id.reorder_behavior = 'against_order'
        self.message_post(body=_("Marked as 'Order Only Against Customer Order'. Suggestion suppressed."))

        urgency = self._determine_urgency(
            self.qty_on_hand, self.months_of_stock, self.avg_monthly_demand,
            self.lead_time_months, self.is_dead_stock, 0.0,
        )
        self.write({
            'raw_reorder_qty':       0.0,
            'suggested_reorder_qty': 0.0,
            'reorder_needed':        self.qty_on_hand < 0,
            'urgency':               urgency,
            'needs_review':          False,
            'needs_review_reason':   _(
                'Cleared: marked as one-time order by %(user)s on %(date)s.'
            ) % {'user': self.env.user.name, 'date': date.today()},
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title':   _('Suggestion Updated'),
                'message': _('Reorder quantity suppressed for this part.'),
                'type':    'success',
                'sticky':  False,
            },
        }

    def action_mark_regular_order(self):
        self.ensure_one()
        if not self.env.user.has_group('smart_reorder_advisor.group_smart_reorder_manager'):
            raise UserError(_('Only Reorder Managers can change how this part is ordered.'))

        self.product_id.product_tmpl_id.reorder_behavior = 'bulk_regular'
        self.message_post(body=_(
            "Marked as 'Customer Buys in Bulk Regularly'. Forecast recalculated using "
            "the plain monthly average (outlier rejection bypassed)."
        ))

        avg_monthly = (self.total_qty_sold / self.analysis_months) if self.analysis_months else 0.0
        levels = calculate_replenishment_levels(
            avg_monthly, self.lead_time_months, self.safety_buffer_months,
            self.order_cycle_months, self.qty_available, self.qty_on_hand, self.moq
        )
        min_level = levels['min_stock_level']
        max_level = levels['max_stock_level']
        raw_qty = levels['raw_reorder_qty']
        suggested_qty = levels['suggested_reorder_qty']
        months_of_stock = self._calc_months_of_stock(self.qty_available, avg_monthly)
        urgency = self._determine_urgency(
            self.qty_on_hand, months_of_stock, avg_monthly,
            self.lead_time_months, self.is_dead_stock, suggested_qty,
        )

        self.write({
            'avg_monthly_demand':      avg_monthly,
            'excluded_outlier_months': 0,
            'demand_forecast_note':    _(
                'Plain monthly average — outlier rejection bypassed '
                '(Customer Buys in Bulk Regularly).'
            ),
            'min_stock_level':      min_level,
            'max_stock_level':      max_level,
            'raw_reorder_qty':      raw_qty,
            'suggested_reorder_qty': suggested_qty,
            'reorder_needed':       suggested_qty > 0 or self.qty_on_hand < 0,
            'urgency':              urgency,
            'sales_pattern':        'regular',
            'needs_review':         False,
            'needs_review_reason':  _(
                'Cleared: marked as regular bulk order by %(user)s on %(date)s.'
            ) % {'user': self.env.user.name, 'date': date.today()},
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title':   _('Forecast Updated'),
                'message': _('Recalculated using the plain monthly average. Suggested reorder qty: %s.') % suggested_qty,
                'type':    'success',
                'sticky':  False,
            },
        }
