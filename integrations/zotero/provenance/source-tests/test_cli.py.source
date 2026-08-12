from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from dump_research_info.__about__ import __version__
from dump_research_info.cli import app

runner = CliRunner(env={"TERM": "dumb"})


class TestCLIInfo:
    def test_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for expected in ["SOURCE", "SERVICE_URL", "--token", "--collection", "--dry-run"]:
            assert expected in result.output

    @pytest.mark.parametrize(
        "extra_args",
        [
            [],                        # no other args at all
            ["http://localhost:8111"],  # service_url but missing source, token, collection
        ],
    )
    def test_version_ignores_missing_required_args(self, extra_args):
        result = runner.invoke(app, ["--version"] + extra_args)
        assert result.exit_code == 0
        assert __version__ in result.output


class TestCLIArgumentValidation:
    @pytest.mark.parametrize(
        "args",
        [
            [],                        # missing both positional args
            ["http://localhost:8111"],  # missing SOURCE (only SERVICE_URL given)
        ],
    )
    def test_missing_positional_args(self, args):
        result = runner.invoke(app, args)
        assert result.exit_code != 0

    def test_source_is_file_not_dir(self, tmp_path):
        f = tmp_path / "not_a_dir.txt"
        f.write_text("content")
        result = runner.invoke(
            app, [str(f), "http://localhost:8111", "-t", "tok", "-c", "coll"]
        )
        assert result.exit_code != 0

    def test_source_does_not_exist(self, tmp_path):
        result = runner.invoke(
            app,
            [str(tmp_path / "nonexistent"), "http://localhost:8111", "-t", "tok", "-c", "coll"],
        )
        assert result.exit_code != 0

    def test_missing_token_mentions_env_var(self, tmp_path):
        result = runner.invoke(
            app, [str(tmp_path), "http://localhost:8111", "-c", "coll"]
        )
        assert result.exit_code != 0
        assert "DUMP_THINGS_TOKEN" in result.output

    def test_missing_collection_mentions_env_var(self, tmp_path):
        result = runner.invoke(
            app, [str(tmp_path), "http://localhost:8111", "-t", "tok"]
        )
        assert result.exit_code != 0
        assert "DUMP_THINGS_COLLECTION" in result.output


class TestCLIEnvVars:
    def test_token_from_env(self, tmp_path):
        with patch(
            "dump_research_info.dump.dump_records", new=AsyncMock(return_value=None)
        ) as mock:
            result = runner.invoke(
                app,
                [str(tmp_path), "http://localhost:8111", "-c", "coll"],
                env={"DUMP_THINGS_TOKEN": "env_token"},
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["token"] == "env_token"

    def test_collection_from_env(self, tmp_path):
        with patch(
            "dump_research_info.dump.dump_records", new=AsyncMock(return_value=None)
        ) as mock:
            result = runner.invoke(
                app,
                [str(tmp_path), "http://localhost:8111", "-t", "tok"],
                env={"DUMP_THINGS_COLLECTION": "env_collection"},
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["collection"] == "env_collection"


class TestCLIExecution:
    def test_dry_run_empty_source(self, tmp_path):
        result = runner.invoke(
            app,
            [str(tmp_path), "http://localhost:8111", "-t", "tok", "-c", "coll", "--dry-run"],
        )
        assert result.exit_code == 0
        assert "No JSON files found" in result.output

    def test_dry_run_output(self, sample_source_dir):
        result = runner.invoke(
            app,
            [
                str(sample_source_dir),
                "http://localhost:8111",
                "-t", "tok",
                "-c", "research_info",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "1 XYZGrant record(s)" in result.output
        assert "2 XYZPerson record(s)" in result.output
        assert "POST http://localhost:8111/research_info/record/XYZGrant" in result.output
        assert "POST http://localhost:8111/research_info/record/XYZPerson" in result.output

    def test_posting_calls_dump_records_with_correct_args(self, sample_source_dir):
        with patch(
            "dump_research_info.dump.dump_records", new=AsyncMock(return_value=None)
        ) as mock:
            result = runner.invoke(
                app,
                [
                    str(sample_source_dir),
                    "http://localhost:8111",
                    "-t", "mytoken",
                    "-c", "research_info",
                ],
            )
        assert result.exit_code == 0
        mock.assert_called_once()
        _, kwargs = mock.call_args
        assert kwargs["token"] == "mytoken"
        assert kwargs["collection"] == "research_info"
        assert kwargs["source"] == sample_source_dir
        assert kwargs["service_url"] == "http://localhost:8111"
        assert kwargs["dry_run"] is False
