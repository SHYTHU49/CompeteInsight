"""A real, lightweight research pipeline for the first runnable system."""

from __future__ import annotations

import hashlib
import asyncio
import re
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse
from uuid import uuid4

from cg.agents import (
    AGENT_SKILLS,
    AgentContext,
    AnalysisAndReviewAgent,
    EvidenceStructuringAgent,
    RESEARCH_AGENT_FLOW,
    ReportComposerAgent,
    ResearchPlanningAgent,
    SourceResearchAgent,
)
from cg.llm import LLMClient
from cg.repositories.base import append_jsonl, atomic_write_json, write_text
from cg.repositories.evidence import EvidenceRepository
from cg.repositories.run import RunRepository
from cg.schemas.research import (
    BattlecardItem,
    Claim,
    CompetitorMatrix,
    CompetitorProfile,
    DEFAULT_DIMENSIONS,
    DIMENSION_LABELS,
    Evidence,
    EvidenceGraph,
    EvidenceGraphEdge,
    EvidenceGraphNode,
    MatrixCell,
    ObservabilitySnapshot,
    OpportunityRecommendation,
    QualityGate,
    QuickExtractRequest,
    RedTeamNote,
    ResearchFeedback,
    ResearchPlan,
    ResearchRequest,
    RunMetrics,
    RunStatus,
    SearchMemory,
    SourceCandidate,
    SourceDocument,
    TraceEvent,
)
from cg.settings import Settings, get_settings
from cg.tools.fetcher import Fetcher, RawPage
from cg.tools.search import SearchTool, classify_source


DIMENSION_KEYWORDS: dict[str, list[str]] = {
    "positioning": [
        "position",
        "mission",
        "built for",
        "designed for",
        "target",
        "developer",
        "定位",
        "面向",
        "适合",
    ],
    "feature": [
        "feature",
        "capability",
        "code",
        "agent",
        "completion",
        "context",
        "功能",
        "能力",
        "代码",
        "智能体",
    ],
    "pricing": [
        "pricing",
        "price",
        "free",
        "pro",
        "team",
        "enterprise",
        "$",
        "billing",
        "subscription",
        "定价",
        "价格",
        "免费",
        "订阅",
    ],
    "user_voice": [
        "review",
        "customer",
        "user",
        "feedback",
        "community",
        "rating",
        "用户",
        "评价",
        "反馈",
        "社区",
    ],
    "enterprise": [
        "enterprise",
        "security",
        "privacy",
        "admin",
        "sso",
        "compliance",
        "team",
        "企业",
        "安全",
        "隐私",
        "权限",
        "团队",
    ],
    "strategy": [
        "launch",
        "roadmap",
        "market",
        "partnership",
        "workflow",
        "productivity",
        "发布",
        "路线图",
        "市场",
        "机会",
    ],
}


class RunStopped(Exception):
    """Raised when the user requests a run to stop."""


class ResearchPipeline:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.runs = RunRepository(self.settings.data_dir)
        self.search = SearchTool(self.settings)
        self.fetcher = Fetcher(self.settings)
        self.llm = LLMClient(self.settings)

    async def prepare_run(self, request: ResearchRequest, owner: str | None = None) -> RunStatus:
        return await self.runs.create(request, owner=owner)

    async def check_stop(self, run_id: str) -> None:
        if self.runs.is_stop_requested(run_id):
            raise RunStopped("Stopped by user")

    async def run(self, request: ResearchRequest, run_id: str) -> None:
        status = await self.runs.load_status(run_id)
        metrics = RunMetrics()

        if not self.llm.is_configured:
            status.status = "failed"
            status.current_stage = "Failed"
            status.error = (
                "⚠️ LLM API Key 未配置。CompeteGraph 需要 LLM 才能运行智能体搜索与分析。"
                "请在 backend/.env 中填写 ARK_API_KEY / DEEPSEEK_API_KEY / QWEN_API_KEY，"
                "然后重启后端服务并重新发起研究。"
            )
            await self.runs.save_status(status)
            await self.trace(run_id, "Pipeline", "error", "failed", status.error)
            return

        try:
            status.status = "running"
            status.current_stage = "Planning"
            await self.runs.save_status(status)

            ctx = self.agent_context(run_id)
            max_loops = self.settings.cg_max_research_loops

            # ── 外层循环：Planning → Search → Analysis（可多轮）──
            feedback: ResearchFeedback | None = None
            all_evidence: list[Evidence] = []
            all_documents: list[SourceDocument] = []
            all_candidates: list[SourceCandidate] = []
            plan: ResearchPlan | None = None
            search_memory = SearchMemory(loop_round=1, remaining_source_slots=request.max_sources)

            for loop_round in range(1, max_loops + 1):
                await self.check_stop(run_id)
                loop_label = f"第 {loop_round} 轮"

                # ── Planning ──
                async with self.node(run_id, status, "ResearchPlanningAgent",
                                     f"{loop_label}：规划研究范围、搜索策略和质量规则"):
                    plan = await ResearchPlanningAgent(ctx).plan(
                        request, feedback=feedback, loop_round=loop_round
                    )
                    await atomic_write_json(self.runs.run_dir(run_id) / "plan.json", plan)
                    await atomic_write_json(self.runs.run_dir(run_id) / "dag.json", build_dag_snapshot(plan))

                # ── Source Research（ReAct 自主搜索）──
                async with self.node(run_id, status, "SourceResearchAgent",
                                     f"{loop_label}：两阶段内容搜索（批量Query快速获取信息 + Search LLM针对性补充）"):
                    source_agent = SourceResearchAgent(ctx)
                    candidates = await source_agent.discover(
                        request,
                        plan,
                        self.search,
                        memory=search_memory,
                        feedback=feedback,
                        loop_round=loop_round,
                    )
                    # 去重合并（多轮累积） — 保存所有发现的候选，不限制数量
                    existing_urls = {c.url for c in all_candidates}
                    remaining_slots = max(0, request.max_sources - len(all_candidates))
                    new_candidates = [c for c in candidates if c.url not in existing_urls][:remaining_slots]
                    dropped_for_limit = max(0, len([c for c in candidates if c.url not in existing_urls]) - len(new_candidates))
                    all_candidates.extend(new_candidates)
                    search_memory = SearchMemory(
                        seen_urls=[c.url for c in all_candidates],
                        seen_queries=unique_strings([c.query for c in all_candidates if c.query]),
                        remaining_source_slots=max(0, request.max_sources - len(all_candidates)),
                        resource_limit_reached=len(all_candidates) >= request.max_sources,
                        loop_round=loop_round,
                        feedback_message=feedback.message if feedback else "",
                    )
                    metrics.source_candidates = len(all_candidates)
                    # 保存全部新候选（不截断，让 UI 能展示所有来源）
                    await self.save_candidates(run_id, new_candidates)
                    if dropped_for_limit or len(all_candidates) >= request.max_sources:
                        await self.trace(
                            run_id,
                            "SourceResearchAgent",
                            "progress",
                            "resource_limit_reached",
                            f"已达到最大来源数 {request.max_sources}，停止保存和搜索更多来源，后续按现有内容生成报告",
                            {
                                "max_sources": request.max_sources,
                                "saved_sources": len(all_candidates),
                                "dropped_candidates": dropped_for_limit,
                            },
                        )
                    if not all_candidates:
                        status.warnings.append(
                            "未发现可用来源。请配置搜索 API Key（TAVILY_API_KEY / EXA_API_KEY / ZHIHU_API_KEY）"
                            "或填写 Seed URL。"
                        )

                    new_documents = await self.materialize_search_documents(run_id, new_candidates)
                    fallback_candidates = [
                        candidate
                        for candidate in sorted(new_candidates, key=lambda c: c.score, reverse=True)
                        if not candidate.content.strip()
                    ][: max(0, min(request.max_sources, 20))]
                    if fallback_candidates:
                        fetched_documents = await source_agent.collect(
                            fallback_candidates,
                            self.fetcher,
                            lambda candidate, page: self.save_document(run_id, candidate, page),
                            lambda phase, event_status, message, payload=None: self.trace(
                                run_id,
                                "SourceResearchAgent",
                                phase,
                                event_status,
                                f"本地抓取{'完成' if event_status == 'fetched' else '失败'}：{message}",
                                payload,
                            ),
                        )
                        new_documents.extend(fetched_documents)
                    all_documents.extend(new_documents)
                    metrics.sources_fetched = len([d for d in all_documents if d.ok])
                    metrics.sources_failed = len([d for d in all_documents if not d.ok])
                    status.metrics = metrics
                    await self.runs.save_status(status)

                # ── Evidence Structuring ──
                async with self.node(run_id, status, "EvidenceStructuringAgent",
                                     f"{loop_label}：从已采集正文中抽取结构化 Evidence"):
                    new_evidence = await self.extract_and_save_evidence(
                        run_id, request, plan, new_documents, existing_evidence=all_evidence
                    )
                    # 去重合并（按 evidence_id）
                    existing_ev_ids = {ev.evidence_id for ev in all_evidence}
                    unique_new = [ev for ev in new_evidence if ev.evidence_id not in existing_ev_ids]
                    all_evidence.extend(unique_new)
                    metrics.evidence_count = len(all_evidence)
                    status.metrics = metrics
                    await self.runs.save_status(status)
                    if not all_evidence:
                        status.warnings.append("抓取完成，但没有抽取到有效 Evidence。")

                # ── Analysis & Review（含缺口评估）──
                async with self.node(run_id, status, "AnalysisAndReviewAgent",
                                     f"{loop_label}：生成 Claim、执行 Red Team 审查并评估证据缺口"):
                    claims = await self.generate_claims(run_id, all_evidence, request)
                    metrics.claim_count = len(claims)
                    reviewed_claims = await self.red_team(run_id, claims, all_evidence)
                    metrics.verified_claim_count = len(
                        [c for c in reviewed_claims if c.verification_status == "verified"]
                    )
                    metrics.challenged_claim_count = len(
                        [c for c in reviewed_claims if c.verification_status != "verified"]
                    )
                    status.metrics = metrics
                    await self.runs.save_status(status)

                    # 构建矩阵用于覆盖度计算
                    dimensions = plan.dimensions or request.analysis_dimensions or DEFAULT_DIMENSIONS
                    competitors = [request.target_product, *request.competitors]
                    interim_matrix = build_competitor_matrix(all_evidence, list(dict.fromkeys(competitors)), dimensions)
                    coverage_score = sum(interim_matrix.coverage_by_dimension.values()) / max(
                        1, len(interim_matrix.coverage_by_dimension)
                    )

                    # Analysis Agent 评估缺口
                    analysis_agent = AnalysisAndReviewAgent(ctx)
                    feedback = await analysis_agent.assess_gaps(
                        request, all_evidence, reviewed_claims, loop_round, coverage_score
                    )
                    await self.trace(
                        run_id, "AnalysisAndReviewAgent", "progress", "gap_assessed",
                        (
                            f"证据缺口评估完成：发现 {len(feedback.gaps)} 个待补充缺口，准备进入第 {loop_round + 1} 轮补充研究"
                            if feedback.needs_more_research
                            else f"证据覆盖已满足当前质量门禁，研究链路将在第 {loop_round} 轮后收敛，无需启动第 {loop_round + 1} 轮"
                        ),
                        {"loop_round": loop_round, "needs_more": feedback.needs_more_research,
                         "gap_count": len(feedback.gaps), "coverage": coverage_score},
                    )

                # 是否继续循环
                if not feedback.needs_more_research or loop_round >= max_loops or len(all_candidates) >= request.max_sources:
                    if feedback.needs_more_research and loop_round >= max_loops:
                        await self.trace(
                            run_id,
                            "Pipeline",
                            "progress",
                            "loop_limit_reached",
                            f"已达到外层最大研究轮次 {max_loops}，仍有 {len(feedback.gaps)} 个缺口；停止补充搜索并进入报告生成",
                            {
                                "loop_round": loop_round,
                                "max_loops": max_loops,
                                "gap_count": len(feedback.gaps),
                                "gaps": [g.model_dump(mode="json") for g in feedback.gaps[:8]],
                            },
                        )
                    elif feedback.needs_more_research and len(all_candidates) >= request.max_sources:
                        await self.trace(
                            run_id,
                            "Pipeline",
                            "progress",
                            "resource_limit_reached",
                            f"已达到最大来源数 {request.max_sources}，即使仍有缺口也不再搜索，进入报告生成",
                            {
                                "max_sources": request.max_sources,
                                "source_count": len(all_candidates),
                                "gap_count": len(feedback.gaps),
                            },
                        )
                    elif not feedback.needs_more_research:
                        await self.trace(
                            run_id,
                            "Pipeline",
                            "progress",
                            "loop_converged",
                            f"第 {loop_round} 轮研究已完成证据闭环，跳过后续补充轮次并进入资产合成",
                            {
                                "loop_round": loop_round,
                                "max_loops": max_loops,
                                "coverage": feedback.coverage_score,
                            },
                        )
                    break

                await self.trace(
                    run_id, "Pipeline", "progress", "loop_continue",
                    f"第 {loop_round} 轮结束，发现 {len(feedback.gaps)} 个缺口，启动第 {loop_round + 1} 轮补充搜索",
                    {"loop_round": loop_round, "gaps": [g.model_dump() for g in feedback.gaps[:4]]},
                )

            # ── 最终 Analysis & Synthesize（只在最后一轮后执行完整资产合成）──
            async with self.node(run_id, status, "AnalysisAndReviewAgent", "整合全轮次结果，合成分析资产"):
                artifacts = await AnalysisAndReviewAgent(ctx).synthesize(
                    request,
                    plan,
                    all_evidence,
                    reviewed_claims,
                    metrics,
                    status.started_at,
                    matrix_builder=build_competitor_matrix,
                    recommendations_builder=build_recommendations,
                    battlecards_builder=build_battlecards,
                    graph_builder=build_evidence_graph,
                    observability_builder=build_observability,
                    average_fn=average,
                    unique_strings_fn=unique_strings,
                )
                await self.save_artifacts(run_id, artifacts)
                metrics.matrix_cell_count = len(artifacts["matrix"].cells)
                metrics.recommendation_count = len(artifacts["recommendations"])
                metrics.battlecard_count = len(artifacts["battlecards"])
                metrics.average_evidence_confidence = artifacts["average_evidence_confidence"]
                metrics.coverage_score = artifacts["observability"].evidence_coverage_score
                status.metrics = metrics
                await self.runs.save_status(status)

            # ── Report Composer ──
            async with self.node(run_id, status, "ReportComposerAgent",
                                 "生成本地 Markdown/JSON/CSV 交付文件"):
                await self.write_report(
                    run_id, request, all_evidence, reviewed_claims, metrics,
                    artifacts["matrix"], artifacts["recommendations"],
                    artifacts["battlecards"], artifacts["observability"],
                )

            status.status = "completed"
            status.current_stage = "Completed"
            status.finished_at = datetime.now(UTC)
            status.metrics = metrics
            await self.runs.save_status(status)
            await self.trace(
                run_id, "ReportComposerAgent", "complete", "completed",
                "运行完成", metrics.model_dump()
            )
        except RunStopped:
            status = await self.runs.mark_stopped(run_id, status)
            await self.trace(run_id, "Pipeline", "complete", "stopped", "Stopped by user")
        except Exception as exc:
            status.status = "failed"
            status.current_stage = "Failed"
            status.finished_at = datetime.now(UTC)
            status.error = str(exc)
            status.metrics = metrics
            await self.runs.save_status(status)
            await self.trace(run_id, "Pipeline", "error", "failed", str(exc))

    async def quick_extract(
        self, request: QuickExtractRequest, owner: str | None = None
    ) -> tuple[RunStatus, list[Evidence]]:
        if request.run_id:
            status = await self.runs.assert_access(request.run_id, owner)
            run_id = request.run_id
        else:
            status = await self.runs.create(
                ResearchRequest(
                    project_name="Quick Extract",
                    target_product=urlparse(str(request.url)).netloc,
                    competitors=[],
                    seed_urls=[str(request.url)],
                    auto_discover_sources=False,
                    max_sources=1,
                ),
                owner=owner,
            )
            run_id = status.run_id

        page = await self.fetcher.fetch(str(request.url))
        source = SourceCandidate(
            url=str(request.url),
            title=page.title,
            source_type=(
                classify_source(str(request.url), page.title)
                if request.source_type == "other"
                else request.source_type
            ),
            score=0.7,
        )
        document = await self.save_document(run_id, source, page)
        evidence = await self.extract_and_save_evidence(
            run_id,
            ResearchRequest(
                project_name=status.project_name,
                target_product=status.target_product,
                competitors=[],
                analysis_dimensions=DEFAULT_DIMENSIONS.copy(),
                seed_urls=[str(request.url)],
                auto_discover_sources=False,
            ),
            [document],
        )
        status.status = "completed"
        status.current_stage = "QuickExtract"
        status.finished_at = datetime.now(UTC)
        status.metrics = RunMetrics(sources_fetched=1 if document.ok else 0, evidence_count=len(evidence))
        await self.runs.save_status(status)
        return status, evidence

    def agent_context(self, run_id: str) -> AgentContext:
        return AgentContext(
            run_id=run_id,
            run_dir=self.runs.run_dir(run_id),
            settings=self.settings,
            llm=self.llm,
            trace=lambda node, phase, status, message, payload=None: self.trace(
                run_id,
                node,
                phase,
                status,
                message,
                payload,
            ),
        )

    async def save_candidates(self, run_id: str, candidates: list[SourceCandidate]) -> None:
        path = self.runs.run_dir(run_id) / "sources" / "_index.jsonl"
        existing_urls: set[str] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if '"url"' in line:
                    match = re.search(r'"url"\s*:\s*"([^"]+)"', line)
                    if match:
                        existing_urls.add(match.group(1))
        for candidate in candidates:
            if candidate.url in existing_urls:
                continue
            existing_urls.add(candidate.url)
            await append_jsonl(path, candidate)

    async def save_document(
        self, run_id: str, candidate: SourceCandidate, page: RawPage
    ) -> SourceDocument:
        source_id = stable_id("src", page.final_url or candidate.url)
        document = SourceDocument(
            source_id=source_id,
            run_id=run_id,
            url=page.final_url or candidate.url,
            title=page.title or candidate.title or candidate.url,
            content=page.content,
            excerpt=page.content[:500],
            source_type=candidate.source_type or classify_source(candidate.url, page.title),
            http_status=page.http_status,
            content_hash=page.content_hash,
            fetched_at=page.fetched_at,
            parser=page.parser,
            provider=candidate.source_provider,
            query=candidate.query,
            content_source=candidate.content_source or "local_fetch",
            ok=page.ok,
            error=page.error,
            published_at=candidate.published_at,
            date_source=candidate.date_source,
        )
        run_dir = self.runs.run_dir(run_id)
        await atomic_write_json(run_dir / "sources" / f"{source_id}.json", document)
        await write_text(run_dir / "documents" / f"{source_id}.txt", page.content)
        await append_jsonl(run_dir / "documents" / "_index.jsonl", document)
        return document

    async def materialize_search_documents(
        self,
        run_id: str,
        candidates: list[SourceCandidate],
    ) -> list[SourceDocument]:
        """Persist provider-returned content as SourceDocument without local web fetching."""

        documents: list[SourceDocument] = []
        run_dir = self.runs.run_dir(run_id)
        seen_source_ids: set[str] = set()
        for candidate in candidates:
            content = (candidate.content or candidate.snippet or "").strip()
            if len(content) < 80:
                continue
            source_id = stable_id("src", f"{candidate.url}:{candidate.source_provider}:{candidate.query}")
            if source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            content_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
            document = SourceDocument(
                source_id=source_id,
                run_id=run_id,
                url=candidate.url,
                title=candidate.title or urlparse(candidate.url).netloc or candidate.url,
                content=content,
                excerpt=content[:500],
                source_type=candidate.source_type or classify_source(candidate.url, candidate.title),
                http_status=200,
                content_hash=content_hash,
                fetched_at=datetime.now(UTC),
                parser=candidate.content_source or "provider_content",
                provider=candidate.source_provider,
                query=candidate.query,
                content_source=candidate.content_source or "provider_content",
                ok=True,
                error=None,
                published_at=candidate.published_at,
                date_source=candidate.date_source,
            )
            await atomic_write_json(run_dir / "sources" / f"{source_id}.json", document)
            await write_text(run_dir / "documents" / f"{source_id}.txt", document.content)
            await append_jsonl(run_dir / "documents" / "_index.jsonl", document)
            documents.append(document)
        if documents:
            await self.trace(
                run_id,
                "SourceResearchAgent",
                "progress",
                "content_ready",
                f"已直接保存 {len(documents)} 篇搜索引擎返回正文，跳过本地抓取",
                {
                    "count": len(documents),
                    "providers": dict(Counter(doc.provider for doc in documents if doc.provider)),
                    "results": [
                        {
                            "title": doc.title,
                            "url": doc.url,
                            "provider": doc.provider,
                            "content_source": doc.content_source,
                            "excerpt": doc.excerpt,
                        }
                        for doc in documents[:20]
                    ],
                },
            )
        return documents

    async def extract_and_save_evidence(
        self,
        run_id: str,
        request: ResearchRequest,
        plan_or_documents,
        maybe_documents: list[SourceDocument] | None = None,
        *,
        existing_evidence: list[Evidence] | None = None,
    ) -> list[Evidence]:
        if maybe_documents is None:
            plan = ResearchPlan(
                research_goal=request.research_goal,
                competitors=request.competitors,
                dimensions=request.analysis_dimensions or DEFAULT_DIMENSIONS,
            )
            documents = plan_or_documents
        else:
            plan = plan_or_documents
            documents = maybe_documents
        repo = EvidenceRepository(self.runs.run_dir(run_id))
        evidence_items: list[Evidence] = []
        dimensions = plan.dimensions or request.analysis_dimensions or DEFAULT_DIMENSIONS
        entities = [request.target_product, *request.competitors]
        agent = EvidenceStructuringAgent(self.agent_context(run_id))
        coverage_counts = evidence_coverage_counts(existing_evidence or [], entities, dimensions)
        skipped_documents = 0
        candidates_for_extraction: list[tuple[SourceDocument, list[Evidence]]] = []
        for document in documents:
            if not document.ok or len(document.content) < 80:
                continue
            doc_pairs = infer_document_pairs(document, entities, dimensions)
            if doc_pairs and all(evidence_cell_sufficient(coverage_counts.get(pair, 0)) for pair in doc_pairs):
                skipped_documents += 1
                continue
            deterministic = extract_evidence_from_document(run_id, document, dimensions, entities)
            candidates_for_extraction.append((document, deterministic))

        semaphore = asyncio.Semaphore(max(1, self.settings.cg_evidence_llm_parallelism))

        async def extract_one(document: SourceDocument, deterministic: list[Evidence]) -> tuple[SourceDocument, list[Evidence]]:
            async with semaphore:
                try:
                    extracted = await agent.extract(
                        request,
                        document,
                        dimensions,
                        entities,
                        deterministic,
                        stable_id,
                        source_weight,
                    )
                    return document, extracted
                except Exception as exc:
                    await self.trace(
                        run_id,
                        "EvidenceStructuringAgent",
                        "progress",
                        "extract_fallback",
                        f"抽取「{document.title}」失败，已使用规则抽取兜底",
                        {
                            "url": document.url,
                            "title": document.title,
                            "error": str(exc),
                            "fallback_count": len(deterministic),
                        },
                    )
                    return document, deterministic

        total_candidates = len(candidates_for_extraction)
        completed_documents = 0
        existing_count = len(existing_evidence or [])
        parallelism = max(1, self.settings.cg_evidence_llm_parallelism)
        await self.trace(
            run_id,
            "EvidenceStructuringAgent",
            "progress",
            "extract_started",
            f"开始抽取 Evidence：{total_candidates} 篇候选文档，LLM 并行度 {parallelism}",
            {"document_count": total_candidates, "parallelism": parallelism},
        )

        stop_heartbeat = asyncio.Event()

        async def heartbeat() -> None:
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=60)
                except asyncio.TimeoutError:
                    await self.trace(
                        run_id,
                        "EvidenceStructuringAgent",
                        "progress",
                        "extract_heartbeat",
                        f"Evidence 抽取进行中：已完成 {completed_documents}/{total_candidates} 篇，已保存 {len(evidence_items)} 条",
                        {
                            "completed_documents": completed_documents,
                            "total_documents": total_candidates,
                            "evidence_saved": len(evidence_items),
                        },
                    )

        heartbeat_task = asyncio.create_task(heartbeat())
        extraction_tasks = [
            asyncio.create_task(extract_one(document, deterministic))
            for document, deterministic in candidates_for_extraction
        ]
        try:
            for extraction_task in asyncio.as_completed(extraction_tasks):
                await self.check_stop(run_id)
                document, extracted = await extraction_task
                completed_documents += 1
                saved_for_document = 0
                for item in extracted:
                    if item.competitor and item.dimension:
                        key = (item.competitor, item.dimension)
                        if evidence_cell_sufficient(coverage_counts.get(key, 0)):
                            continue
                        coverage_counts[key] = coverage_counts.get(key, 0) + 1
                    await repo.save(item)
                    evidence_items.append(item)
                    saved_for_document += 1
                await self.trace(
                    run_id,
                    "EvidenceStructuringAgent",
                    "progress",
                    "extracted",
                    f"从「{document.title}」抽取 {saved_for_document} 条 Evidence（{completed_documents}/{total_candidates}）",
                    {
                        "url": document.url,
                        "title": document.title,
                        "count": saved_for_document,
                        "provider": document.provider,
                        "content_source": document.content_source,
                        "completed_documents": completed_documents,
                        "total_documents": total_candidates,
                    },
                )
                if completed_documents == 1 or completed_documents % 5 == 0 or completed_documents == total_candidates:
                    current_status = await self.runs.load_status(run_id)
                    if current_status.status == "running":
                        current_status.metrics.evidence_count = existing_count + len(evidence_items)
                        await self.runs.save_status(current_status)
        finally:
            for task in extraction_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*extraction_tasks, return_exceptions=True)
            stop_heartbeat.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        if skipped_documents:
            await self.trace(
                run_id,
                "EvidenceStructuringAgent",
                "progress",
                "skipped_sufficient",
                f"跳过 {skipped_documents} 篇文档：对应竞品×维度 Evidence 已较充分",
                {"count": skipped_documents},
            )
        return evidence_items

        extracted_batches = await asyncio.gather(
            *(extract_one(document, deterministic) for document, deterministic in candidates_for_extraction)
        )
        for document, extracted in extracted_batches:
            for item in extracted:
                if item.competitor and item.dimension:
                    key = (item.competitor, item.dimension)
                    if evidence_cell_sufficient(coverage_counts.get(key, 0)):
                        continue
                    coverage_counts[key] = coverage_counts.get(key, 0) + 1
                await repo.save(item)
                evidence_items.append(item)
            await self.trace(
                run_id,
                "EvidenceStructuringAgent",
                "progress",
                "extracted",
                f"从「{document.title}」抽取 {len(extracted)} 条 Evidence",
                {
                    "url": document.url,
                    "title": document.title,
                    "count": len(extracted),
                    "provider": document.provider,
                    "content_source": document.content_source,
                },
            )
        if skipped_documents:
            await self.trace(
                run_id,
                "EvidenceStructuringAgent",
                "progress",
                "skipped_sufficient",
                f"跳过 {skipped_documents} 篇文档：对应竞品×维度 Evidence 已较充分",
                {"count": skipped_documents},
            )
        return evidence_items

    async def generate_claims(self, run_id: str, evidence: list[Evidence], request: ResearchRequest | None = None) -> list[Claim]:
        grouped: dict[str, list[Evidence]] = defaultdict(list)
        for item in evidence:
            grouped[item.dimension].append(item)

        deterministic_claims: list[Claim] = []
        for dimension, items in grouped.items():
            if not items:
                continue
            items = sorted(items, key=lambda ev: ev.confidence, reverse=True)
            dimension_label = DIMENSION_LABELS.get(dimension, dimension)
            by_competitor: dict[str, list[Evidence]] = defaultdict(list)
            for item in items:
                by_competitor[item.competitor or "相关产品"].append(item)

            top_competitors = sorted(by_competitor, key=lambda name: len(by_competitor[name]), reverse=True)[:5]
            if len(top_competitors) >= 2:
                supporting = [ev for name in top_competitors for ev in by_competitor[name][:2]][:8]
                facts = "；".join(f"{name}：{by_competitor[name][0].fact}" for name in top_competitors[:4] if by_competitor[name])
                deterministic_claims.append(build_claim_from_support(
                    run_id,
                    dimension,
                    dimension_label,
                    f"在{dimension_label}维度，公开证据显示不同产品的重点明显不同：{facts}",
                    supporting,
                    f"该横向结论聚合 {len(items)} 条 Evidence，覆盖 {len(top_competitors)} 个产品。",
                ))

            for competitor, competitor_items in list(by_competitor.items())[:6]:
                supporting = competitor_items[:5]
                if not supporting:
                    continue
                fact_snippet = "；".join(ev.fact for ev in supporting[:2])
                deterministic_claims.append(build_claim_from_support(
                    run_id,
                    dimension,
                    dimension_label,
                    f"{competitor} 在{dimension_label}维度的公开信息集中体现为：{fact_snippet}",
                    supporting,
                    f"该单产品结论由 {len(competitor_items)} 条 Evidence 支持。",
                ))
        claims = await AnalysisAndReviewAgent(self.agent_context(run_id)).generate(evidence, deterministic_claims, request)
        return claims

    async def red_team(
        self, run_id: str, claims: list[Claim], evidence: list[Evidence]
    ) -> list[Claim]:
        reviewed = await AnalysisAndReviewAgent(self.agent_context(run_id)).review(claims, evidence)
        run_dir = self.runs.run_dir(run_id)
        for claim in reviewed:
            await atomic_write_json(run_dir / "claims" / f"{claim.claim_id}.json", claim)
            await append_jsonl(run_dir / "claims" / "_index.jsonl", claim)
        return reviewed

    async def save_artifacts(self, run_id: str, artifacts: dict) -> None:
        run_dir = self.runs.run_dir(run_id)
        matrix = artifacts["matrix"]
        recommendations = artifacts["recommendations"]
        battlecards = artifacts["battlecards"]
        evidence_graph = artifacts["evidence_graph"]
        observability = artifacts["observability"]
        await atomic_write_json(run_dir / "exports" / "matrix.json", matrix)
        await write_text(run_dir / "exports" / "matrix.csv", build_matrix_csv(matrix))
        await atomic_write_json(run_dir / "exports" / "recommendations.json", recommendations)
        await write_text(run_dir / "exports" / "recommendations.md", build_recommendations_markdown(recommendations))
        await atomic_write_json(run_dir / "exports" / "battlecards.json", battlecards)
        await write_text(run_dir / "exports" / "battlecards.md", build_battlecards_markdown(battlecards))
        await atomic_write_json(run_dir / "exports" / "observability.json", observability)
        await atomic_write_json(run_dir / "exports" / "evidence_graph.json", evidence_graph)

    async def write_report(
        self,
        run_id: str,
        request: ResearchRequest,
        evidence: list[Evidence],
        claims: list[Claim],
        metrics: RunMetrics,
        matrix: CompetitorMatrix,
        recommendations: list[OpportunityRecommendation],
        battlecards: list[BattlecardItem],
        observability: ObservabilitySnapshot,
    ) -> None:
        composer = ReportComposerAgent(self.agent_context(run_id))
        report = await composer.write(
            request,
            evidence,
            claims,
            metrics,
            {
                "matrix": matrix,
                "recommendations": recommendations,
                "battlecards": battlecards,
                "observability": observability,
            },
        )
        executive_summary = await composer.write_executive_summary(
            request,
            evidence,
            claims,
            metrics,
            matrix,
            recommendations,
            observability,
        )
        methodology = await composer.write_methodology(
            request,
            evidence,
            claims,
            metrics,
            matrix,
            observability,
        )
        run_dir = self.runs.run_dir(run_id)
        await write_text(run_dir / "reports" / "report.md", report)
        await write_text(run_dir / "reports" / "executive_summary.md", executive_summary)
        await write_text(run_dir / "reports" / "methodology.md", methodology)
        await atomic_write_json(
            run_dir / "reports" / "report.json",
            {
                "run_id": run_id,
                "request": request,
                "metrics": metrics,
                "claims": claims,
                "matrix": matrix,
                "recommendations": recommendations,
                "battlecards": battlecards,
                "observability": observability,
                "evidence_ids": [ev.evidence_id for ev in evidence],
            },
        )
        await write_text(
            run_dir / "exports" / "evidence_matrix.csv",
            build_evidence_csv(evidence, {ev.evidence_id: ev for ev in evidence}),
        )

    @asynccontextmanager
    async def node(
        self, run_id: str, status: RunStatus, node: str, message: str
    ) -> AsyncIterator[None]:
        status.current_stage = node
        status.node_status[node] = "running"
        await self.runs.save_status(status)
        await self.trace(run_id, node, "start", "running", message)
        try:
            yield
        except RunStopped:
            status.node_status[node] = "stopped"
            await self.runs.save_status(status)
            await self.trace(run_id, node, "complete", "stopped", "Stopped by user")
            raise
        except Exception as exc:
            status.node_status[node] = "failed"
            await self.runs.save_status(status)
            await self.trace(run_id, node, "error", "failed", str(exc))
            raise
        else:
            status.node_status[node] = "completed"
            await self.runs.save_status(status)
            await self.trace(run_id, node, "complete", "completed", message)

    async def trace(
        self,
        run_id: str,
        node: str,
        phase: str,
        status: str,
        message: str,
        payload: dict | None = None,
    ) -> None:
        event = TraceEvent(
            event_id=f"evt_{uuid4().hex[:12]}",
            run_id=run_id,
            timestamp=datetime.now(UTC),
            node=node,
            phase=phase,  # type: ignore[arg-type]
            status=status,
            message=message,
            payload=payload or {},
        )
        await append_jsonl(self.runs.run_dir(run_id) / "trace" / "events.jsonl", event)

def build_dag_snapshot(plan: ResearchPlan) -> dict:
    requested = set(plan.required_agents or [])
    nodes = [agent for agent in RESEARCH_AGENT_FLOW if not requested or agent in requested]
    for agent in RESEARCH_AGENT_FLOW:
        if agent not in nodes:
            nodes.append(agent)
    return {
        "nodes": [
            {"id": node, "status": "planned", "skills": AGENT_SKILLS.get(node, [])}
            for node in nodes
        ],
        "edges": [{"from": nodes[i], "to": nodes[i + 1]} for i in range(len(nodes) - 1)],
        "queries": plan.queries,
        "source_tasks": [task.model_dump(mode="json") for task in plan.source_tasks],
        "quality_rules": plan.quality_rules,
        "planned_by": plan.planned_by,
    }


def build_competitor_matrix(
    evidence: list[Evidence], competitors: list[str], dimensions: list[str]
) -> CompetitorMatrix:
    now = datetime.now(UTC)
    evidence_by_cell: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for ev in evidence:
        competitor = ev.competitor or detect_entity(ev.fact, ev.source_title, ev.source_url, competitors)
        if not competitor and len(competitors) == 1:
            competitor = competitors[0]
        if competitor and ev.dimension in dimensions:
            evidence_by_cell[(competitor, ev.dimension)].append(ev)

    cells: list[MatrixCell] = []
    for competitor in competitors:
        for dimension in dimensions:
            items = sorted(evidence_by_cell.get((competitor, dimension), []), key=lambda ev: ev.confidence, reverse=True)
            confidence = average([ev.confidence for ev in items[:5]])
            status = cell_status(len(items), confidence)
            cells.append(
                MatrixCell(
                    competitor=competitor,
                    dimension=dimension,
                    dimension_label=DIMENSION_LABELS.get(dimension, dimension),
                    summary=cell_summary(competitor, dimension, items),
                    evidence_count=len(items),
                    confidence=round(confidence, 3),
                    source_types=sorted({ev.source_type for ev in items}),
                    evidence_ids=[ev.evidence_id for ev in items[:6]],
                    status=status,
                )
            )

    profiles: list[CompetitorProfile] = []
    for competitor in competitors:
        competitor_items = [
            ev
            for ev in evidence
            if (ev.competitor or detect_entity(ev.fact, ev.source_title, ev.source_url, competitors)) == competitor
        ]
        strong_dimensions = [
            DIMENSION_LABELS.get(cell.dimension, cell.dimension)
            for cell in cells
            if cell.competitor == competitor and cell.status in {"strong", "partial"}
        ]
        weak_dimensions = [
            DIMENSION_LABELS.get(cell.dimension, cell.dimension)
            for cell in cells
            if cell.competitor == competitor and cell.status in {"weak", "unknown"}
        ]
        profiles.append(
            CompetitorProfile(
                competitor=competitor,
                summary=profile_summary(competitor, competitor_items, strong_dimensions, weak_dimensions),
                evidence_count=len(competitor_items),
                source_count=len({ev.source_url for ev in competitor_items}),
                average_confidence=round(average([ev.confidence for ev in competitor_items]), 3),
                strongest_dimensions=strong_dimensions[:4],
                weak_or_unknown_dimensions=weak_dimensions[:4],
                evidence_ids=[ev.evidence_id for ev in sorted(competitor_items, key=lambda item: item.confidence, reverse=True)[:10]],
            )
        )

    coverage_by_competitor: dict[str, float] = {}
    for competitor in competitors:
        competitor_cells = [cell for cell in cells if cell.competitor == competitor]
        coverage_by_competitor[competitor] = round(
            sum(coverage_points(cell) for cell in competitor_cells) / max(1, len(competitor_cells)),
            3,
        )

    coverage_by_dimension: dict[str, float] = {}
    for dimension in dimensions:
        dimension_cells = [cell for cell in cells if cell.dimension == dimension]
        coverage_by_dimension[dimension] = round(
            sum(coverage_points(cell) for cell in dimension_cells) / max(1, len(dimension_cells)),
            3,
        )

    return CompetitorMatrix(
        generated_at=now,
        competitors=competitors,
        dimensions=dimensions,
        dimension_labels={dimension: DIMENSION_LABELS.get(dimension, dimension) for dimension in dimensions},
        cells=cells,
        profiles=profiles,
        coverage_by_competitor=coverage_by_competitor,
        coverage_by_dimension=coverage_by_dimension,
    )


def build_claim_from_support(
    run_id: str,
    dimension: str,
    dimension_label: str,
    claim_text: str,
    supporting: list[Evidence],
    reasoning: str,
) -> Claim:
    source_types = sorted({ev.source_type for ev in supporting})
    source_count = len({ev.source_url for ev in supporting})
    confidence = min(0.92, average([ev.confidence for ev in supporting]) * min(1.0, 0.65 + len(supporting) * 0.08))
    return Claim(
        claim_id=stable_id("claim", f"{run_id}:{dimension}:{claim_text}:{[ev.evidence_id for ev in supporting]}"),
        run_id=run_id,
        dimension=dimension,
        dimension_label=dimension_label,
        claim=claim_text[:700],
        supporting_evidence_ids=[ev.evidence_id for ev in supporting],
        confidence=round(confidence, 3),
        risk_level="low" if len(supporting) >= 3 and source_count >= 2 else "medium",
        reasoning_summary=f"{reasoning} 来源类型包括：{'、'.join(source_types) or '未知'}。",
        verification_status="draft",
    )


def build_recommendations(
    run_id: str,
    request: ResearchRequest,
    claims: list[Claim],
    evidence: list[Evidence],
    matrix: CompetitorMatrix,
) -> list[OpportunityRecommendation]:
    recommendations: list[OpportunityRecommendation] = []
    by_dimension: dict[str, list[Claim]] = defaultdict(list)
    for claim in sorted(claims, key=lambda item: item.confidence, reverse=True):
        by_dimension[claim.dimension].append(claim)

    weak_dimensions = sorted(
        matrix.coverage_by_dimension,
        key=lambda dimension: matrix.coverage_by_dimension.get(dimension, 0),
    )
    target_profile = next(
        (profile for profile in matrix.profiles if profile.competitor == request.target_product),
        None,
    )
    target_weak = target_profile.weak_or_unknown_dimensions if target_profile else []

    strategic_claims = by_dimension.get("strategy", []) or claims[:3]
    if strategic_claims:
        evidence_ids = unique_strings(
            [ev_id for claim in strategic_claims[:3] for ev_id in claim.supporting_evidence_ids]
        )
        recommendations.append(
            OpportunityRecommendation(
                recommendation_id=stable_id("rec", f"{run_id}:strategy:{evidence_ids}"),
                title="把机会点绑定到已验证证据链",
                recommendation=(
                    f"围绕 {request.target_product} 的公开叙事建立 2-3 个可验证机会主题，"
                    "每个主题都绑定 Evidence、Claim 与 Red Team 审查结果。"
                ),
                priority="high",
                target_audience="executive",
                rationale=best_claim_text(strategic_claims[0]),
                expected_value="让竞品分析从一次性报告变成可审计的产品决策输入。",
                based_on_claim_ids=[claim.claim_id for claim in strategic_claims[:3]],
                evidence_ids=evidence_ids[:8],
                next_steps=[
                    "选择一条高置信度战略 Claim 进入产品路线图评审",
                    "对 Red Team 标记的低证据结论安排二次采集",
                    "将关键 Evidence 纳入汇报附录，避免无来源判断",
                ],
                risks=["当前系统只使用公开资料，商业结果仍需结合内部数据校验。"],
                confidence=round(average([claim.confidence for claim in strategic_claims[:3]]), 3),
            )
        )

    if weak_dimensions:
        dimension = weak_dimensions[0]
        evidence_ids = [
            ev.evidence_id
            for ev in sorted(evidence, key=lambda item: item.confidence, reverse=True)
            if ev.dimension == dimension
        ][:6]
        recommendations.append(
            OpportunityRecommendation(
                recommendation_id=stable_id("rec", f"{run_id}:coverage:{dimension}"),
                title=f"优先补强{DIMENSION_LABELS.get(dimension, dimension)}证据覆盖",
                recommendation=(
                    f"下一轮研究应优先补充 {DIMENSION_LABELS.get(dimension, dimension)} 相关来源，"
                    "特别是第三方评测、官方文档、社区讨论和价格/企业能力页面。"
                ),
                priority="medium" if evidence_ids else "high",
                target_audience="pm",
                rationale=f"该维度当前覆盖度为 {matrix.coverage_by_dimension.get(dimension, 0):.0%}，仍有补证空间。",
                expected_value="提升报告可信度，并降低 Red Team 对关键结论的挑战率。",
                evidence_ids=evidence_ids,
                next_steps=[
                    f"新增 3-5 个 {DIMENSION_LABELS.get(dimension, dimension)} 高质量来源",
                    "重新运行 EvidenceStructuringAgent 与 AnalysisAndReviewAgent",
                    "观察质量门禁中的覆盖度是否提升",
                ],
                risks=["补采集可能受网站反爬或搜索质量影响。"],
                confidence=0.68 if evidence_ids else 0.52,
            )
        )

    if target_weak:
        recommendations.append(
            OpportunityRecommendation(
                recommendation_id=stable_id("rec", f"{run_id}:target-position:{target_weak}"),
                title=f"明确 {request.target_product} 的公开差异化叙事",
                recommendation=(
                    f"当前资料中 {request.target_product} 在 {'、'.join(target_weak[:3])} 上的可核验证据偏少，"
                    "建议把产品页、文档页和案例页补成能被外部研究者直接引用的证据。"
                ),
                priority="high",
                target_audience="pm",
                rationale="竞品分析不仅看产品是否具备能力，也看公开资料是否能形成清晰心智。",
                expected_value="提升市场叙事一致性，让销售、产品和外部评测更容易复用同一套材料。",
                evidence_ids=target_profile.evidence_ids[:6] if target_profile else [],
                next_steps=[
                    "梳理目标产品的核心场景与证据素材",
                    "补充面向客户问题的功能、企业化和定价说明",
                    "将新增资料作为下一次 Run 的 seed URL",
                ],
                risks=["若产品能力本身尚未成熟，公开叙事需要保持谨慎，避免过度承诺。"],
                confidence=0.7,
            )
        )

    return recommendations[:5]


def build_battlecards(
    run_id: str,
    request: ResearchRequest,
    claims: list[Claim],
    matrix: CompetitorMatrix,
) -> list[BattlecardItem]:
    cards: list[BattlecardItem] = []
    claim_by_dimension: dict[str, list[Claim]] = defaultdict(list)
    for claim in sorted(claims, key=lambda item: item.confidence, reverse=True):
        claim_by_dimension[claim.dimension].append(claim)
    target_cells = {
        cell.dimension: cell
        for cell in matrix.cells
        if cell.competitor == request.target_product
    }
    for competitor in [name for name in matrix.competitors if name != request.target_product]:
        competitor_cells = sorted(
            [cell for cell in matrix.cells if cell.competitor == competitor and cell.evidence_count > 0],
            key=lambda cell: (
                cell.status == "strong",
                cell.status == "partial",
                cell.confidence,
                cell.evidence_count,
            ),
            reverse=True,
        )
        if not competitor_cells:
            continue
        for cell in competitor_cells[:2]:
            target_cell = target_cells.get(cell.dimension)
            claims_for_dimension = claim_by_dimension.get(cell.dimension, [])
            evidence_ids = unique_strings(
                [
                    *(target_cell.evidence_ids[:3] if target_cell else []),
                    *cell.evidence_ids[:3],
                    *[ev_id for claim in claims_for_dimension[:2] for ev_id in claim.supporting_evidence_ids],
                ]
            )[:6]
            scenario = scenario_for_dimension(cell.dimension)
            cards.append(
                BattlecardItem(
                    item_id=stable_id("card", f"{run_id}:{competitor}:{cell.dimension}:{evidence_ids}"),
                    competitor=competitor,
                    customer_scenario=scenario,
                    competitor_strength=cell.summary,
                    our_response=response_for_cell(request.target_product, target_cell, cell),
                    talk_track=talk_track_for_cell(request.target_product, competitor, target_cell, cell),
                    objection_handler=followup_for_cell(request.target_product, target_cell, cell),
                    evidence_ids=evidence_ids,
                    confidence=max(0.45, cell.confidence),
                )
            )
    return cards[:8]


def build_observability(
    started_at: datetime,
    plan: ResearchPlan,
    metrics: RunMetrics,
    evidence: list[Evidence],
    claims: list[Claim],
    matrix: CompetitorMatrix,
    recommendations: list[OpportunityRecommendation],
    battlecards: list[BattlecardItem],
) -> ObservabilitySnapshot:
    now = datetime.now(UTC)
    source_mix = Counter(ev.source_type for ev in evidence)
    claim_pass_rate = metrics.verified_claim_count / max(1, metrics.claim_count)
    red_team_rate = metrics.challenged_claim_count / max(1, metrics.claim_count)
    coverage_score = average(list(matrix.coverage_by_competitor.values()) + list(matrix.coverage_by_dimension.values()))
    confidence = average([ev.confidence for ev in evidence] + [claim.confidence for claim in claims])
    gates = [
        QualityGate(
            gate_id="gate_evidence_volume",
            name="Evidence 数量",
            status=gate_status(metrics.evidence_count, 10, 3),
            score=min(1.0, metrics.evidence_count / 10),
            message=f"当前抽取 {metrics.evidence_count} 条 Evidence。",
            suggested_action="若少于 10 条，建议增加搜索来源或 seed URL。",
        ),
        QualityGate(
            gate_id="gate_source_success",
            name="内容可用率",
            status=gate_status(metrics.sources_fetched / max(1, metrics.source_candidates), 0.65, 0.35),
            score=round(metrics.sources_fetched / max(1, metrics.source_candidates), 3),
            message=f"{metrics.sources_fetched}/{max(1, metrics.source_candidates)} 个候选来源已有正文内容。",
            suggested_action="优先配置 Tavily/Exa/知乎内容搜索；少量无正文来源可用本地抓取或 seed URL 补充。",
        ),
        QualityGate(
            gate_id="gate_claim_review",
            name="Claim 审查通过率",
            status=gate_status(claim_pass_rate, 0.6, 0.25),
            score=round(claim_pass_rate, 3),
            message=f"{metrics.verified_claim_count}/{max(1, metrics.claim_count)} 条 Claim 通过 Red Team。",
            suggested_action="被挑战的 Claim 需要补证或降低措辞强度。",
        ),
        QualityGate(
            gate_id="gate_coverage",
            name="竞品矩阵覆盖度",
            status=gate_status(coverage_score, 0.58, 0.28),
            score=round(coverage_score, 3),
            message=f"矩阵覆盖度为 {coverage_score:.0%}。",
            suggested_action="补齐低覆盖竞品或低覆盖维度的来源。",
        ),
    ]
    return ObservabilitySnapshot(
        generated_at=now,
        total_duration_seconds=round((now - started_at).total_seconds(), 2),
        agent_count=len(plan.required_agents or []),
        skill_calls=sum(len(AGENT_SKILLS.get(agent, [])) for agent in (plan.required_agents or RESEARCH_AGENT_FLOW)),
        tool_calls=metrics.source_candidates + metrics.sources_fetched,
        source_mix=dict(source_mix),
        dimension_coverage=matrix.coverage_by_dimension,
        competitor_coverage=matrix.coverage_by_competitor,
        claim_pass_rate=round(claim_pass_rate, 3),
        red_team_challenge_rate=round(red_team_rate, 3),
        evidence_coverage_score=round(coverage_score, 3),
        report_confidence=round(min(0.95, confidence * (0.55 + coverage_score * 0.45)), 3),
        quality_gates=gates,
        export_files={
            "markdown_report": "reports/report.md",
            "executive_summary": "reports/executive_summary.md",
            "methodology": "reports/methodology.md",
            "matrix_json": "exports/matrix.json",
            "matrix_csv": "exports/matrix.csv",
            "recommendations": "exports/recommendations.json",
            "battlecards": "exports/battlecards.json",
            "evidence_matrix_csv": "exports/evidence_matrix.csv",
        },
    )


def build_evidence_graph(evidence: list[Evidence], claims: list[Claim]) -> EvidenceGraph:
    nodes: dict[str, EvidenceGraphNode] = {}
    edges: list[EvidenceGraphEdge] = []
    for claim in claims:
        nodes[claim.claim_id] = EvidenceGraphNode(
            id=claim.claim_id,
            label=(claim.final_wording or claim.claim)[:80],
            node_type="claim",
            score=claim.confidence,
            meta={"dimension": claim.dimension, "status": claim.verification_status},
        )
        dimension_id = f"dim_{claim.dimension}"
        nodes.setdefault(
            dimension_id,
            EvidenceGraphNode(
                id=dimension_id,
                label=claim.dimension_label,
                node_type="dimension",
                score=0.7,
            ),
        )
        edges.append(EvidenceGraphEdge(source=dimension_id, target=claim.claim_id, edge_type="contains", weight=0.6))
    for ev in evidence:
        nodes[ev.evidence_id] = EvidenceGraphNode(
            id=ev.evidence_id,
            label=ev.fact[:80],
            node_type="evidence",
            score=ev.confidence,
            meta={"source_url": ev.source_url, "dimension": ev.dimension},
        )
        source_id = stable_id("source", ev.source_url)
        nodes.setdefault(
            source_id,
            EvidenceGraphNode(
                id=source_id,
                label=ev.source_title[:80] or ev.source_url,
                node_type="source",
                score=ev.authority_score,
                meta={"url": ev.source_url, "source_type": ev.source_type},
            ),
        )
        edges.append(EvidenceGraphEdge(source=ev.evidence_id, target=source_id, edge_type="from_source", weight=ev.confidence))
        if ev.competitor:
            competitor_id = stable_id("competitor", ev.competitor)
            nodes.setdefault(
                competitor_id,
                EvidenceGraphNode(
                    id=competitor_id,
                    label=ev.competitor,
                    node_type="competitor",
                    score=0.75,
                ),
            )
            edges.append(EvidenceGraphEdge(source=competitor_id, target=ev.evidence_id, edge_type="has_evidence", weight=ev.confidence))
    evidence_ids = {ev.evidence_id for ev in evidence}
    for claim in claims:
        for evidence_id in claim.supporting_evidence_ids:
            if evidence_id in evidence_ids:
                edges.append(
                    EvidenceGraphEdge(
                        source=claim.claim_id,
                        target=evidence_id,
                        edge_type="supported_by",
                        weight=claim.confidence,
                    )
                )
    return EvidenceGraph(generated_at=datetime.now(UTC), nodes=list(nodes.values()), edges=edges)


def extract_evidence_from_document(
    run_id: str, document: SourceDocument, dimensions: list[str], entities: list[str]
) -> list[Evidence]:
    sentences = split_sentences(document.content)
    evidence_items: list[Evidence] = []
    used_quotes: set[str] = set()
    for dimension in dimensions:
        keywords = DIMENSION_KEYWORDS.get(dimension, [])
        candidates = rank_sentences(sentences, keywords)
        for sentence, score in candidates[:2]:
            if sentence in used_quotes or len(sentence) < 45:
                continue
            used_quotes.add(sentence)
            competitor = detect_entity(sentence, document.title, document.url, entities)
            quote = sentence[:500]
            fact = build_fact(quote, competitor, dimension)
            confidence = min(0.95, 0.38 + score * 0.09 + source_weight(document.source_type))
            evidence_id = stable_id("ev", f"{run_id}:{document.url}:{dimension}:{quote}")
            evidence_items.append(
                Evidence(
                    evidence_id=evidence_id,
                    run_id=run_id,
                    url=document.url,
                    title=document.title,
                    content=quote,
                    fetched_at=document.fetched_at,
                    source_type=document.source_type,
                    dimension=dimension,
                    dimension_label=DIMENSION_LABELS.get(dimension, dimension),
                    competitor=competitor,
                    fact=fact,
                    quote=quote,
                    source_title=document.title,
                    source_url=document.url,
                    source_id=document.source_id,
                    confidence=round(confidence, 3),
                    authority_score=round(source_weight(document.source_type) + 0.3, 3),
                    freshness_score=0.75,
                    relevance_score=round(min(1.0, 0.35 + score * 0.1), 3),
                )
            )
            break
    if not evidence_items and sentences:
        sentence = max(sentences, key=len)[:500]
        evidence_items.append(
            Evidence(
                evidence_id=stable_id("ev", f"{run_id}:{document.url}:real_text:{sentence}"),
                run_id=run_id,
                url=document.url,
                title=document.title,
                content=sentence,
                fetched_at=document.fetched_at,
                source_type=document.source_type,
                dimension="other",
                dimension_label=DIMENSION_LABELS["other"],
                competitor=detect_entity(sentence, document.title, document.url, entities),
                fact=build_fact(sentence, None, "other"),
                quote=sentence,
                source_title=document.title,
                source_url=document.url,
                source_id=document.source_id,
                confidence=0.45,
                authority_score=round(source_weight(document.source_type) + 0.3, 3),
                freshness_score=0.7,
                relevance_score=0.45,
            )
        )
    return evidence_items


def split_sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    cleaned = [re.sub(r"\s+", " ", chunk).strip(" -•\t") for chunk in chunks]
    return [chunk for chunk in cleaned if 35 <= len(chunk) <= 700]


def rank_sentences(sentences: list[str], keywords: list[str]) -> list[tuple[str, int]]:
    ranked: list[tuple[str, int]] = []
    for sentence in sentences:
        score = sum(1 for keyword in keywords if keyword_matches(sentence, keyword))
        if score:
            ranked.append((sentence, score))
    ranked.sort(key=lambda item: (item[1], len(item[0])), reverse=True)
    return ranked


def keyword_matches(sentence: str, keyword: str) -> bool:
    lower_keyword = keyword.lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9 -]*", lower_keyword):
        pattern = r"\b" + re.escape(lower_keyword).replace(r"\ ", r"\s+") + r"\b"
        return bool(re.search(pattern, sentence.lower()))
    return lower_keyword in sentence.lower()


def detect_entity(sentence: str, title: str, url: str, entities: list[str]) -> str | None:
    haystack = f"{sentence} {title} {url}".lower()
    for entity in entities:
        if entity and entity.lower() in haystack:
            return entity
    host = urlparse(url).netloc.lower()
    for entity in entities:
        token = re.sub(r"[^a-z0-9]+", "", entity.lower())
        if token and token in re.sub(r"[^a-z0-9]+", "", host):
            return entity
    return None


def infer_document_pairs(
    document: SourceDocument,
    entities: list[str],
    dimensions: list[str],
) -> set[tuple[str, str]]:
    text = f"{document.url} {document.title} {document.content[:1200]} {document.query}".lower()
    found_entities: set[str] = set()
    for entity in entities:
        entity_key = re.sub(r"[^a-z0-9]+", "", entity.lower())
        host_key = re.sub(r"[^a-z0-9]+", "", urlparse(document.url).netloc.lower())
        if entity.lower() in text or (entity_key and entity_key in host_key):
            found_entities.add(entity)
    found_dimensions: set[str] = set()
    for dimension in dimensions:
        if dimension in text or any(keyword.lower() in text for keyword in DIMENSION_KEYWORDS.get(dimension, [dimension])):
            found_dimensions.add(dimension)
    return {(entity, dimension) for entity in found_entities for dimension in found_dimensions}


def evidence_coverage_counts(
    evidence: list[Evidence],
    entities: list[str],
    dimensions: list[str],
) -> dict[tuple[str, str], int]:
    entity_set = set(entities)
    dimension_set = set(dimensions)
    counts: dict[tuple[str, str], int] = {}
    for item in evidence:
        if item.competitor in entity_set and item.dimension in dimension_set:
            key = (item.competitor, item.dimension)
            counts[key] = counts.get(key, 0) + 1
    return counts


def evidence_cell_sufficient(count: int) -> bool:
    # 严一点：同一产品×维度至少 5 条 Evidence 后才跳过后续同格文档。
    return count >= 5


def build_fact(quote: str, competitor: str | None, dimension: str) -> str:
    subject = competitor or "该来源"
    label = DIMENSION_LABELS.get(dimension, dimension)
    trimmed = quote.strip()
    if len(trimmed) > 160:
        trimmed = trimmed[:157] + "..."
    return f"{subject} 在{label}相关公开资料中出现了可核验表述：{trimmed}"


def source_weight(source_type: str) -> float:
    weights = {
        "official_website": 0.36,
        "pricing_page": 0.38,
        "docs": 0.35,
        "changelog": 0.32,
        "github": 0.28,
        "review_platform": 0.22,
        "user_review": 0.20,   # 知乎等用户声音，置信度较低但有价值
        "blog": 0.22,
        "other": 0.18,
    }
    return weights.get(source_type, 0.2)


def stable_id(prefix: str, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def build_evidence_csv(evidence: list[Evidence], by_evidence: dict[str, Evidence]) -> str:
    _ = by_evidence
    rows = ["evidence_id,dimension,competitor,confidence,source_url,fact"]
    for ev in evidence:
        rows.append(
            ",".join(
                csv_escape(value)
                for value in [
                    ev.evidence_id,
                    ev.dimension,
                    ev.competitor or "",
                    f"{ev.confidence:.3f}",
                    ev.source_url,
                    ev.fact,
                ]
            )
        )
    return "\n".join(rows) + "\n"


def csv_escape(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def unique_strings(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = (value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0


def cell_status(count: int, confidence: float) -> str:
    if count >= 3 and confidence >= 0.68:
        return "strong"
    if count >= 2 and confidence >= 0.55:
        return "partial"
    if count >= 1:
        return "weak"
    return "unknown"


def coverage_points(cell: MatrixCell) -> float:
    if cell.status == "strong":
        return 1.0
    if cell.status == "partial":
        return 0.68
    if cell.status == "weak":
        return 0.35
    return 0.0


def cell_summary(competitor: str, dimension: str, items: list[Evidence]) -> str:
    label = DIMENSION_LABELS.get(dimension, dimension)
    if not items:
        return f"尚未在本次公开资料中找到 {competitor} 的{label}强证据。"
    top = sorted(items, key=lambda ev: ev.confidence, reverse=True)[:2]
    facts = "；".join(compact_fact(ev.fact) for ev in top)
    return f"{competitor} 在{label}维度有 {len(items)} 条证据：{facts}"


def profile_summary(
    competitor: str, evidence: list[Evidence], strong_dimensions: list[str], weak_dimensions: list[str]
) -> str:
    if not evidence:
        return f"本次运行尚未形成 {competitor} 的有效公开证据画像。"
    source_count = len({ev.source_url for ev in evidence})
    strong = "、".join(strong_dimensions[:3]) or "暂无明显强覆盖维度"
    weak = "、".join(weak_dimensions[:3]) or "暂无明显缺口"
    return (
        f"{competitor} 当前画像来自 {len(evidence)} 条 Evidence 和 {source_count} 个来源；"
        f"证据覆盖较强的方向是 {strong}，仍需补充验证的方向是 {weak}。"
    )


def build_core_findings(request: ResearchRequest, matrix: CompetitorMatrix, claims: list[Claim]) -> list[str]:
    findings: list[str] = []
    target = request.target_product
    by_competitor = {profile.competitor: profile for profile in matrix.profiles}
    target_profile = by_competitor.get(target)
    if target_profile:
        strong = "、".join(target_profile.strongest_dimensions[:3]) or "暂无强覆盖维度"
        weak = "、".join(target_profile.weak_or_unknown_dimensions[:3]) or "暂无明显缺口"
        findings.append(
            f"{target} 的证据画像目前最集中在 {strong}；需要谨慎解读或继续补证的方向是 {weak}。"
        )

    strongest_dimension = sorted(
        matrix.coverage_by_dimension.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    if strongest_dimension:
        label = matrix.dimension_labels.get(strongest_dimension[0][0], strongest_dimension[0][0])
        findings.append(f"本次资料覆盖最充分的维度是 {label}，覆盖度约 {strongest_dimension[0][1]:.0%}。")

    verified = [claim for claim in claims if claim.verification_status == "verified"]
    lead_claims = sorted(verified or claims, key=lambda item: item.confidence, reverse=True)[:3]
    for claim in lead_claims:
        findings.append(f"{DIMENSION_LABELS.get(claim.dimension, claim.dimension)}：{compact_fact(best_claim_text(claim), 150)}")

    return findings[:5] or ["本次运行已形成基础证据库，但仍需要更多可交叉验证的高质量来源来支撑强结论。"]


def build_analysis_report(
    request: ResearchRequest,
    evidence: list[Evidence],
    claims: list[Claim],
    metrics: RunMetrics,
    matrix: CompetitorMatrix,
    recommendations: list[OpportunityRecommendation],
    battlecards: list[BattlecardItem],
    observability: ObservabilitySnapshot,
) -> str:
    citation_numbers, reference_lines = build_citation_index(evidence)
    core_findings = build_core_findings(request, matrix, claims)
    target_strengths, target_weaknesses = build_target_advantage_analysis(request, matrix, claims, citation_numbers)
    user_attention = build_user_attention_analysis(request, matrix, claims, citation_numbers)
    positioning = build_positioning_guidance(request, matrix, target_strengths, target_weaknesses)
    by_dimension: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        by_dimension[claim.dimension].append(claim)

    lines = [
        f"# {request.project_name}",
        "",
        "## 摘要",
        "",
        *[f"- {finding}" for finding in core_findings],
        "",
        "## 竞品对比矩阵",
        "",
        build_matrix_markdown(matrix, citation_numbers),
        "",
        "### 矩阵解读",
        "",
        *build_matrix_insights(matrix),
        "",
        f"## {request.target_product} 的优势与不足",
        "",
        "### 已具备的优势",
        "",
        *target_strengths,
        "",
        "### 需要补齐或谨慎表达的不足",
        "",
        *target_weaknesses,
        "",
        "## 用户更关注什么",
        "",
        *user_attention,
        "",
        "## 应该如何宣传优势",
        "",
        *positioning,
        "",
        "## 分维度深度分析",
        "",
    ]

    report_dimensions = request.analysis_dimensions or matrix.dimensions or DEFAULT_DIMENSIONS
    for dimension in report_dimensions:
        dimension_claims = sorted(by_dimension.get(dimension, []), key=lambda item: item.confidence, reverse=True)
        dimension_cells = [cell for cell in matrix.cells if cell.dimension == dimension]
        dimension_evidence = sorted(
            [item for item in evidence if item.dimension == dimension],
            key=lambda item: item.confidence,
            reverse=True,
        )
        lines.extend([f"### {DIMENSION_LABELS.get(dimension, dimension)}", ""])
        lines.extend(build_dimension_overview(dimension, dimension_cells))
        for claim in dimension_claims[:6]:
            cite = citations_for_ids(claim.supporting_evidence_ids, citation_numbers)
            wording = compact_fact(best_claim_text(claim), 280)
            lines.append(f"- {wording}{cite}")
        if not dimension_claims:
            lines.extend(build_dimension_fallback_points(dimension_cells, citation_numbers))
        if dimension_evidence:
            examples = [
                f"{item.competitor or '相关产品'}：{compact_fact(item.fact, 120)}"
                f"{citations_for_ids([item.evidence_id], citation_numbers)}"
                for item in dimension_evidence[:4]
            ]
            lines.append(f"- 代表性证据显示，{'；'.join(examples)}。")
        lines.append("")

    lines.extend(["## 产品与市场建议", ""])
    if recommendations:
        for item in recommendations[:5]:
            cite = citations_for_ids(item.evidence_ids, citation_numbers)
            lines.extend(
                [
                    f"### {item.title}",
                    "",
                    f"{item.recommendation}{cite}",
                    "",
                    f"- 依据：{item.rationale}",
                    f"- 预期价值：{item.expected_value}",
                    f"- 下一步：{'；'.join(item.next_steps) or '继续补充验证'}",
                    "",
                ]
            )
    else:
        lines.extend(["当前证据不足以形成强产品建议，应优先补齐目标产品的官方页面、定价页、用户反馈与企业化材料。", ""])

    lines.extend(["## 销售战斗卡", ""])
    if battlecards:
        for item in battlecards[:6]:
            cite = citations_for_ids(item.evidence_ids, citation_numbers)
            lines.extend(
                [
                    f"### 面对 {item.competitor}：{item.customer_scenario}",
                    "",
                    f"- 竞品可承认的强项：{item.competitor_strength}{cite}",
                    f"- 我方回应：{item.our_response}",
                    f"- 推荐话术：{item.talk_track}",
                    f"- 后续补齐：{item.objection_handler}",
                    "",
                ]
            )
    else:
        lines.extend(["当前缺少足够高质量竞品证据，暂不生成销售话术。", ""])

    lines.extend(
        [
            "## 风险与局限性",
            "",
            *build_risk_notes(claims, matrix, observability),
            "",
            "## 参考来源",
            "",
            *reference_lines,
            "",
            "## Evidence 附录",
            "",
        ]
    )
    for ev in evidence[:80]:
        cite = citations_for_ids([ev.evidence_id], citation_numbers)
        lines.extend(
            [
                f"### {ev.evidence_id} {cite} · {ev.dimension_label}",
                "",
                f"- 竞品：{ev.competitor or '未识别'}",
                f"- 事实：{ev.fact}",
                f"- 原文片段：{ev.quote}",
                "",
            ]
        )
    return "\n".join(lines)


def build_evidence_gaps(matrix: CompetitorMatrix) -> list[str]:
    weak_cells = sorted(
        [cell for cell in matrix.cells if cell.status in {"weak", "unknown"}],
        key=lambda cell: (cell.status != "unknown", cell.evidence_count),
    )
    if not weak_cells:
        return ["按当前产品×维度矩阵看，未发现明显空白格；后续重点应转向证据交叉验证和时效性复核。"]
    return [
        f"{cell.competitor} × {cell.dimension_label}：{cell.status}，当前 {cell.evidence_count} 条证据。"
        for cell in weak_cells[:8]
    ]


def build_citation_index(evidence: list[Evidence]) -> tuple[dict[str, int], list[str]]:
    citation_numbers: dict[str, int] = {}
    references: list[str] = []
    seen_urls: set[str] = set()
    for ev in sorted(evidence, key=lambda item: item.confidence, reverse=True):
        if ev.evidence_id in citation_numbers:
            continue
        normalized = normalize_reference_url(ev.source_url)
        if normalized in seen_urls:
            number = next(
                (
                    citation_numbers[item.evidence_id]
                    for item in evidence
                    if normalize_reference_url(item.source_url) == normalized and item.evidence_id in citation_numbers
                ),
                len(references) + 1,
            )
            citation_numbers[ev.evidence_id] = number
            continue
        seen_urls.add(normalized)
        number = len(references) + 1
        citation_numbers[ev.evidence_id] = number
        references.append(f"[{number}] {ev.source_title or ev.title}. {ev.source_url}")
        if len(references) >= 120:
            break
    return citation_numbers, references


def normalize_reference_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path}".rstrip("/")


def citations_for_ids(evidence_ids: list[str], citation_numbers: dict[str, int], limit: int = 4) -> str:
    numbers = []
    for evidence_id in evidence_ids:
        number = citation_numbers.get(evidence_id)
        if number and number not in numbers:
            numbers.append(number)
        if len(numbers) >= limit:
            break
    return "".join(f"[{number}]" for number in numbers)


def build_target_advantage_analysis(
    request: ResearchRequest,
    matrix: CompetitorMatrix,
    claims: list[Claim],
    citation_numbers: dict[str, int],
) -> tuple[list[str], list[str]]:
    target_cells = [cell for cell in matrix.cells if cell.competitor == request.target_product]
    strengths: list[str] = []
    weaknesses: list[str] = []
    for cell in sorted(target_cells, key=lambda item: (coverage_points(item), item.confidence, item.evidence_count), reverse=True):
        cite = citations_for_ids(cell.evidence_ids, citation_numbers)
        if cell.status in {"strong", "partial"}:
            strengths.append(f"- **{cell.dimension_label}**：{cell.summary}{cite}")
        else:
            weaknesses.append(
                f"- **{cell.dimension_label}**：当前公开证据偏弱，不能把它作为强卖点；建议补充可被引用的产品页、案例或用户反馈。{cite}"
            )
    if not strengths:
        lead_claims = sorted(claims, key=lambda item: item.confidence, reverse=True)[:3]
        strengths = [
            f"- {compact_fact(best_claim_text(claim), 180)}{citations_for_ids(claim.supporting_evidence_ids, citation_numbers)}"
            for claim in lead_claims
        ] or [f"- 暂未形成 {request.target_product} 的强优势结论，应优先补齐官方资料和用户反馈证据。"]
    if not weaknesses:
        weaknesses = ["- 暂无明显空白维度；下一步应重点验证优势是否能被第三方评测和用户声音交叉支持。"]
    return strengths[:6], weaknesses[:6]


def build_user_attention_analysis(
    request: ResearchRequest,
    matrix: CompetitorMatrix,
    claims: list[Claim],
    citation_numbers: dict[str, int],
) -> list[str]:
    attention_order = ["pricing", "feature", "user_voice", "enterprise", "positioning", "strategy"]
    lines: list[str] = []
    cells_by_dimension: dict[str, list[MatrixCell]] = defaultdict(list)
    for cell in matrix.cells:
        cells_by_dimension[cell.dimension].append(cell)
    for dimension in attention_order:
        cells = cells_by_dimension.get(dimension, [])
        if not cells:
            continue
        total = sum(cell.evidence_count for cell in cells)
        if total <= 0:
            continue
        label = DIMENSION_LABELS.get(dimension, dimension)
        strongest = max(cells, key=lambda item: (item.evidence_count, item.confidence))
        cite = citations_for_ids(strongest.evidence_ids, citation_numbers)
        lines.append(
            f"- **{label}** 是用户决策中的高频关注点之一：相关证据集中在 {strongest.competitor} 等产品，"
            f"说明用户会用这个维度判断工具是否值得迁移或付费。{cite}"
        )
    if not lines:
        for claim in sorted(claims, key=lambda item: item.confidence, reverse=True)[:4]:
            lines.append(
                f"- {compact_fact(best_claim_text(claim), 170)}"
                f"{citations_for_ids(claim.supporting_evidence_ids, citation_numbers)}"
            )
    return lines[:6] or ["- 当前用户关注点证据不足，应优先补充用户评论、评测文章和真实迁移反馈。"]


def build_positioning_guidance(
    request: ResearchRequest,
    matrix: CompetitorMatrix,
    strengths: list[str],
    weaknesses: list[str],
) -> list[str]:
    target = request.target_product
    target_cells = {cell.dimension: cell for cell in matrix.cells if cell.competitor == target}
    guidance = [
        f"- 把 {target} 的传播重心放在已经有证据支撑的场景上，优先讲具体工作流、适用人群和可验证结果，而不是泛泛宣称“更智能”。",
        "- 对竞品已经建立强心智的维度，宣传上不要硬碰绝对优劣，而是转成场景选择：什么时候该选我们、什么时候用户会在意集成/价格/企业治理。",
    ]
    pricing = target_cells.get("pricing")
    if pricing and pricing.status in {"weak", "unknown"}:
        guidance.append("- 定价相关材料需要更清晰：如果没有可引用的价格/套餐/试用边界，销售和评测都会难以形成确定预期。")
    user_voice = target_cells.get("user_voice")
    if user_voice and user_voice.status in {"weak", "unknown"}:
        guidance.append("- 用户口碑不足时，不宜把宣传押在“真实用户都喜欢”这类表达上；更适合用小范围案例、开发者故事和可复现实测来逐步补信任。")
    enterprise = target_cells.get("enterprise")
    if enterprise and enterprise.status in {"weak", "unknown"}:
        guidance.append("- 企业化能力如果公开证据不足，应补齐权限、数据边界、审计、部署和采购流程说明，否则大客户比较时会天然偏向资料更完整的竞品。")
    return guidance[:7]


def build_risk_notes(
    claims: list[Claim],
    matrix: CompetitorMatrix,
    observability: ObservabilitySnapshot,
) -> list[str]:
    challenged = [claim for claim in claims if claim.verification_status != "verified"]
    gaps = build_evidence_gaps(matrix)
    lines = [
        f"- 当前报告可信度约为 {observability.report_confidence:.0%}，适合用于方向判断，但关键商业结论仍应结合内部数据复核。",
    ]
    if challenged:
        lines.append(f"- 仍有 {len(challenged)} 条 Claim 未通过基础审查，报告正文已尽量采用保守措辞。")
    for gap in gaps[:4]:
        lines.append(f"- {gap}")
    return lines


def build_matrix_insights(matrix: CompetitorMatrix) -> list[str]:
    lines: list[str] = []
    for dimension in matrix.dimensions:
        cells = sorted(
            [cell for cell in matrix.cells if cell.dimension == dimension],
            key=lambda cell: (coverage_points(cell), cell.confidence, cell.evidence_count),
            reverse=True,
        )
        if not cells:
            continue
        leader = cells[0]
        laggards = [cell for cell in cells if cell.status in {"weak", "unknown"}]
        sentence = (
            f"- **{matrix.dimension_labels.get(dimension, dimension)}**："
            f"{leader.competitor} 当前证据最强（{leader.evidence_count} 条，置信度 {leader.confidence:.2f}）。"
        )
        if laggards:
            sentence += " 需要补证：" + "、".join(f"{cell.competitor}({cell.evidence_count}条)" for cell in laggards[:3]) + "。"
        else:
            sentence += " 该维度各产品均有可用证据，适合进入横向对比。"
        lines.append(sentence)
    return lines or ["- 当前矩阵暂无足够内容生成解读。"]


def build_dimension_overview(dimension: str, cells: list[MatrixCell]) -> list[str]:
    if not cells:
        return ["当前维度暂无矩阵证据。", ""]
    label = DIMENSION_LABELS.get(dimension, dimension)
    total = sum(cell.evidence_count for cell in cells)
    strong = [cell for cell in cells if cell.status == "strong"]
    weak = [cell for cell in cells if cell.status in {"weak", "unknown"}]
    lines = [
        (
            f"该维度共关联 {total} 条 Evidence；"
            f"强覆盖产品：{'、'.join(cell.competitor for cell in strong) or '暂无'}；"
            f"待补证产品：{'、'.join(cell.competitor for cell in weak) or '暂无'}。"
        )
    ]
    if cells:
        best = max(cells, key=lambda cell: (coverage_points(cell), cell.confidence, cell.evidence_count))
        lines.append(f"{label}当前最可引用的对比锚点是：{best.summary}")
    lines.append("")
    return lines


def build_dimension_fallback_points(
    cells: list[MatrixCell],
    citation_numbers: dict[str, int] | None = None,
) -> list[str]:
    if not cells:
        return ["- 当前没有抽取到足够证据形成结论。", ""]
    citation_numbers = citation_numbers or {}
    lines: list[str] = []
    for cell in sorted(cells, key=lambda item: item.evidence_count, reverse=True)[:5]:
        cite = citations_for_ids(cell.evidence_ids, citation_numbers)
        lines.append(
            f"- **{cell.competitor}**：{cell.summary} "
            f"（状态：{cell.status}；置信度：{cell.confidence:.2f}）{cite}"
        )
    lines.append("")
    return lines


def compact_fact(text: str, max_len: int = 96) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned if len(cleaned) <= max_len else cleaned[: max_len - 3] + "..."


def best_claim_text(claim: Claim) -> str:
    return claim.final_wording or claim.claim


def scenario_for_dimension(dimension: str) -> str:
    return {
        "positioning": "客户比较产品定位与品牌心智",
        "feature": "客户关注功能覆盖和工作流效率",
        "pricing": "客户质疑价格、套餐和采购成本",
        "user_voice": "客户询问真实用户口碑和迁移风险",
        "enterprise": "客户关注团队治理、安全和企业化能力",
        "strategy": "客户评估长期路线和竞争风险",
        "gtm": "客户比较生态、渠道和交付方式",
    }.get(dimension, "客户需要快速理解竞品差异")


def response_for_dimension(target_product: str, dimension: str) -> str:
    return {
        "positioning": f"把 {target_product} 的差异化场景说清楚，避免只在宏观定位上比较。",
        "feature": f"用 {target_product} 的具体任务流、集成方式和落地路径回应功能对比。",
        "pricing": f"把 {target_product} 的价值锚点、试用门槛和团队采购成本拆开说明。",
        "user_voice": f"引用真实反馈承认证据边界，同时强调 {target_product} 正在优化的体验主题。",
        "enterprise": f"围绕 {target_product} 的权限、数据边界、审计和部署策略给出可验证承诺。",
        "strategy": f"把讨论收束到 {target_product} 当前可执行的机会主题和路线图取舍。",
        "gtm": f"强调 {target_product} 面向目标用户的渠道、内容和生态协作方式。",
    }.get(dimension, f"围绕 {target_product} 的真实客户场景给出证据化回应。")


def talk_track_for_dimension(target_product: str, competitor: str, dimension: str) -> str:
    label = DIMENSION_LABELS.get(dimension, dimension)
    return (
        f"{competitor} 在公开资料中确实有{label}相关表达；我们的比较重点不是否认对方，"
        f"而是确认客户当前最重要的约束，再说明 {target_product} 在该约束下能提供什么可验证价值。"
    )


def response_for_cell(target_product: str, target_cell: MatrixCell | None, competitor_cell: MatrixCell) -> str:
    label = competitor_cell.dimension_label
    if target_cell and target_cell.status in {"strong", "partial"}:
        return (
            f"把讨论转到客户的{label}使用场景：{target_product} 已有可引用证据，"
            f"重点强调 {compact_fact(target_cell.summary, 150)}"
        )
    return (
        f"承认 {competitor_cell.competitor} 在{label}上的公开资料更完整；"
        f"{target_product} 当前应避免硬拼该卖点，改用已验证场景切入，并把{label}材料列为补齐项。"
    )


def talk_track_for_cell(
    target_product: str,
    competitor: str,
    target_cell: MatrixCell | None,
    competitor_cell: MatrixCell,
) -> str:
    label = competitor_cell.dimension_label
    if target_cell and target_cell.status in {"strong", "partial"}:
        return (
            f"“如果您关注{label}，{competitor} 的公开资料确实覆盖了这些点；"
            f"但我们建议把比较放到实际工作流里看。{target_product} 目前能拿出来对照的是："
            f"{compact_fact(target_cell.summary, 130)}。这更适合判断它是否贴合您的团队约束。”"
        )
    return (
        f"“在{label}上，{competitor} 的公开证据更充分，我们不建议用一句话判断谁绝对更好。"
        f"如果这个维度是您的采购关键项，我们会先补充 {target_product} 的可验证材料；"
        f"同时可以先从已验证的功能场景或试用结果判断是否值得进入下一轮评估。”"
    )


def followup_for_cell(target_product: str, target_cell: MatrixCell | None, competitor_cell: MatrixCell) -> str:
    label = competitor_cell.dimension_label
    if target_cell and target_cell.status in {"strong", "partial"}:
        return (
            f"准备一页 {label} 对比页，放入 {target_product} 的证据链接、客户场景和可复现实测；"
            "销售沟通中只引用已验证点，避免延展到未覆盖能力。"
        )
    return (
        f"补齐 {target_product} 的{label}公开证据：产品页说明、文档、价格/权限边界、"
        "用户案例或第三方评测至少补齐两类来源后，再把它作为主卖点。"
    )


def gate_status(score: float, pass_threshold: float, fail_threshold: float) -> str:
    if score >= pass_threshold:
        return "pass"
    if score < fail_threshold:
        return "fail"
    return "warn"


def build_matrix_markdown(
    matrix: CompetitorMatrix,
    citation_numbers: dict[str, int] | None = None,
) -> str:
    citation_numbers = citation_numbers or {}
    headers = ["维度", *matrix.competitors]
    rows = ["|" + "|".join(headers) + "|", "|" + "|".join(["---"] * len(headers)) + "|"]
    by_key = {(cell.competitor, cell.dimension): cell for cell in matrix.cells}
    for dimension in matrix.dimensions:
        row = [matrix.dimension_labels.get(dimension, dimension)]
        for competitor in matrix.competitors:
            cell = by_key.get((competitor, dimension))
            if not cell:
                row.append("暂无")
            else:
                summary = cell.summary
                if len(summary) > 130:
                    summary = summary[:127] + "..."
                cite = citations_for_ids(cell.evidence_ids, citation_numbers, limit=2)
                row.append(f"{cell.status} · {cell.confidence:.2f}<br/>{summary}{cite}")
        rows.append("|" + "|".join(csv_safe_markdown(value) for value in row) + "|")
    return "\n".join(rows)


def csv_safe_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def build_matrix_csv(matrix: CompetitorMatrix) -> str:
    rows = ["competitor,dimension,dimension_label,status,confidence,evidence_count,summary,evidence_ids"]
    for cell in matrix.cells:
        rows.append(
            ",".join(
                csv_escape(str(value))
                for value in [
                    cell.competitor,
                    cell.dimension,
                    cell.dimension_label,
                    cell.status,
                    f"{cell.confidence:.3f}",
                    str(cell.evidence_count),
                    cell.summary,
                    ";".join(cell.evidence_ids),
                ]
            )
        )
    return "\n".join(rows) + "\n"


def build_recommendations_markdown(recommendations: list[OpportunityRecommendation]) -> str:
    if not recommendations:
        return "# Opportunity Recommendations\n\n当前没有足够证据生成建议。\n"
    lines = ["# Opportunity Recommendations", ""]
    for item in recommendations:
        lines.extend(
            [
                f"## {item.title}",
                "",
                f"- Priority: {item.priority}",
                f"- Audience: {item.target_audience}",
                f"- Confidence: {item.confidence:.2f}",
                f"- Recommendation: {item.recommendation}",
                f"- Rationale: {item.rationale}",
                f"- Evidence: {', '.join(item.evidence_ids) or 'None'}",
                "",
            ]
        )
    return "\n".join(lines)


def build_battlecards_markdown(battlecards: list[BattlecardItem]) -> str:
    if not battlecards:
        return "# Battlecards\n\n当前没有足够证据生成战斗卡。\n"
    lines = ["# Battlecards", ""]
    for item in battlecards:
        lines.extend(
            [
                f"## {item.competitor} - {item.customer_scenario}",
                "",
                f"- Competitor strength: {item.competitor_strength}",
                f"- Our response: {item.our_response}",
                f"- Talk track: {item.talk_track}",
                f"- Objection handler: {item.objection_handler}",
                f"- Evidence: {', '.join(item.evidence_ids)}",
                "",
            ]
        )
    return "\n".join(lines)


def build_executive_summary(
    request: ResearchRequest,
    metrics: RunMetrics,
    observability: ObservabilitySnapshot,
    recommendations: list[OpportunityRecommendation],
) -> str:
    lines = [
        f"# {request.project_name} · Executive Summary",
        "",
        f"目标产品：{request.target_product}",
        f"本次运行获得 {metrics.sources_fetched} 篇可用内容，抽取 {metrics.evidence_count} 条 Evidence，生成 {metrics.claim_count} 条 Claim。",
        f"综合覆盖度：{observability.evidence_coverage_score:.0%}；报告可信度：{observability.report_confidence:.0%}。",
        "",
        "## Top Recommendations",
        "",
    ]
    for item in recommendations[:3]:
        lines.append(f"- {item.title}：{item.recommendation}（{item.priority}）")
    if not recommendations:
        lines.append("- 当前证据不足，建议先补充来源。")
    return "\n".join(lines) + "\n"


def build_methodology(request: ResearchRequest, observability: ObservabilitySnapshot) -> str:
    gates = "\n".join(
        f"- {gate.name}: {gate.status}, score={gate.score:.2f}, {gate.message}"
        for gate in observability.quality_gates
    )
    return (
        f"# Methodology\n\n"
        f"CompeteGraph 对「{request.project_name}」执行了 5 Agent deep research 流程："
        "ResearchPlanningAgent 规划研究范围与质量规则，SourceResearchAgent 发现来源并抓取网页，"
        "EvidenceStructuringAgent 抽取可溯源事实，AnalysisAndReviewAgent 生成结论、反方审查并整理矩阵/建议/战斗卡，"
        "ReportComposerAgent 生成本地 Markdown、JSON 与 CSV 交付文件。\n\n"
        "所有产物均保存在本 Run 目录下，可直接审计 JSON/JSONL/Markdown/CSV 文件。\n\n"
        "## Quality Gates\n\n"
        f"{gates}\n"
    )
