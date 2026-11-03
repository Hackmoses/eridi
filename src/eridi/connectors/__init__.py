"""Storage connectors, one module per source. `connector_for` picks the right one for a root."""

from collections.abc import Iterable
from pathlib import Path

from eridi.connectors.base import Connector, ConnectorError
from eridi.connectors.local import LocalConnector
from eridi.core.config import Settings
from eridi.core.models import Root


def connector_for(
    root: Root, settings: Settings | None = None, *, ignore: Iterable[str] = ()
) -> Connector:
    """`ignore` adds gitignore-style patterns, e.g. Eridi's own folders inside a root."""
    settings = settings or Settings()
    if root.kind == "local":
        return LocalConnector(
            Path(root.location),
            root.id,
            read_online_only_files=settings.read_online_only_files,
            ignore=ignore,
        )
    raise ConnectorError(
        f"Storage {root.label!r} uses {root.kind!r}, which this version of Eridi can't open. "
        "Update Eridi, or remove this storage."
    )
