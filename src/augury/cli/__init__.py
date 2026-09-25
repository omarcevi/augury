import asyncio

import click

from augury import __version__
from augury import doctor as doctor_checks
from augury.core.config import ConfigError, load_config
from augury.core.paths import app_paths
from augury.sources.http import PoliteClient


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
