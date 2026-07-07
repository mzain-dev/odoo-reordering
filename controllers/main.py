from odoo import http, fields
from odoo.http import request


class SmartReorderBannerController(http.Controller):
    """
    T-34: 'Last Analysis' banner shown above the Reorder Suggestions list via
    banner_route on the tree view (views/reorder_suggestion_views.xml). Plain
    <tree> views can't host arbitrary informational content the way a form
    view can — banner_route is the actual Odoo mechanism for it, fetched by
    the web client and rendered above the list.
    """

    @http.route('/smart_reorder_advisor/last_run_banner', type='json', auth='user')
    def last_run_banner(self):
        return {'html': self._build_banner_html(request.env)}

    @staticmethod
    def _build_banner_html(env):
        """Separated from the route handler so the HTML-building logic can be
        unit tested directly, without going through an actual HTTP/JSON-RPC
        round trip. Prefers the cron log (Feature 1.2) for an accurate run
        timestamp + status; falls back to the most recent suggestion's
        analysis_date for installs with no run history yet. Runs as the
        current user (no sudo) so it never shows data the viewer can't see."""
        company_ids = env.companies.ids

        last_run = env['smart.reorder.cron.log'].search([
            ('company_id', 'in', company_ids),
            ('status', 'in', ('completed', 'completed_with_errors')),
        ], order='finished_at desc', limit=1)

        if last_run:
            local_dt = fields.Datetime.context_timestamp(env.user, last_run.finished_at)
            timestamp_str = local_dt.strftime('%d %b %Y, %H:%M')
            if last_run.status == 'completed_with_errors':
                alert_class = 'alert-warning'
                note = ' — completed with some warnings (see 🕓 Run History)'
            else:
                alert_class = 'alert-info'
                note = ''
            return (
                f'<div class="alert {alert_class} mb-0 py-2" role="alert">'
                f'🕓 <strong>Last Analysis:</strong> {timestamp_str}{note}'
                f'</div>'
            )

        last_suggestion = env['smart.reorder.suggestion'].search([
            ('company_id', 'in', company_ids),
        ], order='analysis_date desc', limit=1)

        if last_suggestion and last_suggestion.analysis_date:
            return (
                f'<div class="alert alert-warning mb-0 py-2" role="alert">'
                f'🕓 <strong>Last Analysis:</strong> {last_suggestion.analysis_date} '
                f'— no run history found yet, showing the most recent suggestion\'s date.'
                f'</div>'
            )

        return (
            '<div class="alert alert-secondary mb-0 py-2" role="alert">'
            '🕓 No analysis has been run yet. Use <strong>⚙️ Generate Analysis</strong> '
            'to create the first batch of suggestions.'
            '</div>'
        )
