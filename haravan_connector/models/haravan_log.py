# -*- coding: utf-8 -*-
from odoo import api, fields, models

class HaravanLog(models.Model):
    _name = 'haravan.log'
    _description = 'Haravan Sync Log'
    _order = 'create_date desc'
    _rec_name = 'message'

    config_id = fields.Many2one('haravan.config', string='Configuration', ondelete='cascade', index=True)
    level = fields.Selection([
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ], string='Level', default='info')
    message = fields.Char(string='Message', required=True)
    details = fields.Text(string='Details')
    create_date = fields.Datetime(string='Date', readonly=True)

    def action_clear_logs(self):
        domain = [('config_id', 'in', self.mapped('config_id').ids)]
        self.search(domain).unlink()
