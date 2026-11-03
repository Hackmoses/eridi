from typer.testing import CliRunner

from eridi import __version__
from eridi.cli.main import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"eridi {__version__}"


def test_package_imports() -> None:
    import eridi

    assert eridi.__version__ == __version__
