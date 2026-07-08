from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    exclude_from_reorder_advisor = fields.Boolean(
        string='Exclude from Reorder Advisor',
        default=False,
        help='If checked, this product will be ignored by the Smart Reorder Advisor.'
    )

    reorder_behavior = fields.Selection([
        ('system', 'Let the System Decide'),
        ('against_order', 'Order Only Against Customer Order'),
        ('bulk_regular', 'Customer Buys in Bulk Regularly'),
    ], string='How to Order This Part', default='system', required=True,
       help='Determines how the forecasting engine should treat this part.')

    superseded_by_id = fields.Many2one(
        'product.template',
        string='Superseded By',
        help='The new product template that replaces this product.'
    )

    @api.constrains('superseded_by_id')
    def _check_superseded_by_id_cycle(self):
        for rec in self:
            visited = set()
            current = rec
            while current.superseded_by_id:
                if current.superseded_by_id.id in visited:
                    raise ValidationError(_('Circular reference detected in product supersession chain.'))
                if current.superseded_by_id == rec:
                    raise ValidationError(_('Circular reference detected in product supersession chain.'))
                visited.add(current.superseded_by_id.id)
                current = current.superseded_by_id

    predecessor_count = fields.Integer(
        string='Predecessor Count',
        compute='_compute_predecessor_count'
    )

    def _compute_predecessor_count(self):
        for rec in self:
            rec.predecessor_count = self.search_count([('superseded_by_id', '=', rec.id)])

    def action_view_predecessors(self):
        self.ensure_one()
        predecessors = self.search([('superseded_by_id', '=', self.id)])
        return {
            'name': 'Predecessor Parts',
            'type': 'ir.actions.act_window',
            'res_model': 'product.template',
            'view_mode': 'tree,form',
            'domain': [('id', 'in', predecessors.ids)],
            'target': 'current',
        }
