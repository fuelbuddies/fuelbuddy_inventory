"""Refusals: each is raised as the exception class named after its code, message prefixed `CODE: `."""

import frappe

from fuelbuddy_inventory import stock_hooks
from fuelbuddy_inventory.tests import fixtures as fx
from fuelbuddy_inventory.tests.base import FuelTestCase


class TestGuards(FuelTestCase):
	def test_fuel_document_without_a_tank_saves_as_draft_but_is_refused_at_submit(self):
		dn = fx.delivery_note(None, 100, "2026-06-05 10:00:00", submit=False)
		self.assertEqual(dn.docstatus, 0)
		self.assertRefused("TANK_MISSING", dn.submit)
		for make in (
			lambda: fx.purchase_receipt(None, 10, "2026-06-05 10:00:00", submit=False),
			lambda: fx.stock_entry("Material Issue", 10, "2026-06-05 10:00:00", submit=False),
		):
			self.assertRefused("TANK_MISSING", make().submit)

	def test_disabled_or_unknown_tank_is_refused_at_submit(self):
		tank = fx.make_tank()
		dn = fx.delivery_note(tank, 100, "2026-06-05 10:00:00", submit=False)
		frappe.db.set_value("Fuel Tank", tank, "disabled", 1)
		self.assertRefused("TANK_NOT_FOUND", dn.submit)
		self.assertRefused("TANK_NOT_FOUND", stock_hooks.check_tank, "no-such-tank")

	def test_fuel_outside_the_fuel_warehouse_is_refused(self):
		tank = fx.make_tank()
		self.assertRefused(
			"WRONG_FUEL_WAREHOUSE",
			fx.delivery_note,
			tank,
			100,
			"2026-06-05 10:00:00",
			submit=False,
			store=fx.OTHER_STORE,
		)
		self.assertRefused(
			"WRONG_FUEL_WAREHOUSE",
			fx.stock_entry,
			"Material Transfer",
			10,
			"2026-06-05 10:00:00",
			tank=tank,
			source=fx.FUEL_STORE,
			target=fx.OTHER_STORE,
			submit=False,
		)

	def test_same_warehouse_transfer_is_refused(self):
		self.assertRefused(
			"STOCK_PATH_NOT_ALLOWED",
			fx.stock_entry,
			"Material Transfer",
			10,
			"2026-06-05 10:00:00",
			tank=fx.make_tank(),
			source=fx.FUEL_STORE,
			target=fx.FUEL_STORE,
			submit=False,
		)

	def test_repack_and_stock_updating_invoices_are_refused(self):
		posting = {"posting_date": "2026-06-05", "posting_time": "10:00:00"}
		item = {"item_code": self.item, "qty": 10}
		repack = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Repack",
				**posting,
				"items": [{**item, "s_warehouse": fx.warehouse(fx.FUEL_STORE)}],
			}
		)
		self.assertRefused("STOCK_PATH_NOT_ALLOWED", stock_hooks.validate_stock_entry, repack)
		for doctype in ("Sales Invoice", "Purchase Invoice"):
			invoice = frappe.get_doc(
				{
					"doctype": doctype,
					"update_stock": 1,
					**posting,
					"items": [{**item, "warehouse": fx.warehouse(fx.FUEL_STORE)}],
				}
			)
			self.assertRefused("STOCK_PATH_NOT_ALLOWED", stock_hooks.refuse_invoice_stock_update, invoice)
			invoice.update_stock = 0
			stock_hooks.refuse_invoice_stock_update(invoice)  # no stock update: no refusal

	def test_count_of_fuel_needs_readings_that_add_up(self):
		tank = fx.make_tank()
		no_readings = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"company": fx.company(),
				"purpose": "Stock Reconciliation",
				"posting_date": "2026-06-05",
				"posting_time": "10:00:00",
				"set_posting_time": 1,
				"items": [
					{
						"item_code": self.item,
						"warehouse": fx.warehouse(fx.FUEL_STORE),
						"qty": 900,
						"valuation_rate": 2,
					}
				],
			}
		)
		self.assertRefused("STOCK_PATH_NOT_ALLOWED", no_readings.insert)
		self.assertRefused(
			"READINGS_MISMATCH",
			fx.stock_reconciliation,
			{tank: 800},
			"2026-06-05 10:00:00",
			qty=900,
			submit=False,
		)

	def test_tank_transfer_refuses_same_tank_and_bad_quantities(self):
		tank, other = fx.make_tank(), fx.make_tank()
		self.assertRefused("SAME_TANK", fx.tank_transfer, tank, tank, 10, "2026-06-05 10:00:00")
		self.assertRefused("BAD_QTY", fx.tank_transfer, tank, other, 0, "2026-06-05 10:00:00")
		self.assertRefused("BAD_QTY", fx.tank_transfer, tank, other, -5, "2026-06-05 10:00:00")

	def test_refusal_messages_never_mention_capacity(self):
		"""erp-functions classes an ERP_VALIDATION mentioning capacity as fixable; refusals are not."""
		dn = fx.delivery_note(None, 100, "2026-06-05 10:00:00", submit=False)
		refusal = self.assertRefused("TANK_MISSING", dn.submit)
		self.assertNotIn("capacity", str(refusal).lower())


class TestPeriodClosed(FuelTestCase):
	def test_tank_transfer_in_a_closed_accounting_period_is_refused(self):
		tank, other = fx.make_tank(), fx.make_tank()
		frappe.get_doc(
			{
				"doctype": "Accounting Period",
				"period_name": f"_Test FI closed {frappe.generate_hash(length=6)}",
				"company": fx.company(),
				"start_date": "2026-08-01",
				"end_date": "2026-08-31",
				"closed_documents": [{"document_type": "Tank Transfer", "closed": 1}],
			}
		).insert(ignore_permissions=True)
		self.assertRefused("PERIOD_CLOSED", fx.tank_transfer, tank, other, 10, "2026-08-10 10:00:00")
		fx.tank_transfer(tank, other, 10, "2026-09-01 10:00:00")  # outside the period
		self.assertIn("Tank Transfer", frappe.get_hooks("period_closing_doctypes"))
