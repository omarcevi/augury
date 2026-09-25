import click

from article_oracle import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="oracle")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Article Oracle: a terminal-native AI digest reader."""
    if ctx.invoked_subcommand is None:
        click.echo("The terminal UI arrives in F14. See `oracle --help` for commands.")
