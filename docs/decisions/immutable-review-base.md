# Immutable Review Base

## Decision

A review run keeps the exact `SourceSnapshot` admitted with its request.

The review consumer reads that frozen source through `FrozenReviewBase`.
Later document versions do not replace or mutate the source already bound to
the run.

## Current boundary

The repository currently provides:

- exact source text
- source version
- UTF-8 SHA-256 digest
- immutable `SourceSnapshot`
- persisted review requests
- reconnect-safe run reads
- explicit `FrozenReviewBase` access

The persisted request remains the source of truth for the review base.

## CRDT decision

Do not add CRDT machinery to this slice.

The current repository does not expose a concurrent document-editing service,
multi-writer merge protocol, or tested CRDT requirement. Adding a CRDT now
would introduce infrastructure without an observable behavior to validate.

A versioned immutable source is sufficient for the current review boundary.

## Consequences

A review can continue reading the exact source it originally received even
after a later document version exists.

Concurrent editing and review snapshotting remain separate concerns:

- concurrent editing may require a mergeable representation
- review consumers require a stable source boundary
- a CRDT merge must not silently rewrite an existing review base

## Revisit conditions

Reconsider this decision only when the repository has:

- more than one concurrent document writer
- a concrete merge operation
- a conflict model
- tests proving the required merge behavior
- a clear relationship between merged heads and frozen review revisions
