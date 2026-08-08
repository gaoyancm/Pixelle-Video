"""Application service for the phase 04-A prompt template API."""

from __future__ import annotations

from typing import Any

from pixelle_video.prompts.antislop import AntiSlopChecker
from pixelle_video.prompts.compiler import PromptCompileError, compile, validate
from pixelle_video.prompts.repository import PromptRepository


class PromptApplicationService:
    """Own API business rules and short transactional repository calls."""

    def __init__(self, repository: PromptRepository):
        self.repository = repository
        self.antislop = AntiSlopChecker()

    # --- P1 CRUD -------------------------------------------------------------

    async def create(self, body) -> Any:
        return await self.repository.create_template(
            name=body.name,
            category=body.category,
            template_text=body.template_text,
            description=body.description,
            variables_json=[variable.model_dump() for variable in body.variables],
            provider=body.provider,
            change_note=body.change_note,
        )

    async def list(self, *, category: str | None, is_active: bool | None, limit: int, offset: int):
        return await self.repository.list_templates(
            category=category, is_active=is_active, limit=limit, offset=offset
        )

    async def get(self, template_id: str):
        template = await self.repository.get_template(template_id)
        if template is None:
            from pixelle_video.prompts.repository import PromptNotFoundError

            raise PromptNotFoundError("prompt template not found")
        return template

    async def update(self, template_id: str, body) -> Any:
        return await self.repository.update_template(
            template_id,
            name=body.name,
            category=body.category,
            description=body.description,
            template_text=body.template_text,
            variables_json=(
                [variable.model_dump() for variable in body.variables]
                if body.variables is not None
                else None
            ),
            provider=body.provider,
            is_active=(1 if body.is_active else 0) if body.is_active is not None else None,
            change_note=body.change_note,
        )

    async def archive(self, template_id: str):
        return await self.repository.archive_template(template_id)

    async def categories(self):
        return await self.repository.list_categories()

    # --- P2 compile / validate ------------------------------------------------

    async def compile_prompt(self, template_id: str, variables: dict[str, Any]) -> dict:
        template = await self.get(template_id)
        if template.is_active != 1 or template.archived_at is not None:
            raise PromptCompileError("inactive or archived template cannot be compiled")
        prompt = compile(
            template.template_text,
            variables,
            variable_defs=template.variables_json,
        )
        await self.repository.record_usage(template_id)
        return {"id": template.id, "name": template.name, "prompt": prompt}

    async def validate_prompt(self, template_id: str, variables: dict[str, Any]) -> dict:
        template = await self.get(template_id)
        issues = validate(template.template_text, template.variables_json, variables)
        return {"id": template.id, "valid": not issues, "issues": issues}

    # --- P3 versions / rollback / rate -----------------------------------------

    async def versions(self, template_id: str):
        return await self.repository.list_versions(template_id)

    async def rollback(self, template_id: str, version: int, change_note: str | None = None):
        return await self.repository.rollback(template_id, version, change_note=change_note)

    async def rate(self, template_id: str, score: int, comment: str | None = None):
        return await self.repository.rate_template(template_id, score=score, comment=comment)

    # --- P4 tags / search ------------------------------------------------------

    async def list_tags(self):
        return await self.repository.list_tags()

    async def bind_tags(self, template_id: str, tag_ids: list[str]):
        return await self.repository.bind_tags(template_id, tag_ids)

    async def template_tags(self, template_id: str):
        return await self.repository.template_tags(template_id)

    async def search(
        self, *, tag: str | None, category: str | None, q: str | None, limit: int, offset: int
    ):
        return await self.repository.search(
            tag=tag, category=category, q=q, limit=limit, offset=offset
        )

    # --- P5 anti-slop ----------------------------------------------------------

    async def check_quality(self, template_id: str) -> dict:
        template = await self.get(template_id)
        return self.antislop.check(template.template_text)
