"""Short-transaction repository for the phase 04-B QC pipeline."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import QCProfile, QCRule


class QCRuleNotFoundError(RuntimeError):
    pass


class QCProfileNotFoundError(RuntimeError):
    pass


class QCRuleNameConflictError(RuntimeError):
    pass


class QCProfileNameConflictError(RuntimeError):
    pass


class QCRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def create_rule(
        self,
        *,
        name: str,
        category: str,
        rule_type: str,
        rule_config_json: dict[str, Any],
        provider: str = "local",
        is_active: int = 1,
        priority: int = 1,
        rule_id: str | None = None,
    ) -> QCRule:
        rule = QCRule(
            id=rule_id or str(uuid.uuid4()),
            name=name,
            category=category,
            rule_type=rule_type,
            rule_config_json=rule_config_json,
            provider=provider,
            is_active=is_active,
            priority=priority,
        )
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    session.add(rule)
                    await session.flush()
            except Exception:
                raise QCRuleNameConflictError(f"qc rule name already exists: {name}") from None
        return rule

    async def get_rule(self, rule_id: str) -> QCRule | None:
        async with self._session_factory() as session:
            return await session.get(QCRule, rule_id)

    async def list_rules(
        self, *, category: str | None = None, is_active: bool | None = None
    ) -> list[QCRule]:
        statement = select(QCRule).order_by(QCRule.priority.asc(), QCRule.id.asc())
        if category is not None:
            statement = statement.where(QCRule.category == category)
        if is_active is not None:
            statement = statement.where(QCRule.is_active == (1 if is_active else 0))
        async with self._session_factory() as session:
            return list((await session.execute(statement)).scalars())

    async def update_rule(
        self,
        rule_id: str,
        *,
        name: str | None = None,
        category: str | None = None,
        rule_type: str | None = None,
        rule_config_json: dict[str, Any] | None = None,
        provider: str | None = None,
        is_active: int | None = None,
        priority: int | None = None,
    ) -> QCRule:
        async with self._session_factory() as session:
            async with session.begin():
                rule = await session.get(QCRule, rule_id)
                if rule is None:
                    raise QCRuleNotFoundError("qc rule not found")
                if name is not None:
                    rule.name = name
                if category is not None:
                    rule.category = category
                if rule_type is not None:
                    rule.rule_type = rule_type
                if rule_config_json is not None:
                    rule.rule_config_json = rule_config_json
                if provider is not None:
                    rule.provider = provider
                if is_active is not None:
                    rule.is_active = is_active
                if priority is not None:
                    rule.priority = priority
                await session.flush()
                return rule

    async def create_profile(
        self,
        *,
        name: str,
        rules_json: list[dict[str, Any]],
        description: str | None = None,
        is_default: int = 0,
        profile_id: str | None = None,
    ) -> QCProfile:
        profile = QCProfile(
            id=profile_id or str(uuid.uuid4()),
            name=name,
            description=description,
            rules_json=rules_json,
            is_default=is_default,
        )
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    session.add(profile)
                    await session.flush()
            except Exception:
                raise QCProfileNameConflictError(
                    f"qc profile name already exists: {name}"
                ) from None
        return profile

    async def list_profiles(self) -> list[QCProfile]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(QCProfile).order_by(QCProfile.is_default.desc(), QCProfile.name.asc())
            )
            return list(result.scalars())

    async def get_profile(self, profile_id: str) -> QCProfile | None:
        async with self._session_factory() as session:
            return await session.get(QCProfile, profile_id)

    async def get_default_profile(self) -> QCProfile | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(QCProfile)
                .where(QCProfile.is_default == 1)
                .order_by(QCProfile.id.asc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def resolve_profile_rules(self, profile: QCProfile) -> list[tuple[QCRule, str | None]]:
        """Resolve a profile's rule references into ordered (rule, severity_override) pairs."""
        entries = profile.rules_json or []
        rule_ids = [entry.get("rule_id") for entry in entries if entry.get("rule_id")]
        rules: dict[str, QCRule] = {}
        if rule_ids:
            async with self._session_factory() as session:
                result = await session.execute(select(QCRule).where(QCRule.id.in_(rule_ids)))
                rules = {rule.id: rule for rule in result.scalars()}
        ordered: list[tuple[QCRule, str | None]] = []
        for entry in entries:
            rule = rules.get(entry.get("rule_id"))
            if rule is not None and rule.is_active == 1:
                ordered.append((rule, entry.get("severity_override")))
        ordered.sort(key=lambda item: (item[0].priority, item[0].id))
        return ordered
