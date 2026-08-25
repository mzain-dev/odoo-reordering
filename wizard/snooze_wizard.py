from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from datetime import date, timedelta

class SmartReorderSnoozeWizard(models.TransientModel):
    _name = 'smart.reorder.snooze.wizard'
    _description = 'Snooze Reorder Suggestions'

    suggestion_ids = fields.Many2many(
        'smart.reorder.suggestion',
        string='Suggestions',
        default=lambda self: self.env.context.get('active_ids', []),
    )
    snooze_until = fields.Date(
        string='Snooze Until',
        required=True,
        default=lambda self: fields.Date.today() + timedelta(days=7),
        help='Date until which the reorder suggestions will be snoozed.'
    )
    snooze_note = fields.Char(
        string='Snooze Reason',
        required=True,
        help='Reason for snoozing the suggestions.'
    )

    @api.constrains('snooze_until')
    def _check_snooze_date(self):
        for rec in self:
            if rec.snooze_until and rec.snooze_until < fields.Date.today():
                raise ValidationError(_('Snooze date cannot be in the past.'))

    def action_confirm(self):
        self.ensure_one()
        if not self.suggestion_ids:
            return
        # check access
        self.suggestion_ids._check_snooze_access()
        
        # update suggestions
        self.suggestion_ids.sudo().write({
            'snoozed_until': self.snooze_until,
            'snoozed_note': self.snooze_note,
            'reorder_needed': False,
        })

        
        return {'type': 'ir.actions.act_window_close'}
