from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from ..config import Settings


CANONICAL_FIELDS = {
    "email",
    "password",
    "cookies",
    "client_id",
    "account_id",
    "workspace_id",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "registered_at",
    "last_refresh",
    "expires_at",
    "status",
    "registration_mode",
    "source",
}

HEADER_ALIASES = {
    "id": "account_id",
    "email": "email",
    "password": "password",
    "clientid": "client_id",
    "accountid": "account_id",
    "workspaceid": "workspace_id",
    "accesstoken": "access_token",
    "refreshtoken": "refresh_token",
    "idtoken": "id_token",
    "sessiontoken": "session_token",
    "status": "status",
    "registeredat": "registered_at",
    "lastrefresh": "last_refresh",
    "expiresat": "expires_at",
    "cookies": "cookies",
    "registrationmode": "registration_mode",
    "source": "source",
}


@dataclass
class ParsedAccount:
    locator: str
    email: str | None = None
    password: str | None = None
    cookies: str | None = None
    client_id: str | None = None
    account_id: str | None = None
    workspace_id: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    session_token: str | None = None
    registered_at: datetime | None = None
    last_refresh: datetime | None = None
    expires_at: datetime | None = None
    registration_mode: str = "codex"
    source: str = "import"
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseError:
    locator: str
    error_type: str
    message: str
    account_hint: str = ""


@dataclass
class ParsedBatch:
    records: list[ParsedAccount] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    formats: set[str] = field(default_factory=set)

    @property
    def detected_format(self) -> str:
        return next(iter(self.formats)) if len(self.formats) == 1 else "mixed"


class ImportParseException(ValueError):
    pass


def _compact(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_email(value: Any) -> str | None:
    email = _compact(value)
    return email.lower() if email else None


def _parse_datetime(value: Any, field_name: str, locator: str) -> datetime | None:
    text = _compact(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportParseException(f"{field_name} 不是有效 ISO 日期") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _normalized_header(header: str) -> str:
    return "".join(char for char in header.lower() if char.isalnum())


def _extract_known(data: dict[str, Any], locator: str, source: str) -> ParsedAccount:
    normalized = {str(key).strip().lower(): value for key, value in data.items()}
    credentials = normalized.get("credentials")
    if isinstance(credentials, dict):
        credentials = {str(key).strip().lower(): value for key, value in credentials.items()}
    else:
        credentials = {}

    def value(name: str) -> Any:
        return normalized.get(name, credentials.get(name))

    known = set(CANONICAL_FIELDS) | {"credentials", "type", "name", "platform", "extra", "expired"}
    extra = normalized.get("extra") if isinstance(normalized.get("extra"), dict) else {}
    account = ParsedAccount(
        locator=locator,
        email=_normalize_email(value("email")),
        password=_compact(value("password")),
        cookies=_compact(value("cookies")),
        client_id=_compact(value("client_id")),
        account_id=_compact(value("account_id")),
        workspace_id=_compact(value("workspace_id") or value("organization_id")),
        access_token=_compact(value("access_token")),
        refresh_token=_compact(value("refresh_token")),
        id_token=_compact(value("id_token")),
        session_token=_compact(value("session_token")),
        registered_at=_parse_datetime(value("registered_at"), "registered_at", locator),
        last_refresh=_parse_datetime(value("last_refresh"), "last_refresh", locator),
        expires_at=_parse_datetime(value("expires_at") or value("expired"), "expires_at", locator),
        registration_mode=_compact(value("registration_mode") or value("type")) or "codex",
        source=_compact(extra.get("source") if isinstance(extra, dict) else None) or source,
        extra_data={key: value for key, value in normalized.items() if key not in known},
    )
    if isinstance(extra, dict):
        account.extra_data.update(extra)
    _validate_record(account)
    return account


def _validate_record(account: ParsedAccount) -> None:
    if not account.email and not account.account_id:
        raise ImportParseException("缺少 email 或 account_id")
    if not account.access_token and not account.refresh_token:
        raise ImportParseException("缺少 access_token 和 refresh_token")


def _parse_json(filename: str, content: bytes, source_locator: str = "") -> ParsedBatch:
    batch = ParsedBatch(formats={"json"})
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportParseException("JSON 文件无法解析") from exc

    if isinstance(payload, list):
        rows = payload
        source = "standard_json"
        detected = "json"
    elif isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        rows = payload["accounts"]
        source = "sub2api"
        detected = "sub2api"
    elif isinstance(payload, dict):
        rows = [payload]
        source = "cpa" if payload.get("type") or payload.get("credentials") else "standard_json"
        detected = "cpa" if source == "cpa" else "json"
    else:
        raise ImportParseException("JSON 根节点必须是对象或数组")

    batch.formats = {detected}
    for index, row in enumerate(rows, start=1):
        locator = f"{source_locator or filename}:{index}"
        if not isinstance(row, dict):
            batch.errors.append(ParseError(locator, "invalid_record", "账号记录必须是对象"))
            continue
        try:
            batch.records.append(_extract_known(row, locator, source))
        except ImportParseException as exc:
            batch.errors.append(ParseError(locator, "invalid_record", str(exc), _normalize_email(row.get("email")) or ""))
    return batch


def _parse_csv(filename: str, content: bytes, source_locator: str = "") -> ParsedBatch:
    batch = ParsedBatch(formats={"csv"})
    try:
        stream = io.StringIO(content.decode("utf-8-sig"), newline="")
        reader = csv.DictReader(stream)
    except UnicodeDecodeError as exc:
        raise ImportParseException("CSV 不是 UTF-8 编码") from exc
    if not reader.fieldnames:
        raise ImportParseException("CSV 缺少表头")

    for index, raw in enumerate(reader, start=2):
        locator = f"{source_locator or filename}:{index}"
        mapped: dict[str, Any] = {}
        for header, value in raw.items():
            if not header:
                continue
            field = HEADER_ALIASES.get(_normalized_header(header))
            if field:
                mapped[field] = value
        try:
            batch.records.append(_extract_known(mapped, locator, "csv"))
        except ImportParseException as exc:
            batch.errors.append(ParseError(locator, "invalid_record", str(exc), _normalize_email(mapped.get("email")) or ""))
    return batch


def _parse_txt(filename: str, content: bytes, source_locator: str = "") -> ParsedBatch:
    batch = ParsedBatch(formats={"txt"})
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ImportParseException("TXT 不是 UTF-8 编码") from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        locator = f"{source_locator or filename}:{index}"
        parts = line.split("----")
        if len(parts) != 3:
            batch.errors.append(ParseError(locator, "invalid_txt", "TXT 必须是 email----password----refresh_token 格式"))
            continue
        try:
            batch.records.append(
                _extract_known(
                    {"email": parts[0], "password": parts[1], "refresh_token": parts[2]},
                    locator,
                    "txt",
                )
            )
        except ImportParseException as exc:
            batch.errors.append(ParseError(locator, "invalid_record", str(exc), _normalize_email(parts[0]) or ""))
    return batch


def _merge_batch(target: ParsedBatch, source: ParsedBatch) -> None:
    target.records.extend(source.records)
    target.errors.extend(source.errors)
    target.formats.update(source.formats)


def _parse_zip(filename: str, content: bytes, settings: Settings) -> ParsedBatch:
    batch = ParsedBatch(formats={"zip"})
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ImportParseException("ZIP 文件无法解析") from exc

    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > settings.max_zip_files:
            raise ImportParseException("ZIP 文件数量超过限制")
        total_size = 0
        for info in members:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or info.is_dir():
                raise ImportParseException("ZIP 包含不安全路径")
            if info.is_dir() or info.filename.endswith("/"):
                continue
            total_size += info.file_size
            if total_size > settings.max_zip_uncompressed_bytes:
                raise ImportParseException("ZIP 解压后总大小超过限制")
            if info.file_size > settings.max_upload_bytes:
                raise ImportParseException("ZIP 内单个文件超过限制")

        for info in members:
            if info.filename.endswith("/"):
                continue
            nested_name = f"{filename}!{info.filename}"
            suffix = PurePosixPath(info.filename).suffix.lower()
            if suffix not in {".json", ".csv", ".txt"}:
                batch.errors.append(ParseError(nested_name, "unsupported_file", "ZIP 内文件格式不受支持"))
                continue
            raw = archive.read(info)
            try:
                child = _parse_by_suffix(info.filename, raw, settings, nested_name)
                _merge_batch(batch, child)
            except ImportParseException as exc:
                batch.errors.append(ParseError(nested_name, "parse_error", str(exc)))
    return batch


def _parse_by_suffix(filename: str, content: bytes, settings: Settings, source_locator: str = "") -> ParsedBatch:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".json":
        return _parse_json(filename, content, source_locator)
    if suffix == ".csv":
        return _parse_csv(filename, content, source_locator)
    if suffix == ".txt":
        return _parse_txt(filename, content, source_locator)
    if suffix == ".zip":
        if source_locator:
            raise ImportParseException("不支持 ZIP 嵌套 ZIP")
        return _parse_zip(filename, content, settings)
    raise ImportParseException("不支持的文件格式")


def parse_import_file(filename: str, content: bytes, settings: Settings) -> ParsedBatch:
    return parse_import_files([(filename, content)], settings)


def parse_import_files(files: list[tuple[str, bytes]], settings: Settings) -> ParsedBatch:
    if not files:
        raise ImportParseException("缺少文件")

    batch = ParsedBatch()
    for filename, content in files:
        if not filename:
            raise ImportParseException("缺少文件名")
        if not content:
            raise ImportParseException("上传文件为空")
        if len(content) > settings.max_upload_bytes:
            raise ImportParseException("上传文件超过大小限制")
        _merge_batch(batch, _parse_by_suffix(filename, content, settings))
        if len(batch.records) > settings.max_import_accounts:
            raise ImportParseException("账号数量超过单次导入限制")
    return batch
