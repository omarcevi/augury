import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import click
import tomli_w
import yaml

from augury.core.db.sources_repo import SourcesRepo
from augury.core.paths import AppPaths


@dataclass
class InitAnswers:
    audience: str = ""
    topics: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    community: bool = True
    export_path: str = ""


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def defaults() -> InitAnswers:
    return InitAnswers()


def ask() -> InitAnswers:
    audience = click.prompt(
        "Who do you read for? (e.g. ML engineers who ship to production)",
        default="",
        show_default=False,
    ).strip()
    topics = _split(
        click.prompt("Topics you care about (comma-separated)", default="", show_default=False)
    )
    avoid = _split(click.prompt("Topics to skip (comma-separated)", default="", show_default=False))
    community = click.confirm(
        "Include Hugging Face community posts? (busier, more self-promotion)", default=True
    )
    export = ""
    while True:
        export = click.prompt(
            "Folder for Markdown digests (used from M2; e.g. a folder inside your Obsidian vault;"
            " blank to skip)",
            default="",
            show_default=False,
        ).strip()
        target = Path(export).expanduser()
        if not export or target.is_dir() or target.parent.is_dir():  # a new leaf folder is fine
            break
        click.echo(f"{export} isn't a directory (and neither is its parent).")
    return InitAnswers(
        audience,
        topics,
        avoid,
        community,
        str(Path(export).expanduser()) if export else "",
    )


def apply(
    paths: AppPaths, answers: InitAnswers, *, conn: sqlite3.Connection, overwrite: bool
) -> list[Path]:
    paths.ensure()
    written: list[Path] = []
    interests = {"audience": answers.audience, "topics": answers.topics, "avoid": answers.avoid}
    config = {"export": {"path": answers.export_path}}
    for path, body in (
        (paths.interests_file, yaml.safe_dump(interests, sort_keys=False, allow_unicode=True)),
        (paths.config_file, tomli_w.dumps(config)),
    ):
        if overwrite or not path.exists():
            path.write_text(body, encoding="utf-8")
            written.append(path)
    if written:
        SourcesRepo(conn).set_enabled("hf-community", answers.community)
    return written
