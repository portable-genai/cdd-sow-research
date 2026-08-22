"""Shared fixed-window enforcement for the multi-replica Mode 5 broker."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from ...api.embed import RateLimitExceeded
from ...config import Settings


class FirestoreFixedWindowRateLimiter:
    """Atomically enforce one broker limit across every serving replica."""

    def __init__(
        self,
        settings: Settings,
        *,
        max_attempts: int = 20,
        window_seconds: int = 60,
    ) -> None:
        if max_attempts < 1 or window_seconds < 1:
            raise ValueError("rate-limit values must be positive")
        self._project = settings.project_id
        self._database = settings.browser_flow_store.database
        self._collection_name = settings.browser_flow_store.rate_limits_collection
        self._max_attempts = max_attempts
        self._window = timedelta(seconds=window_seconds)
        self._client: Any | None = None

    def _db(self) -> Any:
        if self._client is None:
            from google.cloud import firestore

            self._client = firestore.Client(project=self._project, database=self._database)
        return self._client

    def check(self, operation: str, key: str, *, as_of: datetime) -> None:
        from google.cloud import firestore

        checked_at = _utc(as_of)
        document_id = hashlib.sha256(f"{operation}\0{key}".encode()).hexdigest()
        reference = self._db().collection(self._collection_name).document(document_id)
        transaction = self._db().transaction()

        @firestore.transactional
        def _increment(txn: Any) -> None:
            snapshot = reference.get(transaction=txn)
            data = snapshot.to_dict() if snapshot.exists else {}
            txn.set(
                reference,
                _next_rate_limit(
                    data,
                    operation=operation,
                    checked_at=checked_at,
                    window=self._window,
                    max_attempts=self._max_attempts,
                ),
            )

        _increment(transaction)


def _next_rate_limit(
    data: dict[str, Any],
    *,
    operation: str,
    checked_at: datetime,
    window: timedelta,
    max_attempts: int,
) -> dict[str, Any]:
    started_at = _utc(data["started_at"]) if data else checked_at
    count = int(data.get("count", 0))
    if checked_at >= started_at + window:
        started_at = checked_at
        count = 0
    if count >= max_attempts:
        raise RateLimitExceeded("embed broker rate limit exceeded")
    return {
        "operation": operation,
        "started_at": started_at,
        "count": count + 1,
        "expires_at": started_at + window,
    }


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)
