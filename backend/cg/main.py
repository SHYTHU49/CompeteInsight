"""FastAPI 应用入口。

启动方式：
    cd backend && uv run uvicorn cg.main:app --reload
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from fastapi.staticfiles import StaticFiles

from cg.api import auth, extract, runs, skills
from cg.auth import session_user_from_request
from cg.logging import get_logger, setup_logging
from cg.settings import get_settings

logger = get_logger(__name__)


class AuthenticatedStaticFiles(StaticFiles):
    """StaticFiles variant that requires a valid CompeteGraph session cookie."""

    async def get_response(self, path: str, scope):
        request = Request(scope)
        if not session_user_from_request(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
            )
        return await super().get_response(path, scope)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动 / 关闭钩子。"""
    setup_logging()
    settings = get_settings()

    # 1. 确保 data/ 目录骨架存在
    _init_data_dir(settings.data_dir)
    logger.info("data_dir_ready", path=str(settings.data_dir))

    # 2. 清理因服务重启而卡住的僵尸 run（超过 120 秒无活动的 running 状态）
    from cg.repositories.run import RunRepository
    repo = RunRepository(settings.data_dir)
    stale = await repo.mark_stale_running(stale_after_seconds=120)
    failed = [s for s in stale if s.status == "failed" and "Interrupted" in (s.current_stage or "")]
    if failed:
        logger.info("zombie_runs_cleaned", count=len(failed), run_ids=[s.run_id for s in failed])

    logger.info("app_started", version=app.version)
    yield
    logger.info("app_stopped")


def _init_data_dir(data_dir: Path) -> None:
    """与 scripts/init_data.py 等价，确保 data/ 目录骨架存在。"""
    subdirs = [
        "projects",
        "runs",
        "cache/llm",
        "cache/search",
        "cache/fetch",
        "cache/embedding",
        "skills",
        "prompts",
        "templates",
        "demo_replays",
        "archive",
        "logs",
    ]
    for sub in subdirs:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CompeteGraph API",
        version="0.1.0",
        description="AI 驱动的可溯源竞品分析 Agent 协作系统",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 静态文件：把 data/ 目录暴露为 /files，前端可以直链获取快照 / 导出
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/files", AuthenticatedStaticFiles(directory=str(data_dir)), name="files")

    # 健康检查
    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {
            "name": "CompeteGraph API",
            "version": app.version,
            "docs": "/docs",
            "health": "/health",
        }

    app.include_router(auth.router)
    app.include_router(runs.router)
    app.include_router(extract.router)
    app.include_router(skills.router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "cg.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
