from odoo import models, fields, api, _, Command
from odoo.exceptions import UserError
from datetime import date

from ..utils.access import require_group

_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'


class SmartReorderPoWizard(models.TransientModel):
    _name = 'smart.reorder.po.wizard'
    _description = 'Generate Consolidated POs'

    # Task 8: make the Default Vendor fallback visible BEFORE confirming,
    # instead of silently applying it inside action_confirm_consolidation —
    # this is the deliberate place for that fallback to happen (batch flow
    # with a confirmation step already), it just needs to say so up front.
    fallback_vendor_count = fields.Integer(
        string='Items With No Vendor of Their Own',
        compute='_compute_fallback_vendor_info',
        help='Of the selected suggestions, how many have no vendor assigned '
             "and would use each company's configured Default Vendor instead."
    )
    fallback_vendor_names = fields.Char(
        string='Default Vendor(s) That Would Be Used',
        compute='_compute_fallback_vendor_info',
    )

    @api.depends_context('active_ids')
    def _compute_fallback_vendor_info(self):
        active_ids = self.env.context.get('active_ids') or []
        no_vendor_suggestions = self.env['smart.reorder.suggestion'].browse(active_ids).filtered(
            lambda s: s.reorder_needed and not s.vendor_id
        )
        for wiz in self:
            wiz.fallback_vendor_count = len(no_vendor_suggestions)
            if no_vendor_suggestions:
                configs = self.env['smart.reorder.config'].sudo().search([
                    ('company_id', 'in', no_vendor_suggestions.mapped('company_id.id')),
                ])
                names = sorted({
                    c.default_vendor_id.name for c in configs if c.default_vendor_id
                })
                wiz.fallback_vendor_names = ', '.join(names) if names else False
            else:
                wiz.fallback_vendor_names = False

    @require_group(_MANAGER_GROUP)
    def action_confirm_consolidation(self):
        active_ids = self.env.context.get('active_ids')
        if not active_ids:
            return {'type': 'ir.actions.act_window_close'}

        suggestions = self.env['smart.reorder.suggestion'].browse(active_ids)

        company_ids = suggestions.mapped('company_id.id')
        configs = self.env['smart.reorder.config'].sudo().search([('company_id', 'in', company_ids)])
        config_by_company = {c.company_id.id: c for c in configs}

        # Group and Construct
        grouped_suggestions = {}
        for rec in suggestions:
            config = config_by_company.get(rec.company_id.id)
            if not config:
                config = rec._get_config(rec.company_id.id)
            if not config.allow_draft_po:
                raise UserError(_('Draft PO creation is disabled for company %s. Enable in Configuration → Company Settings.') % rec.company_id.name)
            if not rec.reorder_needed:
                continue
            
            vendor = rec.vendor_id or config.default_vendor_id
            if not vendor:
                raise UserError(_(
                    'No vendor for %s. Set a vendor on the product or configure a Default Vendor in settings.'
                ) % rec.product_id.display_name)
            
            key = (vendor.id, rec.company_id.id, rec.warehouse_id.id)
            grouped_suggestions.setdefault(key, self.env['smart.reorder.suggestion'])
            grouped_suggestions[key] |= rec

        if not grouped_suggestions:
            raise UserError(_('No suggestions requiring reordering were selected.'))

        # Cross-company guard: the user must belong to every company they are
        # creating POs for, even though the create() call uses sudo().
        allowed_company_ids = self.env.user.company_ids.ids
        forbidden = [
            cid for (_vendor_id, cid, _warehouse_id) in grouped_suggestions
            if cid not in allowed_company_ids
        ]
        if forbidden:
            forbidden_names = self.env['res.company'].sudo().browse(forbidden).mapped('name')
            raise UserError(_(
                'You do not have access to the following companies and cannot '
                'create purchase orders for them: %s'
            ) % ', '.join(forbidden_names))

        created_pos = self.env['purchase.order']
        for (vendor_id, company_id, warehouse_id), recs in grouped_suggestions.items():
            po_lines = []
            vendor = self.env['res.partner'].browse(vendor_id)
            company = self.env['res.company'].browse(company_id)
            po_currency = vendor.property_purchase_currency_id or company.currency_id
            for rec in recs:
                seller = rec.product_id._select_seller(partner_id=vendor, quantity=rec.suggested_reorder_qty)
                po_price = seller.price if seller else rec.product_cost
                price_currency = seller.currency_id if (seller and seller.currency_id) else company.currency_id
                if price_currency != po_currency:
                    po_price = price_currency._convert(
                        po_price, po_currency, company, date.today()
                    )

                po_lines.append(Command.create({
                    'product_id': rec.product_id.id,
                    'product_qty': rec.suggested_reorder_qty,
                    'price_unit': po_price,
                    'name': (
                        f'{rec.product_id.display_name} — Reorder Advisor '
                        f'{date.today()} (avg {rec.avg_monthly_demand:.1f}/month)'
                    ),
                }))
            
            warehouse = self.env['stock.warehouse'].sudo().browse(warehouse_id)
            picking_type = self.env['stock.picking.type'].sudo().search([
                ('code', '=', 'incoming'),
                ('warehouse_id', '=', warehouse_id),
            ], limit=1)
            if not picking_type:
                raise UserError(_('No incoming operation type found for warehouse %s.') % warehouse.name)

            po = self.env['purchase.order'].sudo().create({
                'partner_id': vendor_id,
                'company_id': company_id,
                'picking_type_id': picking_type.id,
                'order_line': po_lines,
                'notes': (
                    f'Auto-generated by Smart Reorder Advisor (Consolidated)\n'
                    f'Analysis Date: {date.today()}'
                ),
            })
            created_pos |= po

            recs.sudo().write({
                'po_ids': [Command.link(po.id)],
                'reorder_needed': False,
            })

        if len(created_pos) == 1:
            return {
                'type': 'ir.actions.act_window',
                'name': 'Draft Purchase Order',
                'res_model': 'purchase.order',
                'res_id': created_pos.id,
                'view_mode': 'form',
                'target': 'current',
            }
        else:
            return {
                'type': 'ir.actions.act_window',
                'name': 'Draft Purchase Orders',
                'res_model': 'purchase.order',
                'domain': [('id', 'in', created_pos.ids)],
                'view_mode': 'list,form',
                'target': 'current',
            }
