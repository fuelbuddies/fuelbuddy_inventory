# Copyright (c) 2026, FuelBuddy and contributors
# For license information, please see license.txt

"""Tank Transfer: fuel moved from one tank to another. It changes only the tank ledger (two rows, at
the same posting time: out of the source, into the target). No ERP Stock Entry, so ERP's FIFO
queue is untouched. Created by erp-functions through api.create_tank_transfer (replay-safe on
op_key), or in the desk."""

import math

from frappe.model.document import Document
from frappe.utils import flt

from fuelbuddy_inventory import flags, ledger
from fuelbuddy_inventory.exceptions import BAD_QTY, SAME_TANK, TANK_MISSING, refuse
from fuelbuddy_inventory.settings import doc_posting_datetime, get_settings
from fuelbuddy_inventory.stock_hooks import check_tank, reverse_voucher
from fuelbuddy_inventory.tank_documents import check_period_open, fuel_company


class TankTransfer(Document):
	def validate(self):
		settings = get_settings()
		self.company = fuel_company(settings) or self.company
		if self.from_tank and self.from_tank == self.to_tank:
			refuse(SAME_TANK, f"From and To are the same tank ({self.from_tank}).")
		qty = flt(self.qty)
		if not math.isfinite(qty) or qty <= 0:
			refuse(BAD_QTY, f"Qty must be more than 0 litres, got {self.qty}.")
		check_period_open(self)

	def before_submit(self):
		for field in ("from_tank", "to_tank"):
			if not self.get(field):
				refuse(TANK_MISSING, f"Tank Transfer {self.name} names no {field}.")
			check_tank(self.get(field), f"{field}: ")

	def on_submit(self):
		settings = get_settings()
		posting = doc_posting_datetime(self)
		if posting < settings.tracking_start:
			return
		qty = ledger.to_decimal(self.qty)
		changes = [
			ledger.place(tank, self.doctype, self.name, posting, qty=-qty if tank == self.from_tank else qty)
			for tank in ledger.lock_tanks([self.from_tank, self.to_tank])
		]
		flags.review(changes, self.doctype, self.name)

	def on_cancel(self):
		reverse_voucher(self)
