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
        });
        onWillStart(async () => {
            await this._loadWarehouses();
            await this._loadAll();
        });
    }

    async _loadWarehouses() {
        const whs = await this.orm.searchRead("stock.warehouse", [], ["id", "name", "company_id"], { limit: 50 });
        this.state.warehouses = whs;
    }

    async _loadAll() {
        this.state.loading = true;
        this.state.error = null;
        try {
            const baseDomain = this.state.selectedWarehouse
                ? [["warehouse_id", "=", this.state.selectedWarehouse]]
                : [];

            // ── Urgency stats ──
            const urgencyStats = await this.orm.readGroup(
                "smart.reorder.suggestion", baseDomain,
                ["urgency", "__count"], ["urgency"]
            );
            let critical=0, urgent=0, normal=0, ok=0, dead=0;
            for (const s of urgencyStats) {
                if (s.urgency === "critical") critical = s.__count;
                else if (s.urgency === "urgent")   urgent  = s.__count;
                else if (s.urgency === "normal")   normal  = s.__count;
                else if (s.urgency === "ok")       ok      = s.__count;
                else if (s.urgency === "dead")     dead    = s.__count;
            }
            Object.assign(this.state, { critical, urgent, normal, ok, dead,
                total: critical + urgent + normal + ok + dead });

            // ── ABC stats ──
            const abcStats = await this.orm.readGroup(
                "smart.reorder.suggestion", baseDomain,
                ["abc_class", "__count"], ["abc_class"]
            );
            let abc_a=0, abc_b=0, abc_c=0;
            for (const s of abcStats) {
                if (s.abc_class==="A") abc_a=s.__count;
                else if (s.abc_class==="B") abc_b=s.__count;
                else if (s.abc_class==="C") abc_c=s.__count;
            }
            Object.assign(this.state, { abc_a, abc_b, abc_c });

            // ── Phase 2: Trend stats ──
            const trendStats = await this.orm.readGroup(
                "smart.reorder.suggestion", baseDomain,
                ["demand_trend", "__count"], ["demand_trend"]
            );
            let trendUp=0, trendDown=0, trendStable=0;
            for (const s of trendStats) {
                if (s.demand_trend==="up")     trendUp     = s.__count;
                else if (s.demand_trend==="down")   trendDown   = s.__count;
                else if (s.demand_trend==="stable") trendStable = s.__count;
            }
            Object.assign(this.state, { trendUp, trendDown, trendStable });

            // ── Phase 1: Reorder value totals ──
            const valueData = await this.orm.searchRead(
                "smart.reorder.suggestion",
                [...baseDomain, ["reorder_needed","=",true]],
                ["reorder_value","within_budget","currency_id"],
                { limit: 0 }
            );
            const totalReorderValue = valueData.reduce((s,r) => s + (r.reorder_value||0), 0);
            const budgetItems = valueData.filter(r => r.within_budget);
            const withinBudgetValue = budgetItems.reduce((s,r) => s + (r.reorder_value||0), 0);
            const currency = valueData[0]?.currency_id?.[1] || '';
            Object.assign(this.state, {
                totalReorderValue, withinBudgetValue,
                budgetCount: budgetItems.length, currency
            });

            // ── Dead stock count ──
            const deadCount = await this.orm.searchCount(
                "smart.reorder.suggestion",
                [...baseDomain, ["is_dead_stock","=",true]]
            );
            this.state.deadStockCount = deadCount;

            // ── Top Critical ──
            this.state.topCritical = await this.orm.searchRead(
                "smart.reorder.suggestion",
                [...baseDomain, ["urgency","=","critical"]],
                ["product_id","default_code","warehouse_id","qty_on_hand",
                 "suggested_reorder_qty","reorder_value","vendor_id"],
                { limit: 10, order: "qty_on_hand asc" }
            );

            // ── Top Urgent ──
            this.state.topUrgent = await this.orm.searchRead(
                "smart.reorder.suggestion",
                [...baseDomain, ["urgency","=","urgent"]],
                ["product_id","default_code","warehouse_id","months_of_stock",
                 "avg_monthly_demand","suggested_reorder_qty","reorder_value"],
                { limit: 10, order: "months_of_stock asc" }
            );

            // ── Phase 1: Dead stock table ──
            this.state.topDead = await this.orm.searchRead(
                "smart.reorder.suggestion",
                [...baseDomain, ["is_dead_stock","=",true]],
                ["product_id","default_code","warehouse_id","qty_on_hand",
                 "months_since_last_sale","last_sale_date","product_cost"],
                { limit: 10, order: "months_since_last_sale desc" }
            );

            // ── Phase 2: Rising demand ──
            this.state.topRising = await this.orm.searchRead(
                "smart.reorder.suggestion",
                [...baseDomain, ["demand_trend","=","up"],["reorder_needed","=",true]],
                ["product_id","default_code","avg_monthly_demand","trend_pct","reorder_value"],
                { limit: 8, order: "trend_pct desc" }
            );

            // ── Phase 2: Falling demand ──
            this.state.topFalling = await this.orm.searchRead(
                "smart.reorder.suggestion",
                [...baseDomain, ["demand_trend","=","down"]],
                ["product_id","default_code","avg_monthly_demand","trend_pct","qty_on_hand"],
                { limit: 8, order: "trend_pct asc" }
            );

            // ── Last analysis date ──
            const latest = await this.orm.searchRead(
                "smart.reorder.suggestion", baseDomain,
                ["analysis_date"], { limit: 1, order: "analysis_date desc" }
            );
            this.state.lastAnalysisDate = latest[0]?.analysis_date || null;

        } catch (e) {
            this.state.error = "Failed to load dashboard. Please try again.";
            console.error("SmartReorder dashboard error:", e);
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
        this.action.doAction({
            type: "ir.actions.act_window",
            name,
            res_model: "smart.reorder.suggestion",
            views: [[false, "list"], [false, "form"]],
            domain,
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

    // Computed helpers
    get abcAPercent() { return this.state.total > 0 ? Math.round((this.state.abc_a/this.state.total)*100) : 0; }
    get abcBPercent() { return this.state.total > 0 ? Math.round((this.state.abc_b/this.state.total)*100) : 0; }
    get abcCPercent() { return this.state.total > 0 ? Math.round((this.state.abc_c/this.state.total)*100) : 0; }

    formatCurrency(val) {
        return val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    formatMonths(val) {
        if (!val && val !== 0) return "—";
        return val.toFixed(1) + " mo";
    }
}

registry.category("actions").add("smart_reorder.Dashboard", SmartReorderDashboard);
