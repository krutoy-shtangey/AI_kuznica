"""Transactional local regulation index; building and checking take no network access."""
import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
import shutil
from time import perf_counter
from uuid import uuid4

from filelock import FileLock
import numpy as np

from .chunker import Config, chunk_document
from .embeddings import Encoder, MODEL, MODEL_ROOT, REVISION
from .loader import load_directory


class IndexErrorLocal(Exception):
    """Safe, non-identifying error suitable for the command line."""


@dataclass
class Snapshot:
    manifest: dict
    chunks: list
    vectors: np.ndarray
    size: int


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def read_snapshot(root, pointer_path=None):
    try:
        pointer_path = pointer_path or root / "CURRENT"
        pointer = json.loads(pointer_path.read_bytes())
        generation = pointer["generation"]
        if not re.fullmatch(r"[a-f0-9]{32}", generation):
            raise ValueError
        folder = root / generation
        manifest_file = folder / "manifest.json"
        if digest(manifest_file) != pointer["sha256"]:
            raise ValueError
        manifest = json.loads(manifest_file.read_bytes())
        if manifest["version"] != 1 or manifest["generation"] != generation:
            raise ValueError
        for name in ("chunks.json", "embeddings.npy"):
            if digest(folder / name) != manifest["hashes"][name]:
                raise ValueError
        chunks = json.loads((folder / "chunks.json").read_bytes())
        vectors = np.load(folder / "embeddings.npy", allow_pickle=False)
        validate(manifest, chunks, vectors)
        size = sum((folder / file).stat().st_size for file in
                   ("embeddings.npy", "chunks.json", "manifest.json")) + pointer_path.stat().st_size
        return Snapshot(manifest, chunks, vectors, size)
    except (OSError, KeyError, TypeError, ValueError, EOFError, AttributeError) as exc:
        raise IndexErrorLocal("index_missing_or_corrupt") from exc


def validate(manifest, chunks, vectors):
    dimension = manifest["dimension"]
    config = Config(**manifest["chunking"])
    if (dimension < 1 or manifest["count"] != len(chunks)
            or vectors.shape != (len(chunks), dimension) or vectors.dtype != np.float32
            or not np.isfinite(vectors).all()
            or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4)):
        raise ValueError("Invalid vectors")
    seen = set()
    for chunk in chunks:
        if (chunk["source_id"] in seen or not chunk["text"].strip()
                or not 0 < chunk["tokens"] <= config.maximum
                or manifest["documents"][chunk["document_id"]] != chunk["document_sha256"]
                or sha256(chunk["text"].encode()).hexdigest() != chunk["checksum"]):
            raise ValueError("Invalid chunk")
        seen.add(chunk["source_id"])


def load_index(directory):
    root = Path(directory)
    if not root.is_dir():
        raise IndexErrorLocal("index_missing_or_corrupt")
    with FileLock(str(root / ".lock"), timeout=30):
        return read_snapshot(root)


def publish(root, manifest, chunks, vectors, old):
    generation = uuid4().hex
    folder = root / generation
    folder.mkdir()
    # Each generation is immutable. Readers only follow the atomically replaced pointer.
    with (folder / "embeddings.npy").open("xb") as stream:
        np.save(stream, vectors, allow_pickle=False)
        stream.flush()
        os.fsync(stream.fileno())
    write_json(folder / "chunks.json", chunks)
    manifest.update(generation=generation, hashes={
        name: digest(folder / name) for name in ("embeddings.npy", "chunks.json")})
    write_json(folder / "manifest.json", manifest)
    temp = folder / "pointer.tmp"
    write_json(temp, dict(generation=generation, sha256=digest(folder / "manifest.json")))
    size = read_snapshot(root, temp).size
    os.replace(temp, root / "CURRENT")
    if old:
        shutil.rmtree(root / old.manifest["generation"], ignore_errors=True)
    return size


def update_index(documents, directory, encoder, config=Config(), force=False, *,
                 document_ids=None, scopes=None, statuses=None):
    started = perf_counter()
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ".lock"), timeout=30):
        old = None
        if (root / "CURRENT").exists():
            try:
                old = read_snapshot(root)
            except IndexErrorLocal:
                if not force:
                    raise
        identity = dict(version=1, algorithm=1, model=encoder.model, revision=encoder.revision,
                        dimension=encoder.dimension, chunking=asdict(config))
        compatible = old and not force and all(old.manifest[k] == v for k, v in identity.items())
        known = old.manifest["documents"] if compatible else {}
        batch = load_directory(documents, known_checksums=known, document_ids=document_ids,
                               scopes=scopes, statuses=statuses)
        if batch.errors:
            raise IndexErrorLocal(f"source_read_errors={batch.errors}; update_aborted")
        checksums = {meta.document_id: meta.checksum for meta in
                     [*batch.reused, *(doc.metadata for doc in batch.documents)]}
        if len(checksums) != len(batch.reused) + len(batch.documents):
            raise IndexErrorLocal("duplicate_document_id")
        unchanged = {meta.document_id: meta for meta in batch.reused}
        metadata_changed = False
        previous_rows = defaultdict(list)
        if compatible:
            for i, chunk in enumerate(old.chunks):
                previous_rows[chunk["document_id"]].append(i)
        chunks, arrays = [], []
        split_time = embed_time = 0.0
        new = updated = reused = 0
        # Stable ordering across incremental and clean builds.
        fresh = {doc.metadata.document_id: doc for doc in batch.documents}
        for doc_id in sorted(checksums):
            if doc_id not in fresh:
                rows = previous_rows[doc_id]
                meta = unchanged[doc_id]
                for i in rows:
                    chunk = dict(old.chunks[i], document=meta.filename, department=meta.scope.department,
                                 stage=meta.scope.stage, status=meta.status.value)
                    metadata_changed |= chunk != old.chunks[i]
                    chunks.append(chunk)
                arrays.append(old.vectors[rows])
                reused += len(rows)
                continue
            now = perf_counter()
            items = chunk_document(fresh[doc_id], encoder, config)
            split_time += perf_counter() - now
            now = perf_counter()
            if items:
                arrays.append(encoder.encode([item["index_text"] for item in items]))
            embed_time += perf_counter() - now
            chunks.extend(items)
            if old and doc_id in old.manifest["documents"]:
                updated += len(items)
            else:
                new += len(items)
        if not compatible or checksums != known or metadata_changed:
            vectors = (np.concatenate(arrays) if arrays else
                       np.empty((0, encoder.dimension), dtype=np.float32))
            manifest = dict(**identity, documents=checksums, count=len(chunks),
                            updated_at=datetime.now(timezone.utc).isoformat())
            validate(manifest, chunks, vectors)
            size = publish(root, manifest, chunks, vectors, old)
        else:
            size = old.size
        sizes = [item["tokens"] for item in chunks]
        return dict(documents=len(checksums), parsed_documents=len(fresh), chunks=len(chunks),
                    mean_embedding_tokens=sum(sizes) / len(sizes) if sizes else 0,
                    max_embedding_tokens=max(sizes, default=0), chunk_seconds=split_time,
                    embedding_seconds=embed_time, build_seconds=perf_counter() - started,
                    index_bytes=size, reused_embeddings=reused, new_chunks=new,
                    updated_chunks=updated, errors=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, default=Path("data/documents"))
    parser.add_argument("--index", type=Path, default=Path("data/index"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--force", action="store_true")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--model-root", type=Path, default=MODEL_ROOT)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--target", type=int, default=320)
    parser.add_argument("--maximum", type=int, default=512)
    parser.add_argument("--overlap", type=int, default=40)
    args = parser.parse_args()
    try:
        if args.check:
            saved = load_index(args.index)
            result = dict(documents=len(saved.manifest["documents"]), chunks=len(saved.chunks),
                          dimension=saved.vectors.shape[1], index_bytes=saved.size, errors=0)
        else:
            encoder = Encoder(args.model, args.revision, args.batch_size, args.device, args.model_root)
            result = update_index(args.documents, args.index, encoder,
                                  Config(args.target, args.maximum, args.overlap), args.force)
    except Exception as exc:
        # Parser and IO exceptions can contain document names or text.
        result = dict(errors=1, error=str(exc) if isinstance(exc, IndexErrorLocal) else type(exc).__name__)
    print(json.dumps(result))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
