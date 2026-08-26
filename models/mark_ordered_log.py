from odoo import models, fields


class SmartReorderMarkOrderedLog(models.Model):
    """Recommendation: Mark as Ordered had no reconciliation trail — once the
    one-cycle suppression (Task 5) is consumed by the next analysis run, there
    was no way to later ask "of everything marked ordered last month, how much
    actually got resolved vs. quietly resurfaced because nobody followed up?"
    One immutable record per Mark as Ordered click; resolved by
    _generate_for_company()'s existing suppression-consuming step (see
    models/suggestion/suggestion_engine.py) at the moment it decides whether
    the item would still be needed.

    Denormalizes product/warehouse/company onto the log itself (not just a
    link to the suggestion) so the record stays meaningful even if the
    suggestion it came from is later archived or its id reused after a
    product/warehouse change — an audit trail that goes blank the moment its
    subject changes underneath it isn't much of an audit trail.
    """
    _name = 'smart.reorder.mark.ordered.log'
    _description = 'Mark as Ordered — Reconciliation Log'
    _order = 'marked_at desc'

    company_id = fields.Many2one('res.company', string='Company', required=True, index=True)
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', required=True)
    product_id = fields.Many2one('product.product', string='Product', required=True, index=True)
    suggestion_id = fields.Many2one(
        'smart.reorder.suggestion', string='Suggestion', ondelete='set null',
        help='Best-effort link to the originating suggestion — may go blank if '
             'that record is later deleted, without invalidating this log entry.'
    )

    marked_at = fields.Datetime(string='Marked Ordered At', required=True, index=True)
    marked_by_id = fields.Many2one('res.users', string='Marked By')
    confirmer_name = fields.Char(string='Confirmed By (Name)')
    suggested_qty_at_mark = fields.Float(string='Suggested Qty at Time of Marking')

    resolved_at = fields.Datetime(
        string='Resolved At',
        help='When the one-cycle suppression was consumed by the next analysis run.'
    )
    outcome = fields.Selection([
        ('pending', 'Pending — Next Run Not Yet Processed'),
        ('resurfaced', 'Resurfaced — Still Needed After Suppression'),
        ('cleared', 'Cleared — No Longer Needed After Suppression'),
    ], string='Outcome', default='pending', required=True, index=True)
