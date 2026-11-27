import frappe
from frappe.utils import flt

from fuelbuddy_inventory import ledger
from fuelbuddy_inventory.tests import fixtures

try:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase
except ImportError:  # Frappe v16
	from frappe.tests import IntegrationTestCase as FrappeTestCase


class FuelTestCase(FrappeTestCase):
	"""Each test gets a fresh fuel item (so ERP's warehouse quantity is its own) and makes its own tanks."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		fixtures.setup_base()

	def setUp(self):
		frappe.set_user("Administrator")
		self.item = fixtures.new_fuel_item()

	def assertChain(self, tank):
		"""Every active row's balance is the previous balance plus its qty."""
		balance = 0.0
		for row in fixtures.rows(tank):
			self.assertAlmostEqual(
				flt(balance + row.qty, 3), row.balance_after, places=3, msg=f"row {row.name}"
			)
			balance = row.balance_after

	def assertLedgerMatchesStock(self, voucher):
		"""The voucher's tank row holds exactly what its own Stock Ledger Entries moved."""
		stock = flt(ledger.voucher_stock_qty(voucher.doctype, voucher.name), 3)
		tank_rows = frappe.get_all(
			"Tank Ledger Entry",
			filters={"voucher_type": voucher.doctype, "voucher_no": voucher.name, "is_cancelled": 0},
			fields=["qty"],
		)
		self.assertEqual(len(tank_rows), 1)
		self.assertAlmostEqual(flt(tank_rows[0].qty, 3), stock, places=3)

	def assertTanksMatchWarehouse(self, tanks, as_of=None):
		total = sum(flt(ledger.balance_as_of(tank, as_of), 3) for tank in tanks)
		self.assertAlmostEqual(total, flt(ledger.warehouse_qty_as_of(as_of), 3), places=3)

	def assertRefused(self, code, fn, *args, **kwargs):
		"""`fn` raises the refusal class named `code`, with the message prefixed by the code."""
		with self.assertRaises(frappe.ValidationError) as caught:
			fn(*args, **kwargs)
		self.assertEqual(type(caught.exception).__name__, code, str(caught.exception))
		self.assertTrue(str(caught.exception).startswith(f"{code}: "), str(caught.exception))
		frappe.clear_messages()
		return caught.exception
