"""Temporary, read-only export of an existing DCC Postgres database.

Deploy as a separate service in the original DCC project, with its own public
domain and DATABASE_URL reference to that project's Postgres. Protect it with
a fresh DCC_EXPORT_TOKEN, download once, then remove the domain and service.
No data or credentials are printed in logs.
"""

from __future__ import annotations

import decimal
import hmac
import io
import json
import os
import zipfile
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sqlalchemy as sa

TABLES = ("contractors", "routes", "field_nation_orders", "legacy_route_links",
          "route_events", "bundle_maps", "wo_counters")
DATABASE_URL = os.environ["DATABASE_URL"]
TOKEN = os.environ["DCC_EXPORT_TOKEN"]
engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)


def _json_default(value):
    if isinstance(value, (date, datetime, decimal.Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def _snapshot():
    with engine.begin() as conn:
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        available = set(sa.inspect(conn).get_table_names(schema="public"))
        source_counts = {}
        data = {}
        for table in TABLES:
            if table not in available:
                continue
            records = [dict(row) for row in conn.execute(sa.text(f'SELECT * FROM "{table}"')).mappings()]
            data[table] = records
            source_counts[table] = len(records)
        data["_source_tables"] = sorted(available)
        data["_source_counts"] = source_counts
    raw = json.dumps(data, default=_json_default, separators=(",", ":")).encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dcc_source.json", raw)
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = b'<html><body><h1>DCC source snapshot</h1><form method="POST"><label>Export token <input type="password" name="token"></label><button type="submit">Download read-only snapshot</button></form></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        from urllib.parse import parse_qs
        size = int(self.headers.get("Content-Length") or "0")
        if size <= 0 or size > 1024:
            self.send_error(400)
            return
        submitted = parse_qs(self.rfile.read(size).decode("utf-8")).get("token", [""])[0]
        if not TOKEN or not hmac.compare_digest(TOKEN, submitted):
            self.send_error(403)
            return
        try:
            body = _snapshot()
        except Exception:
            self.send_error(500, "Export failed; check service logs")
            raise
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="dcc-source-snapshot.zip"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Avoid logging URLs or user input; only the response status code.
        print("export request", args[1] if len(args) > 1 else "", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
