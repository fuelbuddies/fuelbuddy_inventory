"""Install, migrate and uninstall steps. All are idempotent.

``after_install`` and ``after_migrate`` both create the custom fields (a patch would not do on a
new install: ``bench install-app`` marks a new app's patches as applied without running them).
"""

import frappe

from fuelbuddy_inventory import custom_fields, flags
from fuelbuddy_inventory.settings import DEFAULT_TRACKING_START, SETTINGS

TLE = "Tank Ledger Entry"


def after_install():
	_setup()


def after_migrate():
	_setup()


def _setup():
	custom_fields.ensure_all()
	if not frappe.db.get_single_value(SETTINGS, "tracking_start"):
		frappe.db.set_single_value(SETTINGS, "tracking_start", DEFAULT_TRACKING_START)
	flags.ensure_issue_type()


def before_uninstall():
	"""Refuse while the tank ledger holds rows: uninstalling would drop the only record of where the
	fuel sits, and the custom fields every posted document names a tank in."""
	if frappe.db.table_exists(TLE) and frappe.db.count(TLE):
		frappe.throw(
			f"fuelbuddy_inventory cannot be uninstalled: {TLE} holds {frappe.db.count(TLE)} rows. "
			"The tank ledger is the record of where the fuel sits.",
			title="Uninstall refused",
		)
