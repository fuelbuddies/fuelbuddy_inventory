# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class TankLedgerEntry(Document):
	"""Written only by fuelbuddy_inventory.ledger, in the transaction of the document it records.
	Read-only for every role; a save or delete through the document API is refused."""

	def validate(self):
		frappe.throw("Tank Ledger Entries are written only by submitting or cancelling their document.")

	def on_trash(self):
		frappe.throw("Tank Ledger Entries cannot be deleted. Cancel their document instead.")


def on_doctype_update():
	# The ledger's ordering key, per tank (placing a row, balances as of a time).
	frappe.db.add_index(
		"Tank Ledger Entry",
		["fuel_tank", "is_cancelled", "posting_datetime", "creation"],
		index_name="tank_posting_index",
	)
	# A voucher's rows (cancel, the API's voucher fields).
	frappe.db.add_index("Tank Ledger Entry", ["voucher_no", "voucher_type"], index_name="voucher_index")
	# Rows changed since a time, per tank (get_tank_ledger).
	frappe.db.add_index("Tank Ledger Entry", ["fuel_tank", "modified"], index_name="tank_modified_index")
