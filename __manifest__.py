{
    'name': 'Smart Reorder Advisor',
    'version': '17.0.5.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'Monthly demand-based reorder suggestions — no Min/Max setup needed',
    'description': '''
Smart Reorder Advisor — Spare Parts Intelligence for Odoo 17
============================================================

DEMAND FORECASTING
  - Monthly sales history (spike-rejection, outlier exclusion, confidence score)
  - Outgoing customer reservations subtracted from net available stock
  - Predecessor demand rollup: superseded parts' history merges into successor's series
  - Seasonal demand note vs same period last year
  - Demand trend: Rising / Stable / Falling vs configurable prior period

MIN / MAX REPLENISHMENT POLICY
  - Min level = avg monthly demand × (lead time + safety buffer)
  - Max level = min level + avg monthly demand × order cycle
  - Reorder qty = max level − net available, rounded up to vendor MOQ
  - Review-period coverage auto-derived from cron frequency; manual value always wins

STOCK COVERAGE & OVERSTOCK GUARD
  - Months-of-stock after order computed and flagged when it exceeds ceiling
  - Inter-warehouse transfer recommendation with donor-surplus cap

VENDOR INTELLIGENCE
  - Vendor pricelist price (converted to company currency) used for budget ranking
  - Price-break selection: correct tier for suggested quantity
  - Fastest alternative vendor surfaced in notes for critical/urgent items only
  - Actual vs stated lead time tracker; dynamic lead time override on lateness
  - Overdue PO detection with days-late note

REAL-TIME FLAGGING
  - Delivery validation hook: provisional suggestion created immediately on negative stock
  - Receipt validation hook: lightweight stock/urgency refresh (no full re-analysis)

ADVISORY-ONLY OUTPUT
  - One-click Draft PO consolidation (never auto-confirms)
  - Excel export respecting multi-company record rules
  - PDF report emailed after each cron run

BACK-TESTING & OBSERVABILITY
  - Forecast snapshots compared to actual sales after one lead time
  - MAPE by ABC class and warehouse, visible on OWL dashboard
  - Configurable snapshot retention; scheduled auto-scorer
  - Run history with duration, error count, and per-warehouse error notes

CLASSIFICATION & BUDGET
  - ABC classification by monthly demand (configurable thresholds)
  - Dead stock detection and dedicated view
  - Budget cap with priority ranking; estimated purchase value (vendor price basis)
    ''',
    'author': 'HMI Parts',
    'depends': ['base', 'stock', 'sale', 'purchase', 'mail'],
    'data': [
        'security/smart_reorder_security.xml',
        'security/ir.model.access.csv',
        'data/smart_reorder_data.xml',
        'wizard/export_suggestions_wizard_views.xml',
        'views/reorder_config_views.xml',
        'views/reorder_suggestion_views.xml',
        'views/reorder_dashboard_views.xml',
        'views/product_template_views.xml',
        'views/cron_log_views.xml',
        'views/transfer_lane_views.xml',
        'views/forecast_snapshot_views.xml',
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
