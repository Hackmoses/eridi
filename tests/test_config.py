"""M1.5: config.toml load, save and defaults."""

from pathlib import Path

import pytest

from eridi.core.config import (
    Config,
    ConfigError,
    Settings,
    User,
    config_path,
    load_config,
    save_config,
)
from eridi.core.ids import split_id
from eridi.core.models import Root


def test_missing_config_gives_defaults(isolated_home: Path) -> None:
    config = load_config()
    assert config.user == User()
    assert config.roots == ()
    assert config.settings == Settings()
    assert config.settings.size_limit_bytes == 25 * 1024 * 1024
    assert split_id(config.device_id)[0] == "dev"
    assert config.record_folder is None
    assert config.effective_record_folder == isolated_home / "record"
    assert not config_path().exists()


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    config = Config(
        device_id="dev_01JC8Y0000000000000000000A",
        user=User("A. Okafor", "a.okafor@example.edu"),
        record_folder=tmp_path / "OneDrive - Uni" / "Eridi",
        roots=(
            Root("root_A", "Lab Drive", str(tmp_path / "Lab Drive"), is_home_default=True),
            Root("root_B", "Protokolle_Ü", "C:\\Users\\ü\\Protokolle", search=False, save=True),
        ),
        settings=Settings(size_limit_mb=10, snapshot_cap_mb=100, read_online_only_files=True),
    )
    path = save_config(config)
    assert path == config_path()
    assert load_config() == config
    assert "Protokolle_Ü" in path.read_text(encoding="utf-8")


def test_with_root_adds_or_replaces() -> None:
    config = Config.default().with_root(Root("root_A", "A", "/a"))
    config = config.with_root(Root("root_B", "B", "/b")).with_root(Root("root_A", "A2", "/a"))
    assert [(r.id, r.label) for r in config.roots] == [("root_B", "B"), ("root_A", "A2")]
    assert config.root("root_A") == Root("root_A", "A2", "/a")
    assert config.root("missing") is None


def test_partial_file_fills_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[user]\nname = "J. Lindqvist"\n', encoding="utf-8")
    config = load_config(path)
    assert config.user.name == "J. Lindqvist"
    assert config.settings == Settings()


def test_unknown_keys_survive_a_save(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'device_id = "dev_X"\nfuture_option = 3\n\n[mcp]\nallow_writes = false\n',
        encoding="utf-8",
    )
    save_config(load_config(path), path)
    text = path.read_text(encoding="utf-8")
    assert "future_option = 3" in text
    assert "allow_writes = false" in text


def test_invalid_toml_names_file_and_line(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[user]\nname = "A"\nemail = \n', encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    message = str(info.value)
    assert str(path) in message
    assert "line 3" in message


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("[settings]\nsize_limit_mb = 'big'\n", "settings.size_limit_mb"),
        ("[settings]\nsize_limit_mb = true\n", "settings.size_limit_mb"),
        ("[settings]\nsize_limit_mb = 0\n", "settings.size_limit_mb"),
        ("[settings]\nread_online_only_files = 1\n", "settings.read_online_only_files"),
        ("user = 'me'\n", "user"),
        ("[[roots]]\nid = 'root_A'\nlabel = 'A'\n", "roots[0].location"),
        ("[[roots]]\nid = 'r'\nlabel = 'A'\nlocation = '/a'\nsearch = 'yes'\n", "roots[0].search"),
        (
            "[[roots]]\nid = 'r'\nlabel = 'A'\nlocation = '/a'\n"
            "[[roots]]\nid = 'r'\nlabel = 'B'\nlocation = '/b'\n",
            "roots",
        ),
    ],
)
def test_wrong_values_name_the_key(tmp_path: Path, body: str, key: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert f"'{key}'" in str(info.value)
    assert str(path) in str(info.value)
