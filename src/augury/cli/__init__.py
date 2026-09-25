import click

from augury import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="augury")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Augury: a terminal-native AI digest reader."""
    if ctx.invoked_subcommand is None:
        click.echo("The terminal UI arrives in F14. See `augury --help` for commands.")
