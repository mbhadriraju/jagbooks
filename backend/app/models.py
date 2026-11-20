"""Finance classification map shared by extraction, review, and exports."""
CATEGORIES = {
    "revenue": ["merchandise_sales", "student_dues", "fundraising_revenue", "sponsorship_revenue", "event_revenue", "other_revenue"],
    "expense": ["tournament_fees", "event_fees", "travel_expenses", "lodging_expenses", "meal_expenses", "merchandise_expenses", "equipment_supplies", "software_subscriptions", "printing_materials", "other_expenses"],
    "asset": ["cash", "accounts_receivable", "inventory", "prepaid_expenses", "equipment_assets"],
    "liability": ["accounts_payable", "student_refunds_payable", "loans_payable", "deferred_revenue", "other_liabilities"],
    "cash_flow": ["account_transfers", "bank_deposits", "cash_withdrawals", "loan_proceeds", "loan_repayments", "opening_balance_adjustment"],
    "purchase_order": ["outstanding_purchase_orders"],
}
