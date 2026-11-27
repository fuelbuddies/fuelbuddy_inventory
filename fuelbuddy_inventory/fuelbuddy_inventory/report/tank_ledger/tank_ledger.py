# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

"""One tank: its opening balance, every movement with its document, and the running balance."""

import datetime

import frappe
from frappe.utils import cint, flt, get_first_day, getdate, today

from fuelbuddy_inventory import ledger


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.fuel_tank:
		frappe.throw("Choose a Fuel Tank.")
	start = datetime.datetime.combine(getdate(filters.from_date or get_first_day(today())), datetime.time.min)
	end = datetime.datetime.combine(getdate(filters.to_date or today()), datetime.time.max)
	opening = ledger.balance_as_of(filters.fuel_tank, start - datetime.timedelta(microseconds=1))
	cancelled = "" if cint(filters.include_cancelled) else "and is_cancelled = 0"
	rows = frappe.db.sql(
		f"""select posting_datetime, voucher_type, voucher_no, qty, balance_after, is_anchor, is_cancelled
		from {ledger.TABLE}
		where fuel_tank = %(tank)s and posting_datetime between %(start)s and %(end)s {cancelled}
		order by posting_datetime, creation, name""",
		{"tank": filters.fuel_tank, "start": start, "end": end},
		as_dict=True,
	)
	data = [{"voucher_type": "Opening", "balance_after": flt(opening)}]
	data += rows
	return columns(), data


def columns():
	return [
		{"fieldname": "posting_datetime", "label": "Posting Datetime", "fieldtype": "Datetime", "width": 170},
		{"fieldname": "voucher_type", "label": "Voucher Type", "fieldtype": "Data", "width": 150},
		{
			"fieldname": "voucher_no",
			"label": "Voucher No",
			"fieldtype": "Dynamic Link",
			"options": "voucher_type",
			"width": 180,
		},
		{"fieldname": "qty", "label": "Qty (L)", "fieldtype": "Float", "width": 120},
		{"fieldname": "balance_after", "label": "Balance (L)", "fieldtype": "Float", "width": 130},
		{"fieldname": "is_anchor", "label": "Count", "fieldtype": "Check", "width": 70},
		{"fieldname": "is_cancelled", "label": "Cancelled", "fieldtype": "Check", "width": 90},
	]
