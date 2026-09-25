import click

from augury import __version__
from augury import doctor as doctor_checks
from augury.core.paths import app_paths


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="augury")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Augury: a terminal-native AI digest reader."""
    if ctx.invoked_subcommand is None:
        click.echo("The terminal UI arrives in F14. See `augury --help` for commands.")


@main.command()
@click.option("--offline", is_flag=True, help="Skip checks that need the network.")
def doctor(offline: bool) -> None:
    """Check paths, config, database and (unless --offline) network access."""
    checks = doctor_checks.run_checks(app_paths())
    click.echo(doctor_checks.format_checks(checks))
    if doctor_checks.failed(checks):
        raise SystemExit(1)
