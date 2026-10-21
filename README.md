# Gargi Foundry

The repository contains the first executable runtime boundary for the
`writings` review workflow. It models review requests and runs, admits work
idempotently into SQLite, executes work through a provider boundary, retains
run events, and supports reconnecting to an existing run.

## Overview

The current implementation is deliberately focused on the runtime boundary.
It keeps the review contract independent from a real model provider, HTTP
service, database server, or user interface.

The main flow is:

```text
ReviewRequest
    -> SQLiteRunStore.admit()
    -> AdmissionReceipt + admitted Run + notification
    -> Worker.run_once()
    -> retained RunEvent records
    -> RunObserver.reconnect(run_id)
```

Admission is idempotent for the same request identity and payload. Reusing a
request identity or owner/idempotency key for different work raises an
admission conflict.

## Current capabilities

- Preserve exact source text and identify it with a SHA-256 digest.
- Represent requests, runs, suggestions, review results, and author decisions
  as typed immutable domain objects.
- Enforce valid run-state transitions and provider-attempt counting.
- Admit requests transactionally into SQLite.
- Replay an earlier admission instead of creating duplicate work.
- Persist worker notifications, run results, and ordered retained events.
- Execute a run through a provider protocol without exposing the provider
  credential to the domain model.
- Observe or reconnect to an existing run using its retained events.

## Repository structure

```text
.
├── src/
│   └── writings/
│       ├── admission.py       # Admission boundary and request fingerprints
│       ├── domain.py          # Immutable domain objects and invariants
│       ├── observation.py     # Run observation and reconnect reads
│       ├── store.py           # SQLite persistence and retained events
│       └── worker.py          # Provider boundary and worker execution
├── tests/
│   ├── test_admission_observation.py
│   └── test_domain.py
├── requirements.txt           # Test and lint tooling
└── src/README.md              # Runtime-boundary notes
```

## Requirements

- Python 3.11 or newer.
- A virtual environment is recommended.

The application code uses Python’s standard library. `requirements.txt`
contains the development tools used by the repository: `pytest` and `ruff`.

## Getting started

```bash
git clone <repository-url>
cd gargi-foundry

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export PYTHONPATH=src
```

There is currently no command-line entry point. The runtime is used as Python
modules; the following example admits one request, runs it with a small local
provider, and reconnects to the retained run history:

```python
from writings.domain import ReviewRequest, ReviewResult, SourceSnapshot
from writings.observation import RunObserver
from writings.store import SQLiteRunStore
from writings.worker import Worker


class NoChangeProvider:
    def review(self, run, *, credential: str) -> ReviewResult:
        return ReviewResult(run_id=run.run_id)


request = ReviewRequest(
    request_id="request-1",
    owner_id="owner-1",
    source=SourceSnapshot.create(
        document_id="document-1",
        version=1,
        text="A short draft.",
    ),
    intent="Review this text for clarity.",
    idempotency_key="idempotency-1",
)

with SQLiteRunStore(":memory:") as store:
    receipt = store.admit(request)
    Worker(
        store,
        NoChangeProvider(),
        credential="local-development-credential",
    ).run_once()

    observation = RunObserver(store).reconnect(receipt.run_id)
    print(observation.run.state)
    print([event.event_type for event in observation.events])
```

## Testing and validation

Run the test suite with:

```bash
PYTHONPATH=src python -m pytest -q
```

Run the formatter and linter with:

```bash
python -m ruff format --check .
python -m ruff check .
```

The tests cover exact source hashing, span validation, domain serialization,
valid and invalid state transitions, idempotent admission, SQLite reopen
behavior, worker success and failure paths, notification claiming, and
reconnect observation.

## Design boundaries and limitations

- `domain.py` contains pure domain objects and local invariant checks.
- `store.py` owns SQLite persistence and transaction boundaries.
- `worker.py` owns the provider protocol and provider credential boundary.
- The provider is supplied by the caller; no real model provider is included.
- There is no HTTP API, UI, background process, or deployment configuration in
  the current repository.
- Recovery after an unknown external provider effect is outside this runtime
  slice and remains a later concern.

## Development notes

When extending the runtime, keep new behavior at the narrowest appropriate
boundary:

1. Put invariants and value transitions in `domain.py`.
2. Put durable state changes and event ordering in `store.py`.
3. Keep provider credentials and provider calls in `worker.py`.
4. Use retained events through `observation.py` for reconnect behavior.
5. Add or update tests for each observable state transition and failure path.

## License

No license has been declared in this repository yet.
