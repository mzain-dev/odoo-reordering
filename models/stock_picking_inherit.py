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
                    _logger.warning(
                        'SmartReorder: Negative stock detected for product %d '
                        'in warehouse %s after picking %s. Auto-flagging.',
                        product_id, warehouse.name, picking.name
                    )
                    self.env['smart.reorder.suggestion'].flag_negative_stock_product(
                        product_id=product_id,
                        warehouse_id=warehouse.id,
                        company_id=picking.company_id.id,
                    )

        return result
