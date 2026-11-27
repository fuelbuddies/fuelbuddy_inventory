// Copyright (c) 2026, FuelBuddy and contributors
// For license information, please see license.txt

frappe.query_reports["Tank Ledger"] = {
	filters: [
		{ fieldname: "fuel_tank", label: __("Fuel Tank"), fieldtype: "Link", options: "Fuel Tank", reqd: 1 },
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", default: frappe.datetime.month_start() },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: frappe.datetime.get_today() },
		{ fieldname: "include_cancelled", label: __("Include Cancelled"), fieldtype: "Check", default: 0 },
	],
};
