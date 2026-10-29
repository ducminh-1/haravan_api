# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import logging
import requests
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from base64 import b64encode
from ..lib.haravan import ProxyClient
from ..lib.haravan.exceptions import TooManyRequestsError
import time
_logger = logging.getLogger(__name__)

HARAVAN_API_BASE = "https://apis.haravan.com/com"
HARAVAN_OAUTH_URL = "https://accounts.haravan.com/connect/token"

class HaravanConfig(models.Model):
    _name = 'haravan.config'
    _description = 'Haravan Store Configuration'
    _rec_name = 'shop_domain'

    name = fields.Char(string='Configuration Name', required=True, default='Haravan Store')
    shop_domain = fields.Char(
        string='Shop Domain',
        required=True,
        help='Your Haravan shop domain, e.g. mystore.myharavan.com'
    )
    active = fields.Boolean(default=True)
    state = fields.Selection([
        ('draft', 'Not Connected'),
        ('connected', 'Connected'),
        ('error', 'Connection Error'),
    ], default='draft', string='Status', readonly=True)

    client_id = fields.Char(string='Client ID', required=True)
    client_secret = fields.Char(string='Client Secret', required=True)
    access_token = fields.Char(string='Access Token', readonly=True)
    refresh_token = fields.Char(string='Refresh Token', readonly=True)
    token_expiry = fields.Datetime(string='Token Expiry', readonly=True)
    webhook_secret = fields.Char(
        string='Webhook Secret',
        help='Secret key used to verify Haravan webhook signatures (HMAC-SHA256)'
    )

    auto_sync_products = fields.Boolean(string='Auto Sync Products', default=True)
    auto_sync_orders = fields.Boolean(string='Auto Sync Orders', default=True)
    auto_sync_inventory = fields.Boolean(string='Auto Sync Inventory', default=True)
    sync_interval = fields.Selection([
        ('15', 'Every 15 minutes'),
        ('30', 'Every 30 minutes'),
        ('60', 'Every hour'),
        ('360', 'Every 6 hours'),
        ('1440', 'Daily'),
    ], string='Sync Interval', default='60')
    private_access_token = fields.Char(string='Private Access Token')
    proxy_api_user = fields.Char(string="Proxy Api User")
    proxy_api_password = fields.Char(string="Proxy Api Password")
    default_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Default Warehouse',
        help='Warehouse used for Haravan orders'
    )
    default_pricelist_id = fields.Many2one(
        'product.pricelist', string='Default Pricelist'
    )
    order_prefix = fields.Char(
        string='Order Prefix', default='HAR-',
        help='Prefix added to Haravan order names in Odoo'
    )
    auto_confirm_orders = fields.Boolean(
        string='Auto Confirm Orders', default=False,
        help='Automatically confirm imported Haravan orders'
    )
    import_order_status = fields.Selection([
        ('any', 'Any Status'),
        ('open', 'Open Only'),
        ('closed', 'Closed Only'),
    ], string='Import Orders With Status', default='open')
    retry_seconds = fields.Integer(string="Retry Seconds", default=4)

    default_product_category_id = fields.Many2one(
        'product.category', string='Default Product Category'
    )
    sync_product_images = fields.Boolean(string='Sync Product Images', default=True)

    default_customer_country_id = fields.Many2one(
        'res.country', string='Default Customer Country'
    )

    webhook_ids = fields.One2many(
        'haravan.webhook', 'config_id', string='Webhooks'
    )
    webhook_count = fields.Integer(compute='_compute_webhook_count')

    last_sync_date = fields.Datetime(string='Last Sync', readonly=True)
    total_orders_synced = fields.Integer(string='Orders Synced', readonly=True)
    total_products_synced = fields.Integer(string='Products Synced', readonly=True)
    log_ids = fields.One2many('haravan.log', 'config_id', string='Sync Logs')
    redirect_uri = fields.Char(string='Redirect Url')
    stock_upload_type = fields.Selection([
        ('qty_available', 'Quantity On Hand'),
        ('free_qty', 'Free to Use Quantity'),
    ], default='free_qty', required=True)
    upload_stock = fields.Boolean(default=True, help='Upload stock to Haravan')
    location_ids = fields.One2many(
        'haravan.location.mapping', 'config_id',
        string='Location Mappings',
        help='All Haravan ↔ Odoo location mappings for this store'
    )
    upload_price_limit = fields.Integer(string='Upload Price Limit', default=16)
    upload_stock_limit = fields.Integer(string='Upload Stock Limit', default=50)
    product_tmpl_ids = fields.One2many('haravan.product', 'config_id', string='Products', ondelete='cascade')

    @api.depends('webhook_ids')
    def _compute_webhook_count(self):
        for rec in self:
            rec.webhook_count = len(rec.webhook_ids)

    @api.constrains('shop_domain')
    def _check_shop_domain(self):
        for rec in self:
            if rec.shop_domain and ' ' in rec.shop_domain:
                raise ValidationError(_('Shop domain must not contain spaces.'))

    def _get_api_url(self, endpoint):
        base_url = "https://apis.haravan.com/com"
        endpoint = endpoint.lstrip('/')
        return f"{base_url}/{endpoint}"

    def _get_headers(self):
        return {
            'Authorization': f'Bearer {self.private_access_token}',
            'Content-Type': 'application/json',
        }

    def _ensure_token_valid(self):
        """Refresh access token if expired or near expiry."""
        if not self.access_token:
            raise UserError(_('No access token. Please authenticate first.'))
        if self.token_expiry and fields.Datetime.now() >= self.token_expiry - timedelta(minutes=5):
            self._refresh_access_token()

    def _refresh_access_token(self):
        if not self.refresh_token:
            raise UserError(_('No refresh token available. Re-authenticate.'))
        payload = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        try:
            resp = requests.post(HARAVAN_OAUTH_URL, data=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            self.write({
                'access_token': data['access_token'],
                'refresh_token': data.get('refresh_token', self.refresh_token),
                'token_expiry': fields.Datetime.now() + timedelta(seconds=data.get('expires_in', 3600)),
            })
        except Exception as e:
            self.state = 'error'
            raise UserError(_('Token refresh failed: %s') % str(e))

    def action_haravan_authenticate_callback(self, code):
        """Dùng mã 'code' nhận được từ Haravan để đổi lấy Access Token."""
        self.ensure_one()
        payload = {
            'grant_type': 'authorization_code',
            'code': code, 
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
        }

        headers = {'Content-Type': 'application/x-www-form-urlencoded'}

        try:
            resp = requests.post(HARAVAN_OAUTH_URL, data=payload, headers=headers, timeout=15)

            if resp.status_code != 200:
                _logger.error("Chi tiết lỗi Haravan: %s", resp.text)
                raise UserError(_('Lỗi xác thực: %s') % resp.text)

            data = resp.json()

            self.write({
                'access_token': data.get('access_token'),
                'refresh_token': data.get('refresh_token', ''),
                'token_expiry': fields.Datetime.now() + timedelta(seconds=data.get('expires_in', 3600)),
                'state': 'connected',
            })

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Haravan'),
                    'message': _('Kết nối cửa hàng thành công!'),
                    'type': 'success',
                }
            }
        except Exception as e:
            self.state = 'error'
            raise UserError(_('Authentication failed: %s') % str(e))

    def action_test_connection(self):
        """Quick connectivity check against Haravan shop API."""
        self.ensure_one()
        try:
            resp = requests.get(
                self._get_api_url('/shop.json'),
                headers=self._get_headers(),
                timeout=10
            )
            resp.raise_for_status()
            shop = resp.json().get('shop', {})
            self.state = 'connected'
            self._log('info', f"Connection OK – shop: {shop.get('name', self.shop_domain)}")
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _('Connected to shop: %s') % shop.get('name', self.shop_domain),
                    'type': 'success',
                }
            }
        except Exception as e:
            self.state = 'error'
            raise UserError(_('Connection test failed: %s') % str(e))

    def verify_webhook_signature(self, payload_bytes, signature_header):
        """Return True if HMAC-SHA256 signature matches."""
        if not self.webhook_secret:
            return False
        computed = hmac.new(
            self.webhook_secret.encode('utf-8'),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, signature_header or '')

    def _api_get(self, endpoint, params=None):
        url = self._get_api_url(endpoint)
        resp = requests.get(url, headers=self._get_headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _api_post(self, endpoint, data):
        url = self._get_api_url(endpoint)
        resp = requests.post(url, headers=self._get_headers(), json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _api_put(self, endpoint, data):
        url = self._get_api_url(endpoint)
        resp = requests.put(url, headers=self._get_headers(), json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _api_delete(self, endpoint):
        url = self._get_api_url(endpoint)
        resp = requests.delete(url, headers=self._get_headers(), timeout=15)
        resp.raise_for_status()
        
    def _iter_api_pages(self, endpoint, list_key, params=None, page_size=50):
        """Yield records from a paginated Haravan list endpoint.

        Walks `?page=N` starting at 1, injecting `limit`/`page` into a copy
        of `params` on each request, and stops when a page comes back empty
        or shorter than `page_size`. A failed request is logged and ends the
        iteration. Items without an `id` are skipped.
        """
        self.ensure_one()
        params = dict(params or {})
        params['limit'] = page_size
        page = 1
        while True:
            params['page'] = page
            try:
                data = self._api_get(endpoint, params)
            except Exception as e:
                self._log('error', f'{endpoint} fetch page {page} failed: {e}')
                return

            items = data.get(list_key) or []
            if not items:
                return
            for item in items:
                if item and item.get('id'):
                    yield item

            if len(items) < page_size:
                return
            page += 1

    def _log(self, level, message, details=None):
        self.env['haravan.log'].sudo().create({
            'config_id': self.id,
            'level': level,
            'message': message,
            'details': json.dumps(details, indent=2) if details else '',
        })
        getattr(_logger, level)(f'[Haravan:{self.shop_domain}] {message}')

    def _get_proxy_client(self):
        self.ensure_one()
        base_url = self._get_proxy_client_base_url()
        token = self._get_proxy_client_token()
        return ProxyClient(token=token, base_url=base_url)

    def _get_proxy_client_base_url(self):
        return 'https://proxyinventories.haravan.app/'

    def _get_proxy_client_token(self):
        token = ''
        if self.proxy_api_user and self.proxy_api_password:
            token = b64encode(f'{self.proxy_api_user}:{self.proxy_api_password}'.encode('utf-8')).decode('ascii')
        return token

    def _call_client(self, client, method, *args, **kwargs):
        self.ensure_one()
        try:
            return getattr(client, method)(*args, **kwargs)
        except TooManyRequestsError:
            _logger.info('Haravan limit request reached: %s', method)
            time.sleep(self.retry_seconds)
            return getattr(client, method)(*args, **kwargs)
        except Exception as e:
            _logger.info('Haravan call client error: %s', str(e))
            raise UserError(_('Call Haravan API: %s\nError: %s') % (method, e))

    def action_view_webhooks(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Webhooks'),
            'res_model': 'haravan.webhook',
            'view_mode': 'list,form',
            'domain': [('config_id', '=', self.id)],
            'context': {'default_config_id': self.id},
        }

    def action_open_sync_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Manual Sync'),
            'res_model': 'haravan.sync.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_config_id': self.id},
        }

    def _upload_stock_to_haravan(self):
        HaravanConfig = self.env['haravan.config'].sudo()
        config_id = HaravanConfig.search([], limit=1)
        mapping = config_id.location_ids.filtered(lambda m: m.odoo_location_id and not m.is_unavailable_quantity)
        if not mapping:
            return True

        client = config_id._get_proxy_client()
        location_code = mapping[0].haravan_location_id
        transaction_time = fields.Datetime.now().isoformat()
        # product_variants = config_id.product_tmpl_ids.variant_ids.mapped('odoo_product_id')
        
        product_variants = self.env['product.template'].search([])

        limit = config_id.upload_stock_limit or 50
        for i in range(0, len(product_variants), limit):
            batch = product_variants[i : i + limit]
            items = []

            for product in batch:
                if not product.default_code:
                    _logger.warning("Skipping product %s: No barcode found.", product.display_name)
                    continue          
                items.append({
                    "location_code": location_code,
                    "type": "set",
                    "sku": product.default_code,
                    "quantity": int(product.qty_available),
                    "transaction_time": transaction_time
                })
            if not items:
                continue

            params = {"inventory": {"items": items}}
            try:
                response = config_id._call_client(client, 'inventories', params=params)
                _logger.info('Haravan: Response %s', response)
            except Exception as e:
                _logger.error('Haravan: Failed to upload stock batch. Error: %s', str(e))

        return True

    def _upload_haravan_price(self):
        HaravanConfig = self.env['haravan.config'].sudo()
        config_id = HaravanConfig.search([], limit=1) 

        if not config_id:
            _logger.warning("Haravan: No configuration found for price upload.")
            return False       

        client = config_id._get_proxy_client()
        limit = config_id.upload_price_limit or 50   
        product_ids = self.env['product.template'].search([])
        if not product_ids:
            _logger.info("Haravan: No synchronized products found to update prices.")
            return True

        for i in range(0, len(product_ids), limit):
            batch = product_ids[i : i + limit]
            data = []           
            for product in batch:
                price = product.list_price
                price = config_id._get_odoo_pricelist_price(product)
                compare_at_price = product.list_price or 0.0
                sku = product.default_code
                if compare_at_price <= price:
                    compare_at_price = 0.0
                data.append({
                    'sku': sku,
                    'price': price,
                    'compare_at_price': compare_at_price,
                })
            if not data:
                continue

            params = {'product_variants': data}
            try:
                response = config_id._call_client(client, 'sync_price', params=params)
                _logger.info('Haravan: Price upload response: %s', response)
            except Exception as e:
                _logger.error('Haravan: Price sync failed for batch starting at index %s. Error: %s', i, str(e))

        return True

    def _get_odoo_pricelist_price(self, odoo_product):
        """
        Resolve the sale price for a product variant:
        1. If config has a pricelist → use pricelist price
        2. Else use product.list_price (sales price)
        Always returns a positive float.
        """
        price = 0.0
        try:
            if self.default_pricelist_id:
                price = self.default_pricelist_id._get_product_price(
                    odoo_product, 1.0, currency=self.default_pricelist_id.currency_id
                )
            else:
                price = odoo_product.list_price or 0.0
        except Exception as e:
            _logger.warning('Price resolution failed for %s: %s', odoo_product.default_code, e)
        return max(0.0, price)
