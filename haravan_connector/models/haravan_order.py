# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.fields import Domain
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class HaravanOrder(models.Model):
    _name = 'haravan.order'
    _description = 'Haravan Order Mapping'
    _rec_name = 'haravan_order_number'

    config_id = fields.Many2one(
        'haravan.config', string='Configuration',
        required=True, ondelete='cascade'
    )
    haravan_order_id = fields.Char(string='Haravan Order ID', required=True, index=True)
    haravan_order_number = fields.Char(string='Haravan Order #')
    haravan_financial_status = fields.Char(string='Financial Status')
    haravan_fulfillment_status = fields.Char(string='Fulfillment Status')
    odoo_sale_order_id = fields.Many2one('sale.order', string='Odoo Sale Order', ondelete='set null')
    sync_state = fields.Selection([
        ('synced', 'Synced'),
        ('pending', 'Pending'),
        ('error', 'Error'),
    ], default='pending', string='Sync State')
    last_synced = fields.Datetime(string='Last Synced', readonly=True)
    error_message = fields.Text(string='Last Error')

    # Reporting fields
    amount_total = fields.Monetary(related='odoo_sale_order_id.amount_total', store=True, string='Total Amount')
    currency_id = fields.Many2one(related='odoo_sale_order_id.currency_id', store=True)
    date_order = fields.Datetime(related='odoo_sale_order_id.date_order', store=True, string='Order Date')

    _sql_constraints = [
        ('unique_haravan_order', 'UNIQUE(config_id, haravan_order_id)', 'Order already imported.')
    ]

    @api.model
    def sync_orders(self):
        """Pull recent orders from Haravan and upsert into Odoo for active configs."""
        configs = self.env['haravan.config'].sudo().search([('active', '=', True)])
        total_synced = 0
        for config in configs:
            params = {'status': config.import_order_status or 'open'}
            if config.last_sync_date:
                params['updated_at_min'] = config.last_sync_date.strftime('%Y-%m-%dT%H:%M:%S')

            order_data = list(config._iter_api_pages('/orders.json', 'orders', params))
            
            if not order_data:
                config._log('info', 'Order sync complete: 0 orders to process')
                continue

            synced_count = self._upsert_order_batch(config, order_data)

            config.total_orders_synced = (config.total_orders_synced or 0) + synced_count
            config._log('info', f'Order sync complete: {synced_count} orders processed')
            total_synced += synced_count
        return total_synced

    def _upsert_order_batch(self, config, order_data):
        """Upsert a batch of Haravan orders into Odoo."""
        # Extract identifiers for batch fetching
        haravan_order_ids = [str(o.get('id')) for o in order_data if o.get('id')]
        customer_emails = (o.get('email') for o in order_data)
        customer_phones = (o.get('phone') for o in order_data)
        
        line_skus = []
        for o in order_data:
            for line in o.get('line_items', []):
                if line.get('sku'):
                    line_skus.append(line.get('sku'))
        line_skus = line_skus

        # Pre-fetch existing data
        mappings = self.search([('haravan_order_id', 'in', haravan_order_ids), ('config_id', '=', config.id)])
        mappings_by_id = {m.haravan_order_id: m for m in mappings}

        partners_by_email = {p.email: p for p in self.env['res.partner'].search([('email', 'in', customer_emails)])}
        partners_by_phone = {p.phone: p for p in self.env['res.partner'].search([('phone', 'in', customer_phones)])}
        
        products_by_sku = {p.default_code: p for p in self.env['product.product'].search([('default_code', 'in', line_skus)])}
        
        delivery_product = delivery_product = self.env['product.product'].search(
            [('name', '=', 'Delivery Charges'), ('type', '=', 'service')], limit=1
        )
        if not delivery_product:
            tmpl = self.env['product.template'].create({
                'name': 'Delivery Charges', 'type': 'service', 'sale_ok': True,
            })
            delivery_product = tmpl.product_variant_ids[:1]
            
        discount_product = self.env['product.product'].search(
            [('name', '=', 'Haravan Discount'), ('type', '=', 'service')], limit=1
        )
        if not discount_product:
            tmpl = self.env['product.template'].create({
                'name': 'Haravan Discount', 'type': 'service', 'sale_ok': True,
            })
            discount_product = tmpl.product_variant_ids[:1]
            
        new_mappings = []

        for ho in order_data:
            haravan_id = str(ho.get('id'))
            if haravan_id in mappings_by_id and mappings_by_id[haravan_id].odoo_sale_order_id:
                # Handle order status updates for existing orders
                self._update_order_status(config, mappings_by_id[haravan_id], ho)
                continue

            partner = self._upsert_partner_batch(config, ho, partners_by_email, partners_by_phone)
            
            order_number = ho.get('order_number') or haravan_id
            order_vals = {
                'partner_id': partner.id,
                'client_order_ref': str(order_number),
                'note': ho.get('note') or "",
                'origin': f"Haravan #{order_number}",
                'haravan_order_id': haravan_id,
                'haravan_order_number': order_number,
                'warehouse_id': config.default_warehouse_id.id,
                'pricelist_id': config.default_pricelist_id.id,
            }
            
            so = self.env['sale.order'].create(order_vals)
            
            missing_skus = self._create_order_lines_batch(config, so, ho.get('line_items', []), products_by_sku)
            
            if delivery_product:
                self._create_shipping_lines_batch(so, ho.get('shipping_lines', []), delivery_product)
            
            if discount_product:
                self._create_discount_lines_batch(so, ho.get('total_discounts', 0), discount_product)

            if config.auto_confirm_orders and not missing_skus:
                try:
                    so.action_confirm()
                except Exception as e:
                    _logger.warning(f'Auto-confirm failed for order {haravan_id}: {e}')
                    
            assignes = so.team_id.member_ids
            if missing_skus:
                warning_note = _("Missing products: %s.") % ', '.join(missing_skus)
                so.write({
                    'haravan_missing_products': warning_note,
                    'note': f"{warning_note}\n\n{so.note or ''}"
                })
                for user in assignes:
                    note = _("Haravan order %s is missing products for the following SKUs: %s. Please create them.") % (order_number, ', '.join(missing_skus))
                    self.env['mail.activity'].create({
                        'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
                        'res_model_id': self.env.ref('sale.model_sale_order').id,
                        'res_id': so.id,
                        'summary': _('Missing Haravan Products'),
                        'note': note,
                        'user_id': user.id,
                    })

            map_vals = {
                'config_id': config.id,
                'haravan_order_id': haravan_id,
                'haravan_order_number': order_number,
                'odoo_sale_order_id': so.id,
                'sync_state': 'pending' if missing_skus else 'synced',
                'last_synced': fields.Datetime.now(),
                'error_message': f"Missing SKUs: {', '.join(missing_skus)}" if missing_skus else False,
            }
            new_mappings.append(map_vals)

        if new_mappings:
            self.create(new_mappings)
            
        return len(order_data)
        
    def _upsert_partner_batch(self, config, ho, partners_by_email, partners_by_phone):
        """
        Finds or creates a res.partner record based on Haravan customer data, ensuring
        it's marked as mapped from Haravan.

        This method is optimized to reduce database queries within a batch import by
        using in-memory caches for partners.

        :param config: The haravan.config record.
        :param ho: The Haravan order data dictionary.
        :param partners_by_email: A cache dict {email: res.partner record}, which is updated.
        :param partners_by_phone: A cache dict {phone: res.partner record}, which is updated.
        :return: A res.partner record.
        """
        customer_data = ho.get('customer', {})
        email = customer_data.get('email') or ho.get('email')
        shipping_address = ho.get('shipping_address', {})
        phone = customer_data.get('phone') or shipping_address.get('phone')

        partner = None
        if email and email in partners_by_email:
            partner = partners_by_email[email]
        elif phone and phone in partners_by_phone:
            partner = partners_by_phone[phone]
        else:
            # If not in cache, search DB using email and/or phone
            domain_parts = []
            if email:
                domain_parts.append(('email', '=', email))
            if phone:
                domain_parts.append(('phone', '=', phone))
            
            if domain_parts:
                # Build an OR domain: [ '|', ('email', '=', ...), ('phone', '=', ...)]
                search_domain = (['|'] * (len(domain_parts) - 1)) + domain_parts
                partner = self.env['res.partner'].search(search_domain, limit=1)

        if partner:
            # Partner exists, ensure it's flagged as mapped
            if not partner.is_haravan_mapped:
                partner.write({'is_haravan_mapped': True})
        else:
            # Partner does not exist, create a new one
            name = (
               f"{customer_data.get('last_name', '')} {customer_data.get('first_name', '')}".strip() 
               or _('Haravan Guest')
            )
            partner_vals = {
                'name': name,
                'email': email,
                'phone': phone,
                'street': shipping_address.get('address1'),
                'street2': shipping_address.get('address2'),
                'city': shipping_address.get('city'),
                'zip': shipping_address.get('zip'),
                'is_haravan_mapped': True,
            }
            partner_vals.update(self._resolve_partner_geo(config, shipping_address))
            partner = self.env['res.partner'].create(partner_vals)

        if partner:
            if email and email not in partners_by_email:
                partners_by_email[email] = partner
            if phone and phone not in partners_by_phone:
                partners_by_phone[phone] = partner
                
        return partner

    def _create_order_lines_batch(self, config, so, lines_data, products_by_sku):
        missing_skus = []
        line_vals_list = []
        for line in lines_data:
            sku = line.get('sku')
            product = products_by_sku.get(sku)
            if not product:
                missing_skus.append(sku or line.get('title', 'Unknown'))
                continue
            
            qty = line.get('quantity', 1.0)
            order_grams = float(line.get('grams', 0.0))
            
            haravan_variant = self.env['haravan.product.variant'].search([
                ('odoo_product_id', '=', product.id)
            ], limit=1)
            product_grams = haravan_variant.haravan_grams if haravan_variant else 0.0
            
            if not product_grams:
                variant_id = line.get('variant_id')
                if variant_id:
                    try:
                        variant_data = config._api_get(f'/variants/{variant_id}.json')
                        if variant_data and 'variant' in variant_data:
                            product_grams = float(variant_data['variant'].get('grams') or 0.0)
                            if haravan_variant and product_grams:
                                haravan_variant.haravan_grams = product_grams
                    except Exception:
                        _logger.info(f"Lỗi lấy grams variant {variant_id}: {e}")
            
            if order_grams and product_grams:
                qty = order_grams / product_grams
            else:
                qty = line.get('quantity', 1.0)
            
            line_vals_list.append({
                'order_id': so.id,
                'product_id': product.id,
                'product_uom_qty': qty,
                'price_unit': line.get('price', 0.0),
                'name': line.get('title') or product.name,
            })
        
        if line_vals_list:
            self.env['sale.order.line'].create(line_vals_list)
        return missing_skus

    def _create_shipping_lines_batch(self, so, shipping_data, delivery_product):
        line_vals_list = []
        for shipping in shipping_data:
            price = float(shipping.get('price', 0.0))
            if price > 0:
                line_vals_list.append({
                    'order_id': so.id,
                    'product_id': delivery_product.id,
                    'product_uom_qty': 1,
                    'price_unit': price,
                    'name': shipping.get('title') or 'Shipping',
                })
        if line_vals_list:
            self.env['sale.order.line'].create(line_vals_list)

    def _create_discount_lines_batch(self, so, total_discounts, discount_product):
        amount = float(total_discounts)
        if amount > 0:
            self.env['sale.order.line'].create({
                'order_id': so.id, 
                'product_id': discount_product.id,
                'product_uom_qty': 1,
                'price_unit': -abs(amount),
                'name': 'Haravan Discount',
            })

    def _resolve_partner_geo(self, config, shipping):
        """Resolve country/state ids from a Haravan address dict."""
        country_code = str(shipping.get('country_code') or "")
        country = False
        if country_code:
            country = self.env['res.country'].search([('code', '=', country_code)], limit=1)
        country = country or config.default_customer_country_id
        if not country:
            return {}

        vals = {'country_id': country.id}
        province_code = str(shipping.get('province_code') or "")
        if province_code:
            state = self.env['res.country.state'].search([
                ('code', '=', province_code),
                ('country_id', '=', country.id),
            ], limit=1)
            if state:
                vals['state_id'] = state.id
        return vals

    def _update_order_status(self, config, mapping, ho):
        so = mapping.odoo_sale_order_id
        if not so:
            return
        fulfillment = str(ho.get('fulfillment_status') or "")
        if fulfillment in ('fulfilled', 'partial') and so.state == 'sale':
            for picking in so.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                try:
                    for move in picking.move_ids:
                        move.quantity = move.product_uom_qty
                    picking.button_validate()
                except Exception as e:
                    _logger.warning('Auto-validate picking failed [%s]: %s', picking.name, e)

    def action_view_sale_order(self):
        self.ensure_one()
        if not self.odoo_sale_order_id:
            raise UserError(_('No sale order linked.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sale Order'),
            'res_model': 'sale.order',
            'view_mode': 'form',
            'res_id': self.odoo_sale_order_id.id,
        }

