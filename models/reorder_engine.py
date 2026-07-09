from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import math
import logging
import statistics

_logger = logging.getLogger(__name__)


def calc_months_of_stock(qty_available, avg_monthly_demand):
    """
    Months Left / Months of Stock calculation helper.
    Rules:
    1. Net available <= 0 returns 0.0 (coverage cannot be negative).
    2. Net available > 0 with positive demand returns net available / average monthly demand.
    3. Net available > 0 with zero demand returns 999.0.
    """
    if qty_available <= 0:
        return 0.0
    if avg_monthly_demand > 0:
        return qty_available / avg_monthly_demand
    return 999.0


def round_to_moq(qty, moq):
    """Round qty UP to nearest MOQ multiple. Never returns negative."""
    if moq <= 0 or qty <= 0:
        return max(0.0, qty)
    return math.ceil(qty / moq) * moq


def classify_abc(avg_monthly_demand, config):
    if avg_monthly_demand >= config.abc_a_threshold:
        return 'A'
    if avg_monthly_demand >= config.abc_b_threshold:
        return 'B'
    return 'C'


def determine_urgency(qty_on_hand, months_of_stock, avg_monthly,
                      lead_months, is_dead_stock, suggested_qty):
    if qty_on_hand < 0:
        return 'critical'
    if is_dead_stock and qty_on_hand > 0:
        return 'dead'
    if avg_monthly > 0 and months_of_stock < lead_months:
        return 'urgent'
    if suggested_qty > 0:
        return 'normal'
    return 'ok'


def calc_trend(current_qty, prev_qty, analysis_months, comparison_months):
    """
    Pure calculation — no DB. Called inside loop after bulk pre-fetch.
    Returns (trend_selection, trend_pct).
    """
    current_per_month = current_qty / max(analysis_months, 1)
    prev_per_month    = prev_qty    / max(comparison_months, 1)

    if prev_per_month == 0 and current_per_month == 0:
        return ('new', 0.0)
    if prev_per_month == 0:
        return ('up', 100.0)

    pct = ((current_per_month - prev_per_month) / prev_per_month) * 100
    if pct >= 15:
        return ('up', round(pct, 1))
    if pct <= -15:
        return ('down', round(pct, 1))
    return ('stable', round(pct, 1))


def calc_seasonal_note(current_qty, ly_qty):
    """Pure calculation — no DB."""
    if ly_qty == 0:
        return 'No data for same period last year'
    pct = ((current_qty - ly_qty) / ly_qty) * 100
    if pct >= 20:
        return f'↑ {pct:.0f}% vs same period last year (seasonal spike?)'
    if pct <= -20:
        return f'↓ {abs(pct):.0f}% vs same period last year (seasonal dip?)'
    return f'≈ Consistent with same period last year ({pct:+.0f}%)'


def compute_robust_monthly_demand(monthly_series, min_spike_size=10.0):
    """
    Forecast monthly demand from a most-recent-first list of monthly quantities
    using average + MAD (median absolute deviation) outlier rejection.

    min_spike_size: a month can only be classified as a one-time spike (and
    excluded) if its quantity is at least this large. Prevents a single small
    sale (e.g. 1 unit sold once) from being treated as a "4x the next-highest
    month" spike merely because the next-highest month is zero.
    """
    if not monthly_series:
        return 0.0, 0, None, []

    med = statistics.median(monthly_series)
    distances = [abs(x - med) for x in monthly_series]
    mad = statistics.median(distances)

    if mad == 0:
        total_sales = sum(monthly_series)
        is_spike = False
        if total_sales > 0.0:
            max_val = max(monthly_series)
            max_idx = monthly_series.index(max_val)
            other_months = [x for idx, x in enumerate(monthly_series) if idx != max_idx]
            next_highest = max(other_months) if other_months else 0.0
            is_spike = (
                max_val >= min_spike_size
                and max_val >= 0.5 * total_sales
                and max_val >= 4 * max(next_highest, 1.0)
            )

        if is_spike:
            max_idx = monthly_series.index(max_val)
            clean = [x for idx, x in enumerate(monthly_series) if idx != max_idx]
            forecast = sum(clean) / len(clean) if clean else 0.0
            excluded_count = 1
            direction = 'high'
            excluded_flags = [False] * len(monthly_series)
            excluded_flags[max_idx] = True
        else:
            forecast = sum(monthly_series) / len(monthly_series)
            excluded_count = 0
            direction = None
            excluded_flags = [False] * len(monthly_series)
    else:
        excluded_flags = [x - med > 3 * mad for x in monthly_series]
        clean    = [x for x, ex in zip(monthly_series, excluded_flags) if not ex]
        excluded = [x for x, ex in zip(monthly_series, excluded_flags) if ex]

        if not excluded:
            forecast = sum(monthly_series) / len(monthly_series)
            excluded_count = 0
            direction = None
        else:
            forecast = sum(clean) / len(clean)
            excluded_count = len(excluded)
            direction = 'high'

    total_sales = sum(monthly_series)
    if forecast == 0.0 and total_sales > 0.0 and excluded_count == 0:
        forecast = total_sales / len(monthly_series)

    return forecast, excluded_count, direction, excluded_flags


def calc_delta_pct(old_qty, new_qty):
    if old_qty == 0:
        return 100.0 if new_qty > 0 else 0.0
    return ((new_qty - old_qty) / old_qty) * 100.0


def compute_needs_review(avg_monthly, suggested_qty, is_dead_stock, delta_pct, delta_threshold,
                         mos_after_order=0.0, overstock_ceiling_months=0.0, sales_pattern=False,
                         has_bulk_concentration=False):
    reasons = []
    if delta_threshold > 0 and abs(delta_pct) > delta_threshold:
        reasons.append(
            f'Suggested qty changed {delta_pct:+.0f}% vs last run '
            f'(threshold {delta_threshold:.0f}%).'
        )
    if avg_monthly == 0 and suggested_qty > 0:
        reasons.append('No real monthly demand — only negative stock is driving this suggestion.')
    if is_dead_stock and suggested_qty > 0:
        reasons.append('Flagged as dead stock but still has a positive reorder suggestion.')
    if overstock_ceiling_months > 0 and mos_after_order > overstock_ceiling_months:
        reasons.append(
            f'Vendor MOQ forces {mos_after_order:.1f} months of cover '
            f'(ceiling {overstock_ceiling_months:.1f} months).'
        )
    if sales_pattern == 'one_time_big_order':
        reasons.append('One-time big order detected — replenishment has been suppressed.')
    if has_bulk_concentration:
        reasons.append('Single month carries half or more of total demand (verify whether this bulk order repeats).')
    return (bool(reasons), ' '.join(reasons))


def compute_confidence_score(monthly_series, excluded_months, reorder_behavior='system'):
    ZERO_MONTH_PENALTY     = 5
    OUTLIER_MONTH_PENALTY  = 15
    NEW_PRODUCT_PENALTY    = 10
    CONCENTRATION_PENALTY  = 30

    total_months    = len(monthly_series)
    zero_months     = sum(1 for x in monthly_series if x == 0)
    non_zero_months = total_months - zero_months
    total_sales     = sum(monthly_series)

    score = 100.0
    reasons = []

    if zero_months:
        score -= zero_months * ZERO_MONTH_PENALTY
        reasons.append(
            f'{zero_months} of {total_months} months had zero sales (-{zero_months * ZERO_MONTH_PENALTY}).'
        )
    if excluded_months:
        score -= excluded_months * OUTLIER_MONTH_PENALTY
        reasons.append(
            f'{excluded_months} month(s) excluded as an outlier (-{excluded_months * OUTLIER_MONTH_PENALTY}).'
        )
    if non_zero_months <= 1:
        score -= NEW_PRODUCT_PENALTY
        reasons.append(
            f'Very limited sales history — only {non_zero_months} month(s) with any '
            f'sales (-{NEW_PRODUCT_PENALTY}).'
        )
    is_concentrated = total_sales > 0 and any(m >= 0.5 * total_sales for m in monthly_series)
    if is_concentrated:
        if reorder_behavior == 'bulk_regular':
            reasons.append(
                'Demand concentrated in a single month, but no penalty applied — '
                'buyer confirmed the product as "Customer Buys in Bulk Regularly".'
            )
        else:
            score -= CONCENTRATION_PENALTY
            reasons.append(
                f'Demand heavily concentrated in a single month (-{CONCENTRATION_PENALTY}).'
            )

    score = max(0.0, min(100.0, score))
    reason = ' '.join(reasons) if reasons else 'Full clean history — no deductions.'
    return score, reason


def build_vendor_perf_note(actual_avg_days, stated_lead_days, with_emoji=False):
    actual_avg_days_val = actual_avg_days or 0.0
    stated_lead_days = int(stated_lead_days)
    diff = actual_avg_days_val - stated_lead_days

    if diff > 5:
        prefix = '⚠️ ' if with_emoji else ''
        return f'{prefix}Typically {diff:.0f} days late (avg {actual_avg_days_val:.0f}d vs stated {stated_lead_days}d).'
    elif diff < -5:
        if with_emoji:
            return f'✅ Delivers {abs(diff):.0f} days early on average (avg {actual_avg_days_val:.0f}d).'
        else:
            return f'Delivers {abs(diff):.0f} days early (avg {actual_avg_days_val:.0f}d).'
    else:
        if with_emoji:
            return f'✅ Delivers on time (avg {actual_avg_days_val:.0f}d vs stated {stated_lead_days}d).'
        else:
            return f'On time (avg {actual_avg_days_val:.0f}d vs stated {stated_lead_days}d).'


def calculate_product_suggestion(
    product_id, product_code, product_name, tmpl_id, is_superseded, successor_display_name,
    company_id, warehouse_id, dates, config_data, warehouses_list,
    monthly_series, qty_on_hand, qty_incoming, qty_outgoing, cost,
    last_sale, current_month_sales, prev_qty, ly_qty,
    tmpl_suppliers, primary_vendor_info, actual_avg_days,
    overdue_lines, predecessors, predecessor_names,
    global_stocks, global_sales, lane_lead_times,
    partner_names_map, currency_convert_fn, reorder_behavior='system'
):
    if reorder_behavior == 'bulk_regular':
        avg_monthly = sum(monthly_series) / len(monthly_series) if monthly_series else 0.0
        excluded_months = 0
        excluded_direction = False
        excluded_flags = [False] * len(monthly_series)
    else:
        avg_monthly, excluded_months, excluded_direction, excluded_flags = (
            compute_robust_monthly_demand(
                monthly_series,
                min_spike_size=config_data.get('min_spike_size', 10.0),
            )
        )
    qty_available  = qty_on_hand + qty_incoming - qty_outgoing
    total_sold     = sum(monthly_series)
    has_bulk_concentration = total_sold > 0 and any(m >= 0.5 * total_sold for m in monthly_series)
    clean_total_sold = sum(x for x, ex in zip(monthly_series, excluded_flags) if not ex)
    analysis_months = dates['analysis_months']
    comparison_months = dates['comparison_months']
    date_from = dates['date_from']
    date_to   = dates['date_to']

    # Sales Pattern classification
    if total_sold == 0.0:
        sales_pattern = 'new'
    elif excluded_months > 0:
        if clean_total_sold > 0.0:
            sales_pattern = 'big_order_mixed'
        else:
            sales_pattern = 'one_time_big_order'
    else:
        active_months = sum(1 for x in monthly_series if x > 0.0)
        if active_months >= len(monthly_series) / 2.0:
            sales_pattern = 'regular'
        else:
            sales_pattern = 'sometimes'

    # Vendor info
    if primary_vendor_info:
        v_partner_id, v_lead_days, v_moq, v_price, v_currency_id = primary_vendor_info
    else:
        v_partner_id = False
        v_lead_days = config_data['default_lead_time_months'] * 30
        v_moq = 1.0
        v_price = 0.0
        v_currency_id = False

    lead_months = v_lead_days / 30.0
    stated_lead_days = int(v_lead_days)
    moq = v_moq

    actual_avg_days_val = actual_avg_days or 0.0
    vendor_perf_note = ''
    if config_data['track_vendor_performance'] and v_partner_id:
        if actual_avg_days_val:
            vendor_perf_note = build_vendor_perf_note(actual_avg_days_val, stated_lead_days, with_emoji=False)
            diff = actual_avg_days_val - stated_lead_days
            if diff > 5:
                lead_months = actual_avg_days_val / 30.0

    # Months of stock
    months_of_stock = calc_months_of_stock(qty_available, avg_monthly)

    # Dead stock
    if last_sale:
        rd = relativedelta(date_to, last_sale)
        months_since = rd.years * 12 + rd.months
    else:
        months_since = 0

    if last_sale:
        is_dead = (months_since >= config_data['dead_stock_months'] and config_data['flag_dead_stock'])
    else:
        is_dead = (qty_on_hand > 0 and config_data['flag_dead_stock'])

    # Core formula (Min/Max Reorder Policy)
    min_level = avg_monthly * (lead_months + config_data['safety_buffer_months'])
    max_level = min_level + avg_monthly * config_data['order_cycle_months']
    is_triggered = (qty_available < min_level) or (qty_on_hand < 0)
    raw_qty = max(0.0, max_level - qty_available) if is_triggered else 0.0
    suggested_qty = round_to_moq(raw_qty, moq)

    # Price break selection (Feature 13)
    if tmpl_id and tmpl_suppliers and v_partner_id:
        partner_lines = [
            r for r in tmpl_suppliers
            if r.get('partner_id') and r['partner_id'][0] == v_partner_id
        ]
        if partner_lines:
            lines_below_or_equal = [r for r in partner_lines if (r['min_qty'] or 0.0) <= suggested_qty]
            if lines_below_or_equal:
                matching_line = max(lines_below_or_equal, key=lambda x: x['min_qty'] or 0.0)
            else:
                matching_line = min(partner_lines, key=lambda x: x['min_qty'] or 0.0)

            if matching_line:
                v_price = matching_line['price'] or 0.0
                v_currency_id = matching_line['currency_id'][0] if matching_line.get('currency_id') else False
                new_lead_days = matching_line['delay'] or (config_data['default_lead_time_months'] * 30)
                
                if new_lead_days != stated_lead_days:
                    stated_lead_days = int(new_lead_days)
                    lead_months = new_lead_days / 30.0
                    if config_data['track_vendor_performance'] and v_partner_id:
                        if actual_avg_days_val:
                            vendor_perf_note = build_vendor_perf_note(actual_avg_days_val, stated_lead_days, with_emoji=False)
                            diff = actual_avg_days_val - stated_lead_days
                            if diff > 5:
                                lead_months = actual_avg_days_val / 30.0
                    
                    min_level = avg_monthly * (lead_months + config_data['safety_buffer_months'])
                    max_level = min_level + avg_monthly * config_data['order_cycle_months']
                    is_triggered = (qty_available < min_level) or (qty_on_hand < 0)
                    raw_qty = max(0.0, max_level - qty_available) if is_triggered else 0.0
                    suggested_qty = round_to_moq(raw_qty, moq)

    # Currency conversion
    price_company_currency = currency_convert_fn(v_price, v_currency_id)

    # Fastest Alternative Vendor Selection (Feature 13)
    alt_vendor_id_val = False
    alt_vendor_lead_days_val = 0
    if tmpl_id and tmpl_suppliers:
        other_partners = set(
            r['partner_id'][0]
            for r in tmpl_suppliers
            if r.get('partner_id') and r['partner_id'][0] != v_partner_id
        )
        fastest_alt_vendor_id = False
        fastest_alt_lead_days = 999999
        for alt_pid in other_partners:
            alt_lines = [
                r for r in tmpl_suppliers
                if r.get('partner_id') and r['partner_id'][0] == alt_pid
            ]
            lines_below_or_equal = [r for r in alt_lines if (r['min_qty'] or 0.0) <= suggested_qty]
            if lines_below_or_equal:
                matching_alt_line = max(lines_below_or_equal, key=lambda x: x['min_qty'] or 0.0)
            else:
                matching_alt_line = min(alt_lines, key=lambda x: x['min_qty'] or 0.0)
            
            if matching_alt_line:
                alt_delay = matching_alt_line['delay'] or (config_data['default_lead_time_months'] * 30)
                if alt_delay < fastest_alt_lead_days:
                    fastest_alt_lead_days = alt_delay
                    fastest_alt_vendor_id = alt_pid
        
        margin = config_data['alt_vendor_lead_margin_days']
        if fastest_alt_vendor_id and (stated_lead_days - fastest_alt_lead_days) >= margin:
            alt_vendor_id_val = fastest_alt_vendor_id
            alt_vendor_lead_days_val = fastest_alt_lead_days

    # Suppress automatic reorder quantity for "One-Time Big Order Only" parts
    # or if behavior is "Order Only Against Customer Order"
    if sales_pattern == 'one_time_big_order' or reorder_behavior == 'against_order':
        suggested_qty = 0.0

    reorder_needed = suggested_qty > 0 or qty_on_hand < 0

    class DictWrapper(object):
        def __init__(self, d):
            self.__dict__ = d

    if is_superseded:
        suggested_qty = 0.0
        reorder_needed = False
        abc_class = 'C'
        if qty_on_hand < 0:
            # Negative stock is promised or shipped stock that doesn't exist —
            # that must never be hidden under a "Dead Stock" label, even for a
            # superseded part. No replenishment of this (superseded) part is
            # still suggested; the shortfall must be resolved via the
            # successor or a stock correction, not by reordering this SKU.
            is_dead = False
            urgency = 'critical'
        else:
            is_dead = True
            urgency = 'dead'
    else:
        abc_class = classify_abc(avg_monthly, DictWrapper(config_data))
        urgency = determine_urgency(
            qty_on_hand, months_of_stock, avg_monthly,
            lead_months, is_dead, suggested_qty
        )

    # Inter-Warehouse Rebalancing (Phase 4)
    transfer_source_wh_id = False
    transfer_source_wh_name = False
    max_surplus_qty = 0.0
    transfer_capped_qty = 0.0
    if urgency in ('critical', 'urgent'):
        dest_need = max(0.0, raw_qty)
        for wh_other in warehouses_list:
            if wh_other['id'] == warehouse_id:
                continue
            qty_other = global_stocks.get(wh_other['id'], 0.0)
            if qty_other <= 0:
                continue

            sold_other = global_sales.get(wh_other['id'], 0.0)
            demand_other = sold_other / analysis_months if analysis_months > 0 else 0.0
            mos_other = qty_other / demand_other if demand_other > 0 else 999.0

            if mos_other > config_data['transfer_surplus_threshold']:
                protected_need = demand_other * lead_months
                donor_surplus = max(0.0, qty_other - protected_need)
                if donor_surplus <= 0:
                    continue
                capped = min(donor_surplus, dest_need) if dest_need > 0 else donor_surplus
                if capped > max_surplus_qty:
                    max_surplus_qty = capped
                    transfer_source_wh_id = wh_other['id']
                    transfer_source_wh_name = wh_other['name']
                    transfer_capped_qty = capped

    transfer_lead_time_days = False
    if transfer_source_wh_id:
        transfer_lead_time_days = lane_lead_times.get(
            transfer_source_wh_id, config_data['default_internal_transfer_days']
        )

    # Raw-vs-raw comparison basis: the previous-period and last-year figures
    # are raw sales totals, so the current side must be too. Comparing the
    # outlier-CLEANED current total against a raw prior total made a spike
    # excluded from the current window read as artificial "falling demand".
    prev_qty_val = prev_qty or 0.0
    trend, trend_pct = calc_trend(
        total_sold, prev_qty_val, analysis_months, comparison_months
    )

    ly_qty_val = ly_qty or 0.0
    seasonal_note = calc_seasonal_note(total_sold, ly_qty_val)

    est_purchase_val = suggested_qty * price_company_currency

    lead_time_desc = f'Lead time: {lead_months:.2f} months ({stated_lead_days} days stated)'
    if config_data['track_vendor_performance'] and actual_avg_days_val > stated_lead_days:
        lead_time_desc += f' [OVERRIDDEN dynamically to {actual_avg_days_val:.0f} days due to supplier lateness]'

    normal_months = len(monthly_series) - excluded_months
    if excluded_months > 0:
        direction_word = {'high': 'unusually high', 'low': 'unusually low'}.get(
            excluded_direction, 'unusual'
        )
        month_word  = 'month' if excluded_months == 1 else 'months'
        normal_word = 'month' if normal_months == 1 else 'months'
        demand_forecast_note = (
            f'Excluded {excluded_months} {direction_word} {month_word} — '
            f'based on {normal_months} normal {normal_word}.'
        )
    else:
        demand_forecast_note = f'No outlier months — using average of all {len(monthly_series)} months.'

    month_breakdown_text = ', '.join(
        f'{month_start.strftime("%B %Y")}: {qty:.0f} units'
        + (' [excluded as outlier]' if was_excluded else '')
        for month_start, qty, was_excluded
        in zip(dates['month_starts'], monthly_series, excluded_flags)
    )

    confidence, confidence_note = compute_confidence_score(
        monthly_series, excluded_months, reorder_behavior
    )

    mos_after_order = 0.0
    if avg_monthly > 0:
        mos_after_order = (qty_available + suggested_qty) / avg_monthly
    
    is_overstocked_flag = False
    if config_data['overstock_ceiling_months'] > 0 and mos_after_order > config_data['overstock_ceiling_months']:
        is_overstocked_flag = True

    notes_lines = [
        '══ DEMAND ANALYSIS ══',
        f'Period: {date_from} → {date_to} ({analysis_months} months)',
        f'Total sold: {clean_total_sold:.2f}  |  Avg monthly: {avg_monthly:.2f} units/month',
        f'Forecast method: monthly average with spike rejection — {demand_forecast_note}',
        f'Per-month breakdown: {month_breakdown_text}',
        f'Current month-to-date sales (informational): {current_month_sales:.2f} units',
        f'Confidence: {confidence:.0f}/100 — {confidence_note}',
        '',
        '══ STOCK POSITION ══',
        f'On hand: {qty_on_hand:.2f}  |  Incoming (PO + transfers): {qty_incoming:.2f}',
        f'Outgoing (reserved for customers): {qty_outgoing:.2f}',
        f'Net available: {qty_available:.2f}  |  Months of stock: {months_of_stock:.1f}',
        f'Months left after order: {mos_after_order:.1f}' if avg_monthly > 0 else 'Months left after order: —',
    ]
    if reorder_behavior == 'against_order':
        notes_lines = [
            '📝 ORDER ONLY AGAINST CUSTOMER ORDER — Replenishment is suppressed.',
            ''
        ] + notes_lines

    if overdue_lines:
        overdue_qty = sum(item[0] for item in overdue_lines)
        oldest_dt = min(item[1] for item in overdue_lines).date()
        days_late = (date.today() - oldest_dt).days
        notes_lines.append(f'⚠️ OVERDUE SUPPLY: {overdue_qty:.2f} units are overdue. Oldest is {days_late} days late.')

    notes_lines += [
        lead_time_desc,
        f"Safety buffer: {config_data['safety_buffer_months']:.2f} months  |  Order cycle: {config_data['order_cycle_months']:.2f} months",
        f'Stated MOQ: {moq:.0f} units',
        f'Formula: Min stock = Avg demand × (Lead time + Safety buffer) = {min_level:.2f} units',
        f'Formula: Max stock = Min stock + (Avg demand × Order cycle) = {max_level:.2f} units',
        f'Formula: Reorder qty = Max stock - Net available = {raw_qty:.2f} units (rounded up to MOQ = {suggested_qty:.2f})',
        f'Seasonal comparison: last year same period sold {ly_qty_val:.2f} units. {seasonal_note or ""}'
    ]

    if qty_on_hand < 0:
        notes_lines = [
            '⚠️ NEGATIVE STOCK — CRITICAL',
            '⚠️ Note: The cost figure may be temporarily distorted because average cost (AVCO) is affected by negative stock until the correcting receipt is posted.',
            ''
        ] + notes_lines
    if is_dead:
        notes_lines = [f'💀 DEAD STOCK — {months_since} months without sale', ''] + notes_lines

    if is_superseded:
        if qty_on_hand < 0:
            notes_lines = [
                f'⚠️ SUPERSEDED WITH NEGATIVE STOCK: This part has been superseded by successor '
                f'{successor_display_name}, but on-hand is negative. No replenishment of this '
                f'superseded part is suggested — resolve the shortfall via the successor part '
                f'or a stock correction.',
                ''
            ] + notes_lines
        else:
            notes_lines = [
                f'⚠️ SUPERSEDED: This part has been superseded by successor {successor_display_name}. No replenishment suggested; consume or transfer remaining stock.',
                ''
            ] + notes_lines

    if predecessors:
        pred_names_str = ', '.join(predecessor_names)
        notes_lines = [
            f'📝 Demand includes rolled-up sales history from superseded parts: {pred_names_str}',
            ''
        ] + notes_lines

    if alt_vendor_id_val and urgency in ('critical', 'urgent'):
        alt_name = partner_names_map.get(alt_vendor_id_val, 'Alternative Vendor')
        notes_lines += [
            '',
            '══ ALTERNATIVE VENDOR ══',
            f'💡 Alternative vendor available: {alt_name} can deliver in {alt_vendor_lead_days_val} days '
            f'(beats primary by {stated_lead_days - alt_vendor_lead_days_val} days).',
        ]

    if transfer_source_wh_id:
        notes_lines += [
            '',
            '══ INTERNAL TRANSFER ══',
            f'🚚 Transfer recommended: {transfer_capped_qty:.2f} units from donor warehouse "{transfer_source_wh_name}".'
        ]

    vals = {
        'company_id':               company_id,
        'warehouse_id':             warehouse_id,
        'product_id':               product_id,
        'is_provisional':           False,
        'product_cost':             cost,
        'vendor_price':             price_company_currency,
        'estimated_purchase_value': est_purchase_val,
        'is_stale':                 False,
        'stale_reason':             False,
        'analysis_date':            date.today(),
        'analysis_months':          analysis_months,
        'date_from':                date_from,
        'date_to':                  date_to,
        'total_qty_sold':           total_sold,
        'avg_monthly_demand':       avg_monthly,
        'excluded_outlier_months':  excluded_months,
        'demand_forecast_note':     demand_forecast_note,
        'confidence':               confidence,
        'lead_time_months':         lead_months,
        'safety_buffer_months':     config_data['safety_buffer_months'],
        'order_cycle_months':       config_data['order_cycle_months'],
        'min_stock_level':          min_level,
        'max_stock_level':          max_level,
        'moq':                      moq,
        'prev_period_qty_sold':     prev_qty_val,
        'trend_comparison_months':  comparison_months,
        'demand_trend':             trend,
        'trend_pct':                trend_pct,
        'same_period_last_year_qty':ly_qty_val,
        'seasonal_note':            seasonal_note,
        'qty_on_hand':              qty_on_hand,
        'qty_incoming':             qty_incoming,
        'qty_outgoing':             qty_outgoing,
        'is_dead_stock':            is_dead,
        'months_of_stock_after_order': mos_after_order,
        'is_overstocked':              is_overstocked_flag,
        'last_sale_date':           last_sale,
        'months_since_last_sale':   months_since,
        'raw_reorder_qty':          raw_qty,
        'suggested_reorder_qty':    suggested_qty,
        'reorder_needed':           reorder_needed,
        'within_budget':            True,
        'budget_rank':              0,
        'abc_class':                abc_class,
        'urgency':                  urgency,
        'vendor_id':                v_partner_id,
        'vendor_stated_lead_days':  stated_lead_days,
        'vendor_actual_avg_days':   actual_avg_days_val,
        'vendor_performance_note':  vendor_perf_note,
        'alt_vendor_id':            alt_vendor_id_val,
        'alt_vendor_lead_days':     alt_vendor_lead_days_val,
        'transfer_source_warehouse_id': transfer_source_wh_id,
        'transfer_lead_time_days':  transfer_lead_time_days,
        'transfer_suggested_qty':   transfer_capped_qty,
        'notes':                    '\n'.join(notes_lines),
        'sales_pattern':            sales_pattern,
        'has_bulk_concentration':   has_bulk_concentration,
    }
    return vals
