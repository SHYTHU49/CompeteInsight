"""File-backed Evidence repository."""

from __future__ import annotations

from pathlib import Path

from cg.repositories.base import append_jsonl, atomic_write_json, read_json, read_jsonl
from cg.schemas.research import Evidence, EvidenceSummary


class EvidenceRepository:
    def __init__(self, run_dir: Path):
        self.dir = run_dir / "evidence"
        self.index = self.dir / "_index.jsonl"
        self.dir.mkdir(parents=True, exist_ok=True)

    async def save(self, evidence: Evidence) -> None:
        await atomic_write_json(self.dir / f"{evidence.evidence_id}.json", evidence)
        await append_jsonl(self.index, self.summary(evidence))

    async def get(self, evidence_id: str) -> Evidence:
        data = await read_json(self.dir / f"{evidence_id}.json")
        if data is None:
            raise FileNotFoundError(evidence_id)
        return Evidence(**data)

    async def list_summary(self) -> list[EvidenceSummary]:
        return [EvidenceSummary(**row) for row in await read_jsonl(self.index)]

    @staticmethod
    def summary(evidence: Evidence) -> EvidenceSummary:
        return EvidenceSummary(
            evidence_id=evidence.evidence_id,
            dimension=evidence.dimension,
            dimension_label=evidence.dimension_label,
            competitor=evidence.competitor,
            fact=evidence.fact,
            quote_preview=evidence.quote[:180],
            source_title=evidence.source_title,
            source_url=evidence.source_url,
            source_type=evidence.source_type,
            confidence=evidence.confidence,
            fetched_at=evidence.fetched_at,
        )

