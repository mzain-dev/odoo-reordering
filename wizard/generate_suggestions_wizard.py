from odoo import models, fields, api, _
from odoo.exceptions import UserError

from ..utils.access import require_group

_MANAGER_GROUP = 'smart_reorder_advisor.group_smart_reorder_manager'


class GenerateSuggestionsWizard(models.TransientModel):
    """
    Pop-up wizard that lets the purchasing manager manually trigger
    the reorder analysis for selected companies and warehouses.

    Use this when:
    - You want to refresh suggestions before the weekly cron runs
    - You want to analyse only one branch/warehouse
    - You just received a big shipment and want updated figures
    """
    _name = 'smart.reorder.wizard'
    _description = 'Generate Smart Reorder Suggestions'

    scope = fields.Selection([
        ('all', 'All Companies & Warehouses'),
        ('company', 'Selected Company Only'),
        ('warehouse', 'Selected Warehouse Only'),
    ], string='Analyse Scope', default='all', required=True)

    company_ids = fields.Many2many(
        'res.company',
        string='Companies',
        default=lambda self: self.env.company,
    )
    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        string='Warehouses',
    )

    include_zero_demand = fields.Boolean(
        string='Include Products with Zero Sales',
        default=False,
        help='If checked, products with NO sales in the period are also analysed '
             '(useful to detect dead stock). This makes the analysis slower.'
    )

    @api.onchange('scope')
    def _onchange_scope(self):
        if self.scope == 'all':
            self.company_ids = False
            self.warehouse_ids = False
        elif self.scope == 'company':
            self.warehouse_ids = False

    @api.onchange('company_ids')
    def _onchange_company_ids(self):
        """Filter warehouse options to selected companies."""
        if self.company_ids:
            return {
                'domain': {
                    'warehouse_ids': [('company_id', 'in', self.company_ids.ids)]
                }
            }

    @require_group(_MANAGER_GROUP)
    def action_generate(self):
        """Run the analysis and return to the suggestions list.
        Manager-only: the menu entry is already restricted, but the method
        itself must be guarded too — RPC does not respect menu groups."""
        self.ensure_one()

        company_ids = None
        warehouse_ids = None

        if self.scope == 'company' and self.company_ids:
            company_ids = self.company_ids.ids
        elif self.scope == 'warehouse' and self.warehouse_ids:
            warehouse_ids = self.warehouse_ids.ids
            company_ids = list(set(self.warehouse_ids.mapped('company_id').ids))
        elif self.scope == 'all':
            pass  # None = all
        else:
            raise UserError(_('Please select at least one company or warehouse.'))

        result = self.env['smart.reorder.suggestion'].generate_suggestions(
            company_ids=company_ids,
            warehouse_ids=warehouse_ids,
            include_zero_demand=self.include_zero_demand,  # ← NEW
            trigger_type='manual',
        )

        domain = []
        if self.scope == 'warehouse' and self.warehouse_ids:
            domain = [('warehouse_id', 'in', self.warehouse_ids.ids)]
        elif self.scope == 'company' and self.company_ids:
            domain = [('company_id', 'in', self.company_ids.ids)]

        # Show result summary and redirect to suggestions
        return {
            'type': 'ir.actions.act_window',
            'name': 'Reorder Suggestions',
            'res_model': 'smart.reorder.suggestion',
            'view_mode': 'list,form',
            'domain': domain,
            'context': {
                'default_company_id': self.env.company.id,
                'search_default_reorder_needed': 1,
                'search_default_gb_warehouse': 1,
            },
        }
