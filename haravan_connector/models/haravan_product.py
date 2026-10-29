# -*- coding: utf-8 -*-
import base64
import logging
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class HaravanProduct(models.Model):
    _name = 'haravan.product'
    _description = 'Haravan Product Mapping'
    _rec_name = 'haravan_title'

    config_id = fields.Many2one('haravan.config', string='Configuration', required=True, ondelete='cascade')
    haravan_product_id = fields.Char(string='Haravan Product ID', required=True, index=True)
    haravan_title = fields.Char(string='Haravan Title')
    odoo_product_tmpl_id = fields.Many2one('product.template', string='Odoo Product', ondelete='set null')
    sync_state = fields.Selection([
        ('synced', 'Synced'),
        ('pending', 'Pending'),
        ('error', 'Error'),
    ], default='pending', string='Sync State')
    last_synced = fields.Datetime(string='Last Synced', readonly=True)
    haravan_updated_at = fields.Char(string='Haravan Updated At')
    variant_ids = fields.One2many('haravan.product.variant', 'haravan_product_id', string='Variants')
    error_message = fields.Text('Error Message')

    _sql_constraints = [
        ('unique_haravan_product', 'UNIQUE(config_id, haravan_product_id)',
         'This Haravan product is already mapped.')
    ]

    @api.model
    def sync_products(self):
        """Pull all products from Haravan and upsert into Odoo for active configs."""
        configs = self.env['haravan.config'].sudo().search([('active', '=', True)])
        for config in configs:
            params = {}
            if config.last_sync_date:
                params['updated_at_min'] = config.last_sync_date.strftime('%Y-%m-%dT%H:%M:%S')
                
            product_data = list(config._iter_api_pages('/products.json', 'products', params))    
            if not product_data:
                config._log('info', 'Product sync complete: 0 products to process')
                continue

            synced_count = self._upsert_product_batch(config, product_data)

            config.total_products_synced = (config.total_products_synced or 0) + synced_count
            config._log('info', f'Product sync complete: {synced_count} products processed')

    def _upsert_product_batch(self, config, product_data):
        """Upsert a batch of Haravan products into Odoo, only mapping to existing products."""
        haravan_ids = [str(p.get('id')) for p in product_data if p.get('id')]
        skus_to_check = [
            str((p.get('variants')[0] or {}).get('sku') or "")
            for p in product_data if p.get('variants')
        ]
        skus_to_check = list(filter(None, set(skus_to_check)))

        # Pre-fetch existing data in batch
        mappings = self.search([
            ('config_id', '=', config.id),
            ('haravan_product_id', 'in', haravan_ids)
        ])
        mappings_by_id = {m.haravan_product_id: m for m in mappings}

        products = self.env['product.product'].search([('default_code', 'in', skus_to_check)])
        tmpls_by_sku = {p.default_code: p.product_tmpl_id for p in products}

        new_mappings_vals = []
        variants_data = []
        
        for hp in product_data:
            haravan_id = str(hp.get('id') or "")
            if not haravan_id:
                continue

            mapping = mappings_by_id.get(haravan_id)
            
            variants = (hp.get('variants') or [])
            first_variant = (variants[0] or {}) if variants else {}
            default_code = str(first_variant.get('sku') or "")

            title = (hp.get('title') or f'Haravan Product {haravan_id}')

            tmpl = None
            if mapping and mapping.odoo_product_tmpl_id:
                tmpl = mapping.odoo_product_tmpl_id
            elif default_code in tmpls_by_sku:
                tmpl = tmpls_by_sku[default_code]

            mapping_vals = {
                'config_id': config.id,
                'haravan_product_id': haravan_id,
                'haravan_title': title,
                'last_synced': fields.Datetime.now(),
                'haravan_updated_at': hp.get('updated_at') or "",
            }

            if tmpl:
                mapping_vals.update({
                    'odoo_product_tmpl_id': tmpl.id,
                    'sync_state': 'synced',
                    'error_message': False,
                })
                
                variants_data.extend([{
                    'config_id': config.id,
                    'tmpl_id': tmpl.id,
                    'haravan_product_id': haravan_id,
                    'variant_data': v,
                } for v in variants if v and v.get('id')])
                
                if config.sync_product_images:
                    images = (hp.get('images') or [])
                    if images and images[0].get('src'):
                        self._sync_product_image(tmpl, images[0]['src'])
            else:
                mapping_vals.update({
                    'odoo_product_tmpl_id': False,
                    'sync_state': 'pending',
                    'error_message': f"Product with SKU '{default_code}' not found in Odoo.",
                })

            if mapping:
                mapping.write(mapping_vals)
            else:
                new_mappings_vals.append(mapping_vals)

        if new_mappings_vals:
            self.create(new_mappings_vals)
        
        if variants_data:
            self.env['haravan.product.variant']._upsert_batch(variants_data)

        return len(product_data)

    def _sync_product_image(self, tmpl, image_url):
        if not image_url:
            return
        try:
            resp = requests.get(image_url, timeout=10)
            resp.raise_for_status()
            tmpl.image_1920 = base64.b64encode(resp.content)
        except Exception as e:
            _logger.warning('Image sync failed for %s: %s', tmpl.name, e)

    def action_push_product(self):
        """Push product title, description, and all variant prices to Haravan."""
        self.ensure_one()
        if not self.odoo_product_tmpl_id:
            raise UserError(_('No Odoo product linked.'))
        self._push_product(self.config_id)
        self.write({'sync_state': 'synced', 'last_synced': fields.Datetime.now()})

    def _push_product(self, config):
        """Build a full product payload and PUT it to Haravan."""
        tmpl = self.odoo_product_tmpl_id
        if not tmpl:
            return

        variant_payloads = []
        for vm in self.variant_ids:
            odoo_prod = vm.odoo_product_id
            if not (vm.haravan_variant_id and odoo_prod):
                continue

            price = config._get_odoo_pricelist_price(odoo_prod)
            compare_at = float(vm.haravan_compare_at_price or 0.0)

            variant_payloads.append({
                'id': int(vm.haravan_variant_id),
                'price': str(round(price, 2)),
                'compare_at_price': str(round(compare_at, 2)) if compare_at > 0 else None,
            })

        payload = {'product': {
            'id': int(self.haravan_product_id),
            'title': tmpl.name or '',
            'body_html': tmpl.description_sale or '',
            'variants': variant_payloads,
        }}
        config._api_put(f'/products/{self.haravan_product_id}.json', payload)

        now = fields.Datetime.now()
        for vm in self.variant_ids:
            if vm.odoo_product_id:
                vm.write({
                    'haravan_price': config._get_odoo_pricelist_price(vm.odoo_product_id),
                    'price_last_pushed': now,
                })

    @api.model
    def action_push_prices(self, only_diverged=False):
        """Bulk push prices for all synced products across active configs."""
        configs = self.env['haravan.config'].sudo().search([('active', '=', True)])
        for config in configs:
            domain = [('config_id', '=', config.id), ('sync_state', '=', 'synced')]
            mappings = self.search(domain)
            pushed_count, error_count = 0, 0
            for mapping in mappings:
                if not mapping.odoo_product_tmpl_id:
                    continue
                try:
                    if only_diverged:
                        is_diverged = any(v.price_sync_state != 'synced' for v in mapping.variant_ids)
                        if not is_diverged:
                            continue
                    mapping._push_product(config)
                    pushed_count += 1
                except Exception as e:
                    error_count += 1
                    config._log('warning', f'Price push failed [{mapping.haravan_title}]: {e}')

            config._log('info', f'Price push complete: {pushed_count} updated, {error_count} errors')

    def action_push_inventory(self):
        """Push stock for the selected haravan products."""
        for rec in self:
            try:
                rec._push_inventory()
                rec.write({'last_synced': fields.Datetime.now()})
            except Exception as e:
                _logger.warning('Inventory push failed [%s]: %s', rec.haravan_title, e)

        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Stock Sync'),
            'message': _('Inventory sync completed for %d product(s).') % len(self),
            'type': 'success',
        }}

    def _push_inventory(self):
        self.ensure_one()
        config = self.config_id
        location_mapping = config.location_ids.filtered(lambda m: m.odoo_location_id and not m.is_unavailable_quantity)
        if not location_mapping:
            _logger.warning("No active location mapping for config %s", config.name)
            return

        client = config._get_proxy_client()
        now_iso = fields.Datetime.now().isoformat()

        for m in location_mapping:
            items = []
            for vm in self.variant_ids:
                product = vm.odoo_product_id
                if product and product.default_code:
                    items.append({
                        "location_code": m.haravan_location_id,
                        "type": "set",
                        "sku": product.default_code,
                        "quantity": int(m.get_product_quantity(product)),
                        "transaction_time": now_iso
                    })
            if items:
                try:
                    config._call_client(client, 'inventories', params={"inventory": {"items": items}})
                except Exception as e:
                    _logger.error('Haravan: Failed to push stock for %s at %s. Error: %s', self.haravan_title, m.haravan_location_id, e)

class HaravanProductVariant(models.Model):
    _name = 'haravan.product.variant'
    _description = 'Haravan Product Variant Mapping'

    haravan_product_id = fields.Many2one('haravan.product', string='Haravan Product', ondelete='cascade')
    haravan_variant_id = fields.Char(string='Haravan Variant ID', required=True)
    haravan_sku = fields.Char(string='SKU')
    haravan_inventory_item_id = fields.Char(string='Inventory Item ID')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Variant', ondelete='set null')

    haravan_price = fields.Float(string='Price on Haravan', digits='Product Price', default=0.0)
    haravan_compare_at_price = fields.Float(
        string='Compare-At Price', digits='Product Price', default=0.0,
        help='Original / crossed-out price shown on Haravan product page'
    )
    haravan_grams = fields.Float(string='Weight (g)', default=0.0, help='Weight in grams from Haravan')
    price_last_pushed = fields.Datetime(string='Price Last Pushed', readonly=True)
    price_sync_state = fields.Selection([
        ('synced', 'Synced'),
        ('diverged', 'Diverged'),
        ('pending', 'Pending'),
    ], string='Price State', compute='_compute_price_sync_state', store=True)

    @api.depends('haravan_price', 'odoo_product_id', 'odoo_product_id.lst_price')
    def _compute_price_sync_state(self):
        for rec in self:
            if not rec.price_last_pushed:
                rec.price_sync_state = 'pending'
            elif rec.odoo_product_id and abs(rec.odoo_product_id.lst_price - rec.haravan_price) > 0.009:
                rec.price_sync_state = 'diverged'
            else:
                rec.price_sync_state = 'synced'

    @api.model
    def _upsert_batch(self, all_variants_data):
        """Upsert a batch of Haravan variants."""
        if not all_variants_data:
            return

        variant_ids = [str(d['variant_data']['id']) for d in all_variants_data]
        skus = list(set(filter(None, [str(d['variant_data'].get('sku') or '') for d in all_variants_data])))
        haravan_product_ids = list(set(d['haravan_product_id'] for d in all_variants_data))
        config_id = all_variants_data[0]['config_id']

        # Pre-fetch existing data
        variant_mappings = self.search([('haravan_variant_id', 'in', variant_ids)])
        variant_mappings_by_id = {m.haravan_variant_id: m for m in variant_mappings}

        variants = self.env['product.product'].search([('default_code', 'in', skus)])
        variants_by_sku = {v.default_code: v for v in variants}

        parents = self.env['haravan.product'].search([
            ('config_id', '=', config_id),
            ('haravan_product_id', 'in', haravan_product_ids)
        ])
        parents_by_id = {p.haravan_product_id: p for p in parents}

        new_variant_mappings_vals = []
        for data in all_variants_data:
            variant = data['variant_data']
            haravan_variant_id = str(variant.get('id') or "")
            sku = str(variant.get('sku') or "")
            tmpl_id = data['tmpl_id']

            odoo_variant = variants_by_sku.get(sku)
            if odoo_variant and odoo_variant.product_tmpl_id.id != tmpl_id:
                odoo_variant = None # SKU exists but belongs to another template

            if odoo_variant:
                odoo_variant.write({
                    'is_haravan_mapped': True,
                    'barcode': variant.get('barcode') or False,
                })
            
            mapping_vals = {
                'haravan_variant_id': haravan_variant_id,
                'haravan_sku': sku,
                'odoo_product_id': odoo_variant.id if odoo_variant else False,
                'haravan_price': float(variant.get('price') or 0.0),
                'haravan_compare_at_price': float(variant.get('compare_at_price') or 0.0),
                'haravan_inventory_item_id': str(variant.get('inventory_item_id') or ''),
                'haravan_grams': float(variant.get('grams') or 0.0),
            }

            mapping = variant_mappings_by_id.get(haravan_variant_id)
            if mapping:
                mapping.write(mapping_vals)
            else:
                parent_map = parents_by_id.get(data['haravan_product_id'])
                if parent_map:
                    mapping_vals['haravan_product_id'] = parent_map.id
                    new_variant_mappings_vals.append(mapping_vals)

        if new_variant_mappings_vals:
            self.create(new_variant_mappings_vals)

