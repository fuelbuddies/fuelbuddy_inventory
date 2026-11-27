"""Negative and over-capacity tanks: posted, then flagged.

Most deliveries reach ERP back-dated, so refusing would strand them in draft. After a write, the
first row of the affected range (the written row up to the tank's next anchor) that is below zero or
above the tank's capacity raises an Issue:

- one Issue per tank, code and day (the day the flag is raised), de-duplicated on its subject, as
  fuelbuddy_crm's invoicing alerts are (auto_invoicing._log_invoicing_issue);
- a capacity Issue's subject and text contain the word "Capacity".

An open flag clears (its Issue is set Resolved) when a later write on the tank raises no flag of
that code and the tank's current stretch, from its latest count onward, is back in range.

Issues are written in the document's own transaction, never committed here: a rolled-back submit
leaves no alert. A failure to write one is logged and never fails the post.
"""

import datetime

import frappe
from frappe.utils import flt, today

from fuelbuddy_inventory import ledger

ISSUE_TYPE = "Fuel Tank"
TANK_NEGATIVE = "TANK_NEGATIVE"
TANK_OVER_CAPACITY = "TANK_OVER_CAPACITY"
OPEN_STATUSES_EXCLUDED = ["Resolved", "Closed"]
_EPOCH = datetime.datetime(1900, 1, 1)

# A balance below zero or above capacity at 3 decimals (a millilitre), so 9-decimal dust never flags.
NEGATIVE_SQL = "round(balance_after, 3) < 0"
OVER_CAPACITY_SQL = "round(balance_after, 3) > %(capacity)s"


def review(changes, voucher_type, voucher_no):
	"""Raise or clear flags for the tanks a write touched. `changes` are ledger.place/remove results;
	removed rows are reviewed from the row after them."""
	for change in changes:
		try:
			_review_tank(change, voucher_type, voucher_no)
		except Exception as exc:
			if is_lock_error(exc):
				# A deadlock has rolled the whole transaction back: carrying on would commit half a post.
				raise
			# The tank rows are written; an alert must never fail the post.
			frappe.log_error(
				title=f"Fuel tank flag review failed: {change.tank}"[:140],
				message=frappe.get_traceback(),
			)


def _review_tank(change, voucher_type, voucher_no):
	tank = frappe.db.get_value(
		ledger.FUEL_TANK, change.tank, ["name", "label", "capacity_litres"], as_dict=True
	)
	if not tank:
		return
	inclusive = not change.get("removed")
	capacity = flt(tank.capacity_litres)

	checks = [(TANK_NEGATIVE, NEGATIVE_SQL, {})]
	if capacity > 0:
		checks.append((TANK_OVER_CAPACITY, OVER_CAPACITY_SQL, {"capacity": capacity}))

	for code, condition, params in checks:
		row = ledger.first_out_of_range(
			tank.name, change.key, change.anchor, condition, params, inclusive=inclusive
		)
		if row:
			raise_flag(code, tank, row, voucher_type, voucher_no)
		else:
			clear_flags(code, tank, condition, params)


def is_lock_error(exc):
	try:
		return bool(frappe.db.is_deadlocked(exc) or frappe.db.is_timedout(exc))
	except Exception:
		return False


def _subject_prefix(code, tank_name):
	return f"{code}: {tank_name} "


def raise_flag(code, tank, row, voucher_type, voucher_no):
	"""One Issue per tank, code and day; a second flag the same day adds nothing."""
	day = today()
	prefix = f"{_subject_prefix(code, tank.name)}{day}"
	if frappe.get_all(
		"Issue",
		filters={
			"issue_type": ISSUE_TYPE,
			"status": ["not in", OPEN_STATUSES_EXCLUDED],
			"subject": ["like", f"{prefix}%"],
		},
		limit=1,
	):
		return
	balance = flt(row.balance_after, 3)
	if code == TANK_OVER_CAPACITY:
		headline = (
			f"Capacity exceeded: {tank.label} holds {balance} L, Capacity {flt(tank.capacity_litres, 3)} L"
		)
	else:
		headline = f"Below zero: {tank.label} holds {balance} L"
	detail = (
		f"{headline}, after {row.voucher_type} {row.voucher_no} at {row.posting_datetime}. "
		f"Raised by {voucher_type} {voucher_no}. The post was accepted; check the tank's ledger "
		"(Tank Ledger report) for a missing, late or wrong-tank document."
	)
	_insert_issue(f"{prefix}: {headline}"[:140], detail)


def clear_flags(code, tank, condition, params):
	"""Resolve the tank's open Issues of `code` once its current stretch is back in range."""
	open_issues = frappe.get_all(
		"Issue",
		filters={
			"issue_type": ISSUE_TYPE,
			"status": ["not in", OPEN_STATUSES_EXCLUDED],
			"subject": ["like", f"{_subject_prefix(code, tank.name)}%"],
		},
		pluck="name",
	)
	if not open_issues:
		return
	anchor = ledger.latest_anchor(tank.name)
	start = ledger.row_key(anchor) if anchor else (_EPOCH, _EPOCH, 0)
	if ledger.first_out_of_range(tank.name, start, None, condition, params, inclusive=True):
		return
	for name in open_issues:
		frappe.db.set_value(
			"Issue",
			name,
			{"status": "Resolved", "resolution_details": "The tank is back in range (fuelbuddy_inventory)."},
		)


def _insert_issue(subject, description):
	savepoint = "fuel_tank_flag"
	frappe.db.savepoint(savepoint)
	try:
		issue = frappe.new_doc("Issue")
		issue.subject = subject
		issue.issue_type = ensure_issue_type()
		issue.description = description
		issue.flags.ignore_permissions = True
		issue.insert(ignore_permissions=True)
	except Exception as exc:
		if is_lock_error(exc):
			raise
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=subject[:140], message=f"{description}\n\n{frappe.get_traceback()}")
		return None
	frappe.db.release_savepoint(savepoint)
	return issue.name


def ensure_issue_type():
	if not frappe.db.exists("Issue Type", ISSUE_TYPE):
		issue_type = frappe.new_doc("Issue Type")
		issue_type.name = ISSUE_TYPE
		issue_type.flags.ignore_permissions = True
		issue_type.insert(ignore_permissions=True)
	return ISSUE_TYPE


def raise_issue(subject, description):
	"""An Issue of type Fuel Tank, de-duplicated on its open subject (used by the daily check)."""
	if frappe.get_all(
		"Issue",
		filters={
			"issue_type": ISSUE_TYPE,
			"status": ["not in", OPEN_STATUSES_EXCLUDED],
			"subject": subject[:140],
		},
		limit=1,
	):
		return None
	return _insert_issue(subject[:140], description)
