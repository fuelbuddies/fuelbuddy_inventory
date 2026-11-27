# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

"""What needs a person: open negative and over-capacity flags, tanks out of range now, and fuel drafts
dated from tracking start that would be refused for a missing or disabled tank."""

import frappe
from frappe.utils import flt

from fuelbuddy_inventory import flags, ledger
from fuelbuddy_inventory.settings import get_settings, is_configured

DRAFT_DOCTYPES = (
	("Delivery Note", "Delivery Note Item"),
	("Purchase Receipt", "Purchase Receipt Item"),
	("Stock Entry", "Stock Entry Detail"),
)


def execute(filters=None):
	data = _open_flags() + _out_of_range_now()
	settings = get_settings()
	if is_configured(settings):
		data += _drafts(settings)
	return columns(), data


def _open_flags():
	return [
		{
			"kind": "Open flag",
			"reference_type": "Issue",
			"reference": issue.name,
			"posting_date": issue.opening_date,
			"detail": issue.subject,
		}
		for issue in frappe.get_all(
			"Issue",
			filters={"issue_type": flags.ISSUE_TYPE, "status": ["not in", flags.OPEN_STATUSES_EXCLUDED]},
			fields=["name", "subject", "opening_date"],
			order_by="creation desc",
		)
	]


def _out_of_range_now():
	tanks = frappe.get_all(ledger.FUEL_TANK, fields=["name", "label", "capacity_litres"])
	balances = ledger.balances_as_of(None, [tank.name for tank in tanks]) if tanks else {}
	data = []
	for tank in tanks:
		balance = flt(balances.get(tank.name, 0), 3)
		capacity = flt(tank.capacity_litres)
		if balance < 0:
			data.append(_tank_row("Negative now", tank, f"{tank.label} holds {balance} L"))
		elif capacity and balance > capacity:
			data.append(
				_tank_row("Over capacity now", tank, f"{tank.label} holds {balance} L, Capacity {capacity} L")
			)
	return data


def _tank_row(kind, tank, detail):
	return {
		"kind": kind,
		"reference_type": "Fuel Tank",
		"reference": tank.name,
		"fuel_tank": tank.name,
		"detail": detail,
	}


def _drafts(settings):
	data = []
	since = settings.tracking_start.date()
	for doctype, child in DRAFT_DOCTYPES:
		if not frappe.get_meta(doctype).has_field("fuel_tank"):
			continue
		for row in frappe.db.sql(
			f"""select distinct p.name, p.posting_date, p.fuel_tank, t.disabled
			from `tab{doctype}` p
			join `tab{child}` i on i.parent = p.name and i.item_code = %(item)s
			left join `tabFuel Tank` t on t.name = p.fuel_tank
			where p.docstatus = 0 and p.posting_date >= %(since)s
				and (p.fuel_tank is null or p.fuel_tank = '' or t.disabled = 1)""",
			{"item": settings.fuel_item, "since": since},
			as_dict=True,
		):
			missing = not row.fuel_tank
			data.append(
				{
					"kind": "Draft without tank" if missing else "Draft with disabled tank",
					"reference_type": doctype,
					"reference": row.name,
					"fuel_tank": row.fuel_tank,
					"posting_date": row.posting_date,
					"detail": "TANK_MISSING at submit" if missing else "TANK_NOT_FOUND at submit",
				}
			)
	return data


def columns():
	return [
		{"fieldname": "kind", "label": "Kind", "fieldtype": "Data", "width": 170},
		{"fieldname": "reference_type", "label": "Reference Type", "fieldtype": "Data", "width": 130},
		{
			"fieldname": "reference",
			"label": "Reference",
			"fieldtype": "Dynamic Link",
			"options": "reference_type",
			"width": 180,
		},
		{
			"fieldname": "fuel_tank",
			"label": "Fuel Tank",
			"fieldtype": "Link",
			"options": "Fuel Tank",
			"width": 180,
		},
		{"fieldname": "posting_date", "label": "Date", "fieldtype": "Date", "width": 100},
		{"fieldname": "detail", "label": "Detail", "fieldtype": "Data", "width": 400},
	]
