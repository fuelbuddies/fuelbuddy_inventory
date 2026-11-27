"""doc_events on ERPNext's stock documents.

validate        refuses fuel in the wrong warehouse or on a path the tank ledger cannot follow
before_submit   refuses a fuel document with no tank, or an unknown or disabled one
on_submit       writes the voucher's tank row(s), in the same transaction as ERP's stock ledger
on_cancel       marks them cancelled and recalculates the tank

Only documents dated on or after tracking start (Fuel Inventory Settings) are checked or written.
A movement's litres come from the voucher's own Stock Ledger Entries, never from its lines, so
returns, units and conversion always match ERP. Stock Ledger Entry itself is never hooked: Landed
Cost Voucher and Repost Item Valuation re-create entries and would post twice.

Tank locks are taken in on_submit / on_cancel only, after ERPNext's own stock work in the same
call, and nothing posts to ERP after them. Nothing here commits.
"""

import frappe
from frappe.utils import cint, flt

from fuelbuddy_inventory import flags, ledger
from fuelbuddy_inventory.exceptions import (
	READINGS_MISMATCH,
	STOCK_PATH_NOT_ALLOWED,
	TANK_MISSING,
	TANK_NOT_FOUND,
	WRONG_FUEL_WAREHOUSE,
	refuse,
)
from fuelbuddy_inventory.settings import doc_posting_datetime, get_settings, is_tracked

TANK_FIELD = "fuel_tank"
READINGS_FIELD = "fuel_tank_readings"
APPROVAL_FIELD = "fuel_count_needs_approval"
# Readings and quantities are compared to the millilitre.
LITRE_PLACES = 3


def fuel_rows(doc, settings):
	return [row for row in doc.get("items") or [] if row.get("item_code") == settings.fuel_item]


def check_tank(tank, where=""):
	"""Refuse TANK_NOT_FOUND unless `tank` is an enabled Fuel Tank."""
	disabled = frappe.db.get_value(ledger.FUEL_TANK, tank, "disabled")
	if disabled is None:
		refuse(TANK_NOT_FOUND, f"{where}Fuel Tank {tank} does not exist.")
	if cint(disabled):
		refuse(TANK_NOT_FOUND, f"{where}Fuel Tank {tank} is disabled.")


# ---- validate ---------------------------------------------------------------------------------
def validate_stock_document(doc, method=None):
	"""Delivery Note, Purchase Receipt: every fuel line in the fuel warehouse."""
	settings = get_settings()
	if not is_tracked(doc, settings):
		return
	for row in fuel_rows(doc, settings):
		warehouses = [row.get("warehouse")]
		if doc.doctype == "Purchase Receipt" and flt(row.get("rejected_qty")):
			warehouses.append(row.get("rejected_warehouse"))
		for warehouse in warehouses:
			if warehouse and warehouse != settings.fuel_warehouse:
				refuse(
					WRONG_FUEL_WAREHOUSE,
					f"Row {row.idx}: {row.item_code} is in {warehouse}; fuel must be in {settings.fuel_warehouse}.",
				)


def validate_stock_entry(doc, method=None):
	"""Stock Entry: no Repack and no same-warehouse transfer of fuel; fuel only in the fuel warehouse."""
	settings = get_settings()
	if not is_tracked(doc, settings):
		return
	rows = fuel_rows(doc, settings)
	if not rows:
		return
	if doc.get("purpose") == "Repack":
		refuse(STOCK_PATH_NOT_ALLOWED, f"{settings.fuel_item} cannot be moved by a Repack Stock Entry.")
	for row in rows:
		if row.get("s_warehouse") and row.s_warehouse == row.get("t_warehouse"):
			refuse(
				STOCK_PATH_NOT_ALLOWED,
				f"Row {row.idx}: {row.item_code} moves from {row.s_warehouse} to itself. A move between "
				"tanks is a Tank Transfer, not a Stock Entry.",
			)
	for row in rows:
		for warehouse in (row.get("s_warehouse"), row.get("t_warehouse")):
			if warehouse and warehouse != settings.fuel_warehouse:
				refuse(
					WRONG_FUEL_WAREHOUSE,
					f"Row {row.idx}: {row.item_code} is in {warehouse}; fuel must be in {settings.fuel_warehouse}.",
				)


def refuse_invoice_stock_update(doc, method=None):
	"""Sales / Purchase Invoice with Update Stock: fuel must move on a Delivery Note or Purchase Receipt,
	which names its tank."""
	if not cint(doc.get("update_stock")):
		return
	settings = get_settings()
	if is_tracked(doc, settings) and fuel_rows(doc, settings):
		refuse(
			STOCK_PATH_NOT_ALLOWED,
			f"{doc.doctype} cannot update stock for {settings.fuel_item}: post a "
			f"{'Delivery Note' if doc.doctype == 'Sales Invoice' else 'Purchase Receipt'} with its Fuel Tank.",
		)


def validate_stock_reconciliation(doc, method=None):
	"""A count of fuel carries one reading per tank, adding up to the counted quantity."""
	settings = get_settings()
	if not is_tracked(doc, settings):
		return
	counted = None
	for row in fuel_rows(doc, settings):
		if row.get("warehouse") != settings.fuel_warehouse:
			refuse(
				WRONG_FUEL_WAREHOUSE,
				f"Row {row.idx}: {row.item_code} is in {row.warehouse}; fuel must be in {settings.fuel_warehouse}.",
			)
		counted = row
	readings = doc.get(READINGS_FIELD) or []
	if counted and not readings:
		refuse(
			STOCK_PATH_NOT_ALLOWED,
			f"A Stock Reconciliation of {settings.fuel_item} needs a tank reading for each tank "
			"(Fuel Tank Readings).",
		)
	if readings and not counted:
		refuse(
			READINGS_MISMATCH,
			f"Tank readings are given but there is no {settings.fuel_item} row in {settings.fuel_warehouse} "
			"to count. ERPNext drops a row with no change in quantity or value; for an opening with no "
			"difference use a Fuel Tank Opening.",
		)
	if not counted:
		return
	validate_readings(readings, counted.qty, f"the counted quantity of row {counted.idx}")
	if doc.meta.has_field(APPROVAL_FIELD):
		threshold = settings.approval_threshold_litres
		difference = abs(flt(counted.qty) - flt(counted.get("current_qty")))
		doc.set(APPROVAL_FIELD, 1 if threshold > 0 and difference > threshold else 0)


def validate_readings(readings, expected, what):
	"""Each reading names a tank once with litres >= 0, and they add up to `expected` litres."""
	seen = set()
	total = 0.0
	for reading in readings:
		if not reading.get("fuel_tank"):
			refuse(TANK_MISSING, f"Reading row {reading.idx} names no Fuel Tank.")
		if reading.fuel_tank in seen:
			refuse(READINGS_MISMATCH, f"Fuel Tank {reading.fuel_tank} is read twice.")
		seen.add(reading.fuel_tank)
		if flt(reading.get("measured_qty")) < 0:
			refuse(READINGS_MISMATCH, f"Reading row {reading.idx}: litres cannot be below zero.")
		total += flt(reading.get("measured_qty"))
	if flt(total - flt(expected), LITRE_PLACES) != 0:
		refuse(
			READINGS_MISMATCH,
			f"The tank readings add up to {flt(total, LITRE_PLACES)} L but {what} is "
			f"{flt(expected, LITRE_PLACES)} L.",
		)


# ---- before_submit ----------------------------------------------------------------------------
def require_tank(doc, method=None):
	"""Delivery Note, Purchase Receipt, Stock Entry with fuel lines: an enabled tank is named."""
	settings = get_settings()
	if not is_tracked(doc, settings) or not fuel_rows(doc, settings):
		return
	tank = doc.get(TANK_FIELD)
	if not tank:
		refuse(TANK_MISSING, f"{doc.doctype} {doc.name} moves {settings.fuel_item} but names no Fuel Tank.")
	check_tank(tank)


def require_reading_tanks(doc, method=None):
	settings = get_settings()
	if not is_tracked(doc, settings):
		return
	for reading in doc.get(READINGS_FIELD) or []:
		check_tank(reading.fuel_tank, f"Reading row {reading.idx}: ")


# ---- on_submit / on_cancel --------------------------------------------------------------------
def post_stock_document(doc, method=None):
	"""Delivery Note, Purchase Receipt, Stock Entry: one row for the voucher's tank, with the litres
	its own Stock Ledger Entries moved in the fuel warehouse."""
	settings = get_settings()
	tank = doc.get(TANK_FIELD)
	if not tank or not is_tracked(doc, settings):
		return
	qty = ledger.voucher_stock_qty(doc.doctype, doc.name, settings)
	if not qty:
		return
	ledger.lock_tanks([tank])
	change = ledger.place(tank, doc.doctype, doc.name, doc_posting_datetime(doc), qty=qty)
	flags.review([change], doc.doctype, doc.name)


def post_stock_reconciliation(doc, method=None):
	settings = get_settings()
	if not is_tracked(doc, settings):
		return
	post_readings(doc, doc.get(READINGS_FIELD) or [])


def post_readings(doc, readings):
	"""One anchor row per tank read: balance = the reading, qty = reading - previous balance."""
	if not readings:
		return []
	measured = {reading.fuel_tank: flt(reading.measured_qty) for reading in readings}
	posting = doc_posting_datetime(doc)
	changes = [
		ledger.place(tank, doc.doctype, doc.name, posting, measured=measured[tank])
		for tank in ledger.lock_tanks(measured)
	]
	flags.review(changes, doc.doctype, doc.name)
	return changes


def reverse_voucher(doc, method=None):
	"""on_cancel: the voucher's rows are marked cancelled and their tanks recalculated."""
	changes = ledger.remove_voucher(doc.doctype, doc.name)
	if changes:
		flags.review(changes, doc.doctype, doc.name)
