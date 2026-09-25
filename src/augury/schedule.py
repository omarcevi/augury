import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import click

from augury.core.paths import HOME_ENV

LABEL = "dev.augury.scout"
SYSTEMD_NAME = "augury-scout"
Runner = Callable[[list[str]], object]


def _run(cmd: list[str]) -> object:
    return subprocess.run(cmd, check=False, capture_output=True)


@dataclass
class ScheduleResult:
    message: str
    installed: list[Path] = field(default_factory=list)
    ok: bool = True


def _failure(result: object) -> str | None:
    """What went wrong, or None. An injected test runner may return None: that's success."""
    code = getattr(result, "returncode", 0)
    if not code:
        return None
    stderr = getattr(result, "stderr", b"") or b""
    detail = stderr.decode(errors="replace") if isinstance(stderr, bytes) else str(stderr)
    return f"exit {code}: {detail.strip()}" if detail.strip() else f"exit {code}"


def parse_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except ValueError:
        raise click.BadParameter("use HH:MM, e.g. 07:00") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise click.BadParameter("use HH:MM, e.g. 07:00")
    return hour, minute


def scout_command() -> list[str]:
    exe = shutil.which("augury")
    return [exe, "scout"] if exe else [sys.executable, "-m", "augury", "scout"]


def _systemd_quote(assignment: str) -> str:
    """Quote a NAME=value assignment the way systemd unit files want it (systemd.service(5)):
    the whole assignment wrapped in double quotes, with any literal backslash/quote in the
    value escaped so it can't break out of them. This is not shell quoting -- systemd parses
    unit files with its own rules."""
    escaped = assignment.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def launchd_plist(
    program: list[str],
    *,
    hour: int,
    minute: int,
    log_dir: Path,
    augury_home: str | None = None,
) -> bytes:
    # launchd runs a missed calendar job when the Mac wakes, so a sleeping laptop still scouts.
    log = str(log_dir / "scout.log")
    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": program,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "RunAtLoad": False,
    }
    if augury_home:  # otherwise the scheduled run would use the default data dir instead
        plist["EnvironmentVariables"] = {HOME_ENV: augury_home}
    return plistlib.dumps(plist)


def systemd_units(
    program: list[str], *, hour: int, minute: int, augury_home: str | None = None
) -> tuple[str, str]:
    env_line = f"Environment={_systemd_quote(f'{HOME_ENV}={augury_home}')}\n" if augury_home else ""
    service = (
        "[Unit]\nDescription=augury scout\n\n"
        f"[Service]\nType=oneshot\n{env_line}ExecStart={shlex.join(program)}\n"
    )
    timer = (
        "[Unit]\nDescription=Daily augury scout\n\n"
        f"[Timer]\nOnCalendar=*-*-* {hour:02d}:{minute:02d}:00\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def cron_line(
    program: list[str],
    *,
    hour: int,
    minute: int,
    log_dir: Path,
    augury_home: str | None = None,
) -> str:
    env_prefix = f"{HOME_ENV}={shlex.quote(augury_home)} " if augury_home else ""
    return (
        f"{minute} {hour} * * * {env_prefix}{shlex.join(program)}"
        f" >> {shlex.quote(str(log_dir / 'scout.log'))} 2>&1"
    )


def _plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _unit_dir(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def install(
    *,
    hour: int,
    minute: int,
    log_dir: Path,
    platform: str = sys.platform,
    home: Path | None = None,
    run: Runner = _run,
    program: list[str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    augury_home: str | None = None,
) -> ScheduleResult:
    home = home or Path.home()
    program = program or scout_command()
    if platform == "darwin":
        path = _plist_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            launchd_plist(
                program, hour=hour, minute=minute, log_dir=log_dir, augury_home=augury_home
            )
        )
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", domain, str(path)])  # fine if it wasn't loaded yet
        if error := _failure(run(["launchctl", "bootstrap", domain, str(path)])):
            return ScheduleResult(
                f"Wrote {path}, but `launchctl bootstrap` failed ({error}).", [path], ok=False
            )
        return ScheduleResult(
            f"Daily scout at {hour:02d}:{minute:02d} via launchd "
            "(it runs on wake if the Mac was asleep).",
            [path],
        )
    if platform.startswith("linux") and which("systemctl"):
        unit_dir = _unit_dir(home)
        unit_dir.mkdir(parents=True, exist_ok=True)
        service, timer = systemd_units(program, hour=hour, minute=minute, augury_home=augury_home)
        service_path = unit_dir / f"{SYSTEMD_NAME}.service"
        timer_path = unit_dir / f"{SYSTEMD_NAME}.timer"
        service_path.write_text(service)
        timer_path.write_text(timer)
        for cmd in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_NAME}.timer"],
        ):
            if error := _failure(run(cmd)):
                return ScheduleResult(
                    f"Wrote {timer_path}, but `{shlex.join(cmd)}` failed ({error}).",
                    [service_path, timer_path],
                    ok=False,
                )
        return ScheduleResult(
            f"Daily scout at {hour:02d}:{minute:02d} via a systemd user timer "
            "(Persistent=true catches up after downtime).",
            [service_path, timer_path],
        )
    return ScheduleResult(
        "Add this line to your crontab (crontab -e):\n"
        + cron_line(program, hour=hour, minute=minute, log_dir=log_dir, augury_home=augury_home)
    )


def uninstall(
    *, platform: str = sys.platform, home: Path | None = None, run: Runner = _run
) -> ScheduleResult:
    home = home or Path.home()
    if platform == "darwin":
        path = _plist_path(home)
        run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
        path.unlink(missing_ok=True)
        return ScheduleResult("Removed the launchd job.")
    if platform.startswith("linux"):
        run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_NAME}.timer"])
        for suffix in ("service", "timer"):
            (_unit_dir(home) / f"{SYSTEMD_NAME}.{suffix}").unlink(missing_ok=True)
        return ScheduleResult("Removed the systemd user timer.")
    return ScheduleResult("Remove the augury line from your crontab (crontab -e).")


def status(*, platform: str = sys.platform, home: Path | None = None) -> ScheduleResult:
    """Report whether a daily scout is currently installed, without shelling out to
    launchctl/systemctl -- the on-disk plist/unit files are enough to answer "installed?",
    and it keeps this call (unlike install/uninstall) safe to run with no injected `run`."""
    home = home or Path.home()
    if platform == "darwin":
        path = _plist_path(home)
        if path.exists():
            return ScheduleResult(f"Installed: {path}")
        return ScheduleResult("Not installed. Run `augury schedule install` to add a daily scout.")
    if platform.startswith("linux"):
        service_path = _unit_dir(home) / f"{SYSTEMD_NAME}.service"
        timer_path = _unit_dir(home) / f"{SYSTEMD_NAME}.timer"
        if service_path.exists() and timer_path.exists():
            return ScheduleResult(f"Installed: {timer_path}")
        return ScheduleResult("Not installed. Run `augury schedule install` to add a daily scout.")
    return ScheduleResult(
        "Not managed here; add a cron line yourself (see `augury schedule install`)."
    )
