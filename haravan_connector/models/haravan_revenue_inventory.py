# -*- coding: utf-8 -*-
import logging
import pytz
from dateutil import parser
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

def _parse_datetime(dt_str):
    if not dt_str:
        return False
    try:
        dt = parser.parse(dt_str)
        return dt.astimezone(pytz.UTC).replace(tzinfo=None)
    except Exception:
        return False

class HaravanRevenue(models.Model):
    """
    Đồng bộ doanh thu: Sync sales data from Haravan to Odoo
    for revenue reporting, business analytics and accounting.
    """
    _name = 'haravan.revenue'
    _description = 'Haravan Revenue Record'
    _rec_name = 'haravan_order_number'
    _order = 'order_date desc'

    config_id = fields.Many2one('haravan.config', string='Configuration', required=True, ondelete='cascade')
    haravan_order_id = fields.Char(string='Haravan Order ID', index=True)
    haravan_order_number = fields.Char(string='Order #')
    order_date = fields.Datetime(string='Order Date')
    financial_status = fields.Char(string='Financial Status')

    subtotal_price = fields.Float(string='Subtotal', default=0.0)
    total_discounts = fields.Float(string='Discounts', default=0.0)
    total_shipping = fields.Float(string='Shipping', default=0.0)
    total_tax = fields.Float(string='Tax', default=0.0)
    total_price = fields.Float(string='Total Revenue', default=0.0)
    currency = fields.Char(string='Currency', default='VND')

    odoo_sale_order_id = fields.Many2one('sale.order', string='Sale Order')
    odoo_invoice_id = fields.Many2one('account.move', string='Invoice',
                                      domain=[('move_type', '=', 'out_invoice')])

    revenue_month = fields.Char(string='Month', compute='_compute_period', store=True)
    revenue_year = fields.Integer(string='Year', compute='_compute_period', store=True)

    @api.depends('order_date')
    def _compute_period(self):
        for rec in self:
            if rec.order_date:
                rec.revenue_month = rec.order_date.strftime('%d-%m-%Y')
                rec.revenue_year = rec.order_date.year
            else:
                rec.revenue_month = False
                rec.revenue_year = 0

    @api.model
    def sync_revenue(self, date_from=None, date_to=None):
        """Sync revenue/orders for all active Haravan configurations. Returns total synced."""
        configs = self.env['haravan.config'].sudo().search([('active', '=', True)])
        total_synced = 0
        for config in configs:
            last_record = self.search(
                [('config_id', '=', config.id)], order='haravan_order_id desc', limit=1
            )
            params = {
                'financial_status': 'paid',
                'since_id': last_record.haravan_order_id if last_record else False,
            }
            if date_from:
                params['created_at_min'] = date_from.strftime('%Y-%m-%dT%H:%M:%S')
            if date_to:
                params['created_at_max'] = date_to.strftime('%Y-%m-%dT%H:%M:%S')
            elif not date_from and config.last_sync_date:
                params['created_at_min'] = config.last_sync_date.strftime('%Y-%m-%dT%H:%M:%S')

            synced = 0
            for ho in config._iter_api_pages('/orders.json', 'orders', params, page_size=100):
                try:
                    self._upsert_revenue(config, ho)
                    synced += 1
                except Exception as e:
                    config._log('error', f"Revenue upsert failed [{ho.get('id', '?')}]: {e}")

            config._log('info', f'Revenue sync complete: {synced} records processed')
            total_synced += synced
        return total_synced

    def _upsert_revenue(self, config, ho):
        haravan_id = str(ho.get('id') or "")
        if not haravan_id:
            return

        record = self.search([
            ('config_id', '=', config.id),
            ('haravan_order_id', '=', haravan_id)
        ], limit=1)

        order_mapping = self.env['haravan.order'].search([
            ('config_id', '=', config.id),
            ('haravan_order_id', '=', haravan_id)
        ], limit=1)

        shipping_total = sum(
            float((s or {}).get('price') or 0.0)
            for s in (ho.get('shipping_lines') or [])
        )

        vals = {
            'config_id': config.id,
            'haravan_order_id': haravan_id,
            'haravan_order_number': str(ho.get('order_number') or ""),
            'order_date': _parse_datetime(ho.get('created_at')),     
            'financial_status': str(ho.get('financial_status') or 'unknown'),
            'subtotal_price': float(ho.get('subtotal_price') or 0.0),     
            'total_discounts': float(ho.get('total_discounts') or 0.0),     
            'total_shipping': shipping_total,                         
            'total_tax': float(ho.get('total_tax') or 0.0),               
            'total_price': float(ho.get('total_price') or 0.0),       
            'currency': str(ho.get('currency') or 'VND'),
            'odoo_sale_order_id': (
                order_mapping.odoo_sale_order_id.id
                if order_mapping and order_mapping.odoo_sale_order_id else False
            ),
        }
        if record:
            record.write(vals)
        else:
            self.create(vals)

    def action_create_invoice(self):
        self.ensure_one()
        if not self.odoo_sale_order_id:
            raise models.UserError(_('No linked sale order to invoice.'))
        so = self.odoo_sale_order_id
        if so.state == 'draft':
            so.action_confirm()
        inv = so._create_invoices()
        if inv:
            self.odoo_invoice_id = inv[0].id
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'view_mode': 'form',
                'res_id': inv[0].id,
            }
        return {'type': 'ir.actions.act_window_close'}


