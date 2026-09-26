import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import click
import tomli_w
import yaml

from augury.core.config import DEFAULT_MODELS
from augury.core.db.sources_repo import SourcesRepo
from augury.core.paths import AppPaths
from augury.core.secrets import ENV_NAME, write_env_file

PROVIDERS = ("gemini", "vertex_ai", "litellm", "none")


@dataclass
class InitAnswers:
    audience: str = ""
    topics: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    community: bool = True
    export_path: str = ""
    provider: str = "gemini"
    key_env: str = ""  # the variable the key is stored under in .env
    api_key: str = field(default="", repr=False)  # never printed or logged
    models: dict[str, str] = field(default_factory=dict)  # [models]; empty keeps the defaults
    google_project: str = ""
    google_location: str = ""


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def defaults() -> InitAnswers:
    return InitAnswers()


def _ask_spec(label: str) -> str:
    while True:
        value = click.prompt(label).strip()
        provider, sep, name = value.partition("/")
        if provider and sep and name:
            return value
        click.echo("Use provider/model, e.g. openai/<model-id> or ollama_chat/<model-id>.")


def _ask_env_name() -> str:
    while True:
        name = click.prompt(
            "Environment variable for its API key (e.g. OPENAI_API_KEY; blank if none)",
            default="",
            show_default=False,
        ).strip()
        if not name or ENV_NAME.match(name):
            return name
        click.echo("Use letters, digits and underscores, e.g. OPENAI_API_KEY.")


def _ask_provider(answers: InitAnswers) -> None:
    answers.provider = click.prompt(
        "AI provider for triage and TL;DRs (gemini, vertex_ai, litellm, none)",
        type=click.Choice(PROVIDERS),
        default="gemini",
        show_choices=False,
    )
    if answers.provider == "gemini":
        answers.key_env = "GEMINI_API_KEY"
        answers.api_key = click.prompt(
            "Gemini API key (from https://aistudio.google.com/apikey; blank to skip)",
            default="",
            show_default=False,
            hide_input=True,
        ).strip()
    elif answers.provider == "vertex_ai":  # Vertex uses Google Cloud credentials, not a key
        answers.google_project = click.prompt("Google Cloud project id").strip()
        answers.google_location = click.prompt("Vertex AI location", default="global").strip()
        answers.models = {
            role: "vertex_ai/" + spec.partition("/")[2] for role, spec in DEFAULT_MODELS.items()
        }
    elif answers.provider == "litellm":
        answers.models = {
            "fast": _ask_spec("Model for triage and TL;DRs (provider/model)"),
            "smart": _ask_spec("Model for discovery and Ask (provider/model)"),
        }
        answers.key_env = _ask_env_name()
        if answers.key_env:
            answers.api_key = click.prompt(
                f"{answers.key_env} value", default="", show_default=False, hide_input=True
            ).strip()


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
            "Folder for Markdown digests (e.g. a folder inside your Obsidian vault; blank to skip)",
            default="",
            show_default=False,
        ).strip()
        target = Path(export).expanduser()
        if not export or target.is_dir() or target.parent.is_dir():  # a new leaf folder is fine
            break
        click.echo(f"{export} isn't a directory (and neither is its parent).")
    answers = InitAnswers(
        audience, topics, avoid, community, str(Path(export).expanduser()) if export else ""
    )
    _ask_provider(answers)
    return answers


def apply(
    paths: AppPaths, answers: InitAnswers, *, conn: sqlite3.Connection, overwrite: bool
) -> list[Path]:
    paths.ensure()
    written: list[Path] = []
    interests = {"audience": answers.audience, "topics": answers.topics, "avoid": answers.avoid}
    config: dict[str, object] = {"export": {"path": answers.export_path}}
    if answers.models:
        config["models"] = dict(answers.models)
    if answers.google_project:
        config["google"] = {
            "project": answers.google_project,
            "location": answers.google_location,
        }
    for path, body in (
        (paths.interests_file, yaml.safe_dump(interests, sort_keys=False, allow_unicode=True)),
        (paths.config_file, tomli_w.dumps(config)),
    ):
        if overwrite or not path.exists():
            path.write_text(body, encoding="utf-8")
            written.append(path)
    if written:  # only when the answers were actually written (M1 ruling)
        SourcesRepo(conn).set_enabled("hf-community", answers.community)
    if answers.api_key and answers.key_env:  # a key typed now is always saved
        write_env_file(paths.env_file, {answers.key_env: answers.api_key})
        written.append(paths.env_file)
    return written
