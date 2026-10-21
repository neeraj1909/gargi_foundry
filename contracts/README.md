# Shared Writing Contract

The authoritative Day 2 contract is:

`contracts/writings/v1/writing.proto`

## Boundary

The contract contains:

- request identity
- owner identity
- text target identity
- optional expected revision
- review intent
- idempotency key

`expected_revision` is an optional `uint64`.

Its meaning is:

- field absent: use the current draft
- field present: bind the request to the supplied saved revision

An explicit value of `0` is different from an absent field.

## Wire representation

Protobuf JSON represents `uint64` values as decimal strings.

For example:

```json
{
  "target": {
    "documentId": "document-1",
    "expectedRevision": "42"
  }
}
