# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class FuelInventorySettings(Document):
	def validate(self):
		if self.fuel_warehouse and cint(frappe.db.get_value("Warehouse", self.fuel_warehouse, "is_group")):
			frappe.throw("The fuel warehouse must be a warehouse, not a group.")
		if self.fuel_item and not cint(frappe.db.get_value("Item", self.fuel_item, "is_stock_item")):
			frappe.throw("The fuel item must be a stock item.")
