"""Core schemas for the first end-to-end CompeteGraph research loop."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


DEFAULT_DIMENSIONS = [
    "positioning",
    "feature",
    "pricing",
    "user_voice",
    "enterprise",
    "strategy",
]

DIMENSION_LABELS: dict[str, str] = {
    "positioning": "产品定位",
    "feature": "核心功能",
    "pricing": "定价策略",
    "user_voice": "用户口碑",
    "enterprise": "企业化能力",
    "strategy": "机会与战略",
    "gtm": "增长与渠道",
    "other": "其他",
}


class ResearchRequest(BaseModel):
    """User-facing request for a real research run."""

    project_name: str = Field(default="AI 编程助手竞品研究", min_length=1, max_length=120)
    target_product: str = Field(default="Trae", min_length=1, max_length=80)
    product_description: str = Field(default="", max_length=300)
    competitors: list[str] = Field(default_factory=lambda: ["Cursor", "GitHub Copilot", "Windsurf"])
    analysis_dimensions: list[str] = Field(default_factory=lambda: DEFAULT_DIMENSIONS.copy())
    research_goal: str = Field(
        default="分析目标产品与竞品的公开资料，形成可溯源证据、结论与报告。", max_length=1000
    )
    seed_urls: list[str] = Field(default_factory=list)
    max_sources: int = Field(default=10, ge=1, le=1000)
    max_sources_per_query: int = Field(default=3, ge=1, le=8)
    auto_discover_sources: bool = True
    max_search_rounds: int = Field(default=3, ge=1, le=8)

    @field_validator("competitors", "analysis_dimensions", mode="before")
    @classmethod
    def split_string_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("seed_urls", mode="before")
    @classmethod
    def split_urls(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.splitlines() if item.strip()]
        return value


class QuickExtractRequest(BaseModel):
    url: HttpUrl
    run_id: str | None = None
    source_type: str = "other"


class SourceCandidate(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    content: str = ""
    content_source: str = ""
    source_type: str = "other"
    query: str = ""
    score: float = Field(default=0.5, ge=0, le=1)
    source_provider: str = ""
    published_at: datetime | None = None
    date_source: str = "unknown"  # exa / tavily / zhihu_edit_time / unknown


class SourceTask(BaseModel):
    task_id: str
    entity: str
    dimension: str = "other"
    intent: Literal["official", "pricing", "docs", "changelog", "review", "enterprise", "news", "comparison"] = "official"
    query: str
    expected_source_types: list[str] = Field(default_factory=list)
    rationale: str = ""


class ResearchPlan(BaseModel):
    research_goal: str
    competitors: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    source_tasks: list[SourceTask] = Field(default_factory=list)
    required_agents: list[str] = Field(default_factory=list)
    quality_rules: list[str] = Field(default_factory=list)
    notes: str = ""
    planned_by: str = "ChiefResearchPlanner"


class SourceDocument(BaseModel):
    source_id: str
    run_id: str
    url: str
    title: str
    content: str
    excerpt: str
    source_type: str
    http_status: int | None = None
    content_hash: str
    fetched_at: datetime
    parser: str
    provider: str = ""
    query: str = ""
    content_source: str = ""
    ok: bool = True
    error: str | None = None
    published_at: datetime | None = None
    date_source: str = "unknown"


class Evidence(BaseModel):
    evidence_id: str
    run_id: str
    url: str
    title: str
    content: str
    fetched_at: datetime
    source_type: str
    dimension: str = "other"
    dimension_label: str = "其他"
    competitor: str | None = None
    fact: str
    quote: str
    source_title: str
    source_url: str
    source_id: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    authority_score: float = Field(default=0.5, ge=0, le=1)
    freshness_score: float = Field(default=0.5, ge=0, le=1)
    relevance_score: float = Field(default=0.5, ge=0, le=1)
    extracted_by_agent: str = "EvidenceStructuringAgent"
    extracted_by_skill: str = "skill.evidence_extraction.v1"
    status: Literal["draft", "verified", "rejected"] = "verified"
    published_at: datetime | None = None


class EvidenceSummary(BaseModel):
    evidence_id: str
    dimension: str
    dimension_label: str
    competitor: str | None = None
    fact: str
    quote_preview: str
    source_title: str
    source_url: str
    source_type: str
    confidence: float
    fetched_at: datetime


class RedTeamNote(BaseModel):
    risk_type: str
    comment: str
    suggested_action: str
    severity: Literal["low", "medium", "high"] = "medium"

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, v: Any) -> str:
        if v == "critical":
            return "high"
        return v


class Claim(BaseModel):
    claim_id: str
    run_id: str
    dimension: str
    dimension_label: str
    claim: str
    supporting_evidence_ids: list[str]
    counter_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    risk_level: Literal["low", "medium", "high"] = "medium"
    reasoning_summary: str
    verification_status: Literal[
        "draft",
        "verified",
        "needs_evidence",
        "challenged",
        "rejected",
        "included_in_report",
    ] = "draft"
    red_team_notes: list[RedTeamNote] = Field(default_factory=list)
    generated_by_agent: str = "AnalysisAndReviewAgent"
    generated_by_skill: str = "skill.claim_generation.v1"
    final_wording: str | None = None


class TraceEvent(BaseModel):
    event_id: str
    run_id: str
    timestamp: datetime
    node: str
    phase: Literal["start", "progress", "complete", "error"]
    status: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunMetrics(BaseModel):
    source_candidates: int = 0
    sources_fetched: int = 0
    sources_failed: int = 0
    evidence_count: int = 0
    claim_count: int = 0
    verified_claim_count: int = 0
    challenged_claim_count: int = 0
    matrix_cell_count: int = 0
    recommendation_count: int = 0
    battlecard_count: int = 0
    average_evidence_confidence: float = 0
    coverage_score: float = 0


class RunStatus(BaseModel):
    run_id: str
    project_name: str
    target_product: str
    owner: str | None = None
    status: Literal["queued", "running", "completed", "failed", "stopped"]
    current_stage: str = "queued"
    started_at: datetime
    finished_at: datetime | None = None
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    node_status: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class EvidenceLink(BaseModel):
    evidence_id: str
    source_title: str
    source_url: str
    quote_preview: str
    confidence: float = Field(default=0.5, ge=0, le=1)


class CompetitorProfile(BaseModel):
    competitor: str
    summary: str
    evidence_count: int = 0
    source_count: int = 0
    average_confidence: float = Field(default=0, ge=0, le=1)
    strongest_dimensions: list[str] = Field(default_factory=list)
    weak_or_unknown_dimensions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class MatrixCell(BaseModel):
    competitor: str
    dimension: str
    dimension_label: str
    summary: str
    evidence_count: int = 0
    confidence: float = Field(default=0, ge=0, le=1)
    source_types: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["strong", "partial", "weak", "unknown"] = "unknown"


class CompetitorMatrix(BaseModel):
    generated_at: datetime
    competitors: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    dimension_labels: dict[str, str] = Field(default_factory=dict)
    cells: list[MatrixCell] = Field(default_factory=list)
    profiles: list[CompetitorProfile] = Field(default_factory=list)
    coverage_by_competitor: dict[str, float] = Field(default_factory=dict)
    coverage_by_dimension: dict[str, float] = Field(default_factory=dict)


class OpportunityRecommendation(BaseModel):
    recommendation_id: str
    title: str
    recommendation: str
    priority: Literal["low", "medium", "high"] = "medium"
    target_audience: Literal["executive", "pm", "sales", "investor", "general"] = "pm"
    rationale: str
    expected_value: str
    based_on_claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class BattlecardItem(BaseModel):
    item_id: str
    competitor: str
    customer_scenario: str
    competitor_strength: str
    our_response: str
    talk_track: str
    objection_handler: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class QualityGate(BaseModel):
    gate_id: str
    name: str
    status: Literal["pass", "warn", "fail"] = "warn"
    score: float = Field(default=0, ge=0, le=1)
    message: str
    suggested_action: str = ""


class ObservabilitySnapshot(BaseModel):
    generated_at: datetime
    total_duration_seconds: float = 0
    agent_count: int = 0
    skill_calls: int = 0
    tool_calls: int = 0
    source_mix: dict[str, int] = Field(default_factory=dict)
    dimension_coverage: dict[str, float] = Field(default_factory=dict)
    competitor_coverage: dict[str, float] = Field(default_factory=dict)
    claim_pass_rate: float = Field(default=0, ge=0, le=1)
    red_team_challenge_rate: float = Field(default=0, ge=0, le=1)
    evidence_coverage_score: float = Field(default=0, ge=0, le=1)
    report_confidence: float = Field(default=0, ge=0, le=1)
    quality_gates: list[QualityGate] = Field(default_factory=list)
    export_files: dict[str, str] = Field(default_factory=dict)


class EvidenceGraphNode(BaseModel):
    id: str
    label: str
    node_type: Literal["claim", "evidence", "source", "competitor", "dimension"]
    score: float = Field(default=0.5, ge=0, le=1)
    meta: dict[str, Any] = Field(default_factory=dict)


class EvidenceGraphEdge(BaseModel):
    source: str
    target: str
    edge_type: str
    weight: float = Field(default=0.5, ge=0, le=1)


class EvidenceGraph(BaseModel):
    generated_at: datetime
    nodes: list[EvidenceGraphNode] = Field(default_factory=list)
    edges: list[EvidenceGraphEdge] = Field(default_factory=list)


class RunDetail(BaseModel):
    status: RunStatus
    request: ResearchRequest
    plan: ResearchPlan | None = None
    dag: dict[str, Any] | None = None
    sources: list[SourceCandidate] = Field(default_factory=list)
    documents: list[SourceDocument] = Field(default_factory=list)
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    matrix: CompetitorMatrix | None = None
    recommendations: list[OpportunityRecommendation] = Field(default_factory=list)
    battlecards: list[BattlecardItem] = Field(default_factory=list)
    observability: ObservabilitySnapshot | None = None
    evidence_graph: EvidenceGraph | None = None
    report_markdown: str = ""
    executive_summary_markdown: str = ""
    methodology_markdown: str = ""
    report_path: str | None = None


class RunStarted(BaseModel):
    run_id: str
    status: str


class QuickExtractResponse(BaseModel):
    status: RunStatus
    evidence: list[Evidence]


class ResearchGap(BaseModel):
    """Analysis Agent 反馈给 Planning Agent 的信息缺口。"""
    dimension: str
    competitor: str
    reason: str
    priority: Literal["high", "medium", "low"] = "medium"
    suggested_queries: list[str] = Field(default_factory=list)


class ResearchFeedback(BaseModel):
    """Analysis Agent 向 Planning Agent 的整体反馈。"""
    needs_more_research: bool = False
    gaps: list[ResearchGap] = Field(default_factory=list)
    loop_round: int = 1
    coverage_score: float = 0.0
    message: str = ""


class SearchMemory(BaseModel):
    """跨 Search 调用保留的发现状态，供外层补充研究继续去重。"""

    seen_urls: list[str] = Field(default_factory=list)
    seen_queries: list[str] = Field(default_factory=list)
    rejected_urls: list[str] = Field(default_factory=list)
    remaining_source_slots: int = 0
    resource_limit_reached: bool = False
    loop_round: int = 1
    feedback_message: str = ""
