"""Skill contract APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends

from cg.auth import require_session_user
from cg.settings import REPO_ROOT

router = APIRouter(prefix="/api", tags=["skills"])


@router.get("/skills", response_model=list[dict[str, Any]])
async def list_skills(username: str = Depends(require_session_user)) -> list[dict[str, Any]]:
    _ = username
    skills_dir = REPO_ROOT / "skills"
    contracts: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return contracts
    for path in sorted(skills_dir.glob("*.yaml")):
        data = _read_skill_contract(path)
        if data:
            data["_file"] = str(path.relative_to(REPO_ROOT))
            contracts.append(data)
    return contracts


def _read_skill_contract(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    return loaded if isinstance(loaded, dict) else {}
