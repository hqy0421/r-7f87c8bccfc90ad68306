# Safe entry of tsibble records into a keyed business web form

The business page has **no import interface**.  This tool drives a real
Chromium browser, fills the ordinary scalar form one record at a time, clicks
Save, and then **re-queries the key and compares every field** before it is
willing to call anything a success.

## What "safe" means here

| Situation | Behaviour |
|---|---|
| Key absent, values stored correctly | `created`, receipt written |
| Same key, identical values already remote | `skipped_unchanged` — no submit |
| Same key, different values remote | `conflict` — reported with a per-field diff, existing record **never overwritten** |
| Save page says "Saved" but nothing was stored | `error: save did not persist` — re-query proves it |
| Save committed but the record was altered | `verification_failed` with the diff |
| Save response lost or slow (timeout) | resolved **by querying**, never by re-submitting; `unresolved` if it cannot be determined |
| Page rejected the record | `validation_failed` — the page's own reason is kept |
| Remote stored but the local receipt was lost | on restart the receipt is **backfilled from a query**, without re-submitting |

Only `created` and `skipped_unchanged` are success.  Everything else is
reported with the correct ordinal/key and non-zero exit status.

Native types survive the round trip: `NA`, `""`, `0` and `FALSE` are four
different values, not one falsy blob.  Each cell travels as a type-tagged
object (`{"t": "double", "v": "0"}`, `{"t": "character", "na": true}`, …) and
comparison is done on type + canonical value, not on rendered text.

## Layout

```
entry-tool/
  R/export_dataset.R      tsibble -> type-tagged JSON (keys, index, types)
  R/export_lib.R
  dataset/*.json          exported records (tourism, pedestrian, small slices)
  entry.py                the driver: browser, verification, receipts, recovery
  config.example.json     target configuration template
  bizform/                a faithful local "no import API" form used for testing
    server.py             server-rendered form + query page + fault injection
    make_schema.py        derives the form schema from an exported dataset
    schemas/*.json
    signin.py             one-time human sign-in that seeds a browser profile
  tests/
    test_entry.py         18 end-to-end tests in a real browser
    run_tests.sh
    setup_browser.sh      rootless Playwright + Chromium + private sysroot
```

## 1. Export the records

```sh
Rscript entry-tool/R/export_dataset.R --dataset tourism --limit 5 \
    --out /tmp/tourism-5.json
```

The exporter refuses to run if the business key (tsibble key + index) is not
unique, and writes the key/index/type metadata the driver relies on.

## 2. Sign in once, by hand

The tool **never authenticates and never touches a captcha**.  It expects a
Chromium profile that already holds a live session:

* On a real target, open the profile once yourself — `entry.py --headful`
  uses the same `user_data_dir`, so you can sign in interactively in that
  window and then let the tool run.
* In this repo, `bizform/signin.py` does the same thing for the local form
  (it is a test helper, not part of the entry path):

```sh
python3 entry-tool/bizform/signin.py --url http://127.0.0.1:8765 \
    --profile /var/tmp/profile --username operator --password operator
```

If the profile is not signed in, `entry.py` stops with
"the browser profile is not signed in to the target site" instead of guessing
credentials.

## 3. Configure the target

Copy `entry-tool/config.example.json` and change it.  Every site-specific
piece is configuration; nothing about a particular company's form is baked
into the code:

```jsonc
{
  "base_url": "https://example.internal",
  "entry_path": "/records/new",
  "query_path": "/records/search",
  "record_id": "tourism",              // identity of this feed
  "dataset": "tourism",
  "key_fields": ["Region", "State", "Purpose"],
  "index_field": "Quarter",
  "business_key": ["Region", "State", "Purpose", "Quarter"],
  "timezone": "Australia/Melbourne",
  "selectors": {
    "field_input":       "#f-{field}",   // text input per field
    "na_checkbox":       "#na-{field}",  // "this value is NA"
    "save_button":       "#btn-save",
    "query_input":       "#q-{field}",   // query form key input
    "query_na_checkbox": "#qna-{field}",
    "query_button":      "#btn-query",
    "result_root":       "#result"       // element carrying the found record
  },
  "user_data_dir": "/var/tmp/profile",   // already-signed-in browser profile
  "state_dir":     "/var/tmp/receipts",  // durable receipts
  "timeouts": {"nav_ms": 15000, "save_ms": 20000, "settle_ms": 1500}
}
```

The query result must expose the stored record in a machine-readable way.
This implementation reads `data-field`, `data-type`, `data-na`, `data-value`
attributes from elements inside `result_root`, and `data-state="found|missing"`
on it.  A different site's result markup only requires adapting
`Session._read_result` (one function) — the safety logic is independent of it.

## 4. Run

```sh
python3 entry-tool/entry.py --config /var/tmp/config.json \
    --dataset /tmp/tourism-5.json --out /var/tmp/run.json
```

Records are processed **serially**, in order.  Re-running resumes from the
checkpoint; `--no-resume` re-examines everything (already-confirmed records
are still skipped, not re-entered).  `--recover-only` only repairs receipts.

Exit status is `0` only if every record ended `created` or
`skipped_unchanged`.

## Receipts and crash safety

Under `state_dir`:

| File | Purpose |
|---|---|
| `receipts/<id>.json` | one confirmed record: native key, index, expected values, observed values, full target description, outcome |
| `journal.jsonl` | append-only log of every receipt write |
| `attempts/<id>.jsonl` | per-record attempt log (precheck / submit / verify) |
| `recovery.jsonl` | receipts reconstructed from the remote site |
| `checkpoint.json` | last confirmed ordinal for this target |

A receipt is written with `tmp → fsync → os.replace → fsync(dir)`, so a crash
leaves either the old file or the new one, never a half-file.  On restart,
records after the checkpoint with no receipt are queried: if the remote
already holds them, the receipt is restored (`recovery.jsonl`) and **nothing is
submitted again**.  If the remote holds *different* values, that is reported as
a recovery conflict rather than silently accepted.

Receipts are bound to the target by a fingerprint over base URL, paths,
dataset, record id, business key, index and selectors — a receipt made against
one target can never authorise a skip against another.

## Tests

`bizform/server.py` is a real server-rendered form (session cookie, POST,
query-by-key, unique key, no JSON import).  It also injects the failures that
matter: lost acknowledgement, slow acknowledgement, slow response without
commit, a "Saved" page that stored nothing, and a save that altered the value.

```sh
entry-tool/tests/setup_browser.sh     # once, rootless
entry-tool/tests/run_tests.sh -v      # 18 tests, ~2.5 min
```

Each test gets its own server, database, browser profile and receipt
directory.  The tests drive the same `entry.py` engine the CLI uses.

## Important note about this environment

There is **no real business web page and no entry configuration for one in this
environment** — the sandbox has no route to such a host, and no config or
credential file was present anywhere on disk.  The pipeline was therefore
exercised end-to-end against `bizform/`, a deliberately faithful stand-in for
the described page ("no import API, keyed scalar form, query latest record by
key").  Pointing it at the real target is a matter of writing
`config.json` (plus, if the result markup differs, adjusting
`Session._read_result`) and running the same command; no records were entered
into any external system.
