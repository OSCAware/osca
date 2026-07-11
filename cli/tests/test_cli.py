"""CLI 入口测试：版本、帮助、退出码约定。"""

import subprocess
import sys

from osca_cli import __version__
from osca_cli.main import EXIT_OK, main


def test_version_matches_package():
    result = subprocess.run(
        [sys.executable, "-m", "osca_cli.main", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == EXIT_OK
    assert __version__ in result.stdout


def test_no_command_prints_help_and_exits_zero(capsys):
    assert main([]) == EXIT_OK
    out = capsys.readouterr().out
    for command in ("lint", "pack", "load"):
        assert command in out
