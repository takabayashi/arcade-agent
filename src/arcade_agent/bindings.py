"""On-disk registry of Arcade user bindings (name → user_id) plus an optional default."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field, ValidationError

from arcade_agent import config
from arcade_agent.errors import BindingExists, BindingNotFound, ConfigError


class Binding(BaseModel):
    """A single saved binding: a friendly name plus the Arcade user_id it represents."""

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_\-]+$")
    user_id: str = Field(min_length=1)


class BindingStore:
    """File-backed binding registry.

    The store is lazy: any public method triggers a fresh `load()` from disk so multiple
    short-lived CLI processes see each other's writes. Mutators are write-through and
    use an atomic write (tmp file + os.replace) with mode 0o600.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path if path is not None else config.BINDINGS_FILE
        self._bindings: list[Binding] = []
        self._default: str | None = None
        self._loaded: bool = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Read the TOML file from disk, populating the in-memory state.

        Missing file is treated as "empty store". Malformed TOML or schema errors raise
        :class:`ConfigError`. Always idempotent — re-running it picks up out-of-band edits.
        """
        self._bindings = []
        self._default = None
        self._loaded = True

        if not self._path.exists():
            return

        try:
            with self._path.open("rb") as fh:
                data: dict[str, Any] = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"failed to read bindings file {self._path}: {exc}") from exc

        raw_bindings = data.get("bindings", [])
        if not isinstance(raw_bindings, list):
            raise ConfigError(
                f"'bindings' must be an array of tables in {self._path}, "
                f"got {type(raw_bindings).__name__}"
            )

        seen: set[str] = set()
        for entry in raw_bindings:
            if not isinstance(entry, dict):
                raise ConfigError(f"binding entry must be a table, got {type(entry).__name__}")
            try:
                binding = Binding(**entry)
            except ValidationError as exc:
                raise ConfigError(f"invalid binding entry in {self._path}: {exc}") from exc
            if binding.name in seen:
                raise ConfigError(f"duplicate binding name {binding.name!r} in {self._path}")
            seen.add(binding.name)
            self._bindings.append(binding)

        default = data.get("default")
        if default is not None and not isinstance(default, str):
            raise ConfigError(
                f"'default' must be a string in {self._path}, got {type(default).__name__}"
            )
        self._default = default

    def save(self) -> None:
        """Atomically write the current state back to disk with mode 0o600."""
        if self._path == config.BINDINGS_FILE:
            config.ensure_config_dir()
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "bindings": [b.model_dump() for b in self._bindings],
        }
        if self._default is not None:
            payload["default"] = self._default

        encoded = tomli_w.dumps(payload).encode("utf-8")
        tmp_path = self._path.with_name(f"{self._path.name}.tmp.{os.getpid()}")
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            try:
                dir_fd = os.open(self._path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise ConfigError(f"failed to write bindings file {self._path}: {exc}") from exc

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def list(self) -> list[Binding]:
        self._ensure_loaded()
        return list(self._bindings)

    def get(self, name: str) -> Binding:
        self._ensure_loaded()
        for binding in self._bindings:
            if binding.name == name:
                return binding
        raise BindingNotFound(name)

    def get_default(self) -> Binding:
        self._ensure_loaded()
        if self._default is None:
            raise BindingNotFound("<default>")
        return self.get(self._default)

    def add(self, binding: Binding, *, make_default: bool = False) -> None:
        self._ensure_loaded()
        for existing in self._bindings:
            if existing.name == binding.name:
                raise BindingExists(binding.name)
        self._bindings.append(binding)
        if make_default:
            self._default = binding.name
        self.save()

    def remove(self, name: str) -> None:
        self._ensure_loaded()
        for idx, binding in enumerate(self._bindings):
            if binding.name == name:
                del self._bindings[idx]
                if self._default == name:
                    self._default = None
                self.save()
                return
        raise BindingNotFound(name)

    def set_default(self, name: str) -> None:
        self._ensure_loaded()
        for binding in self._bindings:
            if binding.name == name:
                self._default = name
                self.save()
                return
        raise BindingNotFound(name)
