"""Daily check (02:00): the tanks add up to the warehouse.

The sum of every tank's balance is compared with ERP's quantity of the fuel item in the fuel
warehouse, both over everything posted. ERP's side comes from the stock ledger
(ledger.warehouse_qty_as_of), never the Bin, which is stale during a DN drain. Any difference
raises one Issue per day (type Fuel Tank).
"""

import frappe
from frappe.utils import flt, now_datetime, today

from fuelbuddy_inventory import flags, ledger
from fuelbuddy_inventory.settings import get_settings, is_configured

CODE = "TANK_WAREHOUSE_MISMATCH"
TOLERANCE_LITRES = 0.001


def compare(as_of=None, settings=None):
	"""{tanks, warehouse, difference} in litres, as of `as_of` (None = everything posted)."""
	settings = settings or get_settings()
	tanks = sum(ledger.balances_as_of(as_of).values())
	warehouse = ledger.warehouse_qty_as_of(as_of, settings)
	return frappe._dict(
		tanks=flt(tanks, 3), warehouse=flt(warehouse, 3), difference=flt(tanks - warehouse, 3)
	)


def daily_check():
	settings = get_settings()
	if not is_configured(settings) or now_datetime() < settings.tracking_start:
		return None
	result = compare(settings=settings)
	if abs(result.difference) < TOLERANCE_LITRES:
		return result
	flags.raise_issue(
		f"{CODE}: {today()}: tanks {result.tanks} L, warehouse {result.warehouse} L",
		f"The tank balances add up to {result.tanks} L but ERP's stock ledger holds {result.warehouse} L "
		f"of {settings.fuel_item} in {settings.fuel_warehouse} (difference {result.difference} L). "
		"Look for a fuel document posted without a tank row (dated before tracking start, or a tank "
		"not read on a count), or a count whose readings did not cover every tank. The Tank vs "
		"Warehouse report shows the day the two parted.",
	)
	return result
