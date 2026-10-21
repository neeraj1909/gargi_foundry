from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from writings.contracts import (
    MAX_UINT64,
    ContractError,
    OptionalRevision,
    ReviewRequestContract,
    TextTarget,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "day2_revision_presence.json"
)


def make_request(
    revision: OptionalRevision | None = None,
) -> ReviewRequestContract:
    return ReviewRequestContract(
        request_id="request-1",
        owner_id="owner-1",
        target=TextTarget(
            document_id="document-1",
            expected_revision=revision or OptionalRevision.absent(),
        ),
        intent="Review this text for clarity.",
        idempotency_key="idempotency-1",
    )


def test_absent_revision_means_current_draft() -> None:
    revision = OptionalRevision.absent()

    assert revision.present is False
    assert revision.uses_current_draft is True
    assert revision.binds_saved_revision is False
    assert revision.to_wire_dict() == {}


def test_explicit_zero_revision_is_present() -> None:
    revision = OptionalRevision.supplied(0)

    assert revision.present is True
    assert revision.value == 0
    assert revision.uses_current_draft is False
    assert revision.binds_saved_revision is True
    assert revision.to_wire_dict() == {"expectedRevision": "0"}


def test_maximum_uint64_revision_is_supported() -> None:
    revision = OptionalRevision.supplied(MAX_UINT64)

    assert revision.value == MAX_UINT64
    assert revision.to_wire_dict() == {
        "expectedRevision": "18446744073709551615"
    }


@pytest.mark.parametrize(
    "value",
    [
        -1,
        MAX_UINT64 + 1,
        True,
        1.5,
        "1",
    ],
)
def test_invalid_uint64_values_are_rejected(value: object) -> None:
    with pytest.raises(ContractError):
        OptionalRevision.supplied(value)  # type: ignore[arg-type]


def test_absent_and_explicit_zero_are_not_equal() -> None:
    assert OptionalRevision.absent() != OptionalRevision.supplied(0)


def test_request_uses_canonical_protobuf_json_field_names() -> None:
    request = make_request(OptionalRevision.supplied(42))

    payload = request.to_wire_dict()

    assert payload == {
        "requestId": "request-1",
        "ownerId": "owner-1",
        "target": {
            "documentId": "document-1",
            "expectedRevision": "42",
        },
        "intent": "Review this text for clarity.",
        "idempotencyKey": "idempotency-1",
    }


def test_omitted_revision_is_not_added_during_round_trip() -> None:
    request = make_request()

    restored = ReviewRequestContract.from_wire_dict(
        request.to_wire_dict()
    )

    assert restored == request
    assert "expectedRevision" not in restored.target.to_wire_dict()


def test_explicit_revision_survives_json_round_trip() -> None:
    request = make_request(OptionalRevision.supplied(42))

    restored = ReviewRequestContract.from_json(request.to_json())

    assert restored == request
    assert restored.target.expected_revision.present is True
    assert restored.target.expected_revision.value == 42


def test_json_output_is_deterministic() -> None:
    request = make_request(OptionalRevision.supplied(42))

    assert request.to_json() == (
        '{"idempotencyKey":"idempotency-1",'
        '"intent":"Review this text for clarity.",'
        '"ownerId":"owner-1",'
        '"requestId":"request-1",'
        '"target":{"documentId":"document-1",'
        '"expectedRevision":"42"}}'
    )


@pytest.mark.parametrize(
    "value",
    [
        0,
        None,
        True,
        1.5,
        "",
        " 1",
        "1 ",
        "-1",
        "18446744073709551616",
        "not-a-number",
    ],
)
def test_invalid_wire_revisions_are_rejected(value: object) -> None:
    payload = make_request().to_wire_dict()
    payload["target"]["expectedRevision"] = value

    with pytest.raises(ContractError):
        ReviewRequestContract.from_wire_dict(payload)


def test_unknown_wire_fields_are_rejected() -> None:
    payload = make_request().to_wire_dict()
    payload["target"]["unexpected"] = "value"

    with pytest.raises(ContractError, match="unknown fields"):
        ReviewRequestContract.from_wire_dict(payload)


def test_required_wire_fields_are_rejected_when_missing() -> None:
    payload = make_request().to_wire_dict()
    del payload["target"]

    with pytest.raises(ContractError, match="missing fields"):
        ReviewRequestContract.from_wire_dict(payload)


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(ContractError, match="valid JSON"):
        ReviewRequestContract.from_json("{not-json")


def test_revision_presence_fixtures_preserve_meaning() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    omitted = ReviewRequestContract.from_wire_dict(
        fixtures["omitted_revision"]
    )
    explicit_zero = ReviewRequestContract.from_wire_dict(
        fixtures["explicit_zero_revision"]
    )
    maximum = ReviewRequestContract.from_wire_dict(
        fixtures["maximum_revision"]
    )

    assert omitted.target.expected_revision == OptionalRevision.absent()
    assert explicit_zero.target.expected_revision == OptionalRevision.supplied(
        0
    )
    assert maximum.target.expected_revision == OptionalRevision.supplied(
        MAX_UINT64
    )
