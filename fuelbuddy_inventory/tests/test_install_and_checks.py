"""Install steps, the daily check and the reports."""

from unittest.mock import patch

import frappe

from fuelbuddy_inventory import install, integrity
from fuelbuddy_inventory.tests import fixtures as fx
from fuelbuddy_inventory.tests.base import FuelTestCase


class TestInstall(FuelTestCase):
	def test_uninstall_is_refused_while_the_ledger_has_rows(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 10, "2026-06-02 08:00:00", tank=tank)
		self.assertRaises(frappe.ValidationError, install.before_uninstall)

	def test_custom_fields_are_idempotent_and_carry_the_module(self):
		install.after_migrate()
		install.after_migrate()
		for doctype in ("Delivery Note", "Purchase Receipt", "Stock Entry"):
			fields = frappe.get_all(
				"Custom Field",
				filters={"dt": doctype, "fieldname": "fuel_tank"},
				fields=["module", "allow_on_submit", "options", "fieldtype"],
			)
			self.assertEqual(len(fields), 1, doctype)
			self.assertEqual(
				(fields[0].module, fields[0].allow_on_submit, fields[0].options, fields[0].fieldtype),
				("Fuelbuddy Inventory", 0, "Fuel Tank", "Link"),
			)
		column = frappe.db.sql(
			"""select column_type, is_nullable, column_default from information_schema.columns
			where table_schema = database() and table_name = 'tabDelivery Note' and column_name = 'fuel_tank'""",
			as_dict=True,
		)[0]
		self.assertEqual((column.column_type, column.is_nullable), ("varchar(140)", "YES"))
		self.assertIn(column.column_default, (None, "NULL"))

	def test_tank_ledger_entry_cannot_be_written_through_the_document_api(self):
		tank = fx.make_tank()
		entry = frappe.get_doc(
			{
				"doctype": "Tank Ledger Entry",
				"fuel_tank": tank,
				"voucher_type": "Delivery Note",
				"voucher_no": "x",
			}
		)
		self.assertRaises(frappe.ValidationError, entry.insert, ignore_permissions=True)
		for role in ("Stock Manager", "Stock User", "Accounts User", "Sales User"):
			perms = frappe.get_all(
				"DocPerm",
				filters={"parent": "Tank Ledger Entry", "role": role},
				fields=["read", "write", "create", "delete"],
			)
			self.assertEqual([(p.read, p.write, p.create, p.delete) for p in perms], [(1, 0, 0, 0)], role)


class TestDailyCheck(FuelTestCase):
	def test_compare_counts_every_tank_against_the_stock_ledger(self):
		tank = fx.make_tank()
		fx.stock_reconciliation({tank: 100500}, fx.TRACKING_START)  # the book holds 100000
		fx.delivery_note(tank, 100, "2026-06-02 10:00:00")
		result = integrity.compare()
		# Every test's tanks count on the tank side; this test's item alone is on the warehouse side.
		self.assertAlmostEqual(float(result.warehouse), 100400, places=3)
		self.assertTanksMatchWarehouse([tank])

	def test_a_difference_raises_one_issue_per_day(self):
		subject_prefix = f"{integrity.CODE}: "
		before = frappe.db.count("Issue", {"subject": ["like", f"{subject_prefix}%"]})
		mismatch = frappe._dict(tanks=10.0, warehouse=12.0, difference=-2.0)
		with patch.object(integrity, "compare", return_value=mismatch):
			integrity.daily_check()
			integrity.daily_check()
		self.assertEqual(frappe.db.count("Issue", {"subject": ["like", f"{subject_prefix}%"]}), before + 1)
		match = frappe._dict(tanks=12.0, warehouse=12.0, difference=0.0)
		with patch.object(integrity, "compare", return_value=match):
			self.assertEqual(integrity.daily_check(), match)
		self.assertEqual(frappe.db.count("Issue", {"subject": ["like", f"{subject_prefix}%"]}), before + 1)

	def test_daily_check_is_scheduled_at_two(self):
		self.assertIn(
			"fuelbuddy_inventory.integrity.daily_check",
			frappe.get_hooks("scheduler_events")["cron"]["0 2 * * *"],
		)


class TestReports(FuelTestCase):
	def test_reports_run(self):
		from frappe.desk.query_report import run

		tank = fx.make_tank(capacity=5000)
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=tank)
		fx.delivery_note(tank, 100, "2026-06-05 10:00:00", rate=3)
		fx.delivery_note(None, 10, "2026-06-06 10:00:00", submit=False)

		ledger = run("Tank Ledger", {"fuel_tank": tank, "from_date": "2026-06-01", "to_date": "2026-06-30"})
		self.assertEqual([row.get("balance_after") for row in ledger["result"]], [0, 1000, 900])
		balances = run("Tank Balances", {"as_of": "2026-06-30 00:00:00"})
		self.assertIn(900, [row["balance"] for row in balances["result"] if row["fuel_tank"] == tank])
		days = run("Tank vs Warehouse", {"from_date": "2026-06-01", "to_date": "2026-06-07"})
		self.assertEqual(len(days["result"]), 7)
		margin = run(
			"Truck Margin",
			{
				"from_date": "2026-06-01",
				"to_date": "2026-06-30",
				"group_by": "Tank",
				"vehicle_registration": "TEST-TRUCK-1",
			},
		)
		mine = next(row for row in margin["result"] if row.get("fuel_tank") == tank)
		self.assertEqual((mine["litres"], mine["revenue"]), (100, 300))
		self.assertAlmostEqual(mine["cost"], 200, places=2)  # FIFO from the seed at 2.0 / L
		exceptions = run("Tank Exceptions", {})
		self.assertTrue(any(row.get("kind") == "Draft without tank" for row in exceptions["result"]))
