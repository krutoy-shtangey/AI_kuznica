"""Run a labelled regulation-only audit locally and print aggregate statistics."""
import argparse
import json
from pathlib import Path
from statistics import mean

import numpy as np

from .search import RegulationSearch


class CitationSearch(RegulationSearch):
    require_citation = True


def audit(search, cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected a nonempty list of labelled cases")
    ranks, recalls, negatives, latencies, sizes = [], [], [], [], []
    for case in cases:
        expected = case["expected_ids"]
        if not isinstance(expected, list) or not all(isinstance(i, str) for i in expected):
            raise ValueError("Expected source IDs")
        hits = search.search_documents(case["question"], top_k=5,
                                       department=case.get("department"), stage=case.get("stage"))
        predicted = [hit.source_id for hit in hits]
        if expected:
            ranks.append(next((i for i, source_id in enumerate(predicted, 1) if source_id in expected), 0))
            recalls.append(len(set(predicted).intersection(expected)) / len(set(expected)))
        else:
            negatives.append(bool(predicted))
        latencies.append(search.metrics.total_ms)
        sizes.append(search.metrics.context_chars)
    manifest = search.saved.manifest
    return {
        "cases": len(cases), "answerable": len(ranks), "unanswerable": len(negatives),
        "documents": len(manifest["documents"]), "chunks": len(search.saved.chunks),
        "unnumbered_chunks": sum(citation is None for citation in search.citations),
        "index_bytes": search.saved.size, "cold_ms": search.cold_ms,
        "hit_rate_at_5": mean(bool(rank) for rank in ranks) if ranks else None,
        "recall_at_5": mean(recalls) if recalls else None,
        "mrr": mean(1 / rank if rank else 0 for rank in ranks) if ranks else None,
        "false_positive_rate": mean(negatives) if negatives else None,
        "mean_ms": mean(latencies), "p50_ms": float(np.percentile(latencies, 50)),
        "p95_ms": float(np.percentile(latencies, 95)), "mean_context_chars": mean(sizes),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path("data/index"))
    parser.add_argument("--cases", type=Path, required=True)
    args = parser.parse_args()
    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8-sig"))
        report = audit(CitationSearch(args.index), cases)
    except Exception:
        print('{"error":"local_audit_failed"}')
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
