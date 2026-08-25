# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """Task 4: automated weekly delivery becomes the default behavior.
    noupdate="1" data (the cron record) and existing config rows don't pick
    up their new defaults on a plain module update, so both are flipped on
    here explicitly for any database already installed before this version."""
    env = api.Environment(cr, SUPERUSER_ID, {})

    cron = env.ref('smart_reorder_advisor.cron_smart_reorder_weekly', raise_if_not_found=False)
    if cron and not cron.active:
        cron.active = True

    configs = env['smart.reorder.config'].search([('send_email_report', '=', False)])
    if configs:
        configs.write({'send_email_report': True})
