"""`config.toml`: who the user is, this device, where the record lives, and connected storage.

The file lives in the app-data folder and is never synced. Keys this version doesn't know
are kept when the file is saved again, so an older and a newer Eridi can share it.
"""

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, NoReturn

import tomli_w

from eridi.core.fsutil import atomic_write_bytes
from eridi.core.ids import new_id
from eridi.core.models import Root
from eridi.core.paths import app_home

CONFIG_NAME = "config.toml"


class ConfigError(ValueError):
    """The config file can't be used. The message names the file and what to fix."""


@dataclass(frozen=True)
class User:
    name: str = ""
    email: str | None = None


@dataclass(frozen=True)
class Settings:
    size_limit_mb: int = 25
    """Files larger than this are listed by name only; their text is not extracted."""
    snapshot_cap_mb: int = 500
    """Space for stored text versions; oldest unreferenced ones are removed first."""
    read_online_only_files: bool = False
    """Read files that a sync client keeps only in the cloud (this downloads them)."""
    scan_interval_minutes: int = 10
    """How often the app scans in the background while it is open; 0 turns it off."""
    scan_workers: int = 0
    """Processes reading files in parallel during a scan; 0 means one per CPU core."""

    @property
    def size_limit_bytes(self) -> int:
        return self.size_limit_mb * 1024 * 1024


@dataclass(frozen=True)
class Config:
    device_id: str
    user: User = field(default_factory=User)
    record_folder: Path | None = None
    roots: tuple[Root, ...] = ()
    settings: Settings = field(default_factory=Settings)
    dismissed_changes: tuple[str, ...] = ()
    """Keys of detected changes the user chose not to explain (local only, never synced)."""
    mcp_allow_writes: bool = False
    """Let AI assistants add notes through the MCP server (written as `author.kind = "agent"`)."""
    extra_unknown: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "Config":
        return cls(device_id=new_id("dev"))

    @property
    def effective_record_folder(self) -> Path:
        """The chosen record folder, or `<app-data>/record` until one is chosen."""
        return self.record_folder if self.record_folder is not None else app_home() / "record"

    def root(self, root_id: str) -> Root | None:
        return next((r for r in self.roots if r.id == root_id), None)

    def with_root(self, root: Root) -> "Config":
        """A copy with `root` added, or replacing the root with the same ID."""
        others = tuple(r for r in self.roots if r.id != root.id)
        return replace(self, roots=(*others, root))


def config_path() -> Path:
    return app_home() / CONFIG_NAME


def _bad(path: Path, key: str, problem: str) -> NoReturn:
    raise ConfigError(f"{path}: '{key}' {problem}. Fix or remove it, then try again.")


def _typed(path: Path, key: str, value: Any, kind: type, label: str) -> Any:
    # bool is a subclass of int; don't accept `true` where a number is expected.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        _bad(path, key, f"must be {label}, got {value!r}")
    return value


def _table(path: Path, data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    result: dict[str, Any] = _typed(path, key, value, dict, "a table")
    return result


def _parse(path: Path, data: dict[str, Any]) -> Config:
    known = {"device_id", "user", "record_folder", "roots", "settings", "dismissed_changes"}
    device_id = data.get("device_id")
    if device_id is None:
        device_id = new_id("dev")
    _typed(path, "device_id", device_id, str, "text")

    user_data = _table(path, data, "user")
    user = User(
        name=_typed(path, "user.name", user_data.get("name", ""), str, "text"),
        email=None
        if user_data.get("email") is None
        else _typed(path, "user.email", user_data["email"], str, "text"),
    )

    folder = data.get("record_folder")
    record_folder = (
        None
        if folder in (None, "")
        else Path(_typed(path, "record_folder", folder, str, "a folder path")).expanduser()
    )

    settings_data = _table(path, data, "settings")
    defaults = Settings()
    values: dict[str, Any] = {}
    for f in fields(Settings):
        if f.name in settings_data:
            kind = type(getattr(defaults, f.name))
            label = "true or false" if kind is bool else "a whole number"
            values[f.name] = _typed(path, f"settings.{f.name}", settings_data[f.name], kind, label)
    settings = Settings(**values)
    if settings.size_limit_mb <= 0:
        _bad(path, "settings.size_limit_mb", "must be greater than 0")
    if settings.scan_workers < 0:
        _bad(path, "settings.scan_workers", "must not be negative (0 means automatic)")
    if settings.scan_interval_minutes < 0:
        _bad(path, "settings.scan_interval_minutes", "must not be negative")
    if settings.snapshot_cap_mb < 0:
        _bad(path, "settings.snapshot_cap_mb", "must not be negative")

    roots_data = _typed(path, "roots", data.get("roots", []), list, "a list of [[roots]] tables")
    roots: list[Root] = []
    for i, item in enumerate(roots_data):
        where = f"roots[{i}]"
        _typed(path, where, item, dict, "a table")
        for required in ("id", "label", "location"):
            if required not in item:
                _bad(path, f"{where}.{required}", "is missing")
        root_defaults = Root(id="", label="", location="")
        root_values: dict[str, Any] = {}
        for f in fields(Root):
            if f.name in item:
                kind = type(getattr(root_defaults, f.name))
                label = "true or false" if kind is bool else "text"
                root_values[f.name] = _typed(path, f"{where}.{f.name}", item[f.name], kind, label)
        roots.append(Root(**root_values))
    ids = [r.id for r in roots]
    if len(ids) != len(set(ids)):
        _bad(path, "roots", "lists the same storage ID more than once")

    dismissed = _typed(path, "dismissed_changes", data.get("dismissed_changes", []), list, "a list")
    for i, key in enumerate(dismissed):
        _typed(path, f"dismissed_changes[{i}]", key, str, "text")
    mcp = _table(path, data, "mcp")
    allow_writes = _typed(
        path, "mcp.allow_writes", mcp.get("allow_writes", False), bool, "true or false"
    )
    unknown = {k: v for k, v in data.items() if k not in known}
    mcp_rest = {k: v for k, v in mcp.items() if k != "allow_writes"}
    if mcp_rest:
        unknown["mcp"] = mcp_rest
    else:
        unknown.pop("mcp", None)

    return Config(
        device_id=device_id,
        dismissed_changes=tuple(dismissed),
        mcp_allow_writes=allow_writes,
        user=user,
        record_folder=record_folder,
        roots=tuple(roots),
        settings=settings,
        extra_unknown=unknown,
    )


def load_config(path: Path | None = None) -> Config:
    """Read the config. A missing file gives defaults (with a new device ID)."""
    path = path or config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config.default()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from None
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        # The message ends with "(at line N, column M)".
        raise ConfigError(
            f"{path} is not valid TOML: {exc}. Fix the file, or delete it to start over."
        ) from None
    return _parse(path, data)


def _to_toml(config: Config) -> dict[str, Any]:
    data: dict[str, Any] = dict(config.extra_unknown)
    data["device_id"] = config.device_id
    if config.record_folder is not None:
        data["record_folder"] = str(config.record_folder)
    user: dict[str, Any] = {"name": config.user.name}
    if config.user.email is not None:
        user["email"] = config.user.email
    data["user"] = user
    data["settings"] = {f.name: getattr(config.settings, f.name) for f in fields(Settings)}
    data["roots"] = [{f.name: getattr(r, f.name) for f in fields(Root)} for r in config.roots]
    data["dismissed_changes"] = list(config.dismissed_changes)
    data["mcp"] = {**data.get("mcp", {}), "allow_writes": config.mcp_allow_writes}
    return data


def save_config(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    atomic_write_bytes(path, tomli_w.dumps(_to_toml(config)).encode("utf-8"))
    return path
