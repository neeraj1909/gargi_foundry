from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .domain import ReviewRequest


class AdmissionConflict(RuntimeError):
    """Raised when an admission identity is reused for different work."""


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    """Durable proof that a request was admitted."""

    request_id: str
    run_id: str
    idempotency_key: str
    payload_digest: str
    state: str
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "payload_digest": self.payload_digest,
            "state": self.state,
            "replayed": self.replayed,
        }


class AdmissionPort(Protocol):
    def admit(self, request: ReviewRequest) -> AdmissionReceipt:
        """Admit a request or return its previous admission."""


class AdmissionService:
    """Application-facing admission boundary."""

    def __init__(self, store: AdmissionPort) -> None:
        self._store = store

    def admit(self, request: ReviewRequest) -> AdmissionReceipt:
        return self._store.admit(request)


def canonical_request_json(request: ReviewRequest) -> str:
    """Serialize a request deterministically without changing source text"""
    if not isinstance(request, ReviewRequest):
        raise TypeError("request must be a ReviewRequest")

    return json.dumps(
        request.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def request_payload_digest(request: ReviewRequest) -> str:
    """Return the digest used to detect idempotency conflicts."""

    payload = canonical_request_json(request).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
