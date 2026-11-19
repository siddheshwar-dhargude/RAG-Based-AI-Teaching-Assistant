import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import rag

app = FastAPI(title="EduRAG AI")

# ---------- Configuration ----------

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "quizzes.db"

# Store original teacher-uploaded documents
DOCUMENTS_DIR = BASE_DIR / "uploaded_documents"
DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED = {".pdf", ".docx", ".txt", ".pptx"}
MAX_MB = 20
SESSION_DAYS = 7

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
TEACHER_CODE = os.getenv("TEACHER_CODE", "").strip()  # optional, protects teacher sign-up


def find_frontend() -> Path:
    for candidate in (BASE_DIR.parent / "Frontend", BASE_DIR / "Frontend", BASE_DIR):
        if (candidate / "index.html").exists():
            return candidate
    raise RuntimeError("Frontend folder with index.html not found next to Backend.")


FRONTEND_DIR = find_frontend()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- Database ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_database():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                salt TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS quizzes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                topic TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                questions TEXT NOT NULL,
                teacher_email TEXT NOT NULL,
                published INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quiz_attempts (
                id TEXT PRIMARY KEY,
                quiz_id TEXT NOT NULL,
                student_email TEXT NOT NULL,
                answers TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                submitted_at TEXT NOT NULL,
                FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
            );

            CREATE INDEX IF NOT EXISTS idx_attempts_quiz ON quiz_attempts(quiz_id);
            CREATE INDEX IF NOT EXISTS idx_attempts_student ON quiz_attempts(student_email);
            CREATE INDEX IF NOT EXISTS idx_quizzes_teacher ON quizzes(teacher_email);

            CREATE TABLE IF NOT EXISTS subjects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))


initialize_database()

DEFAULT_SUBJECTS = [
    "DBMS",
    "Java Programming",
    "Python Programming",
    "Computer Networks",
    "Operating Systems",
    "Software Engineering",
    "Design Thinking",
    "Data Structures",
    "Artificial Intelligence",
    "Machine Learning",
]


def seed_subjects():
    with db() as conn:
        for name in DEFAULT_SUBJECTS:
            exists = conn.execute(
                "SELECT id FROM subjects WHERE name = ?", (name,)
            ).fetchone()

            if not exists:
                conn.execute(
                    "INSERT INTO subjects (id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
                    (uuid.uuid4().hex, name, "system", now_iso()),
                )


seed_subjects()


# ---------- Auth helpers ----------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 120_000
    ).hex()


def create_session(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires),
    )
    return token


def public_user(row) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]}


def current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Please sign in.")
    token = authorization[7:].strip()
    with db() as conn:
        row = conn.execute("""
            SELECT u.id, u.name, u.email, u.role, s.expires_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = ?
        """, (token,)).fetchone()
    if not row or row["expires_at"] < now_iso():
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return public_user(row)


def teacher_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Teachers only.")
    return user


def student_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "student":
        raise HTTPException(status_code=403, detail="Students only.")
    return user


def check_teacher_code(role: str, code: str):
    if role == "teacher" and TEACHER_CODE and not hmac.compare_digest(code or "", TEACHER_CODE):
        raise HTTPException(status_code=403, detail="TEACHER_CODE: A valid teacher access code is required.")


# ---------- Request Models ----------

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "student"
    teacher_code: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str
    role: Optional[str] = None


class GoogleRequest(BaseModel):
    credential: str
    role: str = "student"
    teacher_code: str = ""


class ChatRequest(BaseModel):
    question: str
    history: list[dict] = []


class GenerateQuizRequest(BaseModel):
    topic: str
    number_of_questions: int = Field(default=5, ge=1, le=20)
    difficulty: str = "Medium"
    source: str = "materials"


class CreateQuizRequest(BaseModel):
    title: str
    topic: str
    difficulty: str = "Medium"
    questions: list[dict]
    published: bool = True


class PublishRequest(BaseModel):
    published: bool


class SubmitQuizRequest(BaseModel):
    answers: dict


class SubjectRequest(BaseModel):
    name: str


# ---------- Health / config ----------

@app.get("/api/health")
def health():
    return {"status": "ok", "llm": rag.LLM_PROVIDER}


@app.get("/api/config")
def config():
    return {
        "google_client_id": GOOGLE_CLIENT_ID,
        "teacher_code_required": bool(TEACHER_CODE),
    }


# ==================================================
#                 AUTHENTICATION
# ==================================================

@app.post("/api/auth/register")
def register(req: RegisterRequest):
    name = req.name.strip()
    email = req.email.strip().lower()

    if not name:
        raise HTTPException(400, "Please enter your full name.")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address.")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must contain at least 8 characters.")
    if req.role not in ("student", "teacher"):
        raise HTTPException(400, "Role must be student or teacher.")
    check_teacher_code(req.role, req.teacher_code)

    salt = secrets.token_hex(16)
    user_id = uuid.uuid4().hex

    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise HTTPException(409, "An account with this email already exists.")
        conn.execute(
            "INSERT INTO users (id, name, email, role, salt, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, email, req.role, salt, hash_password(req.password, salt), now_iso()),
        )
        token = create_session(conn, user_id)

    return {"token": token, "user": {"id": user_id, "name": name, "email": email, "role": req.role}}


@app.post("/api/auth/login")
def login(req: LoginRequest):
    email = req.email.strip().lower()

    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if not user or not user["password_hash"]:
            raise HTTPException(401, "Incorrect email or password.")

        expected = hash_password(req.password, user["salt"])
        if not hmac.compare_digest(expected, user["password_hash"]):
            raise HTTPException(401, "Incorrect email or password.")

        if req.role and req.role != user["role"]:
            raise HTTPException(
                403, f"This is a {user['role'].title()} account. Please select the {user['role'].title()} role."
            )

        token = create_session(conn, user["id"])

    return {"token": token, "user": public_user(user)}


@app.post("/api/auth/google")
def google_login(req: GoogleRequest):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(400, "Google Sign-In is not configured on the server.")

    try:
        r = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": req.credential},
            timeout=10,
        )
        info = r.json()
    except Exception:
        raise HTTPException(502, "Could not verify the Google token.")

    if r.status_code != 200 or info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "Invalid Google token.")
    if str(info.get("email_verified")).lower() != "true":
        raise HTTPException(401, "Google email is not verified.")

    email = info["email"].strip().lower()
    name = info.get("name") or email.split("@")[0]

    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if user:
            user_id = user["id"]
            result = public_user(user)
        else:
            if req.role not in ("student", "teacher"):
                raise HTTPException(400, "Role must be student or teacher.")
            check_teacher_code(req.role, req.teacher_code)
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users (id, name, email, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, email, req.role, now_iso()),
            )
            result = {"id": user_id, "name": name, "email": email, "role": req.role}

        token = create_session(conn, user_id)

    return {"token": token, "user": result}


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return user


@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    if authorization and authorization.lower().startswith("bearer "):
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (authorization[7:].strip(),))
    return {"ok": True}


# ==================================================
#                 DOCUMENTS (RAG)
# ==================================================

@app.get("/api/documents")
def documents(user: dict = Depends(current_user)):
    return rag.list_documents()


@app.get("/api/documents/{doc_id}/file")
def view_document(doc_id: str, user: dict = Depends(current_user)):
    # Validate document ID
    if not re.fullmatch(r"[a-fA-F0-9]{32}", doc_id):
        raise HTTPException(status_code=400, detail="Invalid document ID.")

    # Confirm that the document exists in the RAG index
    document = next((d for d in rag.list_documents() if d["id"] == doc_id), None)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")

    # Find the saved original document
    matches = list(DOCUMENTS_DIR.glob(f"{doc_id}.*"))
    if not matches:
        raise HTTPException(
            status_code=404,
            detail="Original file is unavailable. Ask your teacher to upload it again.",
        )

    file_path = matches[0]
    filename = Path(document["name"]).name
    ext = file_path.suffix.lower()

    mime_types = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain",
    }

    media_type = mime_types.get(ext, "application/octet-stream")
    disposition = "inline" if ext == ".pdf" else "attachment"

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=filename,
        content_disposition_type=disposition,
    )


@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: str, user: dict = Depends(teacher_only)):
    rag.delete_document(doc_id)

    # Also remove the original uploaded file
    if re.fullmatch(r"[a-fA-F0-9]{32}", doc_id):
        for file_path in DOCUMENTS_DIR.glob(f"{doc_id}.*"):
            file_path.unlink(missing_ok=True)

    return {"deleted": doc_id}


# ---------- Subjects ----------

@app.get("/api/subjects")
def get_subjects(user: dict = Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM subjects ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/subjects")
def create_subject(req: SubjectRequest, user: dict = Depends(teacher_only)):
    name = req.name.strip()

    if not name:
        raise HTTPException(400, "Please enter a subject name.")
    if len(name) > 60:
        raise HTTPException(400, "Subject name must be under 60 characters.")

    with db() as conn:
        existing = conn.execute(
            "SELECT id, name FROM subjects WHERE name = ?", (name,)
        ).fetchone()

        if existing:
            return dict(existing)

        subject_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO subjects (id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (subject_id, name, user["email"], now_iso()),
        )

    return {"id": subject_id, "name": name}


# ---------- Upload ----------
# NOTE: `subject` must be declared with Form(...) because the frontend sends it
# inside multipart form data (a plain `subject: str = ""` would be read from the
# query string and always arrive empty).

@app.post("/api/upload")
def upload(
    files: list[UploadFile] = File(...),
    subject: str = Form(""),
    user: dict = Depends(teacher_only),
):
    results, errors = [], []

    subject = subject.strip()
    if not subject:
        raise HTTPException(400, "Please select a subject.")

    with db() as conn:
        subject_row = conn.execute(
            "SELECT name FROM subjects WHERE id = ?", (subject,)
        ).fetchone()

    if not subject_row:
        raise HTTPException(404, "Selected subject was not found.")

    subject_name = subject_row["name"]

    for f in files:
        ext = Path(f.filename or "").suffix.lower()

        if ext not in ALLOWED:
            errors.append(f"{f.filename}: unsupported type")
            continue

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            shutil.copyfileobj(f.file, tmp)
            tmp_path = Path(tmp.name)

        try:
            size_bytes = tmp_path.stat().st_size

            if size_bytes / (1024 * 1024) > MAX_MB:
                errors.append(f"{f.filename}: larger than {MAX_MB} MB")
                continue

            result = rag.add_document(
                tmp_path,
                f.filename,
                f"{size_bytes / 1024:.2f} KB",
                subject=subject_name,
            )

            doc_id = result["id"]
            saved_path = DOCUMENTS_DIR / f"{doc_id}{ext}"
            shutil.copy2(tmp_path, saved_path)

            results.append(result)

        except Exception as e:
            errors.append(f"{f.filename}: {e}")

        finally:
            tmp_path.unlink(missing_ok=True)

    return {"uploaded": results, "errors": errors}


# ==================================================
#                    AI CHAT
# ==================================================

@app.post("/api/chat")
def chat(req: ChatRequest, user: dict = Depends(current_user)):
    question = req.question.strip()

    if not question:
        raise HTTPException(400, "Question is empty.")
    if len(question) > 4000:
        raise HTTPException(400, "Question is too long (max 4000 characters).")

    history = [
        {"role": h.get("role"), "content": str(h.get("content", ""))[:2000]}
        for h in req.history[-6:]
        if isinstance(h, dict)
    ]

    try:
        return rag.answer_question(question, history)
    except Exception as e:
        raise HTTPException(500, f"AI error: {e}")


# ==================================================
#                  QUIZ HELPERS
# ==================================================

def extract_json(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("The AI did not return JSON.")
    return json.loads(raw[start:end + 1])


def clean_questions(items) -> list[dict]:
    """Keeps only well-formed 4-option questions with a valid correct answer."""
    out = []
    for q in items or []:
        if not isinstance(q, dict) or not isinstance(q.get("options"), list):
            continue
        text = str(q.get("question", "")).strip()
        options = [str(o).strip() for o in q["options"] if str(o).strip()]
        correct = str(q.get("correct_answer", "")).strip()

        if not text or len(options) != 4 or len(set(options)) != 4:
            continue

        if correct not in options:
            if len(correct) == 1 and correct.upper() in "ABCD":
                correct = options["ABCD".index(correct.upper())]
            else:
                match = [o for o in options if o.lower() == correct.lower()]
                if not match:
                    continue
                correct = match[0]

        out.append({
            "question": text,
            "options": options,
            "correct_answer": correct,
            "explanation": str(q.get("explanation", "")).strip(),
        })
    return out


def pct(score, total) -> float:
    return round(100 * score / total, 1) if total else 0.0


# ==================================================
#               QUIZ: TEACHER ENDPOINTS
# ==================================================

@app.post("/api/quizzes/generate")
def generate_quiz(req: GenerateQuizRequest, user: dict = Depends(teacher_only)):
    topic = req.topic.strip()

    if not topic:
        raise HTTPException(400, "Please enter a quiz topic.")
    if req.difficulty not in ("Easy", "Medium", "Hard"):
        raise HTTPException(400, "Difficulty must be Easy, Medium or Hard.")

    if req.source == "materials":
        chunks = rag.retrieve(topic, k=8, max_distance=0.8)
        if not chunks:
            raise HTTPException(
                400,
                "No relevant study material found for this topic. Upload a document "
                "first, or switch the source to 'Topic only'.",
            )
        context = "\n\n".join(
            f"Source: {c['filename']} ({c['location']})\n{c['text']}" for c in chunks
        )
        grounding = "Base every question strictly on the study material above."
    elif req.source == "topic":
        context = "(No study material supplied.)"
        grounding = "Use well-established, accurate knowledge about the topic."
    else:
        raise HTTPException(400, "Invalid quiz source.")

    n = req.number_of_questions
    prompt = f"""Create an educational multiple-choice quiz.

Topic: {topic}
Difficulty: {req.difficulty}
Number of questions: {n}

Study material:
{context}

Return ONLY valid JSON in exactly this structure:
{{
  "title": "Quiz title",
  "questions": [
    {{
      "question": "Question text",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct_answer": "Option A",
      "explanation": "Why this answer is correct"
    }}
  ]
}}

Rules:
1. Generate exactly {n} questions.
2. Every question has exactly four different options.
3. Exactly one option is correct and correct_answer is copied exactly from options.
4. Vary which position holds the correct answer.
5. Include a short, useful explanation.
6. {grounding}
7. Output nothing outside the JSON."""

    last_error = "unknown error"
    for _ in range(2):  # one retry for malformed model output
        try:
            raw = rag.call_llm(
                prompt, system=rag.QUIZ_SYSTEM, max_tokens=900 + 450 * n, json_mode=True
            )
            data = extract_json(raw)
            questions = clean_questions(data.get("questions"))
            if len(questions) >= n:
                return {
                    "title": str(data.get("title") or f"{topic} Quiz").strip(),
                    "topic": topic,
                    "difficulty": req.difficulty,
                    "questions": questions[:n],
                }
            last_error = f"AI produced only {len(questions)} valid question(s) out of {n}."
        except Exception as e:
            last_error = str(e)

    raise HTTPException(500, f"Quiz generation failed: {last_error} Please try again.")


@app.post("/api/quizzes")
def create_quiz(req: CreateQuizRequest, user: dict = Depends(teacher_only)):
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "Quiz title is required.")
    if not req.questions:
        raise HTTPException(400, "Quiz must contain questions.")

    questions = clean_questions(req.questions)
    if len(questions) != len(req.questions):
        raise HTTPException(
            400, "Some questions are invalid. Each needs 4 different options and one correct answer."
        )
    if req.difficulty not in ("Easy", "Medium", "Hard"):
        raise HTTPException(400, "Difficulty must be Easy, Medium or Hard.")

    quiz_id = uuid.uuid4().hex

    with db() as conn:
        conn.execute("""
            INSERT INTO quizzes (id, title, topic, difficulty, questions,
                                 teacher_email, published, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (quiz_id, title, req.topic.strip() or title, req.difficulty,
              json.dumps(questions), user["email"], int(req.published), now_iso()))

    return {
        "message": "Quiz published successfully." if req.published else "Quiz saved as draft.",
        "quiz_id": quiz_id,
    }


@app.get("/api/teacher/quizzes")
def teacher_quizzes(user: dict = Depends(teacher_only)):
    with db() as conn:
        rows = conn.execute("""
            SELECT q.id, q.title, q.topic, q.difficulty, q.published, q.created_at,
                   q.questions, COUNT(a.id) AS attempts,
                   AVG(100.0 * a.score / NULLIF(a.total, 0)) AS avg_pct
            FROM quizzes q
            LEFT JOIN quiz_attempts a ON a.quiz_id = q.id
            WHERE q.teacher_email = ?
            GROUP BY q.id
            ORDER BY q.created_at DESC
        """, (user["email"],)).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["question_count"] = len(json.loads(d.pop("questions")))
        d["avg_pct"] = round(d["avg_pct"], 1) if d["avg_pct"] is not None else None
        d["published"] = bool(d["published"])
        out.append(d)
    return out


def own_quiz_or_404(conn, quiz_id: str, user: dict):
    row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Quiz not found.")
    if row["teacher_email"] != user["email"]:
        raise HTTPException(403, "You cannot access this quiz.")
    return row


@app.patch("/api/quizzes/{quiz_id}/publish")
def set_published(quiz_id: str, req: PublishRequest, user: dict = Depends(teacher_only)):
    with db() as conn:
        own_quiz_or_404(conn, quiz_id, user)
        conn.execute("UPDATE quizzes SET published = ? WHERE id = ?", (int(req.published), quiz_id))
    return {"quiz_id": quiz_id, "published": req.published}


@app.delete("/api/quizzes/{quiz_id}")
def delete_quiz(quiz_id: str, user: dict = Depends(teacher_only)):
    with db() as conn:
        own_quiz_or_404(conn, quiz_id, user)
        conn.execute("DELETE FROM quiz_attempts WHERE quiz_id = ?", (quiz_id,))
        conn.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
    return {"deleted": quiz_id}


@app.get("/api/quizzes/{quiz_id}/results")
def quiz_results(quiz_id: str, user: dict = Depends(teacher_only)):
    with db() as conn:
        quiz = own_quiz_or_404(conn, quiz_id, user)
        rows = conn.execute("""
            SELECT a.student_email, COALESCE(u.name, a.student_email) AS student_name,
                   a.answers, a.score, a.total, a.submitted_at
            FROM quiz_attempts a
            LEFT JOIN users u ON u.email = a.student_email
            WHERE a.quiz_id = ?
            ORDER BY a.submitted_at DESC
        """, (quiz_id,)).fetchall()

    questions = json.loads(quiz["questions"])
    stats = [{"question": q["question"], "correct": 0} for q in questions]
    attempts = []

    for r in rows:
        answers = json.loads(r["answers"])
        for i, q in enumerate(questions):
            if answers.get(str(i)) == q["correct_answer"]:
                stats[i]["correct"] += 1
        attempts.append({
            "student_email": r["student_email"],
            "student_name": r["student_name"],
            "score": r["score"],
            "total": r["total"],
            "percentage": pct(r["score"], r["total"]),
            "submitted_at": r["submitted_at"],
        })

    n = len(attempts)
    return {
        "quiz": {"id": quiz["id"], "title": quiz["title"], "topic": quiz["topic"],
                 "difficulty": quiz["difficulty"], "published": bool(quiz["published"])},
        "attempts": attempts,
        "average_pct": round(sum(a["percentage"] for a in attempts) / n, 1) if n else None,
        "question_stats": [
            {"question": s["question"], "correct_pct": round(100 * s["correct"] / n, 1) if n else None}
            for s in stats
        ],
    }


# ==================================================
#               QUIZ: STUDENT ENDPOINTS
# ==================================================

@app.get("/api/quizzes")
def list_published_quizzes(user: dict = Depends(current_user)):
    with db() as conn:
        rows = conn.execute("""
            SELECT q.id, q.title, q.topic, q.difficulty, q.created_at, q.questions,
                   COALESCE(u.name, q.teacher_email) AS teacher_name
            FROM quizzes q
            LEFT JOIN users u ON u.email = q.teacher_email
            WHERE q.published = 1
            ORDER BY q.created_at DESC
        """).fetchall()

        attempts = conn.execute(
            "SELECT quiz_id, score, total FROM quiz_attempts WHERE student_email = ?",
            (user["email"],),
        ).fetchall()

    mine = {}
    for a in attempts:
        m = mine.setdefault(a["quiz_id"], {"attempts": 0, "best_pct": 0.0})
        m["attempts"] += 1
        m["best_pct"] = max(m["best_pct"], pct(a["score"], a["total"]))

    out = []
    for r in rows:
        d = dict(r)
        d["question_count"] = len(json.loads(d.pop("questions")))
        m = mine.get(d["id"])
        d["attempts"] = m["attempts"] if m else 0
        d["best_pct"] = m["best_pct"] if m else None
        out.append(d)
    return out


@app.get("/api/quizzes/{quiz_id}")
def get_quiz(quiz_id: str, user: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("""
            SELECT id, title, topic, difficulty, questions, teacher_email, published
            FROM quizzes WHERE id = ?
        """, (quiz_id,)).fetchone()

    is_owner = row and user["role"] == "teacher" and row["teacher_email"] == user["email"]
    if not row or (not row["published"] and not is_owner):
        raise HTTPException(404, "Quiz not found.")

    questions = json.loads(row["questions"])

    # Students never receive answers before submitting; the owning teacher can preview them.
    shown = [
        q if is_owner else {"question": q["question"], "options": q["options"]}
        for q in questions
    ]

    return {
        "id": row["id"], "title": row["title"], "topic": row["topic"],
        "difficulty": row["difficulty"], "questions": shown,
    }


@app.post("/api/quizzes/{quiz_id}/submit")
def submit_quiz(quiz_id: str, req: SubmitQuizRequest, user: dict = Depends(student_only)):
    with db() as conn:
        row = conn.execute(
            "SELECT questions FROM quizzes WHERE id = ? AND published = 1", (quiz_id,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "Quiz not found.")

        questions = json.loads(row["questions"])
        score, review = 0, []

        for index, question in enumerate(questions):
            answer = req.answers.get(str(index))
            correct = question["correct_answer"]
            is_correct = answer == correct
            score += is_correct
            review.append({
                "question": question["question"],
                "options": question["options"],
                "your_answer": answer,
                "correct_answer": correct,
                "is_correct": is_correct,
                "explanation": question.get("explanation", ""),
            })

        attempt_id = uuid.uuid4().hex
        conn.execute("""
            INSERT INTO quiz_attempts (id, quiz_id, student_email, answers,
                                       score, total, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (attempt_id, quiz_id, user["email"], json.dumps(req.answers),
              score, len(questions), now_iso()))

    return {
        "attempt_id": attempt_id,
        "score": score,
        "total": len(questions),
        "percentage": pct(score, len(questions)),
        "review": review,
    }


@app.get("/api/student/attempts")
def student_attempts(user: dict = Depends(student_only)):
    with db() as conn:
        rows = conn.execute("""
            SELECT a.id, a.quiz_id, q.title, q.topic, q.difficulty,
                   a.score, a.total, a.submitted_at
            FROM quiz_attempts a
            JOIN quizzes q ON q.id = a.quiz_id
            WHERE a.student_email = ?
            ORDER BY a.submitted_at DESC
        """, (user["email"],)).fetchall()

    return [{**dict(r), "percentage": pct(r["score"], r["total"])} for r in rows]


# ==================================================
#                DASHBOARD STATISTICS
# ==================================================

@app.get("/api/stats")
def stats(user: dict = Depends(current_user)):
    documents = len(rag.list_documents())

    with db() as conn:
        if user["role"] == "student":
            published = [r["id"] for r in conn.execute(
                "SELECT id FROM quizzes WHERE published = 1").fetchall()]
            attempts = conn.execute(
                "SELECT quiz_id, score, total FROM quiz_attempts WHERE student_email = ?",
                (user["email"],)).fetchall()

            taken = {a["quiz_id"] for a in attempts}
            scores = [pct(a["score"], a["total"]) for a in attempts]

            return {
                "role": "student",
                "documents": documents,
                "quizzes_pending": len([q for q in published if q not in taken]),
                "quizzes_completed": len(taken & set(published)),
                "average_pct": round(sum(scores) / len(scores), 1) if scores else None,
            }

        mine = conn.execute("""
            SELECT COUNT(*) AS total, SUM(published) AS published
            FROM quizzes WHERE teacher_email = ?
        """, (user["email"],)).fetchone()

        agg = conn.execute("""
            SELECT COUNT(a.id) AS attempts, COUNT(DISTINCT a.student_email) AS students,
                   AVG(100.0 * a.score / NULLIF(a.total, 0)) AS avg_pct
            FROM quiz_attempts a JOIN quizzes q ON q.id = a.quiz_id
            WHERE q.teacher_email = ?
        """, (user["email"],)).fetchone()

        recent = conn.execute("""
            SELECT COALESCE(u.name, a.student_email) AS student_name, q.title,
                   a.score, a.total, a.submitted_at
            FROM quiz_attempts a
            JOIN quizzes q ON q.id = a.quiz_id
            LEFT JOIN users u ON u.email = a.student_email
            WHERE q.teacher_email = ?
            ORDER BY a.submitted_at DESC LIMIT 5
        """, (user["email"],)).fetchall()

    return {
        "role": "teacher",
        "documents": documents,
        "quizzes_total": mine["total"] or 0,
        "quizzes_published": mine["published"] or 0,
        "attempts": agg["attempts"] or 0,
        "students": agg["students"] or 0,
        "average_pct": round(agg["avg_pct"], 1) if agg["avg_pct"] is not None else None,
        "recent": [{**dict(r), "percentage": pct(r["score"], r["total"])} for r in recent],
    }


# ---------- Serve Frontend ----------
# Keep this LAST so it does not shadow API routes.

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")




#uvicorn main:app --reload