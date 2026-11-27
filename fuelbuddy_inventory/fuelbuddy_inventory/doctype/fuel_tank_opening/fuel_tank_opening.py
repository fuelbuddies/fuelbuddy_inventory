# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

"""Fuel Tank Opening: the opening balance per tank, for the one case the main path cannot take.

The opening is normally a Stock Reconciliation with a reading per tank. ERPNext refuses one in which
nothing changes ("None of the items have any change in quantity or value"), so when ops and
finance's verified litres add up to exactly ERP's quantity, this records the readings instead: one
anchor row per tank, and no ERP stock document. The readings must add up to ERP's quantity of the
fuel item in the fuel warehouse at the posting time."""

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from fuelbuddy_inventory import ledger
from fuelbuddy_inventory.settings import doc_posting_datetime, get_settings, is_configured
from fuelbuddy_inventory.stock_hooks import (
	READINGS_FIELD,
	check_tank,
	post_readings,
	reverse_voucher,
	validate_readings,
)
from fuelbuddy_inventory.tank_documents import check_period_open, fuel_company


class FuelTankOpening(Document):
	def validate(self):
		settings = get_settings()
		if not is_configured(settings):
			frappe.throw("Set the fuel item and the fuel warehouse in Fuel Inventory Settings first.")
		self.company = fuel_company(settings)
		check_period_open(self)
		readings = self.get(READINGS_FIELD) or []
		if not readings:
			frappe.throw("Add a reading for each tank.")
		posting = doc_posting_datetime(self)
		self.warehouse_litres = flt(ledger.warehouse_qty_as_of(posting, settings))
		self.total_litres = sum(flt(reading.measured_qty) for reading in readings)
		validate_readings(
			readings,
			self.warehouse_litres,
			f"ERP's quantity of {settings.fuel_item} in {settings.fuel_warehouse} at {posting}",
		)

	def before_submit(self):
		for reading in self.get(READINGS_FIELD) or []:
			check_tank(reading.fuel_tank, f"Reading row {reading.idx}: ")

	def on_submit(self):
		if doc_posting_datetime(self) < get_settings().tracking_start:
			return
		post_readings(self, self.get(READINGS_FIELD) or [])

	def on_cancel(self):
		reverse_voucher(self)
