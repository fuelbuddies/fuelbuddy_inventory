# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

"""Every tank at a chosen date and time, with its capacity and fill level."""

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime

from fuelbuddy_inventory import ledger


def execute(filters=None):
	filters = frappe._dict(filters or {})
	as_of = get_datetime(filters.as_of) if filters.as_of else now_datetime()
	tank_filters = {}
	if not cint(filters.include_disabled):
		tank_filters["disabled"] = 0
	if filters.vehicle_type:
		tank_filters["vehicle_type"] = filters.vehicle_type
	tanks = frappe.get_all(
		ledger.FUEL_TANK,
		filters=tank_filters,
		fields=["name", "label", "vehicle_registration", "vehicle_type", "capacity_litres", "disabled"],
		order_by="vehicle_registration, label",
	)
	balances = ledger.balances_as_of(as_of, [tank.name for tank in tanks]) if tanks else {}
	data = []
	for tank in tanks:
		balance = flt(balances.get(tank.name, 0))
		capacity = flt(tank.capacity_litres)
		data.append(
			{
				"fuel_tank": tank.name,
				"label": tank.label,
				"vehicle_registration": tank.vehicle_registration,
				"vehicle_type": tank.vehicle_type,
				"capacity_litres": capacity,
				"balance": balance,
				"fill_percent": flt(balance / capacity * 100, 1) if capacity else None,
				"disabled": tank.disabled,
			}
		)
	return columns(), data


def columns():
	return [
		{
			"fieldname": "fuel_tank",
			"label": "Fuel Tank",
			"fieldtype": "Link",
			"options": "Fuel Tank",
			"width": 200,
		},
		{"fieldname": "label", "label": "Label", "fieldtype": "Data", "width": 160},
		{"fieldname": "vehicle_registration", "label": "Vehicle", "fieldtype": "Data", "width": 120},
		{"fieldname": "vehicle_type", "label": "Vehicle Type", "fieldtype": "Data", "width": 100},
		{"fieldname": "capacity_litres", "label": "Capacity (L)", "fieldtype": "Float", "width": 120},
		{"fieldname": "balance", "label": "Balance (L)", "fieldtype": "Float", "width": 120},
		{"fieldname": "fill_percent", "label": "Fill %", "fieldtype": "Percent", "width": 80},
		{"fieldname": "disabled", "label": "Disabled", "fieldtype": "Check", "width": 80},
	]
