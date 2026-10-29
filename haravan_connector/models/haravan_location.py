# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class HaravanLocationMapping(models.Model):
    """
    Maps each Haravan warehouse/location to an Odoo stock.location.

    Haravan exposes locations via GET /locations.json.
    Each location has:
      id, name, address1, city, province, country_code, active
    """
    _name = 'haravan.location.mapping'
    _description = 'Haravan ↔ Odoo Location Mapping'
    _rec_name = 'haravan_location_name'
    _order = 'haravan_location_name'

    config_id = fields.Many2one(
        'haravan.config', string='Configuration',
        required=True, ondelete='cascade', index=True
    )
    haravan_location_id = fields.Char(
        string='Haravan Location ID',
        required=True, index=True,
        help='Numeric ID from Haravan /locations.json'
    )
    haravan_location_name = fields.Char(string='Haravan Location Name')
    haravan_address = fields.Char(string='Address')
    haravan_city = fields.Char(string='City')
    haravan_province = fields.Char(string='Province')
    haravan_country_code = fields.Char(string='Country Code')
    haravan_active = fields.Boolean(string='Active on Haravan', default=True)

    odoo_location_id = fields.Many2one(
        'stock.location', string='Odoo Stock Location',
        domain=[('usage', 'in', ['internal', 'transit'])],
        help='Internal or transit stock location in Odoo that corresponds to this Haravan location'
    )
    odoo_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Odoo Warehouse',
        compute='_compute_odoo_warehouse', store=True,
        help='Warehouse derived from the selected stock location'
    )

    sync_inventory = fields.Boolean(
        string='Sync Inventory', default=True,
        help='Include this location in inventory push/pull operations'
    )
    sync_direction = fields.Selection([
        ('odoo_to_haravan', 'Odoo → Haravan'),
        ('haravan_to_odoo', 'Haravan → Odoo'),
        ('bidirectional', 'Bidirectional'),
    ], string='Sync Direction', default='odoo_to_haravan')

    last_synced = fields.Datetime(string='Last Synced', readonly=True)
    notes = fields.Text(string='Notes')
    is_unavailable_quantity = fields.Boolean()

    _sql_constraints = [
        ('unique_haravan_location', 'UNIQUE(config_id, haravan_location_id)',
         'This Haravan location is already mapped for this configuration.'),
    ]
    location_id = fields.Many2one('stock.location', string="Location ID")
    is_transfer_sale = fields.Boolean(string="Is Transfer Sale")
    stock_upload_type = fields.Selection([
        ('qty_available',     'On-Hand (qty_available)'),
        ('virtual_available', 'Forecasted (virtual_available)'),
    ], string='Qty Type to Upload', default='qty_available',
       help='Which stock quantity field to read when uploading to Haravan.'
    )

    @api.depends('odoo_location_id')
    def _compute_odoo_warehouse(self):
        """Walk up location hierarchy to find the parent warehouse."""
        for rec in self:
            loc = rec.odoo_location_id
            warehouse = False
            while loc:
                warehouse = self.env['stock.warehouse'].search(
                    [('lot_stock_id', '=', loc.id)], limit=1
                ) or self.env['stock.warehouse'].search(
                    [('view_location_id', '=', loc.id)], limit=1
                )
                if warehouse:
                    break
                loc = loc.location_id
            rec.odoo_warehouse_id = warehouse

    @api.model
    def sync_locations(self, config):
        """
        Fetch all Haravan locations and create/update mapping records.
        Does NOT auto-assign Odoo locations — the user maps them manually
        or via action_auto_map().
        """
        try:
            data = config._api_get('/locations.json')
        except Exception as e:
            config._log('error', f'Location fetch failed: {e}')
            raise UserError(_('Could not fetch locations from Haravan: %s') % str(e))

        locations = (data.get('locations') or [])
        synced = 0
        for loc in locations:
            if not loc or not loc.get('id'):
                continue
            try:
                self._upsert_location(config, loc)
                synced += 1
            except Exception as e:
                _logger.warning('Location upsert failed [%s]: %s', loc.get('id'), e)

        config._log('info', f'Location sync complete: {synced} locations fetched from Haravan')
        return synced

    def _upsert_location(self, config, loc):
        haravan_id = str(loc.get('id') or "")
        mapping = self.search([
            ('config_id', '=', config.id),
            ('haravan_location_id', '=', haravan_id),
        ], limit=1)

        vals = {
            'config_id': config.id,
            'haravan_location_id': haravan_id,
            'haravan_location_name': str(loc.get('name') or f'Location {haravan_id}'),
            'haravan_address': str(loc.get('address1') or ""),
            'haravan_city': str(loc.get('city') or ""),
            'haravan_province': str(loc.get('province') or ""),
            'haravan_country_code': str(loc.get('country_code') or ""),
            'haravan_active': bool(loc.get('active', True)),
        }
        if mapping:
            mapping.write(vals)
        else:
            self.create(vals)

    def action_auto_map(self):
        """
        Best-effort auto assignment:
        1. Match by Haravan location name ↔ Odoo warehouse name
        2. Fall back to config.default_warehouse_id.lot_stock_id
        """
        mapped = 0
        for rec in self:
            if rec.odoo_location_id:
                continue 
            wh = self.env['stock.warehouse'].search(
                [('name', 'ilike', rec.haravan_location_name)], limit=1
            ) if rec.haravan_location_name else False

            if not wh:
                wh = rec.config_id.default_warehouse_id

            if wh and wh.lot_stock_id:
                rec.odoo_location_id = wh.lot_stock_id.id
                mapped += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Auto Map'),
                'message': _('%d location(s) mapped automatically.') % mapped,
                'type': 'success' if mapped else 'warning',
            }
        }

    def action_fetch_locations(self):
        """Button action: fetch from Haravan for the config of selected records."""
        configs = self.mapped('config_id') or self.env['haravan.config'].search(
            [('active', '=', True), ('state', '=', 'connected')]
        )
        total = 0
        for config in configs:
            total += self.sync_locations_from_haravan(config)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Haravan Locations'),
                'message': _('%d location(s) fetched.') % total,
                'type': 'success',
            }
        }

    @api.model
    def get_odoo_location(self, config, haravan_location_id):
        """
        Return the mapped Odoo stock.location for a given Haravan location ID.
        Falls back to config.default_warehouse_id.lot_stock_id when not mapped.
        Returns False if nothing is available.
        """
        haravan_location_id = str(haravan_location_id or "")
        if haravan_location_id:
            mapping = self.search([
                ('config_id', '=', config.id),
                ('haravan_location_id', '=', haravan_location_id),
                ('sync_inventory', '=', True),
            ], limit=1)
            if mapping and mapping.odoo_location_id:
                return mapping.odoo_location_id

        wh = config.default_warehouse_id
        if wh and wh.lot_stock_id:
            return wh.lot_stock_id

        return False

    def get_product_quantity(self, product):
        """
        Read the correct stock quantity for a product at this location,
        using the stock_upload_type field.
        Returns max(0, qty) — never negative.
        """
        self.ensure_one()
        if not product or not self.odoo_location_id:
            return 0
        upload_type = self.stock_upload_type or 'qty_available'
        try:
            qty = getattr(
                product.with_context(location=self.odoo_location_id.id),
                upload_type,
                0.0
            ) or 0.0
        except Exception as e:
            _logger.warning('get_product_quantity failed [%s / %s]: %s',
                            product.default_code, self.haravan_location_name, e)
            qty = 0.0
        return max(0, int(qty))
