"""Writes at the same moment, from several connections.

The fixtures are committed so the other connections see them, and each worker commits its own
write, so this leaves committed test data on the test site (made-up values only)."""

import threading
import uuid

import frappe

from fuelbuddy_inventory import api
from fuelbuddy_inventory.tests import fixtures as fx
from fuelbuddy_inventory.tests.base import FuelTestCase


def run_at_once(calls):
	"""Run each (fn, args) on its own connection, all released together. Returns (results, errors)."""
	site, sites_path = frappe.local.site, frappe.local.sites_path
	barrier = threading.Barrier(len(calls))
	results, errors = [], []

	def worker(fn, args):
		frappe.init(site=site, sites_path=sites_path)
		frappe.connect()
		frappe.set_user("Administrator")
		try:
			barrier.wait(timeout=30)
			results.append(fn(*args))
		except Exception as exc:
			frappe.db.rollback()
			errors.append(repr(exc))
		finally:
			frappe.destroy()

	threads = [threading.Thread(target=worker, args=call) for call in calls]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join(timeout=120)
	frappe.db.commit()  # a fresh snapshot: see what the workers committed
	return results, errors


class TestConcurrency(FuelTestCase):
	def test_two_deliveries_on_one_tank_at_once(self):
		tank = fx.make_tank()
		other = fx.make_tank()
		fx.stock_entry("Material Receipt", 10000, "2026-06-02 08:00:00", tank=tank)
		fx.stock_entry("Material Receipt", 10000, "2026-06-02 09:00:00", tank=other)
		frappe.db.commit()

		def deliver(litres, posting, transfer=False):
			if transfer:
				doc = fx.tank_transfer(other, tank, litres, posting)
			else:
				doc = fx.delivery_note(tank, litres, posting)
			frappe.db.commit()
			return doc.name

		names, errors = run_at_once(
			[
				(deliver, (100, "2026-07-01 10:00:00")),
				(deliver, (200, "2026-07-01 10:00:01")),
				# a transfer touching both tanks, in the opposite lock order to nothing: tanks are locked sorted
				(deliver, (50, "2026-07-01 09:00:00", True)),
			]
		)

		self.assertEqual(errors, [])
		self.assertEqual(len(names), 3)
		self.assertEqual(fx.balances(tank), [10000, 10050, 9950, 9750])
		self.assertEqual(fx.balances(other), [10000, 9950])
		self.assertChain(tank)
		self.assertChain(other)

	def test_first_sync_of_new_tanks_at_once(self):
		"""Eight new tanks, and one more new tank three times, from eleven connections at once (an ops
		"Sync all" on a first sync). The ids share a prefix no other tank has, so they all fall in one gap
		of the Fuel Tank primary key: the gap a locking read of a missing tank locked, and the inserts
		then deadlocked on. Every call is answered ok, and each tank is created exactly once."""
		prefix = f"TEST-CONC-{uuid.uuid4()}"
		distinct = [f"{prefix}-{index:02d}" for index in range(8)]
		same = f"{prefix}-same"

		def upsert(tank_id):
			return api.upsert_fuel_tank(
				app_tank_id=tank_id,
				label=f"TEST-CONC {tank_id[-4:]}",
				vehicle_registration="TEST-CONC",
				vehicle_type="BOWSER",
				tank_type="Fuel Tank",
				compartment_no=1,
				capacity_litres=1000,
				is_test=1,  # left behind disabled
			)

		answers, errors = run_at_once([(upsert, (tank_id,)) for tank_id in [*distinct, same, same, same]])

		self.assertEqual(errors, [])
		self.assertEqual(len(answers), 11)
		for answer in answers:
			self.assertEqual((answer["ok"], answer["code"]), (True, None), answer)
		by_name = {}
		for answer in answers:
			by_name.setdefault(answer["name"], []).append(answer["created"])
		self.assertEqual(sorted(by_name), sorted([*distinct, same]))
		for tank_id in distinct:
			self.assertEqual(by_name[tank_id], [True], tank_id)
		self.assertEqual(sorted(by_name[same]), [False, False, True])
		self.assertEqual(frappe.db.count("Fuel Tank", {"name": ["like", f"{prefix}-%"]}), 9)
