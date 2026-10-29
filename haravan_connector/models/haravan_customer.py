# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class HaravanCustomer(models.Model):
    """Maps Odoo Partners to Haravan Customers."""
    _name = 'haravan.customer'
    _description = 'Haravan Customer Mapping'
    _rec_name = 'haravan_email'

    config_id = fields.Many2one('haravan.config', string='Configuration', required=True, ondelete='cascade')
    haravan_customer_id = fields.Char('Haravan Customer ID', required=True, index=True)
    odoo_partner_id = fields.Many2one('res.partner', 'Odoo Contact', ondelete='set null')
    
    # Mirrored Haravan Fields
    haravan_email = fields.Char('Email')
    haravan_first_name = fields.Char('First Name')
    haravan_last_name = fields.Char('Last Name')
    haravan_phone = fields.Char('Phone')
    haravan_orders_count = fields.Integer('Haravan Order Count')
    haravan_total_spent = fields.Float('Total Spent (Haravan)')
    haravan_tags = fields.Char('Haravan Tags')
    haravan_note = fields.Text('Haravan Note')

    # Sync Status
    sync_state = fields.Selection([
        ('synced', 'Synced'), ('pending', 'Pending'), ('error', 'Error')],
        default='pending', string='Sync State')
    last_synced = fields.Datetime('Last Synced', readonly=True)

    _sql_constraints = [
        ('unique_haravan_customer', 'UNIQUE(config_id, haravan_customer_id)', 'Customer already mapped.')
    ]

    @api.model
    def sync_customers(self):
        """Cron job entry point to sync customers from Haravan."""
        for config in self.env['haravan.config'].sudo().search([('active', '=', True)]):
            self._sync_customer_batch(config)

    def _sync_customer_batch(self, config):
        """Sync a batch of customers for a given configuration."""
        params = {}
        if config.last_sync_date:
            params['updated_at_min'] = config.last_sync_date.strftime('%Y-%m-%dT%H:%M:%S')
            
        customer_data = list(config._iter_api_pages('/customers.json', 'customers', params))
        if not customer_data:
            config._log('info', 'Customer sync complete: 0 new customers to process.')
            return

        emails = {c.get('email') for c in customer_data if c.get('email')}
        phones = {c.get('phone') for c in customer_data if c.get('phone')}
        
        partners_by_email = {p.email: p for p in self.env['res.partner'].search([('email', 'in', list(emails))])}
        partners_by_phone = {p.phone: p for p in self.env['res.partner'].search([('phone', 'in', list(phones))])}

        for customer in customer_data:
            try:
                self._upsert_customer(config, customer, partners_by_email, partners_by_phone)
            except Exception as e:
                config._log('error', f"Customer upsert failed [{customer.get('id', '?')}]: {e}")
        
        config._log('info', f'Customer sync complete: {len(customer_data)} customers processed.')

    def _upsert_customer(self, config, customer_data, partners_by_email, partners_by_phone):
        """Create or update a single customer mapping and its corresponding res.partner."""
        haravan_id = str(customer_data.get('id'))
        if not haravan_id:
            return

        email = (customer_data.get('email') or '').strip().lower() or None
        phone = (customer_data.get('phone') or '').strip() or None

        partner = (partners_by_email.get(email) or partners_by_phone.get(phone))
        
        mapping = self.search([('config_id', '=', config.id), ('haravan_customer_id', '=', haravan_id)], limit=1)
        if not partner and mapping.odoo_partner_id:
            partner = mapping.odoo_partner_id
        
        # CHẶN TẠO MỚI: Nếu không có cả email lẫn phone, vÀ cũng không tìm thấy partner cũ từ mapping
        if not email and not phone and not partner:
            return

        partner_vals = self._prepare_partner_vals(config, customer_data)

        if not partner:
            return
        else:
            partner.write(partner_vals)

        mapping_vals = self._prepare_mapping_vals(config, customer_data, partner)
        if mapping:
            mapping.write(mapping_vals)
        else:
            self.create(mapping_vals)

    def _prepare_partner_vals(self, config, data):
        """Prepare a dictionary of values for creating/updating a res.partner."""
        email = (data.get('email') or '').strip().lower() or None
        phone = (data.get('phone') or '').strip() or None
        first_name = data.get('first_name', '')
        last_name = data.get('last_name', '')
        name = ' '.join(filter(None, [last_name, first_name])).strip() or f"Haravan Customer #{data.get('id')}"

        vals = {
            'name': name,
            'email': email,
            'phone': phone,
            'haravan_customer_id': str(data.get('id')),
            'legacy_revenue_amount': float(data.get('total_spent')),
            'comment': data.get('note') or '',
        }
        
        address = data.get('default_address') or (data.get('addresses') and data.get('addresses')[0]) or {}
        if address:
            vals.update({
                'street': address.get('address1'),
                'street2': address.get('address2'),
                'city': address.get('city'),
                'zip': address.get('zip'),
            })
            if address.get('country_code'):
                country = self.env['res.country'].search([('code', '=', address['country_code'])], limit=1)
                vals['country_id'] = country.id if country else config.default_customer_country_id.id
        
        return vals

    def _prepare_mapping_vals(self, config, data, partner):
        """Prepare a dictionary of values for creating/updating a haravan.customer mapping."""
        return {
            'config_id': config.id,
            'haravan_customer_id': str(data.get('id')),
            'odoo_partner_id': partner.id,
            'haravan_email': (data.get('email') or '').strip().lower() or None,
            'haravan_phone': (data.get('phone') or '').strip() or None,
            'haravan_first_name': data.get('first_name'),
            'haravan_last_name': data.get('last_name'),
            'haravan_orders_count': data.get('orders_count', 0),
            'haravan_total_spent': data.get('total_spent', 0.0),
            'haravan_tags': data.get('tags'),
            'haravan_note': data.get('note'),
            'sync_state': 'synced',
            'last_synced': fields.Datetime.now(),
        }

    def action_view_partner(self):
        """Action to open the form view of the linked Odoo partner."""
        self.ensure_one()
        if not self.odoo_partner_id:
            raise UserError(_('No Odoo Contact linked to this Haravan Customer.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'view_mode': 'form',
            'res_id': self.odoo_partner_id.id,
        }