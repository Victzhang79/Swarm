"""api/routers/skills.py — 经验技能（experience tools）系统级管理路由。

系统级、跨项目:内置种子（skills_library/，只读）+ 用户在此编写/导入的技能（落 DB）。
每次写入都过 experience.validation 【导入准入闸】——挡住乱七八糟/意图不明/密钥/注入/
"标题说读正文却写"这类不一致技能。读需登录,写需 config:write（admin）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from swarm.api._shared import _require_perm, _require_user
from swarm.config import skill_store
from swarm.experience.models import SkillDoc
from swarm.experience.validation import validate_skill_doc, validate_skill_text

router = APIRouter()


class SkillPayload(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    body: str = ""
    applies_to_stacks: list[str] = Field(default_factory=lambda: ["*"])
    applies_to_intents: list[str] = Field(default_factory=lambda: ["*"])
    applies_to_phases: list[str] = Field(default_factory=lambda: ["*"])
    target: list[str] = Field(default_factory=lambda: ["worker"])
    priority: int = 50
    max_chars: int = 1200
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class ImportPayload(BaseModel):
    text: str                          # 一份带 frontmatter 的技能 .md 原文
    enabled: bool = True
    use_llm_judge: bool | None = None  # None=按配置默认


class ValidatePayload(BaseModel):
    text: str = ""                     # 二选一:原始 .md 文本
    skill: SkillPayload | None = None  # 或结构化字段
    use_llm_judge: bool | None = None


def _payload_to_doc(p: SkillPayload) -> SkillDoc:
    return SkillDoc(
        id=p.id, title=p.title or p.id, body=p.body, target=tuple(p.target),
        applies_to_stacks=tuple(p.applies_to_stacks),
        applies_to_intents=tuple(p.applies_to_intents),
        applies_to_phases=tuple(p.applies_to_phases),
        priority=p.priority, max_chars=p.max_chars, summary=p.description,
        enabled=bool(getattr(p, "enabled", True)),  # E9-12：preview 对 disabled 保存如实
        tags=tuple(p.tags),
    )


def _doc_to_store(doc: SkillDoc, *, enabled: bool, source: str) -> dict:
    return {
        "id": doc.id, "title": doc.title, "description": doc.summary, "body": doc.body,
        "applies_to_stacks": list(doc.applies_to_stacks),
        "applies_to_intents": list(doc.applies_to_intents),
        "applies_to_phases": list(doc.applies_to_phases),
        "target": list(doc.target), "priority": doc.priority, "max_chars": doc.max_chars,
        "tags": list(doc.tags), "enabled": enabled, "source": source,
    }


def _result_payload(r) -> dict:
    return {"ok": r.ok, "errors": r.errors, "warnings": r.warnings,
            "llm_checked": r.llm_checked}


@router.get("/api/skills", tags=["技能"])
def list_skills(request: Request) -> dict:
    """列出系统级技能:内置种子（source=builtin，只读）+ DB 用户技能（可编辑）。"""
    _require_user(request)
    from swarm.config.settings import PROJECT_ROOT
    from swarm.experience.library import load_skills

    def _view(d: SkillDoc, source: str, editable: bool, enabled: bool) -> dict:
        return {
            "id": d.id, "title": d.title, "description": d.summary, "body": d.body,
            "applies_to_stacks": list(d.applies_to_stacks),
            "applies_to_intents": list(d.applies_to_intents),
            "applies_to_phases": list(d.applies_to_phases),
            "target": list(d.target), "priority": d.priority, "max_chars": d.max_chars,
            "tags": list(d.tags), "source": source, "editable": editable, "enabled": enabled,
        }

    builtin = [_view(d, "builtin", False, bool(getattr(d, "enabled", True)))  # E9-12：下架如实透出
               for d in load_skills(PROJECT_ROOT / "skills_library")]
    db_rows = skill_store.get_all()
    db_ids = {r["id"] for r in db_rows}
    # DB 同 id 覆盖内置:内置项标记 overridden
    for b in builtin:
        b["overridden"] = b["id"] in db_ids
    db = [{**r, "source": r.get("source", "user"), "editable": True} for r in db_rows]
    return {"builtin": builtin, "db": db, "total": len(builtin) + len(db)}


@router.post("/api/skills/validate", tags=["技能"])
def validate_skill(request: Request, payload: ValidatePayload) -> dict:
    """干跑准入闸(不落库),前端"校验"按钮用。"""
    _require_user(request)
    if payload.skill is not None:
        r = validate_skill_doc(_payload_to_doc(payload.skill), use_llm_judge=payload.use_llm_judge)
    elif payload.text.strip():
        r = validate_skill_text(payload.text, use_llm_judge=payload.use_llm_judge)
    else:
        raise HTTPException(status_code=400, detail="需提供 text 或 skill 之一")
    return _result_payload(r)


@router.post("/api/skills/preview", tags=["技能"])
def preview_skill(request: Request, payload: ValidatePayload) -> dict:
    """G9：挂载预览（干跑，不落库不调 LLM）——展示该技能会出现在哪些 栈×意图 面及排位。"""
    _require_user(request)
    from swarm.experience.service import preview_mount_surfaces
    if payload.skill is not None:
        doc = _payload_to_doc(payload.skill)
    elif payload.text.strip():
        from swarm.experience.library import parse_skill_text
        doc = parse_skill_text(payload.text, source_path="<preview>")
        if doc is None:
            raise HTTPException(status_code=400, detail="文本无法解析为技能")
    else:
        raise HTTPException(status_code=400, detail="需提供 text 或 skill 之一")
    return preview_mount_surfaces(doc)


def _admit_and_store(request: Request, doc_result, *, enabled: bool, source: str) -> dict:
    """准入闸通过则落库,否则 422 带 errors。调用方须已做 _require_perm(见下 3 个写端点)。"""
    if not doc_result.ok:
        raise HTTPException(status_code=422, detail={
            "message": "技能未通过准入校验,已拒绝", **_result_payload(doc_result)})
    skill_store.upsert(_doc_to_store(doc_result.doc, enabled=enabled, source=source))
    return {"ok": True, "id": doc_result.doc.id, "warnings": doc_result.warnings,
            "llm_checked": doc_result.llm_checked}


@router.post("/api/skills", tags=["技能"])
def create_skill(request: Request, payload: SkillPayload) -> dict:
    """新建/覆盖一条技能（结构化字段）。经准入闸后落库。"""
    _require_perm(request, "config:write")  # 先鉴权,再跑含 LLM 的校验,防未授权触发模型调用
    r = validate_skill_doc(_payload_to_doc(payload))
    return _admit_and_store(request, r, enabled=payload.enabled, source="user")


@router.put("/api/skills/{skill_id}", tags=["技能"])
def update_skill(request: Request, skill_id: str, payload: SkillPayload) -> dict:
    _require_perm(request, "config:write")
    if skill_id != payload.id:
        raise HTTPException(status_code=400, detail="路径 id 与 body id 不一致")
    r = validate_skill_doc(_payload_to_doc(payload))
    return _admit_and_store(request, r, enabled=payload.enabled, source="user")


@router.post("/api/skills/import", tags=["技能"])
def import_skill(request: Request, payload: ImportPayload) -> dict:
    """导入一份 .md 原文（支持第三方 SKILL.md）。经准入闸后落库。"""
    _require_perm(request, "config:write")
    r = validate_skill_text(payload.text, use_llm_judge=payload.use_llm_judge)
    return _admit_and_store(request, r, enabled=payload.enabled, source="import")


@router.delete("/api/skills/{skill_id}", tags=["技能"])
def delete_skill(request: Request, skill_id: str) -> dict:
    _require_perm(request, "config:write")
    deleted = skill_store.delete(skill_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="技能不存在（内置种子不可删,请改 enabled）")
    return {"ok": True, "id": skill_id}


@router.post("/api/skills/{skill_id}/enabled", tags=["技能"])
def toggle_enabled(request: Request, skill_id: str, enabled: bool = True) -> dict:
    _require_perm(request, "config:write")
    updated = skill_store.set_enabled(skill_id, enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="技能不存在于 DB")
    return {"ok": True, "id": skill_id, "enabled": enabled}
