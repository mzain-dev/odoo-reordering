/** @odoo-module **/

/**
 * Smart Reorder Advisor — OWL Dashboard (All 3 Phases)
 *
 * Shows:
 * Phase 1: KPI cards, ABC bar, monthly demand, reorder value, dead stock, budget
 * Phase 2: Demand trend breakdown, top rising/falling products
 * Phase 3: Vendor performance summary, seasonal alerts
 */

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class SmartReorderDashboard extends Component {
    static template = "smart_reorder.Dashboard";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");

        this.state = useState({
            loading: true,
            error: null,
            // Phase 1: KPI
            critical: 0, urgent: 0, normal: 0, ok: 0, dead: 0, total: 0,
            // Phase 1: ABC
            abc_a: 0, abc_b: 0, abc_c: 0,
            // Phase 1: Value
            totalReorderValue: 0, withinBudgetValue: 0, budgetCount: 0,
            // Phase 1: Dead stock
            deadStockCount: 0,
            // Phase 2: Trend
            trendUp: 0, trendDown: 0, trendStable: 0,
            // Top tables
            topCritical: [], topUrgent: [], topDead: [],
            topRising: [], topFalling: [],
            // Filter
            selectedWarehouse: null, warehouses: [],
            lastAnalysisDate: null,
            currency: '',
            // Feature 15: Back-testing
            backtest: { mape_by_abc: {}, mape_by_warehouse: {}, overall_mape: 0.0 },
        });
        onWillStart(async () => {
            await this._loadAll();
        });
    }

    async _loadAll() {
        this.state.loading = true;
        this.state.error = null;
        try {
            const d = await this.orm.call(
                'smart.reorder.suggestion', 'get_dashboard_data',
                [], { warehouse_id: this.state.selectedWarehouse || false }
            );
            const u = d.urgency;
            Object.assign(this.state, {
                critical: u.critical||0, urgent: u.urgent||0,
                normal: u.normal||0, ok: u.ok||0, dead: u.dead||0,
                total: Object.values(u).reduce((a,b)=>a+b,0),
                abc_a: d.abc.A||0, abc_b: d.abc.B||0, abc_c: d.abc.C||0,
                trendUp: d.trend.up||0, trendDown: d.trend.down||0,
                trendStable: d.trend.stable||0,
                totalReorderValue: d.total_reorder_value,
                withinBudgetValue: d.within_budget_value,
                budgetCount: d.budget_count,
                deadStockCount: d.dead_count,
                currency: d.currency,
                lastAnalysisDate: d.last_analysis_date,
                topCritical: d.top.critical, topUrgent: d.top.urgent,
                topDead: d.top.dead, topRising: d.top.rising,
                topFalling: d.top.falling,
                warehouses: d.warehouses,
                backtest: d.backtest || { mape_by_abc: {}, mape_by_warehouse: {}, overall_mape: 0.0 },
            });
        } catch (e) {
            this.state.error = 'Failed to load dashboard. Please try again.';
            console.error('SmartReorder dashboard error:', e);
        } finally {
            this.state.loading = false;
        }
    }

    async onWarehouseChange(ev) {
        const val = ev.target.value;
        this.state.selectedWarehouse = val ? parseInt(val) : null;
        await this._loadAll();
    }

    // Navigation helpers
    openList(domain, name) {
        // Pass context: {} to clear the search_default_reorder_needed filter
        // that the main action applies by default, so the urgency domain
        // is the only active filter when drillling down from a KPI card.
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "smart.reorder.suggestion",
            views: [[false, "list"], [false, "form"]],
            domain,
            context: {},
            target: "current",
        });
    }
    openCritical()   { this.openList([["urgency","=","critical"]], "🔴 Critical — Negative Stock"); }
    openUrgent()     { this.openList([["urgency","=","urgent"]], "🟠 Urgent Items"); }
    openDead()       { this.openList([["is_dead_stock","=",true]], "💀 Dead Stock"); }
    openReorder()    { this.openList([["reorder_needed","=",true]], "🔁 Needs Reorder"); }
    openBudget()     { this.openList([["within_budget","=",true],["reorder_needed","=",true]], "💰 Within Budget"); }
    openRising()     { this.openList([["demand_trend","=","up"]], "↑ Rising Demand"); }
    openFalling()    { this.openList([["demand_trend","=","down"]], "↓ Falling Demand"); }
    openRunWizard()  { this.action.doAction("smart_reorder_advisor.action_generate_suggestions_wizard"); }
    openBacktest() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Forecast Back-testing",
            res_model: "smart.reorder.forecast.snapshot",
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    // Computed helpers
    get abcAPercent() { return this.state.total > 0 ? Math.round((this.state.abc_a/this.state.total)*100) : 0; }
    get abcBPercent() { return this.state.total > 0 ? Math.round((this.state.abc_b/this.state.total)*100) : 0; }
    get abcCPercent() { return this.state.total > 0 ? Math.round((this.state.abc_c/this.state.total)*100) : 0; }

    formatCurrency(val) {
        if (val == null || typeof val !== 'number') return '—';
        return val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    formatMonths(val) {
        if (!val && val !== 0) return "—";
        return val.toFixed(1) + " mo";
    }
}

registry.category("actions").add("smart_reorder.Dashboard", SmartReorderDashboard);
