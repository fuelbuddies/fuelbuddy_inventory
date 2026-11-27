# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

"""Per day: the sum of tank balances against ERP's stock-ledger quantity of the fuel item in the fuel
warehouse (never the Bin), with the number of fuel drafts still pending on or before that day."""

import datetime

import frappe
from frappe.utils import add_days, date_diff, flt, getdate, today

from fuelbuddy_inventory import ledger
from fuelbuddy_inventory.settings import get_settings, is_configured

MAX_DAYS = 92
DRAFT_DOCTYPES = (
	("Delivery Note", "Delivery Note Item"),
	("Purchase Receipt", "Purchase Receipt Item"),
	("Stock Entry", "Stock Entry Detail"),
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	settings = get_settings()
	if not is_configured(settings):
		frappe.throw("Set the fuel item and the fuel warehouse in Fuel Inventory Settings first.")
	end_day = getdate(filters.to_date or today())
	start_day = getdate(filters.from_date or add_days(end_day, -13))
	if date_diff(end_day, start_day) >= MAX_DAYS:
		frappe.throw(f"Choose at most {MAX_DAYS} days.")
	pending = _pending_by_day(settings, end_day)
	data = []
	day = start_day
	while day <= end_day:
		end = datetime.datetime.combine(day, datetime.time.max)
		tanks = sum(ledger.balances_as_of(end).values())
		warehouse = ledger.warehouse_qty_as_of(end, settings)
		data.append(
			{
				"day": day,
				"tanks": flt(tanks, 3),
				"warehouse": flt(warehouse, 3),
				"difference": flt(tanks - warehouse, 3),
				"pending_drafts": sum(count for posted, count in pending.items() if posted <= day),
			}
		)
		day = add_days(day, 1)
	return columns(), data


def _pending_by_day(settings, end_day):
	"""Draft fuel documents (and Tank Transfers) dated from tracking start, counted per posting date."""
	counts = {}
	since = settings.tracking_start.date()
	for doctype, child in DRAFT_DOCTYPES:
		for posted, count in frappe.db.sql(
			f"""select p.posting_date, count(distinct p.name) from `tab{doctype}` p
			join `tab{child}` i on i.parent = p.name
			where p.docstatus = 0 and i.item_code = %(item)s and p.posting_date between %(since)s and %(end)s
			group by p.posting_date""",
			{"item": settings.fuel_item, "since": since, "end": end_day},
		):
			counts[getdate(posted)] = counts.get(getdate(posted), 0) + count
	for posted, count in frappe.db.sql(
		"""select posting_date, count(*) from `tabTank Transfer`
		where docstatus = 0 and posting_date between %s and %s group by posting_date""",
		(since, end_day),
	):
		counts[getdate(posted)] = counts.get(getdate(posted), 0) + count
	return counts


def columns():
	return [
		{"fieldname": "day", "label": "Day (end of)", "fieldtype": "Date", "width": 110},
		{"fieldname": "tanks", "label": "Sum of Tanks (L)", "fieldtype": "Float", "width": 150},
		{"fieldname": "warehouse", "label": "Warehouse (L)", "fieldtype": "Float", "width": 150},
		{"fieldname": "difference", "label": "Difference (L)", "fieldtype": "Float", "width": 130},
		{"fieldname": "pending_drafts", "label": "Pending Drafts", "fieldtype": "Int", "width": 120},
	]
