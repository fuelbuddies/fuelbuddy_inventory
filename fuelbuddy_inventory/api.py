"""Whitelisted methods for erp-functions.

Every method answers

    {ok, code, message, retryable, result, ...}

ok true    the call was understood and answered; ``result`` carries the answer; code null.
ok false   the call changed nothing; ``code`` says why, ``message`` in words:
           ERP_VALIDATION     bad input, or not permitted                               final
           LOCK_RETRY         a lock wait (8 s), a deadlock, or a racing first write    retry
           TANK_MISSING, TANK_NOT_FOUND, SAME_TANK, BAD_QTY, PERIOD_CLOSED              final
           CANCELLED_IN_ERP, TANK_MISMATCH, QTY_MISMATCH (create_tank_transfer)        final
retryable  true only for LOCK_RETRY.

Anything else (ERP down, a defect) is an HTTP error, which erp-functions treats as "outcome unknown"
for a write and re-checks by key.

upsert_fuel_tank(app_tank_id, label, vehicle_registration, vehicle_type, tank_type, compartment_no,
                 capacity_litres | capacity + capacity_unit, active, vehicle_active, is_test, disabled)  POST
    Creates or updates the Fuel Tank named app_tank_id; never changes its identity. capacity_unit is
    LITRE or IMPERIAL_GALLON (4.546 L). The tank is saved disabled when the vehicle type is not
    BOWSER or CONTAINER, when is_test is true, when active or vehicle_active is false, or when
    disabled is true. Adds name, created, disabled.

create_tank_transfer(op_key, from_tank, to_tank, qty, posting_datetime, source_request_id)  POST
    Replay-safe on op_key, as erp-functions' opKey.js: the key is looked up at any docstatus first.
      submitted  adopted: ok, nothing created
      cancelled  CANCELLED_IN_ERP; never posted again
      draft      submitted and adopted when its tanks and litres match, else TANK_MISMATCH or
                 QTY_MISMATCH (left as it is)
      none       created and submitted
    A racing first write with the same key loses on the unique index and is answered from the
    winner's row. Adds name, docstatus, adopted.

get_tank_ledger(tank_ids, since, limit)  GET
    result {rows, truncated}. Rows of those tanks (all when empty) whose modified >= since
    (inclusive; rows move when a back-dated post, a count or a cancel recalculates them), ordered by
    (modified, name), at most `limit` (default 10000, max 50000). Each row: name, fuel_tank,
    posting_datetime, voucher_type, voucher_no, qty, balance_after, is_anchor, is_cancelled,
    creation, modified, custom_invoiced_item_id (Delivery Note), custom_app_op_key (Purchase
    Receipt, Stock Entry), op_key (Tank Transfer); null where the voucher has none.

get_tank_balances(tank_ids, as_of)  GET
    result {as_of, tanks: [{fuel_tank, found, label, disabled, balance, pending_drafts}]}. balance is
    the tank's balance after its last row dated at or before as_of (a posting datetime in site
    time; default now). pending_drafts is the number of draft Delivery Notes, Purchase Receipts and
    Stock Entries naming the tank dated at or before as_of.
"""

import json
import math

import frappe
from frappe.utils import cint, cstr, flt, get_datetime, now_datetime, strip_html

from fuelbuddy_inventory import ledger
from fuelbuddy_inventory.exceptions import FuelInventoryError, code_of

ERP_VALIDATION = "ERP_VALIDATION"
LOCK_RETRY = "LOCK_RETRY"
CANCELLED_IN_ERP = "CANCELLED_IN_ERP"
TANK_MISMATCH = "TANK_MISMATCH"
QTY_MISMATCH = "QTY_MISMATCH"
TANK_MISSING = "TANK_MISSING"
TANK_NOT_FOUND = "TANK_NOT_FOUND"
SAME_TANK = "SAME_TANK"
BAD_QTY = "BAD_QTY"
RETRYABLE = frozenset({LOCK_RETRY})

LOCK_WAIT_SECONDS = 8
MAX_LENGTH = 140
LITRES_PER_IMPERIAL_GALLON = 4.546
VEHICLE_TYPES = ("BOWSER", "CONTAINER")
DEFAULT_LIMIT = 10000
MAX_LIMIT = 50000
DRAFT_DOCTYPES = ("Delivery Note", "Purchase Receipt", "Stock Entry")
# The voucher fields get_tank_ledger returns, where the voucher's doctype has them.
VOUCHER_FIELDS = {
	"Delivery Note": ("custom_invoiced_item_id",),
	"Purchase Receipt": ("custom_app_op_key",),
	"Stock Entry": ("custom_app_op_key",),
	"Tank Transfer": ("op_key",),
}
ROW_VOUCHER_KEYS = ("custom_invoiced_item_id", "custom_app_op_key", "op_key")


class Refusal(Exception):
	"""Returned to the caller as ok: false with its code."""

	def __init__(self, code, message):
		super().__init__(message)
		self.code = code
		self.message = message


# ---- upsert_fuel_tank ---------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def upsert_fuel_tank(
	app_tank_id=None,
	label=None,
	vehicle_registration=None,
	vehicle_type=None,
	tank_type=None,
	compartment_no=None,
	capacity_litres=None,
	capacity=None,
	capacity_unit=None,
	active=None,
	vehicle_active=None,
	is_test=None,
	disabled=None,
):
	try:
		values = _tank_values(
			app_tank_id,
			label,
			vehicle_registration,
			vehicle_type,
			tank_type,
			compartment_no,
			capacity_litres,
			capacity,
			capacity_unit,
			active,
			vehicle_active,
			is_test,
			disabled,
		)
	except Refusal as refusal:
		return _refused(refusal)
	return _in_transaction(lambda: _upsert_tank(values))


def _tank_values(
	app_tank_id,
	label,
	vehicle_registration,
	vehicle_type,
	tank_type,
	compartment_no,
	capacity_litres,
	capacity,
	capacity_unit,
	active,
	vehicle_active,
	is_test,
	disabled,
):
	kind = cstr(vehicle_type).strip().upper()
	off = (
		_flag(disabled) is True
		or _flag(is_test) is True
		or _flag(active) is False
		or _flag(vehicle_active) is False
		or kind not in VEHICLE_TYPES
	)
	return frappe._dict(
		app_tank_id=_text(app_tank_id, "app_tank_id"),
		label=_text(label, "label"),
		vehicle_registration=_text(vehicle_registration, "vehicle_registration", required=False),
		vehicle_type=kind if kind in VEHICLE_TYPES else None,
		tank_type=_text(tank_type, "tank_type", required=False),
		compartment_no=_whole(compartment_no, "compartment_no"),
		capacity_litres=_capacity(capacity_litres, capacity, capacity_unit),
		disabled=1 if off else 0,
	)


def _upsert_tank(values):
	name = frappe.db.get_value(ledger.FUEL_TANK, values.app_tank_id, "name", for_update=True)
	if name:
		_check_permitted(ledger.FUEL_TANK, ("write",))
		doc = frappe.get_doc(ledger.FUEL_TANK, name)
		changed = False
		for field, value in values.items():
			if field != "app_tank_id" and _differs(doc.get(field), value):
				doc.set(field, value)
				changed = True
		if changed:
			doc.save()
		created = False
	else:
		_check_permitted(ledger.FUEL_TANK, ("create",))
		doc = frappe.get_doc({"doctype": ledger.FUEL_TANK, **values})
		doc.insert()
		created = True
	answer = _answer({"name": doc.name, "created": created, "disabled": cint(doc.disabled)})
	answer.update(name=doc.name, created=created, disabled=cint(doc.disabled))
	return answer


def _differs(current, new):
	if isinstance(new, int | float) or isinstance(current, int | float):
		return current in (None, "") and new is not None or flt(current) != flt(new)
	return cstr(current) != cstr(new)


def _capacity(capacity_litres, capacity, capacity_unit):
	if _given(capacity_litres):
		return _number(capacity_litres, "capacity_litres", minimum=0)
	if not _given(capacity):
		return None
	amount = _number(capacity, "capacity", minimum=0)
	unit = cstr(capacity_unit).strip().upper()
	if unit in ("LITRE", "LITRES", "L"):
		return amount
	if unit in ("IMPERIAL_GALLON", "IG"):
		return flt(amount * LITRES_PER_IMPERIAL_GALLON, 3)
	raise Refusal(ERP_VALIDATION, f"capacity_unit must be LITRE or IMPERIAL_GALLON, got {capacity_unit!r}")


# ---- create_tank_transfer -----------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def create_tank_transfer(
	op_key=None, from_tank=None, to_tank=None, qty=None, posting_datetime=None, source_request_id=None
):
	try:
		request = _transfer_request(op_key, from_tank, to_tank, qty, posting_datetime, source_request_id)
		_check_permitted("Tank Transfer", ("create", "submit"))
	except Refusal as refusal:
		return _transfer_refused(refusal)
	answer = _in_transaction(lambda: _create_transfer(request), retry_on_duplicate=True)
	if not answer["ok"]:
		answer.update(name=None, docstatus=None, adopted=False)
	return answer


def _transfer_request(op_key, from_tank, to_tank, qty, posting_datetime, source_request_id):
	key = cstr(op_key)
	if not key.strip():
		raise Refusal(ERP_VALIDATION, "op_key is required")
	if key != key.strip():
		raise Refusal(ERP_VALIDATION, "op_key must not start or end with spaces")
	if len(key) > MAX_LENGTH:
		raise Refusal(ERP_VALIDATION, f"op_key must be at most {MAX_LENGTH} characters")
	source = _text(from_tank, "from_tank", required=False)
	target = _text(to_tank, "to_tank", required=False)
	if not source or not target:
		raise Refusal(TANK_MISSING, f"{TANK_MISSING}: from_tank and to_tank are both required")
	if source == target:
		raise Refusal(SAME_TANK, f"{SAME_TANK}: From and To are the same tank ({source}).")
	litres = flt(qty) if _given(qty) else 0.0
	if not _is_number(qty) or not math.isfinite(litres) or litres <= 0:
		raise Refusal(BAD_QTY, f"{BAD_QTY}: qty must be more than 0 litres, got {qty!r}.")
	if _given(posting_datetime):
		try:
			posting = get_datetime(posting_datetime)
		except Exception:
			posting = None
		if not posting:
			raise Refusal(
				ERP_VALIDATION, f"posting_datetime must be YYYY-MM-DD HH:MM:SS, got {posting_datetime!r}"
			)
	else:
		posting = now_datetime()
	return frappe._dict(
		op_key=key,
		from_tank=source,
		to_tank=target,
		qty=litres,
		posting=posting,
		source_request_id=_text(source_request_id, "source_request_id", required=False),
	)


def _create_transfer(request):
	existing = frappe.db.get_value(
		"Tank Transfer",
		{"op_key": request.op_key},
		["name", "docstatus", "from_tank", "to_tank", "qty"],
		as_dict=True,
	)
	if existing:
		return _adopt(existing, request)
	for tank in (request.from_tank, request.to_tank):
		if not frappe.db.exists(ledger.FUEL_TANK, tank):
			raise Refusal(TANK_NOT_FOUND, f"{TANK_NOT_FOUND}: Fuel Tank {tank} does not exist.")
	doc = frappe.get_doc(
		{
			"doctype": "Tank Transfer",
			"op_key": request.op_key,
			"from_tank": request.from_tank,
			"to_tank": request.to_tank,
			"qty": request.qty,
			"posting_date": request.posting.date(),
			"posting_time": request.posting.time().strftime("%H:%M:%S"),
			"source_request_id": request.source_request_id,
		}
	)
	doc.insert()
	doc.submit()
	return _transfer_answer(doc.name, doc.docstatus, adopted=False)


def _adopt(existing, request):
	docstatus = cint(existing.docstatus)
	if docstatus == 1:
		return _transfer_answer(existing.name, 1, adopted=True)
	if docstatus == 2:
		raise Refusal(
			CANCELLED_IN_ERP,
			f"Tank Transfer {existing.name} was posted for op key {request.op_key} and has since been "
			"cancelled in ERP. It is not posted again.",
		)
	if (existing.from_tank, existing.to_tank) != (request.from_tank, request.to_tank):
		raise Refusal(
			TANK_MISMATCH,
			f"Tank Transfer {existing.name} holds op key {request.op_key} as a draft from {existing.from_tank} "
			f"to {existing.to_tank}, not from {request.from_tank} to {request.to_tank}. It is left as it is.",
		)
	if round(flt(existing.qty) * 1000) != round(request.qty * 1000):
		raise Refusal(
			QTY_MISMATCH,
			f"Tank Transfer {existing.name} holds op key {request.op_key} as a draft of {flt(existing.qty)} L, "
			f"not {request.qty} L. It is left as it is.",
		)
	doc = frappe.get_doc("Tank Transfer", existing.name, for_update=True)
	if doc.docstatus != 0:
		return _adopt(frappe._dict(doc.as_dict()), request)
	doc.submit()
	return _transfer_answer(doc.name, doc.docstatus, adopted=True)


def _transfer_answer(name, docstatus, adopted):
	answer = _answer({"name": name, "docstatus": docstatus, "adopted": adopted})
	answer.update(name=name, docstatus=docstatus, adopted=adopted)
	return answer


def _transfer_refused(refusal):
	answer = _refused(refusal)
	answer.update(name=None, docstatus=None, adopted=False)
	return answer


# ---- reads ----------------------------------------------------------------------------------------
@frappe.whitelist()
def get_tank_ledger(tank_ids=None, since=None, limit=None):
	try:
		tanks = _tank_list(tank_ids)
		since_at = _datetime(since, "since") if _given(since) else None
		size = min(cint(limit) or DEFAULT_LIMIT, MAX_LIMIT)
		if size < 1:
			raise Refusal(ERP_VALIDATION, "limit must be at least 1")
		_check_permitted(ledger.TLE, ("read",))
	except Refusal as refusal:
		return _refused(refusal)
	return _read_only(lambda: _ledger_rows(tanks, since_at, size))


def _ledger_rows(tanks, since_at, size):
	conditions, params = [], {"limit": size + 1}
	if tanks:
		conditions.append("fuel_tank in %(tanks)s")
		params["tanks"] = tuple(tanks)
	if since_at:
		conditions.append("modified >= %(since)s")
		params["since"] = since_at
	where = f"where {' and '.join(conditions)}" if conditions else ""
	rows = frappe.db.sql(
		f"""select name, fuel_tank, posting_datetime, voucher_type, voucher_no, qty, balance_after,
			is_anchor, is_cancelled, creation, modified
		from {ledger.TABLE} {where}
		order by modified, name
		limit %(limit)s""",
		params,
		as_dict=True,
	)
	truncated = len(rows) > size
	rows = rows[:size]
	_add_voucher_fields(rows)
	for row in rows:
		row.qty = flt(row.qty)
		row.balance_after = flt(row.balance_after)
		row.is_anchor = cint(row.is_anchor)
		row.is_cancelled = cint(row.is_cancelled)
	return _answer({"rows": rows, "truncated": truncated})


def _add_voucher_fields(rows):
	for row in rows:
		for key in ROW_VOUCHER_KEYS:
			row[key] = None
	by_type = {}
	for row in rows:
		by_type.setdefault(row.voucher_type, set()).add(row.voucher_no)
	for voucher_type, names in by_type.items():
		meta = frappe.get_meta(voucher_type)
		fields = [field for field in VOUCHER_FIELDS.get(voucher_type, ()) if meta.has_field(field)]
		if not fields:
			continue
		names = sorted(names)
		values = {}
		for start in range(0, len(names), 1000):
			for found in frappe.get_all(
				voucher_type,
				filters={"name": ["in", names[start : start + 1000]]},
				fields=["name", *fields],
			):
				values[found.name] = found
		for row in rows:
			if row.voucher_type == voucher_type and row.voucher_no in values:
				for field in fields:
					row[field] = values[row.voucher_no].get(field)


@frappe.whitelist()
def get_tank_balances(tank_ids=None, as_of=None):
	try:
		tanks = _tank_list(tank_ids)
		as_of_at = _datetime(as_of, "as_of") if _given(as_of) else now_datetime()
		_check_permitted(ledger.TLE, ("read",))
	except Refusal as refusal:
		return _refused(refusal)
	return _read_only(lambda: _balances(tanks, as_of_at))


def _balances(tanks, as_of):
	known = {
		row.name: row
		for row in frappe.get_all(
			ledger.FUEL_TANK,
			filters={"name": ["in", tanks]} if tanks else {},
			fields=["name", "label", "disabled"],
		)
	}
	names = tanks or sorted(known)
	balances = ledger.balances_as_of(as_of, [name for name in names if name in known]) if known else {}
	drafts = _pending_drafts([name for name in names if name in known], as_of)
	result = []
	for name in names:
		tank = known.get(name)
		result.append(
			{
				"fuel_tank": name,
				"found": bool(tank),
				"label": tank.label if tank else None,
				"disabled": cint(tank.disabled) if tank else None,
				"balance": flt(balances.get(name, 0)),
				"pending_drafts": drafts.get(name, 0),
			}
		)
	return _answer({"as_of": as_of, "tanks": result})


def _pending_drafts(tanks, as_of):
	counts = {}
	if not tanks:
		return counts
	for doctype in DRAFT_DOCTYPES:
		if not frappe.get_meta(doctype).has_field("fuel_tank"):
			continue
		for tank, count in frappe.db.sql(
			f"""select fuel_tank, count(*) from `tab{doctype}`
			where docstatus = 0 and fuel_tank in %(tanks)s
			and timestamp(posting_date, ifnull(posting_time, '00:00:00')) <= %(as_of)s
			group by fuel_tank""",
			{"tanks": tuple(tanks), "as_of": as_of},
		):
			counts[tank] = counts.get(tank, 0) + cint(count)
	return counts


# ---- envelope -------------------------------------------------------------------------------------
def _answer(result, message=None):
	return {"ok": True, "code": None, "message": message, "retryable": False, "result": result}


def _refused(refusal):
	frappe.clear_messages()
	return {
		"ok": False,
		"code": refusal.code,
		"message": refusal.message,
		"retryable": refusal.code in RETRYABLE,
		"result": None,
	}


def _in_transaction(step, retry_on_duplicate=False):
	"""Run one write and commit it, or roll it all back and answer with the refusal. The session waits
	at most LOCK_WAIT_SECONDS for a row lock. A duplicate key (a racing first write committed first)
	is answered by running the step once more, which then reads the winner's row."""
	previous = _set_lock_wait(LOCK_WAIT_SECONDS)
	try:
		for attempt in (1, 2):
			try:
				result = step()
				frappe.db.commit()
				return result
			except Exception as exc:
				frappe.db.rollback()
				if (
					retry_on_duplicate
					and attempt == 1
					and isinstance(exc, frappe.DuplicateEntryError | frappe.UniqueValidationError)
				):
					frappe.clear_messages()
					continue
				refusal = _as_refusal(exc)
				if refusal is None:
					raise
				return _refused(refusal)
	finally:
		_set_lock_wait(previous)


def _read_only(step):
	try:
		return step()
	except Exception as exc:
		refusal = _as_refusal(exc)
		if refusal is None:
			raise
		return _refused(refusal)


def _set_lock_wait(seconds):
	previous = frappe.db.sql("select @@session.innodb_lock_wait_timeout")[0][0]
	frappe.db.sql("set session innodb_lock_wait_timeout = %s", (int(seconds),))
	return previous


def _as_refusal(exc):
	"""Map an exception raised inside the call to its code; None = not ours to answer."""
	if isinstance(exc, Refusal):
		return exc
	if isinstance(exc, FuelInventoryError):
		return Refusal(code_of(exc), _message(exc))
	if isinstance(
		exc,
		frappe.QueryDeadlockError
		| frappe.QueryTimeoutError
		| frappe.TimestampMismatchError
		| frappe.DuplicateEntryError
		| frappe.UniqueValidationError,
	):
		return Refusal(LOCK_RETRY, _message(exc))
	if _is_lock_error(exc):
		return Refusal(LOCK_RETRY, _message(exc))
	if isinstance(exc, frappe.ValidationError | frappe.PermissionError):
		return Refusal(ERP_VALIDATION, _message(exc))
	return None


def _is_lock_error(exc):
	try:
		return bool(frappe.db.is_deadlocked(exc) or frappe.db.is_timedout(exc))
	except Exception:
		return False


def _message(exc):
	return strip_html(str(exc) or exc.__class__.__name__)[:500]


def _check_permitted(doctype, rights):
	for right in rights:
		if not frappe.has_permission(doctype, right):
			raise Refusal(ERP_VALIDATION, f"Not permitted: needs {right} on {doctype}")


# ---- input ----------------------------------------------------------------------------------------
def _given(value):
	return value is not None and cstr(value).strip() != ""


def _flag(value):
	"""True, False, or None when not given."""
	if not _given(value):
		return None
	if isinstance(value, bool):
		return value
	text = cstr(value).strip().lower()
	if text in ("1", "true", "yes"):
		return True
	if text in ("0", "false", "no"):
		return False
	raise Refusal(ERP_VALIDATION, f"expected a true/false value, got {value!r}")


def _text(value, field, required=True):
	text = cstr(value).strip() if value is not None else ""
	if not text:
		if required:
			raise Refusal(ERP_VALIDATION, f"{field} is required")
		return None
	if len(text) > MAX_LENGTH:
		raise Refusal(ERP_VALIDATION, f"{field} must be at most {MAX_LENGTH} characters")
	return text


def _is_number(value):
	if isinstance(value, bool):
		return False
	if isinstance(value, int | float):
		return True
	try:
		float(cstr(value).strip())
		return True
	except ValueError:
		return False


def _number(value, field, minimum=None):
	if not _is_number(value) or not math.isfinite(float(value)):
		raise Refusal(ERP_VALIDATION, f"{field} must be a number, got {value!r}")
	number = float(value)
	if minimum is not None and number < minimum:
		raise Refusal(ERP_VALIDATION, f"{field} must be at least {minimum}, got {value!r}")
	return number


def _whole(value, field):
	if not _given(value):
		return None
	number = _number(value, field, minimum=0)
	if number != int(number):
		raise Refusal(ERP_VALIDATION, f"{field} must be a whole number, got {value!r}")
	return int(number)


def _datetime(value, field):
	try:
		parsed = get_datetime(value)
	except Exception:
		parsed = None
	if not parsed:
		raise Refusal(ERP_VALIDATION, f"{field} must be a datetime as YYYY-MM-DD HH:MM:SS, got {value!r}")
	return parsed


def _tank_list(tank_ids):
	if not _given(tank_ids) and not isinstance(tank_ids, list):
		return []
	if isinstance(tank_ids, str):
		text = tank_ids.strip()
		if text.startswith("["):
			try:
				tank_ids = json.loads(text)
			except ValueError:
				raise Refusal(ERP_VALIDATION, "tank_ids must be a list of App Tank IDs") from None
		else:
			tank_ids = text.split(",")
	if not isinstance(tank_ids, list | tuple):
		raise Refusal(ERP_VALIDATION, "tank_ids must be a list of App Tank IDs")
	tanks = []
	for tank in tank_ids:
		text = _text(tank, "tank_ids[]", required=False)
		if text and text not in tanks:
			tanks.append(text)
	return tanks
