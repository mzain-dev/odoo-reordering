import base64
import io

from odoo import models, fields, api, _
from odoo.exceptions import UserError

URGENCY_LABELS = {
    'critical': 'Critical — Negative Stock',
    'urgent':   'Urgent — Below Lead Time',
    'normal':   'Normal — Reorder Recommended',
    'dead':     'Dead Stock — No Movement',
    'ok':       'OK — Sufficient Stock',
}
TREND_LABELS = {
    'up':     'Rising',
    'stable': 'Stable',
    'down':   'Falling',
    'new':    'New / No History',
}

EXPORT_HEADERS = [
    'Part Number', 'Product Name', 'Category', 'Warehouse',
    'On Hand Qty', 'Avg Monthly Demand', 'Lead Time (Months)',
    'Suggested Reorder Qty', 'Reorder Value', 'Urgency', 'ABC Class',
    'Demand Trend', 'Budget Rank', 'Vendor', 'Last Sale Date',
    'Months Left After Order',
]


class ExportSuggestionsWizard(models.TransientModel):
    """
    Exports the currently filtered/selected smart.reorder.suggestion list to
    an Excel file — triggered from a button in the list view's header, which
    Odoo passes either active_domain (current search filter, no row selected)
    or active_ids (rows explicitly selected) in context.
    """
    _name = 'smart.reorder.export.wizard'
    _description = 'Export Reorder Suggestions to Excel'

    record_count = fields.Integer(string='Suggestions to Export', readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'record_count' in fields_list:
            res['record_count'] = self.env['smart.reorder.suggestion'].search_count(
                self._get_export_domain()
            )
        return res

    def _get_export_domain(self):
        """Mirrors how the list view's header button was triggered: a domain
        (search filter applied, nothing selected), explicit row selection, or
        — if invoked outside that context entirely — everything."""
        domain = self.env.context.get('active_domain')
        if domain is not None:
            return domain
        active_ids = self.env.context.get('active_ids')
        if active_ids:
            return [('id', 'in', active_ids)]
        return []

    def action_export(self):
        self.ensure_one()
        suggestions = self.env['smart.reorder.suggestion'].search(self._get_export_domain())
        if not suggestions:
            raise UserError(_('No suggestions match the current filter — nothing to export.'))

        xlsx_data = self._build_xlsx(suggestions)
        attachment = self.env['ir.attachment'].create({
            'name': f'Reorder_Suggestions_{fields.Date.today()}.xlsx',
            'type': 'binary',
            'datas': base64.b64encode(xlsx_data),
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    @api.model
    def _build_xlsx(self, suggestions):
        """Pure data → bytes — no DB writes, easy to unit test directly."""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = 'Reorder Suggestions'

        header_font = Font(bold=True, color='FFFFFF')
        header_fill = PatternFill(start_color='2C3E50', end_color='2C3E50', fill_type='solid')
        for col, header in enumerate(EXPORT_HEADERS, start=1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')

        for row, rec in enumerate(suggestions, start=2):
            ws.cell(row=row, column=1,  value=rec.default_code or '')
            ws.cell(row=row, column=2,  value=rec.product_id.display_name or '')
            ws.cell(row=row, column=3,  value=rec.product_categ_id.name or '')
            ws.cell(row=row, column=4,  value=rec.warehouse_id.name or '')
            ws.cell(row=row, column=5,  value=rec.qty_on_hand)
            ws.cell(row=row, column=6,  value=rec.avg_monthly_demand)
            ws.cell(row=row, column=7,  value=rec.lead_time_months)
            ws.cell(row=row, column=8,  value=rec.suggested_reorder_qty)
            ws.cell(row=row, column=9,  value=rec.reorder_value)
            ws.cell(row=row, column=10, value=URGENCY_LABELS.get(rec.urgency, rec.urgency or ''))
            ws.cell(row=row, column=11, value=rec.abc_class or '')
            ws.cell(row=row, column=12, value=TREND_LABELS.get(rec.demand_trend, rec.demand_trend or ''))
            ws.cell(row=row, column=13, value=rec.budget_rank)
            ws.cell(row=row, column=14, value=rec.vendor_id.name or '')
            ws.cell(row=row, column=15, value=rec.last_sale_date or None)
            ws.cell(row=row, column=16, value=rec.months_of_stock_after_order)

        for col in range(1, len(EXPORT_HEADERS) + 1):
            ws.column_dimensions[get_column_letter(col)].width = max(14, len(EXPORT_HEADERS[col - 1]) + 2)
        ws.freeze_panes = 'A2'

        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()
