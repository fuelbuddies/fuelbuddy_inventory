"""Fuel Inventory Settings, and the posting-time helpers every hook shares."""

import datetime

import frappe
from frappe.utils import flt, get_datetime, get_time, getdate

SETTINGS = "Fuel Inventory Settings"
DEFAULT_TRACKING_START = "2026-11-30 23:59:59"


def get_settings():
	"""{fuel_item, fuel_warehouse, tracking_start (datetime), approval_threshold_litres}.

	Until both the fuel item and the fuel warehouse are set, the app is inert: no document counts
	as fuel, so nothing is checked and nothing is written."""
	doc = frappe.get_cached_doc(SETTINGS)
	return frappe._dict(
		fuel_item=doc.get("fuel_item") or None,
		fuel_warehouse=doc.get("fuel_warehouse") or None,
		tracking_start=get_datetime(doc.get("tracking_start") or DEFAULT_TRACKING_START),
		approval_threshold_litres=flt(doc.get("approval_threshold_litres")),
	)


def is_configured(settings=None):
	settings = settings or get_settings()
	return bool(settings.fuel_item and settings.fuel_warehouse)


def to_time(value):
	"""A Time field's value (str, timedelta or time) as a datetime.time."""
	if isinstance(value, datetime.timedelta):
		return (datetime.datetime.min + value).time()
	if isinstance(value, datetime.time):
		return value
	return get_time(value or "00:00:00")


def posting_datetime(posting_date, posting_time):
	return datetime.datetime.combine(getdate(posting_date), to_time(posting_time))


def doc_posting_datetime(doc):
	return posting_datetime(doc.posting_date, doc.get("posting_time"))


def is_tracked(doc, settings=None):
	"""True when the app is configured and `doc` is dated on or after tracking start.

	Documents dated before tracking start need no tank and write no tank rows."""
	settings = settings or get_settings()
	if not is_configured(settings) or not doc.get("posting_date"):
		return False
	return doc_posting_datetime(doc) >= settings.tracking_start
