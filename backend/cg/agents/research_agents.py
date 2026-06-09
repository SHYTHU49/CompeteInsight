"""Five focused agents for the runnable deep-research workflow."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from cg.agents.runtime import BaseAgent
from cg.schemas.research import (
    Claim,
    CompetitorMatrix,
    DEFAULT_DIMENSIONS,
    DIMENSION_LABELS,
    Evidence,
    ObservabilitySnapshot,
    OpportunityRecommendation,
    RedTeamNote,
    ResearchFeedback,
    ResearchGap,
    ResearchPlan,
    ResearchRequest,
    RunMetrics,
    SearchMemory,
    SourceCandidate,
    SourceDocument,
    SourceTask,
)
from cg.tools.search import SearchTool, classify_source, normalize_url


RESEARCH_AGENT_FLOW = [
    "ResearchPlanningAgent",
    "SourceResearchAgent",
    "EvidenceStructuringAgent",
    "AnalysisAndReviewAgent",
    "ReportComposerAgent",
]

AGENT_SKILLS: dict[str, list[str]] = {
    "ResearchPlanningAgent": [
        "skill.scope_planning.v1",
        "skill.query_design.v1",
        "skill.quality_contract.v1",
    ],
    "SourceResearchAgent": [
        "skill.source_discovery.v1",
        "skill.source_quality_scoring.v1",
        "skill.web_collection.v1",
    ],
    "EvidenceStructuringAgent": [
        "skill.evidence_extraction.v1",
        "skill.quote_grounding.v1",
        "skill.source_attribution.v1",
    ],
    "AnalysisAndReviewAgent": [
        "skill.claim_generation.v1",
        "skill.red_team_review.v1",
        "skill.asset_synthesis.v1",
    ],
    "ReportComposerAgent": [
        "skill.report_writing.v1",
        "skill.citation_rendering.v1",
        "skill.local_export.v1",
    ],
}

AGENT_ALIASES = {
    "ChiefResearchPlanner": "ResearchPlanningAgent",
    "ChiefResearchPlannerAgent": "ResearchPlanningAgent",
    "Planner": "ResearchPlanningAgent",
    "SourceScout": "SourceResearchAgent",
    "SourceScoutAgent": "SourceResearchAgent",
    "CollectionAgent": "SourceResearchAgent",
    "EvidenceExtractorAgent": "EvidenceStructuringAgent",
    "EvidenceExtractionAgent": "EvidenceStructuringAgent",
    "ClaimGenerationAgent": "AnalysisAndReviewAgent",
    "RedTeamAgent": "AnalysisAndReviewAgent",
    "InsightSynthesisAgent": "AnalysisAndReviewAgent",
    "AnalystAgent": "AnalysisAndReviewAgent",
    "InsightSynthesizer": "AnalysisAndReviewAgent",
    "ReportWriterAgent": "ReportComposerAgent",
    "FinalQAAgent": "ReportComposerAgent",
}


class ResearchPlanningAgent(BaseAgent):
    name = "ResearchPlanningAgent"
    skill_ids = AGENT_SKILLS[name]
    skill_id = skill_ids[0]

    async def plan(
        self,
        request: ResearchRequest,
        feedback: ResearchFeedback | None = None,
        loop_round: int = 1,
    ) -> ResearchPlan:
        """生成研究计划。第 2 轮起可接受 Analysis 的反馈，聚焦补充缺口。"""
        deterministic = deterministic_plan(request)

        # 构建反馈上下文
        feedback_context = ""
        if feedback and feedback.gaps:
            gaps_str = "\n".join(
                f"- 竞品【{g.competitor}】的【{DIMENSION_LABELS.get(g.dimension, g.dimension)}】"
                f"维度：{g.reason}（优先级：{g.priority}）"
                + (f"\n  建议查询：{', '.join(g.suggested_queries)}" if g.suggested_queries else "")
                for g in feedback.gaps
            )
            feedback_context = (
                f"\n\n【第 {loop_round} 轮补充搜索反馈】：\n"
                f"上一轮搜索后分析发现以下信息缺口，请专门针对这些缺口设计补充搜索任务：\n{gaps_str}"
            )

        data = await self.invoke_json(
            system=(
                "你是竞品研究规划智能体（ResearchPlanningAgent），是整个研究流程的战略大脑。\n"
                "\n"
                "【核心职责】\n"
                "把用户的竞品研究目标拆解为覆盖全面、来源多元、可执行的搜索计划。\n"
                "你需要像一位资深行业分析师一样思考：要证明一个竞品结论，需要哪些类型的证据？\n"
                "\n"
                "【搜索策略框架】\n"
                "对每个产品（目标产品 + 所有竞品），必须覆盖以下信息层次：\n"
                "  A. 官方叙事层：官网产品介绍、功能页、定价页、企业版页、文档\n"
                "  B. 市场背书层：媒体报道、行业报告、Gartner/G2/ProductHunt 评级\n"
                "  C. 用户声音层：知乎问答、Reddit 讨论、Twitter/X 评价、GitHub Issues\n"
                "  D. 技术细节层：Changelog、GitHub 仓库、API 文档、开发者博客\n"
                "\n"
                "【查询设计规则】\n"
                "1. 每个 source_task 必须同时提供英文查询（query_en）和中文查询（query_zh）\n"
                "2. query_en 用于 Serper/Tavily 等通用搜索引擎，侧重官方和英文社区\n"
                "3. query_zh 用于知乎搜索，必须写地道中文口语，能引发用户真实讨论的问法：\n"
                "   好的示例：'Cursor 和 GitHub Copilot 哪个更好用？'、'Windsurf IDE 使用体验怎么样？'\n"
                "   差的示例：'Cursor user review'、'Windsurf 评价'（太泛）\n"
                "4. use_zhihu=true 仅用于：用户口碑、使用体验、价格感受、与竞品对比的主观评价类查询\n"
                "5. 定价查询要具体：搜 '{产品名} pricing'、'{产品名} pro plan cost'，而非泛搜\n"
                "6. 企业化查询要聚焦：SSO、SAML、审计日志、数据隐私、合规认证\n"
                "\n"
                "【输出格式】纯 JSON，不加 Markdown 代码块：\n"
                "{\n"
                '  "research_goal": "精炼后的研究目标（一句话）",\n'
                '  "competitors": ["竞品1", "竞品2"],\n'
                '  "dimensions": ["positioning","feature","pricing","user_voice","enterprise","strategy"],\n'
                '  "source_tasks": [\n'
                '    {\n'
                '      "task_id": "task_01",\n'
                '      "entity": "产品名（必须是目标产品或竞品之一）",\n'
                '      "dimension": "维度英文key",\n'
                '      "intent": "official",\n'
                '      "query_en": "具体英文搜索查询",\n'
                '      "query_zh": "具体中文搜索查询",\n'
                '      "use_zhihu": false,\n'
                '      "expected_source_types": ["official_website"],\n'
                '      "rationale": "这条查询能找到什么类型的证据，用于支撑哪个结论"\n'
                '    }\n'
                "  ],\n"
                '  "quality_rules": [\n'
                '    "每条关键结论至少绑定 2 条不同来源的 Evidence",\n'
                '    "用户口碑维度必须有用户声音层来源，不能只靠官方数据",\n'
                '    "定价结论必须来自定价页或官方公告，不接受推测"\n'
                "  ],\n"
                '  "notes": "研究难点或注意事项"\n'
                "}\n"
                "\n"
                "【数量要求】每个产品至少 5 个 source_task，总量 16-28 个，宁多勿少。\n"
                "intent 只能是：official / pricing / docs / changelog / review / enterprise / news / comparison"
            ),
            user=(
                f"项目名：{request.project_name}\n"
                f"目标产品：{request.target_product}"
                + (f"（产品描述：{request.product_description}）" if request.product_description else "")
                + f"\n竞品列表：{', '.join(request.competitors)}\n"
                f"分析维度：{', '.join(request.analysis_dimensions)}\n"
                f"研究目标：{request.research_goal}"
                + feedback_context
            ),
        )

        if not data:
            return deterministic

        # 解析 LLM 返回的 source_tasks，支持 query_en / query_zh 双字段
        raw_tasks = data.get("source_tasks") or []
        parsed_tasks: list[SourceTask] = []
        allowed_intents = {"official", "pricing", "docs", "changelog", "review", "enterprise", "news", "comparison"}
        for i, row in enumerate(raw_tasks[:32]):
            if not isinstance(row, dict):
                continue
            # 优先用 query_en 作为英文 query，再回退到 query
            query_en = str(row.get("query_en") or row.get("query") or "").strip()
            query_zh = str(row.get("query_zh") or "").strip()
            use_zhihu = bool(row.get("use_zhihu", False))
            if not query_en and not query_zh:
                continue
            intent = str(row.get("intent") or "official").lower()
            if intent not in allowed_intents:
                intent = "official"
            dimension = str(row.get("dimension") or "other")
            if dimension not in DIMENSION_LABELS:
                dimension = "other"
            entity = str(row.get("entity") or "unknown").strip() or "unknown"
            # 生成英文任务
            if query_en:
                parsed_tasks.append(SourceTask(
                    task_id=str(row.get("task_id") or f"task_{i + 1:02d}_en"),
                    entity=entity,
                    dimension=dimension,
                    intent=intent,  # type: ignore[arg-type]
                    query=query_en,
                    expected_source_types=coerce_str_list(row.get("expected_source_types")),
                    rationale=str(row.get("rationale") or ""),
                ))
            # 生成中文任务（标注为知乎查询）
            if query_zh and use_zhihu:
                parsed_tasks.append(SourceTask(
                    task_id=f"task_{i + 1:02d}_zh",
                    entity=entity,
                    dimension=dimension,
                    intent=intent,  # type: ignore[arg-type]
                    query=query_zh,
                    expected_source_types=["user_review"],
                    rationale=f"中文用户声音搜索（知乎）: {query_zh}",
                ))

        if not parsed_tasks:
            parsed_tasks = deterministic.source_tasks

        queries = [task.query for task in parsed_tasks]
        return ResearchPlan(
            research_goal=str(data.get("research_goal") or deterministic.research_goal),
            competitors=coerce_str_list(data.get("competitors")) or deterministic.competitors,
            dimensions=normalize_dimensions(coerce_str_list(data.get("dimensions"))) or deterministic.dimensions,
            queries=queries[:32],
            source_tasks=parsed_tasks[:32],
            required_agents=normalize_agent_flow(
                coerce_str_list(data.get("required_agents")) or deterministic.required_agents
            ),
            quality_rules=coerce_str_list(data.get("quality_rules")) or deterministic.quality_rules,
            notes=str(data.get("notes") or ""),
            planned_by=self.name if self.llm_enabled else f"{self.name}:deterministic",
        )


class SourceResearchAgent(BaseAgent):
    """ReAct 自主搜索智能体：自主决定搜什么、何时停止，而非执行预定查询列表。"""
    name = "SourceResearchAgent"
    skill_ids = AGENT_SKILLS[name]
    skill_id = skill_ids[0]

    async def discover(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        search: SearchTool,
        memory: SearchMemory | None = None,
        feedback: ResearchFeedback | None = None,
        loop_round: int = 1,
    ) -> list[SourceCandidate]:
        """两层搜索：先执行 Planning 查询铺底，再由 Search LLM ReAct 补洞。"""
        candidates: list[SourceCandidate] = []
        seen_urls: set[str] = set(memory.seen_urls if memory else [])
        seen_queries: set[str] = set(memory.seen_queries if memory else [])
        remaining_slots = memory.remaining_source_slots if memory and memory.remaining_source_slots > 0 else request.max_sources
        if memory and memory.resource_limit_reached:
            await self.record_llm_event(
                "progress",
                "resource_limit_reached",
                f"来源资源上限 {request.max_sources} 已达到，Search Agent 不再发起新搜索",
                {"max_sources": request.max_sources, "seen_urls": len(seen_urls)},
            )
            return []

        # 种子 URL 直接加入
        for url in request.seed_urls:
            normalized = normalize_url(url)
            if normalized and normalized not in seen_urls:
                seen_urls.add(normalized)
                candidates.append(SourceCandidate(
                    url=normalized,
                    title=urlparse(normalized).netloc or url,
                    source_type=classify_source(url),
                    query="seed_url",
                    score=0.88,
                ))

        if not request.auto_discover_sources:
            return dedupe_source_candidates(candidates)

        max_rounds = getattr(request, "max_search_rounds", 3)
        use_llm_reasoning = isinstance(search, SearchTool)
        no_new_consecutive = 0  # 连续无新增轮次计数
        per_provider_count = max(
            1,
            min(
                request.max_sources_per_query or self.ctx.settings.cg_search_default_results_per_provider,
                self.ctx.settings.cg_search_max_results_per_provider,
            ),
        )

        for round_num in range(1, max_rounds + 1):
            if remaining_slots <= 0:
                await self.record_llm_event(
                    "progress",
                    "resource_limit_reached",
                    f"来源资源上限 {request.max_sources} 已达到，停止后续搜索",
                    {"max_sources": request.max_sources, "seen_urls": len(seen_urls)},
                )
                break
            # 第一层：第 1 轮只执行 Planning 的几十条 query，不调用 Search LLM。
            if round_num == 1:
                next_tasks = unique_tasks_by_query(plan.source_tasks)
                await self.record_llm_event(
                    "progress", "planned_search",
                    f"执行 {len(next_tasks)} 条 Query 快速获取相关信息",
                    {
                        "round": round_num,
                        "outer_loop_round": loop_round,
                        "task_count": len(next_tasks),
                        "queries": [task.query for task in next_tasks],
                        "feedback": feedback.message if feedback else "",
                    },
                )
            else:
                # 第二层：Search LLM 推理当前内容是否充分，并自主决定下一批 query 与返回数量。
                decision = await self._reason_search_decision(
                    request,
                    plan,
                    candidates,
                    seen_urls,
                    seen_queries,
                    round_num,
                    per_provider_count,
                    feedback=feedback,
                    loop_round=loop_round,
                    use_llm_reasoning=use_llm_reasoning,
                )
                per_provider_count = max(
                    1,
                    min(
                        int(decision.get("max_results_per_provider") or per_provider_count),
                        self.ctx.settings.cg_search_max_results_per_provider,
                    ),
                )
                if decision.get("should_stop"):
                    await self.record_llm_event(
                        "progress", "converged",
                        f"第 {round_num} 轮推理：{str(decision.get('stop_reason') or 'Search Agent 判断信息已满足要求')}",
                        {
                            "round": round_num,
                            "outer_loop_round": loop_round,
                            "coverage": estimate_coverage(
                                candidates,
                                [request.target_product, *request.competitors],
                                request.analysis_dimensions,
                            ),
                            "candidate_count": len(candidates),
                        },
                    )
                    break
                next_tasks = decision.get("tasks") or []

            if not next_tasks:
                coverage = estimate_coverage(
                    candidates,
                    [request.target_product, *request.competitors],
                    request.analysis_dimensions,
                )
                if coverage < self.ctx.settings.cg_min_coverage_to_stop:
                    next_tasks = self._fallback_gap_tasks(
                        request, plan, candidates, seen_urls, round_num
                    )
                if not next_tasks:
                    await self.record_llm_event(
                        "progress", "converged",
                        f"第 {round_num} 轮：没有发现新的高价值查询，停止搜索",
                        {"round": round_num, "total_candidates": len(candidates), "coverage": coverage},
                    )
                    break

            # ── Act 阶段：执行搜索（英文通用 + 中文知乎）──
            round_new = 0
            for task in next_tasks:
                if remaining_slots <= 0:
                    break
                if task.query.lower() in seen_queries and round_num > 1:
                    continue
                seen_queries.add(task.query.lower())
                results = await self.search_task(
                    search,
                    task,
                    per_provider_count,
                    f"outer_{loop_round}_search_{round_num}",
                    round_num,
                    loop_round,
                )

                # ── Observe 阶段：判断结果是否带来新信息 ──
                for c in results:
                    norm = normalize_url(c.url)
                    if norm and norm not in seen_urls:
                        seen_urls.add(norm)
                        candidates.append(c)
                        round_new += 1
                        remaining_slots -= 1
                        if remaining_slots <= 0:
                            break

            await self.record_llm_event(
                "progress", "round_complete",
                f"第 {round_num} 轮搜索完成，新增 {round_new} 个来源，累计 {len(candidates)} 个",
                {"round": round_num, "new_this_round": round_new, "total": len(candidates)},
            )

            # 收敛判断：连续 2 轮无新增 → 停止
            if round_new == 0:
                no_new_consecutive += 1
                if no_new_consecutive >= 2:
                    await self.record_llm_event(
                        "progress", "converged",
                        f"连续 2 轮无新来源，提前收敛（共 {round_num} 轮）",
                        {"round": round_num},
                    )
                    break
            else:
                no_new_consecutive = 0

            # 覆盖度充足提前停止
            coverage = estimate_coverage(
                candidates,
                [request.target_product, *request.competitors],
                request.analysis_dimensions,
            )
            dimensions = request.analysis_dimensions or DEFAULT_DIMENSIONS
            entities = [request.target_product, *request.competitors]
            covered_pairs = infer_candidate_pairs(candidates, plan.source_tasks, entities, dimensions)
            empty_pairs = [
                (entity, dimension)
                for entity in entities
                for dimension in dimensions
                if (entity, dimension) not in covered_pairs
            ]
            minimum_candidates = min(
                max(request.max_sources, len(entities) * len(dimensions)),
                len(entities) * len(dimensions) * request.max_sources_per_query,
            )
            if (
                coverage >= self.ctx.settings.cg_min_coverage_to_stop
                and not empty_pairs
                and len(candidates) >= minimum_candidates
            ):
                await self.record_llm_event(
                    "progress", "converged",
                    f"覆盖度 {coverage:.0%} 已达标，提前停止（{round_num} 轮）",
                    {"round": round_num, "coverage": coverage, "candidate_count": len(candidates)},
                )
                break

        return dedupe_source_candidates(candidates)

    async def _reason_search_decision(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        current_candidates: list[SourceCandidate],
        seen_urls: set[str],
        seen_queries: set[str],
        round_num: int,
        current_max_results: int,
        *,
        feedback: ResearchFeedback | None,
        loop_round: int,
        use_llm_reasoning: bool = True,
    ) -> dict[str, Any]:
        if not self.llm_enabled or not use_llm_reasoning:
            return {
                "tasks": self._fallback_gap_tasks(request, plan, current_candidates, seen_urls, round_num),
                "max_results_per_provider": current_max_results,
                "should_stop": False,
            }

        entities = [request.target_product, *request.competitors]
        dimensions = request.analysis_dimensions or DEFAULT_DIMENSIONS
        pair_counts = candidate_pair_counts(current_candidates, plan.source_tasks, entities, dimensions)
        provider_counts = Counter(c.source_provider for c in current_candidates if c.source_provider)
        source_type_counts = Counter(c.source_type for c in current_candidates if c.source_type)
        coverage_lines = []
        for entity in entities:
            cell_parts = []
            for dim in dimensions:
                cell_parts.append(f"{DIMENSION_LABELS.get(dim, dim)}={pair_counts.get((entity, dim), 0)}")
            coverage_lines.append(f"- {entity}: " + "，".join(cell_parts))

        data = await self.invoke_json(
            system=(
                "你是 SourceResearchAgent 的 ReAct 推理模块。\n"
                "你要判断当前搜索内容是否足够支撑后续 Evidence 和 Analysis。\n\n"
                "【停止条件，必须同时满足】\n"
                "1. 每个产品×每个维度至少有 3 条不同 URL 的候选内容\n"
                "2. 定价维度必须尽量有官方定价页或可信 pricing 来源\n"
                "3. 用户口碑维度必须有知乎/社区/评测类用户声音\n"
                "4. Analysis Agent 反馈的缺口已经被补上\n"
                "5. 最近搜索仍有新增内容；若连续两轮没有新增，系统会自动停止\n\n"
                "【行动空间】\n"
                "- 若不满足，生成下一批 query，最多 8 条\n"
                "- query 可以中文或英文；中文用户体验问题优先给知乎，英文技术/官方问题给 Tavily/Exa\n"
                "- 你可以调整 max_results_per_provider，默认 5；持续找不到好内容时提高到 8-12\n\n"
                "输出 JSON：{\n"
                '  "reasoning": "逐产品×维度说明哪里够、哪里不够",\n'
                '  "should_stop": false,\n'
                '  "stop_reason": "",\n'
                '  "max_results_per_provider": 5,\n'
                '  "next_tasks": [{"entity":"产品","dimension":"pricing","intent":"pricing","query":"...","rationale":"..."}]\n'
                "}"
            ),
            user=(
                f"外层 Research Loop：第 {loop_round} 轮\n"
                f"Search 内部轮次：第 {round_num} 轮\n"
                f"目标产品：{request.target_product}\n"
                f"竞品：{', '.join(request.competitors)}\n"
                f"维度：{', '.join(dimensions)}\n"
                f"Analysis 反馈：{feedback.message if feedback else '无'}\n"
                f"Feedback gaps：{[gap.model_dump(mode='json') for gap in (feedback.gaps if feedback else [])]}\n\n"
                "当前覆盖：\n" + "\n".join(coverage_lines) + "\n\n"
                f"总来源数：{len(current_candidates)}\n"
                f"provider 分布：{dict(provider_counts)}\n"
                f"source type 分布：{dict(source_type_counts)}\n"
                f"已搜 query（避免重复）：{list(seen_queries)[-40:]}\n"
                f"已见 URL（截断）：{list(seen_urls)[-40:]}\n"
                f"当前每引擎返回数：{current_max_results}"
            ),
        )
        if not data:
            return {
                "tasks": self._fallback_gap_tasks(request, plan, current_candidates, seen_urls, round_num),
                "max_results_per_provider": current_max_results,
                "should_stop": False,
            }

        reasoning = str(data.get("reasoning") or "")
        should_stop = bool(data.get("should_stop", False))
        await self.record_llm_event(
            "progress", "reasoning",
            f"第 {round_num} 轮推理：{reasoning[:220]}",
            {
                "round": round_num,
                "outer_loop_round": loop_round,
                "reasoning": reasoning,
                "should_stop": should_stop,
                "max_results_per_provider": data.get("max_results_per_provider") or current_max_results,
            },
        )

        tasks: list[SourceTask] = []
        allowed_intents = {"official", "pricing", "docs", "changelog", "review", "enterprise", "news", "comparison"}
        for i, row in enumerate((data.get("next_tasks") or [])[:8]):
            if not isinstance(row, dict):
                continue
            query = str(row.get("query") or "").strip()
            if len(query) < 4:
                continue
            intent = str(row.get("intent") or "official").lower()
            if intent not in allowed_intents:
                intent = "official"
            dimension = str(row.get("dimension") or "other")
            if dimension not in DIMENSION_LABELS:
                dimension = "other"
            tasks.append(SourceTask(
                task_id=f"react_r{round_num}_{i + 1:02d}",
                entity=str(row.get("entity") or request.target_product).strip(),
                dimension=dimension,
                intent=intent,  # type: ignore[arg-type]
                query=query,
                expected_source_types=["user_review"] if dimension == "user_voice" else [],
                rationale=str(row.get("rationale") or ""),
            ))
        return {
            "tasks": tasks,
            "max_results_per_provider": data.get("max_results_per_provider") or current_max_results,
            "should_stop": should_stop,
            "stop_reason": data.get("stop_reason") or "",
        }

    async def _reason_next_tasks(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        current_candidates: list[SourceCandidate],
        seen_urls: set[str],
        round_num: int,
        *,
        use_llm_reasoning: bool = True,
    ) -> list[SourceTask]:
        """ReAct Reason 步骤：LLM 分析当前已搜内容，决定下一批搜索任务。"""

        # 构造当前状态摘要给 LLM
        entity_url_counts = Counter[str]()
        dimension_url_counts = Counter[str]()
        for c in current_candidates:
            text = f"{c.url} {c.title} {c.snippet}".lower()
            for entity in [request.target_product, *request.competitors]:
                if entity.lower() in text:
                    entity_url_counts[entity] += 1
            for dim in request.analysis_dimensions:
                keywords = dimension_search_keywords(dim)
                if any(kw.lower() in text for kw in keywords):
                    dimension_url_counts[dim] += 1

        coverage_summary = {
            "每个产品的已有来源数": dict(entity_url_counts),
            "每个维度的已有来源数": {
                DIMENSION_LABELS.get(d, d): dimension_url_counts[d]
                for d in request.analysis_dimensions
            },
            "总来源数": len(current_candidates),
            "已知URL列表（用于去重）": list(seen_urls)[:40],
        }

        # 第一轮直接用 plan 里的任务，不调 LLM（节省 token）
        if round_num == 1 and plan.source_tasks:
            tasks = plan.source_tasks[:16]
            await self.record_llm_event(
                "progress", "tool_call",
                f"执行 {len(tasks)} 条 Query 快速获取相关信息",
                {"round": 1, "task_count": len(tasks)},
            )
            return tasks

        if not self.llm_enabled or not use_llm_reasoning:
            # 无 LLM：基于缺口规则生成
            return self._fallback_gap_tasks(request, plan, current_candidates, seen_urls, round_num)

        data = await self.invoke_json(
            system=(
                "你是竞品研究搜索智能体（SourceResearchAgent），负责自主决定搜索策略。\n"
                "\n"
                "【当前任务】\n"
                "分析已搜索到的来源覆盖情况，判断哪些信息还不够，决定下一步搜什么。\n"
                "\n"
                "【判断信息充足的标准】\n"
                "满足以下【所有】条件时才可以停止（should_stop=true）：\n"
                "  1. 每个产品在每个维度都有至少 3 条不同来源的 URL\n"
                "  2. 用户声音维度（user_voice）已有来自真实用户讨论的来源（知乎/Reddit/论坛）\n"
                "  3. 定价维度已有每个产品的官方定价页\n"
                "  4. 总来源数超过 40 条\n"
                "只要有任何一个产品×维度组合的来源数为 0，就不能停止。\n"
                "\n"
                "【下一批查询的设计原则】\n"
                "  A. 优先补充完全没有来源的产品×维度组合\n"
                "  B. 其次补充只有官方来源、缺少用户声音的维度\n"
                "  C. 避免重复搜索已覆盖充分的内容\n"
                "  D. 用户口碑类查询（use_zhihu=true）写中文口语，如：\n"
                "     '从 GitHub Copilot 换到 Cursor 之后感受如何？'\n"
                "     'Windsurf IDE 和 Cursor 对比，哪个更值得付费？'\n"
                "  E. 技术对比类查询举例：'Cursor vs GitHub Copilot context window benchmark'\n"
                "  F. 定价类查询举例：'Cursor Pro pricing 2024 annual plan'\n"
                "\n"
                "【输出格式】JSON：\n"
                "{\n"
                '  "reasoning": "逐产品×维度分析：已有X条，还缺Y，原因Z",\n'
                '  "should_stop": false,\n'
                '  "stop_reason": "（should_stop=true 时填写）",\n'
                '  "next_tasks": [\n'
                '    {\n'
                '      "entity": "产品名",\n'
                '      "dimension": "维度英文key",\n'
                '      "intent": "review",\n'
                '      "query": "具体搜索查询（中文或英文）",\n'
                '      "use_zhihu": false,\n'
                '      "rationale": "这条查询能补充什么具体信息"\n'
                '    }\n'
                "  ]\n"
                "}\n"
                "\n"
                "next_tasks 每次最多 8 条，intent 只能是：\n"
                "official / pricing / docs / changelog / review / enterprise / news / comparison"
            ),
            user=(
                f"目标产品：{request.target_product}"
                + (f"（{request.product_description}）" if request.product_description else "")
                + f"\n竞品：{', '.join(request.competitors)}\n"
                f"分析维度：{[DIMENSION_LABELS.get(d, d) for d in request.analysis_dimensions]}\n"
                f"当前第 {round_num} 轮（共最多 {getattr(request, 'max_search_rounds', 3)} 轮）\n\n"
                f"当前搜索覆盖状态：\n"
                f"- 各产品已有来源数：{dict(entity_url_counts)}\n"
                f"- 各维度已有来源数：{ {DIMENSION_LABELS.get(d, d): dimension_url_counts[d] for d in request.analysis_dimensions} }\n"
                f"- 总来源数：{len(current_candidates)}\n"
                f"- 已知URL（用于去重，勿重复）：{list(seen_urls)[:30]}"
            ),
        )

        if not data:
            return self._fallback_gap_tasks(request, plan, current_candidates, seen_urls, round_num)

        reasoning = str(data.get("reasoning") or "")
        should_stop = bool(data.get("should_stop", False))

        await self.record_llm_event(
            "progress", "reasoning",
            f"第 {round_num} 轮推理：{reasoning[:120]}",
            {"round": round_num, "reasoning": reasoning, "should_stop": should_stop},
        )

        if should_stop:
            return []

        raw_tasks = data.get("next_tasks") or []
        tasks: list[SourceTask] = []
        allowed_intents = {"official", "pricing", "docs", "changelog", "review", "enterprise", "news", "comparison"}
        for i, row in enumerate(raw_tasks[:8]):
            if not isinstance(row, dict):
                continue
            query = str(row.get("query") or "").strip()
            if len(query) < 4:
                continue
            intent = str(row.get("intent") or "official").lower()
            if intent not in allowed_intents:
                intent = "official"
            dimension = str(row.get("dimension") or "other")
            if dimension not in DIMENSION_LABELS:
                dimension = "other"
            use_zhihu = bool(row.get("use_zhihu", False))
            tasks.append(SourceTask(
                task_id=f"react_r{round_num}_{i + 1:02d}",
                entity=str(row.get("entity") or request.target_product).strip(),
                dimension=dimension,
                intent=intent,  # type: ignore[arg-type]
                query=query,
                expected_source_types=["user_review"] if use_zhihu else ["official_website"],
                rationale=str(row.get("rationale") or ""),
            ))
        return tasks

    def _fallback_gap_tasks(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        candidates: list[SourceCandidate],
        seen_urls: set[str],
        round_num: int,
    ) -> list[SourceTask]:
        """无 LLM 时基于覆盖缺口规则生成补充任务。"""
        entities = [request.target_product, *request.competitors]
        dimensions = request.analysis_dimensions or DEFAULT_DIMENSIONS
        covered = infer_candidate_pairs(candidates, plan.source_tasks, entities, dimensions)
        tasks: list[SourceTask] = []
        used_queries: set[str] = {normalize_url(c.url) for c in candidates}
        used_queries.update(c.query.lower() for c in candidates if c.query)

        deduped = dedupe_source_candidates(candidates)
        domains = [source_domain(candidate.url) for candidate in deduped]
        domain_counts = Counter(domain for domain in domains if domain)
        dominant_share = max(domain_counts.values(), default=0) / max(1, len(deduped))
        if len(deduped) >= 3 and dominant_share > 0.6:
            for entity in entities:
                if "user_voice" in dimensions:
                    diversity_task = SourceTask(
                        task_id=f"gap_diversity_{round_num:02d}_{len(tasks) + 1:02d}",
                        entity=entity,
                        dimension="user_voice",
                        intent="comparison",
                        query=f"{entity} reviews comparison alternatives user feedback",
                        expected_source_types=["review_platform", "blog"],
                        rationale="Diversify source domains and add third-party user perspective.",
                    )
                    if diversity_task.query.lower() not in used_queries:
                        tasks.append(diversity_task)
                        used_queries.add(diversity_task.query.lower())
                if len(tasks) >= 2:
                    break

        for entity in entities:
            for dim in dimensions:
                if (entity, dim) in covered:
                    continue
                task = make_gap_source_task(entity, dim, len(tasks) + 1)
                if task.query.lower() not in used_queries:
                    tasks.append(task)
                    used_queries.add(task.query.lower())
                if len(tasks) >= 6:
                    return tasks
        return tasks

    async def _search_zhihu_task(
        self,
        search: SearchTool,
        task: SourceTask,
        max_results: int,
        round_num: int,
    ) -> list[SourceCandidate]:
        """调用知乎搜索并记录 trace。"""
        await self.record_llm_event(
            "progress", "tool_call",
            f"知乎搜索：{task.query}",
            {
                "tool": "zhihu_search",
                "query": task.query,
                "entity": task.entity,
                "dimension": task.dimension,
                "round": round_num,
            },
        )
        try:
            results = await search.search_zhihu(task.query, max_results=max_results)
        except Exception as exc:
            await self.record_llm_event(
                "error", "failed",
                f"知乎搜索失败：{exc!s:.200}",
                {"tool": "zhihu_search", "query": task.query},
            )
            return []
        await self.record_llm_event(
            "progress", "tool_result",
            f"知乎搜索返回 {len(results)} 条结果",
            {"tool": "zhihu_search", "query": task.query, "count": len(results)},
        )
        for r in results:
            r.query = task.query
        return results

    async def search_task(
        self,
        search: SearchTool,
        task: SourceTask,
        max_results: int,
        search_round: str,
        round_num: int = 1,
        loop_round: int = 1,
    ) -> list[SourceCandidate]:
        await self.record_llm_event(
            "progress", "tool_call",
            f"内容搜索：{task.query}",
            {
                "tool": "content_search",
                "task_id": task.task_id,
                "entity": task.entity,
                "dimension": task.dimension,
                "intent": task.intent,
                "query": task.query,
                "expected_source_types": task.expected_source_types,
                "search_round": search_round,
                "round": round_num,
                "outer_loop_round": loop_round,
                "max_results_per_provider": max_results,
            },
        )
        try:
            if hasattr(search, "search_content_providers"):
                provider_batches = await search.search_content_providers(task.query, max_results_per_provider=max_results)
            else:
                legacy_results = await search.search(task.query, max_results=max_results)
                provider_batches = {"search": legacy_results}
        except Exception as exc:
            await self.record_llm_event(
                "error", "failed",
                f"搜索失败：{exc!s:.200}",
                {"tool": "search", "task_id": task.task_id, "query": task.query},
            )
            return []
        results: list[SourceCandidate] = []
        for provider, provider_results in provider_batches.items():
            for result in provider_results:
                result.query = task.query
                if result.source_type == "other" and task.expected_source_types:
                    result.source_type = task.expected_source_types[0]
                result.score = score_candidate_for_task(result, task, search_round)
                results.append(result)
            await self.record_llm_event(
                "progress", "tool_result",
                f"{provider_display_name(provider)} 返回 {len(provider_results)} 条结果",
                {
                    "tool": provider,
                    "task_id": task.task_id,
                    "query": task.query,
                    "count": len(provider_results),
                    "search_round": search_round,
                    "results": [
                        {
                            "title": item.title,
                            "url": item.url,
                            "snippet": item.snippet,
                            "content_source": item.content_source,
                        }
                        for item in provider_results
                    ],
                },
            )
        for result in results:
            result.query = task.query
            if result.source_type == "other" and task.expected_source_types:
                result.source_type = task.expected_source_types[0]
            result.score = score_candidate_for_task(result, task, search_round)
        return results

    async def collect(
        self,
        candidates: list[SourceCandidate],
        fetcher,
        save_document: Callable[[SourceCandidate, Any], Awaitable[SourceDocument]],
        trace: Callable[[str, str, str, dict[str, Any] | None], Awaitable[None]],
    ) -> list[SourceDocument]:
        documents: list[SourceDocument] = []
        for candidate in candidates:
            page = await fetcher.fetch(candidate.url)
            document = await save_document(candidate, page)
            documents.append(document)
            await trace(
                "progress",
                "fetched" if document.ok else "failed",
                document.title,
                {"url": document.url, "ok": document.ok, "error": document.error},
            )
        return documents


class EvidenceStructuringAgent(BaseAgent):
    name = "EvidenceStructuringAgent"
    skill_ids = AGENT_SKILLS[name]
    skill_id = skill_ids[0]

    async def extract(
        self,
        request: ResearchRequest,
        document: SourceDocument,
        dimensions: list[str],
        entities: list[str],
        deterministic_items: list[Evidence],
        stable_id_fn,
        source_weight_fn,
    ) -> list[Evidence]:
        if not self.llm_enabled or not document.content:
            return deterministic_items

        is_user_voice = document.source_type in {"user_review", "review_platform"}

        if is_user_voice:
            system_prompt = (
                "你是 Evidence Structuring Agent，专门处理用户真实评价内容。\n"
                "\n"
                "【当前来源类型】用户声音（知乎/社区/评测）\n"
                "\n"
                "【提取规则】\n"
                "1. 重点提取用户对产品的主观感受、对比意见、痛点、好评点\n"
                "2. 保留用户语气中的程度词：'明显快很多'、'基本没用'、'比 X 差多了'\n"
                "3. competitor 字段：填写被评价/被对比的产品名；若是横向对比，填主要被比较的竞品\n"
                "4. fact 字段：用'用户反馈'开头，如'用户反馈 Cursor 的代码补全速度明显优于 GitHub Copilot'\n"
                "5. quote 必须是原文中的真实片段，保留原文措辞\n"
                "6. confidence 按权威度打分：知名用户/有认证/高赞回答 → 0.70-0.82，普通用户 → 0.50-0.65\n"
                "7. dimension 优先分配到 user_voice，若内容明确涉及定价/功能/企业能力则分配对应维度\n"
                "\n"
                f"dimension 只能从以下值选择：{', '.join(dimensions)}\n"
                "输出 JSON：{\"evidence\":[{\"competitor\", \"dimension\", \"fact\", \"quote\", \"confidence\"}]}\n"
                "每篇文档最多提取 8 条，只保留有实质信息的，不要提取空洞废话。"
            )
        else:
            system_prompt = (
                "你是 Evidence Structuring Agent，专门从官方/媒体内容中提取结构化证据。\n"
                "\n"
                "【当前来源类型】官方页面 / 媒体报道 / 技术文档\n"
                "\n"
                "【提取规则】\n"
                "1. 只提取可验证的事实性陈述，禁止提取推测、广告语或无法核实的声明\n"
                "2. fact 字段：用中文写一句完整的竞品事实，包含：主语（哪个产品）+ 具体内容\n"
                "   好的示例：'GitHub Copilot Enterprise 版本支持企业级 SAML SSO 和审计日志'\n"
                "   差的示例：'该产品功能强大，受到广泛好评'（太模糊）\n"
                "3. quote 必须是原文中的连续片段，字数 20-300，不能改写\n"
                "4. competitor 字段：填写证据描述的产品名（可以是目标产品或竞品）\n"
                "5. confidence 按来源权威度：官方定价/文档页 → 0.88-0.95，\n"
                "   新闻/博客 → 0.70-0.82，第三方评测 → 0.65-0.78\n"
                "6. 同一事实只提取一次，不要重复\n"
                "\n"
                f"dimension 只能从以下值选择：{', '.join(dimensions)}\n"
                "输出 JSON：{\"evidence\":[{\"competitor\", \"dimension\", \"fact\", \"quote\", \"confidence\"}]}\n"
                "每篇文档最多提取 10 条最重要的证据。"
            )

        data = await self.invoke_json(
            system=system_prompt,
            user=(
                f"目标产品：{request.target_product}\n"
                f"竞品：{', '.join(request.competitors)}\n"
                f"来源类型：{document.source_type}\n"
                f"网页标题：{document.title}\n"
                f"URL：{document.url}\n\n"
                f"正文内容：\n{document.content[:8000]}"
            ),
        )
        rows = data.get("evidence") if data else None
        if not isinstance(rows, list):
            return deterministic_items
        items: list[Evidence] = []
        content_lower = document.content.lower()
        max_items = 10 if is_user_voice else 12
        for row in rows[:max_items]:
            if not isinstance(row, dict):
                continue
            dimension = str(row.get("dimension") or "other")
            if dimension not in dimensions:
                dimension = "other"
            quote = str(row.get("quote") or "").strip()
            fact = str(row.get("fact") or "").strip()
            if len(quote) < 15 or not fact:
                continue
            # 知乎内容来自 snippet/摘要，quote 可能不完全匹配 content，放宽校验
            if not is_user_voice and quote.lower() not in content_lower:
                continue
            confidence = clamp_float(row.get("confidence"), 0.45, 0.95)
            evidence_id = stable_id_fn("ev", f"{self.ctx.run_id}:{document.url}:{dimension}:{quote}")
            competitor = str(row.get("competitor") or "").strip() or None
            freshness = compute_freshness_score(document.published_at, dimension)
            items.append(
                Evidence(
                    evidence_id=evidence_id,
                    run_id=self.ctx.run_id,
                    url=document.url,
                    title=document.title,
                    content=quote[:500],
                    fetched_at=document.fetched_at,
                    source_type=document.source_type,
                    dimension=dimension,
                    dimension_label=DIMENSION_LABELS.get(dimension, dimension),
                    competitor=competitor,
                    fact=fact[:500],
                    quote=quote[:500],
                    source_title=document.title,
                    source_url=document.url,
                    source_id=document.source_id,
                    confidence=confidence,
                    authority_score=round(source_weight_fn(document.source_type) + 0.3, 3),
                    freshness_score=freshness,
                    relevance_score=confidence,
                    extracted_by_agent=self.name,
                    extracted_by_skill=self.skill_id,
                    published_at=document.published_at,
                )
            )
        return items or deterministic_items


class AnalysisAndReviewAgent(BaseAgent):
    name = "AnalysisAndReviewAgent"
    skill_ids = AGENT_SKILLS[name]
    skill_id = skill_ids[0]

    async def generate(self, evidence: list[Evidence], deterministic_claims: list[Claim], request: "ResearchRequest | None" = None) -> list[Claim]:
        if not self.llm_enabled or not evidence:
            return deterministic_claims

        # 从 evidence 或 request 里推断产品名
        target = request.target_product if request else "目标产品"
        competitors_str = ", ".join(request.competitors) if request else "竞品"

        # 按维度聚合 Evidence，便于 LLM 做横向对比
        by_dim: dict[str, list[dict]] = {}
        for ev in evidence[:100]:
            dim = ev.dimension
            by_dim.setdefault(dim, []).append({
                "evidence_id": ev.evidence_id,
                "competitor": ev.competitor,
                "fact": ev.fact,
                "quote": ev.quote[:200],
                "source_type": ev.source_type,
                "source_url": ev.source_url,
                "confidence": ev.confidence,
            })

        data = await self.invoke_json(
            system=(
                "你是竞品分析智能体（AnalysisAndReviewAgent）的结论生成技能。\n"
                "\n"
                "【核心原则】\n"
                "你的工作是从 Evidence 中提炼真正有价值的竞品洞察，而不是简单复述证据。\n"
                "好的 Claim 要回答：'在这个维度上，这些产品之间的关键差异是什么？这对用户意味着什么？'\n"
                "\n"
                "【结论生成要求】\n"
                "1. 优先生成【横向对比型】结论：\n"
                "   '在企业化能力上，GitHub Copilot 提供了 SAML SSO 和审计日志，而 Cursor 和 Windsurf 尚未公开类似企业级功能'\n"
                "2. 其次生成【单产品洞察型】结论：\n"
                "   'Cursor 的 Context Window 策略是其核心差异化，支持全仓库代码索引'\n"
                "3. 避免生成【无实质内容的废话型】结论：\n"
                "   '各产品均在积极发展中'（×）'功能较为丰富'（×）\n"
                "4. 每条 Claim 必须绑定至少 1 条 supporting_evidence_id，且 evidence_id 必须真实存在\n"
                "5. 如果用户声音证据（source_type=user_review）与官方说法有出入，生成专门的对比 Claim\n"
                "6. confidence 反映证据充分程度：多源交叉验证 → 0.80+，单源 → 0.55-0.70\n"
                "\n"
                "【输出格式】JSON：\n"
                '{"claims":[{\n'
                '  "dimension": "维度英文key",\n'
                '  "claim": "完整的分析结论（中文，50-200字）",\n'
                '  "supporting_evidence_ids": ["ev_xxx", "ev_yyy"],\n'
                '  "confidence": 0.75,\n'
                '  "reasoning_summary": "基于哪些证据得出此结论，推理链条是什么（2-3句）"\n'
                "}]}"
            ),
            user=(
                f"目标产品：{target}\n"
                f"竞品：{competitors_str}\n\n"
                "按维度聚合的 Evidence（请逐维度分析，生成对比型结论）：\n"
                + "\n\n".join(
                    f"=== {DIMENSION_LABELS.get(dim, dim)} 维度（{len(items)} 条证据）===\n"
                    + "\n".join(
                        f"[{ev['evidence_id']}] {ev['competitor'] or '?'} | {ev['source_type']} | "
                        f"置信度{ev['confidence']:.2f}\n事实：{ev['fact']}\n摘要：{ev['quote'][:120]}"
                        for ev in items[:8]
                    )
                    for dim, items in by_dim.items()
                )
            ),
        )
        rows = data.get("claims") if data else None
        if not isinstance(rows, list):
            return deterministic_claims
        evidence_ids = {ev.evidence_id for ev in evidence}
        claims: list[Claim] = []
        for row in rows[:24]:
            if not isinstance(row, dict):
                continue
            supporting = [ev_id for ev_id in coerce_str_list(row.get("supporting_evidence_ids")) if ev_id in evidence_ids]
            if not supporting:
                continue
            dimension = str(row.get("dimension") or "other")
            claim_text = str(row.get("claim") or "").strip()
            if len(claim_text) < 12:
                continue
            claim_id = make_stable_id("claim", f"{self.ctx.run_id}:{dimension}:{claim_text}:{supporting}")
            confidence = clamp_float(row.get("confidence"), 0.45, 0.95)
            claims.append(
                Claim(
                    claim_id=claim_id,
                    run_id=self.ctx.run_id,
                    dimension=dimension,
                    dimension_label=DIMENSION_LABELS.get(dimension, dimension),
                    claim=claim_text,
                    supporting_evidence_ids=supporting,
                    confidence=confidence,
                    risk_level="medium",
                    reasoning_summary=str(row.get("reasoning_summary") or "由 LLM 基于 Evidence 聚合生成。"),
                    verification_status="draft",
                    generated_by_agent=self.name,
                    generated_by_skill=self.skill_id,
                )
            )
        final_claims = merge_claims(claims, deterministic_claims)
        await self.record_llm_event(
            "progress",
            "claims_generated",
            f"基于 {len(evidence)} 条 Evidence 生成 {len(final_claims)} 条分析 Claim",
            {
                "evidence_count": len(evidence),
                "claim_count": len(final_claims),
                "llm_claim_count": len(claims),
                "deterministic_claim_count": len(deterministic_claims),
            },
        )
        return final_claims

    async def review(self, claims: list[Claim], evidence: list[Evidence]) -> list[Claim]:
        reviewed = deterministic_red_team(claims, evidence)
        if not self.llm_enabled or not reviewed:
            return reviewed

        by_id = {ev.evidence_id: ev for ev in evidence}
        payload = []
        for claim in reviewed[:60]:
            supporting_evs = [by_id[eid] for eid in claim.supporting_evidence_ids if eid in by_id]
            source_types = list({ev.source_type for ev in supporting_evs})
            source_count = len({ev.source_url for ev in supporting_evs})
            payload.append({
                "claim_id": claim.claim_id,
                "claim": claim.claim,
                "dimension": claim.dimension,
                "supporting_evidence_ids": claim.supporting_evidence_ids,
                "confidence": claim.confidence,
                "evidence_source_types": source_types,
                "unique_source_count": source_count,
                "current_notes": [note.model_dump(mode="json") for note in claim.red_team_notes],
            })

        data = await self.invoke_json(
            system=(
                "你是竞品分析审查智能体（AnalysisAndReviewAgent）的红队技能，\n"
                "专门从反方视角审查竞品分析结论的可信度和风险。\n"
                "\n"
                "【审查维度】（每条 Claim 逐一检查）\n"
                "1. 【来源单一风险】：所有证据来自同一域名/产品官方 → severity=high\n"
                "2. 【证据不足风险】：仅 1 条证据支撑强结论 → severity=medium/high\n"
                "3. 【过度推断风险】：结论超出证据直接支持的范围 → severity=medium\n"
                "4. 【时效风险】：证据可能过时（产品更新快，6个月前的数据需标注）→ severity=low\n"
                "5. 【措辞风险】：使用了'最好'、'唯一'、'绝对'等绝对化表述而证据不支撑 → severity=medium\n"
                "6. 【用户声音缺失】：官方数据充分但缺乏真实用户反馈佐证 → severity=low\n"
                "\n"
                "【验证状态标准】\n"
                "- verified：多源交叉验证，结论措辞保守，无明显风险\n"
                "- needs_evidence：证据不足，结论需要更多来源支撑\n"
                "- challenged：有明显风险，结论需要大幅修改措辞\n"
                "- rejected：结论无法被证据支撑，或存在明显事实错误\n"
                "\n"
                "【final_wording 要求】\n"
                "对 challenged/needs_evidence 的结论，必须给出修改后的保守表述：\n"
                "  - 加限定语：'根据官方公开资料...'、'据目前可见的证据...'、'部分用户反馈显示...'\n"
                "  - 降低确定性：把'是'改成'似乎是'，把'最好'改成'表现较好'\n"
                "\n"
                "【输出格式】JSON：\n"
                '{"reviews":[{\n'
                '  "claim_id": "...",\n'
                '  "verification_status": "verified|needs_evidence|challenged|rejected",\n'
                '  "final_wording": "（修改后的措辞，verified 时可留空）",\n'
                '  "notes": [{"risk_type": "...", "comment": "...", "suggested_action": "...", "severity": "low|medium|high"}]\n'
                "}]}"
            ),
            user=f"待审查 Claims（含证据元信息）：\n{payload}",
        )
        rows = data.get("reviews") if data else None
        if not isinstance(rows, list):
            return reviewed
        by_id = {claim.claim_id: claim for claim in reviewed}
        for row in rows:
            if not isinstance(row, dict):
                continue
            claim = by_id.get(str(row.get("claim_id") or ""))
            if not claim:
                continue
            notes: list[RedTeamNote] = []
            for note in row.get("notes") or []:
                if isinstance(note, dict):
                    notes.append(
                        RedTeamNote(
                            risk_type=str(note.get("risk_type") or "llm_review"),
                            comment=str(note.get("comment") or ""),
                            suggested_action=str(note.get("suggested_action") or ""),
                            severity=str(note.get("severity") or "medium"),  # type: ignore[arg-type]
                        )
                    )
            if notes:
                claim.red_team_notes = notes
            final_wording = str(row.get("final_wording") or "").strip()
            if final_wording:
                claim.final_wording = final_wording
            status = str(row.get("verification_status") or claim.verification_status)
            # 保留确定性审查的通过基线：LLM 只有给出明确风险 notes 时才降级，避免全量保守判失败。
            if claim.verification_status == "verified" and status != "verified" and not notes:
                status = "verified"
            if status in {"verified", "needs_evidence", "challenged", "rejected", "draft", "included_in_report"}:
                claim.verification_status = status  # type: ignore[assignment]
            if claim.verification_status == "verified":
                claim.red_team_notes = []
                claim.risk_level = "low"
                claim.final_wording = claim.final_wording or claim.claim
                continue
            claim.risk_level = "high" if any(note.severity == "high" for note in claim.red_team_notes) else (
                "medium" if claim.red_team_notes else "low"
            )
        await self.record_llm_event(
            "progress",
            "claims_reviewed",
            f"完成 {len(reviewed)} 条 Claim 的 Red Team 审查",
            {
                "claim_count": len(reviewed),
                "verified": len([claim for claim in reviewed if claim.verification_status == "verified"]),
                "challenged": len([claim for claim in reviewed if claim.verification_status != "verified"]),
            },
        )
        return reviewed

    async def synthesize(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        evidence: list[Evidence],
        claims: list[Claim],
        metrics: RunMetrics,
        started_at: datetime,
        *,
        matrix_builder: Callable[[list[Evidence], list[str], list[str]], Any],
        recommendations_builder: Callable[[str, ResearchRequest, list[Claim], list[Evidence], Any], list[Any]],
        battlecards_builder: Callable[[str, ResearchRequest, list[Claim], Any], list[Any]],
        graph_builder: Callable[[list[Evidence], list[Claim]], Any],
        observability_builder: Callable[..., Any],
        average_fn: Callable[[list[float]], float],
        unique_strings_fn: Callable[[list[str | None]], list[str]],
    ) -> dict[str, Any]:
        dimensions = plan.dimensions or request.analysis_dimensions or DEFAULT_DIMENSIONS
        competitors = unique_strings_fn([request.target_product, *request.competitors])
        matrix = matrix_builder(evidence, competitors, dimensions)
        recommendations = recommendations_builder(self.ctx.run_id, request, claims, evidence, matrix)
        battlecards = battlecards_builder(self.ctx.run_id, request, claims, matrix)
        evidence_graph = graph_builder(evidence, claims)
        observability = observability_builder(
            started_at=started_at,
            plan=plan,
            metrics=metrics,
            evidence=evidence,
            claims=claims,
            matrix=matrix,
            recommendations=recommendations,
            battlecards=battlecards,
        )
        return {
            "matrix": matrix,
            "recommendations": recommendations,
            "battlecards": battlecards,
            "evidence_graph": evidence_graph,
            "observability": observability,
            "average_evidence_confidence": round(average_fn([ev.confidence for ev in evidence]), 3),
        }

    async def assess_gaps(
        self,
        request: ResearchRequest,
        evidence: list[Evidence],
        claims: list[Claim],
        loop_round: int,
        coverage_score: float,
    ) -> ResearchFeedback:
        """评估当前证据缺口，决定是否需要继续搜索以及补充什么。"""
        dimensions = request.analysis_dimensions or DEFAULT_DIMENSIONS
        entities = [request.target_product, *request.competitors]

        # 计算各维度×竞品的覆盖情况
        coverage_map: dict[tuple[str, str], int] = {}
        for ev in evidence:
            if ev.competitor and ev.dimension:
                key = (ev.competitor, ev.dimension)
                coverage_map[key] = coverage_map.get(key, 0) + 1

        # 找出覆盖为 0 的空白格
        empty_cells = [
            (entity, dim)
            for entity in entities
            for dim in dimensions
            if coverage_map.get((entity, dim), 0) == 0
        ]
        weak_cells = [
            (entity, dim)
            for entity in entities
            for dim in dimensions
            if 0 < coverage_map.get((entity, dim), 0) < 2
        ]

        high_priority_weak_cells = [
            (entity, dim)
            for entity, dim in weak_cells
            if entity == request.target_product
            or dim in {"positioning", "feature", "pricing", "user_voice", "enterprise"}
        ]

        # 覆盖度足够且没有关键空白/弱覆盖时才停止，避免 50% 这种刚过线的状态过早收敛。
        if (
            coverage_score >= self.ctx.settings.cg_min_coverage_to_stop
            and not empty_cells
            and not high_priority_weak_cells
        ):
            return ResearchFeedback(
                needs_more_research=False,
                gaps=[],
                loop_round=loop_round,
                coverage_score=coverage_score,
                message=f"覆盖度 {coverage_score:.0%} 已达标，无需补充搜索。",
            )

        if not self.llm_enabled:
            # 直接用规则生成 gap
            gap_cells = empty_cells[:6]
            remaining_slots = max(0, 6 - len(gap_cells))
            gap_cells = [*gap_cells, *high_priority_weak_cells[:remaining_slots]]
            gaps = [
                ResearchGap(
                    dimension=dim,
                    competitor=entity,
                    reason=(
                        "本轮未采集到任何证据"
                        if (entity, dim) in empty_cells
                        else "当前只有少量证据，仍不足以支撑稳健结论"
                    ),
                    priority="high" if entity == request.target_product else "medium",
                    suggested_queries=[
                        f"{entity} {DIMENSION_LABELS.get(dim, dim)} 评测",
                        f"{entity} {dim} official",
                    ],
                )
                for entity, dim in gap_cells
            ]
            return ResearchFeedback(
                needs_more_research=bool(gaps),
                gaps=gaps,
                loop_round=loop_round,
                coverage_score=coverage_score,
                message=f"发现 {len(empty_cells)} 个空白格，{len(weak_cells)} 个弱覆盖格，建议补充搜索。",
            )

        # 用 LLM 生成更智能的补充建议
        payload = {
            "coverage_map": {f"{e}×{DIMENSION_LABELS.get(d, d)}": n for (e, d), n in coverage_map.items()},
            "empty_cells": [{"entity": e, "dimension": DIMENSION_LABELS.get(d, d)} for e, d in empty_cells[:12]],
            "weak_cells": [{"entity": e, "dimension": DIMENSION_LABELS.get(d, d)} for e, d in weak_cells[:8]],
            "coverage_score": f"{coverage_score:.0%}",
            "loop_round": loop_round,
        }
        data = await self.invoke_json(
            system=(
                "你是竞品分析审查智能体（AnalysisAndReviewAgent）的证据缺口评估器。\n"
                "\n"
                "【任务】\n"
                "基于当前证据覆盖矩阵，判断是否需要启动下一轮搜索，以及优先补充哪些信息缺口。\n"
                "\n"
                "【停止搜索的硬条件（设 needs_more_research=false）】\n"
                "只有同时满足以下条件时才停止：\n"
                f"  1. 覆盖度 ≥ {self.ctx.settings.cg_min_coverage_to_stop:.0%}\n"
                "  2. 没有目标产品或关键维度的空白格/弱覆盖格\n"
                "  3. 每个核心产品（目标产品 + 主要竞品）在至少 3 个维度有证据\n"
                "  4. 若包含 user_voice 维度，必须已有来自真实用户或第三方社区的内容\n"
                "  5. 若仍存在高优先级缺口，即使已到第 2 轮也继续建议补充搜索\n"
                "\n"
                "【缺口优先级判断】\n"
                "  high（必须补）：目标产品在任意维度完全没有证据\n"
                "  high（必须补）：定价维度完全没有任何产品的证据\n"
                "  high（必须补）：目标产品或关键维度只有 1 条证据，无法交叉验证\n"
                "  medium（建议补）：主要竞品在关键维度（定价/功能/用户声音）没有证据或证据较弱\n"
                "  low（可选补）：次要维度或非核心竞品证据较弱\n"
                "\n"
                "【suggested_queries 要求】\n"
                "  - 必须具体，包含产品名 + 维度关键词\n"
                "  - 用户声音类写中文口语（知乎风格）\n"
                "  - 官方/技术类写英文精确查询\n"
                "\n"
                "【输出格式】JSON：\n"
                "{\n"
                '  "needs_more_research": true/false,\n'
                '  "reason": "判断原因（2-3句，说明主要缺口是什么）",\n'
                '  "gaps": [\n'
                '    {\n'
                '      "competitor": "产品名",\n'
                '      "dimension": "维度英文key（如 pricing/feature/user_voice）",\n'
                '      "reason": "为什么这个信息现在缺失，对分析有什么影响",\n'
                '      "priority": "high/medium/low",\n'
                '      "suggested_queries": ["具体查询1", "具体查询2"]\n'
                '    }\n'
                "  ]\n"
                "}"
            ),
            user=(
                f"目标产品：{request.target_product}  竞品：{', '.join(request.competitors)}\n"
                f"当前轮次：第 {loop_round} 轮  当前覆盖度：{coverage_score:.0%}\n\n"
                f"证据覆盖矩阵（产品×维度 → 证据条数）：\n"
                + "\n".join(
                    f"  {e} × {DIMENSION_LABELS.get(d, d)}: {coverage_map.get((e, d), 0)} 条"
                    for e in [request.target_product, *request.competitors]
                    for d in request.analysis_dimensions
                )
                + f"\n\n空白格（0条证据）：{len(empty_cells)} 个\n"
                + "\n".join(f"  - {e} × {DIMENSION_LABELS.get(d, d)}" for e, d in empty_cells[:10])
                + f"\n\n弱覆盖格（1条证据）：{len(weak_cells)} 个"
            ),
        )

        if not data:
            gap_cells = empty_cells[:4]
            remaining_slots = max(0, 4 - len(gap_cells))
            gap_cells = [*gap_cells, *high_priority_weak_cells[:remaining_slots]]
            gaps = [
                ResearchGap(
                    dimension=dim,
                    competitor=entity,
                    reason=(
                        "证据为空"
                        if (entity, dim) in empty_cells
                        else "证据较少，建议补充交叉验证来源"
                    ),
                    priority="high",
                    suggested_queries=[f"{entity} {dim}"],
                )
                for entity, dim in gap_cells
            ]
            return ResearchFeedback(
                needs_more_research=bool(gaps),
                gaps=gaps,
                loop_round=loop_round,
                coverage_score=coverage_score,
            )

        needs_more = bool(data.get("needs_more_research", False))
        raw_gaps = data.get("gaps") or []
        gaps: list[ResearchGap] = []
        for row in raw_gaps[:8]:
            if not isinstance(row, dict):
                continue
            dim = str(row.get("dimension") or "other")
            if dim not in DIMENSION_LABELS:
                dim = "other"
            priority = str(row.get("priority") or "medium")
            if priority not in {"high", "medium", "low"}:
                priority = "medium"
            gaps.append(ResearchGap(
                dimension=dim,
                competitor=str(row.get("competitor") or request.target_product),
                reason=str(row.get("reason") or ""),
                priority=priority,  # type: ignore[arg-type]
                suggested_queries=coerce_str_list(row.get("suggested_queries"))[:4],
            ))

        await self.record_llm_event(
            "progress", "gap_assessment",
            f"缺口评估：需要补充={needs_more}，发现 {len(gaps)} 个缺口",
            {"needs_more": needs_more, "gap_count": len(gaps), "coverage_score": coverage_score},
        )

        return ResearchFeedback(
            needs_more_research=needs_more,
            gaps=gaps,
            loop_round=loop_round,
            coverage_score=coverage_score,
            message=str(data.get("reason") or ""),
        )


class ReportComposerAgent(BaseAgent):
    name = "ReportComposerAgent"
    skill_ids = AGENT_SKILLS[name]
    skill_id = skill_ids[0]

    async def write(
        self,
        request: ResearchRequest,
        evidence: list[Evidence],
        claims: list[Claim],
        metrics: RunMetrics,
        artifacts: dict[str, Any] | None = None,
    ) -> str:
        if not self.llm_enabled:
            return _llm_unavailable_report(request)

        # Build citation index: evidence_id -> sequential number, deduplicated by URL
        citation_map: dict[str, int] = {}
        references: list[str] = []
        seen_urls: dict[str, int] = {}
        for ev in sorted(evidence, key=lambda e: e.confidence, reverse=True):
            if ev.evidence_id in citation_map:
                continue
            url_key = ev.source_url.rstrip("/").lower()
            if url_key in seen_urls:
                citation_map[ev.evidence_id] = seen_urls[url_key]
            else:
                n = len(references) + 1
                citation_map[ev.evidence_id] = n
                seen_urls[url_key] = n
                title = ev.source_title or ev.title or ev.source_url
                references.append(f"[{n}] {title}. {ev.source_url}")
            if len(references) >= 250:
                break

        def cite(ev_ids: list[str], limit: int = 4) -> str:
            seen: list[int] = []
            for eid in ev_ids:
                n = citation_map.get(eid)
                if n and n not in seen:
                    seen.append(n)
                if len(seen) >= limit:
                    break
            return "".join(f"[{n}]" for n in seen)

        # Organize evidence by dimension -> competitor
        # Sort by combined score: 60% confidence + 40% freshness → prefer recent, high-quality evidence
        from collections import defaultdict

        def combined_score(ev: Evidence) -> float:
            return 0.6 * ev.confidence + 0.4 * ev.freshness_score

        dim_comp_ev: dict[str, dict[str, list[Evidence]]] = defaultdict(lambda: defaultdict(list))
        for ev in sorted(evidence, key=combined_score, reverse=True):
            comp = ev.competitor or "其他"
            dim_comp_ev[ev.dimension][comp].append(ev)

        dimensions = request.analysis_dimensions or list(dim_comp_ev.keys())
        all_entities = [request.target_product] + list(request.competitors)

        def fmt_fact(ev: Evidence) -> str:
            date_tag = (
                f"（{ev.published_at.strftime('%Y-%m')}）"
                if ev.published_at else ""
            )
            return f"{ev.fact}{date_tag}{cite([ev.evidence_id])}"

        evidence_context_parts: list[str] = []
        for dim in dimensions:
            dim_label = DIMENSION_LABELS.get(dim, dim)
            comp_ev = dim_comp_ev.get(dim)
            if not comp_ev:
                continue
            evidence_context_parts.append(f"### {dim_label}")
            for comp in all_entities:
                evs = comp_ev.get(comp, [])[:4]
                if evs:
                    evidence_context_parts.append(f"**{comp}**：")
                    for ev in evs:
                        evidence_context_parts.append(f"- {fmt_fact(ev)}")
            evidence_context_parts.append("")
        evidence_context = "\n".join(evidence_context_parts)

        # Distilled claims as analytical anchors (clean text + citations only)
        dim_claims: dict[str, list[Claim]] = defaultdict(list)
        for c in sorted(claims, key=lambda c: c.confidence, reverse=True):
            if c.verification_status not in ("rejected",):
                dim_claims[c.dimension].append(c)
        claims_parts: list[str] = []
        for dim in dimensions:
            label = DIMENSION_LABELS.get(dim, dim)
            cs = dim_claims.get(dim, [])[:3]
            if cs:
                claims_parts.append(f"**{label}维度核心结论：**")
                for c in cs:
                    text = (c.final_wording or c.claim)[:200]
                    claims_parts.append(f"- {text}{cite(c.supporting_evidence_ids)}")
        claims_context = "\n".join(claims_parts)

        # Build compact reference list for the report footer (injected after LLM write)
        ref_text = "\n".join(references)

        markdown = await self.invoke_text(
            system=(
                "你是一名专业的产品竞争情报分析师，正在撰写一份供产品团队、市场团队和高管阅读的竞品分析报告。\n"
                "\n"
                "【硬性禁止】报告正文中不得出现以下任何内容：\n"
                "- '维度覆盖度'、'覆盖度约X%'、'该维度共关联N条Evidence'\n"
                "- 'ev_'开头的evidence_id\n"
                "- '置信度0.XX'、'strong·0.76'等置信度分数\n"
                "- '该来源 在...相关公开资料中出现了可核验表述'这类系统内部描述\n"
                "- '相关证据集中在X等产品，说明用户会用这个维度判断工具是否值得迁移或付费'这类套话\n"
                "- 任何'X在Y维度有Z条证据'的表述\n"
                "\n"
                "【报告质量要求】\n"
                "1. 摘要要有真正的判断：谁在哪个维度领先、竞争格局如何、目标产品的核心机会在哪里\n"
                "2. 竞品对比矩阵中每个格子只写简洁的文字判断（1句话），不写分数或证据条数\n"
                "3. 每个维度分析要写对比性段落：说清楚各家的差异是什么、意味着什么、谁更适合哪类用户\n"
                "4. 给出具体的、有据可查的结论——不要写'各有优劣'、'建议持续关注'这类无信息量的话\n"
                "5. 销售对话指南要给出能直接对客户说的差异化话术，针对具体场景，避免模板化\n"
                "6. 战略建议要可操作，结合竞品格局说明为什么这么建议\n"
                "7. 每个关键判断后用方括号标注来源编号，如[1][3]，不要暴露内部ID\n"
                "8. 证据条目后附有（YYYY-MM）格式的发布月份标注；时效性敏感维度（定价、功能更新、战略动态）"
                "优先引用较新的证据，若只有旧证据可用，应在报告中注明'截至YYYY年MM月'并提示信息可能已更新\n"
                "\n"
                "【报告结构】（Markdown格式）\n"
                "# [报告标题]\n"
                "## 执行摘要\n"
                "3-5条核心结论，每条直接给出判断\n"
                "## 竞品格局概览\n"
                "简短段落描述各产品的市场定位和竞争角色\n"
                "## 竞品能力对比矩阵\n"
                "Markdown表格，每格写简洁文字判断\n"
                "## 各维度深度分析\n"
                "每个维度一节，写比较性分析段落\n"
                "## 目标产品分析：优势与机会\n"
                "## 目标产品分析：不足与风险\n"
                "## 战略建议\n"
                "## 销售对话指南\n"
                "## 参考来源\n"
                "在此处输出占位符 <<REFERENCES>>，系统会自动替换为完整的参考来源列表\n"
                "\n"
                "只输出Markdown正文，不要用代码块包裹。"
            ),
            user=(
                f"目标产品：{request.target_product}\n"
                f"竞品：{', '.join(request.competitors)}\n"
                f"研究目标：{request.research_goal}\n"
                f"项目名称：{request.project_name}\n"
                "\n"
                f"【按维度整理的公开证据（每条后面的[N]是引用编号，报告中直接使用）】\n"
                f"{evidence_context}\n"
                "\n"
                f"【分析结论摘要（可作为报告论断的参考基础，写报告时要转化为流畅的分析段落）】\n"
                f"{claims_context}"
            ),
        )
        markdown = (markdown or "").strip()
        if markdown.startswith("```"):
            markdown = markdown.strip("`").removeprefix("markdown").strip()
        if len(markdown) < 800:
            return _llm_unavailable_report(request)
        if "<<REFERENCES>>" in markdown:
            markdown = markdown.replace("<<REFERENCES>>", ref_text)
        elif "## 参考来源" not in markdown:
            markdown = markdown + "\n\n## 参考来源\n\n" + ref_text
        return markdown

    async def write_executive_summary(
        self,
        request: ResearchRequest,
        evidence: list[Evidence],
        claims: list[Claim],
        metrics: RunMetrics,
        matrix: CompetitorMatrix,
        recommendations: list[OpportunityRecommendation],
        observability: ObservabilitySnapshot,
    ) -> str:
        if not self.llm_enabled:
            return _llm_unavailable_report(request)

        context = {
            "project": request.project_name,
            "target_product": request.target_product,
            "competitors": request.competitors,
            "research_goal": request.research_goal,
            "metrics": metrics.model_dump(mode="json"),
            "coverage_by_dimension": matrix.coverage_by_dimension,
            "coverage_by_competitor": matrix.coverage_by_competitor,
            "report_confidence": observability.report_confidence,
            "top_recommendations": [
                item.model_dump(mode="json") for item in recommendations[:6]
            ],
            "claims": [
                {
                    "dimension": DIMENSION_LABELS.get(claim.dimension, claim.dimension),
                    "claim": claim.final_wording or claim.claim,
                    "confidence": claim.confidence,
                    "risk_level": claim.risk_level,
                    "verification_status": claim.verification_status,
                    "supporting_evidence_count": len(claim.supporting_evidence_ids),
                }
                for claim in sorted(claims, key=lambda item: item.confidence, reverse=True)[:24]
            ],
            "evidence_samples": [
                {
                    "dimension": item.dimension_label,
                    "competitor": item.competitor,
                    "fact": item.fact,
                    "source_type": item.source_type,
                    "confidence": item.confidence,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                }
                for item in sorted(evidence, key=lambda ev: ev.confidence, reverse=True)[:36]
            ],
        }

        markdown = await self.invoke_text(
            system=(
                "你是竞品研究报告的执行摘要写作者。请只基于输入中的证据、Claim、矩阵覆盖和建议来写，"
                "不要编造额外事实，不要暴露 ev_ ID，不要写模板化空话。"
                "输出中文 Markdown，结构为：# Summary、## Key Findings、## Recommended Moves、## Confidence Notes。"
                "Key Findings 必须是有判断的 3-5 条结论；Recommended Moves 写 2-4 条可执行建议；"
                "Confidence Notes 简要说明哪些结论较稳、哪些仍需补证。"
            ),
            user=json.dumps(context, ensure_ascii=False, default=str)[:24000],
        )
        markdown = (markdown or "").strip()
        if markdown.startswith("```"):
            markdown = markdown.strip("`").removeprefix("markdown").strip()
        return markdown or _llm_unavailable_report(request)

    async def write_methodology(
        self,
        request: ResearchRequest,
        evidence: list[Evidence],
        claims: list[Claim],
        metrics: RunMetrics,
        matrix: CompetitorMatrix,
        observability: ObservabilitySnapshot,
    ) -> str:
        if not self.llm_enabled:
            return _llm_unavailable_report(request)

        context = {
            "project": request.project_name,
            "target_product": request.target_product,
            "competitors": request.competitors,
            "analysis_dimensions": [
                DIMENSION_LABELS.get(item, item) for item in request.analysis_dimensions
            ],
            "research_goal": request.research_goal,
            "metrics": metrics.model_dump(mode="json"),
            "source_mix": observability.source_mix,
            "dimension_coverage": observability.dimension_coverage,
            "competitor_coverage": observability.competitor_coverage,
            "quality_gates": [
                gate.model_dump(mode="json") for gate in observability.quality_gates
            ],
            "claim_review": {
                "claim_pass_rate": observability.claim_pass_rate,
                "red_team_challenge_rate": observability.red_team_challenge_rate,
                "verified": metrics.verified_claim_count,
                "challenged": metrics.challenged_claim_count,
            },
            "matrix_coverage": {
                "by_dimension": matrix.coverage_by_dimension,
                "by_competitor": matrix.coverage_by_competitor,
            },
            "evidence_profile": {
                "total": len(evidence),
                "source_types": dict(Counter(item.source_type for item in evidence)),
                "dimensions": dict(Counter(item.dimension_label for item in evidence)),
            },
            "risk_examples": [
                {
                    "claim": claim.final_wording or claim.claim,
                    "risk_level": claim.risk_level,
                    "notes": [
                        note.model_dump(mode="json") for note in claim.red_team_notes[:3]
                    ],
                }
                for claim in claims
                if claim.red_team_notes
            ][:12],
        }

        markdown = await self.invoke_text(
            system=(
                "你是竞品研究方法论说明的作者。请基于输入中的真实运行数据说明本次研究如何完成，"
                "包括研究设计、来源采集、Evidence 抽取、Claim 生成与 Red Team 审查、质量门禁和局限性。"
                "不要写成产品介绍，不要暴露内部文件路径，不要把质量门禁简单逐字翻译成表格。"
                "输出中文 Markdown，结构为：# Methodology、## Research Design、## Evidence Pipeline、"
                "## Claim Review、## Quality Controls、## Known Limits。"
            ),
            user=json.dumps(context, ensure_ascii=False, default=str)[:24000],
        )
        markdown = (markdown or "").strip()
        if markdown.startswith("```"):
            markdown = markdown.strip("`").removeprefix("markdown").strip()
        return markdown or _llm_unavailable_report(request)


def estimate_coverage(
    candidates: list[SourceCandidate],
    competitors: list[str],
    dimensions: list[str],
) -> float:
    """快速估算当前来源对 竞品×维度 矩阵的覆盖比例。"""
    if not competitors or not dimensions:
        return 0.0
    covered: set[tuple[str, str]] = set()
    for c in candidates:
        text = f"{c.url} {c.title} {c.snippet}".lower()
        for entity in competitors:
            if entity.lower() in text:
                for dim in dimensions:
                    kws = dimension_search_keywords(dim)
                    if any(kw.lower() in text for kw in kws):
                        covered.add((entity, dim))
    total = len(competitors) * len(dimensions)
    return round(len(covered) / total, 3) if total else 0.0


def deterministic_plan(request: ResearchRequest) -> ResearchPlan:
    entities = [request.target_product, *request.competitors]
    dimensions = request.analysis_dimensions or DEFAULT_DIMENSIONS
    queries: list[str] = []
    source_tasks: list[SourceTask] = []
    for entity in entities:
        add_source_task(source_tasks, entity, "positioning", "official", f"{entity} official product features", ["official_website"])
        if "pricing" in dimensions:
            add_source_task(source_tasks, entity, "pricing", "pricing", f"{entity} pricing plans", ["pricing_page"])
        if "enterprise" in dimensions:
            add_source_task(source_tasks, entity, "enterprise", "enterprise", f"{entity} enterprise security", ["official_website", "docs"])
        if "user_voice" in dimensions:
            add_source_task(source_tasks, entity, "user_voice", "review", f"{entity} user reviews feedback", ["review_platform", "blog"])
        if "strategy" in dimensions:
            add_source_task(source_tasks, entity, "strategy", "news", f"{entity} product roadmap market strategy", ["blog", "changelog"])
    queries = [task.query for task in source_tasks]
    return ResearchPlan(
        research_goal=request.research_goal,
        competitors=request.competitors,
        dimensions=dimensions,
        queries=queries[:18],
        source_tasks=source_tasks[:18],
        required_agents=RESEARCH_AGENT_FLOW.copy(),
        quality_rules=[
            "所有 Evidence 必须绑定原始 URL 和原文片段",
            "每条关键 Claim 优先绑定 2 条以上 Evidence",
            "来源单一、低置信度或过度推断的 Claim 必须降级措辞",
        ],
        notes="LLM 未配置或调用失败时使用的确定性研究计划；只基于真实抓取内容生成产物。",
        planned_by="ResearchPlanningAgent:deterministic",
    )


def add_source_task(
    tasks: list[SourceTask],
    entity: str,
    dimension: str,
    intent: str,
    query: str,
    expected_source_types: list[str],
) -> None:
    tasks.append(
        SourceTask(
            task_id=f"task_{len(tasks) + 1:02d}",
            entity=entity,
            dimension=dimension,
            intent=intent,  # type: ignore[arg-type]
            query=query,
            expected_source_types=expected_source_types,
            rationale=f"Collect {intent} evidence for {entity} / {dimension}.",
        )
    )


def build_gap_source_tasks(
    request: ResearchRequest,
    plan: ResearchPlan,
    candidates: list[SourceCandidate],
    planned_tasks: list[SourceTask],
    executed_task_ids: set[str],
) -> list[SourceTask]:
    if not request.auto_discover_sources:
        return []

    deduped = dedupe_source_candidates(candidates)
    domains = [source_domain(candidate.url) for candidate in deduped]
    domain_counts = Counter(domain for domain in domains if domain)
    dominant_share = max(domain_counts.values(), default=0) / max(1, len(deduped))
    dimensions = plan.dimensions or request.analysis_dimensions or DEFAULT_DIMENSIONS
    entities = [request.target_product, *request.competitors]
    covered_pairs = infer_candidate_pairs(deduped, planned_tasks, entities, dimensions)

    gap_tasks: list[SourceTask] = []
    used_queries = {task.query.lower() for task in planned_tasks if task.task_id in executed_task_ids}

    for task in planned_tasks:
        if task.task_id in executed_task_ids:
            continue
        if (task.entity, task.dimension) not in covered_pairs:
            append_gap_task(gap_tasks, task, used_queries, "continue planned query for uncovered entity/dimension")
        if len(gap_tasks) >= 4:
            return gap_tasks

    for entity in entities:
        for dimension in dimensions:
            if (entity, dimension) in covered_pairs:
                continue
            task = make_gap_source_task(entity, dimension, len(gap_tasks) + 1)
            append_gap_task(gap_tasks, task, used_queries, "fill entity/dimension coverage gap")
            if len(gap_tasks) >= 4:
                return gap_tasks

    needs_more_sources = len(deduped) < min(request.max_sources, max(4, len(entities) * 2))
    too_concentrated = len(deduped) >= 3 and dominant_share > 0.6
    if needs_more_sources or too_concentrated:
        for entity in entities:
            task = SourceTask(
                task_id=f"gap_diversity_{len(gap_tasks) + 1:02d}",
                entity=entity,
                dimension="user_voice" if "user_voice" in dimensions else "strategy",
                intent="comparison",
                query=f"{entity} reviews comparison alternatives user feedback",
                expected_source_types=["review_platform", "blog"],
                rationale="Second pass to diversify source domains and add third-party perspective.",
            )
            append_gap_task(gap_tasks, task, used_queries, "diversify source domains")
            if len(gap_tasks) >= 4:
                return gap_tasks

    return gap_tasks


def append_gap_task(
    tasks: list[SourceTask],
    task: SourceTask,
    used_queries: set[str],
    rationale: str,
) -> None:
    query_key = task.query.lower()
    if query_key in used_queries:
        return
    used_queries.add(query_key)
    tasks.append(
        SourceTask(
            task_id=task.task_id if task.task_id.startswith("gap_") else f"gap_{task.task_id}",
            entity=task.entity,
            dimension=task.dimension,
            intent=task.intent,
            query=task.query,
            expected_source_types=task.expected_source_types,
            rationale=f"{rationale}: {task.rationale or task.query}",
        )
    )


def make_gap_source_task(entity: str, dimension: str, index: int) -> SourceTask:
    intent_by_dimension = {
        "positioning": "official",
        "feature": "docs",
        "pricing": "pricing",
        "user_voice": "review",
        "enterprise": "enterprise",
        "strategy": "news",
        "gtm": "comparison",
    }
    query_by_dimension = {
        "positioning": f"{entity} official product positioning features",
        "feature": f"{entity} docs features capabilities",
        "pricing": f"{entity} pricing plans",
        "user_voice": f"{entity} user reviews feedback",
        "enterprise": f"{entity} enterprise security admin compliance",
        "strategy": f"{entity} roadmap changelog launch market strategy",
        "gtm": f"{entity} customers case studies partners",
    }
    expected_by_intent = {
        "official": ["official_website"],
        "docs": ["docs"],
        "pricing": ["pricing_page"],
        "review": ["review_platform", "blog"],
        "enterprise": ["official_website", "docs"],
        "news": ["blog", "changelog"],
        "comparison": ["review_platform", "blog"],
    }
    intent = intent_by_dimension.get(dimension, "comparison")
    return SourceTask(
        task_id=f"gap_coverage_{index:02d}",
        entity=entity,
        dimension=dimension,
        intent=intent,  # type: ignore[arg-type]
        query=query_by_dimension.get(dimension, f"{entity} {dimension} public sources"),
        expected_source_types=expected_by_intent.get(intent, ["other"]),
        rationale="Generated after first-pass search found low coverage.",
    )


def infer_candidate_dimensions(
    candidates: list[SourceCandidate],
    planned_tasks: list[SourceTask],
    dimensions: list[str],
) -> set[str]:
    dimensions_set = set(dimensions or DEFAULT_DIMENSIONS)
    by_query = {task.query.lower(): task.dimension for task in planned_tasks}
    covered: set[str] = set()
    for candidate in candidates:
        task_dimension = by_query.get(candidate.query.lower())
        if task_dimension in dimensions_set:
            covered.add(task_dimension)
        text = f"{candidate.url} {candidate.title} {candidate.snippet} {candidate.source_type}".lower()
        for dimension in dimensions_set:
            if dimension in text or any(keyword.lower() in text for keyword in dimension_search_keywords(dimension)):
                covered.add(dimension)
    return covered


def infer_candidate_entities(candidates: list[SourceCandidate], entities: list[str]) -> set[str]:
    covered: set[str] = set()
    for candidate in candidates:
        text = f"{candidate.url} {candidate.title} {candidate.snippet} {candidate.query}".lower()
        host_key = "".join(part for part in source_domain(candidate.url).split(".") if part)
        for entity in entities:
            entity_key = "".join(ch for ch in entity.lower() if ch.isalnum())
            if entity.lower() in text or (entity_key and entity_key in host_key):
                covered.add(entity)
    return covered


def infer_candidate_pairs(
    candidates: list[SourceCandidate],
    planned_tasks: list[SourceTask],
    entities: list[str],
    dimensions: list[str],
) -> set[tuple[str, str]]:
    by_query = {task.query.lower(): (task.entity, task.dimension) for task in planned_tasks}
    pairs: set[tuple[str, str]] = set()
    dimensions_set = set(dimensions or DEFAULT_DIMENSIONS)
    for candidate in candidates:
        planned_pair = by_query.get(candidate.query.lower())
        if planned_pair and planned_pair[0] in entities and planned_pair[1] in dimensions_set:
            pairs.add(planned_pair)
            continue
        candidate_entities = infer_candidate_entities([candidate], entities)
        candidate_dimensions = infer_candidate_dimensions([candidate], planned_tasks, dimensions)
        for entity in candidate_entities:
            for dimension in candidate_dimensions:
                pairs.add((entity, dimension))
    return pairs


def candidate_pair_counts(
    candidates: list[SourceCandidate],
    planned_tasks: list[SourceTask],
    entities: list[str],
    dimensions: list[str],
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for candidate in dedupe_source_candidates(candidates):
        pairs = infer_candidate_pairs([candidate], planned_tasks, entities, dimensions)
        for pair in pairs:
            counts[pair] = counts.get(pair, 0) + 1
    return counts


def unique_tasks_by_query(tasks: list[SourceTask]) -> list[SourceTask]:
    seen: set[str] = set()
    unique: list[SourceTask] = []
    for task in tasks:
        key = task.query.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique


def provider_display_name(provider: str) -> str:
    return {
        "tavily": "Tavily",
        "exa": "Exa",
        "zhihu_inner": "知乎站内",
        "zhihu_global": "知乎全网",
        "search": "Search",
    }.get(provider, provider or "搜索引擎")


def dimension_search_keywords(dimension: str) -> list[str]:
    return {
        "positioning": ["official", "features", "product", "overview"],
        "feature": ["docs", "features", "capabilities"],
        "pricing": ["pricing", "plans", "billing"],
        "user_voice": ["review", "reviews", "feedback", "community"],
        "enterprise": ["enterprise", "security", "admin", "compliance"],
        "strategy": ["roadmap", "changelog", "launch", "market", "strategy"],
        "gtm": ["customers", "case studies", "partners"],
    }.get(dimension, [dimension])


def dedupe_source_candidates(candidates: list[SourceCandidate]) -> list[SourceCandidate]:
    seen: set[str] = set()
    deduped: list[SourceCandidate] = []
    for candidate in candidates:
        normalized = normalize_url(candidate.url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidate.url = normalized
        if not candidate.source_type or candidate.source_type == "other":
            candidate.source_type = classify_source(candidate.url, candidate.title)
        candidate.score = round(min(1.0, max(0.1, candidate.score)), 3)
        deduped.append(candidate)
    return deduped


def select_balanced_candidates(candidates: list[SourceCandidate], limit: int) -> list[SourceCandidate]:
    deduped = sorted(dedupe_source_candidates(candidates), key=lambda item: item.score, reverse=True)
    if len(deduped) <= limit:
        return deduped

    selected: list[SourceCandidate] = []
    domain_counts: Counter[str] = Counter()
    for candidate in deduped:
        domain = source_domain(candidate.url)
        if domain and domain_counts[domain] >= 2:
            continue
        selected.append(candidate)
        if domain:
            domain_counts[domain] += 1
        if len(selected) >= limit:
            return selected

    selected_urls = {candidate.url for candidate in selected}
    for candidate in deduped:
        if candidate.url in selected_urls:
            continue
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def source_domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def score_candidate_for_task(candidate: SourceCandidate, task: SourceTask, search_round: str) -> float:
    score = candidate.score or 0.5
    if candidate.source_type in task.expected_source_types:
        score += 0.08
    if task.entity and task.entity.lower() in f"{candidate.url} {candidate.title}".lower():
        score += 0.05
    if search_round == "gap":
        score += 0.03
    return round(min(1.0, max(0.1, score)), 3)


def tasks_from_queries(queries: list[str]) -> list[SourceTask]:
    return [
        SourceTask(
            task_id=f"task_{index + 1:02d}",
            entity="unknown",
            dimension="other",
            intent="official",
            query=query,
            expected_source_types=[],
            rationale="Fallback task generated from planner query.",
        )
        for index, query in enumerate(queries)
    ]


def normalize_source_tasks(value: Any, fallback: list[SourceTask]) -> list[SourceTask]:
    if not isinstance(value, list):
        return fallback
    tasks: list[SourceTask] = []
    allowed_intents = {"official", "pricing", "docs", "changelog", "review", "enterprise", "news", "comparison"}
    for row in value[:24]:
        if not isinstance(row, dict):
            continue
        query = str(row.get("query") or "").strip()
        if len(query) < 4:
            continue
        intent = str(row.get("intent") or "official").strip().lower()
        if intent not in allowed_intents:
            intent = "official"
        dimension = str(row.get("dimension") or "other").strip()
        if dimension not in DIMENSION_LABELS:
            dimension = "other"
        tasks.append(
            SourceTask(
                task_id=str(row.get("task_id") or f"task_{len(tasks) + 1:02d}"),
                entity=str(row.get("entity") or "unknown").strip() or "unknown",
                dimension=dimension,
                intent=intent,  # type: ignore[arg-type]
                query=query,
                expected_source_types=coerce_str_list(row.get("expected_source_types")),
                rationale=str(row.get("rationale") or ""),
            )
        )
    return tasks or fallback


def deterministic_red_team(claims: list[Claim], evidence: list[Evidence]) -> list[Claim]:
    by_id = {ev.evidence_id: ev for ev in evidence}
    for claim in claims:
        supporting = [by_id[ev_id] for ev_id in claim.supporting_evidence_ids if ev_id in by_id]
        source_domains = {urlparse(ev.source_url).netloc for ev in supporting}
        notes: list[RedTeamNote] = []
        if len(supporting) < 2:
            notes.append(
                RedTeamNote(
                    risk_type="insufficient_evidence",
                    comment="该结论目前少于 2 条支持证据。",
                    suggested_action="补充更多来源后再提升表达强度。",
                    severity="high",
                )
            )
        if len(source_domains) < 2:
            notes.append(
                RedTeamNote(
                    risk_type="single_source",
                    comment="支持证据主要来自单一域名，存在来源单一风险。",
                    suggested_action="补充第三方资料、文档或社区讨论来源。",
                    severity="medium",
                )
            )
        if claim.confidence < 0.65:
            notes.append(
                RedTeamNote(
                    risk_type="low_confidence",
                    comment="当前证据质量或相关性不足以支撑强断言。",
                    suggested_action="在报告中使用谨慎措辞，并标记为待验证。",
                    severity="medium",
                )
            )
        claim.red_team_notes = notes
        if notes:
            claim.verification_status = "needs_evidence"
            claim.risk_level = "high" if any(note.severity == "high" for note in notes) else "medium"
            claim.final_wording = claim.claim.replace("显示", "初步显示")
        else:
            claim.verification_status = "verified"
            claim.risk_level = "low"
            claim.final_wording = claim.claim
    return claims


def merge_claims(primary: list[Claim], fallback: list[Claim], limit: int = 80) -> list[Claim]:
    merged: list[Claim] = []
    seen: set[str] = set()
    for claim in [*primary, *fallback]:
        support_key = ",".join(sorted(claim.supporting_evidence_ids[:8]))
        text_key = " ".join(claim.claim.lower().split())[:160]
        key = f"{claim.dimension}:{support_key}:{text_key}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(claim)
        if len(merged) >= limit:
            break
    return merged


def coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def normalize_dimensions(values: list[str]) -> list[str]:
    reverse = {label: key for key, label in DIMENSION_LABELS.items()}
    normalized: list[str] = []
    for value in values:
        item = value.strip()
        if item in DIMENSION_LABELS:
            normalized.append(item)
        elif item in reverse:
            normalized.append(reverse[item])
    return normalized


def normalize_agent_flow(values: list[str]) -> list[str]:
    requested = [AGENT_ALIASES.get(value, value) for value in values]
    requested_set = set(requested)
    ordered = [agent for agent in RESEARCH_AGENT_FLOW if agent in requested_set or not values]
    for agent in RESEARCH_AGENT_FLOW:
        if agent not in ordered:
            ordered.append(agent)
    return ordered


def clamp_float(value: Any, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = low
    return round(max(low, min(high, parsed)), 3)


# 按维度的时效半衰期（天）：定价变化快，品牌定位变化慢
_FRESHNESS_HALF_LIFE: dict[str, float] = {
    "pricing":     90,    # 3 个月，旧定价基本失效
    "strategy":    120,   # 4 个月
    "feature":     180,   # 6 个月
    "enterprise":  270,   # 9 个月
    "user_voice":  365,   # 1 年
    "positioning": 730,   # 2 年，定位变化最慢
}

def compute_freshness_score(published_at: datetime | None, dimension: str) -> float:
    """
    根据发布时间和维度计算时效分数。
    - published_at 未知 → 返回 0.5（中性，不奖不罚）
    - 文章越新分数越高，按维度设定衰减半衰期
    - 公式：score = 0.85 * 0.5^(days / half_life) + 0.15，范围约 [0.15, 1.0]
    """
    if published_at is None:
        return 0.5
    now = datetime.now(timezone.utc)
    pub = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    days_old = max(0, (now - pub).days)
    half_life = _FRESHNESS_HALF_LIFE.get(dimension, 365)
    raw = 0.85 * (0.5 ** (days_old / half_life)) + 0.15
    return round(min(1.0, max(0.15, raw)), 3)


def make_stable_id(prefix: str, text: str) -> str:
    import hashlib

    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _llm_unavailable_report(request: ResearchRequest) -> str:
    return (
        f"# {request.project_name} — 报告生成失败\n\n"
        "> **当前 LLM API 无法使用，无法生成竞品分析报告。**\n\n"
        "请检查以下配置后重新运行研究：\n\n"
        "- `CG_LLM_PROVIDER` 是否正确设置（支持 `deepseek` / `qwen` / `ark`）\n"
        "- 对应的 API Key 是否已配置（`DEEPSEEK_API_KEY` / `QWEN_API_KEY` / `ARK_API_KEY`）\n"
        "- API 服务当前是否可正常访问\n\n"
        f"**研究目标：** {request.research_goal}\n\n"
        f"**目标产品：** {request.target_product}  "
        f"**竞品：** {', '.join(request.competitors)}\n"
    )


# Compatibility aliases for old run manifests and stray imports.
ChiefResearchPlannerAgent = ResearchPlanningAgent
SourceScoutAgent = SourceResearchAgent
CollectionAgent = SourceResearchAgent
EvidenceExtractionAgent = EvidenceStructuringAgent
ClaimGenerationAgent = AnalysisAndReviewAgent
RedTeamAgent = AnalysisAndReviewAgent
InsightSynthesisAgent = AnalysisAndReviewAgent
ReportWriterAgent = ReportComposerAgent
