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

    values = {
        'company_name': company.name,
        'today': date.today(),
        'n_critical': n_critical,
        'n_urgent': n_urgent,
        'n_dead': n_dead,
        'currency': currency,
        'total_value': f'{total_value:,.2f}',
        'critical_items': critical_items,
    }
    body_html = self.env['ir.qweb'].sudo()._render(
        'smart_reorder_advisor.reorder_report_email_template', values
    )

    subject = (
        f'{config.email_subject_prefix} {company.name} — '
        f'{critical_count} Critical | {date.today()}'
    )
    for user in config.notify_user_ids:
        self.env['mail.thread'].sudo().message_notify(
            partner_ids=[user.partner_id.id],
            subject=subject,
            body=body_html,
            subtype_xmlid='mail.mt_comment',
        )


def send_email_report(self, company, config):
    """Returns True on success, False if PDF generation/send failed (a real
    error - surfaced by the caller as a 'completed_with_errors' run status),
    or None when there was simply nothing to send (not an error, or held back
    by the Critical/Urgent-only gate — Task 4)."""
    if not config.notify_user_ids:
        return None
    suggestions = self.sudo().search([
        ('company_id', '=', company.id),
        ('reorder_needed', '=', True),
    ])
    if not suggestions:
        return None

    # Task 4: reuse the existing "Notify Only for Critical / Urgent Items"
    # toggle (already labeled for exactly this) so the boss isn't emailed a
    # near-empty report most weeks — checked fresh here (not the loop's
    # running total_critical) so it reflects snoozed/marked-ordered items
    # correctly at the moment of sending.
    if config.critical_notify_only:
        urgent_or_critical_count = suggestions.filtered(
            lambda s: s.urgency in ('critical', 'urgent')
        )
        if not urgent_or_critical_count:
            return None

    try:
        # Odoo 16+ signature is _render_qweb_pdf(report_ref, res_ids, data):
        # the report reference goes FIRST. Passing the ids as report_ref
        # (the old 15.0 calling convention) makes every render fail.
        pdf_content, _dummy = self.env['ir.actions.report'].sudo()._render_qweb_pdf(
            'smart_reorder_advisor.action_report_boss_weekly_order', suggestions.ids
        )
    except Exception as e:
        _logger.warning('SmartReorder: PDF generation failed — %s', e)
        return False

    try:
        att = self.env['ir.attachment'].sudo().create({
            'name':     f'Weekly_Order_List_{company.name}_{date.today()}.pdf',
            'type':     'binary',
            'datas':    base64.b64encode(pdf_content),
            'mimetype': 'application/pdf',
            # Link to the config record so these weekly PDFs are reachable and
            # cleanable instead of accumulating as orphan attachments forever.
            'res_model': 'smart.reorder.config',
            'res_id':    config.id,
        })
        self.env['mail.mail'].sudo().create({
            'subject':         f'{config.email_subject_prefix} Weekly Order List — {company.name} — {date.today()}',
            'body_html':       f'<p>Weekly Order List for <strong>{html_escape(company.name)}</strong> attached.</p>',
            'recipient_ids':   [(6, 0, config.notify_user_ids.mapped('partner_id').ids)],
            'attachment_ids':  [(6, 0, [att.id])],
        }).send()
        _logger.info('SmartReorder: PDF email created/sent to %d users', len(config.notify_user_ids))
        return True
    except Exception:
        _logger.exception('SmartReorder: PDF email sending failed')
        return False
