{
    'name': 'Smart Reorder Advisor',
    'version': '17.0.2.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'Monthly demand-based reorder suggestions — no Min/Max setup needed',
    'description': '''
Smart Reorder Advisor for Spare Parts Shops
===========================================
PHASE 1 — Core Intelligence:
  - Monthly demand calculation (not daily — correct for spare parts)
  - MOQ rounding from vendor pricelist
  - Reorder value in company currency (qty × cost)
  - Dead stock detection and dedicated view
  - Budget cap with priority ranking
  - ABC classification by monthly demand

PHASE 2 — Trend & Automation:
  - Demand trend: ↑ Rising / → Stable / ↓ Falling vs prior period
  - Email PDF report delivery after each cron run
  - Branch/warehouse stock coverage (months of stock)
  - Budget-ranked sorting

PHASE 3 — Advanced:
  - One-click Draft PO creation from suggestion
  - Auto-flag when delivery causes negative stock (real-time)
  - Seasonal demand note vs same period last year
  - Vendor actual lead time tracker
  - 100% read-only advisory — never auto-confirms POs
    ''',
    'author': 'HMI Parts',
    'depends': ['base', 'stock', 'sale', 'purchase', 'mail'],
    'data': [
        'security/smart_reorder_security.xml',
        'security/ir.model.access.csv',
        'data/smart_reorder_data.xml',
        'views/reorder_config_views.xml',
        'views/reorder_suggestion_views.xml',
        'views/reorder_dashboard_views.xml',
        'views/product_template_views.xml',
        'wizard/generate_suggestions_wizard_views.xml',
        'wizard/smart_reorder_po_wizard_views.xml',
        'views/menu_views.xml',
        'report/reorder_report_template.xml',
        'report/reorder_report_action.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'smart_reorder_advisor/static/src/js/reorder_dashboard.js',
            'smart_reorder_advisor/static/src/xml/reorder_dashboard.xml',
            'smart_reorder_advisor/static/src/css/reorder_dashboard.css',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
