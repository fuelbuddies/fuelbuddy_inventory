# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

"""Per vehicle (or tank) and period: litres delivered, revenue, FIFO cost and margin.

Cost is each Delivery Note line's own FIFO cost, stock_qty x incoming_rate (company FIFO at the
delivery's posting time). Revenue is, for now, the line's base_net_amount; finance may switch it to
invoice revenue allocated to the delivery, since discounts sit on the invoice. Deliveries are found
through the tank ledger (active Delivery Note rows), never the header field, so a cancelled or
amended note is never counted twice."""

import datetime

import frappe
from frappe.utils import flt, get_first_day, getdate, today

from fuelbuddy_inventory import ledger
from fuelbuddy_inventory.settings import get_settings, is_configured

REVENUE_NOTE = (
	"Revenue is the Delivery Note lines' base net amount, before invoice discounts. Finance may "
	"switch it to invoice revenue allocated to the delivery. Cost is each line's FIFO cost "
	"(stock qty x incoming rate)."
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	settings = get_settings()
	if not is_configured(settings):
		frappe.throw("Set the fuel item and the fuel warehouse in Fuel Inventory Settings first.")
	start = datetime.datetime.combine(getdate(filters.from_date or get_first_day(today())), datetime.time.min)
	end = datetime.datetime.combine(getdate(filters.to_date or today()), datetime.time.max)
	by_tank = filters.group_by == "Tank"
	group = "t.vehicle_registration, t.vehicle_type" + (", l.fuel_tank, t.label" if by_tank else "")
	vehicle = "and t.vehicle_registration = %(vehicle)s" if filters.vehicle_registration else ""
	rows = frappe.db.sql(
		f"""select {group}, count(distinct l.voucher_no) as deliveries,
			sum(i.stock_qty) as litres,
			sum(i.stock_qty * i.incoming_rate) as cost,
			sum(i.base_net_amount) as revenue
		from {ledger.TABLE} l
		join `tabFuel Tank` t on t.name = l.fuel_tank
		join `tabDelivery Note Item` i on i.parent = l.voucher_no and i.parenttype = 'Delivery Note'
		where l.voucher_type = 'Delivery Note' and l.is_cancelled = 0
			and l.posting_datetime between %(start)s and %(end)s
			and i.item_code = %(item)s and i.warehouse = %(warehouse)s {vehicle}
		group by {group}
		order by t.vehicle_registration{', t.label' if by_tank else ''}""",
		{
			"start": start,
			"end": end,
			"item": settings.fuel_item,
			"warehouse": settings.fuel_warehouse,
			"vehicle": filters.vehicle_registration,
		},
		as_dict=True,
	)
	for row in rows:
		row.litres = flt(row.litres, 3)
		row.cost = flt(row.cost, 2)
		row.revenue = flt(row.revenue, 2)
		row.margin = flt(row.revenue - row.cost, 2)
		row.margin_percent = flt(row.margin / row.revenue * 100, 2) if row.revenue else None
	return columns(by_tank), rows, REVENUE_NOTE


def columns(by_tank):
	result = [
		{"fieldname": "vehicle_registration", "label": "Vehicle", "fieldtype": "Data", "width": 130},
		{"fieldname": "vehicle_type", "label": "Vehicle Type", "fieldtype": "Data", "width": 100},
	]
	if by_tank:
		result += [
			{
				"fieldname": "fuel_tank",
				"label": "Fuel Tank",
				"fieldtype": "Link",
				"options": "Fuel Tank",
				"width": 180,
			},
			{"fieldname": "label", "label": "Label", "fieldtype": "Data", "width": 150},
		]
	result += [
		{"fieldname": "deliveries", "label": "Deliveries", "fieldtype": "Int", "width": 100},
		{"fieldname": "litres", "label": "Litres Delivered", "fieldtype": "Float", "width": 140},
		{"fieldname": "revenue", "label": "Revenue (DN net)", "fieldtype": "Currency", "width": 140},
		{"fieldname": "cost", "label": "FIFO Cost", "fieldtype": "Currency", "width": 130},
		{"fieldname": "margin", "label": "Margin", "fieldtype": "Currency", "width": 130},
		{"fieldname": "margin_percent", "label": "Margin %", "fieldtype": "Percent", "width": 90},
	]
	return result
