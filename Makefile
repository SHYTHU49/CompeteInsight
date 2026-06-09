# ============================================================
# CompeteGraph - Makefile
# 一站式开发命令（Windows Git Bash / Mac / Linux 通用）
# ============================================================

.PHONY: help init install dev dev-backend dev-frontend test lint clean

help:
	@echo "CompeteGraph - 开发命令"
	@echo ""
	@echo "  make init           初始化 data/ 目录骨架"
	@echo "  make install        安装所有依赖（后端 uv + 前端 pnpm）"
	@echo "  make dev            同时启动前后端开发服务器"
	@echo "  make dev-backend    仅启动后端（http://localhost:8000）"
	@echo "  make dev-frontend   仅启动前端（http://localhost:5173）"
	@echo "  make test           运行所有测试"
	@echo "  make lint           代码风格检查"
	@echo "  make clean          清理缓存与构建产物"
	@echo ""

# ============================================================
# 初始化
# ============================================================

init:
	@echo ">>> 初始化 data/ 目录..."
	@python scripts/init_data.py
	@echo ">>> 完成"

install:
	@echo ">>> 安装后端依赖..."
	cd backend && uv sync
	@echo ">>> 安装前端依赖..."
	cd frontend && pnpm install
	@echo ">>> 完成"

# ============================================================
# 开发
# ============================================================

dev-backend:
	cd backend && uv run uvicorn cg.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend:
	cd frontend && pnpm dev

# 同时启动（用 & 后台 + wait；Windows 用 start 也行）
dev:
	@echo ">>> 同时启动前后端..."
	@$(MAKE) -j 2 dev-backend dev-frontend

# ============================================================
# 测试 / 检查
# ============================================================

test:
	cd backend && uv run pytest -v
	cd frontend && pnpm test

lint:
	cd backend && uv run ruff check .
	cd backend && uv run mypy cg
	cd frontend && pnpm lint

# ============================================================
# 清理
# ============================================================

clean:
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache
	rm -rf frontend/node_modules/.vite frontend/dist
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo ">>> 清理完成"
