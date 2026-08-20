from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastmcp import Client

import jgrants_mcp_server.core as core


def test_validate_search():
    assert core._validate_search("IT", "acceptance_end_datetime", "ASC", 1, 20) is None
    assert core._validate_search("A", "acceptance_end_datetime", "ASC", 1, 20)
    assert core._validate_search("IT", "bad", "ASC", 1, 20)
    assert core._validate_search("IT", "created_date", "sideways", 1, 20)
    assert core._validate_search("IT", "created_date", "ASC", 3, 20)
    assert core._validate_search("IT", "created_date", "ASC", 1, 101)


def test_safe_path_rejects_traversal(tmp_path: Path):
    assert core._safe_path(tmp_path, "subsidy", "file.pdf").is_relative_to(tmp_path)
    with pytest.raises(ValueError):
        core._safe_path(tmp_path, "..", "secret")


def test_sanitize_name():
    assert core._sanitize_name("../申請 書?.pdf") == "申請_書_.pdf"


def test_save_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(core, "FILES_DIR", tmp_path)
    payload = base64.b64encode(b"hello").decode()
    saved = core._save_attachments(
        "abc123",
        {"application_guidelines": [{"name": "guide.txt", "data": payload}]},
    )
    item = saved["application_guidelines"][0]
    assert item["name"] == "guide.txt"
    assert item["size_bytes"] == 5
    assert (tmp_path / "abc123" / "guide.txt").read_bytes() == b"hello"


def test_save_attachments_rejects_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(core, "FILES_DIR", tmp_path)
    monkeypatch.setattr(core, "MAX_ATTACHMENT_BYTES", 2)
    payload = base64.b64encode(b"hello").decode()
    saved = core._save_attachments(
        "abc123",
        {"application_form": [{"name": "form.txt", "data": payload}]},
    )
    assert "上限" in saved["application_form"][0]["error"]


@pytest.mark.asyncio
async def test_search_calls_public_api(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    async def fake_get_json(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"result": [{"id": "a1", "title": "IT補助金"}]}

    monkeypatch.setattr(core, "_get_json", fake_get_json)
    result = await core._search(keyword="IT", target_area_search="東京都")
    assert captured["path"] == "/subsidies"
    assert captured["params"]["target_area_search"] == "東京都"
    assert result["items"][0]["title"] == "IT補助金"


def test_auth_requires_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("JGRANTS_REQUIRE_AUTH", "1")
    with pytest.raises(RuntimeError):
        core.auth_from_env()


def test_auth_rejects_short_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
    monkeypatch.delenv("JGRANTS_REQUIRE_AUTH", raising=False)
    with pytest.raises(RuntimeError):
        core.auth_from_env()


@pytest.mark.asyncio
async def test_mcp_exposes_expected_tools():
    async with Client(core.mcp) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert names == {
            "ping",
            "search_subsidies",
            "get_subsidy_overview",
            "get_subsidy_detail",
            "get_file_content",
        }
        result = await client.call_tool("ping", {})
        assert not result.is_error
