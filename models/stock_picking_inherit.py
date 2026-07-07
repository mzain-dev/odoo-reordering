from datetime import date
from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    """
    PHASE 3: Auto-flag when a delivery validation causes negative stock.

    We hook into _action_done() — called when a picking is validated.
    After validation, we check if any product went negative and immediately
    create/update a Critical suggestion without waiting for the Monday cron.
    """
    _inherit = 'stock.picking'

    def _action_done(self):
        """Override to detect negative stock after delivery validation."""
        result = super()._action_done()

        # Only care about outgoing deliveries (customer deliveries)
        for picking in self.filtered(lambda p: p.picking_type_code == 'outgoing'):
            config = self.env['smart.reorder.config'].sudo().search(
                [('company_id', '=', picking.company_id.id)], limit=1
            )
            if not config or not config.auto_flag_on_negative:
                continue

            warehouse = picking.picking_type_id.warehouse_id
            if not warehouse:
                continue

            location = warehouse.lot_stock_id
            product_ids = picking.move_ids.mapped('product_id.id')

            if not product_ids:
                continue

            # Check which products are now negative in this warehouse
            quant_data = self.env['stock.quant'].sudo().read_group(
                domain=[
                    ('product_id', 'in', product_ids),
                    ('location_id', 'child_of', location.id),
                ],
                fields=['product_id', 'quantity:sum'],
                groupby=['product_id'],
            )

            for r in quant_data:
                if r['quantity'] < 0:
                    product_id = r['product_id'][0]
                    try:                  # ← NEW: isolate flagging from delivery
                        _logger.warning(
                            'SmartReorder: Negative stock product %d in %s after %s',
                            product_id, warehouse.name, picking.name
                        )
                        self.env['smart.reorder.suggestion'] \
                            .flag_negative_stock_product(
                            product_id=product_id,
                            warehouse_id=warehouse.id,
                            company_id=picking.company_id.id,
                        )
                    except Exception:  # ← never block delivery
                        _logger.exception(
                            'SmartReorder: Auto-flag FAILED for product %d '
                            '(delivery still completed)', product_id
                        )
                        try:
                            if config:
                                config.sudo().write({'picking_hook_error_count': config.picking_hook_error_count + 1})
                        except Exception:
                            _logger.error('SmartReorder: Failed to increment picking hook error counter.')

        # T-20: Receipt Refresh Hook — lightweight targeted refresh after incoming validation.
        # PERFORMANCE NOTE (Obs 1): We deliberately do NOT call generate_suggestions() here,
        # as that re-analyzes all SKUs in the warehouse (O(all products), not O(received)).
        # Instead we do exactly 2 queries scoped to the received product IDs only, then write
        # the updated stock fields in-place. Receipt confirmation stays fast regardless of
        # warehouse SKU count.
        for picking in self.filtered(lambda p: p.picking_type_code == 'incoming'):
            try:
                warehouse = picking.picking_type_id.warehouse_id
                if not warehouse:
                    continue
                product_ids_received = picking.move_ids.mapped('product_id.id')
                if not product_ids_received:
                    continue

                # Find existing suggestion records for these products in this warehouse
                suggestions = self.env['smart.reorder.suggestion'].sudo().search([
                    ('warehouse_id', '=', warehouse.id),
                    ('company_id',   '=', picking.company_id.id),
                    ('product_id',   'in', product_ids_received),
                ])
                if not suggestions:
                    _logger.info(
                        'SmartReorder: Receipt %s — no existing suggestions to refresh '
                        'for %d product(s) in %s (cron will create them on next run)',
                        picking.name, len(product_ids_received), warehouse.name
                    )
                    continue

                _logger.info(
                    'SmartReorder: Receipt %s — lightweight refresh of %d suggestion(s) in %s',
                    picking.name, len(suggestions), warehouse.name
                )

                # Q1: Updated on-hand qty for these products only (2 queries total)
                location = warehouse.lot_stock_id
                quant_data = self.env['stock.quant'].sudo().read_group(
                    domain=[
                        ('product_id',  'in', product_ids_received),
                        ('location_id', 'child_of', location.id),
                    ],
                    fields=['product_id', 'quantity:sum'],
                    groupby=['product_id'],
                )
                onhand_map = {r['product_id'][0]: r['quantity'] for r in quant_data}

                # Q2: Remaining incoming PO qty (open PO lines, not yet received)
                incoming_data = self.env['purchase.order.line'].sudo().read_group(
                    domain=[
                        ('order_id.state',      'in', ['purchase', 'done']),
                        ('order_id.company_id', '=',  picking.company_id.id),
                        ('order_id.picking_type_id.warehouse_id', '=', warehouse.id),
                        ('product_id',          'in',  product_ids_received),
                    ],
                    fields=['product_id', 'product_qty:sum', 'qty_received:sum'],
                    groupby=['product_id'],
                )
                incoming_map = {
                    r['product_id'][0]: max(0.0, r['product_qty'] - r['qty_received'])
                    for r in incoming_data
                }

                for sug in suggestions:
                    pid            = sug.product_id.id
                    qty_on_hand    = onhand_map.get(pid, sug.qty_on_hand)
                    qty_incoming   = incoming_map.get(pid, sug.qty_incoming)
                    qty_outgoing   = sug.qty_outgoing
                    qty_available  = qty_on_hand + qty_incoming - qty_outgoing
                    avg_monthly    = sug.avg_monthly_demand
                    lead_months    = sug.lead_time_months
                    months_of_stock= (qty_available / avg_monthly
                                      if avg_monthly > 0 else 999.0)

                    # Recompute urgency with updated stock figures using the shared helper
                    urgency = self.env['smart.reorder.suggestion']._determine_urgency(
                        qty_on_hand, months_of_stock, avg_monthly,
                        lead_months, sug.is_dead_stock, sug.suggested_reorder_qty
                    )

                    demand_lead   = avg_monthly * lead_months
                    demand_buffer = avg_monthly * sug.safety_buffer_months
                    raw_qty       = max(0.0, (demand_lead + demand_buffer) - qty_available)
                    reorder_needed = raw_qty > 0 or qty_on_hand < 0

                    sug.sudo().write({
                        'qty_on_hand':    qty_on_hand,
                        'qty_incoming':   qty_incoming,
                        'urgency':        urgency,
                        'reorder_needed': reorder_needed and not sug.is_snoozed,
                        'analysis_date':  date.today(),
                    })

            except Exception:
                _logger.exception(
                    'SmartReorder: Receipt refresh FAILED for %s '
                    '(receipt still completed)', picking.name
                )
                try:
                    config = self.env['smart.reorder.config'].sudo().search(
                        [('company_id', '=', picking.company_id.id)], limit=1
                    )
                    if config:
                        config.sudo().write({'picking_hook_error_count': config.picking_hook_error_count + 1})
                except Exception:
                    _logger.error('SmartReorder: Failed to increment picking hook error counter.')

        return result
