# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID

def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    pf = env.ref('smart_reorder_advisor.paperformat_landscape_summary', raise_if_not_found=False)
    if pf:
        pf.default = False
    
    cr.execute("ALTER TABLE smart_reorder_transfer_lane DROP CONSTRAINT IF EXISTS smart_reorder_transfer_lane_lane_unique")
