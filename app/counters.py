"""Counters that survive restarts, for spend limits. Numbers only: no names, resumes, answers or IP addresses
(addresses arrive here already hashed).

  partition "day-2026-11-01",  row "<address hash>:answers"   -> how many answers that address scored today
  partition "month-2026-11",   row "claude_usd"                -> what Claude cost the app this month

On Azure the counters live in an Azure Table (COUNTERS_TABLE_URL; the container app's managed identity has the
Storage Table Data Contributor role, no keys). Locally and in tests they live in memory.
"""
import logging
import threading

from .config import settings

log = logging.getLogger("bol")


class MemoryCounters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, str], float] = {}

    def add(self, partition: str, row: str, amount: float) -> float:
        with self._lock:
            key = (partition, row)
            self._values[key] = self._values.get(key, 0) + amount
            return self._values[key]

    def get(self, partition: str, row: str) -> float:
        with self._lock:
            return self._values.get((partition, row), 0)

    def reset(self) -> None:
        with self._lock:
            self._values = {}


class TableCounters:
    """Azure Table Storage. Tables have no atomic increment, so each add reads the row and writes it back only if
    nobody changed it in between (ETag check), retrying on a clash."""

    def __init__(self, url: str, table: str) -> None:
        self._url, self._table = url, table
        self._client = None
        self._lock = threading.Lock()

    def _table_client(self):
        with self._lock:
            if self._client is None:
                from azure.data.tables import TableServiceClient
                from azure.identity import DefaultAzureCredential

                service = TableServiceClient(endpoint=self._url, credential=DefaultAzureCredential())
                self._client = service.create_table_if_not_exists(self._table)
            return self._client

    def add(self, partition: str, row: str, amount: float) -> float:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        client = self._table_client()
        for _ in range(6):
            try:
                entity = client.get_entity(partition, row)
            except ResourceNotFoundError:
                try:
                    client.create_entity({"PartitionKey": partition, "RowKey": row, "value": float(amount)})
                    return float(amount)
                except ResourceExistsError:
                    continue  # someone created it first: read it again
            value = float(entity.get("value", 0)) + amount
            entity["value"] = value
            try:
                client.update_entity(entity, mode=UpdateMode.REPLACE, etag=entity.metadata["etag"],
                                     match_condition=MatchConditions.IfNotModified)
                return value
            except ResourceModifiedError:
                continue
        raise RuntimeError("counter kept changing; gave up")

    def get(self, partition: str, row: str) -> float:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return float(self._table_client().get_entity(partition, row).get("value", 0))
        except ResourceNotFoundError:
            return 0.0


store = TableCounters(settings.counters_table_url, settings.counters_table) if settings.counters_table_url \
    else MemoryCounters()
