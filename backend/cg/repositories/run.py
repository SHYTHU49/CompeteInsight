"""File-backed Run repository."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cg.repositories.base import atomic_write_json, read_json, read_jsonl, read_text
from cg.schemas.research import (
    BattlecardItem,
    Claim,
    CompetitorMatrix,
    EvidenceGraph,
    EvidenceSummary,
    ObservabilitySnapshot,
    OpportunityRecommendation,
    ResearchPlan,
    ResearchRequest,
    RunDetail,
    RunMetrics,
    RunStatus,
    SourceCandidate,
    SourceDocument,
    TraceEvent,
)


RUN_SUBDIRS = ["sources", "documents", "evidence", "claims", "trace", "reports", "exports"]


class RunRepository:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.runs_dir = data_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    async def create(self, request: ResearchRequest, owner: str | None = None) -> RunStatus:
        now = datetime.now(UTC)
        run_id = f"run_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        run_dir = self.run_dir(run_id)
        for subdir in RUN_SUBDIRS:
            (run_dir / subdir).mkdir(parents=True, exist_ok=True)

        status = RunStatus(
            run_id=run_id,
            project_name=request.project_name,
            target_product=request.target_product,
            owner=owner,
            status="queued",
            started_at=now,
            node_status={},
        )
        await atomic_write_json(run_dir / "manifest.json", request)
        await atomic_write_json(run_dir / "status.json", status)
        return status

    async def save_status(self, status: RunStatus) -> None:
        await atomic_write_json(self.run_dir(status.run_id) / "status.json", status)

    async def load_status(self, run_id: str) -> RunStatus:
        data = await read_json(self.run_dir(run_id) / "status.json")
        if data is None:
            raise FileNotFoundError(run_id)
        return RunStatus(**data)

    async def load_request(self, run_id: str) -> ResearchRequest:
        data = await read_json(self.run_dir(run_id) / "manifest.json")
        if data is None:
            raise FileNotFoundError(run_id)
        return ResearchRequest(**data)

    async def request_stop(self, run_id: str) -> RunStatus:
        status = await self.load_status(run_id)
        if status.status in {"queued", "running"}:
            stop_path = self.run_dir(run_id) / ".stop_requested"
            stop_path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
            status.current_stage = "Stopping"
            if "Stop requested by user." not in status.warnings:
                status.warnings.append("Stop requested by user.")
            await self.save_status(status)
        return status

    def is_stop_requested(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / ".stop_requested").exists()

    async def mark_stopped(self, run_id: str, current_status: RunStatus | None = None) -> RunStatus:
        status = current_status or await self.load_status(run_id)
        status.status = "stopped"
        status.current_stage = "Stopped"
        status.finished_at = datetime.now(UTC)
        status.error = None
        for node, node_status in list(status.node_status.items()):
            if node_status == "running":
                status.node_status[node] = "stopped"
        if "Stopped by user." not in status.warnings:
            status.warnings.append("Stopped by user.")
        await self.save_status(status)
        return status

    def is_accessible_by(self, status: RunStatus, owner: str | None) -> bool:
        return owner is None or status.owner in {None, owner}

    async def assert_access(self, run_id: str, owner: str | None) -> RunStatus:
        status = await self.load_status(run_id)
        if not self.is_accessible_by(status, owner):
            raise PermissionError(run_id)
        return status

    async def list_statuses(self, owner: str | None = None) -> list[RunStatus]:
        statuses: list[RunStatus] = []
        for path in sorted(self.runs_dir.glob("run_*/status.json"), reverse=True):
            data = await read_json(path)
            if data is not None:
                status = RunStatus(**data)
                if self.is_accessible_by(status, owner):
                    statuses.append(status)
        return statuses

    async def mark_stale_running(
        self, stale_after_seconds: int, owner: str | None = None
    ) -> list[RunStatus]:
        statuses: list[RunStatus] = []
        now = datetime.now(UTC)
        for status in await self.list_statuses(owner):
            if status.status != "running":
                statuses.append(status)
                continue

            run_dir = self.run_dir(status.run_id)
            last_event_at = self._last_trace_timestamp(run_dir / "trace" / "events.jsonl")
            last_write_at = self._last_run_write_timestamp(run_dir)
            latest_activity_at = max(
                value for value in [last_event_at, last_write_at, status.started_at] if value is not None
            )
            age_seconds = (now - latest_activity_at).total_seconds()

            if age_seconds > stale_after_seconds:
                status.status = "failed"
                status.current_stage = "Interrupted"
                status.finished_at = now
                status.error = (
                    "Background task appears interrupted or the backend was restarted. "
                    "This run is no longer progressing; please start a new research run. "
                    "Generated artifacts remain in the run directory."
                )
                if status.error not in status.warnings:
                    status.warnings.append(status.error)
                await self.save_status(status)
            statuses.append(status)
        return statuses

    def _last_trace_timestamp(self, trace_path: Path) -> datetime | None:
        if not trace_path.exists():
            return None
        try:
            last_line = ""
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    last_line = line
            if not last_line:
                return None
            timestamp = json.loads(last_line).get("timestamp")
            if not timestamp:
                return None
            return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except Exception:
            return None

    def _last_run_write_timestamp(self, run_dir: Path) -> datetime | None:
        try:
            latest = max((path.stat().st_mtime for path in run_dir.rglob("*") if path.is_file()), default=0.0)
            if latest <= 0:
                return None
            return datetime.fromtimestamp(latest, UTC)
        except Exception:
            return None

    async def detail(self, run_id: str, owner: str | None = None) -> RunDetail:
        run_dir = self.run_dir(run_id)
        status = await self.assert_access(run_id, owner)
        request = await self.load_request(run_id)
        plan_data = await read_json(run_dir / "plan.json")
        dag_data = await read_json(run_dir / "dag.json")
        source_rows = await read_jsonl(run_dir / "sources" / "_index.jsonl")
        doc_rows = await read_jsonl(run_dir / "documents" / "_index.jsonl")
        evidence_rows = await read_jsonl(run_dir / "evidence" / "_index.jsonl")
        claim_rows = await read_jsonl(run_dir / "claims" / "_index.jsonl")
        trace_rows = await read_jsonl(run_dir / "trace" / "events.jsonl")
        report = await read_text(run_dir / "reports" / "report.md")
        executive_summary = await read_text(run_dir / "reports" / "executive_summary.md")
        methodology = await read_text(run_dir / "reports" / "methodology.md")
        matrix_data = await read_json(run_dir / "exports" / "matrix.json")
        recommendations_data = await read_json(run_dir / "exports" / "recommendations.json", [])
        battlecards_data = await read_json(run_dir / "exports" / "battlecards.json", [])
        observability_data = await read_json(run_dir / "exports" / "observability.json")
        graph_data = await read_json(run_dir / "exports" / "evidence_graph.json")
        evidence_summaries: list[EvidenceSummary] = []
        for row in evidence_rows:
            full_evidence = await read_json(run_dir / "evidence" / f"{row.get('evidence_id')}.json")
            if full_evidence and full_evidence.get("quote"):
                row = {**row, "quote_preview": full_evidence["quote"]}
            evidence_summaries.append(EvidenceSummary(**row))

        return RunDetail(
            status=status,
            request=request,
            plan=ResearchPlan(**plan_data) if plan_data else None,
            dag=dag_data,
            sources=[SourceCandidate(**row) for row in source_rows],
            documents=[SourceDocument(**row) for row in doc_rows],
            evidence=evidence_summaries,
            claims=[Claim(**row) for row in claim_rows],
            trace=[TraceEvent(**row) for row in trace_rows],
            matrix=CompetitorMatrix(**matrix_data) if matrix_data else None,
            recommendations=[OpportunityRecommendation(**row) for row in recommendations_data],
            battlecards=[BattlecardItem(**row) for row in battlecards_data],
            observability=ObservabilitySnapshot(**observability_data) if observability_data else None,
            evidence_graph=EvidenceGraph(**graph_data) if graph_data else None,
            report_markdown=report,
            executive_summary_markdown=executive_summary,
            methodology_markdown=methodology,
            report_path=f"/files/runs/{run_id}/reports/report.md" if report else None,
        )

    async def set_metrics(self, run_id: str, metrics: RunMetrics) -> RunStatus:
        status = await self.load_status(run_id)
        status.metrics = metrics
        await self.save_status(status)
        return status

    async def rename_run(
        self,
        run_id: str,
        project_name: str,
        owner: str | None = None,
    ) -> RunStatus:
        run_dir = self.run_dir(run_id)
        status = await self.assert_access(run_id, owner)
        request = await self.load_request(run_id)
        status.project_name = project_name
        request.project_name = project_name
        await atomic_write_json(run_dir / "manifest.json", request)
        await self.save_status(status)
        return status

    async def delete_run(self, run_id: str, owner: str | None = None) -> None:
        import asyncio
        import shutil

        run_dir = self.run_dir(run_id)
        if not run_dir.exists():
            raise FileNotFoundError(run_id)
        await self.assert_access(run_id, owner)
        await asyncio.to_thread(shutil.rmtree, run_dir)
