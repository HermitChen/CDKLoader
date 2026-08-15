from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


_SIZE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt](?:i?b)?|b)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
}


def parse_size_bytes(value: str | int) -> int:
    """Parse a byte setting such as ``10M``, ``1.5G`` or ``10485760``."""
    if isinstance(value, int):
        if value < 0:
            raise ValueError("文件大小不能为负数")
        return value

    match = _SIZE_PATTERN.fullmatch(str(value))
    if not match:
        raise ValueError(f"无效的文件大小：{value!r}，示例：100M、1G 或 104857600")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError(f"无效的文件大小：{value!r}") from exc

    unit = (match.group(2) or "B").upper()
    size = int(amount * _SIZE_UNITS[unit])
    if size < 1:
        raise ValueError("文件大小必须大于 0")
    return size


@dataclass(frozen=True)
class RuntimeSettingSpec:
    key: str
    group: str
    group_label: str
    label: str
    description: str
    value_type: str = "string"
    default: str | int | float = ""
    min_value: int | float | None = None
    max_value: int | float | None = None
    unit: str = ""
    options: tuple[tuple[str, str], ...] = ()


RUNTIME_SETTING_SPECS: tuple[RuntimeSettingSpec, ...] = (
    RuntimeSettingSpec(
        "account_auto_delete_days",
        "cleanup",
        "自动清理",
        "已交付、已失效账号保留",
        "超过此天数后自动删除；设置为 0 可关闭",
        "integer",
        30,
        0,
        3650,
        "天",
    ),
    RuntimeSettingSpec(
        "operation_log_retention_days",
        "cleanup",
        "自动清理",
        "任务日志保留",
        "任务和操作日志超过此天数后自动清理",
        "integer",
        30,
        1,
        3650,
        "天",
    ),
    RuntimeSettingSpec(
        "exhausted_cdk_auto_delete_days",
        "cleanup",
        "自动清理",
        "已耗尽 CDK 保留",
        "额度耗尽后超过此天数自动删除；设置为 0 可关闭",
        "integer",
        30,
        0,
        3650,
        "天",
    ),
    RuntimeSettingSpec(
        "export_retention_seconds",
        "cleanup",
        "自动清理",
        "导出文件保留",
        "导出目录中未被引用文件的最长保留时间",
        "integer",
        86400,
        300,
        2592000,
        "秒",
    ),
    RuntimeSettingSpec(
        "validation_mode",
        "validation",
        "验活",
        "验活模式",
        "远端验活适用于生产环境",
        "select",
        "remote",
        options=(("远端验活", "remote"), ("结构检查", "structural")),
    ),
    RuntimeSettingSpec(
        "validation_timeout_seconds",
        "validation",
        "验活",
        "单次请求超时",
        "单次远端请求最长等待时间",
        "number",
        30,
        0.1,
        300,
        "秒",
    ),
    RuntimeSettingSpec(
        "validation_attempts",
        "validation",
        "验活",
        "最大尝试次数",
        "单个账号远端验活失败后的最大尝试次数",
        "integer",
        3,
        1,
        10,
        "次",
    ),
    RuntimeSettingSpec(
        "validation_concurrency",
        "validation",
        "验活",
        "并发验活数量",
        "同一进程同时执行的账号验活数量",
        "integer",
        2,
        1,
        32,
        "个",
    ),
    RuntimeSettingSpec(
        "validation_egress_mode",
        "validation",
        "验活",
        "验活出口模式",
        "选择远端请求使用的网络出口",
        "select",
        "direct",
        options=(
            ("直连", "direct"),
            ("账号代理", "account"),
            ("代理池", "pool"),
        ),
    ),
    RuntimeSettingSpec(
        "validation_probe_mode",
        "validation",
        "验活",
        "验活探测模式",
        "严格模式会额外检查账号接口",
        "select",
        "fast",
        options=(("快速", "fast"), ("严格", "strict")),
    ),
    RuntimeSettingSpec(
        "validation_impersonate",
        "validation",
        "验活",
        "浏览器指纹",
        "curl_cffi 使用的 impersonate 名称",
        "string",
        "chrome146",
    ),
    RuntimeSettingSpec(
        "validation_proxy",
        "validation",
        "验活",
        "统一代理",
        "留空表示不使用统一代理",
        "string",
        "",
    ),
    RuntimeSettingSpec(
        "validation_proxy_pool",
        "validation",
        "验活",
        "代理池",
        "多个代理可用逗号、分号或换行分隔",
        "string",
        "",
    ),
    RuntimeSettingSpec(
        "validation_retry_base_seconds",
        "validation",
        "验活",
        "重试初始等待",
        "临时错误重试的初始等待时间",
        "number",
        2,
        0,
        60,
        "秒",
    ),
    RuntimeSettingSpec(
        "validation_retry_max_seconds",
        "validation",
        "验活",
        "重试最长等待",
        "指数退避的最长等待时间",
        "number",
        30,
        0,
        300,
        "秒",
    ),
    RuntimeSettingSpec(
        "validation_retry_jitter_seconds",
        "validation",
        "验活",
        "重试随机抖动",
        "重试等待的随机增加范围",
        "number",
        0.5,
        0,
        60,
        "秒",
    ),
    RuntimeSettingSpec(
        "validation_gate_threshold",
        "validation",
        "验活",
        "风控触发阈值",
        "60 秒内达到此数量后暂停新的验活请求",
        "integer",
        5,
        1,
        1000,
        "次",
    ),
    RuntimeSettingSpec(
        "validation_cooldown_seconds",
        "validation",
        "验活",
        "风控冷却时间",
        "触发阈值后的暂停时间",
        "number",
        60,
        0,
        3600,
        "秒",
    ),
    RuntimeSettingSpec(
        "redelivery_window_seconds",
        "delivery",
        "交付",
        "CDK 补发窗口",
        "已兑换 CDK 可公开补发的时间窗口；设置为 0 可关闭",
        "integer",
        1800,
        0,
        604800,
        "秒",
    ),
    RuntimeSettingSpec(
        "public_base_url",
        "delivery",
        "交付",
        "公开服务地址",
        "用于生成公开访问链接的外部地址",
        "string",
        "http://localhost:1456",
    ),
    RuntimeSettingSpec(
        "oauth_client_id",
        "delivery",
        "交付",
        "OAuth Client ID",
        "Refresh Token 兑换使用的 OAuth 客户端标识",
        "string",
        "app_2SKx67EdpoN0G6j64rFvigXD",
    ),
    RuntimeSettingSpec(
        "oauth_redirect_uri",
        "delivery",
        "交付",
        "OAuth Redirect URI",
        "留空时不向 OAuth 服务发送 redirect_uri",
        "string",
        "",
    ),
    RuntimeSettingSpec(
        "max_upload_bytes",
        "imports",
        "导入限制",
        "单文件大小",
        "支持 B、K、M、G、T 单位，例如 100M",
        "size",
        "100M",
    ),
    RuntimeSettingSpec(
        "max_import_accounts",
        "imports",
        "导入限制",
        "单次账号数量",
        "单次导入允许的最大账号数量",
        "integer",
        5000,
        1,
        100000,
        "条",
    ),
    RuntimeSettingSpec(
        "max_zip_files",
        "imports",
        "导入限制",
        "ZIP 文件数量",
        "单个 ZIP 包允许包含的最大文件数量",
        "integer",
        1000,
        1,
        10000,
        "个",
    ),
    RuntimeSettingSpec(
        "max_zip_uncompressed_bytes",
        "imports",
        "导入限制",
        "ZIP 解压大小",
        "ZIP 解压后的总大小上限",
        "size",
        "100M",
    ),
    RuntimeSettingSpec(
        "export_dir",
        "advanced",
        "高级",
        "导出文件目录",
        "导出文件在容器内的存放目录",
        "string",
        str(PROJECT_ROOT / "data" / "exports"),
    ),
)

RUNTIME_SETTING_SPEC_BY_KEY = {spec.key: spec for spec in RUNTIME_SETTING_SPECS}


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_password: str
    admin_token: str
    credential_secret: str
    cdk_pepper: str
    validation_mode: str
    validation_timeout_seconds: float
    validation_attempts: int
    validation_concurrency: int
    redelivery_window_seconds: int
    public_base_url: str
    max_upload_bytes: int
    max_import_accounts: int
    max_zip_files: int
    max_zip_uncompressed_bytes: int
    oauth_client_id: str
    oauth_redirect_uri: str
    operation_log_retention_days: int = 30
    export_retention_seconds: int = 86400
    export_dir: str = ""
    validation_proxy: str = ""
    validation_retry_base_seconds: float = 1.0
    validation_retry_max_seconds: float = 30.0
    validation_retry_jitter_seconds: float = 0.25
    validation_proxy_pool: str = ""
    validation_egress_mode: str = "direct"
    validation_probe_mode: str = "fast"
    validation_cooldown_seconds: float = 60.0
    validation_gate_threshold: int = 5
    validation_impersonate: str = "chrome146"
    account_auto_delete_days: int = 30
    exhausted_cdk_auto_delete_days: int = 30

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    default_db = f"sqlite:///{PROJECT_ROOT / 'data' / 'cdkloader.db'}"
    return Settings(
        database_url=os.getenv("DATABASE_URL", default_db),
        admin_password=os.getenv("ADMIN_PASSWORD", "change-me"),
        admin_token=os.getenv("ADMIN_TOKEN", "change-me-admin-token"),
        credential_secret=os.getenv("CREDENTIAL_SECRET", "development-credential-secret"),
        cdk_pepper=os.getenv("CDK_PEPPER", "development-cdk-pepper"),
        validation_mode=os.getenv("VALIDATION_MODE", "remote").strip().lower(),
        validation_timeout_seconds=float(os.getenv("VALIDATION_TIMEOUT_SECONDS", "30")),
        validation_attempts=max(1, int(os.getenv("VALIDATION_ATTEMPTS", "3"))),
        validation_concurrency=max(1, int(os.getenv("VALIDATION_CONCURRENCY", "2"))),
        redelivery_window_seconds=max(0, int(os.getenv("REDELIVERY_WINDOW_SECONDS", "1800"))),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:1456").rstrip("/"),
        max_upload_bytes=parse_size_bytes(os.getenv("MAX_UPLOAD_BYTES", "100M")),
        max_import_accounts=int(os.getenv("MAX_IMPORT_ACCOUNTS", "5000")),
        max_zip_files=int(os.getenv("MAX_ZIP_FILES", "1000")),
        max_zip_uncompressed_bytes=int(
            os.getenv("MAX_ZIP_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024))
        ),
        oauth_client_id=os.getenv("OPENAI_OAUTH_CLIENT_ID", "app_2SKx67EdpoN0G6j64rFvigXD"),
        oauth_redirect_uri=os.getenv("OPENAI_OAUTH_REDIRECT_URI", ""),
        operation_log_retention_days=max(1, int(os.getenv("OPERATION_LOG_RETENTION_DAYS", "30"))),
        export_retention_seconds=max(300, int(os.getenv("EXPORT_RETENTION_SECONDS", "86400"))),
        export_dir=os.getenv("EXPORT_DIR", str(PROJECT_ROOT / "data" / "exports")),
        validation_proxy=os.getenv("VALIDATION_PROXY", "").strip(),
        validation_retry_base_seconds=max(
            0.0, float(os.getenv("VALIDATION_RETRY_BASE_SECONDS", "2"))
        ),
        validation_retry_max_seconds=max(
            0.0, float(os.getenv("VALIDATION_RETRY_MAX_SECONDS", "30"))
        ),
        validation_retry_jitter_seconds=max(
            0.0, float(os.getenv("VALIDATION_RETRY_JITTER_SECONDS", "0.5"))
        ),
        validation_proxy_pool=os.getenv("VALIDATION_PROXY_POOL", "").strip(),
        validation_egress_mode=os.getenv("VALIDATION_EGRESS_MODE", "direct").strip().lower(),
        validation_probe_mode=os.getenv("VALIDATION_PROBE_MODE", "fast").strip().lower(),
        validation_cooldown_seconds=max(
            0.0, float(os.getenv("VALIDATION_COOLDOWN_SECONDS", "60"))
        ),
        validation_gate_threshold=max(
            1, int(os.getenv("VALIDATION_GATE_THRESHOLD", "5"))
        ),
        validation_impersonate=os.getenv("VALIDATION_IMPERSONATE", "chrome146").strip() or "chrome146",
        account_auto_delete_days=max(0, int(os.getenv("ACCOUNT_AUTO_DELETE_DAYS", "30"))),
        exhausted_cdk_auto_delete_days=max(
            0, int(os.getenv("EXHAUSTED_CDK_AUTO_DELETE_DAYS", "30"))
        ),
    )
