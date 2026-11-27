"""Refusal codes erp-functions reads from ERP's answer.

Each refusal is raised as an exception class named exactly after its code, so Frappe's error answer
carries the code twice:

- ``exc_type`` is the class name (Frappe sets it from ``exc.__class__.__name__``), e.g. TANK_MISSING;
- the message starts with ``CODE: `` (``_server_messages`` and the exception text).

All are ValidationErrors (HTTP 417), so a caller that only knows Frappe's classes still treats them as
refusals. No refusal message here mentions tank size: erp-functions classes any ERP_VALIDATION
whose message contains that word as fixable, and these refusals are not about it.
"""

import frappe


class FuelInventoryError(frappe.ValidationError):
	"""Base class: a refusal with a machine-readable code (the class name)."""


class TANK_MISSING(FuelInventoryError):
	"""A fuel document dated on or after tracking start names no tank."""


class TANK_NOT_FOUND(FuelInventoryError):
	"""The tank named does not exist, or is disabled."""


class WRONG_FUEL_WAREHOUSE(FuelInventoryError):
	"""A fuel line sits in a warehouse other than the fuel warehouse."""


class STOCK_PATH_NOT_ALLOWED(FuelInventoryError):
	"""Fuel moved by a path the tank ledger cannot follow."""


class SAME_TANK(FuelInventoryError):
	"""A Tank Transfer names the same tank on both sides."""


class BAD_QTY(FuelInventoryError):
	"""A Tank Transfer quantity is zero, negative or not a number."""


class READINGS_MISMATCH(FuelInventoryError):
	"""Tank readings do not add up to the counted quantity."""


class PERIOD_CLOSED(FuelInventoryError):
	"""The document is dated inside a closed Accounting Period."""


def refuse(exc_class, message):
	"""Raise `exc_class` with the message prefixed by its code."""
	code = exc_class.__name__
	frappe.throw(f"{code}: {message}", exc=exc_class, title=code)


def code_of(exc):
	"""The refusal code of an exception raised by this app, else None."""
	return type(exc).__name__ if isinstance(exc, FuelInventoryError) else None
