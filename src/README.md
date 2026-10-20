# Writings Domain

## Overview

The `writings` domain module defines the core data and state rules for the
author-controlled review workflow.

## Purpose

`domain.py` keeps the review contract independent from databases, model
providers, workers, and user interfaces. It preserves the identity of the
source text and gives the rest of the application typed objects to pass
between boundaries.

## Main Types

- `SourceSnapshot` stores the exact source text, document version, and
  SHA-256 digest.
- `ReviewRequest` records the owner, review intent, source snapshot, and
  idempotency key.
- `Run` and `RunState` represent the review lifecycle and valid state changes.
- `Suggestion` represents a possible source-grounded editorial suggestion.
- `ReviewResult` represents one or more suggestions, including no-change
  results.
- `DecisionResult` records the author's explicit accept or dismiss decision.

## Design Boundary

The module contains pure domain objects and local invariant checks. It does
not call providers, persist data, mutate source text, or render a UI.

Source identity is based on the exact UTF-8 text. The text is not normalized,
trimmed, or rewritten while the snapshot is created.

## Next Boundary

Publication-time checks such as exact quote/span matching, duplicate suggestion
IDs, allowed categories, and author-ownership checks belong in the guardrail
layer before a suggestion is published or accepted.
