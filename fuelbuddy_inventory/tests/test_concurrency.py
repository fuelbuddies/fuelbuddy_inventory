"""Two deliveries on one tank at the same moment, from two connections.

The fixtures are committed so the other connections see them, and each worker commits its own
delivery, so this leaves committed test data on the test site (made-up values only)."""

import threading

import frappe

from fuelbuddy_inventory.tests import fixtures as fx
from fuelbuddy_inventory.tests.base import FuelTestCase


class TestConcurrency(FuelTestCase):
	def test_two_deliveries_on_one_tank_at_once(self):
		tank = fx.make_tank()
		other = fx.make_tank()
		fx.stock_entry("Material Receipt", 10000, "2026-06-02 08:00:00", tank=tank)
		fx.stock_entry("Material Receipt", 10000, "2026-06-02 09:00:00", tank=other)
		frappe.db.commit()

		site, sites_path = frappe.local.site, frappe.local.sites_path
		barrier = threading.Barrier(3)
		errors, names = [], []

		def deliver(litres, posting, transfer=False):
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				barrier.wait(timeout=30)
				if transfer:
					doc = fx.tank_transfer(other, tank, litres, posting)
				else:
					doc = fx.delivery_note(tank, litres, posting)
				frappe.db.commit()
				names.append(doc.name)
			except Exception as exc:
				frappe.db.rollback()
				errors.append(repr(exc))
			finally:
				frappe.destroy()

		threads = [
			threading.Thread(target=deliver, args=(100, "2026-07-01 10:00:00")),
			threading.Thread(target=deliver, args=(200, "2026-07-01 10:00:01")),
			# a transfer touching both tanks, in the opposite lock order to nothing: tanks are locked sorted
			threading.Thread(target=deliver, args=(50, "2026-07-01 09:00:00", True)),
		]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=120)

		frappe.db.commit()  # a fresh snapshot: see what the workers committed
		self.assertEqual(errors, [])
		self.assertEqual(len(names), 3)
		self.assertEqual(fx.balances(tank), [10000, 10050, 9950, 9750])
		self.assertEqual(fx.balances(other), [10000, 9950])
		self.assertChain(tank)
		self.assertChain(other)
