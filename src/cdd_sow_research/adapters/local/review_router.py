"""Local ReviewRouterPort: a durable SQLite outbox and direct human-review-console service hand-off.

An escalated dossier is first committed to cdd-sow-research's own SQLite outbox.  If the local
human-review-console is available, the adapter then attempts an immediate S2S flush; failures remain
pending for the next route/startup flush.  This keeps the local journey genuine without letting
cdd-sow-research write human-review-console storage or use the browser/portal identity boundary as a
service integration.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path

from review_kit import (
    Citation,
    OutboxEntry,
    Review,
    ReviewClient,
    ReviewClientError,
    ReviewSubmitted,
)

from ...config import Settings
from ...domain.models import CDDCase, PerpetualKycAssessment, UboResolution
from ...envread import optional_setting
from .._review_payload import assessment_to_review, case_to_review, resolution_to_review

_DEFAULT_OUTBOX_PATH = Path.home() / ".cdd_sow_research" / "review-outbox.db"
_ACTOR = "doc1-cdd-sow-research"


class SqliteReviewOutbox:
    """cdd-sow-research-owned durable outbox with retry-safe, producer-keyed entries.

    The console owns its own review database.  This table is only cdd-sow-research's delivery log
    and
    intentionally stores the complete, already-redacted shared-kit payload needed to retry.
    """

    def __init__(self, path: str) -> None:
        if path not in (":memory:", "") and not path.startswith("file:"):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS review_outbox (
                    source_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def _key(review: Review) -> str:
        if review.source_key:
            return review.source_key
        # Legacy producers may not supply source_key.  Their full payload is still stable for a
        # repeat of the same review, without changing the shared-kit backward-compatible shape.
        encoded = json.dumps(review.to_payload(), sort_keys=True, separators=(",", ":"))
        return f"legacy:{sha256(encoded.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _from_payload(payload: str) -> Review:
        raw = json.loads(payload)
        return Review(
            action=str(raw["action"]),
            subject=str(raw["subject"]),
            maker=str(raw["maker"]),
            tenant=str(raw["tenant"]),
            summary=str(raw.get("summary", "")),
            severity=str(raw.get("severity", "medium")),
            required_approvals=int(raw.get("required_approvals", 1)),
            sod_group=str(raw.get("sod_group", "")),
            case_ref=str(raw.get("case_ref", "")),
            source_key=str(raw.get("source_key", "")),
            citations=tuple(
                Citation(
                    source_id=str(c["source_id"]),
                    title=str(c["title"]),
                    snippet=str(c.get("snippet", "")),
                )
                for c in raw.get("citations", [])
            ),
        )

    def enqueue(self, review: Review, *, actor: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO review_outbox (source_key, payload, actor)
                VALUES (?, ?, ?)
                ON CONFLICT(source_key) DO NOTHING
                """,
                (self._key(review), json.dumps(review.to_payload(), sort_keys=True), actor),
            )
            self._conn.commit()

    def pending(self) -> tuple[OutboxEntry, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload, actor FROM review_outbox "
                "WHERE delivered_at IS NULL ORDER BY created_at"
            ).fetchall()
        return tuple(
            OutboxEntry(review=self._from_payload(row["payload"]), actor=row["actor"])
            for row in rows
        )

    def flush(self, client: ReviewClient) -> list[ReviewSubmitted]:
        """Attempt every pending delivery, retaining failures for a future retry."""
        submitted: list[ReviewSubmitted] = []
        for entry in self.pending():
            try:
                result = client.submit(entry.review, actor=entry.actor)
            except ReviewClientError as exc:
                with self._lock:
                    self._conn.execute(
                        """
                        UPDATE review_outbox SET attempts = attempts + 1, last_error = ?
                        WHERE source_key = ? AND delivered_at IS NULL
                        """,
                        (str(exc), self._key(entry.review)),
                    )
                    self._conn.commit()
                continue
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE review_outbox
                    SET delivered_at = CURRENT_TIMESTAMP, attempts = attempts + 1, last_error = ''
                    WHERE source_key = ? AND delivered_at IS NULL
                    """,
                    (self._key(entry.review),),
                )
                self._conn.commit()
            submitted.append(result)
        return submitted


class LocalReviewRouter:
    """Persist escalations locally and optionally submit them to
    human-review-console's S2S intake.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        outbox_path = optional_setting("CDD_LOCAL_REVIEW_OUTBOX") or ""
        self._outbox = SqliteReviewOutbox(outbox_path or str(_DEFAULT_OUTBOX_PATH))
        base_url = optional_setting("CDD_HRZ7_URL") or ""
        self._client: ReviewClient | None = None
        if base_url:
            if optional_setting("CDD_S2S_TOKEN") is None:
                raise RuntimeError("CDD_S2S_TOKEN must be set when CDD_HRZ7_URL is configured")
            # ReviewClient enforces HTTPS except for a loopback HTTP local console.
            self._client = ReviewClient(
                base_url,
                token_env="CDD_S2S_TOKEN",
                signing_key_env="CDD_S2S_SIGNING_KEY",
            )
            # Startup retry makes a previously queued escalation visible as soon as
            # human-review-console returns.
            self._outbox.flush(self._client)

    def route(self, case: CDDCase, *, maker: str) -> None:
        self._submit(case_to_review(case, maker=maker))

    def route_monitoring(self, assessment: PerpetualKycAssessment, *, maker: str) -> None:
        """Route a perpetual-KYC re-score through the same durable outbox as a dossier."""
        self._submit(assessment_to_review(assessment, maker=maker))

    def route_ownership(self, resolution: UboResolution, *, maker: str) -> None:
        """Route a UBO-graph resolution through the same durable outbox as a dossier."""
        self._submit(
            resolution_to_review(resolution, maker=maker, policy=self._settings.policy.ubo_graph)
        )

    def _submit(self, review: Review) -> None:
        self._outbox.enqueue(review, actor=_ACTOR)
        if self._client is not None:
            self._outbox.flush(self._client)

    @property
    def outbox(self) -> SqliteReviewOutbox:
        """Expose pending local delivery records for tests and local demo diagnostics."""
        return self._outbox
