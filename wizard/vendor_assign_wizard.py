from odoo import models, fields, api, _
from odoo.exceptions import UserError

from ..utils.access import require_group

_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'


class SmartReorderVendorAssignWizard(models.TransientModel):
    """Recommendation: bulk triage tool for the "needs vendor assignment"
    backlog (no vendor, or only a Temporary/Placeholder Vendor). Writing
    vendor_id directly on the suggestion would only last until the next
    analysis run recomputes it from the product's real supplier data — so
    this creates/updates an actual product.supplierinfo record instead,
    which is the durable fix, then refreshes the suggestion for instant
    feedback."""
    _name = 'smart.reorder.vendor.assign.wizard'
    _description = 'Bulk Assign Vendor'

    vendor_id = fields.Many2one(
        'res.partner', string='Vendor', required=True,
        help='Set as the primary supplier (sequence 0) on each selected '
             "product's template. This is a real vendor assignment, not a "
             'cosmetic change to the suggestion.'
    )
    suggestion_count = fields.Integer(string='Suggestions Selected', readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'suggestion_count' in fields_list:
            active_ids = self.env.context.get('active_ids') or []
            res['suggestion_count'] = len(active_ids)
        return res

    @require_group(_MANAGER_GROUP)
    def action_assign_vendor(self):
        self.ensure_one()
        active_ids = self.env.context.get('active_ids')
        if not active_ids:
            raise UserError(_('No suggestions selected.'))

        suggestions = self.env['smart.reorder.suggestion'].browse(active_ids)
        Supplierinfo = self.env['product.supplierinfo'].sudo()
        updated_templates = self.env['product.template']

        for suggestion in suggestions:
            tmpl = suggestion.product_id.product_tmpl_id
            if tmpl in updated_templates:
                continue
            existing = Supplierinfo.search([
                ('product_tmpl_id', '=', tmpl.id),
                ('partner_id', '=', self.vendor_id.id),
            ], limit=1)
            if existing:
                existing.sequence = 0
            else:
                Supplierinfo.create({
                    'product_tmpl_id': tmpl.id,
                    'partner_id': self.vendor_id.id,
                    'sequence': 0,
                })
            updated_templates |= tmpl

        # Instant feedback — the next analysis run will confirm this from the
        # product's real supplier data, but there's no reason to make the
        # user wait a week to see it reflected.
        suggestions.sudo().write({
            'vendor_id': self.vendor_id.id,
            'needs_vendor_assignment': False,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Vendor Assigned'),
                'message': _('%(vendor)s set as primary supplier for %(count)d product(s).') % {
                    'vendor': self.vendor_id.name,
                    'count': len(updated_templates),
                },
                'type': 'success',
                'sticky': False,
            },
        }
