# -*- coding: utf-8 -*-
{
    'name': 'Haravan Connector',
    'version': '19.0.1.0.0',
    'category': 'eCommerce/Connectors',
    'summary': 'Integrate Haravan eCommerce platform with Odoo 19',
    'author': 'NOS Technology',
    'website': 'https://nostech.vn',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'product',
        'sale_stock',
        'stock',
        'account',
        'sale_management',
    ],
    'data': [
        # Security
        'security/haravan_security.xml',
        'security/ir.model.access.csv',
        'data/haravan_cron.xml',
        # Views
        'views/haravan_config_views.xml',
        'views/haravan_webhook_views.xml',
        'views/haravan_product_views.xml',
        'views/haravan_order_views.xml',
        'views/haravan_log_views.xml',
        'views/haravan_customer_revenue_inventory_views.xml',
        'views/haravan_location_views.xml',
        'views/haravan_report_views.xml',
        'wizard/haravan_sync_wizard_views.xml',
        # Menus
        'views/haravan_menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'haravan_connector/static/src/css/haravan.css',
        ],
    },
    'images': ['static/description/icon.png'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
