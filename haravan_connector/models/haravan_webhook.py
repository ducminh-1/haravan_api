# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

WEBHOOK_TOPICS = [
    ('orders/create', 'Order Created'),
    ('orders/updated', 'Order Updated'),
    ('orders/paid', 'Order Paid'),
    ('orders/cancelled', 'Order Cancelled'),
    ('orders/fulfilled', 'Order Fulfilled'),
    ('products/create', 'Product Created'),
    ('products/update', 'Product Updated'),
    ('products/delete', 'Product Deleted'),
    ('customers/create', 'Customer Created'),
    ('customers/update', 'Customer Updated'),
    ('locations/create', 'Location Created'),
    ('locations/update', 'Location Updated'),
    ('inventorytransaction/create', 'Inventory Transaction Create'),
    ('inventorylocationbalances/create', 'Details Inventory'),
    ('inventorylocationbalances/update', 'Update Details Inventory'),
    ('refunds/create', 'Refund Created'),
]

class HaravanWebhook(models.Model):
    _name = 'haravan.webhook'
    _description = 'Haravan Webhook Subscription'
    _rec_name = 'topic'

    config_id = fields.Many2one(
        'haravan.config', string='Configuration',
        required=True, ondelete='cascade'
    )
    topic = fields.Selection(WEBHOOK_TOPICS, string='Event Topic', required=True)
    haravan_webhook_id = fields.Char(string='Haravan Webhook ID', readonly=True)
    callback_url = fields.Char(string='Callback URL', readonly=True, compute='_compute_callback_url', store=True)
    format = fields.Selection([('json', 'JSON')], default='json', string='Format')
    last_triggered = fields.Datetime(string='Last Triggered', readonly=True)
    trigger_count = fields.Integer(string='Trigger Count', default=0, readonly=True)

    _sql_constraints = [
        ('unique_config_topic', 'UNIQUE(config_id, topic)',
         'A webhook for this topic already exists for this configuration.')
    ]

    @api.depends('config_id')
    def _compute_callback_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for rec in self:
            rec.callback_url = f"{base}/haravan/webhook/{rec.config_id.id}"

    @api.model
    def process_webhook(self, config_id, topic, payload):
        config = self.env['haravan.config'].browse(config_id)
        if not config.exists():
            return

        handler_key = topic.replace('/', '_')
        handler_name = f'_handle_{handler_key}'
        handler = getattr(self, handler_name, None)

        if handler is None:
            _logger.info('Unhandled Haravan webhook topic for config %s', config.id)
            return

        try:
            handler(config, payload)
        except Exception as e:
            _logger.error('Webhook handler error [%s]: %s', topic, e, exc_info=True)
            config._log('error', f'Webhook handler error [{topic}]: {e}')

    @api.model
    def _handle_orders_create(self, config, payload):
        order = (payload.get('order') or payload or {})
        if not order.get('id'):
            return
        self.env['haravan.order']._upsert_order(config, order)

    @api.model
    def _handle_orders_updated(self, config, payload):
        order = (payload.get('order') or payload or {})
        if not order.get('id'):
            return
        self.env['haravan.order']._upsert_order(config, order)

    @api.model
    def _handle_orders_paid(self, config, payload):
        order = (payload.get('order') or payload or {})
        haravan_id = str(order.get('id') or "")
        if not haravan_id:
            return
        mapping = self.env['haravan.order'].search(
            [('config_id', '=', config.id), ('haravan_order_id', '=', haravan_id)], limit=1
        )
        if mapping and mapping.odoo_sale_order_id:
            so = mapping.odoo_sale_order_id
            if so.state == 'draft':
                try:
                    so.action_confirm()
                except Exception as e:
                    _logger.warning('Auto-confirm on paid failed [order %s]: %s', haravan_id, e)

    @api.model
    def _handle_orders_cancelled(self, config, payload):
        order = (payload.get('order') or payload or {})
        haravan_id = str(order.get('id') or "")
        if not haravan_id:
            return
        mapping = self.env['haravan.order'].search(
            [('config_id', '=', config.id), ('haravan_order_id', '=', haravan_id)], limit=1
        )
        if mapping:
            if mapping.odoo_sale_order_id and mapping.odoo_sale_order_id.state in ('draft', 'sent'):
                try:
                    mapping.odoo_sale_order_id.action_cancel()
                except Exception as e:
                    _logger.warning('Cancel SO failed [order %s]: %s', haravan_id, e)
            mapping.write({'haravan_fulfillment_status': 'cancelled'})

    @api.model
    def _handle_orders_fulfilled(self, config, payload):
        order = (payload.get('order') or payload or {})
        haravan_id = str(order.get('id') or "")
        if not haravan_id:
            return
        mapping = self.env['haravan.order'].search(
            [('config_id', '=', config.id), ('haravan_order_id', '=', haravan_id)], limit=1
        )
        if mapping:
            self.env['haravan.order']._update_order_status(config, mapping, order)
            mapping.write({'haravan_fulfillment_status': 'fulfilled'})

    @api.model
    def _handle_products_create(self, config, payload):
        product = (payload.get('product') or payload or {})
        if not product.get('id'):
            return
        self.env['haravan.product']._upsert_product(config, product)

    @api.model
    def _handle_products_update(self, config, payload):
        product = (payload.get('product') or payload or {})
        if not product.get('id'):
            return
        self.env['haravan.product']._upsert_product(config, product)

    @api.model
    def _handle_products_delete(self, config, payload):
        haravan_id = str(payload.get('id') or "")
        if not haravan_id:
            return
        mapping = self.env['haravan.product'].search(
            [('config_id', '=', config.id), ('haravan_product_id', '=', haravan_id)], limit=1
        )
        if mapping:
            mapping.write({'sync_state': 'pending'})
            config._log('warning', f'Product deleted on Haravan: {haravan_id}')

