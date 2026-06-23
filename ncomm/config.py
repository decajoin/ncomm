"""Configuration loading for ncomm.

Resolution order for every setting:
  1. Environment variable
  2. Config file (~/.config/ncomm/config.toml or $NCOMM_CONFIG)
  3. Built-in default

Mirrors nlsh's config module so a single mental model covers both tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for 3.9/3.10
    try:
        import tomli as tomllib  # provided as a dependency on <3.11
    except ModuleNotFoundError:
        tomllib = None


DEFAULT_BASE_URL = "https://api.deepseek.com"
# deepseek-chat / deepseek-reasoner are deprecated on 2026-07-24;
# v4 models are the current generation (…-flash is the cheaper/faster tier).
FLASH_MODEL = "deepseek-v4-flash"
PRO_MODEL = "deepseek-v4-pro"
DEFAULT_MODEL = FLASH_MODEL

# Keys we own in the config file and are allowed to (re)write. String-valued
# keys are written quoted; bool-valued keys are written bare (true/false) so the
# file stays idiomatic TOML and a user-set `learn_style = false` survives a
# rewrite triggered by an unrelated `config set-key` / `set-model`.
WRITABLE_KEYS = ("api_key", "base_url", "model")
WRITABLE_BOOL_KEYS = ("learn_style",)


def config_path() -> Path:
    override = os.environ.get("NCOMM_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "ncomm" / "config.toml"


def _load_file() -> dict:
    path = config_path()
    if not path.is_file() or tomllib is None:
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    # Accept either top-level keys or a [deepseek] table.
    section = data.get("deepseek", {})
    merged = {**data, **section}
    return merged


@dataclass
class Config:
    api_key: str | None
    base_url: str
    model: str
    learn_style: bool = True

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    file_cfg = _load_file()
    api_key = (
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("NCOMM_API_KEY")
        or file_cfg.get("api_key")
    )
    base_url = (
        os.environ.get("DEEPSEEK_BASE_URL")
        or file_cfg.get("base_url")
        or DEFAULT_BASE_URL
    )
    model = (
        os.environ.get("NCOMM_MODEL")
        or file_cfg.get("model")
        or DEFAULT_MODEL
    )
    learn_style = _as_bool(
        os.environ.get("NCOMM_LEARN_STYLE"), _as_bool(file_cfg.get("learn_style"), True)
    )
    return Config(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        learn_style=learn_style,
    )


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def save_config(updates: dict) -> Path:
    """Merge `updates` into the config file and write it back (mode 0600).

    Only WRITABLE_KEYS / WRITABLE_BOOL_KEYS are persisted; values of None are
    ignored. Crucially, existing values for keys we don't touch in `updates`
    (e.g. a hand-set `learn_style = false`) are preserved across the rewrite,
    not dropped. The file is rewritten as flat top-level keys, which the loader
    also accepts.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_file()
    current = {k: v for k, v in existing.items() if k in WRITABLE_KEYS}
    # Coerce existing bool keys through _as_bool so a stringy "false" round-trips
    # back out as a bare `false`, not a quoted string.
    current_bools = {
        k: _as_bool(existing[k], True) for k in WRITABLE_BOOL_KEYS if k in existing
    }
    for key, value in updates.items():
        if value is None:
            continue
        if key in WRITABLE_KEYS:
            current[key] = value
        elif key in WRITABLE_BOOL_KEYS:
            current_bools[key] = _as_bool(value, True)

    lines = ["# ncomm configuration\n"]
    for key in WRITABLE_KEYS:
        value = current.get(key)
        if value:
            lines.append(f'{key} = "{_toml_escape(str(value))}"\n')
    for key in WRITABLE_BOOL_KEYS:
        # Use `in`, not truthiness: a stored `false` must still be written out.
        if key in current_bools:
            lines.append(f"{key} = {'true' if current_bools[key] else 'false'}\n")
    path.write_text("".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
