"""`duplio drives` on network addresses: described from the address alone, with no connection."""

from __future__ import annotations

from click.testing import CliRunner

from duplio.cli.main import cli


def test_network_addresses_are_described_without_connecting() -> None:
    runner = CliRunner(env={"COLUMNS": "150"})
    for address, unencrypted in (
        ("ftp://me@127.0.0.1:1/photos", True),
        ("ftps://me@127.0.0.1:1/photos", False),
        ("dav://127.0.0.1:1/x", True),
        ("davs://127.0.0.1:1/x", False),
        ("sftp://me@127.0.0.1:1/x", False),
    ):  # nothing listens on port 1: describing a drive must not need it
        result = runner.invoke(cli, ["--no-color", "drives", address], catch_exceptions=False)
        assert result.exit_code == 0, address
        assert "slow or costly" in result.output and "Parallel reads used" in result.output, address
        assert ("NOT ENCRYPTED" in result.output) is unencrypted, address
