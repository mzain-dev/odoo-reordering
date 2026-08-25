from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class SmartReorderTransferLane(models.Model):
    """
    A 'lane' between two specific warehouses with its own transfer lead time
    (T-31), so inter-warehouse rebalancing suggestions can reflect real shipping
    time — e.g. Muscat → Salalah: 3 days, Salalah → Sohar: 5 days — instead of
    applying one global default lead time to every possible warehouse pair
    regardless of distance.
    """
    _name = 'smart.reorder.transfer.lane'
    _description = 'Smart Reorder Inter-Warehouse Transfer Lane'
    _order = 'source_warehouse_id, dest_warehouse_id'

    source_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Source Warehouse', required=True, index=True,
    )
    dest_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Destination Warehouse', required=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', string='Company', related='source_warehouse_id.company_id',
        store=True, index=True,
    )
    lead_time_days = fields.Integer(
        string='Lead Time (Days)', required=True, default=3,
        help='How long a transfer from the source to the destination warehouse '
             'typically takes. Used instead of the company default lead time '
             '(Configuration → Company Settings) when recommending an '
             'inter-warehouse transfer for this specific pair.'
    )
    active = fields.Boolean(default=True)

    @api.constrains('source_warehouse_id', 'dest_warehouse_id', 'active')
    def _check_warehouse_pair(self):
        for rec in self:
            if rec.source_warehouse_id == rec.dest_warehouse_id:
                raise ValidationError(_('Source and destination warehouse must be different.'))
            if rec.source_warehouse_id.company_id != rec.dest_warehouse_id.company_id:
                raise ValidationError(_('Source and destination warehouses must belong to the same company.'))
        
        active_recs = self.filtered('active')
        if not active_recs:
            return
            
        # In-memory duplicate check for records being validated together
        seen = set()
        for rec in active_recs:
            pair = (rec.source_warehouse_id.id, rec.dest_warehouse_id.id)
            if pair in seen:
                raise ValidationError(_('Duplicate active lane detected for the same warehouse pair.'))
            seen.add(pair)
            
        # Batch database duplicate check in one query
        domain = ['|'] * (len(active_recs) - 1) if len(active_recs) > 1 else []
        for rec in active_recs:
            if len(active_recs) > 1:
                domain.extend(['&', ('source_warehouse_id', '=', rec.source_warehouse_id.id), ('dest_warehouse_id', '=', rec.dest_warehouse_id.id)])
            else:
                domain.extend([('source_warehouse_id', '=', rec.source_warehouse_id.id), ('dest_warehouse_id', '=', rec.dest_warehouse_id.id)])
        
        duplicates = self.search(domain + [('active', '=', True), ('id', 'not in', active_recs.ids)])
        if duplicates:
            raise ValidationError(_('An active lane already exists for one of the source/destination warehouse pairs.'))

    @api.constrains('lead_time_days')
    def _check_positive_lead_time(self):
        for rec in self:
            if rec.lead_time_days <= 0:
                raise ValidationError(_('Lead time must be at least 1 day.'))

    @api.depends('source_warehouse_id.name', 'dest_warehouse_id.name', 'lead_time_days')
    def _compute_display_name(self):
        for rec in self:
            src = rec.source_warehouse_id.name or '?'
            dst = rec.dest_warehouse_id.name or '?'
            rec.display_name = _('%s → %s (%s days)') % (src, dst, rec.lead_time_days)
