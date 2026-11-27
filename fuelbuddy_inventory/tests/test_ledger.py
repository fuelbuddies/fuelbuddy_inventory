"""Posting, correction, back-dating and counts on the tank ledger."""

import frappe
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from frappe.utils import flt

from fuelbuddy_inventory import ledger
from fuelbuddy_inventory.tests import fixtures as fx
from fuelbuddy_inventory.tests.base import FuelTestCase


def amend(doc):
	"""ERPNext's amend: a copy of the cancelled document, linked by amended_from."""
	new = frappe.copy_doc(doc)
	new.amended_from = doc.name
	new.docstatus = 0
	new.insert(ignore_permissions=True)
	new.submit()
	return new


class TestPosting(FuelTestCase):
	def test_delivery_note_submit_cancel_amend(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 5000, "2026-06-02 08:00:00", tank=tank)
		dn = fx.delivery_note(tank, 100, "2026-06-10 10:00:00")
		self.assertEqual(fx.balances(tank), [5000, 4900])
		self.assertLedgerMatchesStock(dn)
		row = fx.rows(tank)[-1]
		self.assertEqual((row.voucher_type, row.voucher_no, row.qty), ("Delivery Note", dn.name, -100))

		dn.cancel()
		self.assertEqual(fx.balances(tank), [5000])
		self.assertEqual([r.is_cancelled for r in fx.rows(tank, include_cancelled=True)], [0, 1])

		amended = amend(dn)
		self.assertEqual(amended.fuel_tank, tank)
		self.assertEqual(fx.balances(tank), [5000, 4900])
		self.assertEqual(fx.rows(tank)[-1].voucher_no, amended.name)
		self.assertChain(tank)

	def test_purchase_receipt_in_imperial_gallons(self):
		tank = fx.make_tank()
		pr = fx.purchase_receipt(tank, 100, "2026-06-03 09:00:00", uom=fx.IG)
		self.assertEqual(fx.balances(tank), [454.6])
		self.assertLedgerMatchesStock(pr)

		pr.cancel()
		self.assertEqual(fx.balances(tank), [])
		amended = amend(pr)
		self.assertEqual(fx.balances(tank), [454.6])
		self.assertLedgerMatchesStock(amended)

	def test_stock_entry_own_use(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=tank)
		issue = fx.stock_entry("Material Issue", 50, "2026-06-04 08:00:00", tank=tank)
		self.assertEqual(fx.balances(tank), [1000, 950])
		self.assertLedgerMatchesStock(issue)
		issue.cancel()
		self.assertEqual(fx.balances(tank), [1000])

	def test_return_delivery_goes_back_into_its_tank(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=tank)
		dn = fx.delivery_note(tank, 200, "2026-06-05 10:00:00")
		ret = make_return_doc("Delivery Note", dn.name)
		ret.posting_date, ret.posting_time, ret.set_posting_time = "2026-06-06", "10:00:00", 1
		ret.insert(ignore_permissions=True)
		ret.submit()
		self.assertEqual(ret.fuel_tank, tank)
		self.assertEqual(fx.balances(tank), [1000, 800, 1000])
		self.assertEqual(fx.rows(tank)[-1].qty, 200)
		self.assertLedgerMatchesStock(ret)

	def test_documents_before_tracking_start_need_no_tank_and_write_nothing(self):
		dn = fx.delivery_note(None, 100, "2026-05-20 10:00:00")
		self.assertEqual(dn.docstatus, 1)
		self.assertFalse(frappe.db.exists("Tank Ledger Entry", {"voucher_no": dn.name}))

	def test_tank_transfer_moves_litres_between_tanks_only(self):
		source, target = fx.make_tank(), fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=source)
		warehouse_before = ledger.warehouse_qty_as_of()
		transfer = fx.tank_transfer(source, target, 300, "2026-06-03 08:00:00")
		self.assertEqual(fx.balances(source), [1000, 700])
		self.assertEqual(fx.balances(target), [300])
		self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_no": transfer.name}))
		self.assertFalse(frappe.db.exists("Stock Entry", {"remarks": ["like", f"%{transfer.name}%"]}))
		self.assertEqual(ledger.warehouse_qty_as_of(), warehouse_before)
		transfer.cancel()
		self.assertEqual(fx.balances(source), [1000])
		self.assertEqual(fx.balances(target), [])


class TestBackDating(FuelTestCase):
	def test_back_dated_delivery_shifts_only_its_tank_up_to_the_next_count(self):
		a, b = fx.make_tank(), fx.make_tank()
		fx.stock_entry("Material Receipt", 5000, "2026-06-02 08:00:00", tank=a)
		fx.stock_entry("Material Receipt", 3000, "2026-06-02 09:00:00", tank=b)
		fx.delivery_note(a, 100, "2026-06-10 10:00:00")
		fx.delivery_note(b, 100, "2026-06-12 10:00:00")
		count = fx.stock_reconciliation({a: 4000, b: 2900}, "2026-06-20 18:00:00")
		fx.delivery_note(a, 200, "2026-06-25 10:00:00")
		self.assertEqual(fx.balances(a), [5000, 4900, 4000, 3800])
		anchor = fx.rows(a)[2]
		self.assertEqual((anchor.voucher_no, anchor.is_anchor, anchor.qty), (count.name, 1, -900))
		after_count = {row.name: (row.balance_after, row.qty) for row in fx.rows(a)[3:]}
		b_before = fx.rows(b)

		late = fx.delivery_note(a, 50, "2026-06-15 10:00:00")  # lands between the delivery and the count

		self.assertEqual(fx.balances(a), [5000, 4900, 4850, 4000, 3800])
		anchor = fx.rows(a)[3]
		self.assertEqual((anchor.balance_after, anchor.qty), (4000, -850))  # the count absorbs it
		self.assertEqual({row.name: (row.balance_after, row.qty) for row in fx.rows(a)[4:]}, after_count)
		self.assertEqual(fx.rows(b), b_before)  # no other tank is touched
		self.assertLedgerMatchesStock(late)
		self.assertChain(a)
		self.assertChain(b)

		late.cancel()  # cancelling before the count gives the difference back to the count
		self.assertEqual(fx.balances(a), [5000, 4900, 4000, 3800])
		self.assertEqual(fx.rows(a)[2].qty, -900)
		self.assertEqual({row.name: (row.balance_after, row.qty) for row in fx.rows(a)[3:]}, after_count)
		self.assertTanksMatchWarehouse([a, b])

	def test_back_dated_delivery_without_a_count_shifts_every_later_row(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=tank)
		fx.delivery_note(tank, 100, "2026-06-05 10:00:00")
		fx.delivery_note(tank, 100, "2026-06-07 10:00:00")
		fx.delivery_note(tank, 50, "2026-06-03 10:00:00")
		self.assertEqual(fx.balances(tank), [1000, 950, 850, 750])
		self.assertChain(tank)


class TestCounts(FuelTestCase):
	def test_count_anchors_each_tank_and_cancel_recomputes_forward(self):
		a, b = fx.make_tank(), fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=a)
		fx.stock_entry("Material Receipt", 500, "2026-06-02 09:00:00", tank=b)
		count = fx.stock_reconciliation({a: 980, b: 510}, "2026-06-10 18:00:00")
		fx.delivery_note(a, 80, "2026-06-12 10:00:00")
		self.assertEqual(fx.balances(a), [1000, 980, 900])
		self.assertEqual([(r.qty, r.is_anchor) for r in fx.rows(b)], [(500, 0), (10, 1)])
		self.assertTanksMatchWarehouse([a, b])

		count.cancel()
		self.assertEqual(fx.balances(a), [1000, 920])
		self.assertEqual(fx.balances(b), [500])
		self.assertChain(a)

	def test_late_delivery_before_a_count_changes_the_count_not_later_balances(self):
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 2000, "2026-06-02 08:00:00", tank=tank)
		fx.stock_reconciliation({tank: 1500}, "2026-06-30 23:59:59")
		fx.delivery_note(tank, 100, "2026-07-02 10:00:00")
		self.assertEqual(fx.balances(tank), [2000, 1500, 1400])

		fx.delivery_note(tank, 300, "2026-06-20 10:00:00")
		self.assertEqual(fx.balances(tank), [2000, 1700, 1500, 1400])
		self.assertEqual(fx.rows(tank)[2].qty, -200)
		self.assertTanksMatchWarehouse([tank])
		self.assertChain(tank)

	def test_count_difference_above_threshold_is_marked_for_approval(self):
		fx.new_fuel_item(seed_litres=0)  # the warehouse holds only this tank's fuel
		tank = fx.make_tank()
		fx.stock_entry("Material Receipt", 1000, "2026-06-02 08:00:00", tank=tank)
		small = fx.stock_reconciliation({tank: 950}, "2026-06-03 08:00:00", submit=False)
		self.assertEqual(small.fuel_count_needs_approval, 0)  # 1000 - 950 = 50 <= 100
		small.submit()
		big = fx.stock_reconciliation({tank: 700}, "2026-06-04 08:00:00", submit=False)
		self.assertEqual(big.fuel_count_needs_approval, 1)

	def test_go_live_opening_by_count_then_the_tanks_follow_the_warehouse(self):
		"""The main opening path: a count at tracking start with a reading per tank."""
		a, b = fx.make_tank(), fx.make_tank()
		fx.stock_reconciliation({a: 60000, b: 40500}, fx.TRACKING_START)  # book 100000: a +500 difference
		self.assertTanksMatchWarehouse([a, b])
		fx.delivery_note(a, 1000, "2026-06-02 10:00:00")
		fx.purchase_receipt(b, 100, "2026-06-03 10:00:00")
		fx.stock_entry("Material Issue", 40, "2026-06-04 10:00:00", tank=b)
		fx.tank_transfer(a, b, 500, "2026-06-05 10:00:00")
		fx.delivery_note(b, 10, "2026-06-01 12:00:00")  # back-dated
		self.assertTanksMatchWarehouse([a, b])
		# a pre-tracking document posted late: ERP's count absorbs it, the openings stay as verified
		fx.delivery_note(None, 700, "2026-05-31 10:00:00")
		self.assertTanksMatchWarehouse([a, b])
		self.assertEqual(fx.rows(a)[0].balance_after, 60000)


class TestOpening(FuelTestCase):
	def test_fuel_tank_opening_when_the_count_has_no_difference(self):
		a, b = fx.make_tank(), fx.make_tank()
		opening = frappe.get_doc(
			{
				"doctype": "Fuel Tank Opening",
				"posting_date": "2026-06-01",
				"posting_time": "00:00:00",
				"fuel_tank_readings": [
					{"fuel_tank": a, "measured_qty": 60000},
					{"fuel_tank": b, "measured_qty": 39000},
				],
			}
		)
		self.assertRefused("READINGS_MISMATCH", opening.insert)  # ERP holds 100000

		opening.fuel_tank_readings[1].measured_qty = 40000
		opening.insert()
		opening.submit()
		self.assertEqual(fx.balances(a), [60000])
		self.assertEqual([(r.qty, r.is_anchor) for r in fx.rows(b)], [(40000, 1)])
		self.assertEqual(flt(opening.warehouse_litres), 100000)
		self.assertTanksMatchWarehouse([a, b])
		self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_no": opening.name}))


class TestFlags(FuelTestCase):
	def test_negative_tank_is_posted_flagged_then_cleared(self):
		tank = fx.make_tank()
		dn = fx.delivery_note(tank, 100, "2026-06-05 10:00:00")
		self.assertEqual(dn.docstatus, 1)  # posted, not refused
		self.assertEqual(fx.balances(tank), [-100])
		issues = fx.tank_issues(tank, "TANK_NEGATIVE")
		self.assertEqual(len(issues), 1)
		self.assertEqual(issues[0].status, "Open")

		fx.delivery_note(tank, 10, "2026-06-06 10:00:00")
		self.assertEqual(len(fx.tank_issues(tank, "TANK_NEGATIVE")), 1)  # one per tank per day

		fx.stock_entry("Material Receipt", 500, "2026-06-04 08:00:00", tank=tank)  # the missing receipt
		self.assertEqual(fx.balances(tank), [500, 400, 390])
		self.assertEqual(fx.tank_issues(tank, "TANK_NEGATIVE")[0].status, "Resolved")

	def test_over_capacity_is_posted_flagged_then_cleared(self):
		tank = fx.make_tank(capacity=1000)
		fx.stock_entry("Material Receipt", 800, "2026-06-02 08:00:00", tank=tank)
		self.assertEqual(fx.tank_issues(tank), [])
		fx.stock_entry("Material Receipt", 400, "2026-06-05 08:00:00", tank=tank)
		issues = fx.tank_issues(tank, "TANK_OVER_CAPACITY")
		self.assertEqual(len(issues), 1)
		self.assertIn("Capacity", issues[0].subject)
		self.assertIn("Capacity", frappe.db.get_value("Issue", issues[0].name, "description"))

		fx.stock_entry("Material Receipt", 10, "2026-06-06 08:00:00", tank=tank)
		self.assertEqual(len(fx.tank_issues(tank, "TANK_OVER_CAPACITY")), 1)

		fx.delivery_note(tank, 300, "2026-06-03 10:00:00")  # a late delivery brings it back in range
		self.assertEqual(fx.balances(tank), [800, 500, 900, 910])
		self.assertEqual(fx.tank_issues(tank, "TANK_OVER_CAPACITY")[0].status, "Resolved")


class TestLandedCost(FuelTestCase):
	def test_landed_cost_voucher_leaves_the_tank_ledger_unchanged(self):
		tank = fx.make_tank()
		pr = fx.purchase_receipt(tank, 100, "2026-06-03 09:00:00", uom=fx.IG)
		fx.delivery_note(tank, 100, "2026-06-04 10:00:00")
		before = fx.rows(tank, include_cancelled=True)
		company = fx.company()
		lcv = frappe.get_doc(
			{
				"doctype": "Landed Cost Voucher",
				"company": company,
				"distribute_charges_based_on": "Qty",
				"purchase_receipts": [
					{
						"receipt_document_type": "Purchase Receipt",
						"receipt_document": pr.name,
						"supplier": pr.supplier,
						"posting_date": pr.posting_date,
						"grand_total": pr.grand_total,
					}
				],
				"taxes": [
					{
						"description": "Freight (test)",
						"expense_account": frappe.get_cached_value(
							"Company", company, "expenses_included_in_valuation"
						),
						"amount": 50,
					}
				],
			}
		)
		lcv.insert(ignore_permissions=True)
		total = sum(flt(item.qty) for item in lcv.items)
		for item in lcv.items:
			item.applicable_charges = flt(item.qty) * flt(lcv.total_taxes_and_charges) / total
		lcv.submit()
		self.assertEqual(fx.rows(tank, include_cancelled=True), before)
		self.assertLedgerMatchesStock(pr)
		self.assertFalse(frappe.db.exists("Tank Ledger Entry", {"voucher_no": lcv.name}))
