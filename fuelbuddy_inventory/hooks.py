app_name = "fuelbuddy_inventory"
app_title = "Fuelbuddy Inventory"
app_publisher = "FuelBuddy"
app_description = "Litres-only ledger per fuel tank beside ERPNext's single fuel warehouse"
app_email = "dev@example.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "fuelbuddy_inventory.install.after_install"
after_migrate = "fuelbuddy_inventory.install.after_migrate"
before_uninstall = "fuelbuddy_inventory.install.before_uninstall"

# Runs ERPNext's test setup (company, fiscal year) before this app's tests: `bench run-tests --app
# fuelbuddy_inventory` runs only this app's before_tests hooks.
before_tests = "fuelbuddy_inventory.tests.fixtures.before_tests"

# Tank documents join ERPNext's Accounting Period closing list (ERPNext's back-dating settings never
# see a document that posts no stock). Their controllers call validate_accounting_period_on_doc_save.
period_closing_doctypes = ["Tank Transfer", "Fuel Tank Opening"]

# Deleting a cancelled stock document is ERPNext's to allow: its cancelled tank rows stay as they
# are, and do not block it. (Fuel Tank refuses its own deletion while it has rows.)
ignore_links_on_delete = ["Tank Ledger Entry"]

_STOCK = "fuelbuddy_inventory.stock_hooks"

doc_events = {
	"Delivery Note": {
		"validate": f"{_STOCK}.validate_stock_document",
		"before_submit": f"{_STOCK}.require_tank",
		"on_submit": f"{_STOCK}.post_stock_document",
		"on_cancel": f"{_STOCK}.reverse_voucher",
	},
	"Purchase Receipt": {
		"validate": f"{_STOCK}.validate_stock_document",
		"before_submit": f"{_STOCK}.require_tank",
		"on_submit": f"{_STOCK}.post_stock_document",
		"on_cancel": f"{_STOCK}.reverse_voucher",
	},
	"Stock Entry": {
		"validate": f"{_STOCK}.validate_stock_entry",
		"before_submit": f"{_STOCK}.require_tank",
		"on_submit": f"{_STOCK}.post_stock_document",
		"on_cancel": f"{_STOCK}.reverse_voucher",
	},
	"Stock Reconciliation": {
		"validate": f"{_STOCK}.validate_stock_reconciliation",
		"before_submit": f"{_STOCK}.require_reading_tanks",
		"on_submit": f"{_STOCK}.post_stock_reconciliation",
		"on_cancel": f"{_STOCK}.reverse_voucher",
	},
	"Sales Invoice": {"validate": f"{_STOCK}.refuse_invoice_stock_update"},
	"Purchase Invoice": {"validate": f"{_STOCK}.refuse_invoice_stock_update"},
}

scheduler_events = {
	"cron": {
		"0 2 * * *": ["fuelbuddy_inventory.integrity.daily_check"],
	},
}
