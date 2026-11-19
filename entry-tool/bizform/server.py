#!/usr/bin/env python3
"""A small server-rendered business form: the kind of page that has no import
API and must be filled in by hand (or by a browser driver).

Design notes
------------
* Every mutation happens through a normal HTML form POST, so a real browser
  click is required.  There is deliberately no JSON import endpoint.
* The business key is unique.  A POST whose key already exists either matches
  the stored record exactly (reported "unchanged") or is rejected as a
  conflict -- existing data is never overwritten.
* Query is by business key and always returns the latest complete record.
* Values keep their native type (and the NA / "" / 0 / FALSE distinctions)
  by transporting each cell as a type-tagged object.
* Fault injection (query string `fault=...`) exists only so the driver's
  "a save is not success until re-queried" rule can be exercised:
      lost_ack     commit, then drop the connection (response is lost)
      slow_ack     commit, then stall before replying (driver times out)
      slow_no_commit  stall, then drop without committing
      ack_no_commit    render a normal "Saved" page WITHOUT storing anything
      ack_altered      store a record whose measured value was altered
      force_validation  reject with a validation reason even for valid data

Usage:
    python3 bizform/server.py --schema bizform/schemas/tourism.json \
        --db /var/tmp/bizform/tourism.sqlite --port 8765
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import html
import http.cookies
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.parse
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCHEMA_PATH = None
SCHEMA = None
DB_PATH = None
SECRET = None



# --------------------------------------------------------------------------
# schema / values
# --------------------------------------------------------------------------
def load_schema(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    schema.setdefault("title", schema.get("dataset", "Business form"))
    schema.setdefault("fields", [])
    schema["key_fields"] = list(schema.get("key_fields") or [])
    schema["index_field"] = schema.get("index_field")
    if schema.get("dataset"):
        pass
    names = [f["name"] for f in schema["fields"]]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate field names in schema")
    for k in schema["key_fields"]:
        if k not in names:
            raise SystemExit(f"key field not declared: {k}")
    return schema


FIELD_DEFAULTS = {
    "yearquarter": {"pattern": r"^\d{4} Q[1-4]$"},
    "yearmonth": {"pattern": r"^\d{4} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$"},
    "yearweek": {"pattern": r"^\d{4} W\d{2}$"},
    "date": {"pattern": r"^\d{4}-\d{2}-\d{2}$"},
    "datetime": {
        "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$"
    },
    "integer": {"pattern": r"^-?\d+$"},
    "logical": {"pattern": r"^(TRUE|FALSE)$"},
    "double": {"pattern": None},
    "character": {"pattern": None},
}


def cell(t: str, *, v: str | None = None, na: bool = False, **extra) -> dict:
    out = {"t": t}
    if na:
        out["na"] = True
    else:
        out["v"] = v
        for k, val in extra.items():
            if val is not None:
                out[k] = val
    return out


def parse_cell(value: str, t: str) -> dict:
    """Parse a submitted text value into a typed cell."""
    if t in ("yearquarter", "yearmonth", "yearweek"):
        return cell(t, v=value)
    if t == "date":
        dt.date.fromisoformat(value)
        return cell(t, v=value)
    if t == "datetime":
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return cell(t, v=value, epoch=parsed.timestamp(), tz=value[-6:] if value[-6:].count(":") == 1 else None)
    if t == "integer":
        return cell(t, v=str(int(value)))
    if t == "double":
        d = Decimal(value)
        if not d.is_finite():
            raise ValueError("not a finite number")
        return cell(t, v=value, num=format(float(value), ".17g"))
    if t == "logical":
        return cell(t, v=value.upper())
    return cell(t, v=value)


def validate_value(field: dict, raw: str, is_na: bool) -> list[str]:
    """Return a list of human-readable validation reasons (empty = valid)."""
    name = field["name"]
    t = field["type"]
    if is_na:
        if field.get("required"):
            return [f"{name}: required, cannot be NA"]
        return []
    if raw == "" and field.get("required"):
        return [f"{name}: required, empty string is not allowed"]
    if raw == "":
        return []
    spec = {**FIELD_DEFAULTS.get(t, {}), **{k: v for k, v in field.items() if k in FIELD_DEFAULTS.get(t, {})}}
    pat = spec.get("pattern")
    if pat and not re.match(pat, raw):
        return [f"{name}: value {raw!r} does not match the {t} format"]
    try:
        if t == "double":
            d = Decimal(raw)
            if not d.is_finite():
                return [f"{name}: value {raw!r} is not a finite number"]
            if field.get("min") is not None and d < Decimal(str(field["min"])):
                return [f"{name}: {raw} is below the minimum {field['min']}"]
            if field.get("max") is not None and d > Decimal(str(field["max"])):
                return [f"{name}: {raw} is above the maximum {field['max']}"]
            dec = field.get("decimals")
            if dec is not None:
                exponent = max(0, -d.as_tuple().exponent)
                if exponent > int(dec):
                    return [f"{name}: {raw} has more than {dec} decimal places"]
        elif t == "date":
            dt.date.fromisoformat(raw)
        elif t == "datetime":
            dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        elif t == "integer":
            int(raw)
    except (ValueError, InvalidOperation) as exc:
        return [f"{name}: invalid {t} value {raw!r} ({exc})"]
    if t == "character":
        maxlen = field.get("maxlen")
        if maxlen is not None and len(raw) > int(maxlen):
            return [f"{name}: longer than {maxlen} characters"]
    return []


def normalize_compare(c: dict) -> tuple:
    """Canonical comparison tuple for a stored/expected cell."""
    if not isinstance(c, dict):
        raise ValueError("cell must be an object")
    t = c.get("t")
    if c.get("na"):
        return (t, "NA")
    v = c.get("v")
    if t in ("integer",):
        return (t, "NUM", int(v))
    if t in ("double",):
        return (t, "NUM", Decimal(str(v)))
    if t == "datetime":
        if c.get("epoch") is not None:
            return (t, "NUM", Decimal(str(c["epoch"])))
        parsed = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (t, "NUM", Decimal(str(parsed.timestamp())))
    if t == "logical":
        return (t, "BOOL", str(v).upper() == "TRUE")
    return (t, "STR", "" if v is None else str(v))


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def key_of(cells: dict) -> str:
    parts = []
    for name in SCHEMA["key_fields"]:
        c = cells[name]
        if c.get("na"):
            parts.append([name, "NA"])
        else:
            parts.append([name, str(c.get("v"))])
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=False)


def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS records (
               key_json TEXT PRIMARY KEY,
               fields_json TEXT NOT NULL,
               version INTEGER NOT NULL,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
               token TEXT PRIMARY KEY,
               username TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    return conn


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(24)
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO sessions(token, username, created_at) VALUES(?,?,?)",
            (token, username, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
    finally:
        conn.close()
    return token


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    conn = db_connect()
    try:
        row = conn.execute("SELECT username FROM sessions WHERE token=?", (token,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def drop_session(token: str | None):
    if not token:
        return
    conn = db_connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    finally:
        conn.close()


def fetch(key_json: str):
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT fields_json, version, created_at, updated_at FROM records WHERE key_json=?",
            (key_json,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "fields": json.loads(row[0]),
        "version": row[1],
        "created_at": row[2],
        "updated_at": row[3],
    }


def insert_record(fields: dict, key_json: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT fields_json, version, created_at, updated_at FROM records WHERE key_json=?",
            (key_json,),
        ).fetchone()
        if existing:
            stored = json.loads(existing[0])
            same = all(
                normalize_compare(stored[n]) == normalize_compare(fields[n])
                for n in stored
                if n in fields
            ) and set(stored) == set(fields)
            conn.execute("COMMIT")
            if same:
                return {"outcome": "unchanged", "version": existing[1]}
            return {"outcome": "conflict"}
        conn.execute(
            "INSERT INTO records(key_json, fields_json, version, created_at, updated_at) VALUES(?,?,?,?,?)",
            (key_json, json.dumps(fields, ensure_ascii=False), 1, now, now),
        )
        conn.execute("COMMIT")
        return {"outcome": "created", "version": 1}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


CSS = """
:root { color-scheme: light; }
body { font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
       margin: 0; background: #f6f7f9; color: #1c2430; }
header { background: #12395c; color: #fff; padding: 10px 22px;
         display: flex; justify-content: space-between; align-items: center; }
header h1 { font-size: 16px; margin: 0; font-weight: 600; letter-spacing: .2px; }
header .who { font-size: 13px; opacity: .9; }
main { max-width: 860px; margin: 22px auto 60px; padding: 0 16px; }
section { background: #fff; border: 1px solid #d9dee6; border-radius: 8px;
          padding: 18px 20px; margin-bottom: 18px; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .08em;
     color: #5a6879; margin: 0 0 12px; }
label { display: block; font-size: 13px; color: #3a4656; margin-bottom: 3px; }
.field { margin-bottom: 10px; }
input[type=text] { width: 100%; box-sizing: border-box; padding: 7px 9px;
                   border: 1px solid #b9c2cf; border-radius: 5px; font-size: 14px;
                   background: #fff; }
input[type=text]:focus { outline: 2px solid #7aa7d4; border-color: #7aa7d4; }
.na { font-size: 12px; color: #6a7686; margin-top: 3px; }
button { background: #1766a6; color: #fff; border: 0; border-radius: 5px;
         padding: 8px 18px; font-size: 14px; cursor: pointer; }
button.secondary { background: #e4e9f0; color: #1c3247; }
dl { display: grid; grid-template-columns: 190px 1fr; gap: 4px 14px; margin: 0; }
dt { color: #5a6879; font-size: 13px; }
dd { margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
     font-size: 13px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 12px; font-weight: 600; }
.ok { background: #e2f4e8; color: #14613a; }
.bad { background: #fde8e8; color: #8d1b1b; }
.muted { color: #6a7686; font-size: 13px; }
#errors { border-left: 3px solid #c0392b; background: #fdf1f0; padding: 10px 12px; }
#errors ul { margin: 6px 0 0 18px; }
.na-marker { color: #8d1b1b; font-style: italic; }
.empty-marker { color: #6a7686; }
footer { text-align: center; color: #8a94a3; font-size: 12px; margin-top: 24px; }
"""


def page(title: str, body: str, *, user: str | None = None, wide: bool = False) -> str:
    session = (
        f'<span class="who">signed in as <span id="session-user">{esc(user)}</span>'
        f' &middot; <a href="/logout" style="color:#cfe3f5">log out</a></span>'
        if user
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body>
<header><h1>{esc(SCHEMA['title'])}</h1>{session}</header>
<main>{body}<footer>{esc(SCHEMA.get('footer', 'Business records portal'))} &middot; no import interface</footer></main>
</body></html>"""


def value_marker(c: dict) -> str:
    if c.get("na"):
        return '<span class="na-marker">NULL</span>'
    if c.get("v") == "":
        return '<span class="empty-marker">(empty string)</span>'
    return esc(c.get("v"))


def render_fields(fields: dict, *, editable=False, action="", fault="", errors=None) -> str:
    rows = []
    for f in SCHEMA["fields"]:
        name = f["name"]
        c = fields.get(name) or cell(f["type"], v="")
        if editable:
            val = "" if c.get("na") else ("" if c.get("v") is None else str(c.get("v")))
            checked = " checked" if c.get("na") else ""
            rows.append(
                f'<div class="field"><label for="f-{esc(name)}">{esc(name)} '
                f'<span class="muted">({esc(f["type"])})</span></label>'
                f'<input type="text" id="f-{esc(name)}" name="f.{esc(name)}" '
                f'value="{esc(val)}" autocomplete="off">'
                f'<label class="na"><input type="checkbox" id="na-{esc(name)}" '
                f'name="na.{esc(name)}"{checked}> value is NA</label></div>'
            )
        else:
            rows.append(
                f"<dt>{esc(name)}</dt><dd data-field=\"{esc(name)}\" "
                f"data-type=\"{esc(c.get('t'))}\" data-na=\"{'true' if c.get('na') else 'false'}\" "
                f"data-value=\"{esc('' if c.get('na') else c.get('v'))}\">{value_marker(c)}</dd>"
            )
    body = "\n".join(rows)
    if not editable:
        return f"<dl>{body}</dl>"
    hidden = f'<input type="hidden" name="fault" value="{esc(fault)}">' if fault else ""
    err = render_errors(errors or [])
    return f"""<form method="post" action="{esc(action)}" id="entry-form">
{hidden}
{body}
<div style="margin-top:14px"><button type="submit" id="btn-save">Save record</button>
<a href="/query" class="muted" style="margin-left:12px">Query by key</a></div>
</form>{err}"""


def render_errors(errors: list) -> str:
    if not errors:
        return ""
    items = []
    for e in errors:
        if isinstance(e, dict):
            items.append(
                f'<li data-field="{esc(e.get("field"))}">{esc(e.get("reason"))}</li>'
            )
        else:
            items.append(f"<li>{esc(e)}</li>")
    return f'<div id="errors" role="alert" style="margin-top:14px"><strong>Could not save</strong><ul>' + "".join(items) + "</ul></div>"


def render_result(rec: dict | None, key_values: dict) -> str:
    keydesc = ", ".join(
        f"{k}=" + ("NULL" if v is None else repr(v)) for k, v in key_values.items()
    )
    if not rec:
        return (
            f'<div id="result" data-state="missing" data-key="{esc(keydesc)}">'
            f'<p class="badge bad">No record</p>'
            f'<p class="muted">No stored record for {esc(keydesc)}.</p></div>'
        )
    body = render_fields(rec["fields"])
    return (
        f'<div id="result" data-state="found" data-key="{esc(keydesc)}" '
        f'data-version="{rec["version"]}">'
        f'<p><span class="badge ok">Stored record</span> '
        f'<span class="muted">version {rec["version"]} &middot; updated '
        f'{esc(rec["updated_at"])}</span></p>{body}</div>'
    )


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------
def parse_form(body: bytes) -> dict:
    text = body.decode("utf-8", "replace")
    parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


def collect_cells(form: dict) -> tuple[dict, list]:
    cells: dict = {}
    errors: list = []
    for f in SCHEMA["fields"]:
        name = f["name"]
        raw = form.get(f"f.{name}", "")
        is_na = form.get(f"na.{name}") == "on"
        reasons = validate_value(f, raw, is_na)
        if reasons:
            for r in reasons:
                errors.append({"field": name, "reason": r})
            cells[name] = cell(f["type"], v=raw, na=is_na)
            continue
        if is_na:
            cells[name] = cell(f["type"], na=True)
        else:
            cells[name] = parse_cell(raw, f["type"])
    return cells, errors


class Handler(BaseHTTPRequestHandler):
    server_version = "BizForm/1.0"

    # -- helpers ---------------------------------------------------------
    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get("bf_session")
        return morsel.value if morsel else None

    def _session_user(self) -> str | None:
        return session_user(self._session_token())

    def _redirect(self, location: str, cookie: str | None = None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code: int, body: str, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fault(self, query: dict) -> str:
        return query.get("fault", [""])[0]

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        user = self._session_user()
        q = {k: v[-1] for k, v in query.items()}

        if url.path == "/healthz":
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if url.path == "/login":
            return self._send(200, page("Sign in", self.login_body()))
        if url.path == "/logout":
            drop_session(self._session_token())
            return self._redirect("/login", "bf_session=; Path=/; Max-Age=0; SameSite=Lax")
        if not user:
            return self._redirect("/login")
        if url.path in ("/", "/entry"):
            return self._send(200, page("Enter record", self.entry_body(user, q), user=user))
        if url.path == "/query":
            return self._send(200, page("Query record", self.query_body(user, q), user=user))
        if url.path == "/":
            return self._redirect("/entry")
        return self._send(404, page("Not found", "<p>Not found</p>", user=user))

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        user = self._session_user()
        if url.path == "/login":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_form(self.rfile.read(length) if length else b"")
            login = SCHEMA.get("login") or {}
            if form.get("username") == login.get("username") and form.get("password") == login.get("password"):
                token = create_session(form.get("username", "user"))
                cookie = f"bf_session={token}; Path=/; Max-Age=43200; HttpOnly; SameSite=Lax"
                return self._redirect("/entry", cookie)
            return self._send(401, page("Sign in", self.login_body("Wrong username or password.")))
        if not user:
            return self._redirect("/login")
        if url.path == "/entry":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_form(self.rfile.read(length) if length else b"")
            fault = form.get("fault", "")
            return self.handle_save(user, form, fault)
        return self._send(404, page("Not found", "<p>Not found</p>", user=user))

    # -- pages -----------------------------------------------------------
    def login_body(self, error: str | None = None) -> str:
        err = f'<p id="login-error" class="badge bad">{esc(error)}</p>' if error else ""
        return f"""<section><h2>Sign in</h2>{err}
<form method="post" action="/login" id="login-form">
<div class="field"><label for="username">Username</label>
<input type="text" id="username" name="username" autocomplete="username"></div>
<div class="field"><label for="password">Password</label>
<input type="text" id="password" name="password" autocomplete="current-password"></div>
<button type="submit" id="btn-login">Sign in</button>
</form></section>"""

    def entry_body(self, user: str, q: dict) -> str:
        fault = self._fault({"fault": [q.get("fault", "")]})
        return f"""<section><h2>New record</h2>
<p class="muted">Records are identified by {esc(', '.join(SCHEMA['key_fields']))}.
An existing key is never overwritten.</p>
{render_fields({}, editable=True, action='/entry', fault=fault)}</section>
<section><h2>Find an existing record</h2>
<p class="muted"><a href="/query">Open the query page</a></p></section>"""

    def query_body(self, user: str, q: dict) -> str:
        parts = []
        for f in SCHEMA["fields"]:
            if f["name"] not in SCHEMA["key_fields"]:
                continue
            name = f["name"]
            checked = " checked" if q.get("qna." + name) == "on" else ""
            parts.append(
                f'<div class="field"><label for="q-{esc(name)}">{esc(name)}</label>'
                f'<input type="text" id="q-{esc(name)}" name="q.{esc(name)}" '
                f'value="{esc(q.get("q." + name, ""))}" autocomplete="off">'
                f'<label class="na"><input type="checkbox" id="qna-{esc(name)}" '
                f'name="qna.{esc(name)}"{checked}> key value is NA</label></div>'
            )
        key_inputs = "".join(parts)
        result = ""
        if any(k.startswith("q.") or k.startswith("qna.") for k in q):
            result = self.run_query(q)
        return f"""<section><h2>Query by key</h2>
<form method="get" action="/query" id="query-form">{key_inputs}
<button type="submit" id="btn-query">Query</button></form></section>
<section><h2>Result</h2>{result or '<p class="muted">Enter a key to look up the latest complete record.</p>'}</section>"""

    def run_query(self, q: dict) -> str:
        key_values = {}
        for f in SCHEMA["fields"]:
            if f["name"] not in SCHEMA["key_fields"]:
                continue
            raw = q.get("q." + f["name"], "")
            key_values[f["name"]] = raw if raw != "" else None
        cells = {}
        for f in SCHEMA["fields"]:
            name = f["name"]
            if name in SCHEMA["key_fields"]:
                raw = q.get("q." + name, "")
                is_na = q.get("qna." + name) == "on"
                if is_na:
                    cells[name] = cell(f["type"], na=True)
                else:
                    cells[name] = cell(f["type"], v=raw)
        key_json = json.dumps(
            [[n, "NA" if cells[n].get("na") else str(cells[n].get("v"))] for n in SCHEMA["key_fields"]],
            separators=(",", ":"),
        )
        return render_result(fetch(key_json), key_values)

    # -- save ------------------------------------------------------------
    def handle_save(self, user: str, form: dict, fault: str):
        cells, errors = collect_cells(form)
        if errors:
            return self.finish_save_page(400, cells, None, errors, None)
        if fault == "force_validation":
            errors = [{"field": SCHEMA["fields"][-1]["name"],
                       "reason": "server-side policy rejected this record (injected)"}]
            return self.finish_save_page(422, cells, None, errors, None)
        key_json = key_of(cells)
        if fault == "ack_no_commit":
            # Lie: say it saved, write nothing.
            return self.finish_save_page(200, cells, {"fields": cells}, [], "created")
        if fault == "ack_altered":
            altered = json.loads(json.dumps(cells))
            numeric = [n for n, c in altered.items() if c.get("t") in ("double", "integer") and not c.get("na")]
            if numeric:
                altered[numeric[-1]] = {"t": altered[numeric[-1]]["t"], "v": "999999"}
            insert_record(altered, key_json)
            return self.finish_save_page(200, cells, fetch(key_json), [], "created")
        if fault == "slow_no_commit":
            time.sleep(30)
            self.close_connection = True
            return
        outcome = insert_record(cells, key_json)
        if outcome["outcome"] == "conflict":
            existing = fetch(key_json)
            diffs = diff_fields(existing["fields"], cells) if existing else []
            errors = [{"field": d["field"],
                       "reason": f"a different value is already stored ({d['stored']!r}); refused to overwrite"}
                      for d in diffs] or [{"field": ", ".join(SCHEMA["key_fields"]),
                                           "reason": "key already exists with different values"}]
            return self.finish_save_page(409, cells, existing, errors, None)
        if fault == "lost_ack":
            self.close_connection = True
            try:
                self.connection.close()
            except Exception:
                pass
            return
        if fault == "slow_ack":
            time.sleep(30)
            self.close_connection = True
            return
        return self.finish_save_page(200, cells, fetch(key_json), [], outcome["outcome"])

    def finish_save_page(self, code, cells, rec, errors, outcome):
        status = ""
        if outcome == "unchanged":
            status = '<p><span class="badge ok">Unchanged</span> identical record already stored.</p>'
        elif outcome == "created":
            status = '<p><span class="badge ok">Saved</span> record stored.</p>'
        keydesc = ", ".join(k for k in SCHEMA["key_fields"])
        rendered = render_result(rec, {n: (cells[n].get("v") if not cells[n].get("na") else None) for n in SCHEMA["key_fields"]})
        body = f"""<section><h2>Save result</h2>{status}
{render_errors(errors)}
<form method="get" action="/query" id="post-save-query">
<div class="muted">Look the record up again by key ({esc(keydesc)}).</div>
<button type="submit" class="secondary" id="btn-requery" style="margin-top:10px">Query record</button>
</form></section>
<section><h2>Record</h2>{rendered}</section>"""
        return self._send(code, page("Save result", body, user=self._session_user()))

    def log_message(self, fmt, *args):
        if os.environ.get("BIZFORM_QUIET"):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def diff_fields(stored: dict, incoming: dict) -> list:
    out = []
    for name in incoming:
        if name not in stored:
            continue
        if normalize_compare(stored[name]) != normalize_compare(incoming[name]):
            out.append({
                "field": name,
                "stored": stored[name].get("v") if not stored[name].get("na") else None,
                "incoming": incoming[name].get("v") if not incoming[name].get("na") else None,
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--secret", default="bizform-dev-secret")
    args = ap.parse_args()
    global SCHEMA, DB_PATH, SECRET
    SCHEMA = load_schema(args.schema)
    DB_PATH = os.path.abspath(args.db)
    SECRET = args.secret
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"bizform listening on http://{args.host}:{args.port} db={DB_PATH}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
