"""Unit tests for arcade_agent.bindings.BindingStore."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from arcade_agent.bindings import Binding, BindingStore
from arcade_agent.errors import BindingExists, BindingNotFound, ConfigError


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "bindings.toml")
    assert store.list() == []


def test_add_persists_to_disk(tmp_path: Path) -> None:
    store_path = tmp_path / "bindings.toml"
    store = BindingStore(path=store_path)
    store.add(Binding(name="personal", user_id="alice@example.com"))

    assert store_path.exists()
    fresh = BindingStore(path=store_path)
    assert [b.name for b in fresh.list()] == ["personal"]
    assert fresh.get("personal").user_id == "alice@example.com"


def test_add_without_make_default_does_not_set_default(tmp_path: Path) -> None:
    """Spec §5.2: ``add`` only updates the default pointer when ``make_default=True``."""
    store = BindingStore(path=tmp_path / "bindings.toml")
    store.add(Binding(name="personal", user_id="a@x"))
    with pytest.raises(BindingNotFound):
        store.get_default()


def test_first_add_with_make_default_sets_default(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "bindings.toml")
    store.add(Binding(name="personal", user_id="a@x"), make_default=True)
    assert store.get_default().name == "personal"


def test_add_duplicate_raises(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "bindings.toml")
    store.add(Binding(name="personal", user_id="a@x"))
    with pytest.raises(BindingExists):
        store.add(Binding(name="personal", user_id="b@x"))


def test_remove_clears_default_when_target(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "bindings.toml")
    store.add(Binding(name="personal", user_id="a@x"), make_default=True)
    store.add(Binding(name="work", user_id="b@x"))
    store.remove("personal")
    with pytest.raises(BindingNotFound):
        store.get_default()
    assert [b.name for b in store.list()] == ["work"]


def test_remove_missing_raises(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "bindings.toml")
    with pytest.raises(BindingNotFound):
        store.remove("nope")


def test_set_default_validates_existence(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "bindings.toml")
    store.add(Binding(name="personal", user_id="a@x"))
    store.add(Binding(name="work", user_id="b@x"))
    store.set_default("work")
    assert store.get_default().name == "work"

    with pytest.raises(BindingNotFound):
        store.set_default("missing")


def test_pydantic_name_pattern() -> None:
    Binding(name="ok_name-1", user_id="a@x")
    with pytest.raises(ValidationError):
        Binding(name="bad name!", user_id="a@x")


def test_load_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "bindings.toml"
    p.write_text("not = valid = toml")
    store = BindingStore(path=p)
    with pytest.raises(ConfigError):
        store.list()


def test_load_duplicate_name_in_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "bindings.toml"
    p.write_text(
        '[[bindings]]\nname = "a"\nuser_id = "x"\n[[bindings]]\nname = "a"\nuser_id = "y"\n'
    )
    store = BindingStore(path=p)
    with pytest.raises(ConfigError):
        store.list()


def test_unknown_top_level_keys_ignored(tmp_path: Path) -> None:
    p = tmp_path / "bindings.toml"
    p.write_text('future_field = "ignored"\n[[bindings]]\nname = "a"\nuser_id = "x"\n')
    store = BindingStore(path=p)
    assert [b.name for b in store.list()] == ["a"]


def test_default_pointing_at_missing_binding(tmp_path: Path) -> None:
    p = tmp_path / "bindings.toml"
    p.write_text('default = "ghost"\n[[bindings]]\nname = "a"\nuser_id = "x"\n')
    store = BindingStore(path=p)
    assert store.list()[0].name == "a"
    with pytest.raises(BindingNotFound):
        store.get_default()


def test_save_uses_mode_600(tmp_path: Path) -> None:
    store = BindingStore(path=tmp_path / "bindings.toml")
    store.add(Binding(name="personal", user_id="a@x"))
    mode = os.stat(store.path).st_mode & 0o777
    assert mode == 0o600


def test_load_is_idempotent_and_picks_up_external_writes(tmp_path: Path) -> None:
    p = tmp_path / "bindings.toml"
    store = BindingStore(path=p)
    store.add(Binding(name="a", user_id="x@y"))

    p.write_text('[[bindings]]\nname = "b"\nuser_id = "z@y"\n')
    store.load()
    assert [b.name for b in store.list()] == ["b"]
