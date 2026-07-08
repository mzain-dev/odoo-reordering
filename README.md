# Smart Reorder Advisor

**Odoo 17 — Version 17.0.5.0.0**  
Author: HMI Parts  
License: LGPL-3

---

## Overview

Smart Reorder Advisor is a demand-driven reorder suggestion engine built specifically for **spare parts shops**. Unlike Odoo's built-in Min/Max reordering rules, this module calculates monthly demand from actual sales history and generates smart purchase suggestions — no manual threshold setup required per SKU.

The module is **100% advisory**: it never auto-confirms purchase orders. Every suggested order passes through a buyer review step before anything is committed.

---

## Dependencies

```
base, stock, sale, purchase, mail
```

Optional: `openpyxl` (Python package) — required only for Excel export. If not installed, all other features work normally.

---

## Installation

1. Copy the `smart_reorder_advisor` folder into your Odoo `addons` path.
2. Update the app list: **Settings → Apps → Update App List**.
3. Search for "Smart Reorder Advisor" and click **Install**.
4. Go to **Smart Reorder → Configuration** and create a configuration record for your company.
5. The scheduled cron job is **disabled by default** — enable it manually once configuration is complete.

---

## User Roles

| Group               | Access                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------- |
| **Reorder User**    | Read suggestions, run wizards, export to Excel, view run history                            |
| **Reorder Manager** | All of the above + edit configuration, manage transfer lanes, clear stuck locks, create POs |

---

## Features

### Phase 1 — Core Intelligence

- **Monthly demand calculation** — uses actual sale order lines over a configurable window (3, 6, 9, or 12 months). Monthly units, not daily, which is correct for irregular spare parts demand.
- **Robust monthly demand forecasting** — calculates the monthly average with high-side spike rejection. Resistant to freak bulk-orders (high-side outliers are filtered out, while low/zero sales months are preserved as authentic demand data).
- **Months of stock calculation** — displays clean coverage months, floor-bounded at zero (no negative coverage months), with a 999 sentinel for positive net available with zero demand.
- **MOQ rounding** — suggested quantity is rounded up to the vendor's minimum order quantity from the pricelist.
- **Reorder value** — quantity × standard cost in company currency, with budget ranking.
- **Budget cap** — configurable weekly spend ceiling; suggestions ranked and flagged when the total exceeds the cap.
- **ABC classification** — automatic A/B/C class by monthly demand volume (thresholds configurable per company).
- **Dead stock detection** — products with zero sales for N months (configurable) are flagged separately.

### Phase 2 — Trend & Signals

- **Demand trend** — compares current period demand against a prior comparison window: Rising / Stable / Falling / New.
- **Trend percentage** — exact % change shown in list, form, and PDF report.
- **Confidence score** — computed from data completeness (history length, sales consistency, vendor data availability).
- **Delta tracking** — each regeneration computes the % change in suggested quantity vs the previous run; large swings are highlighted.
- **Needs Review flag** — auto-triage: suggestions where the suggested qty shifted by more than the configured threshold (default 50%) are flagged for buyer attention.
- **Per-month demand breakdown** — the Notes field includes a month-by-month sales table so buyers can see the raw data behind the forecast.
- **Seasonal comparison** — same period last year quantity shown alongside the current period.
- **Email PDF report** — optional: send the summary PDF to configured recipients after each cron run.

### Phase 3 — Advanced

- **Min/Max Reorder Policy** — replaces simple reorder points. Calculates Min Stock Level (Reorder Point) based on lead time and safety buffer, and Max Stock Level (Order-up-to Level) based on the order cycle. Suggestions are triggered only when net available stock drops below Min Level (or on-hand is negative).
- **One-click Draft PO** — from a suggestion, open a wizard to create a draft purchase order for the suggested quantity. Requires Reorder Manager role.
- **Auto-flag on negative stock** — when a delivery (stock picking) causes a product to go negative, the corresponding suggestion is automatically flagged as Critical in real time.
- **Vendor performance notes** — tracks stated lead time vs actual; shown on suggestion form and PDF report.
- **Stale data carry-forward** — if a warehouse fails during a cron run (e.g., missing vendor data), the previous run's suggestions are preserved rather than being wiped.
- **Per-warehouse transfer lead times** — configure inter-warehouse transfer lanes with lead time in days; factored into coverage calculations for multi-branch operations.
- **Configurable cron frequency** — Weekly / Bi-weekly / Monthly, configurable from the company config form (updates the scheduled action directly).
- **Bulk snooze** — select multiple suggestions in the list view and snooze them for 7 or 30 days via the Action menu.
- **Excel export** — export the current filtered list to a formatted `.xlsx` file with one click from the list view header. Requires `openpyxl`.
- **Last computed banner** — the suggestion list view shows a banner with the timestamp of the last completed analysis run.

---

## Models

| Model                         | Description                                             |
| ----------------------------- | ------------------------------------------------------- |
| `smart.reorder.suggestion`    | Core suggestion record — one per product/warehouse pair |
| `smart.reorder.config`        | Per-company configuration                               |
| `smart.reorder.cron.log`      | Run history — one record per cron/manual execution      |
| `smart.reorder.transfer.lane` | Inter-warehouse transfer lane with lead time            |
| `smart.reorder.wizard`        | Wizard: generate suggestions manually                   |
| `smart.reorder.po.wizard`     | Wizard: create draft PO from a suggestion               |
| `smart.reorder.export.wizard` | Wizard: export filtered suggestions to Excel            |

---

## Configuration Reference

| Setting                      | Default    | Description                                                                                                    |
| ---------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------- |
| Sales Analysis Period        | 6 months   | History window for demand calculation                                                                          |
| Default Lead Time            | 1.5 months | Used when vendor lead time is not set                                                                          |
| Safety Buffer                | 1.0 month  | Extra stock buffer beyond lead time demand                                                                     |
| Order Cycle                  | 1.0 month  | Reorder quantity cycle, determines Max Stock Level. Auto-derived from cron frequency if not overridden.       |
| Dead Stock Threshold         | 6 months   | No-movement period for dead stock flag                                                                         |
| Flag Dead Stock              | Yes        | Enable/disable dead stock detection                                                                            |
| Weekly Budget Cap            | —          | Spend ceiling; suggestions ranked when exceeded                                                                |
| ABC A Threshold              | —          | Monthly units/month to qualify as A class                                                                      |
| ABC B Threshold              | —          | Monthly units/month to qualify as B class                                                                      |
| Trend Comparison Window      | —          | Months to compare against for trend direction                                                                  |
| Cron Frequency               | Weekly     | How often the scheduled analysis runs                                                                          |
| Needs-Review Threshold       | 50%        | Delta % that triggers the Needs Review flag                                                                    |
| Overstock Ceiling            | 12.0 mos   | Ceiling in months for post-order stock coverage. Set to 0 to disable.                                          |
| Alt. Vendor Lead Margin      | 5 days     | Minimum lead time difference in days to suggest an alternative vendor. Set to 0 to disable.                    |
| Snapshot Retention           | 12 months  | Purge forecast snapshots older than this many months. Set to 0 to keep forever.                                |
| Send Email Report            | No         | Email PDF summary after each run                                                                               |
| Notify Users                 | —          | Recipients for email report                                                                                    |
| Allow Draft PO               | No         | Enable one-click Draft PO creation                                                                             |
| Auto-Flag Negative Stock     | No         | Flag suggestions when delivery causes negative qty                                                             |

---

## Reordering Policy Formulas

The reorder calculations are performed per product and warehouse based on the Min/Max Reorder Policy:

### 1. Robust Monthly Demand Forecast
Sales history is analyzed over the configured **Sales Analysis Period**. The forecasting engine calculates the robust monthly average with high-side outlier (spike) rejection:
- An outlier month is defined as a month whose sales exceed the median monthly sales by more than $3 \times$ the Median Absolute Deviation (MAD).
- Outlier months are excluded from the series.
- The **Average Monthly Demand** is calculated as the mean of the remaining (non-excluded) months.

### 2. Stock Position Metrics
- **Net Available Stock** is calculated by subtracting customer reservations (outgoing) from stock on hand and incoming supplies:
  $$\text{Net Available} = \text{Qty On Hand} + \text{Qty Incoming} - \text{Qty Outgoing}$$
- **Months of Stock** is the current supply coverage:
  $$\text{Months of Stock} = \frac{\text{Net Available}}{\text{Average Monthly Demand}}$$

### 3. Reorder Levels & Suggested Quantity
- **Min Stock Level (Reorder Point)**:
  $$\text{Min Stock Level} = \text{Average Monthly Demand} \times (\text{Lead Time Months} + \text{Safety Buffer Months})$$
- **Max Stock Level (Order-up-to Level)**:
  $$\text{Max Stock Level} = \text{Min Stock Level} + (\text{Average Monthly Demand} \times \text{Order Cycle Months})$$
- **Trigger**: A suggestion is triggered if $\text{Net Available} < \text{Min Stock Level}$ OR $\text{Qty On Hand} < 0$.
- **Suggested Reorder Quantity**:
  $$\text{Raw Qty} = \max(0.0, \text{Max Stock Level} - \text{Net Available})$$
  $$\text{Suggested Quantity} = \text{Round Up to Vendor MOQ}(\text{Raw Qty})$$

---

## Cron Job

The scheduled action **"Smart Reorder: Weekly Analysis"** is **disabled by default**. Enable it manually from:

> Settings → Technical → Scheduled Actions → Smart Reorder: Weekly Analysis

The cron processes all companies in a single run. Frequency is controlled from the company configuration form — no need to edit the scheduled action directly.

A **per-company run lock** prevents overlapping runs. If a run appears stuck, use the **"Clear Stuck Lock"** button on the configuration form (visible only when a lock is active). The lock auto-expires after 60 minutes.

---

## PDF Reports

Two report layouts are available from the suggestion list view:

- **Detail Report** — one page per product with full calculation breakdown, stock position, demand table, trend detail, and vendor info.
- **Summary Report** — one page per warehouse with KPI boxes (Critical / Urgent / Dead Stock / Need Reorder counts and total value) and a compact product table.

Both reports use DejaVu Sans font for reliable rendering of all characters across wkhtmltopdf.

---

## Run History

Every cron and manual run is logged in **Smart Reorder → Run History**. Each log record shows:

- Start and finish timestamp
- Trigger type (cron / manual)
- Status (success / partial / failed / aborted)
- Products processed and critical count
- Duration in seconds
- Error notes (if any)

---

## Security

All records are company-scoped via record rules — users in company A cannot see suggestions, configs, or run logs belonging to company B. The transfer lane model is scoped by the source warehouse's company.

---

## Known Limitations

- The cron frequency setting is system-wide (one `ir.cron` record). If multiple companies save different frequencies, the last save wins.
- Excel export requires `openpyxl` to be installed in the Odoo server's Python environment (`pip install openpyxl`). The rest of the module functions normally without it.
- The inter-warehouse transfer lead time is informational — it is factored into coverage calculations but does not create inter-company transfers automatically.

---

## Release Process

**Test execution is a mandatory gate — no zip/merge ships without it.** A regression once shipped where the test suite asserted an outcome the code could not produce, which only happens if the suite was never actually run before packaging. Tests nobody runs protect nothing.

1. **CI must be green.** Every push and PR runs `.github/workflows/tests.yml` (lint, then `--test-enable --test-tags=smart_reorder_advisor` against a fresh Odoo 17 + Postgres 15 service). This is enforced automatically and blocks merge on any lint or test failure — do not merge with a red or skipped CI run, and do not bypass branch protection to do so.
2. **Before cutting a release zip, additionally run the suite against a staging build restored from a recent production backup**, not just the CI's fresh test database:

   ```bash
   python odoo-bin --addons-path=<odoo-addons>,. \
     -d <staging_db_restored_from_prod> \
     -u smart_reorder_advisor \
     --test-enable --test-tags=smart_reorder_advisor \
     --stop-after-init --no-http --log-level=test
   ```

   Production-shaped data (multi-company records, large snapshot backlogs, real vendor/supplier configs) catches issues a clean test DB won't. Any `FAILED`/`ERROR` in the output is a release blocker — fix it or roll the change back out of the release, do not ship around it.
3. Only after both (1) and (2) are green does the module zip get packaged and merged/deployed.

The test suite itself must earn this gate by actually catching regressions — see `tests/test_reorder_suggestion.py` for the module's regression coverage, including: the one-time-sale review-flag scenario, single-month low-quantity (1–3 unit) averaging, bulk-regular products skipping the concentration review flag *and* confidence deduction, the draft-PO no-double-order guard, in-transit internal transfers counting as incoming stock, and dashboard multi-company access isolation.
