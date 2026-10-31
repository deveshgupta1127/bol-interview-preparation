"""Generated speech, cached in Azure Blob Storage so replays cost nothing.

Blobs live under tts-cache/<user_id>/... and a lifecycle rule on the storage account deletes them after 7 days.
The API signs in to Storage with its managed identity (no key). Without TTS_CACHE_URL nothing is cached.
The cache is best effort: if Storage fails, the audio is simply generated again.
"""
import logging
from functools import lru_cache

from .config import settings

log = logging.getLogger("bol")


def enabled() -> bool:
    return bool(settings.tts_cache_url)


@lru_cache(maxsize=1)
def _container():
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient(settings.tts_cache_url, credential=DefaultAzureCredential())
    return service.get_container_client(settings.tts_cache_container)


def get(key: str) -> bytes | None:
    if not enabled():
        return None
    try:
        return _container().download_blob(key).readall()
    except Exception as exc:  # ResourceNotFoundError is the usual case: not cached yet
        if type(exc).__name__ != "ResourceNotFoundError":
            log.warning("tts cache read failed: %s", type(exc).__name__)
        return None


def put(key: str, data: bytes) -> None:
    if not enabled():
        return
    try:
        from azure.storage.blob import ContentSettings

        _container().upload_blob(key, data, overwrite=True, content_settings=ContentSettings(content_type="audio/wav"))
    except Exception as exc:
        log.warning("tts cache write failed: %s", type(exc).__name__)


def delete_user(user_id: str) -> None:
    """Removes every cached clip of one student (voice deleted or account deleted)."""
    if not enabled():
        return
    try:
        container = _container()
        names = [b.name for b in container.list_blobs(name_starts_with=f"{user_id}/")]
        for start in range(0, len(names), 256):
            container.delete_blobs(*names[start:start + 256])
    except Exception as exc:
        log.warning("tts cache delete failed: %s", type(exc).__name__)
