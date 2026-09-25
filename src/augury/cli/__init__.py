import asyncio
import os

import click

from augury import __version__, init_wizard
from augury import doctor as doctor_checks
from augury import schedule as scheduling
from augury.agents.scout import ScoutDeps, ScoutReport, recover_interrupted_runs, run_scout
from augury.core.clock import utcnow
from augury.core.config import ConfigError, load_config
from augury.core.db.open import open_db
from augury.core.lock import ScoutAlreadyRunning
from augury.core.paths import HOME_ENV, app_paths
from augury.sources.http import PoliteClient


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="augury")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Augury: a terminal-native AI digest reader."""
    if ctx.invoked_subcommand is None:
        _run_tui()


def _run_tui() -> None:
    from augury.tui.app import AuguryApp  # imported lazily so plain CLI commands stay quick

    paths = app_paths()
    try:
        config = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    conn = open_db(paths)
    try:
        AuguryApp(conn=conn, config=config, paths=paths).run()
    finally:
        conn.close()


@main.command()
@click.option("--offline", is_flag=True, help="Skip checks that need the network.")
def doctor(offline: bool) -> None:
    """Check paths, config, database and (unless --offline) network access."""
    paths = app_paths()
    checks = doctor_checks.run_checks(paths)
    if not offline:
        try:
            http_cfg = load_config(paths).http
        except ConfigError:
            http_cfg = None

        async def network() -> list[doctor_checks.Check]:
            async with PoliteClient(http_cfg) as http:
                return await doctor_checks.check_network(http)

        checks += asyncio.run(network())
    click.echo(doctor_checks.format_checks(checks))
    if doctor_checks.failed(checks):
        raise SystemExit(1)


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Accept the defaults without asking.")
def init(yes: bool) -> None:
    """First-run setup: your interests, sources and (optionally) a Markdown export folder."""
    paths = app_paths()
    conn = open_db(paths)
    try:
        answers = init_wizard.defaults() if yes else init_wizard.ask()
        existing = [p for p in (paths.config_file, paths.interests_file) if p.exists()]
        overwrite = (
            bool(existing)
            and not yes
            and click.confirm(
                f"{', '.join(p.name for p in existing)} already "
                f"{'exists' if len(existing) == 1 else 'exist'}. Overwrite?",
                default=False,
            )
        )
        written = init_wizard.apply(paths, answers, conn=conn, overwrite=overwrite)
    finally:
        conn.close()
    for path in written:
        click.echo(f"wrote {path}")
    click.echo("Next: `augury scout` to fetch today's items, then `augury` to read them.")


def format_report(report: ScoutReport) -> str:
    lines = [
        f"Scout {report.status} · {report.new_items} new"
        + (f" · {report.enriched} enriched" if report.enriched else "")
    ]
    for source_id, s in sorted(report.sources.items()):
        if s.error:
            lines.append(f"  ✗ {source_id:<16} {s.error}")
        elif s.not_modified:
            lines.append(f"  ✓ {source_id:<16} not modified")
        else:
            extra = f" · {s.skipped} skipped" if s.skipped else ""
            lines.append(f"  ✓ {source_id:<16} {s.fetched} fetched · {s.new} new{extra}")
    return "\n".join(lines)


@main.command()
@click.option("--source", "only", metavar="ID", help="Scout only this source.")
def scout(only: str | None) -> None:
    """Fetch new items from every enabled source."""
    paths = app_paths()
    try:
        config = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    conn = open_db(paths)
    recover_interrupted_runs(conn, paths.scout_lock_file, now=utcnow())

    async def go() -> ScoutReport:
        async with PoliteClient(config.http) as http:
            return await run_scout(
                ScoutDeps(conn=conn, http=http, config=config, lock_path=paths.scout_lock_file),
                only=only,
            )

    try:
        report = asyncio.run(go())
    except (ScoutAlreadyRunning, ValueError) as e:
        raise click.ClickException(str(e)) from e
    finally:
        conn.close()
    click.echo(format_report(report))
    if report.status == "failed":
        raise SystemExit(1)


@main.group("schedule")
def schedule_group() -> None:
    """Run a daily scout in the background (launchd, systemd or cron)."""


@schedule_group.command("install")
@click.option("--time", "at", default="07:00", show_default=True, help="Local time, HH:MM.")
def schedule_install(at: str) -> None:
    """Install a daily scout via launchd (macOS), a systemd user timer (Linux), or print a
    cron line to add yourself."""
    hour, minute = scheduling.parse_time(at)
    paths = app_paths()
    paths.ensure()
    # Carry a custom AUGURY_HOME into the job's own environment: it runs detached from this
    # shell (launchd/systemd/cron all start with a near-empty environment), so without this
    # a scheduled scout would silently use the default data dir instead of the user's.
    augury_home = os.environ.get(HOME_ENV)
    click.echo(
        scheduling.install(
            hour=hour, minute=minute, log_dir=paths.log_dir, augury_home=augury_home
        ).message
    )


@schedule_group.command("uninstall")
def schedule_uninstall() -> None:
    """Remove a previously installed daily scout."""
    click.echo(scheduling.uninstall().message)


@schedule_group.command("status")
def schedule_status() -> None:
    """Show whether a daily scout is currently installed."""
    click.echo(scheduling.status().message)


from augury.cli.dev import dev_group  # noqa: E402
from augury.cli.sources import sources_group  # noqa: E402

main.add_command(sources_group)
main.add_command(dev_group)
