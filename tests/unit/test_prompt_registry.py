import pytest

from augury.llm.prompt_registry import PromptError, available_prompts, load_prompt, parse_prompt


def test_frontmatter_and_body():
    template = parse_prompt("x", "---\nprompt_version: 3\n---\nDo the thing.\n")
    assert (template.name, template.version, template.system) == ("x", 3, "Do the thing.")


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Do the thing.", "frontmatter"),
        ("---\nprompt_version: 1\nDo the thing.", "not closed"),
        ("---\nprompt_version: 0\n---\nx", "positive integer"),
        ("---\nprompt_version: '1'\n---\nx", "positive integer"),
        ("---\nprompt_version: true\n---\nx", "positive integer"),
        ("---\nprompt_version: 1\n---\n   \n", "empty"),
    ],
)
def test_malformed_prompts_are_rejected(text, problem):
    with pytest.raises(PromptError, match=problem):
        parse_prompt("x", text)


def test_every_packaged_prompt_loads():
    names = available_prompts()
    assert "probe" in names
    for name in names:
        assert load_prompt(name).version >= 1


def test_an_unknown_prompt_is_an_error():
    with pytest.raises(PromptError, match="nope"):
        load_prompt("nope")
