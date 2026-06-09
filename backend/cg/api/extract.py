"""Quick URL extraction API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cg.auth import require_session_user
from cg.orchestrator.pipeline import ResearchPipeline
from cg.schemas.research import QuickExtractRequest, QuickExtractResponse

router = APIRouter(prefix="/api", tags=["extract"])


@router.post("/quick-extract", response_model=QuickExtractResponse)
async def quick_extract(
    request: QuickExtractRequest,
    username: str = Depends(require_session_user),
) -> QuickExtractResponse:
    try:
        status, evidence = await ResearchPipeline().quick_extract(request, owner=username)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {request.run_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {request.run_id}") from exc
    return QuickExtractResponse(status=status, evidence=evidence)
