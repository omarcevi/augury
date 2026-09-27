import asyncio
import os
from datetime import date

import click

from augury import __version__, init_wizard
from augury import doctor as doctor_checks
from augury import schedule as scheduling
from augury.agents.scout import ScoutDeps, ScoutReport, recover_interrupted_runs, run_scout
from augury.cli.output import safe
from augury.core.clock import local_day, utcnow
from augury.core.config import (
    Config,
    ConfigError,
    Interests,
    load_config,
    load_interests,
    load_raw_toml,
)
from augury.core.db.open import open_db
from augury.core.lock import ScoutAlreadyRunning
from augury.core.paths import HOME_ENV, app_paths
from augury.core.secrets import load_env_file
from augury.export.markdown import export_daily, last_scout_run_id
from augury.llm.resolver import default_resolver
from augury.sources.http import PoliteClient


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="augury")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Augury: a terminal-native AI digest reader."""
    load_env_file(app_paths())  # before any command, so every model sees the keys
    if ctx.invoked_subcommand is None:
        _run_tui()


def _run_tui() -> None:
    from augury.tui.app import AuguryApp  # imported lazily so plain CLI commands stay quick

    paths = app_paths()
    try:
        config = load_config(paths)
        interests = load_interests(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    conn = open_db(paths)
    try:
        AuguryApp(conn=conn, config=config, paths=paths, interests=interests).run()
    finally:
        conn.close()


@main.command()
@click.option(
    "--offline", is_flag=True, help="Skip checks that need the network (and model probes)."
)
def doctor(offline: bool) -> None:
    """Check paths, config, database, the AI models and (unless --offline) the network."""
    paths = app_paths()
    checks = doctor_checks.run_checks(paths)
    try:
        config: Config | None = load_config(paths)
    except ConfigError:
        config = None  # the config check above already explains it
    if config is not None:
        resolver = default_resolver(config)
        probes = None
        if not offline:
            probes = asyncio.run(doctor_checks.probe_models(paths, config, resolver))
        checks += doctor_checks.check_ai(paths, config, resolver, probes)
        checks += doctor_checks.check_search(config, resolver)
        checks += doctor_checks.check_embedder(config)
    if not offline:

        async def network() -> list[doctor_checks.Check]:
            async with PoliteClient(config.http if config else None) as http:
                return await doctor_checks.check_network(http)

        checks += asyncio.run(network())
    click.echo(safe(doctor_checks.format_checks(checks)))  # M1.1: keep safe()
    if doctor_checks.failed(checks):
        raise SystemExit(1)


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Accept the defaults without asking.")
def init(yes: bool) -> None:
    """First-run setup: your interests, sources and (optionally) a Markdown export folder."""
    paths = app_paths()
    conn = open_db(paths)
    try:
        answers = init_wizard.defaults() if yes else init_wizard.ask(paths)
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


@main.command()
@click.option("--path", "show_path", is_flag=True, help="Print only the config.toml path.")
def config(show_path: bool) -> None:
    """Show effective settings and their source, paths, source health, schedule and versions."""
    paths = app_paths()
    if show_path:
        click.echo(str(paths.config_file))
        return
    from augury.core.config_edit import load_raw_interests
    from augury.tui import ui_state  # lazy: keeps plain CLI commands quick
    from augury.tui.widgets.config_view import build_config_report, render_config_text

    try:
        cfg = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    try:
        interests: Interests | None = load_interests(paths)
    except ConfigError as e:  # the rest of the report still helps; its rows are left out
        interests = None
        click.echo(safe(f"interests.yaml isn't shown: {e}"), err=True)
    conn = open_db(paths)
    try:
        # The theme the app runs with: the one picked with `t` beats config.toml's.
        saved = ui_state.load(paths).theme
        report = build_config_report(
            conn,
            cfg,
            paths,
            load_raw_toml(paths),
            saved_theme=saved,
            interests=interests,
            raw_interests=load_raw_interests(paths),
        )
    finally:
        conn.close()
    click.echo(safe(render_config_text(report)))


def format_report(report: ScoutReport) -> str:
    lines = [
        f"Scout {report.status} · {report.new_items} new"
        + (f" · {report.enriched} enriched" if report.enriched else "")
        + (" · enrichment failed" if report.enrich_error else "")
    ]
    for source_id, s in sorted(report.sources.items()):
        if s.error:
            lines.append(f"  ✗ {source_id:<16} {safe(s.error)}")
        elif s.not_modified:
            lines.append(f"  ✓ {source_id:<16} not modified")
        else:
            extra = f" · {s.skipped} skipped" if s.skipped else ""
            lines.append(f"  ✓ {source_id:<16} {s.fetched} fetched · {s.new} new{extra}")
    if report.enrich_error:
        lines.append(f"  ! {'enrichment':<16} {safe(report.enrich_error)}")
    lines.extend(_triage_lines(report))
    lines.extend(_prefetch_lines(report))
    lines.extend(_export_lines(report))
    return "\n".join(lines)


def _triage_lines(report: ScoutReport) -> list[str]:
    t = report.triage
    if t is None or not t.attempted or t.degraded_kind == "not_configured":
        return []  # no key: the report stays exactly as it was in M1
    if t.degraded:
        return [
            f"  ⚠ {'triage':<16} {t.triaged}/{t.attempted} items"
            f" · ranking degraded: {safe(t.degraded)}"
        ]
    extra = (f" · {t.hidden} hidden by triage" if t.hidden else "") + (
        f" · {t.failed} failed" if t.failed else ""
    )
    return [f"  ✓ {'triage':<16} {t.triaged} items{extra}"]


def _prefetch_lines(report: ScoutReport) -> list[str]:
    p = report.prefetch
    if p is None or not (p.extracted or p.stopped or p.error):
        return []
    mark = "⚠" if p.stopped or p.error else "✓"
    line = f"  {mark} {'prefetch':<16} {p.extracted} read ahead · {p.summarized} TL;DRs"
    if p.stopped:
        line += f" · stopped: {safe(p.stopped)}"
    elif p.error:
        line += f" · {safe(p.error)}"
    return [line]


def _export_lines(report: ScoutReport) -> list[str]:
    if report.export_error:
        return [f"  ✗ {'export':<16} {safe(report.export_error)}"]
    return [f"  ✓ {'export':<16} {safe(report.exported)}"] if report.exported else []


@main.command()
@click.option("--source", "only", metavar="ID", help="Scout only this source.")
def scout(only: str | None) -> None:
    """Fetch new items from every enabled source."""
    paths = app_paths()
    try:
        config = load_config(paths)
        interests = load_interests(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    conn = open_db(paths)
    recover_interrupted_runs(conn, paths.scout_lock_file, now=utcnow())

    async def go() -> ScoutReport:
        async with PoliteClient(config.http) as http:
            return await run_scout(
                ScoutDeps(
                    conn=conn,
                    http=http,
                    config=config,
                    lock_path=paths.scout_lock_file,
                    interests=interests,
                ),
                only=only,
            )

    try:
        report = asyncio.run(go())
    except (ScoutAlreadyRunning, ValueError) as e:
        raise click.ClickException(safe(e)) from e
    finally:
        conn.close()
    click.echo(format_report(report))
    if report.status == "failed":
        raise SystemExit(1)


@main.command("export")
@click.option("--day", "day_text", metavar="YYYY-MM-DD", help="Which day (default: today).")
def export_command(day_text: str | None) -> None:
    """Write a day's digest as Markdown into [export] path."""
    paths = app_paths()
    try:
        config = load_config(paths)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    if not config.export.path.strip():
        raise click.ClickException(
            "Markdown export is off: set [export] path in config.toml, or run `augury init`."
        )
    try:
        day = date.fromisoformat(day_text) if day_text else local_day(utcnow())
    except ValueError:
        raise click.BadParameter("use YYYY-MM-DD", param_hint="--day") from None
    conn = open_db(paths)
    try:
        path = export_daily(conn, config, day, run_id=last_scout_run_id(conn, day))
    except Exception as e:  # any failure is a clean message, never a traceback
        message = f"could not write the export: {type(e).__name__}: {e}"
        raise click.ClickException(safe(message)) from e
    finally:
        conn.close()
    click.echo(safe(f"wrote {path}"))


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
    result = scheduling.install(
        hour=hour, minute=minute, log_dir=paths.log_dir, augury_home=augury_home
    )
    if not result.ok:
        raise click.ClickException(result.message)
    click.echo(result.message)


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
