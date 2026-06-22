from odoo import models, fields

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    exclude_from_reorder_advisor = fields.Boolean(
        string='Exclude from Reorder Advisor',
        default=False,
        help='If checked, this product will be ignored by the Smart Reorder Advisor.'
    )
