// Copyright (c) 2026, FuelBuddy and contributors
// For license information, please see license.txt

frappe.query_reports["Tank vs Warehouse"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", default: frappe.datetime.add_days(frappe.datetime.get_today(), -13) },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: frappe.datetime.get_today() },
	],
};
