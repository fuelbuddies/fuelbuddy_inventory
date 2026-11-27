"""The tank ledger: litres per tank, one Tank Ledger Entry per (voucher, tank).

Rows are ordered by ``(posting_datetime, creation, name)``, the key ERPNext orders its stock ledger
by. A row's ``balance_after`` is the tank's balance after it. An anchor (``is_anchor``) is a count:
its balance is the measured litres and its ``qty`` is the difference from the book.

Placing a row with quantity q (a back-dated row is the normal case):

1. the caller locks the tank rows (``lock_tanks``: last, sorted, one statement);
2. the previous row's balance is read with a locking read;
3. the row is inserted;
4. every later row of that tank up to its next anchor moves by q in one UPDATE, and that anchor's
   qty moves by -q (it keeps its measured balance and absorbs the change).

Removing a row (its voucher is cancelled) is the same with -q. Removing an anchor works the same
way: the rows after it were computed from its balance, which was the previous balance plus its qty.

All reads of tank rows are locking reads: under MariaDB's REPEATABLE READ a plain read sees the
snapshot taken at the transaction's first read and can miss rows another post committed since.
Nothing here commits; it all happens in the transaction of the document being submitted or cancelled.
"""

from decimal import Decimal

import frappe
from frappe.utils import flt, now_datetime

from fuelbuddy_inventory.settings import get_settings

TLE = "Tank Ledger Entry"
TABLE = "`tabTank Ledger Entry`"
FUEL_TANK = "Fuel Tank"
NINE_PLACES = Decimal("0.000000001")


def to_decimal(value):
	"""Litres as an exact decimal (9 places, the column's scale), so SQL arithmetic stays exact."""
	if value is None:
		return Decimal(0)
	if isinstance(value, Decimal):
		return value.quantize(NINE_PLACES)
	return Decimal(str(flt(value, 9))).quantize(NINE_PLACES)


# ---- keys -------------------------------------------------------------------------------------
# A key is (posting_datetime, creation, name). These fragments compare a row's key with key `p`,
# given as %(p_pd)s, %(p_c)s, %(p_n)s. Each starts with a plain posting_datetime bound, so the
# (fuel_tank, is_cancelled, posting_datetime, creation) index serves the range.


def _after(p):
	return (
		f"(posting_datetime >= %({p}_pd)s and (posting_datetime > %({p}_pd)s or creation > %({p}_c)s"
		f" or (creation = %({p}_c)s and name > %({p}_n)s)))"
	)


def _at_or_after(p):
	return (
		f"(posting_datetime >= %({p}_pd)s and (posting_datetime > %({p}_pd)s or creation > %({p}_c)s"
		f" or (creation = %({p}_c)s and name >= %({p}_n)s)))"
	)


def _before(p):
	return (
		f"(posting_datetime <= %({p}_pd)s and (posting_datetime < %({p}_pd)s or creation < %({p}_c)s"
		f" or (creation = %({p}_c)s and name < %({p}_n)s)))"
	)


def _key_params(p, key):
	posting, creation, name = key
	return {f"{p}_pd": posting, f"{p}_c": creation, f"{p}_n": int(name)}


def row_key(row):
	return (row.posting_datetime, row.creation, row.name)


# ---- locks ------------------------------------------------------------------------------------
def lock_tanks(tanks):
	"""Lock the Fuel Tank rows, sorted by name, in one statement. Called last in a submit or cancel:
	after ERPNext's own stock locks, and never followed by an ERP post. Returns the sorted names."""
	names = sorted({tank for tank in tanks if tank})
	if names:
		frappe.db.sql(
			"select name from `tabFuel Tank` where name in %(names)s order by name for update",
			{"names": tuple(names)},
		)
	return names


def _previous_row(tank, key):
	rows = frappe.db.sql(
		f"""select name, balance_after from {TABLE}
		where fuel_tank = %(tank)s and is_cancelled = 0 and {_before("k")}
		order by posting_datetime desc, creation desc, name desc
		limit 1 for update""",
		{"tank": tank, **_key_params("k", key)},
		as_dict=True,
	)
	return rows[0] if rows else None


def next_anchor(tank, key):
	"""The first active anchor of `tank` after `key` (locking read), or None."""
	rows = frappe.db.sql(
		f"""select name, posting_datetime, creation from {TABLE}
		where fuel_tank = %(tank)s and is_cancelled = 0 and is_anchor = 1 and {_after("k")}
		order by posting_datetime, creation, name
		limit 1 for update""",
		{"tank": tank, **_key_params("k", key)},
		as_dict=True,
	)
	return rows[0] if rows else None


def _shift(tank, key, delta, now):
	"""Move every active row of `tank` after `key`, up to its next anchor, by `delta` litres, and that
	anchor's qty by -delta. Returns the anchor (or None): the end of the affected range."""
	anchor = next_anchor(tank, key)
	if not delta:
		return anchor
	params = {"tank": tank, "delta": delta, "now": now, "user": frappe.session.user, **_key_params("k", key)}
	bound = ""
	if anchor:
		bound = f" and {_before('a')}"
		params.update(_key_params("a", row_key(anchor)))
	frappe.db.sql(
		f"""update {TABLE}
		set balance_after = balance_after + %(delta)s, modified = %(now)s, modified_by = %(user)s
		where fuel_tank = %(tank)s and is_cancelled = 0 and {_after("k")}{bound}""",
		params,
	)
	if anchor:
		frappe.db.sql(
			f"""update {TABLE} set qty = qty - %(delta)s, modified = %(now)s, modified_by = %(user)s
			where name = %(name)s""",
			{"delta": delta, "now": now, "user": frappe.session.user, "name": anchor.name},
		)
	return anchor


# ---- writes -----------------------------------------------------------------------------------
def place(tank, voucher_type, voucher_no, posting_datetime, qty=None, measured=None):
	"""Insert one row for `tank` and recalculate the tank up to its next anchor.

	Give `qty` (signed litres) for a movement, or `measured` (litres) for an anchor. The caller has
	locked the tank (lock_tanks). Returns {name, key, qty, balance_after, anchor}."""
	now = now_datetime()
	name = int(frappe.db.get_next_sequence_val(TLE))
	key = (posting_datetime, now, name)
	previous = _previous_row(tank, key)
	previous_balance = to_decimal(previous.balance_after) if previous else Decimal(0)
	if measured is not None:
		is_anchor = 1
		balance = to_decimal(measured)
		qty = balance - previous_balance
	else:
		is_anchor = 0
		qty = to_decimal(qty)
		balance = previous_balance + qty

	frappe.db.sql(
		f"""insert into {TABLE}
		(name, creation, modified, modified_by, owner, docstatus, idx,
		 fuel_tank, posting_datetime, voucher_type, voucher_no, qty, balance_after, is_anchor, is_cancelled)
		values (%(name)s, %(now)s, %(now)s, %(user)s, %(user)s, 0, 0,
		 %(tank)s, %(posting)s, %(voucher_type)s, %(voucher_no)s, %(qty)s, %(balance)s, %(is_anchor)s, 0)""",
		{
			"name": name,
			"now": now,
			"user": frappe.session.user,
			"tank": tank,
			"posting": posting_datetime,
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"qty": qty,
			"balance": balance,
			"is_anchor": is_anchor,
		},
	)
	anchor = _shift(tank, key, qty, now)
	return frappe._dict(name=name, tank=tank, key=key, qty=qty, balance_after=balance, anchor=anchor)


def remove_voucher(voucher_type, voucher_no):
	"""Mark the voucher's active rows cancelled and recalculate each tank up to its next anchor.
	Returns [{name, tank, key, qty, anchor}] for the rows removed (empty when it wrote none)."""
	# A plain read only to learn which tanks to lock: the voucher's rows were written by its submit.
	# Taking their row locks before the tank locks would invert the lock order.
	tanks = frappe.db.sql_list(
		f"""select distinct fuel_tank from {TABLE}
		where voucher_type = %s and voucher_no = %s and is_cancelled = 0""",
		(voucher_type, voucher_no),
	)
	if not tanks:
		return []
	lock_tanks(tanks)
	rows = frappe.db.sql(
		f"""select name, fuel_tank, posting_datetime, creation, qty from {TABLE}
		where voucher_type = %s and voucher_no = %s and is_cancelled = 0
		order by fuel_tank, posting_datetime, creation, name
		for update""",
		(voucher_type, voucher_no),
		as_dict=True,
	)
	now = now_datetime()
	removed = []
	for row in rows:
		frappe.db.sql(
			f"update {TABLE} set is_cancelled = 1, modified = %s, modified_by = %s where name = %s",
			(now, frappe.session.user, row.name),
		)
		qty = to_decimal(row.qty)
		anchor = _shift(row.fuel_tank, row_key(row), -qty, now)
		removed.append(
			frappe._dict(
				name=row.name, tank=row.fuel_tank, key=row_key(row), qty=qty, anchor=anchor, removed=True
			)
		)
	return removed


# ---- reads ------------------------------------------------------------------------------------
def first_out_of_range(tank, start_key, end_anchor, condition, params=None, inclusive=True):
	"""The first active row of `tank` from `start_key` (inclusive or not) up to `end_anchor`
	(exclusive; None = to the end) that meets `condition` (SQL on balance_after). Locking read."""
	query_params = {"tank": tank, **(params or {}), **_key_params("s", start_key)}
	bound = ""
	if end_anchor:
		bound = f" and {_before('a')}"
		query_params.update(_key_params("a", row_key(end_anchor)))
	start = _at_or_after("s") if inclusive else _after("s")
	rows = frappe.db.sql(
		f"""select name, posting_datetime, voucher_type, voucher_no, balance_after from {TABLE}
		where fuel_tank = %(tank)s and is_cancelled = 0 and {start}{bound} and ({condition})
		order by posting_datetime, creation, name
		limit 1 for update""",
		query_params,
		as_dict=True,
	)
	return rows[0] if rows else None


def latest_anchor(tank):
	rows = frappe.db.sql(
		f"""select name, posting_datetime, creation from {TABLE}
		where fuel_tank = %s and is_cancelled = 0 and is_anchor = 1
		order by posting_datetime desc, creation desc, name desc
		limit 1 for update""",
		(tank,),
		as_dict=True,
	)
	return rows[0] if rows else None


def balance_as_of(tank, as_of=None, for_update=False):
	"""The tank's balance after its last active row dated at or before `as_of` (None = all rows)."""
	condition = "and posting_datetime <= %(as_of)s" if as_of else ""
	lock = " for update" if for_update else ""
	rows = frappe.db.sql(
		f"""select balance_after from {TABLE}
		where fuel_tank = %(tank)s and is_cancelled = 0 {condition}
		order by posting_datetime desc, creation desc, name desc
		limit 1{lock}""",
		{"tank": tank, "as_of": as_of},
	)
	return to_decimal(rows[0][0]) if rows else Decimal(0)


def balances_as_of(as_of=None, tanks=None):
	"""{tank: balance} for every Fuel Tank (or `tanks`), each from its last active row dated at or
	before `as_of`. One statement; each tank's lookup is an index range read."""
	condition = "and l.posting_datetime <= %(as_of)s" if as_of else ""
	tank_filter = "where t.name in %(tanks)s" if tanks else ""
	rows = frappe.db.sql(
		f"""select t.name,
			(select l.balance_after from {TABLE} l
			 where l.fuel_tank = t.name and l.is_cancelled = 0 {condition}
			 order by l.posting_datetime desc, l.creation desc, l.name desc limit 1) as balance
		from `tabFuel Tank` t {tank_filter}""",
		{"as_of": as_of, "tanks": tuple(tanks or ()) or ("",)},
	)
	return {name: to_decimal(balance) for name, balance in rows}


def voucher_stock_qty(voucher_type, voucher_no, settings=None):
	"""Signed litres the voucher moved in the fuel warehouse: SUM(actual_qty) of its own active Stock
	Ledger Entries for the fuel item. None when it has none. Returns, units and conversions are then
	exactly ERP's."""
	settings = settings or get_settings()
	rows = frappe.db.sql(
		"""select count(*), sum(actual_qty) from `tabStock Ledger Entry`
		where voucher_type = %s and voucher_no = %s and item_code = %s and warehouse = %s
		and is_cancelled = 0""",
		(voucher_type, voucher_no, settings.fuel_item, settings.fuel_warehouse),
	)
	count, total = rows[0]
	return to_decimal(total) if count else None


def warehouse_qty_as_of(as_of=None, settings=None):
	"""ERP's quantity of the fuel item in the fuel warehouse at `as_of` (None = everything posted),
	from the stock ledger itself, never the Bin (stale during a DN drain), and never a later SLE's
	qty_after_transaction (stale until ERPNext's future-entry update or repost runs).

	A Stock Reconciliation entry is ERP's own anchor: it stores the counted quantity in
	qty_after_transaction (its actual_qty is 0). So: the last such count at or before `as_of`, plus
	SUM(actual_qty) of every other active entry after it; with no count, the plain sum."""
	settings = settings or get_settings()
	params = {
		"item": settings.fuel_item,
		"warehouse": settings.fuel_warehouse,
		"as_of": as_of,
	}
	until = "and posting_datetime <= %(as_of)s" if as_of else ""
	counts = frappe.db.sql(
		f"""select name, posting_datetime, creation, qty_after_transaction from `tabStock Ledger Entry`
		where item_code = %(item)s and warehouse = %(warehouse)s and is_cancelled = 0
		and voucher_type = 'Stock Reconciliation' {until}
		order by posting_datetime desc, creation desc limit 1""",
		params,
		as_dict=True,
	)
	if counts:
		count = counts[0]
		params.update(c_pd=count.posting_datetime, c_c=count.creation)
		since = "and (posting_datetime > %(c_pd)s or (posting_datetime = %(c_pd)s and creation > %(c_c)s))"
		base = to_decimal(count.qty_after_transaction)
	else:
		since = ""
		base = Decimal(0)
	moved = frappe.db.sql(
		f"""select sum(actual_qty) from `tabStock Ledger Entry`
		where item_code = %(item)s and warehouse = %(warehouse)s and is_cancelled = 0
		and voucher_type != 'Stock Reconciliation' {since} {until}""",
		params,
	)[0][0]
	return base + to_decimal(moved)
