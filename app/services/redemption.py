from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from ..models import Account, CDK, DeliveryItem, Redemption, RedemptionCDK, utcnow
from ..security import SecurityManager
from .validator import TokenValidator, persist_validation_result


class RedemptionError(ValueError):
    def __init__(self, code: str, message: str, details: list[dict] | None = None):
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


@dataclass
class ReservedAccount:
    account_id: str
    cdk_id: str


def refresh_cdk_status(cdk: CDK) -> None:
    now = utcnow()
    if cdk.disabled:
        cdk.status = "disabled"
    elif cdk.expires_at and cdk.expires_at <= now:
        cdk.status = "expired"
    elif cdk.remaining_quota <= 0:
        cdk.status = "exhausted"
    elif cdk.remaining_quota == cdk.total_quota:
        cdk.status = "unused"
    else:
        cdk.status = "partial"


class RedemptionService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        security: SecurityManager,
        validator: TokenValidator,
    ):
        self.factory = factory
        self.security = security
        self.validator = validator

    @staticmethod
    def normalize_codes(codes: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in codes:
            for part in raw.replace(",", "\n").splitlines():
                code = part.strip().upper()
                if code and code not in seen:
                    seen.add(code)
                    normalized.append(code)
        return normalized

    def create(
        self,
        session: Session,
        *,
        codes: list[str],
        idempotency_key: str,
        client_ip: str,
    ) -> Redemption:
        existing = session.scalar(
            select(Redemption)
            .options(joinedload(Redemption.cdks).joinedload(RedemptionCDK.cdk))
            .where(Redemption.idempotency_key == idempotency_key)
        )
        if existing:
            return existing

        normalized_codes = self.normalize_codes(codes)
        if not normalized_codes:
            raise RedemptionError("invalid_cdk", "请至少输入一个 CDK")

        digests = [self.security.cdk_digest(code) for code in normalized_codes]
        cdks = session.scalars(
            select(CDK).where(CDK.code_hmac.in_(digests)).with_for_update()
        ).all()
        by_digest = {item.code_hmac: item for item in cdks}
        errors: list[dict] = []
        selected: list[CDK] = []
        now = utcnow()
        for line, digest in enumerate(digests, start=1):
            cdk = by_digest.get(digest)
            if not cdk:
                errors.append({"line": line, "code": "not_found", "message": "CDK 无效"})
                continue
            refresh_cdk_status(cdk)
            available = cdk.remaining_quota - cdk.reserved_quota
            if cdk.status in {"expired", "disabled", "exhausted"} or available <= 0:
                errors.append({"line": line, "code": cdk.status, "message": "CDK 不可用或额度不足", "prefix": cdk.code_prefix})
                continue
            if cdk.expires_at and cdk.expires_at <= now:
                errors.append({"line": line, "code": "expired", "message": "CDK 已过期", "prefix": cdk.code_prefix})
                continue
            selected.append(cdk)
        if errors:
            raise RedemptionError("invalid_cdk", "存在无效、过期或额度不足的 CDK", errors)

        redemption = Redemption(
            idempotency_key=idempotency_key,
            status="queued",
            input_count=len(selected),
            requested_count=sum(cdk.remaining_quota - cdk.reserved_quota for cdk in selected),
            client_ip_hash=self.security.opaque_digest(client_ip or "unknown"),
        )
        session.add(redemption)
        session.flush()
        for ordinal, cdk in enumerate(selected):
            quantity = cdk.remaining_quota - cdk.reserved_quota
            cdk.reserved_quota += quantity
            session.add(
                RedemptionCDK(
                    redemption_id=redemption.id,
                    cdk_id=cdk.id,
                    ordinal=ordinal,
                    reserved_quantity=quantity,
                )
            )
        session.flush()
        return redemption

    @staticmethod
    def _account_query(cdk: CDK):
        query = select(Account).where(Account.status == "available")
        if cdk.account_source:
            query = query.where(Account.source == cdk.account_source)
        if cdk.registration_mode:
            query = query.where(Account.registration_mode == cdk.registration_mode)
        return query.order_by(Account.validated_at.desc().nullslast(), Account.created_at.asc())

    def _reserve_accounts(self, redemption_id: str, cdk_id: str, desired: int) -> list[str]:
        if desired <= 0:
            return []
        with self.factory.begin() as session:
            cdk = session.get(CDK, cdk_id)
            if not cdk:
                return []
            candidates = session.scalars(
                self._account_query(cdk).with_for_update(skip_locked=True).limit(desired)
            ).all()
            lease_until = utcnow() + timedelta(minutes=5)
            for account in candidates:
                account.status = "reserved"
                account.reserved_by = redemption_id
                account.reserved_until = lease_until
            return [account.id for account in candidates]

    def _load_reserved_accounts(self, redemption_id: str, ids: list[str]) -> list[Account]:
        if not ids:
            return []
        with self.factory() as session:
            accounts = session.scalars(
                select(Account).where(Account.id.in_(ids), Account.reserved_by == redemption_id)
            ).all()
            for account in accounts:
                session.expunge(account)
            return accounts

    def _validate_reserved(self, redemption_id: str, reserved: list[ReservedAccount]) -> list[ReservedAccount]:
        accounts = self._load_reserved_accounts(redemption_id, [item.account_id for item in reserved])
        by_id = {item.account_id: item for item in reserved}
        if not accounts:
            return []
        workers = min(self.validator.settings.validation_concurrency, len(accounts))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="validate") as executor:
            results = list(executor.map(self.validator.validate, accounts))

        valid: list[ReservedAccount] = []
        for account, result in zip(accounts, results, strict=True):
            with self.factory.begin() as session:
                current = session.get(Account, account.id)
                if not current or current.reserved_by != redemption_id:
                    continue
                persist_validation_result(
                    session,
                    current,
                    self.validator,
                    result,
                    preserve_reservation=True,
                )
                if result.outcome == "valid":
                    valid.append(by_id[account.id])
        return valid

    def _release_redemption(self, redemption_id: str, *, message: str, code: str) -> None:
        with self.factory.begin() as session:
            redemption = session.get(Redemption, redemption_id)
            if not redemption or redemption.status == "completed":
                return
            for relation in session.scalars(
                select(RedemptionCDK).where(RedemptionCDK.redemption_id == redemption_id)
            ).all():
                cdk = session.get(CDK, relation.cdk_id)
                if cdk:
                    cdk.reserved_quota = max(0, cdk.reserved_quota - relation.reserved_quantity)
                    refresh_cdk_status(cdk)
            for account in session.scalars(
                select(Account).where(Account.reserved_by == redemption_id)
            ).all():
                account.status = "available"
                account.reserved_by = None
                account.reserved_until = None
            redemption.status = "failed"
            redemption.error_code = code
            redemption.error_message = message
            redemption.completed_at = utcnow()

    def process(self, redemption_id: str) -> None:
        with self.factory.begin() as session:
            redemption = session.get(Redemption, redemption_id)
            if not redemption or redemption.status not in {"queued", "processing"}:
                return
            redemption.status = "processing"
            relations = session.scalars(
                select(RedemptionCDK)
                .options(joinedload(RedemptionCDK.cdk))
                .where(RedemptionCDK.redemption_id == redemption_id)
                .order_by(RedemptionCDK.ordinal)
            ).all()
            target_by_cdk = {relation.cdk_id: relation.reserved_quantity for relation in relations}

        accepted: dict[str, list[ReservedAccount]] = {cdk_id: [] for cdk_id in target_by_cdk}
        for cdk_id, target in target_by_cdk.items():
            while len(accepted[cdk_id]) < target:
                needed = target - len(accepted[cdk_id])
                candidate_ids = self._reserve_accounts(redemption_id, cdk_id, needed)
                if not candidate_ids:
                    self._release_redemption(redemption_id, message="可交付账号库存不足", code="insufficient_stock")
                    return
                reserved = [ReservedAccount(account_id=item, cdk_id=cdk_id) for item in candidate_ids]
                accepted[cdk_id].extend(self._validate_reserved(redemption_id, reserved))

        with self.factory.begin() as session:
            redemption = session.get(Redemption, redemption_id)
            if not redemption or redemption.status != "processing":
                return
            relations = session.scalars(
                select(RedemptionCDK)
                .options(joinedload(RedemptionCDK.cdk))
                .where(RedemptionCDK.redemption_id == redemption_id)
            ).all()
            delivered_count = 0
            for relation in relations:
                valid_accounts = accepted.get(relation.cdk_id, [])
                if len(valid_accounts) != relation.reserved_quantity:
                    raise RuntimeError("兑换完成前账号数量不一致")
                for item in valid_accounts:
                    account = session.get(Account, item.account_id)
                    if not account or account.reserved_by != redemption_id or account.status != "reserved":
                        raise RuntimeError("账号预约已失效")
                    account.status = "delivered"
                    account.reserved_by = None
                    account.reserved_until = None
                    account.delivered_at = utcnow()
                    session.add(DeliveryItem(redemption_id=redemption_id, cdk_id=relation.cdk_id, account_id=account.id))
                    delivered_count += 1
                relation.debited_quantity = relation.reserved_quantity
                cdk = relation.cdk
                cdk.reserved_quota = max(0, cdk.reserved_quota - relation.reserved_quantity)
                cdk.remaining_quota = max(0, cdk.remaining_quota - relation.reserved_quantity)
                refresh_cdk_status(cdk)
            redemption.status = "completed"
            redemption.delivered_count = delivered_count
            redemption.completed_at = utcnow()

    def recover_stale_reservations(self) -> int:
        with self.factory.begin() as session:
            now = utcnow()
            stale = session.scalars(
                select(Account).where(Account.status == "reserved", Account.reserved_until < now)
            ).all()
            redemption_ids = {account.reserved_by for account in stale if account.reserved_by}
            for account in stale:
                account.status = "available"
                account.reserved_by = None
                account.reserved_until = None
            for redemption_id in redemption_ids:
                redemption = session.get(Redemption, redemption_id)
                if redemption and redemption.status in {"queued", "processing"}:
                    redemption.status = "failed"
                    redemption.error_code = "reservation_expired"
                    redemption.error_message = "兑换任务超时，预约已释放"
                    redemption.completed_at = now
                    for relation in session.scalars(
                        select(RedemptionCDK).where(RedemptionCDK.redemption_id == redemption.id)
                    ).all():
                        cdk = session.get(CDK, relation.cdk_id)
                        if cdk:
                            cdk.reserved_quota = max(0, cdk.reserved_quota - relation.reserved_quantity)
                            refresh_cdk_status(cdk)
            return len(stale)


def serialize_redemption(redemption: Redemption, security: SecurityManager) -> dict:
    return {
        "id": redemption.id,
        "status": redemption.status,
        "requested_count": redemption.requested_count,
        "delivered_count": redemption.delivered_count,
        "input_count": redemption.input_count,
        "error_code": redemption.error_code or None,
        "error_message": redemption.error_message or None,
        "created_at": redemption.created_at.isoformat(),
        "completed_at": redemption.completed_at.isoformat() if redemption.completed_at else None,
        "downloaded_at": redemption.downloaded_at.isoformat() if redemption.downloaded_at else None,
        "cdks": [
            {
                "prefix": relation.cdk.code_prefix,
                "reserved_quantity": relation.reserved_quantity,
                "debited_quantity": relation.debited_quantity,
            }
            for relation in redemption.cdks
        ],
        "task_token": security.redemption_token(redemption.id, redemption.idempotency_key),
    }

