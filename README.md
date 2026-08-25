# Smart Reorder Advisor

**Odoo 17 — Version 17.0.5.4.0**  
Author: HMI Parts  
License: LGPL-3

---

## Overview

Smart Reorder Advisor is a demand-driven reorder suggestion engine built specifically for **spare parts shops**. Unlike Odoo's built-in Min/Max reordering rules, this module calculates monthly demand from actual sales history and generates smart purchase suggestions — no manual threshold setup required per SKU.

The module is **100% advisory**: it never auto-confirms or auto-sends a purchase order. Every suggested order passes through a human review step before anything is committed — including the weekly report itself, which is meant to be handed to the buyer (or, at a shop like HMI's, read and then acted on by phone/email), not treated as an automated purchasing pipeline.

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
5. Confirm the analysis window, lead time, safety buffer, and budget cap defaults are right for your business (Configuration → Company Settings).
6. The scheduled weekly analysis and the email report are **enabled by default** as of 17.0.5.4.0 (see *Automated Weekly Delivery* below) — set `notify_user_ids` before your first real run if you don't want an empty/test run to email anyone.

---

## User Roles

| Group               | Access                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------- |
| **Reorder User**    | Read suggestions, snooze, mark as ordered, export to Excel, view run history                |
| **Reorder Manager** | All of the above + run the analysis wizard, edit configuration, manage transfer lanes, clear stuck locks, create Draft POs |

---

## Features

### Core Intelligence

- **Monthly demand calculation** — uses actual sale order lines over a configurable window (3, 6, 9, or 12 months). Monthly units, not daily, which is correct for irregular spare parts demand.
- **Robust monthly demand forecasting** — calculates the monthly average with high-side spike rejection. Resistant to freak bulk-orders (high-side outliers are filtered out, while low/zero sales months are preserved as authentic demand data).
- **Months of stock calculation** — displays clean coverage months, floor-bounded at zero (no negative coverage months), with a 999 sentinel for positive net available with zero demand.
- **MOQ rounding** — suggested quantity is rounded up to the vendor's minimum order quantity from the pricelist.
- **Reorder value** — quantity × standard cost in company currency, with budget ranking.
- **Budget cap** — configurable weekly spend ceiling; suggestions ranked and flagged when the total exceeds the cap.
- **ABC classification** — automatic A/B/C class by monthly demand volume (thresholds configurable per company).
- **Dead stock detection** — products with zero sales for N months (configurable) are flagged separately.

### Trend & Signals

- **Demand trend** — compares current period demand against a prior comparison window: Rising / Stable / Falling / New.
- **Trend percentage** — exact % change shown in list, form, and PDF reports.
- **Confidence score** — computed from data completeness (history length, sales consistency, vendor data availability).
- **Delta tracking** — each regeneration computes the % change in suggested quantity vs the previous run; large swings are highlighted.
- **Needs Review flag** — auto-triage: suggestions where the suggested qty shifted by more than the configured threshold, where negative stock alone is driving a suggestion with no real demand, where dead stock still shows a positive reorder qty, or where vendor MOQ forces overstocked coverage, are flagged for buyer attention.
- **Per-month demand breakdown** — the Notes field includes a month-by-month sales table so buyers can see the raw data behind the forecast.
- **Seasonal comparison** — same period last year quantity shown alongside the current period.

### Cost Intelligence

- **Last Purchase Cost** — the unit price actually paid on the most recent confirmed Purchase Order for each product (converted to the product's UoM and company currency), tracked separately from the vendor's pricelist quote and from standard/AVCO cost.
- **Effective Unit Cost** — the best available cost for ordering decisions, in priority order: Last Purchase Cost (what was actually paid) → Vendor Price (pricelist) → Standard Cost. Used anywhere the module needs one practical cost figure — the boss's weekly report, budget ranking display, etc.
- **Price Discrepancy Flag** — when a vendor's pricelist price and the last actual purchase cost diverge by more than 15% (a fixed threshold, not configurable by design), the suggestion is flagged. Only fires when both figures are actually on record — never flags a missing value against a real one.
- **Cost Detail page** — Standard Cost, Vendor Price, Last Purchase Cost, and Effective Unit Cost shown side by side on the suggestion form, along with the discrepancy check.

### Min/Max Reorder Policy

- Replaces simple reorder points. Calculates Min Stock Level (Reorder Point) based on lead time and safety buffer, and Max Stock Level (Order-up-to Level) based on the order cycle. Suggestions are triggered only when net available stock drops below Min Level (or on-hand is negative).
- **One-click Draft PO** — from a suggestion, create a draft purchase order for the suggested quantity directly (no wizard). Requires a vendor to actually be assigned to the product — it will **not** silently fall back to a configured Default Vendor (see *Vendor Fallback Guard* below). Requires Reorder Manager role.
- **Consolidated Draft POs** — select multiple suggestions from the list and generate one draft PO per vendor/company/warehouse in one action. This is the deliberate place the Default Vendor fallback applies, and it shows exactly how many items (and to which vendor) will use that fallback before anything is created.
- **Auto-flag on negative stock** — when a delivery (stock picking) causes a product to go negative, the corresponding suggestion is automatically flagged as Critical in real time, using a lightweight provisional calculation (not a full re-analysis).
- **Vendor performance notes** — tracks stated lead time vs actual (on-demand refresh, not part of the bulk weekly run); shown on the suggestion form and PDF reports.
- **Stale data carry-forward** — if a warehouse fails during a cron run (e.g., missing vendor data), the previous run's suggestions are preserved rather than being wiped.
- **Per-warehouse transfer lead times** — configure inter-warehouse transfer lanes with lead time in days; factored into coverage calculations for multi-branch operations.
- **Mark as Ordered** — one-click, no PO and no wizard: records that the order was already placed directly (e.g. by phone/email/vendor site), suppressing the suggestion for exactly the *next* analysis run. If it's still not resolved after that, it resurfaces automatically rather than staying silently hidden — this exists specifically to bridge the gap between "the order was placed" and "someone logged the purchase in Odoo."
- **Stale Draft PO alert** — flags a suggestion when a linked Draft PO has sat unconfirmed longer than a configurable threshold (default 7 days, 0 disables it), refreshed on each analysis run.
- **Snooze** — 7/30-day quick actions or a custom date + reason, individually or in bulk from the list view's Action menu.

### Reports & Exports

- **Boss's Weekly Order Report** — a plain, vendor-grouped PDF: one block per vendor, listing product name (never a vendor part code), suggested quantity, urgency, and Effective Unit Cost, with a subtotal per vendor and a grand total. Suggestions with no vendor, a designated placeholder/"temporary" vendor, or that are Dead Stock flagged Critical (a real but misleading combination — see *Configuration Reference*) are routed to a separate "Needs Attention Before Ordering" section instead of being presented as a normal, urgent line item.
- **Detail Report** — one page per product with full calculation breakdown, stock position, demand table, trend detail, and vendor info.
- **Summary Report** — one page per warehouse with KPI boxes (Critical / Urgent / Dead Stock / Need Reorder counts and total value) and a compact, all-columns product table. This is the internal/technical report; the Boss's Weekly Order Report is the one meant for someone who doesn't use Odoo day to day.
- **Excel export** — export the current filtered list to a formatted `.xlsx` file with one click from the list view header. Two formats: **Essential** (default — the short working-list column set: product, warehouse, on-hand, suggested qty, vendor, unit cost, reorder value, dead-stock flag, last sale date) and **Full** (every column, including analysis internals). Requires `openpyxl`.
- **Last computed banner** — the suggestion list view shows a banner with the timestamp of the last completed analysis run.

### Automated Weekly Delivery

- The scheduled action **"Smart Reorder: Weekly Analysis"** and each company's **Email PDF Report After Analysis** setting are both **enabled by default**.
- The email is gated by the existing "Notify Only for Critical / Urgent Items" toggle — it is **not** sent every week regardless of contents; it only goes out when at least one Critical or Urgent suggestion exists, so recipients don't get trained to skim past a routine near-empty email. Disable the toggle to send every week regardless.
- The email attaches the **Boss's Weekly Order Report**, not the technical Summary Report — the technical one stays available as a manual Print option.
- Upgrading from an installation older than 17.0.5.4.0 flips both defaults on for you via a migration script — they don't silently turn on only for fresh installs.

### Back-testing (Forecast Snapshots)

- Forecast snapshots are recorded at analysis time and scored against actual sales once one lead time has passed, giving MAPE (Mean Absolute Percentage Error) by ABC class and warehouse.
- **Deferred by design**: the scoring scheduled action stays off by default — there needs to be enough analysis history for snapshots to actually mature before scoring them is meaningful. Revisit once the weekly cron has been running for a couple of months.

---

## Models

| Model                                  | Description                                                  |
| --------------------------------------- | ------------------------------------------------------------- |
| `smart.reorder.suggestion`              | Core suggestion record — one per product/warehouse pair       |
| `smart.reorder.config`                  | Per-company configuration                                     |
| `smart.reorder.cron.log`                | Run history — one record per cron/manual execution            |
| `smart.reorder.transfer.lane`           | Inter-warehouse transfer lane with lead time                  |
| `smart.reorder.forecast.snapshot`       | Forecast back-testing snapshot, scored after one lead time    |
| `smart.reorder.observability.dashboard` | Transient — cron failure rate / duration / hook error summary |
| `smart.reorder.wizard`                  | Wizard: generate suggestions manually                         |
| `smart.reorder.po.wizard`               | Wizard: create consolidated draft POs from selected suggestions |
| `smart.reorder.export.wizard`           | Wizard: export filtered suggestions to Excel (Essential/Full) |
| `smart.reorder.snooze.wizard`           | Wizard: snooze selected suggestions with a custom date/reason |

---

## Configuration Reference

| Setting                          | Default    | Description                                                                                                    |
| --------------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------- |
| Sales Analysis Period             | 6 months   | History window for demand calculation                                                                          |
| Default Lead Time                 | 1.5 months | Used when vendor lead time is not set                                                                          |
| Safety Buffer                     | 1.0 month  | Extra stock buffer beyond lead time demand                                                                     |
| Order Cycle                       | 1.0 month  | Reorder quantity cycle, determines Max Stock Level. Auto-derived from cron frequency if not overridden.        |
| Dead Stock Threshold              | 6 months   | No-movement period for dead stock flag                                                                         |
| Flag Dead Stock                   | Yes        | Enable/disable dead stock detection                                                                            |
| Weekly Budget Cap                 | —          | Spend ceiling; suggestions ranked when exceeded. Set to 0 to disable.                                          |
| ABC A Threshold                   | 5.0        | Monthly units/month to qualify as A class                                                                      |
| ABC B Threshold                   | 1.0        | Monthly units/month to qualify as B class                                                                      |
| Trend Comparison Window           | 3 months   | Months to compare against for trend direction                                                                  |
| Cron Frequency                    | Weekly     | How often the scheduled analysis runs. **System-wide, not per-company** — see *Known Limitations*.             |
| Needs-Review Threshold            | 50%        | Delta % that triggers the Needs Review flag. Set to 0 to disable this specific trigger.                        |
| Overstock Ceiling                 | 12.0 mos   | Ceiling in months for post-order stock coverage. Set to 0 to disable.                                          |
| Alt. Vendor Lead Margin           | 5 days     | Minimum lead time difference in days to suggest an alternative vendor. Set to 0 to disable.                    |
| Snapshot Retention                | 6 months   | Purge forecast snapshots older than this many months. Set to 0 to keep forever.                                |
| Send Email Report                 | **Yes**    | Email the Weekly Order List PDF after each run (gated by the Critical/Urgent toggle below)                     |
| Notify Only for Critical/Urgent   | Yes        | Governs both inbox alerts and the weekly email — no send at all when nothing is Critical/Urgent                |
| Notify Users                      | —          | Recipients for inbox alerts + the email report                                                                 |
| Allow Draft PO                    | No         | Enable one-click Draft PO creation                                                                             |
| Default Vendor (for Draft POs)    | —          | Used only by the *consolidated* PO wizard when a product has no vendor — never by the single-click button      |
| Stale Draft PO Alert Threshold    | 7 days     | Flag a suggestion when its linked Draft PO has sat unconfirmed this long. Set to 0 to disable.                 |
| Temporary/Placeholder Vendors     | —          | Vendors that mean "no real vendor assigned yet" (e.g. a "Temporary Supplier" record) — suggestions tagged with one of these are excluded from the boss's report and routed to "Needs Attention" instead |
| Auto-Flag Negative Stock          | Yes        | Flag suggestions in real time when a delivery causes negative qty                                              |

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

### 4. Effective Unit Cost (fallback chain)
$$\text{Effective Unit Cost} = \text{Last Purchase Cost if} > 0 \text{, else Vendor Price if} > 0 \text{, else Standard Cost}$$

---

## Cron Job

The scheduled action **"Smart Reorder: Weekly Analysis"** is **enabled by default** as of 17.0.5.4.0. It runs every Monday by default; frequency is controlled from the company configuration form (Weekly / Bi-Weekly / Monthly) — no need to edit the scheduled action directly.

⚠️ **This setting is not actually per-company**, even though the field lives on the per-company configuration record — saving it updates every company's config and the one shared scheduled action, since a single `ir.cron` record only supports one interval. Changing it now pops a warning listing exactly which other companies will also be affected, instead of relying on help text alone.

A **per-company run lock** prevents overlapping runs. If a run appears stuck, use the **"Clear Stuck Lock"** button on the configuration form (visible only when a lock is active). The lock auto-expires after 60 minutes.

---

## PDF Reports

Three report layouts are available from the suggestion list view's Print menu:

- **Weekly Order List (Boss Report)** — vendor-grouped, plain-language, no vendor part codes. This is the one automatically emailed.
- **Detail Report** — one page per product with full calculation breakdown, stock position, demand table, trend detail, and vendor info.
- **Summary Report** — one page per warehouse with KPI boxes and a compact, all-columns product table (internal/technical use).

All reports use DejaVu Sans font for reliable rendering of all characters across wkhtmltopdf.

---

## Run History

Every cron and manual run is logged in **Smart Reorder → Run History**. Each log record shows:

- Start and finish timestamp
- Trigger type (cron / manual)
- Status (completed / completed with errors / running / aborted)
- Products processed and critical count
- Duration in seconds
- Error notes (if any)

---

## Security

All records are company-scoped via record rules — users in company A cannot see suggestions, configs, run logs, transfer lanes, or forecast snapshots belonging to company B.

---

## Known Limitations

- **Lost Sales Visibility**: Forecasts use actual delivered sales quantity as demand. If a product was out of stock and a customer walked away without a sale line being created, that lost demand is invisible to the engine, which can lead to continued under-forecasting. There is no lost-demand capture feature yet (evaluated and deliberately deferred — a walk-in "logged missed sale" flow is a reasonable follow-up but is a distinct feature, not a quick fix).
- **Order-then-log blind spot**: for a shop where purchasing happens outside Odoo (order placed by phone/email/vendor website, then logged afterward), the system has no visibility into an order between the moment it's placed and the moment someone logs it. *Mark as Ordered* and running "Generate Analysis" right after logging an arrival are the practical mitigations — a workflow habit, not something the system can fully close on its own.
- **Cron frequency is system-wide**, not per-company (see *Cron Job* above) — a single shared scheduled action only supports one interval. Changing it now shows an explicit warning before saving, but the underlying architecture is unchanged; true per-company scheduling would require splitting into multiple `ir.cron` records, which hasn't been done.
- Excel export requires `openpyxl` to be installed in the Odoo server's Python environment (`pip install openpyxl`). The rest of the module functions normally without it.
- The inter-warehouse transfer lead time is informational — it is factored into coverage calculations but does not create inter-company transfers automatically; a manager still clicks "Create Internal Transfer" on the suggestion.
- Superseded-part handling (`superseded_by_id` on the product template) exists in the data model and calculation engine but is not populated in most real-world catalogs yet — it has no effect until products are actually linked.

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

The test suite itself must earn this gate by actually catching regressions — see `tests/test_reorder_suggestion.py` for the module's regression coverage, including: the one-time-sale review-flag scenario, single-month low-quantity (1–3 unit) averaging, bulk-regular products skipping the concentration review flag *and* confidence deduction, the draft-PO no-double-order guard and vendor-fallback guard, in-transit internal transfers counting as incoming stock, dashboard multi-company access isolation, the Mark-as-Ordered one-cycle suppression lifecycle, the price-discrepancy fallback chain, and the boss's weekly report's vendor-grouping/routing logic.
