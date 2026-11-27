"""Test fixtures. Every value here is made up: no real vehicles, tanks, customers or suppliers.

Each test gets its own fuel item (fresh code), so ERP's warehouse quantity in the shared fuel
warehouse belongs to that test alone, and its own tanks (generated uuids). The fuel item is seeded
with stock dated before tracking start, which the tank ledger does not follow.
"""

import uuid

import frappe
from frappe.utils import add_days, flt

from fuelbuddy_inventory import ledger
from fuelbuddy_inventory.settings import SETTINGS

TRACKING_START = "2026-06-01 00:00:00"
SEED_DATE = "2026-05-15"
FUEL_STORE = "_Test FI Fuel Store"
OTHER_STORE = "_Test FI Other Store"
CUSTOMER = "_Test FI Customer"
SUPPLIER = "_Test FI Supplier"
LITRE = "Litre"
IG = "_Test FI IG"
LITRES_PER_IG = 4.546


def before_tests():
	"""ERPNext's own test setup (setup wizard: a company, fiscal year, defaults)."""
	from erpnext.setup.utils import before_tests as erpnext_before_tests

	erpnext_before_tests()


def company():
	return frappe.db.get_value("Company", {}, "name", order_by="creation asc")


def abbr():
	return frappe.get_cached_value("Company", company(), "abbr")


def warehouse(name):
	return f"{name} - {abbr()}"


def setup_base():
	"""Company-level fixtures shared by every test (created once, committed)."""
	co = company()
	for name in (FUEL_STORE, OTHER_STORE):
		if not frappe.db.exists("Warehouse", warehouse(name)):
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": name,
					"company": co,
					"parent_warehouse": frappe.db.get_value(
						"Warehouse", {"company": co, "is_group": 1}, "name"
					),
				}
			).insert(ignore_permissions=True)
	for uom in (LITRE, IG):
		if not frappe.db.exists("UOM", uom):
			frappe.get_doc({"doctype": "UOM", "uom_name": uom, "must_be_whole_number": 0}).insert()
	if not frappe.db.exists("Customer", CUSTOMER):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": CUSTOMER,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Supplier", SUPPLIER):
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": SUPPLIER,
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)
	frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
	frappe.db.commit()


def new_fuel_item(seed_litres=100000, rate=2.0):
	"""A fresh stock item (litres, FIFO), set as the fuel item, seeded before tracking start."""
	code = f"_Test FI Diesel {uuid.uuid4().hex[:8]}"
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
			"stock_uom": LITRE,
			"is_stock_item": 1,
			"valuation_method": "FIFO",
			"uoms": [{"uom": LITRE, "conversion_factor": 1}, {"uom": IG, "conversion_factor": LITRES_PER_IG}],
		}
	).insert(ignore_permissions=True)
	set_settings(fuel_item=code)
	if seed_litres:
		stock_entry("Material Receipt", seed_litres, f"{SEED_DATE} 08:00:00", tank=None, rate=rate)
	return code


def set_settings(**values):
	settings = frappe.get_doc(SETTINGS)
	settings.update(
		{
			"fuel_warehouse": warehouse(FUEL_STORE),
			"tracking_start": TRACKING_START,
			"approval_threshold_litres": 100,
			**values,
		}
	)
	settings.save(ignore_permissions=True)
	return settings


def make_tank(capacity=None, vehicle="TEST-TRUCK-1", label=None, vehicle_type="BOWSER"):
	tank_id = str(uuid.uuid4())
	return (
		frappe.get_doc(
			{
				"doctype": "Fuel Tank",
				"app_tank_id": tank_id,
				"label": label or f"{vehicle} T{tank_id[:4]}",
				"vehicle_registration": vehicle,
				"vehicle_type": vehicle_type,
				"tank_type": "Fuel Tank",
				"compartment_no": 1,
				"capacity_litres": capacity or 0,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _split(posting):
	date, time = posting.split(" ")
	return date, time


def fuel_item():
	return frappe.get_cached_doc(SETTINGS).fuel_item


def delivery_note(tank, litres, posting, submit=True, store=FUEL_STORE, rate=2.5, uom=LITRE):
	date, time = _split(posting)
	co = company()
	doc = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": co,
			"customer": CUSTOMER,
			"currency": frappe.get_cached_value("Company", co, "default_currency"),
			"posting_date": date,
			"posting_time": time,
			"set_posting_time": 1,
			"fuel_tank": tank,
			"items": [
				{
					"item_code": fuel_item(),
					"qty": litres,
					"uom": uom,
					"conversion_factor": 1 if uom == LITRE else LITRES_PER_IG,
					"rate": rate,
					"warehouse": warehouse(store),
					"cost_center": frappe.get_cached_value("Company", co, "cost_center"),
				}
			],
		}
	)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def purchase_receipt(tank, qty, posting, uom=IG, submit=True, store=FUEL_STORE, rate=9.0):
	date, time = _split(posting)
	co = company()
	doc = frappe.get_doc(
		{
			"doctype": "Purchase Receipt",
			"company": co,
			"supplier": SUPPLIER,
			"currency": frappe.get_cached_value("Company", co, "default_currency"),
			"posting_date": date,
			"posting_time": time,
			"set_posting_time": 1,
			"fuel_tank": tank,
			"items": [
				{
					"item_code": fuel_item(),
					"qty": qty,
					"received_qty": qty,
					"uom": uom,
					"conversion_factor": LITRES_PER_IG if uom == IG else 1,
					"rate": rate,
					"warehouse": warehouse(store),
					"cost_center": frappe.get_cached_value("Company", co, "cost_center"),
				}
			],
		}
	)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def stock_entry(purpose, litres, posting, tank=None, source=None, target=None, submit=True, rate=2.0):
	"""Material Issue takes from the fuel store, Material Receipt puts into it, unless told otherwise."""
	date, time = _split(posting)
	co = company()
	if purpose == "Material Issue" and source is None:
		source = FUEL_STORE
	if purpose == "Material Receipt" and target is None:
		target = FUEL_STORE
	doc = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"company": co,
			"purpose": purpose,
			"stock_entry_type": purpose,
			"posting_date": date,
			"posting_time": time,
			"set_posting_time": 1,
			"fuel_tank": tank,
			"items": [
				{
					"item_code": fuel_item(),
					"qty": litres,
					"uom": LITRE,
					"stock_uom": LITRE,
					"conversion_factor": 1,
					"transfer_qty": litres,
					"s_warehouse": warehouse(source) if source else None,
					"t_warehouse": warehouse(target) if target else None,
					"basic_rate": rate,
					"cost_center": frappe.get_cached_value("Company", co, "cost_center"),
					"expense_account": frappe.get_cached_value("Company", co, "stock_adjustment_account"),
				}
			],
		}
	)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def stock_reconciliation(readings, posting, qty=None, submit=True, rate=2.0, store=FUEL_STORE):
	"""A count of the fuel item: `readings` is {tank: litres}; qty defaults to their sum."""
	date, time = _split(posting)
	co = company()
	doc = frappe.get_doc(
		{
			"doctype": "Stock Reconciliation",
			"company": co,
			"purpose": "Stock Reconciliation",
			"posting_date": date,
			"posting_time": time,
			"set_posting_time": 1,
			"expense_account": frappe.get_cached_value("Company", co, "stock_adjustment_account"),
			"cost_center": frappe.get_cached_value("Company", co, "cost_center"),
			"items": [
				{
					"item_code": fuel_item(),
					"warehouse": warehouse(store),
					"qty": sum(readings.values()) if qty is None else qty,
					"valuation_rate": rate,
				}
			],
			"fuel_tank_readings": [
				{"fuel_tank": tank, "measured_qty": litres} for tank, litres in readings.items()
			],
		}
	)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def tank_transfer(from_tank, to_tank, litres, posting, submit=True):
	date, time = _split(posting)
	doc = frappe.get_doc(
		{
			"doctype": "Tank Transfer",
			"from_tank": from_tank,
			"to_tank": to_tank,
			"qty": litres,
			"posting_date": date,
			"posting_time": time,
		}
	)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


def rows(tank, include_cancelled=False):
	"""The tank's ledger rows in ledger order, as dicts with float quantities."""
	cancelled = "" if include_cancelled else "and is_cancelled = 0"
	result = frappe.db.sql(
		f"""select name, posting_datetime, voucher_type, voucher_no, qty, balance_after, is_anchor,
			is_cancelled
		from {ledger.TABLE} where fuel_tank = %s {cancelled}
		order by posting_datetime, creation, name""",
		(tank,),
		as_dict=True,
	)
	for row in result:
		row.qty = flt(row.qty, 3)
		row.balance_after = flt(row.balance_after, 3)
	return result


def balances(tank):
	return [row.balance_after for row in rows(tank)]


def recompute(tank):
	"""The tank's balances recomputed from scratch (anchors reset), to check the stored ones."""
	balance = 0.0
	result = []
	for row in rows(tank):
		balance = row.balance_after if row.is_anchor else flt(balance + row.qty, 3)
		result.append(balance)
	return result


def tank_issues(tank, code=None):
	filters = {"issue_type": "Fuel Tank", "subject": ["like", f"{code or '%'}: {tank} %"]}
	return frappe.get_all("Issue", filters=filters, fields=["name", "subject", "status"])
