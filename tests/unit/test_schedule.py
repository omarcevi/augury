import plistlib
import shlex

import click
import pytest

from augury import schedule

PROGRAM = ["/usr/local/bin/augury", "scout"]


def test_launchd_plist_runs_the_scout_daily(tmp_path):
    plist = plistlib.loads(schedule.launchd_plist(PROGRAM, hour=7, minute=30, log_dir=tmp_path))
    assert plist["Label"] == schedule.LABEL and plist["ProgramArguments"] == PROGRAM
    assert plist["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}


def test_systemd_timer_is_persistent():
    service, timer = schedule.systemd_units(PROGRAM, hour=7, minute=0)
    assert "ExecStart=/usr/local/bin/augury scout" in service
    assert "OnCalendar=*-*-* 07:00:00" in timer and "Persistent=true" in timer


def test_install_on_macos_writes_the_plist_and_bootstraps(tmp_path):
    calls: list[list[str]] = []
    result = schedule.install(
        hour=7,
        minute=0,
        log_dir=tmp_path,
        platform="darwin",
        home=tmp_path,
        run=calls.append,
        program=PROGRAM,
    )
    plist_path = tmp_path / "Library" / "LaunchAgents" / f"{schedule.LABEL}.plist"
    assert result.installed == [plist_path] and plist_path.exists()
    assert calls[-1][:2] == ["launchctl", "bootstrap"]


def test_install_on_linux_enables_a_user_timer(tmp_path):
    calls: list[list[str]] = []
    schedule.install(
        hour=7,
        minute=0,
        log_dir=tmp_path,
        platform="linux",
        home=tmp_path,
        run=calls.append,
        program=PROGRAM,
        which=lambda _: "/usr/bin/systemctl",
    )
    assert (tmp_path / ".config/systemd/user/augury-scout.timer").exists()
    assert ["systemctl", "--user", "enable", "--now", "augury-scout.timer"] in calls


def test_elsewhere_prints_a_cron_line(tmp_path):
    result = schedule.install(
        hour=6,
        minute=5,
        log_dir=tmp_path,
        platform="freebsd",
        home=tmp_path,
        run=lambda _: None,
        program=PROGRAM,
    )
    assert result.installed == [] and "5 6 * * * /usr/local/bin/augury scout" in result.message


def test_uninstall_on_macos_removes_the_plist(tmp_path):
    calls: list[list[str]] = []
    schedule.install(
        hour=7,
        minute=0,
        log_dir=tmp_path,
        platform="darwin",
        home=tmp_path,
        run=calls.append,
        program=PROGRAM,
    )
    schedule.uninstall(platform="darwin", home=tmp_path, run=calls.append)
    assert not (tmp_path / "Library" / "LaunchAgents" / f"{schedule.LABEL}.plist").exists()
    assert calls[-1][:2] == ["launchctl", "bootout"]


def test_parse_time():
    assert schedule.parse_time("07:05") == (7, 5)
    with pytest.raises(click.BadParameter):
        schedule.parse_time("25:00")


# --- AUGURY_HOME passthrough: a scheduled job (launchd/systemd/cron) runs in its own,
# mostly-empty environment, so if the user set AUGURY_HOME for a custom data dir, the job
# must carry it along or it'll silently scout into the default location instead. The home
# path below has a space in it -- the case the plain (unquoted) implementation would get
# wrong -- so these tests double as the "spaces in home" coverage the review asked for.
HOME_WITH_SPACE = "/Users/o mar/augury home"


def test_launchd_plist_includes_augury_home_when_set(tmp_path):
    raw = schedule.launchd_plist(
        PROGRAM, hour=7, minute=30, log_dir=tmp_path, augury_home=HOME_WITH_SPACE
    )
    plist = plistlib.loads(raw)  # round-trip through the real plist parser, not a string match
    assert plist["EnvironmentVariables"] == {"AUGURY_HOME": HOME_WITH_SPACE}


def test_launchd_plist_omits_augury_home_when_unset(tmp_path):
    plist = plistlib.loads(schedule.launchd_plist(PROGRAM, hour=7, minute=30, log_dir=tmp_path))
    assert "EnvironmentVariables" not in plist


def test_systemd_service_includes_augury_home_when_set():
    service, _timer = schedule.systemd_units(PROGRAM, hour=7, minute=0, augury_home=HOME_WITH_SPACE)
    assert f'Environment="AUGURY_HOME={HOME_WITH_SPACE}"' in service


def test_systemd_service_omits_augury_home_when_unset():
    service, _timer = schedule.systemd_units(PROGRAM, hour=7, minute=0)
    assert "Environment=" not in service


def test_cron_line_includes_augury_home_when_set(tmp_path):
    line = schedule.cron_line(
        PROGRAM, hour=6, minute=5, log_dir=tmp_path, augury_home=HOME_WITH_SPACE
    )
    assert f"AUGURY_HOME={shlex.quote(HOME_WITH_SPACE)} {shlex.join(PROGRAM)}" in line


def test_cron_line_omits_augury_home_when_unset(tmp_path):
    line = schedule.cron_line(PROGRAM, hour=6, minute=5, log_dir=tmp_path)
    assert "AUGURY_HOME" not in line


def test_install_passes_augury_home_into_the_plist_on_macos(tmp_path):
    result = schedule.install(
        hour=7,
        minute=0,
        log_dir=tmp_path,
        platform="darwin",
        home=tmp_path,
        run=lambda _: None,
        program=PROGRAM,
        augury_home=HOME_WITH_SPACE,
    )
    plist = plistlib.loads(result.installed[0].read_bytes())
    assert plist["EnvironmentVariables"] == {"AUGURY_HOME": HOME_WITH_SPACE}


def test_install_passes_augury_home_into_the_service_on_linux(tmp_path):
    result = schedule.install(
        hour=7,
        minute=0,
        log_dir=tmp_path,
        platform="linux",
        home=tmp_path,
        run=lambda _: None,
        program=PROGRAM,
        which=lambda _: "/usr/bin/systemctl",
        augury_home=HOME_WITH_SPACE,
    )
    service_path = result.installed[0]
    assert f'Environment="AUGURY_HOME={HOME_WITH_SPACE}"' in service_path.read_text()


def test_install_passes_augury_home_into_the_cron_line_elsewhere(tmp_path):
    result = schedule.install(
        hour=6,
        minute=5,
        log_dir=tmp_path,
        platform="freebsd",
        home=tmp_path,
        run=lambda _: None,
        program=PROGRAM,
        augury_home=HOME_WITH_SPACE,
    )
    assert f"AUGURY_HOME={shlex.quote(HOME_WITH_SPACE)}" in result.message


# --- `augury schedule status`: not in the brief's interfaces list, but the task brief (top
# level) names `install|uninstall|status` explicitly and the review checks run it. Status never
# shells out (no `launchctl`/`systemctl` call): it just reports whether the plist/unit files it
# would install are on disk, so it needs no injected `run` to stay test-safe.


def test_status_reports_not_installed_on_macos(tmp_path):
    result = schedule.status(platform="darwin", home=tmp_path)
    assert "not installed" in result.message.lower()


def test_status_reports_installed_on_macos_after_install(tmp_path):
    schedule.install(
        hour=7,
        minute=0,
        log_dir=tmp_path,
        platform="darwin",
        home=tmp_path,
        run=lambda _: None,
        program=PROGRAM,
    )
    result = schedule.status(platform="darwin", home=tmp_path)
    plist_path = tmp_path / "Library" / "LaunchAgents" / f"{schedule.LABEL}.plist"
    assert "installed" in result.message.lower() and str(plist_path) in result.message


def test_status_reports_not_installed_on_linux(tmp_path):
    result = schedule.status(platform="linux", home=tmp_path)
    assert "not installed" in result.message.lower()


def test_status_reports_installed_on_linux_after_install(tmp_path):
    schedule.install(
        hour=7,
        minute=0,
        log_dir=tmp_path,
        platform="linux",
        home=tmp_path,
        run=lambda _: None,
        program=PROGRAM,
        which=lambda _: "/usr/bin/systemctl",
    )
    result = schedule.status(platform="linux", home=tmp_path)
    assert "installed" in result.message.lower()


def test_status_elsewhere_points_at_cron(tmp_path):
    result = schedule.status(platform="freebsd", home=tmp_path)
    assert "cron" in result.message.lower()
