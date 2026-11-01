"""Spend counters: daily limits per address and the monthly budget, kept as numbers only."""
import dataclasses
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from fastapi.testclient import TestClient

from conftest import prepare, score
from app import config, counters, limits, main


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh():
    counters.store.reset()
    limits.heavy.reset()
    yield
    counters.store.reset()


def set_limits(monkeypatch, **values):
    monkeypatch.setattr(limits, "settings", dataclasses.replace(
        config.settings, limits=dataclasses.replace(config.settings.limits, **values)))


def test_daily_answer_limit_per_address(client, monkeypatch):
    p = prepare(client)
    set_limits(monkeypatch, answers_per_day=2)
    codes = [score(client, p, "I built the canteen app in Flask.").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = score(client, p, "I built the canteen app in Flask.")
    assert "See you tomorrow" in r.json()["detail"]


def test_daily_session_limit(client, monkeypatch):
    set_limits(monkeypatch, sessions_per_day=1)
    prepare(client)
    from conftest import JD, RESUME
    r = client.post("/api/prepare", data={"resume_text": RESUME, "jd_text": JD})
    assert r.status_code == 429


def test_retry_with_same_key_is_not_counted_twice(client, monkeypatch):
    p = prepare(client)
    set_limits(monkeypatch, answers_per_day=1)
    headers = {"Idempotency-Key": "count-once-0001"}
    assert score(client, p, "I built the canteen app in Flask.", headers=headers).status_code == 200
    assert score(client, p, "I built the canteen app in Flask.", headers=headers).status_code == 200  # the replay


def test_addresses_are_hashed():
    key = limits.address_key("203.0.113.7")
    assert "203" not in key and len(key) == 20 and key == limits.address_key("203.0.113.7")


def test_monthly_budget_counts_up_and_pauses(monkeypatch):
    set_limits(monkeypatch, claude_usd_per_month=0.05)
    limits.budget.add(claude_usd=0.03)
    assert not limits.budget.claude_paused()
    limits.budget.add(claude_usd=0.03)
    assert limits.budget.claude_paused()


def test_counter_outage_lets_requests_through(monkeypatch):
    class Broken:
        def add(self, *a):
            raise ConnectionError("table down")

        def get(self, *a):
            raise ConnectionError("table down")

    monkeypatch.setattr(counters, "store", Broken())
    set_limits(monkeypatch, answers_per_day=1, claude_usd_per_month=1)
    limits.check_daily("1.2.3.4", "answers")  # no exception
    assert not limits.budget.claude_paused()


# ---------- the Azure Table version, with a fake table ----------

class Entity(dict):
    def __init__(self, data, etag):
        super().__init__(data)
        self.metadata = {"etag": etag}


class FakeTable:
    """Rows with ETags. `clash` makes the next update fail once, as if another server wrote first."""

    def __init__(self):
        self.rows, self.version, self.clash = {}, 0, 0

    def get_entity(self, partition, row):
        if (partition, row) not in self.rows:
            raise ResourceNotFoundError("no row")
        data, etag = self.rows[(partition, row)]
        return Entity(data, etag)

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("exists")
        self.version += 1
        self.rows[key] = (dict(entity), f"v{self.version}")

    def update_entity(self, entity, mode, etag, match_condition):
        key = (entity["PartitionKey"], entity["RowKey"])
        if self.clash:
            self.clash -= 1
            data, _ = self.rows[key]
            data["value"] += 1  # the other server's write
            self.version += 1
            self.rows[key] = (data, f"v{self.version}")
        if self.rows[key][1] != etag:
            raise ResourceModifiedError("changed")
        self.version += 1
        self.rows[key] = (dict(entity), f"v{self.version}")


def test_table_counters_add_and_survive_a_clash(monkeypatch):
    table = FakeTable()
    store = counters.TableCounters("https://example.table.core.windows.net", "counters")
    monkeypatch.setattr(store, "_table_client", lambda: table)
    assert store.add("day-2026-11-01", "abc:answers", 1) == 1
    assert store.add("day-2026-11-01", "abc:answers", 1) == 2
    table.clash = 1  # another replica adds 1 between our read and our write
    assert store.add("day-2026-11-01", "abc:answers", 1) == 4  # retried on top of their write: nothing lost
    assert store.get("day-2026-11-01", "abc:answers") == 4 and store.get("day-2026-11-01", "other") == 0
