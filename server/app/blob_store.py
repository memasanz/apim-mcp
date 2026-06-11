"""Optional Azure Blob backend for the shared config file.

When ``CONFIG_BLOB_URL`` is set (e.g.
``https://acct.blob.core.windows.net/config/config.json``) the app:

* downloads the blob to ``CONFIG_PATH`` at startup (creating the local file),
* uploads to the blob on every ``ConfigStore.replace`` call,
* polls the blob's ETag periodically to pull remote changes.

Authentication uses ``DefaultAzureCredential`` (managed identity in Azure;
local dev falls back to ``az login`` creds). No account keys are used, which
is required by tenants that enforce ``allowSharedKeyAccess=false``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BlobLocation:
    account_url: str  # https://acct.blob.core.windows.net
    container: str
    blob: str

    @classmethod
    def parse(cls, url: str) -> BlobLocation:
        u = urlparse(url)
        if not u.scheme.startswith("http") or not u.netloc:
            raise ValueError(f"invalid CONFIG_BLOB_URL: {url}")
        parts = [p for p in u.path.split("/") if p]
        if len(parts) < 2:
            raise ValueError(
                "CONFIG_BLOB_URL must include container and blob path, e.g. "
                "https://acct.blob.core.windows.net/<container>/<blob>"
            )
        return cls(
            account_url=f"{u.scheme}://{u.netloc}",
            container=parts[0],
            blob="/".join(parts[1:]),
        )


class BlobSync:
    """Thin wrapper around azure-storage-blob with ETag tracking."""

    def __init__(self, url: str, client_id: str | None = None) -> None:
        # Imported lazily so the module is optional in unit tests.
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        self.loc = BlobLocation.parse(url)
        # When a UAMI client_id is provided (typical Container Apps setup),
        # bypass the DefaultAzureCredential chain entirely and use
        # ManagedIdentityCredential directly. Newer azure-identity releases
        # added WorkloadIdentityCredential to the default chain, which can
        # short-circuit incorrectly in Container Apps when AZURE_CLIENT_ID
        # is set without a federated-token file.
        if client_id:
            self._credential = ManagedIdentityCredential(client_id=client_id)
        else:
            self._credential = DefaultAzureCredential()
        self._svc = BlobServiceClient(self.loc.account_url, credential=self._credential)
        self._client = self._svc.get_blob_client(self.loc.container, self.loc.blob)
        self._etag: str | None = None

    @property
    def etag(self) -> str | None:
        return self._etag

    def exists(self) -> bool:
        try:
            return self._client.exists()
        except Exception:  # noqa: BLE001
            log.exception("blob exists check failed")
            return False

    def download_to(self, dest: Path) -> None:
        log.info("downloading config blob %s -> %s", self.loc.blob, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        downloader = self._client.download_blob()
        data = downloader.readall()
        props = self._client.get_blob_properties()
        self._etag = getattr(props, "etag", None)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

    def upload_from(self, src: Path) -> None:
        log.info("uploading config to blob %s", self.loc.blob)
        data = src.read_bytes()
        result = self._client.upload_blob(data, overwrite=True)
        self._etag = result.get("etag") if isinstance(result, dict) else getattr(
            result, "etag", None
        )

    def fetch_remote_etag(self) -> str | None:
        try:
            return self._client.get_blob_properties().etag
        except Exception:  # noqa: BLE001
            log.exception("could not fetch blob etag")
            return None


def maybe_create_blob_sync() -> BlobSync | None:
    """Construct a ``BlobSync`` if ``CONFIG_BLOB_URL`` is set, else ``None``."""
    url = os.getenv("CONFIG_BLOB_URL")
    if not url:
        return None
    client_id = os.getenv("AZURE_CLIENT_ID") or None
    return BlobSync(url, client_id=client_id)


async def remote_watch_loop(
    blob: BlobSync,
    local_path: Path,
    on_change: "asyncio.Future[None] | None" = None,
    interval: float = 15.0,
    after_download: "callable | None" = None,
) -> None:
    """Poll the blob's ETag and re-download when it changes."""

    while True:
        try:
            await asyncio.sleep(interval)
            remote = await asyncio.to_thread(blob.fetch_remote_etag)
            if remote and remote != blob.etag:
                log.info("remote config etag changed (%s -> %s); refreshing", blob.etag, remote)
                await asyncio.to_thread(blob.download_to, local_path)
                if after_download is not None:
                    res = after_download()
                    if asyncio.iscoroutine(res):
                        await res
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("blob watch loop error")


async def stop_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
