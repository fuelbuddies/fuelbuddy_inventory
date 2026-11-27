"""The app's fields on ERPNext's stock documents, created idempotently with the app's module set (so
uninstall removes the Custom Field records).

- ``fuel_tank`` (Link Fuel Tank, header, not editable after submit) on Delivery Note, Purchase
  Receipt and Stock Entry;
- ``fuel_tank_readings`` (Table Fuel Tank Reading) and ``fuel_count_needs_approval`` (read-only
  Check) on Stock Reconciliation.

Delivery Note is a large, busy table, so its column is added the way
fuelbuddy_crm.dn_key_columns adds its key columns:

1. the column first, alone: ``varchar(140) DEFAULT NULL`` (exactly what Frappe builds for a Link
   field), with ALGORITHM=INSTANT, falling back to NOCOPY, never a table rebuild;
2. its plain index ``fuel_tank_index`` (the name Frappe gives a search_index field's index) with
   ALGORITHM=INPLACE, LOCK=NONE, so writes carry on while it builds;
3. each ALTER under ``SET SESSION lock_wait_timeout = 5``, retried after 2, 4, 8 and 16 s on a lock
   wait, so a busy table is frozen for 5 s at most per try;
4. the Custom Field record last, without Frappe's table sync (which would also apply any other
   pending schema change on Delivery Note, uncapped): the column and index already match it.

Every step reads information_schema first, so a re-run (after_migrate) changes nothing once done,
and a run stopped anywhere leaves a valid table that the next run finishes. Purchase Receipt and
Stock Entry take the normal create_custom_fields path.
"""

import json
import time

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field, create_custom_fields

MODULE = "Fuelbuddy Inventory"
TANK_FIELD = "fuel_tank"
READINGS_FIELD = "fuel_tank_readings"
APPROVAL_FIELD = "fuel_count_needs_approval"

DN = "Delivery Note"
DN_TABLE = "tabDelivery Note"
COLUMN_TYPE = "varchar(140)"
INDEX_NAME = f"{TANK_FIELD}_index"

LOCK_WAIT_SECONDS = 5
ATTEMPTS = 5
FIRST_BACKOFF_SECONDS = 2
ER_LOCK_WAIT_TIMEOUT = 1205
ER_LOCK_DEADLOCK = 1213
ER_ALTER_OPERATION_NOT_SUPPORTED = 1845
ER_ALTER_OPERATION_NOT_SUPPORTED_REASON = 1846


def tank_field(insert_after):
	return {
		"fieldname": TANK_FIELD,
		"fieldtype": "Link",
		"options": "Fuel Tank",
		"label": "Fuel Tank",
		"insert_after": insert_after,
		"search_index": 1,
		"allow_on_submit": 0,
		"print_hide": 1,
		# The desk picker leaves disabled tanks out; submit refuses them anyway (TANK_NOT_FOUND).
		"link_filters": json.dumps([["Fuel Tank", "disabled", "=", 0]]),
		"description": "The tank the fuel on this document leaves or enters (the app's tank id). "
		"Required on fuel documents dated from tracking start. Not editable after submit: cancel and "
		"amend to change it.",
		"module": MODULE,
	}


CUSTOM_FIELDS = {
	"Purchase Receipt": [tank_field("set_warehouse")],
	"Stock Entry": [tank_field("stock_entry_type")],
	"Stock Reconciliation": [
		{
			"fieldname": READINGS_FIELD,
			"fieldtype": "Table",
			"options": "Fuel Tank Reading",
			"label": "Fuel Tank Readings",
			"insert_after": "items",
			"allow_on_submit": 0,
			"description": "One reading per tank, in litres. For the fuel item in the fuel warehouse the "
			"readings must add up to the counted quantity; each becomes the tank's balance at this "
			"posting time.",
			"module": MODULE,
		},
		{
			"fieldname": APPROVAL_FIELD,
			"fieldtype": "Check",
			"label": "Fuel Count Needs Approval",
			"insert_after": READINGS_FIELD,
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 0,
			"description": "Set when the fuel row's difference from the book exceeds the approval threshold "
			"(Fuel Inventory Settings). For an ERP Workflow condition; the app itself refuses nothing on it.",
			"module": MODULE,
		},
	],
}
DN_FIELD = tank_field("set_warehouse")


def ensure_all():
	ensure_delivery_note_field()
	create_custom_fields(CUSTOM_FIELDS)


# ---- Delivery Note ----------------------------------------------------------------------------
class ColumnStepError(Exception):
	"""The Delivery Note step stopped. The table is valid: each ALTER applies whole or not at all."""


def ensure_delivery_note_field(log=print, sleep=time.sleep):
	if frappe.db.db_type != "mariadb":
		create_custom_fields({DN: [DN_FIELD]})
		return
	column = _column()
	if column and (
		column.column_type.lower() != COLUMN_TYPE
		or column.is_nullable != "YES"
		or _default(column.column_default) is not None
	):
		raise ColumnStepError(
			f"{DN_TABLE}.{TANK_FIELD} exists as {column.column_type} nullable={column.is_nullable} "
			f"default={column.column_default}, not {COLUMN_TYPE} NULL DEFAULT NULL. Frappe would rebuild "
			"the table to change it; nothing was changed. Fix it by hand, then re-run bench migrate."
		)
	previous = frappe.db.sql("select @@session.lock_wait_timeout")[0][0]
	frappe.db.sql(f"set session lock_wait_timeout = {LOCK_WAIT_SECONDS}")
	try:
		if not column:
			_add_column(log, sleep)
		if not _has_plain_index():
			_alter(
				f"alter table `{DN_TABLE}` add index if not exists `{INDEX_NAME}` (`{TANK_FIELD}`), "
				"algorithm=inplace, lock=none",
				log,
				sleep,
			)
	finally:
		frappe.db.sql(f"set session lock_wait_timeout = {int(previous)}")
	_register_dn_field()


def _column():
	rows = frappe.db.sql(
		"""select column_type, is_nullable, column_default from information_schema.columns
		where table_schema = database() and table_name = %s and column_name = %s""",
		(DN_TABLE, TANK_FIELD),
		as_dict=True,
	)
	return rows[0] if rows else None


def _default(value):
	"""information_schema shows a NULL default as NULL or, from MariaDB 10.2.7, as the text 'NULL'."""
	return None if value is None or str(value).upper() == "NULL" else str(value)


def _has_plain_index():
	"""A non-unique index led by the column: what Frappe counts as the field's search index."""
	return bool(
		frappe.db.sql(
			"""select 1 from information_schema.statistics
			where table_schema = database() and table_name = %s and column_name = %s
			and non_unique = 1 and seq_in_index = 1 limit 1""",
			(DN_TABLE, TANK_FIELD),
		)
	)


def _add_column(log, sleep):
	refusals = []
	for algorithm in ("INSTANT", "NOCOPY"):
		try:
			return _alter(
				f"alter table `{DN_TABLE}` add column if not exists `{TANK_FIELD}` {COLUMN_TYPE} "
				f"default null, algorithm={algorithm}",
				log,
				sleep,
			)
		except Exception as e:
			if _error_code(e) not in (
				ER_ALTER_OPERATION_NOT_SUPPORTED,
				ER_ALTER_OPERATION_NOT_SUPPORTED_REASON,
			):
				raise
			refusals.append(f"{algorithm}: {e}")
	raise ColumnStepError(
		f"MariaDB would add {DN_TABLE}.{TANK_FIELD} only by rebuilding the table ({'; '.join(refusals)}). "
		"Nothing was changed; do not add it without an ALGORITHM clause on a busy table."
	)


def _alter(statement, log, sleep):
	"""One ALTER, retried after a pause while it fails on a lock wait (it changed nothing then)."""
	for attempt in range(1, ATTEMPTS + 1):
		try:
			frappe.db.sql_ddl(statement)  # DDL commits the open transaction first
			frappe.cache.hdel("table_columns", DN_TABLE)
			log(f"fuelbuddy_inventory: {statement}")
			return statement
		except Exception as e:
			if _error_code(e) not in (ER_LOCK_WAIT_TIMEOUT, ER_LOCK_DEADLOCK) or attempt == ATTEMPTS:
				raise
			pause = FIRST_BACKOFF_SECONDS * 2 ** (attempt - 1)
			log(f"fuelbuddy_inventory: lock wait on {DN_TABLE}, nothing changed; retry in {pause} s")
			sleep(pause)


def _error_code(error):
	"""MariaDB's error number behind an exception (pymysql puts it first in args), or None."""
	seen = set()
	while error is not None and id(error) not in seen:
		seen.add(id(error))
		first = error.args[0] if error.args else None
		if isinstance(first, int) and not isinstance(first, bool):
			return first
		error = first if isinstance(first, BaseException) else error.__cause__
	return None


def _register_dn_field():
	"""The Custom Field record, without Frappe's table sync (the column and index match it already)."""
	if frappe.db.exists("Custom Field", {"dt": DN, "fieldname": TANK_FIELD}):
		return
	previous = frappe.flags.in_create_custom_fields
	frappe.flags.in_create_custom_fields = True
	try:
		create_custom_field(DN, {**DN_FIELD, "owner": "Administrator"})
	finally:
		frappe.flags.in_create_custom_fields = previous
	frappe.clear_cache(doctype=DN)
