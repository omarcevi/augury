from click.testing import CliRunner

from augury.cli import main
from augury.core.config import load_config, load_interests
from augury.core.db.open import open_db
from augury.core.db.sources_repo import SourcesRepo


def test_wizard_writes_valid_files_and_applies_choices(paths, tmp_path):
    export_dir = tmp_path / "vault" / "Augury"  # a new folder inside an existing one is accepted
    export_dir.parent.mkdir()
    answers = f"ML engineers\nagents, rag\ncrypto\nn\n{export_dir}\n"
    result = CliRunner().invoke(main, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    interests = load_interests(paths)
    assert (interests.audience, interests.topics, interests.avoid) == (
        "ML engineers",
        ["agents", "rag"],
        ["crypto"],
    )
    config = load_config(paths)
    assert config.export.path == str(export_dir)
    assert SourcesRepo(open_db(paths)).get("hf-community").source.enabled is False  # type: ignore[union-attr]
    assert "augury scout" in result.output


def test_a_bad_export_path_is_asked_again(paths, tmp_path):
    answers = f"\n\n\ny\n/definitely/not/here\n{tmp_path}\n"
    result = CliRunner().invoke(main, ["init"], input=answers)
    assert result.exit_code == 0 and "isn't a directory" in result.output


def test_existing_files_are_kept_unless_confirmed(paths):
    paths.config_file.write_text('[tui]\ntheme = "nord"\n')
    result = CliRunner().invoke(main, ["init"], input="\n\n\ny\n\nn\n")
    assert result.exit_code == 0
    assert load_config(paths).tui.theme == "nord"


def test_yes_accepts_defaults_without_touching_existing_files(paths):
    paths.interests_file.write_text("topics: [agents]\n")
    result = CliRunner().invoke(main, ["init", "--yes"])
    assert result.exit_code == 0
    assert load_interests(paths).topics == ["agents"]
    assert paths.config_file.exists()


def test_community_disabled_is_kept_after_yes_flag(paths):
    """Once user disables community, --yes should not re-enable it."""
    # First run: disable community
    answers = "ML engineers\nagents\n\nn\n\n"
    result = CliRunner().invoke(main, ["init"], input=answers)
    assert result.exit_code == 0
    assert SourcesRepo(open_db(paths)).get("hf-community").source.enabled is False  # type: ignore[union-attr]

    # Second run: --yes should not touch the database (no files written)
    result = CliRunner().invoke(main, ["init", "--yes"])
    assert result.exit_code == 0
    assert SourcesRepo(open_db(paths)).get("hf-community").source.enabled is False  # type: ignore[union-attr]


def test_overwrite_existing_files_with_new_answers(paths):
    """Interactive init with overwrite confirmation replaces files."""
    # First run
    paths.config_file.write_text('[tui]\ntheme = "nord"\n')
    paths.interests_file.write_text("audience: old\n")

    # Second run: wizard asks for answers first, then overwrite confirmation
    # Order: audience, topics, avoid, community, export_path, then overwrite prompt
    answers = "new audience\nnew topic\n\ny\n\ny\n"
    result = CliRunner().invoke(main, ["init"], input=answers)
    assert result.exit_code == 0, result.output

    # Check that new answers are written
    interests = load_interests(paths)
    assert interests.audience == "new audience"
    assert interests.topics == ["new topic"]

    # Check that existing tui config is overwritten (not preserved)
    config = load_config(paths)
    assert config.tui.theme == "textual-dark"  # reset to default from new apply
