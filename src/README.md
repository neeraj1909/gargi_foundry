# Writings Runtime Boundary

## Overview

The `writings` package implements the first executable review-run boundary:
admission, worker execution, retained events, and reconnect observation.

## Day 1 Flow

```text
ReviewRequest
    → SQLiteRunStore.admit()
    → AdmissionReceipt + Run + Notification
    → Worker.claim_next()
    → retained RunEvent records
    → RunObserver.reconnect(run_id)
```

The same request identity and payload are replayed idempotently. Reusing the
identity for different work raises an admission conflict.

## Modules

- `domain.py` contains immutable source, request, run, suggestion, result, and
  decision types with local invariant checks.
- `admission.py` defines deterministic request fingerprints, admission receipts,
  and the application admission boundary.
- `store.py` persists requests, runs, notifications, results, and retained
  events in SQLite transactions.
- `worker.py` claims admitted runs and owns the provider credential boundary.
- `observation.py` reads an existing run and its retained events after a
  reconnect without submitting a second request.

## Design Boundary

The implementation uses a provider protocol and does not call a real provider,
HTTP service, or user interface. Provider recovery after an unknown external
effect belongs to a later runtime-recovery slice.

Source identity remains based on exact UTF-8 text. The source is not
normalized, trimmed, or rewritten while a snapshot is created.
