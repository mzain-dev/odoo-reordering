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
        counts = {}
        if self.ids:
            self.env.cr.execute("""
                SELECT superseded_by_id, COUNT(*)
                FROM product_template
                WHERE superseded_by_id = ANY(%s)
                GROUP BY superseded_by_id
            """, [self.ids])
            counts = dict(self.env.cr.fetchall())
        for rec in self:
            rec.predecessor_count = counts.get(rec.id, 0)

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

    reorder_suggestion_count = fields.Integer(
        string='Reorder Suggestion Count',
        compute='_compute_reorder_suggestion_count'
    )

    def _compute_reorder_suggestion_count(self):
        counts = {}
        if self.ids:
            self.env.cr.execute("""
                SELECT pt.id, COUNT(s.id)
                FROM smart_reorder_suggestion s
                JOIN product_product pp ON pp.id = s.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                WHERE s.active = true
                  AND s.company_id = ANY(%s)
                  AND pt.id = ANY(%s)
                GROUP BY pt.id
            """, [list(self.env.companies.ids), list(self.ids)])
            counts = dict(self.env.cr.fetchall())
        for rec in self:
            rec.reorder_suggestion_count = counts.get(rec.id, 0)

    def action_view_reorder_suggestions(self):
        self.ensure_one()
        return {
            'name': _('Reorder Suggestions'),
            'type': 'ir.actions.act_window',
            'res_model': 'smart.reorder.suggestion',
            'view_mode': 'tree,form',
            'domain': [('product_id.product_tmpl_id', '=', self.id)],
            'target': 'current',
        }


class ProductProduct(models.Model):
    """Odoo 17 reuses product.template's button-box layout (added via
    stock.view_template_property_form) for the Product Variant form too, so
    these stat buttons render there as well — but a type="object" button call
    resolves against the record's actual model (product.product), and _inherits
    delegation only proxies fields, never action methods. Without these, clicking
    either button from a Product Variant record raises AttributeError. Delegating
    to the template keeps the count shown and the list opened consistent (the
    stat button's count field is template-wide either way, via delegation)."""
    _inherit = 'product.product'

    def action_view_predecessors(self):
        return self.product_tmpl_id.action_view_predecessors()

    def action_view_reorder_suggestions(self):
        return self.product_tmpl_id.action_view_reorder_suggestions()
