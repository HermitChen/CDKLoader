from __future__ import annotations

from sqlalchemy import ColumnElement, and_, func, not_, or_


CDK_EMAIL_TYPE_GENERIC = "generic"
CDK_EMAIL_TYPE_MS = "ms"
CDK_EMAIL_TYPE_ICLOUD = "icloud"
CDK_EMAIL_TYPE_GMAIL = "gmail"
CDK_EMAIL_TYPES = {
    CDK_EMAIL_TYPE_GENERIC,
    CDK_EMAIL_TYPE_MS,
    CDK_EMAIL_TYPE_ICLOUD,
    CDK_EMAIL_TYPE_GMAIL,
}

CDK_EMAIL_TYPE_LABELS = {
    CDK_EMAIL_TYPE_GENERIC: "通用邮箱",
    CDK_EMAIL_TYPE_MS: "微软邮箱",
    CDK_EMAIL_TYPE_ICLOUD: "苹果邮箱",
    CDK_EMAIL_TYPE_GMAIL: "谷歌邮箱",
}

CDK_EMAIL_TYPE_PREFIXES = {
    CDK_EMAIL_TYPE_GENERIC: "CDK",
    CDK_EMAIL_TYPE_MS: "CDK-MS",
    CDK_EMAIL_TYPE_ICLOUD: "CDK-IC",
    CDK_EMAIL_TYPE_GMAIL: "CDK-GM",
}

CDK_PREFIX_EMAIL_TYPES = {
    "MS": CDK_EMAIL_TYPE_MS,
    "IC": CDK_EMAIL_TYPE_ICLOUD,
    "GM": CDK_EMAIL_TYPE_GMAIL,
}

CDK_TYPED_PREFIXES = {
    CDK_EMAIL_TYPE_MS: ("CDK-MS",),
    CDK_EMAIL_TYPE_ICLOUD: ("CDK-IC",),
    CDK_EMAIL_TYPE_GMAIL: ("CDK-GM",),
}


def normalize_cdk_email_type(value: str | None) -> str:
    return value if value in CDK_EMAIL_TYPES else CDK_EMAIL_TYPE_GENERIC


def infer_cdk_email_type(value: str | None) -> str:
    parts = (value or "").strip().upper().split("-")
    if len(parts) > 1:
        if candidate := CDK_PREFIX_EMAIL_TYPES.get(parts[1]):
            return candidate
    return CDK_EMAIL_TYPE_GENERIC


def effective_cdk_email_type(cdk) -> str:
    stored = normalize_cdk_email_type(getattr(cdk, "email_type", None))
    if stored != CDK_EMAIL_TYPE_GENERIC:
        return stored
    return infer_cdk_email_type(getattr(cdk, "code_prefix", None))


def cdk_code_prefix(email_type: str) -> str:
    normalized = normalize_cdk_email_type(email_type)
    return CDK_EMAIL_TYPE_PREFIXES[normalized]


def cdk_type_condition(email_type_column: ColumnElement, prefix_column: ColumnElement, email_type: str):
    normalized = normalize_cdk_email_type(email_type)
    typed_prefixes = tuple(prefix for prefixes in CDK_TYPED_PREFIXES.values() for prefix in prefixes)
    typed_condition = or_(
        email_type_column == normalized,
        prefix_column.in_(CDK_TYPED_PREFIXES.get(normalized, ())),
    )
    if normalized == CDK_EMAIL_TYPE_GENERIC:
        return and_(
            or_(email_type_column.is_(None), email_type_column == CDK_EMAIL_TYPE_GENERIC),
            not_(prefix_column.in_(typed_prefixes)),
        )
    return typed_condition


def cdk_email_condition(email_column: ColumnElement, email_type: str):
    normalized = normalize_cdk_email_type(email_type)
    if normalized == CDK_EMAIL_TYPE_GENERIC:
        return None

    return _provider_email_condition(email_column, normalized)


def account_email_condition(email_column: ColumnElement, email_type: str):
    normalized = normalize_cdk_email_type(email_type)
    if normalized == CDK_EMAIL_TYPE_GENERIC:
        known_provider = or_(
            _provider_email_condition(email_column, CDK_EMAIL_TYPE_MS),
            _provider_email_condition(email_column, CDK_EMAIL_TYPE_ICLOUD),
            _provider_email_condition(email_column, CDK_EMAIL_TYPE_GMAIL),
        )
        return or_(email_column.is_(None), not_(known_provider))
    return _provider_email_condition(email_column, normalized)


def _provider_email_condition(email_column: ColumnElement, email_type: str):
    domain = func.lower(email_column)
    if email_type == CDK_EMAIL_TYPE_MS:
        return or_(domain.like("%@hotmail.%"), domain.like("%@outlook.%"))
    if email_type == CDK_EMAIL_TYPE_ICLOUD:
        return or_(domain.like("%@icloud.%"), domain.like("%@me.com"), domain.like("%@mac.com"))
    if email_type == CDK_EMAIL_TYPE_GMAIL:
        return domain.like("%@gmail.%")
    return None
