#!/usr/bin/env python3
"""End-to-end tests for the tsibble -> business form entry pipeline.

These tests run a real Chromium against a real server-rendered form.  Nothing
about the browser path is mocked: values are typed into inputs, buttons are
clicked, and results are read back from rendered HTML.

Prerequisites (the harness checks and reports clearly if missing):
    python3 -m pip install --target /var/tmp/automation/lib playwright
    PLAYWRIGHT_BROWSERS_PATH=... python3 -m playwright install chromium
plus the OS libraries Chromium needs, which this repo stages with
entry-tool/tests/setup_browser.sh.

Run:
    PYTHONPATH=/var/tmp/automation/lib python3 entry-tool/tests/test_entry.py -v
or, in this sandbox:
    entry-tool/tests/run_tests.sh
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
REPO = os.path.dirname(TOOL)
sys.path.insert(0, TOOL)

import entry  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    """Server + signed-in persistent profile + config, isolated per test."""

    def __init__(self, records: list[dict], *, key_fields: list[str],
                 index_field: str, business_key: list[str] | None = None,
                 schema_title: str = "Test records"):
        self.tmp = tempfile.mkdtemp(prefix="entry-test-")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.dataset_path = os.path.join(self.tmp, "dataset.json")
        fields = []
        seen = set()
        for r in records:
            for name, cell in r["fields"].items():
                if name in seen:
                    continue
                seen.add(name)
                fields.append({"name": name, "type": cell["t"]})
        bkey = business_key or key_fields + [index_field]
        dataset = {
            "schema": "tsibble-entry-dataset/v1",
            "dataset": {"name": "test", "object": "test", "key_fields": key_fields,
                        "index_field": index_field, "business_key": bkey,
                        "fields": fields, "record_count": len(records)},
            "records": records,
        }
        with open(self.dataset_path, "w", encoding="utf-8") as fh:
            json.dump(dataset, fh)

        schema = {
            "title": schema_title,
            "dataset": "test",
            "key_fields": bkey,
            "index_field": index_field,
            "fields": [{"name": f["name"], "type": f["type"],
                        "required": f["name"] in bkey} for f in dataset["dataset"]["fields"]],
            "login": {"username": "operator", "password": "operator"},
        }
        self.schema_path = os.path.join(self.tmp, "schema.json")
        with open(self.schema_path, "w", encoding="utf-8") as fh:
            json.dump(schema, fh)
        self.db_path = os.path.join(self.tmp, "records.sqlite")
        self.profile = os.path.join(self.tmp, "profile")
        self.state_dir = os.path.join(self.tmp, "state")
        self.server = subprocess.Popen(
            [sys.executable, os.path.join(TOOL, "bizform", "server.py"),
             "--schema", self.schema_path, "--db", self.db_path,
             "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "BIZFORM_QUIET": "1"},
        )
        self._wait_ready()
        self._sign_in()
        self.config_path = os.path.join(self.tmp, "config.json")
        cfg = {
            "base_url": self.base,
            "record_id": "test",
            "dataset": "test",
            "key_fields": key_fields,
            "index_field": index_field,
            "business_key": bkey,
            "selectors": {
                "field_input": "#f-{field}",
                "na_checkbox": "#na-{field}",
                "save_button": "#btn-save",
                "query_input": "#q-{field}",
                "query_na_checkbox": "#qna-{field}",
                "query_button": "#btn-query",
                "result_root": "#result",
            },
            "user_data_dir": self.profile,
            "state_dir": self.state_dir,
            "timezone": "UTC",
            "headless": True,
            "timeouts": {"nav_ms": 8000, "save_ms": 8000, "settle_ms": 1200},
        }
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def _wait_ready(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + "/healthz", timeout=0.5) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("bizform server did not start")

    def _sign_in(self):
        subprocess.run(
            [sys.executable, os.path.join(TOOL, "bizform", "signin.py"),
             "--url", self.base, "--profile", self.profile,
             "--username", "operator", "--password", "operator"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def stop(self):
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def rows(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT fields_json FROM records ORDER BY created_at, key_json")]
        finally:
            conn.close()

    def submit_events(self):
        """Every recorded submit attempt, across all per-record attempt logs."""
        out = []
        d = os.path.join(self.state_dir, "attempts")
        for name in sorted(os.listdir(d)):
            with open(os.path.join(d, name)) as fh:
                for line in fh:
                    if line.strip():
                        ev = json.loads(line)
                        if ev.get("event") == "submit":
                            out.append(ev)
        return out

    def run_engine(self, *, fault_plan: dict[int, str] | None = None,
                   cfg: entry.Config | None = None, ordinals=None):
        """Drive the engine directly so per-record faults can be scripted."""
        cfg = cfg or entry.load_config(self.config_path)
        doc = entry.load_dataset(self.dataset_path)
        records = doc["records"]
        if ordinals is not None:
            records = [records[i - 1] for i in ordinals]
        store = entry.ReceiptStore(cfg)
        plan = {i - 1: f for i, f in (fault_plan or {}).items()}
        results = []
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                cfg.user_data_dir, headless=True, timezone_id=cfg.tz,
                viewport={"width": 1200, "height": 900},
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            session = entry.Session(
                cfg, page,
                fault_queue=[plan.get(i, "") for i in range(len(records))],
            )
            session.ensure_signed_in()
            engine = entry.EntryEngine(cfg, session, store)
            for i, rec in enumerate(records, start=1):
                ordinal = ordinals[i - 1] if ordinals else i
                result = engine.process(rec, ordinal)
                results.append(result)
                if result["status"] in (entry.CREATED, entry.UNCHANGED):
                    store.write_checkpoint({
                        "record_id": cfg.record_id,
                        "fingerprint": cfg.target_fingerprint(),
                        "completed": ordinal,
                    })
            ctx.close()
        return results, store


def char(name, value):
    return {"t": "character", "v": value}


def num(name, value):
    return {"t": "double", "v": value}


def qtr(value):
    return {"t": "yearquarter", "v": value}


def rec(region, quarter, trips, **extra):
    fields = {"Region": char("Region", region), "Quarter": qtr(quarter),
              "Trips": num("Trips", trips)}
    fields.update(extra)
    return {"ordinal": 0, "fields": fields}


class EntryTests(unittest.TestCase):
    def make(self, records, **kw):
        h = Harness(records, key_fields=kw.pop("key_fields", ["Region"]),
                    index_field=kw.pop("index_field", "Quarter"), **kw)
        self.addCleanup(h.stop)
        return h

    # -- happy path + idempotence ---------------------------------------
    def test_create_then_skip_identical(self):
        h = self.make([rec("A", "1998 Q1", "1.5"), rec("A", "1998 Q2", "2.5")])
        results, store = h.run_engine()
        self.assertEqual([r["status"] for r in results], [entry.CREATED, entry.CREATED])
        self.assertEqual(store.receipt_count(), 2)
        self.assertEqual(len(h.rows()), 2)

        results2, _ = h.run_engine()
        self.assertEqual([r["status"] for r in results2],
                         [entry.UNCHANGED, entry.UNCHANGED])
        self.assertEqual(len(h.rows()), 2, "identical records must not be duplicated")

    def test_receipts_bind_native_key_index_and_target(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        h.run_engine()
        files = [f for f in os.listdir(os.path.join(h.state_dir, "receipts"))
                 if f.endswith(".json")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(h.state_dir, "receipts", files[0])) as fh:
            receipt = json.load(fh)
        self.assertEqual(receipt["schema"], entry.RECEIPT_SCHEMA)
        self.assertEqual(receipt["native_key"]["Region"], {"t": "character", "v": "A"})
        self.assertEqual(receipt["native_key"]["Quarter"], {"t": "yearquarter", "v": "1998 Q1"})
        self.assertEqual(receipt["index"], {"t": "yearquarter", "v": "1998 Q1"})
        self.assertEqual(receipt["expected_values"]["Trips"], {"t": "double", "v": "1.5"})
        self.assertEqual(receipt["target"]["fingerprint"],
                         entry.load_config(h.config_path).target_fingerprint())
        self.assertEqual(receipt["outcome"], entry.CREATED)
        # journal mirrors the receipt durably
        with open(os.path.join(h.state_dir, "journal.jsonl")) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(any(e["event"] == "receipt_written" and
                            e["receipt_id"] == receipt["receipt_id"] for e in events))

    # -- conflict --------------------------------------------------------
    def test_conflict_is_reported_and_not_overwritten(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        h.run_engine()
        before = h.rows()
        # same key, different value
        with open(h.dataset_path) as fh:
            doc = json.load(fh)
        doc["records"] = [rec("A", "1998 Q1", "9.25")]
        with open(h.dataset_path, "w") as fh:
            json.dump(doc, fh)
        results, store = h.run_engine()
        self.assertEqual(results[0]["status"], entry.CONFLICT)
        self.assertEqual(results[0]["diffs"][0]["field"], "Trips")
        self.assertEqual(results[0]["diffs"][0]["expected"], "9.25")
        self.assertEqual(results[0]["diffs"][0]["actual"], "1.5")
        self.assertEqual(h.rows(), before, "existing data must never be overwritten")
        self.assertEqual(store.receipt_count(), 1)

    # -- type fidelity ---------------------------------------------------
    def test_na_empty_zero_false_are_distinct(self):
        fields = {
            "Region": char("Region", "A"),
            "Quarter": qtr("1998 Q1"),
            "Trips": num("Trips", "0"),
            "Note": {"t": "character", "v": ""},
            "Missing": {"t": "character", "na": True},
            "Flag": {"t": "logical", "v": "FALSE"},
        }
        h = self.make([{"ordinal": 0, "fields": fields}])
        results, _ = h.run_engine()
        self.assertEqual(results[0]["status"], entry.CREATED)
        stored = h.rows()[0]
        self.assertEqual({k: stored["Trips"][k] for k in ("t", "v")},
                         {"t": "double", "v": "0"})
        self.assertEqual(stored["Note"], {"t": "character", "v": ""})
        self.assertEqual(stored["Missing"], {"t": "character", "na": True})
        self.assertEqual(stored["Flag"], {"t": "logical", "v": "FALSE"})
        # a second run still sees them as identical (no false conflicts)
        results2, _ = h.run_engine()
        self.assertEqual(results2[0]["status"], entry.UNCHANGED)

    # -- deceptive / lost acknowledgements --------------------------------
    def test_success_page_without_persist_is_not_success(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        results, store = h.run_engine(fault_plan={1: "ack_no_commit"})
        self.assertEqual(results[0]["status"], entry.ERROR)
        self.assertIn("still missing", results[0]["reason"])
        self.assertEqual(store.receipt_count(), 0)
        self.assertEqual(h.rows(), [])

    def test_altered_record_after_save_is_caught(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        results, store = h.run_engine(fault_plan={1: "ack_altered"})
        self.assertEqual(results[0]["status"], entry.VERIFICATION_FAILED)
        self.assertEqual(results[0]["diffs"][0]["actual"], "999999")
        self.assertEqual(store.receipt_count(), 0)

    def test_lost_ack_is_resolved_without_resubmit(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        results, store = h.run_engine(fault_plan={1: "lost_ack"})
        self.assertEqual(results[0]["status"], entry.CREATED)
        self.assertEqual(len(h.submit_events()), 1, "the lost ack must not be resubmitted")
        self.assertEqual(len(h.rows()), 1, "must not be entered twice")
        self.assertEqual(store.receipt_count(), 1)

    def test_timeout_after_commit_is_resolved(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        cfg = entry.load_config(h.config_path)
        cfg.timeouts["save_ms"] = 3000
        cfg.timeouts["settle_ms"] = 800
        results, store = h.run_engine(fault_plan={1: "slow_ack"}, cfg=cfg)
        self.assertIn(results[0]["status"], (entry.CREATED, entry.UNKNOWN_AFTER_RETRY))
        if results[0]["status"] == entry.CREATED:
            self.assertEqual(len(h.rows()), 1)
            self.assertEqual(store.receipt_count(), 1)

    def test_timeout_without_commit_is_not_success(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        cfg = entry.load_config(h.config_path)
        cfg.timeouts["save_ms"] = 3000
        cfg.timeouts["settle_ms"] = 600
        results, store = h.run_engine(fault_plan={1: "slow_no_commit"}, cfg=cfg)
        self.assertIn(results[0]["status"], (entry.ERROR, entry.UNKNOWN_AFTER_RETRY))
        self.assertEqual(h.rows(), [])
        self.assertEqual(store.receipt_count(), 0)

    # -- validation -------------------------------------------------------
    def test_validation_failure_keeps_reason(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        results, store = h.run_engine(fault_plan={1: "force_validation"})
        self.assertEqual(results[0]["status"], entry.VALIDATION_FAILED)
        self.assertIn("rejected", results[0]["reason"])
        self.assertEqual(results[0]["validation"][0]["field"], "Trips")
        self.assertEqual(h.rows(), [])
        self.assertEqual(store.receipt_count(), 0)

    def test_client_side_validation_blocks_bad_value(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        bad = rec("A", "1998 Q1", "not-a-number")
        with open(h.dataset_path) as fh:
            doc = json.load(fh)
        doc["records"] = [bad]
        with open(h.dataset_path, "w") as fh:
            json.dump(doc, fh)
        results, _ = h.run_engine()
        self.assertEqual(results[0]["status"], entry.VALIDATION_FAILED)
        self.assertIn("Trips", results[0]["reason"])
        self.assertEqual(h.rows(), [])

    # -- crash recovery ---------------------------------------------------
    def test_lost_receipt_is_recovered_without_resubmitting(self):
        h = self.make([rec("A", "1998 Q1", "1.5"), rec("A", "1998 Q2", "2.5")])
        h.run_engine()
        receipts = os.path.join(h.state_dir, "receipts")
        victim = sorted(os.listdir(receipts))[0]
        os.remove(os.path.join(receipts, victim))
        # also forget the checkpoint frontier so recovery must inspect the record
        os.remove(os.path.join(h.state_dir, "checkpoint.json"))
        cfg = entry.load_config(h.config_path)
        store = entry.ReceiptStore(cfg)
        records = entry.load_dataset(h.dataset_path)["records"]
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                cfg.user_data_dir, headless=True, timezone_id=cfg.tz,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            session = entry.Session(cfg, page)
            session.ensure_signed_in()
            result = entry.recover(cfg, session, records, store, lambda *a: None)
            ctx.close()
        self.assertEqual(len(result["recovered"]), 1)
        self.assertEqual(store.receipt_count(), 2)
        with open(os.path.join(h.state_dir, "recovery.jsonl")) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(events[-1]["event"], "receipt_recovered")
        self.assertEqual(len(h.rows()), 2, "recovery must not duplicate the record")

    def test_recovery_flags_remote_divergence(self):
        h = self.make([rec("A", "1998 Q1", "1.5")])
        h.run_engine()
        os.remove(os.path.join(h.state_dir, "checkpoint.json"))
        for f in os.listdir(os.path.join(h.state_dir, "receipts")):
            os.remove(os.path.join(h.state_dir, "receipts", f))
        # someone changed the record at the site behind our back
        import sqlite3
        conn = sqlite3.connect(h.db_path)
        row = conn.execute("SELECT key_json, fields_json FROM records").fetchone()
        fields = json.loads(row[1])
        fields["Trips"] = {"t": "double", "v": "7.5"}
        conn.execute("UPDATE records SET fields_json=? WHERE key_json=?",
                     (json.dumps(fields), row[0]))
        conn.commit()
        conn.close()
        cfg = entry.load_config(h.config_path)
        store = entry.ReceiptStore(cfg)
        records = entry.load_dataset(h.dataset_path)["records"]
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                cfg.user_data_dir, headless=True, timezone_id=cfg.tz,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            session = entry.Session(cfg, page)
            session.ensure_signed_in()
            result = entry.recover(cfg, session, records, store, lambda *a: None)
            ctx.close()
        self.assertEqual(len(result["recovered"]), 0)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["diffs"][0]["field"], "Trips")

    # -- dataset export fidelity -----------------------------------------
    def test_datetime_index_roundtrip(self):
        def prec(sensor, stamp, count):
            return {"ordinal": 0, "fields": {
                "Sensor": char("Sensor", sensor),
                "Date_Time": {"t": "datetime", "v": stamp,
                              "epoch": None, "tz": stamp[-6:]},
                "Count": {"t": "integer", "v": str(count)},
            }}

        h = self.make([prec("Birrarung Marr", "2015-01-01T00:00:00+11:00", 1630),
                       prec("Birrarung Marr", "2015-01-01T01:00:00+11:00", 826)],
                      key_fields=["Sensor"], index_field="Date_Time")
        results, _ = h.run_engine()   # epoch absent -> parsed from ISO text
        self.assertEqual([r["status"] for r in results], [entry.CREATED, entry.CREATED])
        stored = h.rows()
        self.assertEqual(stored[0]["Count"], {"t": "integer", "v": "1630"})
        self.assertEqual(stored[0]["Date_Time"]["v"], "2015-01-01T00:00:00+11:00")
        self.assertNotEqual(results[0]["key"], results[1]["key"])
        results2, _ = h.run_engine()
        self.assertEqual([r["status"] for r in results2], [entry.UNCHANGED, entry.UNCHANGED])

    def test_export_roundtrip_from_tsibble(self):
        rscript = shutil.which("Rscript")
        if not rscript:
            self.skipTest("Rscript not available")
        out = os.path.join(tempfile.mkdtemp(prefix="entry-export-"), "tourism.json")
        subprocess.run(
            [rscript, os.path.join(TOOL, "R", "export_dataset.R"),
             "--dataset", "tourism", "--limit", "2", "--out", out],
            check=True, cwd=REPO, stdout=subprocess.DEVNULL,
        )
        doc = json.load(open(out))
        self.assertEqual(doc["schema"], "tsibble-entry-dataset/v1")
        self.assertEqual(doc["dataset"]["business_key"],
                         ["Region", "State", "Purpose", "Quarter"])
        first = doc["records"][0]["fields"]
        self.assertEqual(first["Quarter"], {"t": "yearquarter", "v": "1998 Q1"})
        self.assertEqual(first["Trips"]["t"], "double")
        self.assertEqual(doc["dataset"]["record_count"], 2)


class ConfigTests(unittest.TestCase):
    def test_missing_selector_is_rejected(self):
        import tempfile
        raw = {
            "base_url": "http://x", "record_id": "r", "dataset": "d",
            "key_fields": ["k"], "index_field": "i",
            "selectors": {"field_input": "#f-{field}"},
        }
        path = os.path.join(tempfile.mkdtemp(), "c.json")
        with open(path, "w") as fh:
            json.dump(raw, fh)
        with self.assertRaises(entry.ConfigError):
            entry.load_config(path)

    def test_retry_resubmission_is_rejected(self):
        import tempfile
        raw = {
            "base_url": "http://x", "record_id": "r", "dataset": "d",
            "key_fields": ["k"], "index_field": "i", "max_submit_attempts": 3,
            "selectors": {k: "x" for k in (
                "field_input", "na_checkbox", "save_button", "query_input",
                "query_na_checkbox", "query_button", "result_root")},
        }
        path = os.path.join(tempfile.mkdtemp(), "c.json")
        with open(path, "w") as fh:
            json.dump(raw, fh)
        with self.assertRaises(entry.ConfigError):
            entry.load_config(path)

    def test_fingerprint_changes_with_target(self):
        import tempfile
        base = {
            "base_url": "http://x", "record_id": "r", "dataset": "d",
            "key_fields": ["k"], "index_field": "i",
            "selectors": {k: "x" for k in (
                "field_input", "na_checkbox", "save_button", "query_input",
                "query_na_checkbox", "query_button", "result_root")},
        }
        p1 = os.path.join(tempfile.mkdtemp(), "c.json")
        p2 = os.path.join(tempfile.mkdtemp(), "c.json")
        json.dump(base, open(p1, "w"))
        other = dict(base)
        other["base_url"] = "http://y"
        json.dump(other, open(p2, "w"))
        self.assertNotEqual(entry.load_config(p1).target_fingerprint(),
                            entry.load_config(p2).target_fingerprint())


if __name__ == "__main__":
    unittest.main(verbosity=2)
