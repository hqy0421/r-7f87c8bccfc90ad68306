#!/usr/bin/env python3
"""Drive a real browser to enter tsibble records through a keyed business form,
verifying every record by re-querying it.

Guarantees (all exercised by tests/test_entry.py):

* Values are typed/clicked through Chromium, one record at a time, no import API.
* A save response is never trusted.  After every submit the tool re-queries the
  key on the page and compares *every* field, honouring native types and the
  NA / "" / 0 / FALSE distinctions.
* Same key + same values already stored -> confirmed skip.
  Same key + different values -> conflict, reported, never overwritten.
* Unknown result (navigation failure, timeout, lost acknowledgement) is
  resolved by querying, never by blindly re-submitting.
* Every confirmed record writes a durable receipt bound to the native
  key/index, the expected values, and the target configuration.  Receipts are
  written atomically and appended to a journal.
* On restart, records whose receipt was lost are recovered: if the remote
  already holds the record, the receipt is re-created without re-submitting.
* Validation failures keep the page's reason and yield key/index-correct
  results.

Configuration (JSON): see entry-tool/config.example.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from decimal import Decimal
from pathlib import Path

try:  # playwright is installed into a private target dir by tests/setup
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception as exc:  # pragma: no cover
    sys.stderr.write(
        "playwright is required. Install it with:\n"
        "  python3 -m pip install --target /var/tmp/automation/lib playwright\n"
        f"(import error: {exc})\n"
    )
    raise

RECEIPT_SCHEMA = "tsibble-entry-receipt/v1"
TOOL_VERSION = "1.0.0"

# outcomes, in the vocabulary of the task
CREATED = "created"
UNCHANGED = "skipped_unchanged"
CONFLICT = "conflict"
VALIDATION_FAILED = "validation_failed"
VERIFICATION_FAILED = "verification_failed"
UNKNOWN_AFTER_RETRY = "unresolved"
ERROR = "error"


class ConfigError(Exception):
    pass


class VerificationError(Exception):
    pass


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
class Config:
    def __init__(self, raw: dict, path: str):
        self.path = os.path.abspath(path)
        self.raw = raw
        self.base_url = self._req("base_url").rstrip("/")
        self.entry_path = raw.get("entry_path", "/entry")
        self.query_path = raw.get("query_path", "/query")
        self.login_path = raw.get("login_path", "/login")
        self.record_id = self._req("record_id")
        self.dataset = self._req("dataset")
        self.key_fields = list(self._req("key_fields"))
        self.index_field = self._req("index_field")
        if self.index_field in raw.get("exclude_index_from_key", []):
            raise ConfigError("index field cannot be excluded from the business key")
        self.business_key = list(raw.get("business_key") or (self.key_fields + [self.index_field]))
        missing = [f for f in self.key_fields if f not in self.business_key]
        if missing:
            raise ConfigError(f"key fields not in business_key: {missing}")
        self.selectors = self._req("selectors")
        for required in ("field_input", "na_checkbox", "save_button", "query_input",
                         "query_na_checkbox", "query_button", "result_root"):
            if required not in self.selectors:
                raise ConfigError(f"selectors.{required} is required")
        self.timeouts = {
            "nav_ms": int(raw.get("timeouts", {}).get("nav_ms", 15000)),
            "save_ms": int(raw.get("timeouts", {}).get("save_ms", 20000)),
            "settle_ms": int(raw.get("timeouts", {}).get("settle_ms", 1500)),
        }
        self.max_submit_attempts = int(raw.get("max_submit_attempts", 1))
        if self.max_submit_attempts != 1:
            raise ConfigError(
                "max_submit_attempts must be 1: an unknown outcome is resolved by "
                "querying, never by submitting again"
            )
        self.tz = raw.get("timezone") or "UTC"
        self.headless = raw.get("headless", True)
        # Persistent profile directory -- this is where the already-signed-in
        # browser session lives.  The tool never performs a login itself.
        self.user_data_dir = raw.get("user_data_dir") or os.path.join(
            os.path.dirname(self.path), ".browser-profile"
        )
        self.state_dir = os.path.abspath(
            (raw.get("state_dir") or os.path.join(os.path.dirname(self.path), "state"))
        )

    def _req(self, name):
        if name not in self.raw:
            raise ConfigError(f"missing required config key: {name}")
        return self.raw[name]

    def target_fingerprint(self) -> str:
        """Stable identity of the target configuration a receipt was made against."""
        material = {
            "base_url": self.base_url,
            "entry_path": self.entry_path,
            "dataset": self.dataset,
            "record_id": self.record_id,
            "business_key": self.business_key,
            "index_field": self.index_field,
            "selectors": self.selectors,
        }
        blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def describe(self) -> dict:
        return {
            "base_url": self.base_url,
            "entry_path": self.entry_path,
            "query_path": self.query_path,
            "dataset": self.dataset,
            "record_id": self.record_id,
            "business_key": self.business_key,
            "index_field": self.index_field,
            "fingerprint": self.target_fingerprint(),
            "config_path": self.path,
        }


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        return Config(json.load(fh), path)


# --------------------------------------------------------------------------
# dataset + cell helpers
# --------------------------------------------------------------------------
def load_dataset(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("schema") != "tsibble-entry-dataset/v1":
        raise ConfigError(f"unexpected dataset schema: {doc.get('schema')!r}")
    return doc


def sig(cell_obj: dict) -> tuple:
    """A type-aware, order-independent comparison signature for one cell.

    Every part is JSON-serialisable (numbers become normalized decimal text),
    because signatures are also persisted inside receipts.
    """
    if not isinstance(cell_obj, dict):
        raise ValueError(f"cell must be an object, got {cell_obj!r}")
    t = cell_obj.get("t")
    if cell_obj.get("na") is True:
        return (t, "NA")
    v = cell_obj.get("v")
    if t == "integer":
        return (t, "NUM", str(int(str(v))))
    if t == "double":
        # Compare the canonical text that was typed and stored.  Both sides
        # originate from the same canonical string, so exact decimal equality
        # is the faithful test -- no float tolerance games.
        return (t, "NUM", str(Decimal(str(v)).normalize()))
    if t == "datetime":
        # Compare at microsecond resolution on the UTC instant, so the same
        # moment written with a different offset is still recognised, while
        # float printing noise can never split a match.
        if cell_obj.get("epoch") is not None:
            micros = int(round(float(cell_obj["epoch"]) * 1_000_000))
        else:
            micros = int(round(_parse_dt(str(v)).timestamp() * 1_000_000))
        return (t, "NUM", str(micros))
    if t == "date":
        return (t, "STR", str(v))
    if t == "logical":
        return (t, "BOOL", str(v).upper() == "TRUE")
    return (t, "STR", "" if v is None else str(v))


def _parse_dt(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def display(cell_obj: dict) -> str:
    if cell_obj.get("na"):
        return "NULL"
    v = cell_obj.get("v")
    if v == "":
        return "''"
    return str(v)


def record_key_values(record: dict, cfg: Config) -> dict:
    return {f: record["fields"][f] for f in cfg.business_key}


def key_signature(record: dict, cfg: Config) -> tuple:
    return tuple(sig(record["fields"][f]) for f in cfg.business_key)


def key_text(record: dict, cfg: Config) -> str:
    parts = []
    for name in cfg.business_key:
        c = record["fields"][name]
        parts.append(f"{name}={display(c)}")
    return ", ".join(parts)


def receipt_id(cfg: Config, record: dict) -> str:
    material = json.dumps(
        {
            "fingerprint": cfg.target_fingerprint(),
            "record_id": cfg.record_id,
            "key": [[f, list(sig(record["fields"][f]))] for f in cfg.business_key],
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def expected_fields(record: dict, cfg: Config) -> dict:
    return {name: record["fields"][name] for name in cfg.business_key + [
        f for f in record["fields"] if f not in cfg.business_key
    ]}


def format_for_input(cell_obj: dict) -> str:
    t = cell_obj.get("t")
    v = cell_obj.get("v")
    if cell_obj.get("na"):
        raise ValueError("NA cells must use the NA control, not text input")
    if t == "logical":
        return str(v).upper()
    return "" if v is None else str(v)


# --------------------------------------------------------------------------
# receipts (durable, atomic, journaled)
# --------------------------------------------------------------------------
class ReceiptStore:
    """One receipt file per (target, record key), plus a write-ahead journal.

    Layout (under cfg.state_dir):
        receipts/<receipt_id>.json     final receipt
        journal.jsonl                  append-only log of receipt writes
        attempts/<receipt_id>.jsonl    per-record attempt log
        recovery.jsonl                 remote-present / receipt-missing repairs
        checkpoint.json                last completed record ordinal
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = Path(cfg.state_dir)
        self.receipts_dir = self.root / "receipts"
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.root / "journal.jsonl"
        self.attempts_dir = self.root / "attempts"
        self.attempts_dir.mkdir(parents=True, exist_ok=True)
        self.recovery_path = self.root / "recovery.jsonl"
        self.checkpoint_path = self.root / "checkpoint.json"
        self._cache: dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self):
        for p in self.receipts_dir.glob("*.json"):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            key = self._identity(data.get("expected_key"), data.get("target", {}).get("fingerprint"))
            if key:
                self._cache[key] = data

    @staticmethod
    def _identity(expected_key, fingerprint):
        if not expected_key or not fingerprint:
            return None
        return hashlib.sha256(
            json.dumps({"k": expected_key, "f": fingerprint}, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _identity_for(self, record: dict) -> str:
        return self._identity(
            [[f, list(sig(record["fields"][f]))] for f in self.cfg.business_key],
            self.cfg.target_fingerprint(),
        )

    def get(self, record: dict) -> dict | None:
        rid = self._identity_for(record)
        hit = self._cache.get(rid)
        if hit is None:
            return None
        # A receipt only counts for this exact target and record identity; the
        # cached data already encodes both, but re-check defensively.
        if hit.get("target", {}).get("fingerprint") != self.cfg.target_fingerprint():
            return None
        return hit

    def put(self, receipt: dict) -> Path:
        rid = receipt["receipt_id"]
        final = self.receipts_dir / f"{rid}.json"
        tmp = self.receipts_dir / f".{rid}.tmp"
        receipt = dict(receipt)
        receipt["written_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        payload = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        _fsync_dir(self.receipts_dir)
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "receipt_written", "receipt_id": rid,
                                 "ordinal": receipt.get("ordinal"),
                                 "outcome": receipt.get("outcome"),
                                 "written_at": receipt["written_at"]}, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._cache[self._identity_for_by_receipt(receipt)] = receipt
        return final

    def _identity_for_by_receipt(self, receipt: dict) -> str:
        return self._identity(receipt["expected_key"], receipt["target"]["fingerprint"])

    def log_attempt(self, receipt_id_value: str, event: dict):
        path = self.attempts_dir / f"{receipt_id_value}.jsonl"
        event = dict(event)
        event.setdefault("at", dt.datetime.now(dt.timezone.utc).isoformat())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def log_recovery(self, event: dict):
        event = dict(event)
        event.setdefault("at", dt.datetime.now(dt.timezone.utc).isoformat())
        with open(self.recovery_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_checkpoint(self) -> dict:
        try:
            with open(self.checkpoint_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def write_checkpoint(self, data: dict):
        tmp = self.checkpoint_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.checkpoint_path)

    def receipt_count(self) -> int:
        return len(list(self.receipts_dir.glob("*.json")))


def _fsync_dir(path: Path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# browser session
# --------------------------------------------------------------------------
class Session:
    """A logged-in, serial browser session bound to one target configuration."""

    def __init__(self, cfg: Config, page, *, fault_queue: list | None = None):
        self.cfg = cfg
        self.page = page
        self.fault_queue = fault_queue or []

    # -- low level -------------------------------------------------------
    def url(self, path: str) -> str:
        return urllib.parse.urljoin(self.cfg.base_url + "/", path.lstrip("/"))

    def sel(self, name: str) -> str:
        return self.cfg.selectors[name].format(**{})

    def field_sel(self, name: str) -> str:
        template = self.cfg.selectors["field_input"]
        return template.replace("{field}", name)

    def na_sel(self, name: str) -> str:
        template = self.cfg.selectors["na_checkbox"]
        return template.replace("{field}", name)

    def query_input_sel(self, name: str) -> str:
        template = self.cfg.selectors["query_input"]
        return template.replace("{field}", name)

    def query_na_sel(self, name: str) -> str:
        template = self.cfg.selectors.get("query_na_checkbox")
        if not template:
            return "___no_query_na_control___"
        return template.replace("{field}", name)

    # -- actions ---------------------------------------------------------
    def ensure_signed_in(self):
        """Never automate authentication.  The browser profile is expected to
        already hold a live session; if the site bounces us to its login page
        we stop and report it rather than guessing credentials."""
        target = self.url(self.cfg.entry_path)
        self.page.goto(target, wait_until="domcontentloaded", timeout=self.cfg.timeouts["nav_ms"])
        try:
            self.page.wait_for_selector(self.sel("save_button"), timeout=5000)
        except PlaywrightTimeoutError:
            raise ConfigError(
                "the browser profile is not signed in to the target site "
                f"(no save form at {target}); sign in manually and re-run"
            )

    def open_entry(self, fault: str = ""):
        url = self.url(self.cfg.entry_path)
        if fault:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"fault": fault})
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.timeouts["nav_ms"])
        self.page.wait_for_selector(self.sel("save_button"), timeout=self.cfg.timeouts["nav_ms"])

    def fill_record(self, record: dict):
        """Type one record into the form.  NA is expressed through the page's
        own NA control so it stays distinct from an empty string."""
        for name, cell_obj in record["fields"].items():
            na_box = self.page.locator(self.na_sel(name))
            if na_box.count() == 0:
                raise ConfigError(
                    f"field {name!r} has no NA control matching {self.na_sel(name)!r}; "
                    "check selectors.na_checkbox and the target form"
                )
            if cell_obj.get("na"):
                if not na_box.is_checked():
                    na_box.check()
                # clear any text so the state is unambiguous
                self.page.fill(self.field_sel(name), "")
            else:
                if na_box.is_checked():
                    na_box.uncheck()
                self.page.fill(self.field_sel(name), format_for_input(cell_obj))

    def submit(self) -> dict:
        """Click save.  Returns a description of what happened, but never
        treats it as success -- verification always re-queries."""
        save = self.page.locator(self.sel("save_button"))
        info = {"clicked": False, "error": None, "settled": False}
        try:
            # `no_wait_after` keeps a slow response from being mistaken for a
            # navigation timeout; we decide by re-querying either way.
            save.click(no_wait_after=True, timeout=self.cfg.timeouts["save_ms"])
            info["clicked"] = True
            # Wait for *this* response to actually render -- either the save
            # result, or a validation block.  A missing response (timeout or
            # dropped connection) must not be treated as an answer.
            try:
                self.page.wait_for_function(
                    """() => !!document.querySelector('#errors')
                             || !!document.querySelector('#post-save-query')
                             || !!document.querySelector('#btn-requery')""",
                    timeout=self.cfg.timeouts["settle_ms"],
                )
                info["settled"] = True
            except PlaywrightTimeoutError:
                info["settled"] = False
            info["url"] = self.page.url
            info["body_text"] = self._safe_body_text()
        except PlaywrightTimeoutError as exc:
            info["error"] = f"click timeout: {exc}"
        except Exception as exc:  # lost connection, detached frame, ...
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    def _safe_body_text(self) -> str:
        try:
            return self.page.locator("body").inner_text(timeout=1500)
        except Exception:
            return ""

    def query(self, record: dict) -> dict:
        """Query the key on the page's query form and read the latest record.

        Returns {"state": "found"|"missing"|"error", "fields": {...}, "raw": ...}
        """
        self.page.goto(self.url(self.cfg.query_path), wait_until="domcontentloaded",
                       timeout=self.cfg.timeouts["nav_ms"])
        self.page.wait_for_selector(self.sel("query_button"), timeout=self.cfg.timeouts["nav_ms"])
        for name in self.cfg.business_key:
            cell_obj = record["fields"][name]
            box = self.page.locator(self.query_na_sel(name))
            if box.count() == 0:
                raise ConfigError(
                    f"no NA control for query field {name!r} "
                    f"(selectors.query_na_checkbox = {self.cfg.selectors.get('query_na_checkbox')!r})"
                )
            if cell_obj.get("na"):
                if not box.is_checked():
                    box.check()
                self.page.fill(self.query_input_sel(name), "")
            else:
                if box.is_checked():
                    box.uncheck()
                self.page.fill(self.query_input_sel(name), format_for_input(cell_obj))
        self.page.click(self.sel("query_button"), no_wait_after=True)
        try:
            self.page.wait_for_selector(self.sel("result_root"), timeout=self.cfg.timeouts["save_ms"])
        except PlaywrightTimeoutError:
            return {"state": "error", "error": "query result did not render"}
        return self._read_result()

    def _read_result(self) -> dict:
        root = self.page.locator(self.sel("result_root"))
        state = (root.get_attribute("data-state") or "").strip()
        if state == "missing":
            return {"state": "missing"}
        if state != "found":
            return {"state": "error", "error": f"unexpected result state {state!r}"}
        fields = {}
        dd = root.locator("[data-field]")
        count = dd.count()
        for i in range(count):
            el = dd.nth(i)
            name = el.get_attribute("data-field")
            ctype = el.get_attribute("data-type")
            is_na = el.get_attribute("data-na") == "true"
            value = el.get_attribute("data-value") or ""
            if is_na:
                fields[name] = {"t": ctype, "na": True}
            elif ctype in ("integer",):
                fields[name] = {"t": ctype, "v": value}
            elif ctype == "double":
                fields[name] = {"t": ctype, "v": value, "num": value}
            else:
                fields[name] = {"t": ctype, "v": value}
        return {"state": "found", "fields": fields, "version": root.get_attribute("data-version")}

    def validation_errors(self) -> list:
        out = []
        items = self.page.locator("#errors li")
        for i in range(items.count()):
            el = items.nth(i)
            out.append({"field": el.get_attribute("data-field"), "reason": el.inner_text().strip()})
        return out


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------
def compare_fields(expected: dict, actual: dict, cfg: Config) -> list[dict]:
    diffs = []
    for name, exp in expected.items():
        if name not in actual:
            diffs.append({"field": name, "expected": display(exp), "actual": "<absent>"})
            continue
        try:
            same = sig(exp) == sig(actual[name])
        except ValueError as exc:
            diffs.append({"field": name, "expected": display(exp), "actual": f"<unreadable: {exc}>"})
            continue
        if not same:
            diffs.append({"field": name, "expected": display(exp), "actual": display(actual[name])})
    for name in actual:
        if name not in expected:
            diffs.append({"field": name, "expected": "<absent>", "actual": display(actual[name])})
    return diffs


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------
class EntryEngine:
    def __init__(self, cfg: Config, session: Session, store: ReceiptStore, log=None):
        self.cfg = cfg
        self.session = session
        self.store = store
        self.log = log or (lambda *a, **k: None)

    def process(self, record: dict, ordinal: int) -> dict:
        result = {
            "ordinal": ordinal,
            "record_id": self.cfg.record_id,
            "key": key_text(record, self.cfg),
            "native_key": record_key_values(record, self.cfg),
            "index": record["fields"][self.cfg.index_field],
            "status": None,
            "receipt": None,
            "attempts": 0,
            "reason": None,
        }
        rid = receipt_id(self.cfg, record)

        # 0. already confirmed locally? Receipts are bound to target + key +
        #    expected values, so this is safe to trust without the network.
        hit = self.store.get(record)
        if hit is not None and hit.get("outcome") in (CREATED, UNCHANGED):
            if hit.get("expected_values") == expected_fields(record, self.cfg):
                result.update(status=UNCHANGED, receipt=hit["receipt_id"],
                              reason="receipt already confirms this exact record")
                self.log("skip-receipt", result)
                return result

        # 1. Always resolve current remote state first: this both detects
        #    conflicts before writing and recovers a lost receipt.
        before = self.session.query(record)
        self.store.log_attempt(rid, {"event": "precheck", "state": before.get("state")})
        if before.get("state") == "found":
            diffs = compare_fields(expected_fields(record, self.cfg), before["fields"], self.cfg)
            if not diffs:
                result.update(status=UNCHANGED, receipt=self._write_receipt(record, ordinal, UNCHANGED, before),
                              reason="remote already holds an identical record; no submit")
                self.log("skip-remote", result)
                return result
            result.update(status=CONFLICT, reason="remote holds different values for this key")
            result["diffs"] = diffs
            self.log("conflict", result)
            return result
        if before.get("state") == "error":
            result.update(status=ERROR, reason=f"could not query before writing: {before.get('error')}")
            self.log("precheck-error", result)
            return result

        # 2. Submit, then verify by re-query regardless of what the page said.
        fault = self.session.fault_queue.pop(0) if self.session.fault_queue else ""
        self.session.open_entry(fault)
        self.session.fill_record(record)
        click_info = self.session.submit()
        result["attempts"] += 1
        self.store.log_attempt(rid, {
            "event": "submit", "fault": fault, "clicked": click_info.get("clicked"),
            "error": click_info.get("error"),
        })
        # Only a *rendered* response can carry a validation verdict.  If the
        # page never answered, fall through to verification instead of
        # inventing a failure (or a success).
        page_errors = self.session.validation_errors() if click_info.get("settled") else []
        if page_errors:
            result.update(status=VALIDATION_FAILED, reason="; ".join(e["reason"] for e in page_errors))
            result["validation"] = page_errors
            self.log("validation-failed", result)
            return result

        verify = self._query_after_submit(record)
        self.store.log_attempt(rid, {"event": "verify", "state": verify.get("state")})

        if verify.get("state") == "found":
            diffs = compare_fields(expected_fields(record, self.cfg), verify["fields"], self.cfg)
            if not diffs:
                rid_written = self._write_receipt(record, ordinal, CREATED, verify)
                result.update(status=CREATED, receipt=rid_written)
                self.log("created", result)
                return result
            result.update(status=VERIFICATION_FAILED,
                          reason="remote holds different values right after the save; not overwritten")
            result["diffs"] = diffs
            self.log("verify-mismatch", result)
            return result

        if verify.get("state") == "missing":
            # The page may have said anything; the truth is "not stored".
            result.update(status=ERROR,
                          reason="save did not persist: key is still missing after re-query")
            self.log("not-persisted", result)
            return result

        # 3. Result genuinely unknown. Investigate again with backoff; never
        #    resubmit blindly.
        resolved = self._resolve_unknown(record)
        if resolved.get("state") == "found":
            diffs = compare_fields(expected_fields(record, self.cfg), resolved["fields"], self.cfg)
            if not diffs:
                rid_written = self._write_receipt(record, ordinal, CREATED, resolved)
                result.update(status=CREATED, receipt=rid_written,
                              reason="resolved an unknown result by re-query; no resubmit")
                self.log("created-after-unknown", result)
                return result
            result.update(status=CONFLICT, reason="a different record appeared for this key")
            result["diffs"] = diffs
            return result
        result.update(status=UNKNOWN_AFTER_RETRY,
                      reason=f"could not determine the outcome: {resolved.get('error')}")
        self.log("unresolved", result)
        return result

    def _query_after_submit(self, record: dict) -> dict:
        """A slow or dropped response can leave the context mid-navigation;
        recover and query, because the truth is only in the stored data."""
        try:
            return self.session.query(record)
        except Exception as exc:
            try:
                self.session.page.goto(self.session.url(self.cfg.entry_path),
                                       wait_until="domcontentloaded")
            except Exception:
                pass
            try:
                return self.session.query(record)
            except Exception as exc2:
                return {"state": "error", "error": f"{type(exc).__name__}: {exc}; retry: {exc2}"}

    def _resolve_unknown(self, record: dict) -> dict:
        last = {"state": "error", "error": "no query attempted"}
        for delay in (0.0, 1.0, 2.0, 4.0):
            if delay:
                time.sleep(delay)
            try:
                last = self.session.query(record)
            except Exception as exc:
                last = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
            if last.get("state") in ("found", "missing"):
                return last
        return last

    def _write_receipt(self, record: dict, ordinal: int, outcome: str, observed: dict) -> str:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "tool_version": TOOL_VERSION,
            "receipt_id": receipt_id(self.cfg, record),
            "record_id": self.cfg.record_id,
            "dataset": self.cfg.dataset,
            "ordinal": ordinal,
            "outcome": outcome,
            "native_key": record_key_values(record, self.cfg),
            "index": record["fields"][self.cfg.index_field],
            "expected_key": [[f, list(sig(record["fields"][f]))] for f in self.cfg.business_key],
            "expected_values": expected_fields(record, self.cfg),
            "observed_values": observed.get("fields"),
            "observed_version": observed.get("version"),
            "target": self.cfg.describe(),
        }
        self.store.put(receipt)
        return receipt["receipt_id"]


# --------------------------------------------------------------------------
# recovery: remote present, local receipt missing
# --------------------------------------------------------------------------
def pending_ordinals(cfg: Config, records: list[dict], store: ReceiptStore) -> list[int]:
    """Records that the checkpoint never confirmed and that have no receipt.

    Recovery only needs to look at those: a receipt already proves the record
    was confirmed, and anything past the checkpoint frontier is untouched.
    """
    ck = store.read_checkpoint()
    frontier = 0
    if ck.get("record_id") == cfg.record_id and ck.get("fingerprint") == cfg.target_fingerprint():
        frontier = int(ck.get("completed", 0) or 0)
    out = []
    for ordinal, record in enumerate(records, start=1):
        if ordinal <= frontier:
            continue
        if store.get(record) is None:
            out.append(ordinal)
    return out


def recover(cfg: Config, session: Session, records: list[dict], store: ReceiptStore, log,
            scanned: list[int] | None = None) -> dict:
    """Resolve records whose receipt is missing but whose data may be remote.

    A crash can happen after the site stored the record but before the receipt
    reached disk (or after the receipt was lost).  On restart, we query exactly
    those keys and, if the remote already matches, write the receipt without
    re-submitting.
    """
    recovered = []
    conflicts = []
    ordinals = scanned if scanned is not None else pending_ordinals(cfg, records, store)
    for ordinal in ordinals:
        record = records[ordinal - 1]
        if store.get(record) is not None:
            continue
        state = session.query(record)
        if state.get("state") != "found":
            continue
        diffs = compare_fields(expected_fields(record, cfg), state["fields"], cfg)
        if diffs:
            conflicts.append({"ordinal": ordinal, "key": key_text(record, cfg), "diffs": diffs})
            continue
        rid = receipt_id(cfg, record)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "tool_version": TOOL_VERSION,
            "receipt_id": rid,
            "record_id": cfg.record_id,
            "dataset": cfg.dataset,
            "ordinal": ordinal,
            "outcome": UNCHANGED,
            "recovered": True,
            "native_key": record_key_values(record, cfg),
            "index": record["fields"][cfg.index_field],
            "expected_key": [[f, list(sig(record["fields"][f]))] for f in cfg.business_key],
            "expected_values": expected_fields(record, cfg),
            "observed_values": state.get("fields"),
            "observed_version": state.get("version"),
            "target": cfg.describe(),
        }
        store.put(receipt)
        store.log_recovery({"event": "receipt_recovered", "receipt_id": rid, "ordinal": ordinal,
                            "key": key_text(record, cfg)})
        recovered.append({"ordinal": ordinal, "key": key_text(record, cfg)})
        log("recovered", {"ordinal": ordinal, "key": key_text(record, cfg)})
    return {"recovered": recovered, "conflicts": conflicts, "scanned": list(ordinals)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_summary(results: list[dict], cfg: Config) -> dict:
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    ok = [r for r in results if r["status"] in (CREATED, UNCHANGED)]
    bad = [r for r in results if r["status"] not in (CREATED, UNCHANGED)]
    return {
        "schema": "tsibble-entry-run/v1",
        "tool_version": TOOL_VERSION,
        "target": cfg.describe(),
        "counts": counts,
        "ok": len(ok),
        "not_ok": len(bad),
        "results": results,
    }


def run(config_path: str, dataset_path: str, *, limit: int = 0, resume: bool = True,
        recover_only: bool = False, headless: bool | None = None, out: str | None = None,
        user_data_dir: str | None = None, log=print) -> dict:
    cfg = load_config(config_path)
    if headless is not None:
        cfg.headless = headless
    doc = load_dataset(dataset_path)
    records = doc["records"]
    if limit:
        records = records[:limit]
    if not records:
        raise SystemExit("no records to enter")
    for r in records:
        missing = [f for f in cfg.business_key if f not in r["fields"]]
        if missing:
            raise ConfigError(f"dataset records lack configured key fields: {missing}")

    store = ReceiptStore(cfg)

    with sync_playwright() as p:
        # A persistent profile is what carries the already-signed-in session.
        # The tool never fills a login form or handles a captcha.
        profile = os.path.abspath(user_data_dir or cfg.user_data_dir)
        os.makedirs(profile, exist_ok=True)
        context = p.chromium.launch_persistent_context(
            profile,
            headless=cfg.headless,
            timezone_id=cfg.tz,
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        session = Session(cfg, page)
        session.ensure_signed_in()
        engine = EntryEngine(cfg, session, store, log=log)

        recovery = {"recovered": [], "conflicts": [], "scanned": []}
        if recover_only or resume:
            recovery = recover(cfg, session, records, store, log,
                               scanned=None if recover_only else pending_ordinals(cfg, records, store))

        results = []
        if not recover_only:
            start = 0
            if resume:
                ck = store.read_checkpoint()
                if ck.get("record_id") == cfg.record_id and ck.get("fingerprint") == cfg.target_fingerprint():
                    start = int(ck.get("completed", 0))
                    if start:
                        log("resume", {"from_ordinal": start + 1})
            for idx in range(start, len(records)):
                record = records[idx]
                ordinal = idx + 1
                result = engine.process(record, ordinal)
                results.append(result)
                if result["status"] in (CREATED, UNCHANGED):
                    store.write_checkpoint({
                        "record_id": cfg.record_id,
                        "fingerprint": cfg.target_fingerprint(),
                        "completed": ordinal,
                        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    })
                log("record", result)
        context.close()

    summary = build_summary(results, cfg)
    summary["recovery"] = recovery
    summary["receipts_on_disk"] = store.receipt_count()
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--recover-only", action="store_true")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    def log(kind, payload):
        if args.quiet:
            return
        if kind == "record":
            print(f"[{payload['ordinal']:>4}] {payload['status']:<18} {payload['key']}"
                  + (f"  ({payload['reason']})" if payload.get("reason") else ""))
        elif kind in ("recovered", "resume"):
            print(f"[{kind}] {payload}")

    summary = run(
        args.config, args.dataset, limit=args.limit,
        resume=not args.no_resume, recover_only=args.recover_only,
        headless=None if args.headful else True, out=args.out, log=log,
    )
    print(json.dumps({"counts": summary["counts"], "ok": summary["ok"],
                      "not_ok": summary["not_ok"],
                      "recovery": summary["recovery"]}, ensure_ascii=False, indent=2))
    return 0 if summary["not_ok"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
