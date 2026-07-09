import logging
import base64
from datetime import date

from odoo.tools import html_escape

_logger = logging.getLogger(__name__)


def send_notifications(self, company, config, critical_count, warehouse_id=None):
    if not config.notify_user_ids:
        return
    if config.critical_notify_only and critical_count == 0:
        return

    wh_domain = [('warehouse_id', '=', warehouse_id)] if warehouse_id else []
    critical_domain = [
        ('company_id', '=', company.id),
        ('urgency', '=', 'critical'),
        ('is_snoozed', '=', False),
        ('active', '=', True),
    ] + wh_domain

    Suggestion = self.sudo()
    # Counts and the value sum are computed DB-side; only the bounded top-10
    # critical table loads actual records.
    n_critical = Suggestion.search_count(critical_domain)
    n_urgent = Suggestion.search_count(
        [('company_id', '=', company.id), ('urgency', '=', 'urgent')] + wh_domain)
    n_dead = Suggestion.search_count(
        [('company_id', '=', company.id), ('is_dead_stock', '=', True)] + wh_domain)
    value_group = Suggestion.read_group(
        [('company_id', '=', company.id), ('reorder_needed', '=', True)] + wh_domain,
        ['estimated_purchase_value:sum'], [],
    )
    total_value = (value_group[0]['estimated_purchase_value'] or 0.0) if value_group else 0.0
    currency = company.currency_id.symbol or ''
    critical_items = Suggestion.search(critical_domain, limit=10)

    company_name = html_escape(company.name)
    body_lines = [
        f'<h3>📦 Smart Reorder Advisor — {company_name}</h3>',
        f'<p>Analysis completed on <strong>{date.today()}</strong></p>',
        f'<table style="border-collapse:collapse;width:100%;">',
        f'<tr style="background:#f5f5f5;"><td style="padding:8px;"><strong>🔴 Critical</strong></td>'
        f'<td style="padding:8px;"><strong>{n_critical}</strong></td></tr>',
        f'<tr><td style="padding:8px;">🟠 Urgent</td><td style="padding:8px;"><strong>{n_urgent}</strong></td></tr>',
        f'<tr style="background:#f5f5f5;"><td style="padding:8px;">💀 Dead Stock</td><td style="padding:8px;"><strong>{n_dead}</strong></td></tr>',
        f'<tr><td style="padding:8px;">💰 Total Est. Purchase Value</td><td style="padding:8px;"><strong>{currency} {total_value:,.2f}</strong></td></tr>',
        f'</table>',
    ]

    if critical_items:
        body_lines += [
            '<p><strong>Critical Items (top 10):</strong></p>',
            '<table style="border-collapse:collapse;width:100%;font-size:12px;">',
            '<tr style="background:#e74c3c;color:white;"><th style="padding:6px;">Part No.</th>',
            '<th style="padding:6px;">Product</th><th style="padding:6px;">On Hand</th>',
            '<th style="padding:6px;">Suggest</th><th style="padding:6px;">Value</th></tr>',
        ]
        for item in critical_items:
            body_lines.append(
                f'<tr style="background:#fdecea;"><td style="padding:5px;">{html_escape(item.default_code or "—")}</td>'
                f'<td style="padding:5px;">{html_escape(item.product_id.display_name)}</td>'
                f'<td style="padding:5px;color:#e74c3c;"><strong>{item.qty_on_hand:.0f}</strong></td>'
                f'<td style="padding:5px;"><strong>{item.suggested_reorder_qty:.0f}</strong></td>'
                f'<td style="padding:5px;">{currency} {item.estimated_purchase_value:,.2f}</td></tr>'
            )
        body_lines.append('</table>')

    body_lines.append('<p>Open <strong>Reorder Advisor</strong> in Odoo for the full report.</p>')

    subject = (
        f'{config.email_subject_prefix} {company.name} — '
        f'{critical_count} Critical | {date.today()}'
    )
    for user in config.notify_user_ids:
        self.env['mail.thread'].sudo().message_notify(
            partner_ids=[user.partner_id.id],
            subject=subject,
            body=''.join(body_lines),
            subtype_xmlid='mail.mt_comment',
        )


def send_email_report(self, company, config):
    """Returns True on success, False if PDF generation/send failed (a real
    error - surfaced by the caller as a 'completed_with_errors' run status),
    or None when there was simply nothing to send (not an error)."""
    if not config.notify_user_ids:
        return None
    suggestions = self.sudo().search([
        ('company_id', '=', company.id),
        ('reorder_needed', '=', True),
    ])
    if not suggestions:
        return None

    try:
        # Odoo 16+ signature is _render_qweb_pdf(report_ref, res_ids, data):
        # the report reference goes FIRST. Passing the ids as report_ref
        # (the old 15.0 calling convention) makes every render fail.
        pdf_content, _dummy = self.env['ir.actions.report'].sudo()._render_qweb_pdf(
            'smart_reorder_advisor.action_report_reorder_summary', suggestions.ids
        )
    except Exception as e:
        _logger.warning('SmartReorder: PDF generation failed — %s', e)
        return False

    try:
        att = self.env['ir.attachment'].sudo().create({
            'name':     f'Reorder_Report_{company.name}_{date.today()}.pdf',
            'type':     'binary',
            'datas':    base64.b64encode(pdf_content),
            'mimetype': 'application/pdf',
            # Link to the config record so these weekly PDFs are reachable and
            # cleanable instead of accumulating as orphan attachments forever.
            'res_model': 'smart.reorder.config',
            'res_id':    config.id,
        })
        self.env['mail.mail'].sudo().create({
            'subject':         f'{config.email_subject_prefix} Weekly Reorder — {company.name} — {date.today()}',
            'body_html':       f'<p>Weekly Smart Reorder Report for <strong>{html_escape(company.name)}</strong> attached.</p>',
            'recipient_ids':   [(6, 0, config.notify_user_ids.mapped('partner_id').ids)],
            'attachment_ids':  [(6, 0, [att.id])],
        }).send()
        _logger.info('SmartReorder: PDF email created/sent to %d users', len(config.notify_user_ids))
        return True
    except Exception:
        _logger.exception('SmartReorder: PDF email sending failed')
        return False
