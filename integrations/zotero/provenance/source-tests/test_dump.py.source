import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from dump_research_info.dump import _post_class_file, _post_record


class TestPostRecord:
    async def test_success(self):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_response

        result = await _post_record(mock_client, "/endpoint", {"key": "val"}, "token")

        assert result is None
        mock_client.post.assert_called_once_with(
            "/endpoint",
            json={"key": "val"},
            headers={"X-DumpThings-Token": "token"},
        )

    @pytest.mark.parametrize(
        "status_code,response_text",
        [
            (400, "Bad request"),
            (422, "Validation error"),
            (500, "Internal server error"),
        ],
    )
    async def test_http_error(self, status_code, response_text):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error",
            request=httpx.Request("POST", "http://test.com/"),
            response=MagicMock(status_code=status_code, text=response_text),
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_response

        result = await _post_record(mock_client, "/endpoint", {"key": "val"}, "token")

        assert result == f"HTTP {status_code}: {response_text}"

    async def test_network_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError(
            "Connection refused",
            request=httpx.Request("POST", "http://test.com/"),
        )

        result = await _post_record(mock_client, "/endpoint", {"key": "val"}, "token")

        assert result is not None
        assert "Connection refused" in result


class TestPostClassFile:
    async def test_all_success(self, tmp_path, capsys):
        records = [{"pid": f"https://example.com/{i}"} for i in range(3)]
        file_path = tmp_path / "XYZPerson.json"
        file_path.write_text(json.dumps(records))

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = mock_response

        await _post_class_file(mock_client, file_path, "research_info", "token")

        captured = capsys.readouterr()
        assert "XYZPerson: 3/3 records posted successfully" in captured.out
        assert mock_client.post.call_count == 3

    async def test_empty_file(self, tmp_path):
        file_path = tmp_path / "XYZPerson.json"
        file_path.write_text("[]")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        await _post_class_file(mock_client, file_path, "research_info", "token")

        mock_client.post.assert_not_called()

    @pytest.mark.parametrize(
        "n_records,n_failures",
        [
            (3, 1),
            (5, 2),
        ],
    )
    async def test_partial_failure(self, tmp_path, capsys, n_records, n_failures):
        records = [{"pid": f"https://example.com/{i}"} for i in range(n_records)]
        file_path = tmp_path / "XYZPerson.json"
        file_path.write_text(json.dumps(records))

        success = MagicMock()
        success.raise_for_status.return_value = None
        failure = MagicMock()
        failure.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error",
            request=httpx.Request("POST", "http://test.com/"),
            response=MagicMock(status_code=422, text="Invalid"),
        )

        side_effects = [failure] * n_failures + [success] * (n_records - n_failures)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = side_effects

        await _post_class_file(mock_client, file_path, "research_info", "token")

        captured = capsys.readouterr()
        n_ok = n_records - n_failures
        assert f"XYZPerson: {n_ok}/{n_records} records posted successfully" in captured.out
        assert captured.out.count("HTTP 422") == n_failures
