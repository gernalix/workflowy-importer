"""Private, fully local multilingual semantic discovery for Workflowy exports.

Makes suggestions ONLY. Cosine similarity is NOT evidence enough for publication:
a reviewer must curate exact ID pairs separately. No HTTP calls or API writes.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import os
from pathlib import Path
import re
import time

import numpy as np


_HTML = re.compile(r"<[^>]*>")
_BAD = re.compile(r"(?i)^(?:https?://|ftp://|file://|[/~]|codex://|\\|git@)")
_CHAT_MESSAGE = re.compile(r"^\s*\[\s*(?:\d{1,2}[:/]\d{1,2}|\d{4}-\d{1,2})[^]\n]{0,45}\]\s*[^:]{1,60}:")
_DATE_LABEL = re.compile(r"(?i)^(?:installata?\s+il\s+)?(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d")
_GENERIC = {
    "notes", "note", "inbox", "untitled", "task", "new node", "today",
    "prompt", "risposta", "link", "codice", "app", "test", "altro",
}


def display(raw: str) -> str:
    t = html.unescape(_HTML.sub(" ", raw or ""))
    return " ".join(t.split())


def exclude_note_generated(note: str) -> str:
    # First-pass-generated topic tags and "Correlati" are not evidence.
    lines = []
    for ln in (note or "").split("\n"):
        val = ln.strip()
        if val.startswith("Correlati:") or re.fullmatch(r"(?:#[a-z][a-z0-9_]*\s*)+", val):
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def eligible(n: dict, child_count: int) -> bool:
    raw = n.get("name") or ""
    t = display(raw)
    if not t or len(t) > 220 or len(raw) > 550:
        return False
    if (_BAD.search(t) or _CHAT_MESSAGE.search(t) or _DATE_LABEL.search(t)
            or t.casefold() in _GENERIC or t.count("http") >= 2):
        return False
    if t.count("\n") > 1 or len(re.findall(r"\w+", t)) > 40:
        return False
    if len(t) < 13 and child_count < 1 and len(t.split()) < 2:
        return False
    if (n.get("data") or {}).get("layoutMode") == "code-block":
        return False
    if sum(c.isalpha() for c in t) < max(3, len(t) // 6):
        return False
    return True


def prepare(nodes: list[dict]) -> tuple[list[dict], list[str]]:
    by = {n["id"]: n for n in nodes}
    children = collections.Counter(n.get("parent_id") for n in nodes)
    documents = []
    texts = []

    for node in nodes:
        if not eligible(node, children[node["id"]]):
            continue
        name = display(node.get("name") or "")
        # Include up to two meaningful ancestors; exclude generic headings.
        parent, path, root = node.get("parent_id"), [], node["id"]
        traversed = set()
        while parent and parent in by and parent not in traversed:
            traversed.add(parent)
            ancestor = by[parent]
            label = display(ancestor.get("name") or "")
            if label and label.casefold() not in _GENERIC and len(label) < 115:
                if len(path) < 2:
                    path.append(label)
            root = parent
            parent = ancestor.get("parent_id")
        note = display(exclude_note_generated(node.get("note") or ""))
        if note and len(note) <= 500:
            excerpt = note[:240]
        else:
            excerpt = ""
        # Current node matters far more than broad parent and incidental text.
        text = "passage: " + name
        if path:
            text += ". Context: " + " > ".join(path)
        if excerpt and excerpt.casefold() != name.casefold():
            text += ". Nota: " + excerpt
        texts.append(text)
        documents.append({
            "id": node["id"],
            "name": name,
            "root": root,
            "parent_id": node.get("parent_id"),
            "context": " > ".join(path),
            "title_len": len(name),
        })
    return documents, texts


def embed(texts: list[str], model_dir: Path, batch_size: int, threads: int) -> np.ndarray:
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=96)
    tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_dir / "model_quantized.onnx"),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    vectors = []
    started = time.monotonic()
    for start in range(0, len(texts), batch_size):
        enc = tokenizer.encode_batch(texts[start:start + batch_size])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        masks = np.array([e.attention_mask for e in enc], dtype=np.int64)
        token_types = np.array([e.type_ids for e in enc], dtype=np.int64)
        hidden = session.run(
            ["last_hidden_state"],
            {"input_ids": ids, "attention_mask": masks, "token_type_ids": token_types},
        )[0]
        weighted = hidden * masks[:, :, None]
        v = weighted.sum(1) / np.maximum(masks.sum(1)[:, None], 1)
        v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-10)
        vectors.append(v.astype(np.float32))
        if start == 0 or (start // batch_size) % 20 == 0:
            print(f"ENCODING {min(start+batch_size, len(texts))}/{len(texts)} in {time.monotonic()-started:.1f}s", flush=True)
    return np.concatenate(vectors, axis=0)


def candidates(
    docs: list[dict], vectors: np.ndarray, *, topk: int = 6,
    floor: float = 0.84, min_csls: float = 0.055,
    max_out: int = 1800
) -> list[dict]:
    roots = np.array([d["root"] for d in docs])
    selected: dict[tuple[int, int], dict] = {}
    n = len(docs)
    radius = np.zeros(n, np.float32)
    # Correct embedding hubness using cross-root local-neighbour density.
    for start in range(0, n, 128):
        scores = vectors[start:start + 128] @ vectors.T
        for ri in range(len(scores)):
            i = start + ri
            scores[ri, roots == roots[i]] = -1
            k = min(12, n)
            radius[i] = float(np.partition(scores[ri], -k)[-k:].mean())
    for start in range(0, n, 128):
        scores = vectors[start:start + 128] @ vectors.T
        for ri in range(len(scores)):
            i = start + ri
            raw = scores[ri]
            adjusted = 2 * raw - radius[i] - radius
            adjusted[roots == roots[i]] = -9
            k = min(topk, n)
            take = np.argpartition(adjusted, -k)[-k:]
            for j in take:
                j = int(j)
                sim = float(raw[j])
                csls = float(adjusted[j])
                if sim < floor or csls < min_csls:
                    continue
                a, b = min(i, j), max(i, j)
                key = (a, b)
                if key in selected:
                    continue
                x, y = docs[a], docs[b]
                selected[key] = {
                    "a": x["id"], "b": y["id"],
                    "score": round(sim, 5), "csls": round(csls, 5),
                    "title_a": x["name"][:200], "title_b": y["name"][:200],
                    "path_a": x["context"], "path_b": y["context"],
                    "root_a": x["root"], "root_b": y["root"],
                }
    return sorted(selected.values(), key=lambda p: -p["csls"])[:max_out]


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True, type=Path)
    ap.add_argument("--model-dir", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--floor", type=float, default=0.84)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--min-csls", type=float, default=0.025)
    args = ap.parse_args()

    raw = args.backup.read_bytes()
    nodes = json.loads(raw)
    docs, texts = prepare(nodes)
    print(f"PREPARED {len(docs)}/{len(nodes)} eligible nodes", flush=True)
    vectors = embed(texts, args.model_dir, args.batch_size, args.threads)
    cand = candidates(docs, vectors, topk=args.topk, floor=args.floor, min_csls=args.min_csls)
    private_json(args.output, {
        "schema": "workflowy-semantic-candidates/v2",
        "backup": str(args.backup),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "model": "Xenova/multilingual-e5-small:onnx/model_quantized.onnx",
        "eligible_nodes": len(docs), "candidate_count": len(cand),
        "pairs": cand,
    })
    print(f"CANDIDATE_REPORT {args.output} pairs={len(cand)}", flush=True)


if __name__ == "__main__":
    main()
