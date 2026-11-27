# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cstr, flt


class FuelTank(Document):
	"""One tank of the app, named by its App Tank ID. Kept in step from the app (api.upsert_fuel_tank);
	ERP never edits the identity. Disable a tank to retire it."""

	def validate(self):
		self.app_tank_id = cstr(self.app_tank_id).strip()
		self.label = cstr(self.label).strip()
		if flt(self.capacity_litres) < 0:
			frappe.throw(f"Capacity (Litres) of {self.label} cannot be below zero.")

	def on_trash(self):
		if frappe.db.exists("Tank Ledger Entry", {"fuel_tank": self.name}):
			frappe.throw(
				f"Fuel Tank {self.label} has tank ledger rows and cannot be deleted. Disable it instead."
			)
