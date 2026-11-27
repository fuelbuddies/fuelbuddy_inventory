"""Helpers shared by the app's own submittable documents (Tank Transfer, Fuel Tank Opening)."""

import frappe
from frappe.utils import strip_html

from fuelbuddy_inventory.exceptions import PERIOD_CLOSED, refuse


def fuel_company(settings):
	"""The fuel warehouse's company: the tank documents' company for Accounting Period checks."""
	if not settings.fuel_warehouse:
		return None
	return frappe.db.get_value("Warehouse", settings.fuel_warehouse, "company")


def check_period_open(doc):
	"""ERPNext's Accounting Period check (the tank doctypes join its list through the
	period_closing_doctypes hook), refused as PERIOD_CLOSED."""
	if not doc.get("company") or not doc.get("posting_date"):
		return
	try:
		from erpnext.accounts.doctype.accounting_period.accounting_period import (
			ClosedAccountingPeriod,
			validate_accounting_period_on_doc_save,
		)
	except ImportError:  # moved in a later ERPNext: the same check, read directly
		period = _closed_period(doc)
		if period:
			refuse(PERIOD_CLOSED, f"{doc.doctype} is dated inside the closed Accounting Period {period}.")
		return

	try:
		validate_accounting_period_on_doc_save(doc)
	except ClosedAccountingPeriod as exc:
		_drop_last_message()
		refuse(PERIOD_CLOSED, strip_html(str(exc)))


def _closed_period(doc):
	rows = frappe.db.sql(
		"""select ap.name from `tabAccounting Period` ap
		join `tabClosed Document` cd on cd.parent = ap.name
		where ap.company = %(company)s and cd.closed = 1 and cd.document_type = %(doctype)s
		and %(date)s between ap.start_date and ap.end_date
		limit 1""",
		{"company": doc.company, "doctype": doc.doctype, "date": doc.posting_date},
	)
	return rows[0][0] if rows else None


def _drop_last_message():
	"""frappe.throw logged ERPNext's message too; the refusal replaces it."""
	log = getattr(frappe.local, "message_log", None)
	if log:
		log.pop()
