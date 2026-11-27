// Copyright (c) 2026, FuelBuddy and contributors
// For license information, please see license.txt

frappe.query_reports["Truck Margin"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", default: frappe.datetime.month_start() },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: frappe.datetime.get_today() },
		{ fieldname: "group_by", label: __("Group By"), fieldtype: "Select", options: "Vehicle\nTank", default: "Vehicle" },
		{ fieldname: "vehicle_registration", label: __("Vehicle"), fieldtype: "Data" },
	],
};
