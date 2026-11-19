import json
import math
import os
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import chromadb
import requests
from dotenv import load_dotenv
from pypdf import PdfReader
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
import docx
from docx.table import Table
from docx.text.paragraph import Paragraph
from sentence_transformers import SentenceTransformer

BASE_DIR = Path(__file__).resolve().parent

# Accepts a normal ".env" and also the "_env" name some download tools produce.
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "_env")

# ---------- Settings ----------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2").strip()
# 127.0.0.1 instead of "localhost": on Windows "localhost" can add ~2s per request (IPv6 lookup)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").strip().rstrip("/").replace("//localhost", "//127.0.0.1")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()

# IMPORTANT: Ollama reloads the model whenever num_ctx changes, so this one value
# is used for EVERY request (warm-up, chat and quiz generation).
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "6144"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "60m").strip()
MAX_ANSWER_TOKENS = int(os.getenv("MAX_ANSWER_TOKENS", "500"))

CHUNK_WORDS = int(os.getenv("CHUNK_WORDS", "180"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "35"))
CHAT_CHUNKS = int(os.getenv("CHAT_CHUNKS", "4"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "6500"))

# Cosine distance (0 = identical). A question counts as "about the materials" when
# the best vector match is closer than this, OR several of its keywords appear.
RELEVANCE_MAX_DISTANCE = float(os.getenv("RELEVANCE_MAX_DISTANCE", "0.70"))

# Downloads once (~90 MB), then runs offline
embedder = SentenceTransformer("all-MiniLM-L6-v2")

chroma = chromadb.PersistentClient(path=str(BASE_DIR / "chroma_db"))
# New collection name: chunks now carry unit/sequence metadata (re-upload old documents).
collection = chroma.get_or_create_collection(
    name="study_materials_v2",
    metadata={"hnsw:space": "cosine"},
)

_http = requests.Session()  # re-uses the connection to Ollama


# =====================================================
# 1. READ DOCUMENTS
# =====================================================

def _clean(text: str) -> str:
    text = re.sub(r"-\n(\w)", r"\1", text)          # re-join hyphenated line breaks
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pptx_shape_texts(shape):
    """Text from a shape, including tables and grouped shapes."""
    out = []
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for s in shape.shapes:
            out += _pptx_shape_texts(s)
        return out
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        t = shape.text_frame.text.strip()
        if t:
            out.append(t)
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))
            if cells:
                out.append(" | ".join(cells))
    return out


def extract_sections(path: Path):
    """Returns a list of (label, text) so answers can cite pages/slides."""
    ext = path.suffix.lower()
    sections = []

    if ext == ".pdf":
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ValueError("This PDF is password-protected.")
        for i, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            sections.append((f"page {i}", _clean(text)))

    elif ext == ".docx":
        d = docx.Document(str(path))
        label, buf = "document", []
        for child in d.element.body.iterchildren():
            if child.tag.endswith("}p"):
                p = Paragraph(child, d)
                text = p.text.strip()
                if not text:
                    continue
                style = (p.style.name or "") if p.style is not None else ""
                if style.lower().startswith(("heading", "title")):
                    if buf:
                        sections.append((label, "\n".join(buf)))
                        buf = []
                    label = text[:60]
                buf.append(text)
            elif child.tag.endswith("}tbl"):
                for row in Table(child, d).rows:
                    cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))
                    if cells:
                        buf.append(" | ".join(cells))
        if buf:
            sections.append((label, "\n".join(buf)))

    elif ext == ".pptx":
        prs = Presentation(str(path))
        for i, slide in enumerate(prs.slides, start=1):
            parts = []
            title = slide.shapes.title.text.strip() if slide.shapes.title is not None else ""
            if title:
                parts.append(title)
            for shape in slide.shapes:
                for t in _pptx_shape_texts(shape):
                    if t != title:
                        parts.append(t)
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append("Notes: " + notes)
            sections.append((f"slide {i}", "\n".join(parts)))

    elif ext == ".txt":
        sections.append(("text", path.read_text(encoding="utf-8", errors="ignore")))

    else:
        raise ValueError(f"Unsupported file type: {ext}")

    return [(label, text) for label, text in sections if text.strip()]


# =====================================================
# 2. UNITS / HEADINGS, CHUNKING
# =====================================================

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
_KINDS = "unit|chapter|module|lesson|week|session"
_HEADING_RE = re.compile(
    rf"^\W*({_KINDS})\s*[-:#.]?\s*(\d{{1,2}}|[ivx]{{1,4}})\b[\s:.\-–—]*(.*)$", re.I)
_QUERY_UNIT_RE = re.compile(rf"\b({_KINDS})\s*[-:#]?\s*(\d{{1,2}}|[ivx]{{1,4}})\b", re.I)


def _unit_number(token: str):
    return int(token) if token.isdigit() else ROMAN.get(token.lower())


def parse_heading(line: str):
    """'UNIT III: Prototyping' -> ('Unit 3', 'Prototyping')."""
    line = line.strip()
    if not line or len(line) > 120:
        return None
    m = _HEADING_RE.match(line)
    if not m:
        return None
    n = _unit_number(m.group(2))
    if n is None:
        return None
    return f"{m.group(1).title()} {n}", m.group(3).strip()


def chunk_text(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP):
    """Sentence-aware chunks of about `size` words with a small overlap."""
    pieces = []
    for s in re.split(r"(?<=[.!?])\s+", text):
        words = s.split()
        if len(words) > size:
            for i in range(0, len(words), max(1, size - overlap)):
                pieces.append(" ".join(words[i:i + size]))
        elif s.strip():
            pieces.append(s.strip())

    chunks, cur, n = [], [], 0
    for p in pieces:
        pw = len(p.split())
        if cur and n + pw > size:
            chunks.append(" ".join(cur))
            carry, cn = [], 0
            for q in reversed(cur):
                qn = len(q.split())
                if cn + qn > overlap:
                    break
                carry.insert(0, q)
                cn += qn
            cur, n = carry, cn
        cur.append(p)
        n += pw
    if cur:
        chunks.append(" ".join(cur))
    return [c for c in chunks if len(c.split()) >= 5]


def _search_text(text: str, meta: dict) -> str:
    """Text used for embedding and keyword search: includes file name, place and unit."""
    head = f"{meta.get('filename', '')} | {meta.get('location', '')}"
    if meta.get("unit"):
        head += f" | {meta['unit']}: {meta.get('unit_title', '')}"
    return head + "\n" + text


# =====================================================
# 3. INDEX (vector store + in-memory keyword index)
# =====================================================

_STOP = set(
    "a an the of and or to in on for with is are was were be been it this that these those as at by "
    "from how what why when which who do does did can could should would explain tell me about give "
    "please my your our i you we they he she his her their its into than then so if not no any some "
    "more most less very make create generate write list".split())


def _tokens(s: str):
    out = []
    for t in re.findall(r"[a-z0-9]+", s.lower()):
        if t in _STOP:
            continue
        out.append(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return out


_lock = threading.Lock()
_index = {"ids": [], "texts": [], "metas": [], "tf": [], "len": [], "df": Counter(),
          "avg": 1.0, "id2i": {}}


def _rebuild_index():
    global _index
    data = collection.get(include=["documents", "metadatas"])
    rows = sorted(zip(data["ids"], data["documents"], data["metadatas"]),
                  key=lambda r: (r[2].get("doc_id", ""), r[2].get("seq", 0)))
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    metas = [r[2] for r in rows]

    tf, lens, df = [], [], Counter()
    for t, m in zip(texts, metas):
        toks = _tokens(_search_text(t, m))
        c = Counter(toks)
        tf.append(c)
        lens.append(len(toks))
        df.update(c.keys())

    new = {"ids": ids, "texts": texts, "metas": metas, "tf": tf, "len": lens, "df": df,
           "avg": (sum(lens) / len(lens)) if lens else 1.0,
           "id2i": {cid: i for i, cid in enumerate(ids)}}
    with _lock:
        _index = new


def add_document(
    path: Path,
    original_name: str,
    size_kb: str,
    subject: str = "General"
):
    sections = extract_sections(path)
    if not sections:
        raise ValueError(
            "No readable text found. If this is a scanned PDF (pictures of pages), "
            "convert it to a text PDF with OCR first.")

    # Walk through the document in order and remember which Unit/Chapter each part belongs to
    segments, unit, utitle = [], "", ""
    for label, text in sections:
        cur = []
        for line in text.splitlines():
            h = parse_heading(line)
            if h:
                if cur:
                    segments.append((unit, utitle, label, " ".join(cur)))
                    cur = []
                unit, utitle = h
            if line.strip():
                cur.append(line.strip())
        if cur:
            segments.append((unit, utitle, label, " ".join(cur)))

    doc_id = uuid.uuid4().hex
    texts, metas, ids, embed_inputs = [], [], [], []

    for unit, utitle, label, body in segments:
        for chunk in chunk_text(body):
            meta = {
    "doc_id": doc_id,
    "filename": original_name,
    "location": label,
    "size": size_kb,
    "subject": subject,
    "unit": unit,
    "unit_title": utitle,
    "seq": len(ids)
}
            ids.append(f"{doc_id}_{len(ids)}")
            texts.append(chunk)
            metas.append(meta)
            embed_inputs.append(_search_text(chunk, meta))

    if not texts:
        raise ValueError("The document has too little text to index.")

    embeddings = embedder.encode(
        embed_inputs, normalize_embeddings=True, batch_size=32, show_progress_bar=False).tolist()
    collection.add(ids=ids, documents=texts, embeddings=embeddings, metadatas=metas)
    _rebuild_index()

    units = sorted({m["unit"] for m in metas if m["unit"]})
    print(f"[EduRAG] Indexed '{original_name}': {len(texts)} chunks, units found: {units or 'none'}")
    return {"id": doc_id, "name": original_name, "size": size_kb, "chunks": len(texts), "units": units, "subject": subject}


def list_documents():
    docs = {}

    for m in _index["metas"]:
        d = docs.setdefault(
            m["doc_id"],
            {
                "id": m["doc_id"],
                "name": m["filename"],
                "size": m["size"],
                "chunks": 0,
                "units": set(),
                "subject": m.get("subject", "General")
            }
        )

        d["chunks"] += 1

        if m.get("unit"):
            d["units"].add(m["unit"])

    out = []

    for d in docs.values():
        d["units"] = sorted(d["units"])
        out.append(d)

    return out


def delete_document(doc_id: str):
    collection.delete(where={"doc_id": doc_id})
    _rebuild_index()


# =====================================================
# 4. RETRIEVE (hybrid: meaning + keywords + unit filter)
# =====================================================

_OVERVIEW_RE = re.compile(
    r"summar|overview|outline|syllabus|table of contents|key points|main points|revise|revision|"
    r"what('?s| is| are) (in|covered|inside)|(my|the|uploaded) (documents?|materials?|notes|files?|slides?)|"
    r"all topics|list (the )?topics", re.I)
_GENERATE_RE = re.compile(r"generat|create|make|quiz|question|mcq|test|exam|practice", re.I)


def _query_unit(q: str):
    m = _QUERY_UNIT_RE.search(q)
    if not m:
        return None, None
    n = _unit_number(m.group(2))
    return (f"{m.group(1).title()} {n}", n) if n is not None else (None, None)


def _bm25(query_tokens, idx):
    N = len(idx["ids"])
    scores = defaultdict(float)
    k1, b = 1.5, 0.75
    for q in set(query_tokens):
        n_q = idx["df"].get(q, 0)
        if not n_q:
            continue
        idf = math.log(1 + (N - n_q + 0.5) / (n_q + 0.5))
        for i in range(N):
            f = idx["tf"][i].get(q)
            if f:
                scores[i] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * idx["len"][i] / idx["avg"]))
    return scores


def _spread(pool, n, idx):
    """Pick n chunks evenly across the pool (per document) to cover a whole unit or file."""
    groups = {}
    for i in pool:
        groups.setdefault(idx["metas"][i]["doc_id"], []).append(i)
    per = max(1, n // len(groups))
    chosen = []
    for g in groups.values():
        if len(g) <= per:
            chosen += g
        elif per == 1:
            chosen.append(g[0])
        else:
            step = (len(g) - 1) / (per - 1)
            chosen += [g[round(j * step)] for j in range(per)]
    return list(dict.fromkeys(chosen))


def _as_chunk(i, idx, distance=0.0):
    m = idx["metas"][i]
    return {"text": idx["texts"][i], "filename": m["filename"], "location": m["location"],
            "unit": m.get("unit", ""), "distance": distance}


def retrieve(question: str, k: int = 4, max_distance: float | None = None):
    """
    Returns the best chunks for a question, or [] when the materials don't cover it.
    - "unit 3" / "chapter II" in the question restricts the search to that unit
    - summaries / "generate questions on unit 3" sample the whole unit or document
    - otherwise vector search and keyword (BM25) search are combined
    """
    idx = _index
    N = len(idx["ids"])
    if N == 0:
        return []
    max_distance = RELEVANCE_MAX_DISTANCE if max_distance is None else max_distance

    unit, unit_n = _query_unit(question)
    allowed = None
    if unit:
        allowed = {i for i, m in enumerate(idx["metas"]) if m.get("unit") == unit}
        if not allowed:  # the file may call it "Module 3" while the student typed "unit 3"
            allowed = {i for i, m in enumerate(idx["metas"])
                       if m.get("unit", "").endswith(f" {unit_n}")}
        allowed = allowed or None

    overview = bool(_OVERVIEW_RE.search(question))
    wants_generation = bool(_GENERATE_RE.search(question))

    # Whole-unit / whole-document requests: cover everything instead of the top few hits
    if overview or (allowed and wants_generation):
        pool = sorted(allowed) if allowed else list(range(N))
        return [_as_chunk(i, idx) for i in _spread(pool, max(k, 6), idx)]

    # Vector search
    q_emb = embedder.encode([question], normalize_embeddings=True).tolist()
    query_args = dict(query_embeddings=q_emb, include=["distances"])
    if allowed:
        query_args["where"] = {"unit": idx["metas"][next(iter(allowed))]["unit"]} if len(
            {idx["metas"][i]["unit"] for i in allowed}) == 1 else None
        if query_args["where"] is None:
            del query_args["where"]
    n_vec = min(20, len(allowed) if allowed else N)
    res = collection.query(n_results=max(1, n_vec), **query_args)
    vec = [(idx["id2i"][cid], d) for cid, d in zip(res["ids"][0], res["distances"][0])
           if cid in idx["id2i"] and (allowed is None or idx["id2i"][cid] in allowed)]

    # Keyword search
    qt = _tokens(question)
    bm_scores = _bm25(qt, idx)
    bm = sorted(((s, i) for i, s in bm_scores.items() if allowed is None or i in allowed),
                reverse=True)[:20]

    # Is the question really about the materials?
    best_dist = min((d for _, d in vec), default=2.0)
    keyword_hit = False
    if bm:
        matched = sum(1 for t in set(qt) if t in idx["tf"][bm[0][1]])
        keyword_hit = matched >= (2 if len(set(qt)) >= 3 else 1)
    confident = bool(allowed) or best_dist <= max_distance or keyword_hit
    if not confident:
        return []

    # Reciprocal-rank fusion of both rankings
    fused = defaultdict(float)
    for r, (i, _) in enumerate(vec):
        fused[i] += 1 / (60 + r)
    for r, (_, i) in enumerate(bm):
        fused[i] += 1 / (60 + r)
    dist_of = {i: d for i, d in vec}

    ranked = sorted(fused, key=fused.get, reverse=True)[:k]
    return [_as_chunk(i, idx, dist_of.get(i, 1.0)) for i in ranked]


def _format_context(chunks, max_chars=MAX_CONTEXT_CHARS):
    parts, used, total = [], [], 0
    for c in chunks:
        where = f"{c['filename']}, {c['location']}" + (f", {c['unit']}" if c.get("unit") else "")
        block = f"[{len(used) + 1}] ({where})\n{c['text']}"
        if used and total + len(block) > max_chars:
            break
        parts.append(block)
        used.append(c)
        total += len(block)
    return "\n\n".join(parts), used


# =====================================================
# 5. LLM
# =====================================================

GROUNDED_SYSTEM = (
    "You are EduRAG AI, a friendly teaching assistant. The student's message comes with numbered "
    "excerpts from their uploaded study materials. Base your answer on those excerpts: if they ask "
    "for a summary, explanation, questions or revision notes, produce it from the excerpts. Explain "
    "clearly and simply and cite excerpts like [1] or [2]. If the excerpts only cover part of the "
    "question, answer that part, then add the rest under a line starting with 'Beyond your "
    "materials:'. Never invent facts. Be concise (under 250 words) unless asked for detail. "
    "Use Markdown (short paragraphs, lists, fenced code blocks)."
)

GENERAL_SYSTEM = (
    "You are EduRAG AI, a friendly and highly capable tutor. Help with ANY problem: school and "
    "university subjects, maths, science, programming, writing, exam preparation, study planning "
    "and more. For problems, work step by step and show the final answer clearly. Be accurate; if "
    "unsure, say so. Be concise (under 250 words) unless asked for detail. Use Markdown (short "
    "paragraphs, lists, fenced code blocks)."
)

QUIZ_SYSTEM = (
    "You are an expert exam-setter. You output ONLY valid JSON exactly as requested, with no "
    "Markdown fences and no commentary."
)

_anthropic_client = None
_ollama_model = OLLAMA_MODEL      # may be switched automatically if it is not installed
LAST_STATS = {}                   # timing of the most recent answer (see /api/diagnostics)


def _clean_history(history):
    """Anthropic needs strictly alternating roles starting with 'user'."""
    cleaned = []
    for h in history or []:
        role = "assistant" if h.get("role") == "assistant" else "user"
        content = str(h.get("content", "")).strip()
        if not content:
            continue
        if cleaned and cleaned[-1]["role"] == role:
            cleaned[-1]["content"] += "\n" + content
        else:
            cleaned.append({"role": role, "content": content})
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    if cleaned and cleaned[-1]["role"] == "user":
        cleaned.pop()  # the next message is the new user turn
    return cleaned


def _trim_history(history, keep: int = 4, max_chars: int = 500):
    return [{"role": h.get("role"), "content": str(h.get("content", ""))[:max_chars]}
            for h in (history or [])[-keep:]]


# ----- Ollama helpers -----
def _ollama_installed_models():
    """[(name, size_bytes)] of installed chat models, smallest first."""
    try:
        r = _http.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        r.raise_for_status()
        models = [(m["name"], m.get("size", 0)) for m in r.json().get("models", [])
                  if not any(x in m["name"].lower() for x in ("embed", "bge", "nomic", "minilm"))]
        return sorted(models, key=lambda m: m[1])
    except Exception:
        return []


def _pick_model(models):
    """Prefer a mid-size model (about 3-7B, 1.5-5.5 GB): fast but still capable."""
    good = [m for m in models if 1.5e9 <= m[1] <= 5.5e9]
    return (good or models)[0][0]


def _ollama_payload(system, messages, max_tokens, json_mode, stream):
    payload = {
        "model": _ollama_model,
        "stream": stream,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "messages": [{"role": "system", "content": system}] + messages,
        "options": {
            "num_predict": max_tokens,
            "num_ctx": OLLAMA_NUM_CTX,       # same everywhere => no model reloads
            "temperature": 0.2 if json_mode else 0.4,
        },
    }
    if json_mode:
        payload["format"] = "json"
    return payload


def _ollama_post(payload: dict, stream: bool):
    try:
        return _http.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600, stream=stream)
    except requests.ConnectionError:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_URL}. Start it with 'ollama serve', "
            "or set LLM_PROVIDER=anthropic in .env.")


def _ollama_error_text(resp) -> str:
    try:
        return resp.json().get("error") or resp.text
    except Exception:
        return resp.text


def _ollama_open(payload: dict, stream: bool):
    """POST to Ollama; if the model is not installed, switch to one that is."""
    global _ollama_model
    resp = _ollama_post(payload, stream)

    if resp.status_code == 404:
        resp.close()
        installed = _ollama_installed_models()
        if not installed:
            raise RuntimeError(
                f"Ollama has no models installed (wanted '{_ollama_model}'). "
                f"Run:  ollama pull {_ollama_model}")
        if _ollama_model not in [m[0] for m in installed]:
            fallback = _pick_model(installed)
            print(f"[EduRAG] Model '{_ollama_model}' is not installed. Using '{fallback}'. "
                  f"Installed: {[m[0] for m in installed]}")
            _ollama_model = fallback
            payload["model"] = fallback
            resp = _ollama_post(payload, stream)

    if resp.status_code != 200:
        text = _ollama_error_text(resp)
        resp.close()
        raise RuntimeError(f"Ollama error {resp.status_code}: {text} (model: {payload['model']})")
    return resp


def _get_anthropic():
    global _anthropic_client
    import anthropic
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _anthropic_client


def _record_stats(first_token_s, data=None):
    stats = {"model": _ollama_model if LLM_PROVIDER == "ollama" else ANTHROPIC_MODEL,
             "first_token_seconds": round(first_token_s, 2) if first_token_s is not None else None}
    if data:
        ec, ed = data.get("eval_count"), data.get("eval_duration")
        stats["prompt_tokens"] = data.get("prompt_eval_count")
        stats["answer_tokens"] = ec
        if ec and ed:
            stats["tokens_per_second"] = round(ec / (ed / 1e9), 1)
        stats["model_load_seconds"] = round((data.get("load_duration") or 0) / 1e9, 2)
    LAST_STATS.clear()
    LAST_STATS.update(stats)
    print(f"[EduRAG] {stats}")


# ----- Normal call (waits for the whole answer; used for quizzes) -----
def call_llm(user_prompt: str, system: str = GENERAL_SYSTEM, history=None,
             max_tokens: int = 1500, json_mode: bool = False) -> str:
    messages = _clean_history(history) + [{"role": "user", "content": user_prompt}]

    if LLM_PROVIDER == "anthropic":
        resp = _get_anthropic().messages.create(
            model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system, messages=messages)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    payload = _ollama_payload(system, messages, max_tokens, json_mode, stream=False)
    resp = _ollama_open(payload, stream=False)
    data = resp.json()
    _record_stats(None, data)
    return data["message"]["content"]


# ----- Streaming call (yields text while it is being written) -----
def stream_llm(user_prompt: str, system: str = GENERAL_SYSTEM, history=None,
               max_tokens: int = MAX_ANSWER_TOKENS):
    messages = _clean_history(history) + [{"role": "user", "content": user_prompt}]
    t0, first = time.time(), None

    if LLM_PROVIDER == "anthropic":
        with _get_anthropic().messages.stream(
                model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system, messages=messages) as s:
            for text in s.text_stream:
                if first is None:
                    first = time.time() - t0
                yield text
        _record_stats(first)
        return

    payload = _ollama_payload(system, messages, max_tokens, json_mode=False, stream=True)
    resp = _ollama_open(payload, stream=True)
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError(data["error"])
            piece = data.get("message", {}).get("content", "")
            if piece:
                if first is None:
                    first = time.time() - t0
                yield piece
            if data.get("done"):
                _record_stats(first, data)
                break
    finally:
        resp.close()


# =====================================================
# 6. ANSWER A QUESTION
# =====================================================

def _prepare_answer(question: str, history):
    history = _trim_history(history)

    # Short follow-ups ("why?", "explain more") need the previous question to search well
    search_q = question
    if history and len(question.split()) < 6:
        prev = next((h["content"] for h in reversed(history)
                     if h.get("role") == "user" and h.get("content")), "")
        search_q = f"{prev} {question}".strip()

    t0 = time.time()
    chunks = retrieve(search_q, k=CHAT_CHUNKS)
    print(f"[EduRAG] retrieval {1000 * (time.time() - t0):.0f} ms -> {len(chunks)} chunk(s)")

    if chunks:
        context, used = _format_context(chunks)
        prompt = f"Study material excerpts:\n{context}\n\nStudent request: {question}"
        sources = sorted({f"{c['filename']} ({c['location']})" for c in used})
        return prompt, GROUNDED_SYSTEM, "materials", sources, history

    return question, GENERAL_SYSTEM, "general", [], history


def answer_question(question: str, history=None):
    prompt, system, mode, sources, history = _prepare_answer(question, history)
    answer = call_llm(prompt, system=system, history=history, max_tokens=MAX_ANSWER_TOKENS)
    return {"answer": answer, "sources": sources, "mode": mode}


def stream_answer(question: str, history=None):
    """Yields dict events: meta (mode + sources), many delta events, then done."""
    prompt, system, mode, sources, history = _prepare_answer(question, history)
    yield {"type": "meta", "mode": mode, "sources": sources}
    for piece in stream_llm(prompt, system=system, history=history):
        yield {"type": "delta", "text": piece}
    yield {"type": "done"}


# =====================================================
# 7. DIAGNOSTICS + WARM-UP
# =====================================================

def diagnostics():
    info = {
        "provider": LLM_PROVIDER,
        "model": _ollama_model if LLM_PROVIDER == "ollama" else ANTHROPIC_MODEL,
        "num_ctx": OLLAMA_NUM_CTX,
        "documents": len(list_documents()),
        "chunks": len(_index["ids"]),
        "last_answer": dict(LAST_STATS),
    }
    if LLM_PROVIDER == "ollama":
        try:
            tags = _http.get(f"{OLLAMA_URL}/api/tags", timeout=5).json().get("models", [])
            info["installed_models"] = [
                {"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)} for m in tags]
        except Exception:
            info["ollama"] = f"NOT REACHABLE at {OLLAMA_URL}"
        try:
            running = _http.get(f"{OLLAMA_URL}/api/ps", timeout=5).json().get("models", [])
            for m in running:
                size, vram = m.get("size", 0), m.get("size_vram", 0)
                proc = ("100% GPU" if size and vram >= size else
                        "CPU only (slow)" if not vram else f"{round(100 * vram / size)}% GPU, rest CPU")
                info["running_on"] = {"model": m["name"], "processor": proc}
        except Exception:
            pass
    return info


def _warmup():
    """Load the model into memory at startup so the first question is not slow."""
    if LLM_PROVIDER != "ollama":
        return
    try:
        payload = _ollama_payload("Reply with OK.", [{"role": "user", "content": "hi"}],
                                  max_tokens=1, json_mode=False, stream=False)
        _ollama_open(payload, stream=False).close()
        print(f"[EduRAG] Model '{_ollama_model}' loaded and ready.")
    except Exception as e:
        print(f"[EduRAG] Warm-up skipped: {e}")


_rebuild_index()
threading.Thread(target=_warmup, daemon=True).start()