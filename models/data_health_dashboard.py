from odoo import models, fields, api


class SmartReorderDataHealthDashboard(models.TransientModel):
    """Recommendation: nothing in the module surfaces product master-data gaps
    that silently degrade forecast quality — a product with no vendor, no
    real cost, or no part number doesn't error, it just quietly produces a
    weaker suggestion. This is a lightweight, on-demand snapshot (same
    TransientModel pattern as the Observability Dashboard) rather than a live
    OWL widget, since it's a periodic setup check, not something anyone
    watches continuously."""
    _name = 'smart.reorder.data.health.dashboard'
    _description = 'Reorder Data Quality / Setup Health'

    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)

    total_active_products = fields.Integer(string='Products in Scope', compute='_compute_stats')
    missing_cost_count = fields.Integer(string='Missing Standard Cost', compute='_compute_stats')
    missing_vendor_count = fields.Integer(string='No Vendor At All', compute='_compute_stats')
    temp_vendor_only_count = fields.Integer(string='Only a Placeholder Vendor', compute='_compute_stats')
    missing_part_number_count = fields.Integer(string='Missing Part Number', compute='_compute_stats')

    def _base_domain(self):
        return [
            ('active', '=', True),
            ('type', '=', 'product'),
            ('exclude_from_reorder_advisor', '=', False),
        ]

    def _compute_stats(self):
        Template = self.env['product.template'].sudo()
        Supplierinfo = self.env['product.supplierinfo'].sudo()
        Config = self.env['smart.reorder.config'].sudo()
        for rec in self:
            base = rec._base_domain()
            rec.total_active_products = Template.search_count(base)
            rec.missing_cost_count = Template.search_count(base + [('standard_price', '<=', 0)])
            rec.missing_part_number_count = Template.search_count(
                base + ['|', ('default_code', '=', False), ('default_code', '=', '')]
            )

            config = Config.search([('company_id', '=', rec.company_id.id)], limit=1)
            temp_vendor_ids = config.temp_vendor_ids.ids if config else []

            tmpl_ids = Template.search(base).ids
            if tmpl_ids:
                suppliers = Supplierinfo.search([('product_tmpl_id', 'in', tmpl_ids)])
                tmpl_with_supplier = set(suppliers.mapped('product_tmpl_id.id'))
                tmpl_with_real_supplier = {
                    s.product_tmpl_id.id for s in suppliers if s.partner_id.id not in temp_vendor_ids
                }
                rec.missing_vendor_count = len([t for t in tmpl_ids if t not in tmpl_with_supplier])
                rec.temp_vendor_only_count = len([
                    t for t in tmpl_ids if t in tmpl_with_supplier and t not in tmpl_with_real_supplier
                ])
            else:
                rec.missing_vendor_count = 0
                rec.temp_vendor_only_count = 0

    def _open_filtered_products(self, name, domain):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'product.template',
            'view_mode': 'tree,form',
            'domain': self._base_domain() + domain,
            'context': {'company_id': self.company_id.id},
        }

    def action_view_missing_cost(self):
        return self._open_filtered_products(
            'Products Missing Standard Cost', [('standard_price', '<=', 0)]
        )

    def action_view_missing_part_number(self):
        return self._open_filtered_products(
            'Products Missing Part Number',
            ['|', ('default_code', '=', False), ('default_code', '=', '')],
        )

    def action_view_missing_vendor(self):
        self.ensure_one()
        Template = self.env['product.template'].sudo()
        Supplierinfo = self.env['product.supplierinfo'].sudo()
        tmpl_ids = Template.search(self._base_domain()).ids
        with_supplier = set(Supplierinfo.search(
            [('product_tmpl_id', 'in', tmpl_ids)]
        ).mapped('product_tmpl_id.id'))
        missing_ids = [t for t in tmpl_ids if t not in with_supplier]
        return {
            'type': 'ir.actions.act_window',
            'name': 'Products With No Vendor At All',
            'res_model': 'product.template',
            'view_mode': 'tree,form',
            'domain': [('id', 'in', missing_ids)],
        }

    @api.model
    def action_open_dashboard(self):
        dashboard = self.create({'company_id': self.env.company.id})
        return {
            'name': 'Data Quality / Setup Health',
            'type': 'ir.actions.act_window',
            'res_model': 'smart.reorder.data.health.dashboard',
            'res_id': dashboard.id,
            'view_mode': 'form',
            'target': 'current',
        }
