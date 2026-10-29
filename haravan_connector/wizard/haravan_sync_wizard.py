# -*- coding: utf-8 -*-
from odoo import api, fields, models, _

class HaravanSyncWizard(models.TransientModel):
    _name = 'haravan.sync.wizard'
    _description = 'Haravan Manual Sync Wizard'

    config_id = fields.Many2one('haravan.config', string='Configuration', required=True)
    sync_products = fields.Boolean(string='Sync Products', default=True)
    sync_orders = fields.Boolean(string='Sync Orders', default=True)
    sync_customers = fields.Boolean(string='Sync Customers', default=True)
    sync_revenue = fields.Boolean(string='Sync Revenue', default=False)
    result_message = fields.Text(string='Result', readonly=True)

    def action_run_sync(self):
        messages = []
        config = self.config_id

        if self.sync_products:
            self.env['haravan.product'].sync_products_from_haravan()
            messages.append(_('✓ Products synced'))

        if self.sync_orders:
            self.env['haravan.order'].sync_orders_from_haravan()
            messages.append(_('✓ Orders synced'))

        if self.sync_customers:
            self.env['haravan.customer'].sync_customers_from_haravan()
            messages.append(_('✓ Customers synced to CRM'))

        if self.sync_revenue:
            count = self.env['haravan.revenue'].sync_revenue_from_haravan()
            messages.append(_('✓ Revenue records synced: %d') % count)

        self.result_message = '\n'.join(messages) or _('Nothing selected.')
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }
