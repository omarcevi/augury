"""Versioned instructions in llm/prompts/<name>.md (spec §5.8). Each file starts with a YAML
frontmatter block holding `prompt_version`, which is stored with every output and used in
cache keys. Bump it whenever the instruction changes meaningfully."""

from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from importlib.resources.abc import Traversable

import yaml


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: int
    system: str


def parse_prompt(name: str, text: str) -> PromptTemplate:
    if not text.startswith("---\n"):
        raise PromptError(f"{name}: must start with a '---' frontmatter block")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise PromptError(f"{name}: the frontmatter is not closed with '---'")
    meta = yaml.safe_load(head)
    version = meta.get("prompt_version") if isinstance(meta, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PromptError(f"{name}: prompt_version must be a positive integer")
    system = body.strip()
    if not system:
        raise PromptError(f"{name}: the prompt body is empty")
    return PromptTemplate(name, version, system)


def _root() -> Traversable:
    return files("augury.llm").joinpath("prompts")


@cache
def load_prompt(name: str) -> PromptTemplate:
    resource = _root().joinpath(f"{name}.md")
    if not resource.is_file():
        raise PromptError(f"no packaged prompt named {name!r}")
    return parse_prompt(name, resource.read_text(encoding="utf-8"))


def available_prompts() -> list[str]:
    return sorted(e.name.removesuffix(".md") for e in _root().iterdir() if e.name.endswith(".md"))
