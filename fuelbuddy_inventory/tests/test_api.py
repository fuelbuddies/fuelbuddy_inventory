"""The whitelisted API. Write methods commit, so fixtures are committed before calling them."""

import uuid

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from fuelbuddy_inventory import api
from fuelbuddy_inventory.tests import fixtures as fx
from fuelbuddy_inventory.tests.base import FuelTestCase


class TestTankTransferApi(FuelTestCase):
	def setUp(self):
		super().setUp()
		self.source, self.target = fx.make_tank(), fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=self.source)
		frappe.db.commit()

	def call(self, op_key, qty=100, source=None, target=None):
		return api.create_tank_transfer(
			op_key=op_key,
			from_tank=source or self.source,
			to_tank=target or self.target,
			qty=qty,
			posting_datetime="2026-06-03 10:00:00",
			source_request_id="req-test",
		)

	def test_replay_returns_the_same_transfer(self):
		key = f"erp-tank-transfer-{uuid.uuid4()}"
		first = self.call(key)
		self.assertTrue(first["ok"], first)
		self.assertEqual((first["code"], first["retryable"], first["adopted"]), (None, False, False))
		second = self.call(key)
		self.assertTrue(second["ok"])
		self.assertEqual((second["name"], second["adopted"]), (first["name"], True))
		self.assertEqual(fx.balances(self.source), [1000, 900])
		self.assertEqual(fx.balances(self.target), [100])
		self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_no": first["name"]}))

	def test_cancelled_key_is_never_posted_again(self):
		key = f"erp-tank-transfer-{uuid.uuid4()}"
		name = self.call(key)["name"]
		frappe.get_doc("Tank Transfer", name).cancel()
		frappe.db.commit()
		answer = self.call(key)
		self.assertEqual(
			(answer["ok"], answer["code"], answer["retryable"]), (False, "CANCELLED_IN_ERP", False)
		)
		self.assertIn(name, answer["message"])
		self.assertIsNone(answer["name"])

	def test_draft_is_adopted_only_when_it_matches(self):
		key = f"erp-tank-transfer-{uuid.uuid4()}"
		draft = fx.tank_transfer(self.source, self.target, 100, "2026-06-03 10:00:00", submit=False)
		draft.db_set("op_key", key)
		frappe.db.commit()
		self.assertEqual(self.call(key, target=fx.make_tank())["code"], "TANK_MISMATCH")
		self.assertEqual(self.call(key, qty=101)["code"], "QTY_MISMATCH")
		self.assertEqual(frappe.db.get_value("Tank Transfer", draft.name, "docstatus"), 0)
		answer = self.call(key)
		self.assertTrue(answer["ok"], answer)
		self.assertEqual((answer["name"], answer["adopted"], answer["docstatus"]), (draft.name, True, 1))

	def test_refusals_carry_their_codes(self):
		key = f"erp-tank-transfer-{uuid.uuid4()}"
		self.assertEqual(self.call(key, target=self.source)["code"], "SAME_TANK")
		self.assertEqual(self.call(key, qty=0)["code"], "BAD_QTY")
		self.assertEqual(self.call(key, target="no-such-tank")["code"], "TANK_NOT_FOUND")
		frappe.db.set_value("Fuel Tank", self.target, "disabled", 1)
		frappe.db.commit()
		answer = self.call(key)
		self.assertEqual((answer["ok"], answer["code"]), (False, "TANK_NOT_FOUND"))
		self.assertTrue(answer["message"].startswith("TANK_NOT_FOUND: "))
		self.assertFalse(frappe.db.exists("Tank Transfer", {"op_key": key}))


class TestReadApi(FuelTestCase):
	def test_ledger_rows_carry_voucher_keys_and_follow_recalculation(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=tank)
		dn = fx.delivery_note(tank, 100, "2026-06-10 10:00:00")
		answer = api.get_tank_ledger(tank_ids=[tank])
		self.assertTrue(answer["ok"])
		rows = answer["result"]["rows"]
		self.assertEqual([row["voucher_type"] for row in rows], ["Stock Entry", "Delivery Note"])
		for key in (
			"fuel_tank",
			"posting_datetime",
			"voucher_no",
			"qty",
			"balance_after",
			"is_anchor",
			"is_cancelled",
			"custom_invoiced_item_id",
			"custom_app_op_key",
		):
			self.assertIn(key, rows[0])
		self.assertEqual(rows[1]["voucher_no"], dn.name)
		self.assertEqual((rows[1]["qty"], rows[1]["balance_after"]), (-100, 900))

		since = max(get_datetime(row["modified"]) for row in rows)
		since = add_to_date(since, seconds=1)
		self.assertEqual(api.get_tank_ledger(tank_ids=[tank], since=str(since))["result"]["rows"], [])
		frappe.db.sql("do sleep(1.1)")
		fx.delivery_note(tank, 50, "2026-06-05 10:00:00")  # back-dated: moves the later delivery's row
		changed = api.get_tank_ledger(tank_ids=frappe.as_json([tank]), since=str(since))["result"]["rows"]
		self.assertEqual(sorted(row["balance_after"] for row in changed), [850, 950])

	def test_balances_as_of_and_pending_drafts(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=tank)
		fx.delivery_note(tank, 100, "2026-06-10 10:00:00")
		fx.delivery_note(tank, 30, "2026-06-11 10:00:00", submit=False)
		fx.delivery_note(tank, 30, "2026-06-20 10:00:00", submit=False)
		answer = api.get_tank_balances(tank_ids=[tank, "no-such-tank"], as_of="2026-06-15 00:00:00")
		self.assertTrue(answer["ok"])
		found, missing = answer["result"]["tanks"]
		self.assertEqual((found["fuel_tank"], found["balance"], found["pending_drafts"]), (tank, 900, 1))
		self.assertEqual((missing["found"], missing["balance"], missing["pending_drafts"]), (False, 0, 0))
		early = api.get_tank_balances(tank_ids=[tank], as_of="2026-06-05 00:00:00")["result"]["tanks"][0]
		self.assertEqual((early["balance"], early["pending_drafts"]), (1000, 0))

	def test_bad_input_is_refused(self):
		answer = api.get_tank_balances(tank_ids="[not json", as_of="2026-06-15")
		self.assertEqual(
			(answer["ok"], answer["code"], answer["retryable"]), (False, "ERP_VALIDATION", False)
		)


class TestUpsertFuelTank(FuelTestCase):
	def test_upsert_creates_then_updates_by_app_tank_id(self):
		tank_id = str(uuid.uuid4())
		first = api.upsert_fuel_tank(
			app_tank_id=tank_id,
			label="TEST-TRUCK-9 C1",
			vehicle_registration="TEST-TRUCK-9",
			vehicle_type="BOWSER",
			tank_type="Fuel Tank",
			compartment_no=1,
			capacity=1000,
			capacity_unit="IMPERIAL_GALLON",
			active=1,
		)
		self.assertTrue(first["ok"], first)
		self.assertEqual((first["name"], first["created"], first["disabled"]), (tank_id, True, 0))
		tank = frappe.get_doc("Fuel Tank", tank_id)
		self.assertEqual((tank.label, tank.capacity_litres), ("TEST-TRUCK-9 C1", 4546))

		second = api.upsert_fuel_tank(
			app_tank_id=tank_id, label="TEST-TRUCK-10 C1", vehicle_type="BOWSER", capacity_litres=5000
		)
		self.assertEqual((second["name"], second["created"]), (tank_id, False))
		tank.reload()
		self.assertEqual(
			(tank.label, tank.capacity_litres, tank.app_tank_id), ("TEST-TRUCK-10 C1", 5000, tank_id)
		)

	def test_upsert_disables_out_of_scope_test_and_inactive_tanks(self):
		cases = [
			{"vehicle_type": "TPT_TANK"},
			{"vehicle_type": "BOWSER", "is_test": 1},
			{"vehicle_type": "CONTAINER", "active": 0},
			{"vehicle_type": "CONTAINER", "vehicle_active": "false"},
			{"vehicle_type": None},
		]
		for case in cases:
			answer = api.upsert_fuel_tank(app_tank_id=str(uuid.uuid4()), label="TEST-TRUCK-11 C1", **case)
			self.assertTrue(answer["ok"], answer)
			self.assertEqual(answer["disabled"], 1, case)
		enabled = api.upsert_fuel_tank(
			app_tank_id=str(uuid.uuid4()), label="TEST-TRUCK-11 C2", vehicle_type="container", is_test=0
		)
		self.assertEqual(enabled["disabled"], 0)

	def test_upsert_refuses_bad_input(self):
		answer = api.upsert_fuel_tank(app_tank_id="", label="x", vehicle_type="BOWSER")
		self.assertEqual((answer["ok"], answer["code"]), (False, "ERP_VALIDATION"))
		answer = api.upsert_fuel_tank(
			app_tank_id=str(uuid.uuid4()),
			label="x",
			vehicle_type="BOWSER",
			capacity=5,
			capacity_unit="BARREL",
		)
		self.assertEqual((answer["ok"], answer["code"]), (False, "ERP_VALIDATION"))
