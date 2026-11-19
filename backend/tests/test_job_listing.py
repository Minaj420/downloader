"""GET /api/v1/jobs legacy and scoped listing (ARCHITECTURE §59 Q1 technical contract)."""

import base64
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.domain.actors import Actor
from app.models.jobs import DownloadJob, JobItem
from app.repositories import jobs as job_repo
from app.services.job_service import (
    InvalidJobCursor,
    decode_job_cursor,
    encode_job_cursor,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
ACTIVE = ["queued", "analyzing", "downloading", "post_processing", "finalizing"]
TERMINAL = ["completed", "failed", "cancelled"]
INVALID = {
    "error": {
        "code": "invalid_request",
        "message": "The request payload is invalid.",
        "retryable": False,
        "analysis_id": None,
    }
}


@pytest.fixture(autouse=True)
def paused_queue(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Listing is read-only; the runner must never touch seeded rows.
    async def paused() -> None:
        pass

    monkeypatch.setattr(cast(FastAPI, client.app).state.queue_manager, "_claim_and_run", paused)


def app_of(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def make_job(
    status: str,
    created_at: datetime,
    *,
    owner: str = "local",
    job_type: str = "single",
    error_code: str | None = None,
    job_id: str | None = None,
    total_items: int = 1,
) -> DownloadJob:
    if error_code is None and status in {"failed", "cancelled"}:
        error_code = "network_error" if status == "failed" else "cancelled"
    return DownloadJob(
        id=job_id or uuid4().hex,
        owner_id=owner,
        analysis_id="a" * 32,
        source_url="https://example.com/v",
        title=f"{status} job",
        job_type=job_type,
        total_items=total_items,
        collection_mode="audio" if job_type == "collection" else None,
        variant_id="" if job_type == "collection" else "v1",
        destination="app_library",
        conflict_policy="skip",
        keep_app_copy=False,
        status=status,
        progress_percent=0.0,
        error_code=error_code,
        error_message="Summary" if error_code else None,
        created_at=created_at,
        updated_at=created_at,
    )


def make_child(
    parent: DownloadJob, order: int, status: str, error_code: str | None = None
) -> JobItem:
    return JobItem(
        id=uuid4().hex,
        job_id=parent.id,
        item_id=f"item-{order}",
        source_order=order,
        source_url=f"https://example.com/{order}",
        title=f"Track {order}",
        status=status,
        progress_percent=0.0,
        error_code=error_code,
        error_message="Child" if error_code else None,
        created_at=parent.created_at,
        updated_at=parent.created_at,
    )


def seed(
    client: TestClient, records: list[DownloadJob], items: Sequence[JobItem] = (),
) -> None:
    async def run() -> None:
        async with app_of(client).state.sessionmaker() as session:
            session.add_all(records)
            await session.flush()
            session.add_all(items)
            await session.commit()

    assert client.portal is not None
    client.portal.call(run)


def collection(
    created_at: datetime, status: str, children: list[tuple[str, str | None]],
    *, owner: str = "local", error_code: str | None = None,
) -> tuple[DownloadJob, list[JobItem]]:
    parent = make_job(
        status, created_at, owner=owner, job_type="collection",
        error_code=error_code, total_items=len(children),
    )
    # Inserted in reverse to prove the read orders by source_order.
    rows = [make_child(parent, order, s, c) for order, (s, c) in enumerate(children, start=1)]
    return parent, rows[::-1]


@contextmanager
def count_selects(client: TestClient) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = app_of(client).state.engine.sync_engine

    def listener(*args: Any) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def cursor_of(payload: Any) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def ids(body: dict[str, Any]) -> list[str]:
    return [job["id"] for job in body["jobs"]]


def history_order(records: list[DownloadJob]) -> list[str]:
    ordered = sorted(records, key=lambda r: (r.created_at, r.id), reverse=True)
    return [record.id for record in ordered]


def all_history_pages(client: TestClient, limit: int) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"scope": "history", "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/api/v1/jobs", params=params).json()
        assert set(body) == {"jobs", "next_cursor"}
        assert len(body["jobs"]) <= limit
        seen.extend(ids(body))
        cursor = body["next_cursor"]
        if cursor is None:
            assert len(body["jobs"]) <= limit
            return seen
        assert len(body["jobs"]) == limit


# --- Legacy -----------------------------------------------------------------


def test_legacy_list_matches_detail_with_batched_ordered_children(client: TestClient) -> None:
    failed, failed_items = collection(
        BASE, "failed", [("completed", None), ("failed", "network_error")],
        error_code="partial_failure",
    )
    cancelled, cancelled_items = collection(
        BASE + timedelta(seconds=1), "cancelled",
        [("cancelled", "cancelled"), ("completed", None), ("queued", None)],
        error_code="cancelled",
    )
    blocked, blocked_items = collection(
        BASE + timedelta(seconds=2), "failed", [("failed", "unsupported_url")],
        error_code="all_children_failed",
    )
    empty, _ = collection(BASE + timedelta(seconds=3), "queued", [])
    single_ok = make_job("failed", BASE + timedelta(seconds=4), error_code="network_error")
    single_no = make_job("failed", BASE + timedelta(seconds=5), error_code="unsupported_url")
    foreign, foreign_items = collection(
        BASE + timedelta(seconds=6), "queued", [("queued", None)], owner="other",
    )
    seed(
        client, [failed, cancelled, blocked, empty, single_ok, single_no, foreign],
        failed_items + cancelled_items + blocked_items + foreign_items,
    )

    body = client.get("/api/v1/jobs").json()
    assert set(body) == {"jobs"}
    newest_first = [single_no, single_ok, empty, blocked, cancelled, failed]
    assert ids(body) == [record.id for record in newest_first]
    details = [client.get(f"/api/v1/jobs/{record.id}").json() for record in newest_first]
    assert body["jobs"] == details
    by_id = {job["id"]: job for job in body["jobs"]}
    assert [i["source_order"] for i in by_id[cancelled.id]["items"]] == [1, 2, 3]
    assert [i["item_id"] for i in by_id[failed.id]["items"]] == ["item-1", "item-2"]
    assert by_id[empty.id]["items"] == []
    assert by_id[single_ok.id]["items"] is None
    assert by_id[failed.id]["error"]["retryable"] is True
    assert by_id[cancelled.id]["error"]["retryable"] is True
    assert by_id[blocked.id]["error"]["retryable"] is False
    assert by_id[single_ok.id]["error"]["retryable"] is True
    assert by_id[single_no.id]["error"]["retryable"] is False
    # Unknown query parameters are ignored.
    assert client.get("/api/v1/jobs", params={"foo": "bar"}).json() == body


def test_legacy_list_stays_newest_50(client: TestClient) -> None:
    records = [make_job("completed", BASE + timedelta(seconds=i)) for i in range(55)]
    seed(client, records)
    body = client.get("/api/v1/jobs").json()
    assert ids(body) == [r.id for r in reversed(records)][:50]


def test_batched_children_are_owner_scoped(client: TestClient) -> None:
    own, own_items = collection(BASE, "queued", [("queued", None), ("queued", None)])
    other, other_items = collection(BASE, "queued", [("queued", None)], owner="other")
    seed(client, [own, other], own_items + other_items)

    async def read() -> dict[str, list[JobItem]]:
        async with app_of(client).state.sessionmaker() as session:
            return await job_repo.list_job_items_for_jobs(session, [own.id, other.id], "local")

    assert client.portal is not None
    grouped = client.portal.call(read)
    assert [item.source_order for item in grouped[own.id]] == [1, 2]
    assert grouped[other.id] == []


@pytest.mark.parametrize("params", [
    {}, {"scope": "active"}, {"scope": "history"},
])
def test_query_count_independent_of_collection_count(
    client: TestClient, params: dict[str, str],
) -> None:
    status = "queued" if params.get("scope") == "active" else "failed"

    def add(offset: int) -> None:
        parent, children = collection(
            BASE + timedelta(seconds=offset), status,
            [("failed", "network_error"), ("completed", None)],
            error_code="partial_failure" if status == "failed" else None,
        )
        seed(client, [parent], children)

    add(0)
    seed(client, [make_job(status, BASE - timedelta(seconds=1))])
    with count_selects(client) as small:
        first = client.get("/api/v1/jobs", params=params)
    assert first.status_code == 200 and len(first.json()["jobs"]) == 2
    for offset in range(1, 8):
        add(offset)
    with count_selects(client) as large:
        second = client.get("/api/v1/jobs", params=params)
    assert second.status_code == 200 and len(second.json()["jobs"]) == 9
    assert len(small) == len(large)
    assert len(large) <= 3


# --- scope=active -------------------------------------------------------------


def test_active_scope_shows_every_nonterminal_job_oldest_first(client: TestClient) -> None:
    # 60 newer terminal jobs hide every older non-terminal job from legacy newest-50.
    terminal = [
        make_job(TERMINAL[i % 3], BASE + timedelta(hours=1, seconds=i)) for i in range(60)
    ]
    old = [make_job(status, BASE + timedelta(seconds=i)) for i, status in enumerate(ACTIVE)]
    tie_at = BASE + timedelta(seconds=30)
    tied = [make_job("queued", tie_at, job_id=f"{n:032x}") for n in (9, 3, 7)]
    many = [make_job("queued", BASE + timedelta(minutes=1, seconds=i)) for i in range(52)]
    foreign = [make_job(status, BASE, owner="other") for status in ACTIVE]
    seed(client, terminal + old + tied + many + foreign)

    legacy = set(ids(client.get("/api/v1/jobs").json()))
    assert legacy.isdisjoint(r.id for r in old + tied + many)

    body = client.get("/api/v1/jobs", params={"scope": "active"}).json()
    assert set(body) == {"jobs", "next_cursor"}
    assert body["next_cursor"] is None
    expected = old + sorted(tied, key=lambda r: r.id) + many
    assert ids(body) == [r.id for r in expected]
    assert len(body["jobs"]) == 60
    assert {job["status"] for job in body["jobs"]} == set(ACTIVE)


def test_unknown_status_in_neither_scope(client: TestClient) -> None:
    seed(client, [make_job("mystery", BASE), make_job("queued", BASE + timedelta(seconds=1))])
    active = client.get("/api/v1/jobs", params={"scope": "active"}).json()
    history = client.get("/api/v1/jobs", params={"scope": "history"}).json()
    assert [job["status"] for job in active["jobs"]] == ["queued"]
    assert history == {"jobs": [], "next_cursor": None}


# --- scope=history ------------------------------------------------------------


def test_history_empty(client: TestClient) -> None:
    seed(client, [make_job("queued", BASE), make_job("downloading", BASE)])
    for params in ({"scope": "history"}, {"scope": "history", "limit": "1"}):
        assert client.get("/api/v1/jobs", params=params).json() == {
            "jobs": [], "next_cursor": None,
        }


def test_history_terminal_only_default_50_and_limit_bounds(client: TestClient) -> None:
    terminal = [make_job(TERMINAL[i % 3], BASE + timedelta(seconds=i)) for i in range(120)]
    nonterminal = [
        make_job(ACTIVE[i % 5], BASE + timedelta(seconds=i, microseconds=5)) for i in range(20)
    ]
    seed(client, terminal + nonterminal)
    expected = history_order(terminal)

    default = client.get("/api/v1/jobs", params={"scope": "history"}).json()
    assert ids(default) == expected[:50]
    assert default["next_cursor"] is not None
    assert {job["status"] for job in default["jobs"]} == set(TERMINAL)

    one = client.get("/api/v1/jobs", params={"scope": "history", "limit": 1}).json()
    assert ids(one) == expected[:1] and one["next_cursor"] is not None

    hundred = client.get("/api/v1/jobs", params={"scope": "history", "limit": 100}).json()
    assert ids(hundred) == expected[:100] and hundred["next_cursor"] is not None
    rest = client.get("/api/v1/jobs", params={
        "scope": "history", "limit": 100, "cursor": hundred["next_cursor"],
    }).json()
    assert ids(rest) == expected[100:] and rest["next_cursor"] is None
    assert all_history_pages(client, 50) == expected


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 12, 13, 100])
def test_history_pages_concatenate_to_full_set_across_ties(
    client: TestClient, limit: int,
) -> None:
    tie = BASE + timedelta(seconds=10)
    records = [
        # Equal microsecond: id is the tie-break.
        *(make_job("completed", tie, job_id=f"{n:032x}") for n in (5, 1, 9, 3)),
        # 1 µs neighbours on both sides of the tie, with ids that would sort the other way.
        make_job("failed", tie + timedelta(microseconds=1), job_id="0" * 32),
        make_job("cancelled", tie - timedelta(microseconds=1), job_id="f" * 32),
        make_job("failed", tie + timedelta(microseconds=2), job_id=f"{2:032x}"),
        make_job("completed", tie - timedelta(microseconds=2), job_id=f"{8:032x}"),
        # Whole-second values (stored microsecond 000000) and further ties.
        *(make_job("cancelled", BASE, job_id=f"{n:032x}") for n in (4, 6)),
        make_job("completed", BASE + timedelta(seconds=20)),
        make_job("failed", BASE - timedelta(days=1)),
    ]
    seed(client, records)
    assert len(records) == 12
    pages = all_history_pages(client, limit)
    assert pages == history_order(records)
    assert len(pages) == len(set(pages))


@pytest.mark.parametrize("limit", [4, 6, 12])
def test_exact_page_multiple_ends_with_null_cursor(client: TestClient, limit: int) -> None:
    records = [make_job("completed", BASE + timedelta(microseconds=i)) for i in range(12)]
    seed(client, records)
    expected = history_order(records)
    cursor: str | None = None
    pages: list[list[str]] = []
    for _ in range(12 // limit):
        params: dict[str, Any] = {"scope": "history", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/api/v1/jobs", params=params).json()
        pages.append(ids(body))
        cursor = body["next_cursor"]
    assert cursor is None
    assert [len(page) for page in pages] == [limit] * (12 // limit)
    assert sum(pages, []) == expected


def test_history_cursor_is_opaque_utc_microsecond_json(client: TestClient) -> None:
    whole = make_job("completed", BASE, job_id="b" * 32)
    later = make_job("completed", BASE + timedelta(seconds=1), job_id="c" * 32)
    seed(client, [whole, later])
    body = client.get("/api/v1/jobs", params={"scope": "history", "limit": 1}).json()
    cursor = body["next_cursor"]
    assert "=" not in cursor
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert payload == {"created_at": "2026-01-01T12:00:01.000000+00:00", "id": "c" * 32}
    # DTO created_at is UTC-normalized too (rows come back naive from SQLite).
    created = datetime.fromisoformat(body["jobs"][0]["created_at"])
    assert created.utcoffset() == timedelta(0)
    assert created == BASE + timedelta(seconds=1)
    nxt = client.get("/api/v1/jobs", params={"scope": "history", "cursor": cursor}).json()
    assert ids(nxt) == [whole.id] and nxt["next_cursor"] is None


def test_cursor_codec_normalizes_naive_and_offset_times_to_utc() -> None:
    naive = make_job("completed", datetime(2026, 3, 1, 10, 0, 0), job_id="d" * 32)
    shifted = make_job(
        "completed",
        datetime(2026, 3, 1, 13, 0, 0, 7, tzinfo=timezone(timedelta(hours=3))),
        job_id="e" * 32,
    )
    when, job_id = decode_job_cursor(encode_job_cursor(naive))
    assert (when, job_id) == (datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC), "d" * 32)
    raw = encode_job_cursor(shifted)
    decoded = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert decoded["created_at"] == "2026-03-01T10:00:00.000007+00:00"
    assert decode_job_cursor(raw)[0] == datetime(2026, 3, 1, 10, 0, 0, 7, tzinfo=UTC)


def test_service_history_path_and_invalid_cursor(client: TestClient) -> None:
    records = [make_job("failed", BASE + timedelta(seconds=i)) for i in range(3)]
    seed(client, records)
    service = app_of(client).state.job_service

    async def run() -> tuple[list[str], str | None, list[str]]:
        page, cursor = await service.list_history_jobs(Actor(), limit=2)
        assert cursor is not None
        rest, end = await service.list_history_jobs(Actor(), limit=2, cursor=cursor)
        assert end is None
        with pytest.raises(InvalidJobCursor):
            await service.list_history_jobs(Actor(), limit=2, cursor="!")
        return [r.id for r in page], cursor, [r.id for r in rest]

    assert client.portal is not None
    first, _, rest = client.portal.call(run)
    assert first + rest == history_order(records)


# --- Ownership ----------------------------------------------------------------


def test_foreign_jobs_never_listed_and_foreign_cursor_is_only_a_position(
    client: TestClient,
) -> None:
    own = [make_job(TERMINAL[i % 3], BASE + timedelta(seconds=2 * i)) for i in range(6)]
    foreign = [
        make_job(TERMINAL[i % 3], BASE + timedelta(seconds=2 * i + 1), owner="other")
        for i in range(6)
    ]
    foreign_active = [make_job(status, BASE, owner="other") for status in ACTIVE]
    seed(client, own + foreign + foreign_active)
    foreign_ids = {r.id for r in foreign + foreign_active}

    for params in ({}, {"scope": "active"}, {"scope": "history", "limit": 100}):
        assert foreign_ids.isdisjoint(ids(client.get("/api/v1/jobs", params=params).json()))

    # A cursor positioned on a foreign row yields only own rows strictly older than it.
    pivot = foreign[3]
    cursor = encode_job_cursor(pivot)
    body = client.get("/api/v1/jobs", params={"scope": "history", "cursor": cursor}).json()
    expected = [r for r in own if (r.created_at, r.id) < (pivot.created_at, pivot.id)]
    assert ids(body) == history_order(expected)
    assert foreign_ids.isdisjoint(ids(body))


# --- Scoped DTOs and retryability ---------------------------------------------


def test_scoped_dtos_null_items_and_correct_retryability(client: TestClient) -> None:
    failed_ok, items_a = collection(
        BASE, "failed", [("failed", "network_error"), ("completed", None)],
        error_code="partial_failure",
    )
    failed_no, items_b = collection(
        BASE + timedelta(seconds=1), "failed", [("failed", "unsupported_url")],
        error_code="all_children_failed",
    )
    cancelled, items_c = collection(
        BASE + timedelta(seconds=2), "cancelled", [("cancelled", "cancelled")],
        error_code="cancelled",
    )
    running, items_d = collection(BASE + timedelta(seconds=3), "downloading", [("queued", None)])
    single_ok = make_job("failed", BASE + timedelta(seconds=4), error_code="network_error")
    single_no = make_job("failed", BASE + timedelta(seconds=5), error_code="unsupported_url")
    seed(
        client, [failed_ok, failed_no, cancelled, running, single_ok, single_no],
        items_a + items_b + items_c + items_d,
    )
    history = client.get("/api/v1/jobs", params={"scope": "history"}).json()["jobs"]
    active = client.get("/api/v1/jobs", params={"scope": "active"}).json()["jobs"]
    assert [job["id"] for job in active] == [running.id]
    for job in history + active:
        detail = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert job == {**detail, "items": None}
    retryable = {job["id"]: job["error"]["retryable"] for job in history}
    assert retryable == {
        failed_ok.id: True, failed_no.id: False, cancelled.id: True,
        single_ok.id: True, single_no.id: False,
    }


# --- Validation ---------------------------------------------------------------


def _bad_cursors() -> list[str]:
    good_time = "2026-01-01T12:00:00.000000+00:00"
    good_id = "a" * 32
    valid = cursor_of({"created_at": good_time, "id": good_id})
    return [
        "",
        "!!!!",
        "a",
        valid + "=",
        valid[:-1] + ("B" if valid[-1] != "B" else "C"),  # non-canonical trailing bits
        cursor_of(b"not json"),
        cursor_of(b"\xff\xfe\x00"),
        cursor_of([good_time, good_id]),
        cursor_of({"created_at": good_time}),
        cursor_of({"id": good_id}),
        cursor_of({"created_at": good_time, "id": good_id, "extra": 1}),
        cursor_of({"created_at": "2026-01-01T12:00:00.000000", "id": good_id}),
        cursor_of({"created_at": "2026-01-01T12:00:00.000000Z", "id": good_id}),
        cursor_of({"created_at": "2026-01-01T15:00:00.000000+03:00", "id": good_id}),
        cursor_of({"created_at": "2026-01-01T12:00:00.00000+00:00", "id": good_id}),
        cursor_of({"created_at": "2026-01-01T12:00:00+00:00", "id": good_id}),
        cursor_of({"created_at": "2026-13-01T12:00:00.000000+00:00", "id": good_id}),
        cursor_of({"created_at": "garbage", "id": good_id}),
        cursor_of({"created_at": 1767268800, "id": good_id}),
        cursor_of({"created_at": good_time, "id": "A" * 32}),
        cursor_of({"created_at": good_time, "id": "a" * 31}),
        cursor_of({"created_at": good_time, "id": "g" * 32}),
        cursor_of({"created_at": good_time, "id": 7}),
        cursor_of({"created_at": None, "id": None}),
    ]


@pytest.mark.parametrize("cursor", _bad_cursors())
def test_malformed_cursor_is_invalid_request(client: TestClient, cursor: str) -> None:
    response = client.get("/api/v1/jobs", params={"scope": "history", "cursor": cursor})
    assert response.status_code == 422
    assert response.json() == INVALID


def test_valid_handbuilt_cursor_is_accepted(client: TestClient) -> None:
    cursor = cursor_of({"created_at": "2026-01-01T12:00:00.000000+00:00", "id": "a" * 32})
    response = client.get("/api/v1/jobs", params={"scope": "history", "cursor": cursor})
    assert response.json() == {"jobs": [], "next_cursor": None}


@pytest.mark.parametrize("query", [
    "scope=", "scope=bogus", "scope=ACTIVE", "scope=all",
    "limit=10", "limit=", "cursor=", "cursor=abc",
    "scope=active&limit=10", "scope=active&limit=", "scope=active&cursor=",
    "scope=active&cursor=abc",
    "scope=history&limit=0", "scope=history&limit=101", "scope=history&limit=-1",
    "scope=history&limit=abc", "scope=history&limit=1.5", "scope=history&limit=",
    "scope=history&cursor=",
])
def test_invalid_parameter_combinations(client: TestClient, query: str) -> None:
    seed(client, [make_job("completed", BASE), make_job("queued", BASE)])
    response = client.get(f"/api/v1/jobs?{query}")
    assert response.status_code == 422
    assert response.json() == INVALID


def test_unknown_parameters_ignored_in_scoped_listing(client: TestClient) -> None:
    seed(client, [make_job("completed", BASE), make_job("queued", BASE)])
    for scope in ("active", "history"):
        plain = client.get("/api/v1/jobs", params={"scope": scope}).json()
        noisy = client.get("/api/v1/jobs", params={"scope": scope, "foo": "1", "page": "2"})
        assert noisy.status_code == 200 and noisy.json() == plain
