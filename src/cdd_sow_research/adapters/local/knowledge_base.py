"""Local knowledge-base adapter (KnowledgeBaseClientPort) — SQLite FTS5 governed RAG.

The ``local`` profile's stand-in for the **A2 Enterprise Knowledge Base** (whose GCP
adapter is Agent Search): a ``sqlite3`` database with an **FTS5** virtual table over the
case's KYC passages, queried with BM25 (``ORDER BY rank``). It is SDK-free, deterministic
and **seedable**, so the same code grounds the offline CLI run and the unit tests. Under
``local`` this platform client uses an in-process index, NOT HTTP to the A2 sibling (a
laptop runs one app, not the whole platform). There is no Google emulator for Agent
Search, so this path is unconditional.

The adapter returns the same :class:`RetrievedPassage` objects with page-level
:class:`Citation` provenance as the managed adapter, preserving interface parity, and it
honours the case ACL: a passage is only returned when the query's ``acl_principals``
intersect the passage's ``acl_tags`` (or the passage carries no tags). It self-seeds from
the built-in synthetic corpus on first use so an out-of-the-box local run grounds a
dossier without any ingestion step; callers may also ``seed(passages)`` or ``ingest(...)``
a corpus of their own.

Default DB path is under a per-package local dir (``~/.cdd_sow_research/local.db``); tests
pass ``:memory:`` for an ephemeral, deterministic index.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

from ...config import Settings
from ...domain.models import (
    Citation,
    IngestResult,
    KycDocument,
    RetrievalQuery,
    RetrievedPassage,
    SourceType,
    citation_title,
)
from ._seed import DEMO_CORPUS_TAG, SEED_PASSAGES

# Default on-disk location for the local index (overridable via settings.local.db_path).
_DEFAULT_DB_DIR = Path.home() / ".cdd_sow_research"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "local.db"

# FTS5 query syntax is strict; keep only word characters so a free-text query never trips
# an "fts5: syntax error" (e.g. on punctuation), and OR the terms for recall.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_ACL_SEP = "␟"  # unit-separator: joins acl_tags into one UNINDEXED column safely.


class LocalKnowledgeBaseAdapter:
    """Index + retrieve case passages from a local SQLite FTS5 store (BM25 ranked)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        db_path = getattr(getattr(settings, "local", None), "db_path", "") or str(_DEFAULT_DB_PATH)
        self._db_path = db_path
        # check_same_thread=False + an RLock: deps.get_container is lru_cached (one
        # process-wide adapter) while the sync API endpoints run in Starlette's worker
        # threadpool, so search()/ingest() are called from worker threads other than the
        # one that opened the connection. The RLock serialises access (single-writer) so
        # cross-thread reuse does not raise, and re-entrant calls (seed -> _insert,
        # ingest -> add -> _insert) do not deadlock.
        self._lock = threading.RLock()
        self._conn = self._connect(db_path)
        self._init_schema()
        # Self-seed the built-in corpus so an out-of-the-box `local` run is grounded.
        #
        # Never under any other profile. The synthetic corpus is a fixture: its passages
        # are invented and their citations point at a reserved domain that does not
        # resolve. Seeding it into a `live` index would mix fabricated evidence into a
        # dossier about a real subject, and hand a reviewer citations they cannot open.
        # An empty live index is the correct state: it fills up when a user uploads
        # documents, and a case with nothing indexed is refused as ungrounded.
        if self._settings.profile == "local" and self._is_empty():
            self.seed(SEED_PASSAGES)
        elif self._settings.profile == "local":
            self._retag_legacy_seed_rows()

    # ------------------------------------------------------------------ #
    # Connection / schema
    # ------------------------------------------------------------------ #
    @staticmethod
    def _connect(db_path: str) -> sqlite3.Connection:
        if db_path not in (":memory:", "") and not db_path.startswith("file:"):
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the FastAPI TestClient / uvicorn worker may touch the
        # in-process store from a different thread than the one that built it. Cross-thread
        # access is serialised by the adapter's RLock (single-writer), so reuse is safe.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        # One FTS5 table holds the searchable text; citation metadata rides alongside as
        # UNINDEXED columns so a single query returns everything needed to cite a hit.
        with self._lock:
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
                    text,
                    source_id UNINDEXED,
                    source_type UNINDEXED,
                    title UNINDEXED,
                    url UNINDEXED,
                    page UNINDEXED,
                    score UNINDEXED,
                    acl_tags UNINDEXED
                )
                """
            )
            self._conn.commit()

    def _retag_legacy_seed_rows(self) -> int:
        """Tag seed rows written before the demo corpus was scoped. Returns the count fixed.

        Tagging ``SEED_PASSAGES`` in source only reaches an index that does not exist yet,
        because the corpus is seeded exactly once, when the store is empty. Every laptop
        that has already run this application therefore keeps the untagged -- and so
        PUBLIC -- rows it was seeded with, and the fix reaches precisely the machines that
        never had the defect while missing the ones that do. That is the worse half of the
        two: nothing looks wrong, because the CLI still prints a cited dossier.

        Matched by seed ``source_id`` AND an empty tag set, both conditions required. The
        id alone would re-tag a case document that happened to share an id; an empty tag
        set alone would capture any legitimately public passage a future profile writes.
        """
        seed_ids = tuple(p.citation.source_id for p in SEED_PASSAGES)
        if not seed_ids:
            return 0
        placeholders = ",".join("?" for _ in seed_ids)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE passages SET acl_tags = ? "  # noqa: S608 - placeholders are generated
                f"WHERE acl_tags = '' AND source_id IN ({placeholders})",
                (DEMO_CORPUS_TAG, *seed_ids),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    def _is_empty(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT count(*) AS n FROM passages").fetchone()
        return int(row["n"]) == 0

    # ------------------------------------------------------------------ #
    # Seeding / ingestion
    # ------------------------------------------------------------------ #
    def seed(self, passages: tuple[RetrievedPassage, ...] | list[RetrievedPassage]) -> int:
        """Replace the index contents with ``passages`` (deterministic test/CLI seed)."""
        with self._lock:
            self._conn.execute("DELETE FROM passages")
            return self._insert(list(passages))

    def add(self, passages: list[RetrievedPassage]) -> int:
        """Append ``passages`` to the index without clearing existing rows."""
        return self._insert(passages)

    def _insert(self, passages: list[RetrievedPassage]) -> int:
        rows = []
        for p in passages:
            c = p.citation
            rows.append(
                (
                    p.text,
                    c.source_id,
                    c.source_type.value,
                    c.title,
                    c.url,
                    "" if c.page is None else str(c.page),
                    f"{p.score:.6f}",
                    _ACL_SEP.join(p.acl_tags),
                )
            )
        with self._lock:
            self._conn.executemany(
                "INSERT INTO passages "
                "(text, source_id, source_type, title, url, page, score, acl_tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    # ------------------------------------------------------------------ #
    # KnowledgeBaseClientPort
    # ------------------------------------------------------------------ #
    def ingest(
        self,
        document: KycDocument,
        content: bytes,
        acl_tags: tuple[str, ...],
        page_texts: tuple[str, ...] = (),
    ) -> IngestResult:
        """Index a case KYC document's text into the local FTS5 store with ACL tags.

        With ``page_texts`` the document is indexed one passage per source page, so a
        retrieved claim carries the page a reviewer must open to check it. Without it,
        the document text is parsed by the local document parser and indexed whole.
        """
        pages = [p for p in page_texts if p.strip()] if page_texts else []
        page_numbers = [i + 1 for i, p in enumerate(page_texts) if p.strip()] if page_texts else []
        if not pages:
            from .extraction import LocalDocumentExtractionAdapter

            parser = LocalDocumentExtractionAdapter(self._settings)
            extract = parser.extract(document, content, "application/pdf")
            body = (extract.text or "").strip()
            pages = [body] if body else []
            page_numbers = [1] if body else []

        passages: list[RetrievedPassage] = [
            RetrievedPassage(
                text=text,
                citation=Citation(
                    source_id=document.id,
                    source_type=SourceType.DOCUMENT,
                    title=citation_title(document.doc_type),
                    url=document.uri,
                    page=page,
                    snippet=text[:120],
                    score=0.5,
                ),
                score=0.5,
                acl_tags=tuple(acl_tags),
            )
            for text, page in zip(pages, page_numbers, strict=True)
        ]
        # Re-index this document: drop any prior rows for it, then add the new passages.
        with self._lock:
            self._conn.execute("DELETE FROM passages WHERE source_id = ?", (document.id,))
            self._conn.commit()
            n = self.add(passages)
        return IngestResult(
            document_id=document.id,
            chunks=n,
            status="indexed",
            ok=True,
            detail=f"indexed {n} passages into local FTS5",
        )

    def retract(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        """Remove every passage indexed from ``document_id``, ACL-checked first.

        A document is retractable only by a caller who can read it, so the tags are read back
        and compared rather than taken on trust. Removing nothing returns False; running a
        repair twice must be as safe as running it once.
        """
        rows = list(
            self._conn.execute("SELECT acl_tags FROM passages WHERE source_id = ?", (document_id,))
        )
        if not rows:
            return False
        for (raw,) in rows:
            tags = tuple(t for t in str(raw or "").split(_ACL_SEP) if t)
            if tags and not set(tags) <= set(acl_principals):
                raise PermissionError(f"not readable, so not retractable: {document_id!r}")
        self._conn.execute("DELETE FROM passages WHERE source_id = ?", (document_id,))
        self._conn.commit()
        return True

    def search(self, query: RetrievalQuery) -> list[RetrievedPassage]:
        """Return ranked, ACL-filtered passages with page-level citations for ``query``.

        The built-in demo corpus is admitted only as a FALLBACK, when the case's own
        evidence retrieved nothing. It used to be untagged, which under the ACL contract
        means public: it then competed with a case's uploaded documents on relevance, and
        since retrieval is capped at ``top_k`` it did not merely join them but displaced
        them. A dossier for a real subject cited a fictional bank statement.

        Ordering the two passes this way is what makes the rule stateable in one sentence:
        the demo corpus grounds a query that would otherwise be ungrounded, and never
        competes with real evidence for a place in the result.
        """
        rows = self._ranked_rows(query)
        out = self._admit(rows, query.acl_principals, query.top_k)
        if not out:
            out = self._admit(rows, (*query.acl_principals, DEMO_CORPUS_TAG), query.top_k)
        return out

    def _ranked_rows(self, query: RetrievalQuery) -> list[sqlite3.Row]:
        match = self._build_match(query.text)
        if not match:
            sql = "SELECT * FROM passages ORDER BY score DESC LIMIT ?"
            params: list[object] = [max(query.top_k, 1) * 4]
        else:
            sql = "SELECT * FROM passages WHERE passages MATCH ? ORDER BY rank LIMIT ?"
            params = [match, max(query.top_k, 1) * 4]
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _admit(
        self, rows: list[sqlite3.Row], principals: tuple[str, ...], top_k: int
    ) -> list[RetrievedPassage]:
        out: list[RetrievedPassage] = []
        for row in rows:
            passage = self._row_to_passage(row)
            if self._acl_ok(passage.acl_tags, principals):
                out.append(passage)
            if len(out) >= max(top_k, 1):
                break
        return out

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _acl_ok(passage_tags: tuple[str, ...], acl_principals: tuple[str, ...]) -> bool:
        """A passage is visible when untagged, or when the query holds EVERY tag.

        Subset (all-of) semantics, fail-closed: evidence tagged ``case:<id>`` AND
        ``tenant:<t>`` is visible only to a query carrying both, so a case id alone
        never crosses a tenant boundary, and an empty principal set sees only
        untagged (public reference) passages. Mirrors the ACL contract documented on
        ``ports.knowledge_base.KnowledgeBaseClientPort``.
        """
        if not passage_tags:
            return True
        return set(passage_tags) <= set(acl_principals)

    @staticmethod
    def _build_match(text: str) -> str:
        """Build a safe FTS5 MATCH expression: OR of the alphanumeric query tokens."""
        tokens = _TOKEN_RE.findall(text or "")
        if not tokens:
            return ""
        # Quote each token so reserved words (AND/OR/NOT/NEAR) are treated as literals.
        return " OR ".join(f'"{t}"' for t in tokens)

    @staticmethod
    def _row_to_passage(row: sqlite3.Row) -> RetrievedPassage:
        page_raw = row["page"]
        page = int(page_raw) if page_raw not in (None, "") else None
        try:
            score = float(row["score"])
        except (TypeError, ValueError):
            score = 0.0
        acl_tags = tuple(t for t in (row["acl_tags"] or "").split(_ACL_SEP) if t)
        citation = Citation(
            source_id=row["source_id"],
            source_type=LocalKnowledgeBaseAdapter._parse_source_type(row["source_type"]),
            title=row["title"],
            url=row["url"],
            page=page,
            snippet=(row["text"] or "")[:280],
            score=score,
        )
        return RetrievedPassage(text=row["text"], citation=citation, score=score, acl_tags=acl_tags)

    @staticmethod
    def _parse_source_type(value: str | None) -> SourceType:
        try:
            return SourceType(str(value))
        except (ValueError, AttributeError):
            return SourceType.DOCUMENT
