// Copyright (c) 2026, FuelBuddy and contributors
// For license information, please see license.txt

frappe.query_reports["Tank Balances"] = {
	filters: [
		{ fieldname: "as_of", label: __("As Of"), fieldtype: "Datetime", default: frappe.datetime.now_datetime() },
		{ fieldname: "vehicle_type", label: __("Vehicle Type"), fieldtype: "Select", options: "\nBOWSER\nCONTAINER" },
		{ fieldname: "include_disabled", label: __("Include Disabled"), fieldtype: "Check", default: 0 },
	],
};
