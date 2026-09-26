"""One setting changed in config.toml, from the config page (P14).

config.toml is the user's own file, so an edit changes that one key and nothing else: comments,
order and every other key stay as they are (tomlkit). The file is read again just before each
edit (it may have changed in $EDITOR), the whole of it with the change must pass the same
validation as `load_config` before anything is written, and the write is atomic.
"""

import contextlib
import inspect
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import annotated_types
import tomlkit
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument

from augury.core.config import Config, ConfigError, validate_config
from augury.core.paths import AppPaths

# Config never holds a key (keys live in the environment or .env) -- this is a tripwire in
# case a future section ever grows one, so it can never be echoed, or edited, on the config page.
SECRET_FIELD = re.compile(r"(?:^|_)(?:api_key|key|secret|password|token)$")

EditorKind = Literal["bool", "choice", "number", "text", "file", "secret"]


class MalformedConfig(ConfigError):
    """config.toml can't be read as TOML: nothing is edited until it's fixed."""


def field_info(section: str, field: str) -> FieldInfo | None:
    """The pydantic field behind `section.field`, if it's a field of a section model."""
    outer = Config.model_fields.get(section)
    model = outer.annotation if outer is not None else None
    if not (inspect.isclass(model) and get_origin(model) is None and issubclass(model, BaseModel)):
        return None  # e.g. [pricing], a dict with no fixed fields
    return model.model_fields.get(field)


def editor_kind(section: str, field: str) -> EditorKind:
    """How the config page edits this setting, from the field's type."""
    if SECRET_FIELD.search(field):
        return "secret"
    info = field_info(section, field)
    annotation = info.annotation if info is not None else None
    if annotation is bool:
        return "bool"
    if get_origin(annotation) is Literal:
        return "choice"
    if annotation in (int, float):
        return "number"
    if annotation is str:
        return "text"
    return "file"  # dicts, lists, anything else: config.toml only


def choices(section: str, field: str) -> tuple[str, ...]:
    info = field_info(section, field)
    return tuple(str(v) for v in get_args(info.annotation)) if info is not None else ()


def default_of(section: str, field: str) -> object:
    info = field_info(section, field)
    return info.get_default(call_default_factory=True) if info is not None else None


def _number(value: object) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def bounds(section: str, field: str) -> str:
    """The allowed range from the Field's constraints: both ends as a range (ge=40, le=200 is
    40, an en dash, 200), else the one bound, e.g. "≥ 0" or "> 0"; "" if there's none."""
    info = field_info(section, field)
    low = high = ""
    for rule in info.metadata if info is not None else ():
        if isinstance(rule, annotated_types.Ge):
            low = f"≥ {_number(rule.ge)}"
        elif isinstance(rule, annotated_types.Gt):
            low = f"> {_number(rule.gt)}"
        elif isinstance(rule, annotated_types.Le):
            high = f"≤ {_number(rule.le)}"
        elif isinstance(rule, annotated_types.Lt):
            high = f"< {_number(rule.lt)}"
    if low.startswith("≥") and high.startswith("≤"):
        return f"{low[2:]}\N{EN DASH}{high[2:]}"
    return " and ".join(part for part in (low, high) if part)


def _read(path: Path) -> TOMLDocument:
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError) as e:
        raise MalformedConfig(f"Can't read {path} ({e}). Nothing was changed.") from e
    try:
        tomllib.loads(text)  # what load_config reads it with
        return tomlkit.parse(text)
    except (tomllib.TOMLDecodeError, TOMLKitError) as e:
        raise MalformedConfig(
            f"{path} is not valid TOML ({e}). Nothing was changed: fix it first (press e)."
        ) from e


def ensure_editable(paths: AppPaths) -> None:
    """Raises MalformedConfig when config.toml can't be read as TOML (a missing one is fine)."""
    _read(paths.config_file)


def _validate(text: str, path: Path) -> Config:
    """What `text`, as config.toml, loads as: parsed and validated as load_config does it."""
    try:
        return validate_config(tomllib.loads(text), path)
    except tomllib.TOMLDecodeError as e:  # tomlkit wrote something tomllib reads differently
        raise ConfigError(f"The edit wouldn't be valid TOML ({e}). Nothing was changed.") from e
    except ConfigError as e:
        if not isinstance(cause := e.__cause__, ValidationError):
            raise
        problems = [
            f"{'.'.join(str(part) for part in err['loc']) or '(top level)'}: {err['msg']}"
            for err in cause.errors()
        ]
        raise ConfigError("; ".join(problems)) from cause


def _normalize(section: str, field: str, value: object) -> object:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if (section, field) == ("export", "path") and value:  # the same rule as `augury init`
        target = Path(value).expanduser()
        if not (target.is_dir() or target.parent.is_dir()):  # a new leaf folder is fine
            raise ConfigError(f"{value} isn't a directory (and neither is its parent).")
        value = str(target)
    return value


def _table(doc: TOMLDocument, section: str, path: Path) -> MutableMapping[str, Any] | None:
    table = doc.get(section)
    if table is not None and not isinstance(table, MutableMapping):
        raise ConfigError(f"{section} in {path} isn't a table. Nothing was changed: press e.")
    return table


def _with_value(paths: AppPaths, section: str, field: str, value: object) -> tuple[str, Config]:
    """config.toml's new text with the change, and the config that text loads as."""
    path = paths.config_file
    doc = _read(path)
    value = _normalize(section, field, value)
    table = _table(doc, section, path)
    if table is None:
        doc[section] = tomlkit.table()
        table = _table(doc, section, path)
        assert table is not None
    table[field] = value  # as typed: pydantic parses "100" for an int field, as for any file
    config = _validate(tomlkit.dumps(doc), path)
    table[field] = getattr(getattr(config, section), field)  # ...but it's written as 100
    text = tomlkit.dumps(doc)
    return text, _validate(text, path)  # exactly what will be written


def check_value(paths: AppPaths, section: str, field: str, value: object) -> Config:
    """What config.toml would load as with this change, or a ConfigError; writes nothing."""
    return _with_value(paths, section, field, value)[1]


def set_value(paths: AppPaths, section: str, field: str, value: object) -> Config:
    """Write `section.field = value` to config.toml (created if missing) and return the config
    it now loads as. Raises ConfigError (nothing written) if the result wouldn't load."""
    text, config = _with_value(paths, section, field, value)
    _write(paths.config_file, text)
    return config


def reset_value(paths: AppPaths, section: str, field: str) -> Config | None:
    """Remove `section.field` from config.toml, and its section if that leaves it empty, so the
    built-in default applies. None (nothing written) when the file doesn't set it."""
    path = paths.config_file
    if not path.exists():
        return None
    doc = _read(path)
    table = _table(doc, section, path)
    if table is None or field not in table:
        return None
    del table[field]
    if not table:
        del doc[section]
    text = tomlkit.dumps(doc)
    config = _validate(text, path)
    _write(path, text)
    return config


def _write(path: Path, text: str) -> None:
    """Atomically: a temp file in the same dir, then os.replace. A symlinked config.toml (a
    dotfiles repo, say) stays a link: the file it points to is the one replaced."""
    target = path.resolve()
    tmp: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp, target)
    except OSError as e:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise ConfigError(f"Couldn't write {path}: {e}") from e
