"""Authenticated remote MCP server for the public jGrants API."""

from __future__ import annotations

import base64
import binascii
import csv
import inspect
import io
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pdfplumber
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from markitdown import MarkItDown

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.jgrants-portal.go.jp/exp/v1/public"
PORTAL_BASE_URL = "https://www.jgrants-portal.go.jp/grants/view"
FILES_DIR = Path(os.environ.get("JGRANTS_FILES_DIR", "/tmp/jgrants_files"))
MAX_ATTACHMENT_BYTES = int(os.environ.get("JGRANTS_MAX_ATTACHMENT_BYTES", 25_000_000))
MAX_CONTENT_CHARS = int(os.environ.get("JGRANTS_MAX_CONTENT_CHARS", 80_000))
_READ_ONLY = {"readOnlyHint": True}
_HTTP_CLIENT: httpx.AsyncClient | None = None


class EnvironmentTokenVerifier(TokenVerifier):
    """Validate the shared secret injected by the hosting platform."""

    def __init__(self, expected_token: str):
        super().__init__()
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="torchi-notion-jgrants",
            scopes=["read:jgrants"],
            claims={"auth_method": "environment-bearer-token"},
        )


def auth_from_env() -> EnvironmentTokenVerifier | None:
    token = os.environ.get("MCP_AUTH_TOKEN")
    require_auth = os.environ.get("JGRANTS_REQUIRE_AUTH") == "1"
    if not token:
        if require_auth:
            raise RuntimeError("MCP_AUTH_TOKEN is required when JGRANTS_REQUIRE_AUTH=1")
        return None
    if len(token) < 32:
        raise RuntimeError("MCP_AUTH_TOKEN must be at least 32 characters")
    return EnvironmentTokenVerifier(token)


def _safe_path(base: Path, *parts: str) -> Path:
    resolved = (base / Path(*parts)).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError("不正なパスです")
    return resolved


def _sanitize_name(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r'[<>:"|?*\\/\x00-\x1f]', "_", name).strip().replace(" ", "_")
    return name[:240] or "attachment.bin"


def _http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=5),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
            headers={"User-Agent": "torchi-jgrants-mcp/1.0"},
        )
    return _HTTP_CLIENT


async def _get_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = await _http_client().get(f"{API_BASE_URL}{path}", params=params)
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else {"error": "JグランツAPIの応答形式が不正です"}
    except httpx.TimeoutException:
        return {"error": "JグランツAPIへのリクエストがタイムアウトしました"}
    except httpx.ConnectError:
        return {"error": "JグランツAPIへ接続できませんでした"}
    except httpx.HTTPStatusError as exc:
        return {"error": f"JグランツAPIがHTTP {exc.response.status_code}を返しました"}
    except (ValueError, TypeError):
        return {"error": "JグランツAPIのJSONを解析できませんでした"}


def _validate_search(keyword: str, sort: str, order: str, acceptance: int, limit: int) -> str | None:
    if not isinstance(keyword, str) or not 2 <= len(keyword.strip()) <= 255:
        return "keyword は2〜255文字で指定してください"
    if sort not in {"created_date", "acceptance_start_datetime", "acceptance_end_datetime"}:
        return "sort の値が不正です"
    if order.upper() not in {"ASC", "DESC"}:
        return "order は ASC または DESC を指定してください"
    if acceptance not in {0, 1}:
        return "acceptance は 0 または 1 を指定してください"
    if not 1 <= limit <= 100:
        return "limit は1〜100で指定してください"
    return None


def _compact_subsidy(item: dict[str, Any]) -> dict[str, Any]:
    subsidy_id = str(item.get("id") or "")
    return {
        "id": subsidy_id,
        "title": item.get("title"),
        "summary": item.get("detail") or item.get("description"),
        "subsidy_max_limit": item.get("subsidy_max_limit"),
        "subsidy_rate": item.get("subsidy_rate"),
        "acceptance_start": item.get("acceptance_start_datetime"),
        "acceptance_end": item.get("acceptance_end_datetime"),
        "target_area": item.get("target_area_search"),
        "target_industry": item.get("target_industry"),
        "target_employees": item.get("target_number_of_employees"),
        "use_purpose": item.get("use_purpose"),
        "official_url": f"{PORTAL_BASE_URL}/{subsidy_id}" if subsidy_id else None,
    }


async def _search(
    *,
    keyword: str = "事業",
    use_purpose: str | None = None,
    industry: str | None = None,
    target_number_of_employees: str | None = None,
    target_area_search: str | None = None,
    sort: str = "acceptance_end_datetime",
    order: str = "ASC",
    acceptance: int = 1,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "keyword": keyword.strip(),
        "sort": sort,
        "order": order.upper(),
        "acceptance": acceptance,
    }
    for key, value in {
        "use_purpose": use_purpose,
        "industry": industry,
        "target_number_of_employees": target_number_of_employees,
        "target_area_search": target_area_search,
    }.items():
        if value:
            params[key] = value
    data = await _get_json("/subsidies", params=params)
    if "error" in data:
        return data
    result = data.get("result", [])
    if not isinstance(result, list):
        return {"error": "JグランツAPIの検索結果形式が不正です"}
    return {"items": result, "conditions": params}


def _save_attachments(subsidy_id: str, detail: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    target_dir = _safe_path(FILES_DIR, subsidy_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, list[dict[str, Any]]] = {}
    for category in ("application_guidelines", "outline_of_grant", "application_form"):
        files = detail.get(category) or []
        if not isinstance(files, list):
            continue
        category_result: list[dict[str, Any]] = []
        for index, file_info in enumerate(files):
            if not isinstance(file_info, dict):
                continue
            original_name = str(file_info.get("name") or f"{category}_{index + 1}.bin")
            encoded = file_info.get("data")
            if not isinstance(encoded, str) or not encoded:
                category_result.append({"name": original_name, "error": "ファイルデータがありません"})
                continue
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                category_result.append({"name": original_name, "error": "BASE64をデコードできません"})
                continue
            if len(raw) > MAX_ATTACHMENT_BYTES:
                category_result.append({"name": original_name, "error": "ファイルサイズ上限を超えています"})
                continue
            safe_name = _sanitize_name(original_name)
            destination = _safe_path(target_dir, safe_name)
            destination.write_bytes(raw)
            category_result.append(
                {
                    "name": safe_name,
                    "original_name": original_name,
                    "size_bytes": len(raw),
                    "read_with": {
                        "tool": "get_file_content",
                        "arguments": {"subsidy_id": subsidy_id, "filename": safe_name},
                    },
                }
            )
        if category_result:
            saved[category] = category_result
    return saved


def _render_markdown(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        with pdfplumber.open(path) as document:
            pages = [(page.extract_text() or "").strip() for page in document.pages]
        text = "\n\n".join(page for page in pages if page)
        if text:
            return text
    return MarkItDown().convert(str(path)).text_content


def _instructions() -> str:
    return """Jグランツ補助金検索MCPです。search_subsidiesで候補を探し、get_subsidy_detailで正式条件と添付資料を確認してください。回答ではJグランツを出典として示し、申請前にofficial_urlで最新情報を再確認するよう案内してください。取得データにない適格性を推測しないでください。"""


def _fastmcp_kwargs() -> dict[str, Any]:
    parameters = inspect.signature(FastMCP.__init__).parameters
    return {"mask_error_details": True} if "mask_error_details" in parameters else {}


def create_mcp() -> FastMCP:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    server = FastMCP(
        "jgrants-subsidy-mcp",
        instructions=_instructions(),
        auth=auth_from_env(),
        **_fastmcp_kwargs(),
    )

    @server.tool(annotations=_READ_ONLY)
    async def ping() -> dict[str, Any]:
        """接続状態を確認します。"""
        return {
            "status": "ok",
            "server": "jgrants-subsidy-mcp",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @server.tool(annotations=_READ_ONLY)
    async def search_subsidies(
        keyword: str = "事業",
        use_purpose: str | None = None,
        industry: str | None = None,
        target_number_of_employees: str | None = None,
        target_area_search: str | None = None,
        sort: str = "acceptance_end_datetime",
        order: str = "ASC",
        acceptance: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Jグランツの補助金を検索します。

        keywordは2〜255文字。地域、業種、従業員数、利用目的を組み合わせて
        絞り込めます。acceptance=1は受付中のみ、0は全件です。
        sortはcreated_date、acceptance_start_datetime、
        acceptance_end_datetimeのいずれかです。
        """
        error = _validate_search(keyword, sort, order, acceptance, limit)
        if error:
            return {"error": error}
        result = await _search(
            keyword=keyword,
            use_purpose=use_purpose,
            industry=industry,
            target_number_of_employees=target_number_of_employees,
            target_area_search=target_area_search,
            sort=sort,
            order=order,
            acceptance=acceptance,
        )
        if "error" in result:
            return result
        items = result["items"]
        return {
            "count_returned": min(len(items), limit),
            "count_received_from_api": len(items),
            "subsidies": [_compact_subsidy(item) for item in items[:limit]],
            "search_conditions": result["conditions"],
            "source": "Jグランツ",
            "source_url": "https://www.jgrants-portal.go.jp/",
        }

    @server.tool(annotations=_READ_ONLY)
    async def get_subsidy_overview(keyword: str = "事業", output_format: str = "json") -> dict[str, Any]:
        """受付中補助金を締切までの日数と上限金額で集計します。"""
        if output_format not in {"json", "csv"}:
            return {"error": "output_format は json または csv を指定してください"}
        error = _validate_search(keyword, "acceptance_end_datetime", "ASC", 1, 100)
        if error:
            return {"error": error}
        result = await _search(keyword=keyword)
        if "error" in result:
            return result
        stats: dict[str, Any] = {
            "count_received_from_api": len(result["items"]),
            "by_deadline": {"within_14_days": 0, "within_30_days": 0, "within_60_days": 0, "later": 0, "unknown": 0},
            "by_max_amount": {"up_to_1m": 0, "up_to_10m": 0, "up_to_100m": 0, "over_100m": 0, "unknown": 0},
            "urgent": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "Jグランツ",
        }
        now = datetime.now(timezone.utc)
        for item in result["items"]:
            end_raw = item.get("acceptance_end_datetime")
            if not end_raw:
                stats["by_deadline"]["unknown"] += 1
            else:
                try:
                    end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                    days = (end - now).days
                    if days <= 14:
                        bucket = "within_14_days"
                    elif days <= 30:
                        bucket = "within_30_days"
                    elif days <= 60:
                        bucket = "within_60_days"
                    else:
                        bucket = "later"
                    stats["by_deadline"][bucket] += 1
                    if 0 <= days <= 14:
                        stats["urgent"].append({"id": item.get("id"), "title": item.get("title"), "days_left": days})
                except (TypeError, ValueError):
                    stats["by_deadline"]["unknown"] += 1
            try:
                amount = float(item.get("subsidy_max_limit"))
                if amount <= 1_000_000:
                    amount_bucket = "up_to_1m"
                elif amount <= 10_000_000:
                    amount_bucket = "up_to_10m"
                elif amount <= 100_000_000:
                    amount_bucket = "up_to_100m"
                else:
                    amount_bucket = "over_100m"
                stats["by_max_amount"][amount_bucket] += 1
            except (TypeError, ValueError):
                stats["by_max_amount"]["unknown"] += 1
        if output_format == "csv":
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(["dimension", "bucket", "count"])
            for dimension in ("by_deadline", "by_max_amount"):
                for bucket, count in stats[dimension].items():
                    writer.writerow([dimension, bucket, count])
            return {"format": "csv", "csv": stream.getvalue(), "source": "Jグランツ"}
        return stats

    @server.tool(annotations=_READ_ONLY)
    async def get_subsidy_detail(subsidy_id: str) -> dict[str, Any]:
        """補助金の詳細を取得し、添付資料をサーバーへ一時保存します。

        添付資料の本文が必要な場合は、返却されるread_with引数を使って
        get_file_contentを呼び出してください。
        """
        if not isinstance(subsidy_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", subsidy_id):
            return {"error": "subsidy_id の形式が不正です"}
        data = await _get_json(f"/subsidies/id/{subsidy_id}")
        if "error" in data:
            return data
        raw = data.get("result", data)
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        if not isinstance(raw, dict) or not raw:
            return {"error": "補助金が見つかりません"}
        compact = _compact_subsidy(raw)
        compact.update(
            {
                "description": raw.get("detail") or raw.get("description"),
                "inquiry_url": raw.get("inquiry_url"),
                "last_updated": raw.get("update_datetime"),
                "attachments": _save_attachments(subsidy_id, raw),
                "source": "Jグランツ",
                "verification_note": "申請前にofficial_urlで最新の公募要領と受付状況を確認してください。",
            }
        )
        return compact

    @server.tool(annotations=_READ_ONLY)
    async def get_file_content(subsidy_id: str, filename: str) -> dict[str, Any]:
        """get_subsidy_detailで一時保存した添付資料をMarkdownへ変換します。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", subsidy_id or ""):
            return {"error": "subsidy_id の形式が不正です"}
        if filename != _sanitize_name(filename):
            return {"error": "filename の形式が不正です"}
        try:
            path = _safe_path(FILES_DIR, subsidy_id, filename)
        except ValueError:
            return {"error": "不正なパスです"}
        if not path.is_file():
            return {"error": "ファイルが見つかりません。先にget_subsidy_detailを実行してください"}
        try:
            content = _render_markdown(path)
        except Exception:
            logger.exception("Attachment conversion failed")
            return {"error": "添付資料をMarkdownへ変換できませんでした"}
        truncated = len(content) > MAX_CONTENT_CHARS
        return {
            "subsidy_id": subsidy_id,
            "filename": filename,
            "format": "markdown",
            "content": content[:MAX_CONTENT_CHARS],
            "truncated": truncated,
            "character_count": len(content),
            "source": "Jグランツ添付資料",
        }

    @server.resource("jgrants://guidelines")
    async def usage_guidelines() -> str:
        return """# JグランツMCP利用ガイド\n\n1. search_subsidiesで候補を検索する。\n2. get_subsidy_detailで詳細と添付一覧を取得する。\n3. 必要な添付だけget_file_contentで読む。\n\n取得情報は参考情報です。申請前に必ずJグランツ公式ページで最新情報を確認し、公開時は出典を明記してください。"""

    @server.custom_route("/health", methods=["GET"])
    async def health_check(request):
        from mcp.types import LATEST_PROTOCOL_VERSION
        from starlette.responses import JSONResponse

        return JSONResponse(
            {
                "status": "healthy",
                "server": "jgrants-subsidy-mcp",
                "mcp_protocol_version": LATEST_PROTOCOL_VERSION,
            }
        )

    return server


mcp = create_mcp()


def main() -> None:
    host = os.environ.get("JGRANTS_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    path = os.environ.get("JGRANTS_PATH", "/mcp")
    mcp.run(transport="streamable-http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
