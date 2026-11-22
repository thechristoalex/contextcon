#!/bin/sh
# Reset the desk to a clean demo state. Keeps the vendor-check results and response cache
# (so live checks replay instantly and cost nothing), clears documents, payments and the log.
# A copy of the current database is saved first.
set -e
cd "$(dirname "$0")/.."
DB=data/vendorcheck.db
[ -f "$DB" ] && cp "$DB" "data/vendorcheck.backup.$(date +%H%M%S).db"
sqlite3 "$DB" "DELETE FROM invoices; DELETE FROM files_seen; DELETE FROM payments; DELETE FROM events;"
rm -f data/fixtures/inbox/scan_*.txt
echo "Clean. Start the desk with: python3 -m vendorcheck.cli serve --port 8080"
