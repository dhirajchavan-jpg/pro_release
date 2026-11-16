"""PostgreSQL-backed operation state; workers claim rows with row locking."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from procurement.platform.env import env_int

from procurement.domain.models import AuditEvent, IdempotencyKey, Operation, Tenant, new_id
from procurement.platform.http import request_id_context


class IdempotencyConflict(ValueError):
    pass


class QueueCapacityExceeded(RuntimeError):
    pass


class OperationStateError(RuntimeError):
    pass


class OperationLeaseError(RuntimeError):
    pass


class AuditChainError(RuntimeError):
    pass


_TRANSITIONS = {
    "queued": {"running"},
    "running": {"queued", "succeeded", "failed"},
    "succeeded": set(),
    "failed": set(),
}

_QUEUE_ADMISSION_LOCK = 1347571523
_WORK_SLOT_LOCK = 1347571524


def _stored_request_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest() if len(value) > 64 else value


def idempotent_result(
    session: Session,
    *,
    tenant_id: str,
    key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    request_hash = _stored_request_hash(request_hash)
    session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    record = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.tenant_id == tenant_id,
            IdempotencyKey.key == key,
        )
    )
    if not record:
        return None
    if record.request_hash != request_hash:
        raise IdempotencyConflict("Idempotency-Key was already used for a different request")
    operation = session.get(Operation, record.operation_id)
    if not operation or operation.status != "succeeded" or operation.result_json is None:
        raise IdempotencyConflict("Idempotency-Key references an incomplete request")
    return dict(operation.result_json)


def remember_result(
    session: Session,
    *,
    tenant_id: str,
    key: str,
    request_hash: str,
    entity_type: str,
    entity_id: str,
    stage: str,
    result: dict[str, Any],
) -> None:
    now = _now()
    operation = Operation(
        id=new_id(),
        tenant_id=tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        stage=stage,
        status="succeeded",
        result_json=result,
        started_at=now,
        finished_at=now,
    )
    session.add(operation)
    session.flush([operation])
    session.add(
        IdempotencyKey(
            id=new_id(),
            tenant_id=tenant_id,
            key=key,
            request_hash=_stored_request_hash(request_hash),
            operation_id=operation.id,
        )
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _audit_hash(
    *,
    event_id: str,
    tenant_id: str,
    actor_user_id: str | None,
    event_type: str,
    entity_type: str,
    entity_id: str,
    request_id: str | None,
    reason: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    previous_hash: str | None,
) -> str:
    canonical = json.dumps(
        {
            "id": event_id,
            "tenant_id": tenant_id,
            "actor_user_id": actor_user_id,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "request_id": request_id,
            "reason": reason,
            "before": before,
            "after": after,
            "previous_hash": previous_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_audit_chain(session: Session, tenant_id: str) -> dict[str, Any]:
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    errors: list[str] = []
    hashed = {event.event_hash: event for event in events if event.event_hash}
    if len(hashed) != len(events):
        errors.append("one or more audit events are missing an event hash")
    children: dict[str | None, list[AuditEvent]] = {}
    for event in events:
        if event.previous_hash and event.previous_hash not in hashed:
            errors.append(f"event {event.id} references an unknown predecessor")
        children.setdefault(event.previous_hash, []).append(event)
        expected = _audit_hash(
            event_id=event.id,
            tenant_id=event.tenant_id,
            actor_user_id=event.actor_user_id,
            event_type=event.event_type,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            request_id=event.request_id,
            reason=event.reason,
            before=event.before_json,
            after=event.after_json,
            previous_hash=event.previous_hash,
        )
        if event.event_hash != expected:
            errors.append(f"event {event.id} content does not match its hash")
    roots = children.get(None, [])
    if events and len(roots) != 1:
        errors.append(f"audit chain has {len(roots)} roots")
    visited: set[str] = set()
    current = roots[0] if len(roots) == 1 else None
    while current and current.event_hash and current.event_hash not in visited:
        visited.add(current.event_hash)
        next_events = children.get(current.event_hash, [])
        if len(next_events) > 1:
            errors.append(f"audit chain forks after event {current.id}")
            break
        current = next_events[0] if next_events else None
    if len(visited) != len(events):
        errors.append(f"audit chain covers {len(visited)} of {len(events)} events")
    return {"valid": not errors, "event_count": len(events), "errors": errors}


def _lease_seconds(value: int | None = None) -> int:
    return max(30, value if value is not None else env_int("OPERATION_LEASE_SECONDS", 300))


def _transition(operation: Operation, target: str, *, lease_id: str | None = None) -> None:
    current = str(operation.status)
    if target not in _TRANSITIONS.get(current, set()):
        raise OperationStateError(f"invalid operation transition: {current} -> {target}")
    if current == "running":
        expected = getattr(operation, "lease_id", None)
        if lease_id is not None and expected != lease_id:
            raise OperationLeaseError("operation lease is no longer owned by this worker")
        if expected is None and lease_id is not None:
            raise OperationLeaseError("operation has no active lease")
    operation.status = target
    operation.state_version = int(getattr(operation, "state_version", 0) or 0) + 1


def _clear_lease(operation: Operation) -> None:
    operation.lease_id = None
    operation.lease_expires_at = None
    operation.heartbeat_at = None


def _owned_transition(
    session: Session,
    operation: Operation,
    target: str,
    *,
    lease_id: str | None,
    values: dict[str, Any],
) -> None:
    current = str(operation.status)
    if target not in _TRANSITIONS.get(current, set()):
        raise OperationStateError(f"invalid operation transition: {current} -> {target}")
    if lease_id is None:
        _transition(operation, target)
        for name, value in values.items():
            setattr(operation, name, value)
        return
    if getattr(operation, "lease_id", None) != lease_id:
        raise OperationLeaseError("operation lease is no longer owned by this worker")
    result = session.execute(
        update(Operation)
        .where(
            Operation.id == operation.id,
            Operation.status == current,
            Operation.lease_id == lease_id,
        )
        .values(status=target, state_version=Operation.state_version + 1, **values)
    )
    if result.rowcount != 1:
        raise OperationLeaseError("operation lease is no longer owned by this worker")
    session.refresh(operation)


def enqueue(
    session: Session, *, tenant_id: str, entity_type: str, entity_id: str, stage: str, payload: dict[str, Any], key: str | None, request_hash: str
) -> Operation:
    request_hash = _stored_request_hash(request_hash)
    # ponytail: this deployment has one global work slot; use a tenant-scoped
    # advisory key only when the product is intentionally opened to concurrency.
    session.execute(select(func.pg_advisory_xact_lock(_QUEUE_ADMISSION_LOCK)))
    if key:
        existing = session.scalar(select(IdempotencyKey).where(IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key).with_for_update())
        if existing:
            if existing.request_hash != request_hash:
                raise IdempotencyConflict("Idempotency-Key was already used for a different request")
            return session.get(Operation, existing.operation_id)  # type: ignore[return-value]
    capacity = env_int("MAX_QUEUED_OPERATIONS", 11, minimum=1, maximum=11)
    pending = session.scalar(
        select(func.count(Operation.id)).where(Operation.status.in_(("queued", "running")))
    ) or 0
    if pending >= capacity:
        raise QueueCapacityExceeded(f"operation queue reached its configured limit of {capacity}")
    operation = Operation(id=new_id(), tenant_id=tenant_id, entity_type=entity_type, entity_id=entity_id, stage=stage, status="queued", payload_json=payload)
    if not key:
        session.add(operation)
        session.flush([operation])
        return operation
    try:
        with session.begin_nested():
            session.add(operation)
            session.flush([operation])
            session.add(IdempotencyKey(id=new_id(), tenant_id=tenant_id, key=key, request_hash=request_hash, operation_id=operation.id))
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(IdempotencyKey)
            .where(IdempotencyKey.tenant_id == tenant_id, IdempotencyKey.key == key)
            .with_for_update()
        )
        if not existing:
            raise
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("Idempotency-Key was already used for a different request")
        existing_operation = session.get(Operation, existing.operation_id)
        if not existing_operation:
            raise RuntimeError("idempotency key references a missing operation")
        return existing_operation
    return operation


def claim_next(session: Session, *, stage: str | None = None, lease_seconds: int | None = None) -> Operation | None:
    session.execute(select(func.pg_advisory_xact_lock(_WORK_SLOT_LOCK)))
    running = session.scalar(select(func.count(Operation.id)).where(Operation.status == "running")) or 0
    if running:
        return None
    now = _now()
    query = select(Operation).where(
        Operation.status == "queued",
        Operation.available_at <= now,
    )
    if stage:
        query = query.where(Operation.stage == stage)
    operation = session.scalar(
        query.order_by(Operation.available_at, Operation.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not operation:
        return None
    _transition(operation, "running")
    operation.attempt += 1
    operation.lease_id = new_id()
    operation.heartbeat_at = now
    operation.lease_expires_at = now + timedelta(seconds=_lease_seconds(lease_seconds))
    operation.started_at = now
    operation.error_code = None
    operation.error_detail = None
    return operation


def heartbeat(session: Session, operation: Operation, *, lease_id: str, lease_seconds: int | None = None) -> None:
    if operation.status != "running" or operation.lease_id != lease_id:
        raise OperationLeaseError("operation lease is no longer owned by this worker")
    now = _now()
    result = session.execute(
        update(Operation)
        .where(Operation.id == operation.id, Operation.status == "running", Operation.lease_id == lease_id)
        .values(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=_lease_seconds(lease_seconds)),
        )
    )
    if result.rowcount != 1:
        raise OperationLeaseError("operation lease is no longer owned by this worker")
    session.refresh(operation)


def recover_interrupted(
    session: Session,
    *,
    maximum_attempts: int,
    stale_after_seconds: int | None = None,
    now: datetime | None = None,
) -> int:
    now = _utc(now or _now())
    stale_after = max(
        30,
        stale_after_seconds
        if stale_after_seconds is not None
        else env_int(
            "OPERATION_STALE_SECONDS",
            env_int("OPERATION_LEASE_SECONDS", 300),
        ),
    )
    cutoff = now - timedelta(seconds=stale_after)
    operations = list(
        session.scalars(
            select(Operation)
            .where(
                Operation.status == "running",
                or_(
                    Operation.lease_expires_at <= now,
                    and_(
                        Operation.lease_expires_at.is_(None),
                        or_(
                            Operation.heartbeat_at <= cutoff,
                            and_(Operation.heartbeat_at.is_(None), Operation.started_at <= cutoff),
                        ),
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
    )
    recovered = 0
    for operation in operations:
        lease_expires_at = _utc(getattr(operation, "lease_expires_at", None))
        heartbeat_at = _utc(getattr(operation, "heartbeat_at", None))
        started_at = _utc(getattr(operation, "started_at", None))
        stale = (
            lease_expires_at is not None and lease_expires_at <= now
        ) or (
            lease_expires_at is None
            and (heartbeat_at or started_at) is not None
            and (heartbeat_at or started_at) <= cutoff
        )
        if not stale:
            continue
        recovered += 1
        if operation.attempt >= maximum_attempts:
            fail(session, operation, code="restart_attempts_exhausted", detail="operation interrupted too many times")
        else:
            _transition(operation, "queued")
            operation.available_at = now
            operation.started_at = None
            operation.result_json = None
            operation.progress_version = int(getattr(operation, "progress_version", 0) or 0) + 1
            _clear_lease(operation)
            operation.error_code = "worker_restarted"
            operation.error_detail = "operation returned to queue after worker restart"
    return recovered


def finish(session: Session, operation: Operation, result: dict[str, Any], *, lease_id: str | None = None) -> None:
    finished_at = _now()
    _owned_transition(
        session,
        operation,
        "succeeded",
        lease_id=lease_id,
        values={
            "result_json": result,
            "finished_at": finished_at,
            "lease_id": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
        },
    )


def retry_later(
    session: Session,
    operation: Operation,
    *,
    code: str,
    detail: str,
    delay_seconds: int,
    lease_id: str | None = None,
) -> None:
    _owned_transition(
        session,
        operation,
        "queued",
        lease_id=lease_id,
        values={
            "available_at": _now() + timedelta(seconds=max(1, delay_seconds)),
            "started_at": None,
            "finished_at": None,
            "result_json": None,
            "progress_version": int(getattr(operation, "progress_version", 0) or 0) + 1,
            "lease_id": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "error_code": code,
            "error_detail": detail[:500],
        },
    )


def fail(session: Session, operation: Operation, *, code: str, detail: str, lease_id: str | None = None) -> None:
    _owned_transition(
        session,
        operation,
        "failed",
        lease_id=lease_id,
        values={
            "error_code": code,
            "error_detail": detail[:500],
            "finished_at": _now(),
            "lease_id": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
        },
    )


def audit(session: Session, *, tenant_id: str, actor_user_id: str | None, event_type: str, entity_type: str, entity_id: str, reason: str | None, before: dict[str, Any] | None, after: dict[str, Any] | None) -> AuditEvent:
    session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    # ponytail: the one-user trial can afford this integrity-first tail check;
    # replace it with a tenant sequence when audit volume becomes substantial.
    events = list(session.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)))
    referenced = {event.previous_hash for event in events if event.previous_hash}
    tails = [event for event in events if event.event_hash and event.event_hash not in referenced]
    if events and len(tails) != 1:
        raise AuditChainError("existing audit chain is incomplete or forked")
    previous = tails[0] if tails else None
    event_id = new_id()
    previous_hash = previous.event_hash if previous else None
    request_id = request_id_context.get()
    event = AuditEvent(
        id=event_id,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=request_id,
        reason=reason,
        before_json=before,
        after_json=after,
        previous_hash=previous_hash,
        event_hash=_audit_hash(
            event_id=event_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=request_id,
            reason=reason,
            before=before,
            after=after,
            previous_hash=previous_hash,
        ),
        occurred_at=_now(),
    )
    session.add(event)
    return event
