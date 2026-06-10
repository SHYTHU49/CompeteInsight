from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _repo_root()
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from cg.orchestrator.pipeline import build_analysis_report, build_evidence_csv  # noqa: E402
from cg.repositories.base import write_text  # noqa: E402
from cg.schemas.research import (  # noqa: E402
    BattlecardItem,
    Claim,
    CompetitorMatrix,
    Evidence,
    ObservabilitySnapshot,
    OpportunityRecommendation,
    ResearchRequest,
    RunMetrics,
)
from cg.settings import get_settings  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_full_evidence(run_dir: Path) -> list[Evidence]:
    evidence: list[Evidence] = []
    for row in read_jsonl(run_dir / "evidence" / "_index.jsonl"):
        evidence_id = row.get("evidence_id")
        if not evidence_id:
            continue
        full = read_json(run_dir / "evidence" / f"{evidence_id}.json")
        evidence.append(Evidence(**full))
    return evidence


async def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate a completed run report from saved artifacts.")
    parser.add_argument("run_id")
    args = parser.parse_args()

    settings = get_settings()
    run_dir = settings.data_dir / "runs" / args.run_id
    if not run_dir.exists():
        raise SystemExit(f"Run not found: {run_dir}")

    request = ResearchRequest(**read_json(run_dir / "manifest.json"))
    status = read_json(run_dir / "status.json")
    metrics = RunMetrics(**status.get("metrics", {}))
    evidence = load_full_evidence(run_dir)
    claims = [Claim(**row) for row in read_jsonl(run_dir / "claims" / "_index.jsonl")]
    matrix = CompetitorMatrix(**read_json(run_dir / "exports" / "matrix.json"))
    recommendations = [
        OpportunityRecommendation(**row)
        for row in read_json(run_dir / "exports" / "recommendations.json")
    ]
    battlecards = [
        BattlecardItem(**row)
        for row in read_json(run_dir / "exports" / "battlecards.json")
    ]
    observability = ObservabilitySnapshot(**read_json(run_dir / "exports" / "observability.json"))

    report = build_analysis_report(
        request,
        evidence,
        claims,
        metrics,
        matrix,
        recommendations,
        battlecards,
        observability,
    )

    await write_text(run_dir / "reports" / "report.md", report)
    await write_text(
        run_dir / "exports" / "evidence_matrix.csv",
        build_evidence_csv(evidence, {ev.evidence_id: ev for ev in evidence}),
    )
    print(f"Regenerated report for {args.run_id}: {len(report)} chars")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
