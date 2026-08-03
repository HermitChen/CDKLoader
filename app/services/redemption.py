from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload, sessionmaker

from ..models import Account, CDK, DeliveryItem, Redelivery, RedeliveryItem, Redemption, RedemptionCDK, utcnow
from ..security import SecurityManager
from ..time import to_china_iso
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


@dataclass
class RedeliveryCandidate:
    cdk: CDK
    source_redemption: Redemption
    deliveries: list[DeliveryItem]
    recovery_expires_at: datetime


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
        self.redelivery_window_seconds = validator.settings.redelivery_window_seconds

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

    @staticmethod
    def _latest_completed_deliveries(
        session: Session,
        cdk_ids: list[str],
    ) -> dict[str, tuple[Redemption, list[DeliveryItem]]]:
        if not cdk_ids:
            return {}
        deliveries = session.scalars(
            select(DeliveryItem)
            .join(Redemption, DeliveryItem.redemption_id == Redemption.id)
            .options(joinedload(DeliveryItem.redemption))
            .where(
                DeliveryItem.cdk_id.in_(cdk_ids),
                Redemption.status == "completed",
                Redemption.completed_at.is_not(None),
            )
            .order_by(Redemption.completed_at.desc(), DeliveryItem.id)
        ).all()
        source_by_cdk: dict[str, Redemption] = {}
        deliveries_by_cdk: defaultdict[str, list[DeliveryItem]] = defaultdict(list)
        for delivery in deliveries:
            source = source_by_cdk.setdefault(delivery.cdk_id, delivery.redemption)
            if source.id == delivery.redemption_id:
                deliveries_by_cdk[delivery.cdk_id].append(delivery)
        return {
            cdk_id: (source, deliveries_by_cdk[cdk_id])
            for cdk_id, source in source_by_cdk.items()
            if deliveries_by_cdk[cdk_id]
        }

    def _create_redelivery(
        self,
        session: Session,
        *,
        candidates: list[RedeliveryCandidate],
        idempotency_key: str,
        client_ip: str,
    ) -> Redelivery:
        redelivery = Redelivery(
            idempotency_key=idempotency_key,
            status="ready",
            input_count=len(candidates),
            delivered_count=sum(len(candidate.deliveries) for candidate in candidates),
            client_ip_hash=self.security.opaque_digest(client_ip or "unknown"),
            recovery_expires_at=min(candidate.recovery_expires_at for candidate in candidates),
        )
        session.add(redelivery)
        session.flush()
        ordinal = 0
        for candidate in candidates:
            for delivery in candidate.deliveries:
                session.add(
                    RedeliveryItem(
                        redelivery_id=redelivery.id,
                        source_redemption_id=candidate.source_redemption.id,
                        cdk_id=candidate.cdk.id,
                        account_id=delivery.account_id,
                        cdk_prefix=candidate.cdk.code_prefix,
                        export_format=candidate.cdk.export_format,
                        export_fields=candidate.cdk.export_fields or "[]",
                        ordinal=ordinal,
                    )
                )
                ordinal += 1
        session.flush()
        return redelivery

    def create(
        self,
        session: Session,
        *,
        codes: list[str],
        idempotency_key: str,
        client_ip: str,
    ) -> Redemption | Redelivery:
        existing = session.scalar(
            select(Redemption)
            .options(joinedload(Redemption.cdks).joinedload(RedemptionCDK.cdk))
            .where(Redemption.idempotency_key == idempotency_key)
        )
        if existing:
            return existing
        existing_redelivery = session.scalar(
            select(Redelivery)
            .options(selectinload(Redelivery.items))
            .where(Redelivery.idempotency_key == idempotency_key)
        )
        if existing_redelivery:
            return existing_redelivery

        normalized_codes = self.normalize_codes(codes)
        if not normalized_codes:
            raise RedemptionError("invalid_cdk", "请至少输入一个 CDK")

        digests = [self.security.cdk_digest(code) for code in normalized_codes]
        cdks = session.scalars(
            select(CDK).where(CDK.code_hmac.in_(digests)).with_for_update()
        ).all()
        by_digest = {item.code_hmac: item for item in cdks}
        latest_deliveries = self._latest_completed_deliveries(session, [item.id for item in cdks])
        errors: list[dict] = []
        selected: list[CDK] = []
        redelivery_candidates: list[RedeliveryCandidate] = []
        now = utcnow()
        for line, digest in enumerate(digests, start=1):
            cdk = by_digest.get(digest)
            if not cdk:
                errors.append({"line": line, "code": "not_found", "message": "CDK 无效"})
                continue
            refresh_cdk_status(cdk)
            available = cdk.remaining_quota - cdk.reserved_quota
            if cdk.disabled:
                errors.append({"line": line, "code": "disabled", "message": "CDK 已禁用", "prefix": cdk.code_prefix})
                continue
            if available <= 0 and cdk.reserved_quota > 0:
                errors.append({"line": line, "code": "in_progress", "message": "CDK 正在兑换中", "prefix": cdk.code_prefix})
                continue
            if available <= 0 and (history := latest_deliveries.get(cdk.id)):
                source_redemption, deliveries = history
                recovery_expires_at = source_redemption.completed_at + timedelta(seconds=self.redelivery_window_seconds)
                if cdk.expires_at:
                    recovery_expires_at = min(recovery_expires_at, cdk.expires_at)
                if recovery_expires_at <= now:
                    errors.append(
                        {
                            "line": line,
                            "code": "redelivery_expired",
                            "message": "CDK 已兑换，补发时效已过",
                            "prefix": cdk.code_prefix,
                        }
                    )
                    continue
                redelivery_candidates.append(
                    RedeliveryCandidate(
                        cdk=cdk,
                        source_redemption=source_redemption,
                        deliveries=deliveries,
                        recovery_expires_at=recovery_expires_at,
                    )
                )
                continue
            if cdk.expires_at and cdk.expires_at <= now:
                errors.append({"line": line, "code": "expired", "message": "CDK 已过期", "prefix": cdk.code_prefix})
                continue
            if available <= 0:
                errors.append({"line": line, "code": "exhausted", "message": "CDK 已耗尽或未完成交付", "prefix": cdk.code_prefix})
                continue
            selected.append(cdk)
        if selected and redelivery_candidates:
            raise RedemptionError(
                "mixed_cdk_state",
                "请将新 CDK 与已兑换 CDK 分开提交",
                [
                    {"code": "new_cdk", "message": "包含可兑换的新 CDK"},
                    {"code": "already_redeemed", "message": "包含可限时补发的已兑换 CDK"},
                ],
            )
        if errors:
            if all(item["code"] == "redelivery_expired" for item in errors):
                raise RedemptionError("cdk_redelivery_expired", "CDK 已兑换，补发时效已过，请联系管理员", errors)
            raise RedemptionError("invalid_cdk", "存在无效、过期或额度不足的 CDK", errors)
        if redelivery_candidates:
            return self._create_redelivery(
                session,
                candidates=redelivery_candidates,
                idempotency_key=idempotency_key,
                client_ip=client_ip,
            )

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


def serialize_redemption(
    redemption: Redemption,
    security: SecurityManager,
    *,
    include_cdk_codes: bool = False,
) -> dict:
    def cdk_code(cdk: CDK) -> str | None:
        if not include_cdk_codes or not cdk.code_encrypted:
            return None
        try:
            return security.decrypt(cdk.code_encrypted)
        except Exception:
            return None

    return {
        "id": redemption.id,
        "status": redemption.status,
        "requested_count": redemption.requested_count,
        "delivered_count": redemption.delivered_count,
        "input_count": redemption.input_count,
        "error_code": redemption.error_code or None,
        "error_message": redemption.error_message or None,
        "created_at": to_china_iso(redemption.created_at),
        "completed_at": to_china_iso(redemption.completed_at),
        "downloaded_at": to_china_iso(redemption.downloaded_at),
        "cdks": [
            {
                "id": relation.cdk.id,
                "code": cdk_code(relation.cdk),
                "prefix": relation.cdk.code_prefix,
                "reserved_quantity": relation.reserved_quantity,
                "debited_quantity": relation.debited_quantity,
            }
            for relation in redemption.cdks
        ],
        "task_token": security.redemption_token(redemption.id, redemption.idempotency_key),
    }


def serialize_redelivery(redelivery: Redelivery, security: SecurityManager) -> dict:
    cdks_by_id: dict[str, dict] = {}
    for item in redelivery.items:
        current = cdks_by_id.setdefault(
            item.cdk_id,
            {
                "id": item.cdk_id,
                "code": None,
                "prefix": item.cdk_prefix,
                "account_count": 0,
            },
        )
        current["account_count"] += 1
    return {
        "id": redelivery.id,
        "status": f"redelivery_{redelivery.status}",
        "delivery_type": "redelivery",
        "requested_count": redelivery.delivered_count,
        "delivered_count": redelivery.delivered_count,
        "input_count": redelivery.input_count,
        "message": "CDK 已兑换，正在补发首次交付的关联账号。",
        "created_at": to_china_iso(redelivery.created_at),
        "completed_at": to_china_iso(redelivery.created_at),
        "downloaded_at": to_china_iso(redelivery.downloaded_at),
        "recovery_expires_at": to_china_iso(redelivery.recovery_expires_at),
        "cdks": list(cdks_by_id.values()),
        "task_token": security.redelivery_token(redelivery.id, redelivery.idempotency_key),
    }
