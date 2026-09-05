"""mammon.reports -- Mammon's reporting layer (SRD 5.9).

Reports are pure, read-only aggregations over the ledger tables, returning plain
data structures so a GUI, a CSV export, or a bundled text renderer can consume
them without re-querying. First and highest priority: spending by category.
"""
from mammon.reports.spending import (
    CategoryReportRow,
    SpendingReport,
    format_spending_report,
    period_range,
    preset_range,
    spending_by_category,
)
from mammon.reports.charts import (
    NetWorthPoint,
    NetWorthSeries,
    PeriodSpending,
    PieSlice,
    SpendingByPeriod,
    SpendingPie,
    income_category_rows,
    income_pie,
    net_worth_series,
    spending_by_period,
    spending_category_rows,
    spending_pie,
)
from mammon.reports.itemized import (
    ItemizedReport,
    ItemizedRow,
    ItemizedTree,
    TreeNode,
    TxnLine,
    format_itemized_report,
    itemize_by_category,
    itemize_tree,
)
from mammon.reports.flows import (
    AverageRow,
    AveragesReport,
    CashFlowReport,
    ComparisonReport,
    ComparisonRow,
    ComparisonTotal,
    FlowRow,
    IncomeExpenseReport,
    TransferRow,
    bucket_of,
    buckets_in,
    cash_flow,
    category_averages,
    compare_periods,
    income_expense,
)
from mammon.reports.balances import (
    AccountBalance,
    BalanceReport,
    BalanceSample,
    BalanceSeries,
    account_balances,
    balances_over_time,
)
from mammon.reports.payees import PayeeReport, PayeeRow, by_payee, by_tag
from mammon.reports.tags import spending_by_tag
from mammon.reports.listing import ListingReport, transactions
from mammon.reports.investment_performance import (
    HoldingPerformance,
    InvestmentPerformanceReport,
    investment_performance,
)
from mammon.reports.budget import (
    BudgetCategoryTotal,
    BudgetPeriodTotal,
    BudgetRangeReport,
    budget_vs_actual_range,
    budget_vs_actual_ytd,
)

__all__ = [
    "AverageRow", "AveragesReport", "CashFlowReport", "ComparisonReport",
    "ComparisonRow", "ComparisonTotal", "FlowRow", "IncomeExpenseReport",
    "TransferRow", "bucket_of", "buckets_in", "cash_flow", "category_averages",
    "compare_periods", "income_expense",
    "AccountBalance", "BalanceReport", "BalanceSample", "BalanceSeries",
    "account_balances", "balances_over_time",
    "PayeeReport", "PayeeRow", "by_payee", "by_tag",
    "spending_by_tag",
    "ListingReport", "transactions",
    "HoldingPerformance", "InvestmentPerformanceReport", "investment_performance",
    "BudgetCategoryTotal", "BudgetPeriodTotal", "BudgetRangeReport",
    "budget_vs_actual_range", "budget_vs_actual_ytd",
    "CategoryReportRow",
    "SpendingReport",
    "format_spending_report",
    "period_range",
    "preset_range",
    "spending_by_category",
    "NetWorthPoint",
    "NetWorthSeries",
    "PeriodSpending",
    "PieSlice",
    "SpendingByPeriod",
    "SpendingPie",
    "income_category_rows",
    "income_pie",
    "net_worth_series",
    "spending_by_period",
    "spending_category_rows",
    "spending_pie",
    "ItemizedReport",
    "ItemizedRow",
    "ItemizedTree",
    "TreeNode",
    "TxnLine",
    "format_itemized_report",
    "itemize_by_category",
    "itemize_tree",
]
