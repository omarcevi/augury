from click.testing import CliRunner

import augury.cli
from augury.agents.scout import ScoutReport
from augury.cli import format_report, main


def test_export_without_a_path_explains_how_to_turn_it_on(paths):
    result = CliRunner().invoke(main, ["export"])
    assert result.exit_code == 1 and "[export] path" in result.output


def test_export_writes_the_requested_day(paths, tmp_path):
    paths.config_file.write_text(f'[export]\npath = "{tmp_path / "out"}"\n')
    result = CliRunner().invoke(main, ["export", "--day", "2026-09-24"])
    assert result.exit_code == 0, result.output
    written = tmp_path / "out" / "Daily" / "2026-09-24.md"
    assert written.exists() and "No digest for this day yet" in written.read_text()


def test_a_bad_day_is_refused(paths, tmp_path):
    paths.config_file.write_text(f'[export]\npath = "{tmp_path / "out"}"\n')
    result = CliRunner().invoke(main, ["export", "--day", "yesterday"])
    assert result.exit_code == 2 and "YYYY-MM-DD" in result.output


def test_an_unwritable_folder_is_a_clean_error(paths, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    paths.config_file.write_text(f'[export]\npath = "{blocker}"\n')
    result = CliRunner().invoke(main, ["export"])
    assert result.exit_code == 1 and "could not write the export" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_any_export_failure_is_a_clean_error(paths, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("bad \x1b[2Jrow")  # not an OSError: still no traceback

    monkeypatch.setattr(augury.cli, "export_daily", broken)
    paths.config_file.write_text(f'[export]\npath = "{tmp_path / "out"}"\n')
    result = CliRunner().invoke(main, ["export"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "could not write the export: ValueError: bad row" in result.output
    assert "\x1b" not in result.output and "Traceback" not in result.output


def test_the_report_shows_the_export_line_only_when_it_ran():
    report = ScoutReport(run_id="scout-1", status="ok", sources={}, new_items=0)
    assert "export" not in format_report(report)  # [export] path empty: export is off
    done = report.model_copy(update={"exported": "/notes/Daily/2026-09-25.md"})
    assert "✓ export           /notes/Daily/2026-09-25.md" in format_report(done)
    failed = report.model_copy(update={"export_error": "NotADirectoryError: \x1b[2Jnope"})
    text = format_report(failed)
    assert "✗ export           NotADirectoryError: nope" in text and "\x1b" not in text
