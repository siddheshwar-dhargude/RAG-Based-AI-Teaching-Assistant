"use strict";

/* =========================================================
   EduRAG AI - frontend
   Sections: 1 helpers · 2 auth · 3 navigation · 4 dashboard
             5 student quizzes · 6 teacher quizzes · 7 documents
             8 chatbot · 9 live updates · 10 startup
========================================================= */

/* ---------- 1. HELPERS ---------- */

const $ = id => document.getElementById(id);

const esc = s => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

const store = {
    get(k) { try { return localStorage.getItem(k) ?? sessionStorage.getItem(k); } catch { return null; } },
    set(k, v, persist = true) { try { (persist ? localStorage : sessionStorage).setItem(k, v); } catch { /* ignore */ } },
    del(k) { try { localStorage.removeItem(k); sessionStorage.removeItem(k); } catch { /* ignore */ } },
};

const state = {
    token: null,
    user: null,
    role: "student",        // role selected on the login screen
    config: { google_client_id: "", teacher_code_required: false },
    page: "dashboard",
    quizzes: [],            // student: published quizzes
    seen: new Set(),        // student: quiz ids already viewed
    known: null,            // student: ids known this session (for toasts)
    runner: null,           // student: quiz being taken
    draft: null,            // teacher: generated quiz being edited
    docs: [],
    conversation: [],
    busy: false,
    poll: null,
    subjects: [],
    selectedSubject: "",
};

const isTeacher = () => state.user && state.user.role === "teacher";

function fmtDate(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

const pctText = v => (v === null || v === undefined ? "—" : `${v}%`);

function cleanError(e) {
    return String(e.message || e).replace(/^TEACHER_CODE:\s*/, "");
}

function toast(message, opts = {}) {
    const t = document.createElement("div");
    t.className = "toast" + (opts.error ? " error" : "");
    t.textContent = message;
    t.onclick = () => { t.remove(); if (opts.onClick) opts.onClick(); };
    $("toasts").appendChild(t);
    setTimeout(() => t.remove(), opts.error ? 7000 : 6000);
}

function showError(id, message) {
    const el = $(id);
    el.textContent = message;
    el.classList.toggle("hidden", !message);
}

async function api(path, { method = "GET", body, form } = {}) {
    const headers = {};
    if (state.token) headers.Authorization = "Bearer " + state.token;

    let payload;
    if (form) {
        payload = form;
    } else if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        payload = JSON.stringify(body);
    }

    const res = await fetch("/api" + path, { method, headers, body: payload });

    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }

    if (!res.ok) {
        let msg = "Request failed (" + res.status + ")";
        if (data && typeof data.detail === "string") msg = data.detail;
        else if (data && Array.isArray(data.detail)) msg = data.detail.map(d => d.msg).join(", ");

        if (res.status === 401 && state.token && !path.startsWith("/auth/")) {
            signOut("Your session expired. Please sign in again.");
        }
        throw new Error(msg);
    }
    return data;
}

// Small, safe Markdown renderer for AI answers (escapes HTML first)
function md(src) {
    const blocks = [];
    let t = esc(src).replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => {
        blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
        return `\u0000${blocks.length - 1}\u0000`;
    });

    t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>")
         .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");

    let out = "", list = null;
    const close = () => { if (list) { out += `</${list}>`; list = null; } };

    for (const line of t.split("\n")) {
        let m;
        if (/^\u0000\d+\u0000$/.test(line.trim())) { close(); out += line.trim(); }
        else if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
            if (list !== "ul") { close(); out += "<ul>"; list = "ul"; }
            out += `<li>${m[1]}</li>`;
        } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
            if (list !== "ol") { close(); out += "<ol>"; list = "ol"; }
            out += `<li>${m[1]}</li>`;
        } else if ((m = line.match(/^#{1,4}\s+(.*)/))) { close(); out += `<h4>${m[1]}</h4>`; }
        else if (!line.trim()) close();
        else { close(); out += `<p>${line}</p>`; }
    }
    close();
    return out.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[i]);
}


/* ---------- 2. AUTH ---------- */

function selectRole(role) {
    if (!["student", "teacher"].includes(role)) return;

    state.role = role;

    document.querySelectorAll(".role").forEach(button => {
        const selected = button.dataset.role === role;

        button.classList.toggle("active", selected);
        button.setAttribute("aria-pressed", selected);
    });

    const teacherCodeWrap = $("teacherCodeWrap");

    if (teacherCodeWrap) {
        teacherCodeWrap.classList.toggle(
            "hidden",
            !(role === "teacher" && state.config.teacher_code_required)
        );
    }
}

function switchAuth(toRegister) {
    $("signInSection").classList.toggle("hidden", toRegister);
    $("createAccountSection").classList.toggle("hidden", !toRegister);
    showError("loginError", "");
    showError("registerError", "");
}

async function finishAuth(data, remember = true) {
    state.token = data.token;
    store.del("edurag_token");
    store.set("edurag_token", data.token, remember);
    enterApp(data.user);
}

async function handleLogin(e) {
    e.preventDefault();
    showError("loginError", "");
    const btn = $("loginBtn");
    btn.disabled = true;
    try {
        const data = await api("/auth/login", {
            method: "POST",
            body: { email: $("email").value, password: $("password").value, role: state.role },
        });
        const remember = $("remember").checked;
        $("loginForm").reset();
        await finishAuth(data, remember);
    } catch (err) {
        showError("loginError", cleanError(err));
    } finally {
        btn.disabled = false;
    }
}

async function handleRegister(e) {
    e.preventDefault();
    showError("registerError", "");

    if ($("registerPassword").value !== $("confirmPassword").value) {
        showError("registerError", "Passwords do not match.");
        return;
    }

    const btn = $("registerBtn");
    btn.disabled = true;
    try {
        const data = await api("/auth/register", {
            method: "POST",
            body: {
                name: $("registerName").value,
                email: $("registerEmail").value,
                password: $("registerPassword").value,
                role: state.role,
                teacher_code: $("teacherCode").value,
            },
        });
        $("registerForm").reset();
        await finishAuth(data, true);
    } catch (err) {
        showError("registerError", cleanError(err));
    } finally {
        btn.disabled = false;
    }
}

async function handleGoogle(response, teacherCode = "") {
    try {
        const data = await api("/auth/google", {
            method: "POST",
            body: { credential: response.credential, role: state.role, teacher_code: teacherCode },
        });
        await finishAuth(data, true);
    } catch (err) {
        if (String(err.message).startsWith("TEACHER_CODE") && !teacherCode) {
            const code = prompt("Enter your teacher access code:");
            if (code) return handleGoogle(response, code);
        }
        showError("loginError", cleanError(err));
    }
}

function setupGoogle() {
    if (!state.config.google_client_id) return;
    const s = document.createElement("script");
    s.src = "https://accounts.google.com/gsi/client";
    s.async = true;
    s.onload = () => {
        google.accounts.id.initialize({
            client_id: state.config.google_client_id,
            callback: handleGoogle,
        });
        google.accounts.id.renderButton($("googleButton"), {
            theme: "outline", size: "large", width: 350, text: "continue_with",
        });
        $("googleBlock").classList.remove("hidden");
    };
    document.head.appendChild(s);
}

async function signOut(message) {
    if (state.token) { try { await api("/auth/logout", { method: "POST" }); } catch { /* ignore */ } }
    clearInterval(state.poll);
    state.token = null; state.user = null; state.quizzes = []; state.runner = null;
    state.draft = null; state.known = null; state.conversation = [];
    store.del("edurag_token");

    $("appPage").classList.add("hidden");
    $("loginPage").classList.remove("hidden");
    $("botPanel").classList.add("hidden");
    $("quizPreview").innerHTML = "";
    switchAuth(false);
    selectRole("student");
    if (message) showError("loginError", message);
    window.scrollTo(0, 0);
}


/* ---------- 3. NAVIGATION ---------- */

const TITLES = {
    dashboard: "Dashboard", quizzes: "Quizzes", results: "My Results", studio: "Create Quiz",
    myquizzes: "My Quizzes", documents: "Documents", chat: "AI Assistant", history: "Chat History",
};

function enterApp(user) {
    state.user = user;
    $("loginPage").classList.add("hidden");
    $("appPage").classList.remove("hidden");

    const initial = user.name.charAt(0).toUpperCase();
    $("userName").textContent = user.name;
    $("userRole").textContent = isTeacher() ? "Teacher Account" : "Student Account";
    $("welcomeName").textContent = user.name.split(" ")[0];
    $("avatar").textContent = initial;
    $("topAvatar").textContent = initial;

    // Show only the menu items for this role
    document.querySelectorAll(".menu-item[data-for]").forEach(b =>
        b.classList.toggle("hidden", b.dataset.for !== user.role));

    // Only teachers upload files and manage subjects
    $("uploadBox").classList.toggle("hidden", !isTeacher());
    $("subjectSelect").closest(".panel-card").classList.toggle("hidden", !isTeacher());
    $("docsSubtitle").textContent = isTeacher()
        ? "Upload materials. The AI assistant and quiz generator use them."
        : "Materials uploaded by your teachers. The AI assistant uses them to help you.";

    try { state.seen = new Set(JSON.parse(store.get("edurag_seen_" + user.email) || "[]")); }
    catch { state.seen = new Set(); }
    try { state.conversation = JSON.parse(store.get("edurag_chat_" + user.email) || "[]"); }
    catch { state.conversation = []; }

    renderChips();
    renderConversation();
    showPage("dashboard");

    clearInterval(state.poll);
    state.poll = setInterval(pollTick, 8000);
    if (!isTeacher()) pollQuizzes(true);
}

function showPage(page) {
    if (document.querySelector(`.menu-item[data-page="${page}"]`)?.classList.contains("hidden")) page = "dashboard";

    state.page = page;
    Object.keys(TITLES).forEach(p => $(p + "Page").classList.toggle("hidden", p !== page));
    $("pageTitle").textContent = TITLES[page];

    document.querySelectorAll(".menu-item").forEach(b =>
        b.classList.toggle("active", b.dataset.page === page));

    document.body.classList.toggle("on-chat-page", page === "chat");
    if (page === "chat") $("botPanel").classList.add("hidden");

    if (page === "dashboard") loadDashboard();
    else if (page === "quizzes") showQuizList();
    else if (page === "results") loadResults();
    else if (page === "myquizzes") showMyQuizzes();
    else if (page === "documents") loadDocuments();
    else if (page === "history") renderHistory();
    else if (page === "chat") { renderConversation(); $("question").focus(); }
}


/* ---------- 4. DASHBOARD ---------- */

const statCard = (icon, value, label) =>
    `<div class="stat-card"><span>${icon}</span><h2>${esc(value)}</h2><p>${esc(label)}</p></div>`;

const actionCard = (icon, title, desc, go) =>
    `<div class="action-card" data-go="${go}"><span>${icon}</span><h3>${esc(title)}</h3><p>${esc(desc)}</p></div>`;

const panel = (title, inner) =>
    `<div class="dash-panel"><h3>${title}</h3>${inner}</div>`;

async function loadDashboard() {
    const teacher = isTeacher();

    $("welcomeTag").textContent = teacher ? "TEACHER WORKSPACE" : "YOUR AI LEARNING PARTNER";
    $("welcomeMessage").textContent = teacher
        ? "Generate quizzes from your materials and track how students perform."
        : "Take quizzes from your teachers and ask the AI tutor anything.";
    $("welcomeBtn").textContent = teacher ? "Create a Quiz →" : "Take a Quiz →";
    $("welcomeBtn").dataset.go = teacher ? "studio" : "quizzes";
    $("quickTitle").textContent = teacher ? "Teaching Tools" : "Quick Actions";

    $("quickActions").innerHTML = teacher
        ? actionCard("☁", "Upload Materials", "Add notes, slides and PDFs.", "documents") +
          actionCard("✨", "Create AI Quiz", "Generate and publish a quiz.", "studio") +
          actionCard("📋", "View Results", "See how students performed.", "myquizzes")
        : actionCard("📝", "Take a Quiz", "Quizzes your teachers published.", "quizzes") +
          actionCard("✦", "Ask AI Assistant", "Get help with any problem.", "chat") +
          actionCard("📄", "Study Materials", "Browse learning resources.", "documents");

    try {
        if (teacher) {
            const [s, mine] = await Promise.all([api("/stats"), api("/teacher/quizzes")]);

            $("statsGrid").innerHTML =
                statCard("📋", s.quizzes_published, "Published Quizzes") +
                statCard("🧑‍🎓", s.students, "Active Students") +
                statCard("📨", s.attempts, "Submissions") +
                statCard("📊", pctText(s.average_pct), "Class Average") +
                statCard("📚", s.documents, "Study Documents");

            const recent = s.recent.length
                ? s.recent.map(r => `<div class="history-item">🧑‍🎓 <b>${esc(r.student_name)}</b> scored
                    ${r.score}/${r.total} on “${esc(r.title)}”<p>${fmtDate(r.submitted_at)}</p></div>`).join("")
                : "No submissions yet. They appear here as soon as students finish a quiz.";

            const quizzes = mine.length
                ? mine.slice(0, 4).map(q => `<div class="history-item clickable" data-go="myquizzes">
                    📋 ${esc(q.title)} ${q.published ? "" : '<span class="pill draft">Draft</span>'}
                    <p>${q.attempts} attempt(s) · avg ${pctText(q.avg_pct)}</p></div>`).join("")
                : "You have not created a quiz yet. Try “Create AI Quiz”.";

            $("dashPanels").innerHTML = panel("Recent Student Submissions", recent) + panel("Your Quizzes", quizzes);
        } else {
            const [s, quizzes, attempts] = await Promise.all([
                api("/stats"), api("/quizzes"), api("/student/attempts")]);
            state.quizzes = quizzes;
            updateBadges();

            $("statsGrid").innerHTML =
                statCard("📝", s.quizzes_pending, "Quizzes To Take") +
                statCard("✅", s.quizzes_completed, "Quizzes Completed") +
                statCard("📊", pctText(s.average_pct), "Average Score") +
                statCard("📚", s.documents, "Study Documents");

            const pending = quizzes.filter(q => q.attempts === 0).slice(0, 4);
            const pendingHtml = pending.length
                ? pending.map(q => `<div class="history-item">📝 <b>${esc(q.title)}</b>
                    <p>${esc(q.topic)} · ${q.question_count} questions · ${esc(q.difficulty)}</p>
                    <button class="btn btn-primary small-btn" data-start="${q.id}" style="margin-top:10px">Start quiz</button></div>`).join("")
                : "You're all caught up. New quizzes will appear here automatically.";

            const recentHtml = attempts.length
                ? attempts.slice(0, 4).map(a => `<div class="history-item">🏆 ${esc(a.title)}
                    <p>${a.score}/${a.total} (${a.percentage}%) · ${fmtDate(a.submitted_at)}</p></div>`).join("")
                : "No quiz attempts yet.";

            $("dashPanels").innerHTML = panel("📝 Pending Quizzes", pendingHtml) + panel("Recent Results", recentHtml);
        }
    } catch (err) {
        $("dashPanels").innerHTML = `<div class="empty">Could not load the dashboard: ${esc(cleanError(err))}</div>`;
    }
}


/* ---------- 5. STUDENT QUIZZES ---------- */

function unseenIds() {
    return state.quizzes.filter(q => q.attempts === 0 && !state.seen.has(q.id)).map(q => q.id);
}

function updateBadges() {
    const n = isTeacher() ? 0 : unseenIds().length;
    for (const id of ["quizBadge", "bellBadge"]) {
        $(id).textContent = n;
        $(id).classList.toggle("hidden", n === 0);
    }
}

function markSeen() {
    state.quizzes.forEach(q => state.seen.add(q.id));
    store.set("edurag_seen_" + state.user.email, JSON.stringify([...state.seen]));
    updateBadges();
}

async function showQuizList() {
    $("quizListView").classList.remove("hidden");
    $("quizRunner").classList.add("hidden");
    state.runner = null;
    try { state.quizzes = await api("/quizzes"); }
    catch (err) { $("quizList").innerHTML = `<div class="empty">${esc(cleanError(err))}</div>`; return; }
    renderQuizList();
}

function renderQuizList() {
    const fresh = new Set(unseenIds());
    const list = $("quizList");

    if (!state.quizzes.length) {
        list.innerHTML = `<div class="empty">No quizzes yet. When a teacher publishes one it shows up here automatically.</div>`;
    } else {
        list.innerHTML = state.quizzes.map(q => `
            <div class="quiz-card ${fresh.has(q.id) ? "is-new" : ""}">
                <div class="quiz-info">
                    <h3>${esc(q.title)}</h3>
                    <div class="quiz-meta">
                        <span class="pill ${q.difficulty.toLowerCase()}">${esc(q.difficulty)}</span>
                        ${fresh.has(q.id) ? '<span class="pill new">NEW</span>' : ""}
                        ${q.attempts ? `<span class="pill done">Best ${q.best_pct}%</span>` : ""}<br>
                        ${esc(q.topic)} · ${q.question_count} questions · by ${esc(q.teacher_name)} · ${fmtDate(q.created_at)}
                    </div>
                </div>
                <div class="quiz-actions">
                    <button class="btn btn-primary" data-start="${q.id}">${q.attempts ? "Retake" : "Start quiz"}</button>
                </div>
            </div>`).join("");
    }
    // Mark as seen after this render so the NEW tags are visible once
    if (state.page === "quizzes") markSeen();
}

async function startQuiz(id) {
    try {
        const quiz = await api("/quizzes/" + id);
        if (state.page !== "quizzes") showPage("quizzes");  // resets the runner, so set it afterwards
        state.runner = { quiz, answers: {} };
        renderRunner();
    } catch (err) {
        toast(cleanError(err), { error: true });
    }
}

function renderRunner() {
    const { quiz } = state.runner;
    $("quizListView").classList.add("hidden");
    const box = $("quizRunner");
    box.classList.remove("hidden");

    box.innerHTML = `
        <button class="back-link" data-act="back">← Back to quizzes</button>
        <div class="runner-top">
            <h2>${esc(quiz.title)}</h2>
            <div class="quiz-meta"><span class="pill ${quiz.difficulty.toLowerCase()}">${esc(quiz.difficulty)}</span>
                ${esc(quiz.topic)} · ${quiz.questions.length} questions</div>
            <div class="progress"><div id="runProg"></div></div>
            <div class="quiz-meta" id="runCount"></div>
        </div>
        ${quiz.questions.map((q, i) => `
            <div class="q-card">
                <h4><span class="q-num">${i + 1}.</span>${esc(q.question)}</h4>
                ${q.options.map((o, j) => `
                    <label class="opt">
                        <input type="radio" name="q${i}" data-q="${i}" data-o="${j}">
                        <span>${esc(o)}</span>
                    </label>`).join("")}
            </div>`).join("")}
        <button class="btn btn-primary" id="submitQuizBtn" data-act="submit">Submit Quiz</button>`;

    updateProgress();
    window.scrollTo(0, 0);
}

function updateProgress() {
    const total = state.runner.quiz.questions.length;
    const done = Object.keys(state.runner.answers).length;
    $("runProg").style.width = (100 * done / total) + "%";
    $("runCount").textContent = `${done} / ${total} answered`;
}

async function submitQuiz() {
    const { quiz, answers } = state.runner;
    const missing = quiz.questions.length - Object.keys(answers).length;
    if (missing && !confirm(`${missing} question(s) are unanswered. Submit anyway?`)) return;

    const btn = $("submitQuizBtn");
    btn.disabled = true;
    btn.textContent = "Submitting…";
    try {
        const result = await api(`/quizzes/${quiz.id}/submit`, { method: "POST", body: { answers } });
        renderReview(quiz, result);
        api("/quizzes").then(q => { state.quizzes = q; updateBadges(); }).catch(() => {});
    } catch (err) {
        toast(cleanError(err), { error: true });
        btn.disabled = false;
        btn.textContent = "Submit Quiz";
    }
}

function renderReview(quiz, r) {
    state.runner.review = r.review;
    const msg = r.percentage >= 80 ? "Excellent work! 🎉" : r.percentage >= 50 ? "Good effort. Review the explanations below." : "Keep practising. The explanations will help.";

    $("quizRunner").innerHTML = `
        <button class="back-link" data-act="back">← Back to quizzes</button>
        <div class="score-hero">
            <div class="big">${r.percentage}%</div>
            <p>You scored ${r.score} out of ${r.total} on “${esc(quiz.title)}”. ${msg}</p>
        </div>
        ${r.review.map((q, i) => `
            <div class="q-card">
                <h4><span class="q-num">${i + 1}.</span>${esc(q.question)}</h4>
                ${q.options.map(o => {
                    const cls = o === q.correct_answer ? "correct" : (o === q.your_answer ? "wrong" : "");
                    const mark = o === q.correct_answer ? "✓" : (o === q.your_answer ? "✗" : "•");
                    return `<div class="opt review ${cls}"><span>${mark}</span><span>${esc(o)}${o === q.your_answer ? " <em>(your answer)</em>" : ""}</span></div>`;
                }).join("")}
                ${q.your_answer ? "" : '<div class="quiz-meta">You skipped this question.</div>'}
                ${q.explanation ? `<div class="explain"><b>Explanation:</b> ${esc(q.explanation)}</div>` : ""}
                ${q.is_correct ? "" : `<button class="btn btn-ghost small-btn" style="margin-top:12px" data-explain="${i}">💬 Ask AI to explain</button>`}
            </div>`).join("")}
        <div class="publish-bar">
            <button class="btn btn-primary" data-start="${quiz.id}">Retake quiz</button>
            <button class="btn btn-ghost" data-act="back">Back to quizzes</button>
            <button class="btn btn-ghost" data-go="results">My results</button>
        </div>`;
    window.scrollTo(0, 0);
}

async function loadResults() {
    const box = $("resultsList");
    box.innerHTML = '<div class="empty">Loading…</div>';
    try {
        const rows = await api("/student/attempts");
        if (!rows.length) { box.innerHTML = '<div class="empty">You have not taken any quiz yet.</div>'; return; }
        box.innerHTML = `<div class="table-wrap"><table class="data">
            <tr><th>QUIZ</th><th>SCORE</th><th></th><th>DATE</th><th></th></tr>
            ${rows.map(r => `<tr>
                <td><b>${esc(r.title)}</b><br><span class="quiz-meta">${esc(r.topic)} · ${esc(r.difficulty)}</span></td>
                <td>${r.score}/${r.total} (${r.percentage}%)</td>
                <td><div class="bar"><div style="width:${r.percentage}%"></div></div></td>
                <td>${fmtDate(r.submitted_at)}</td>
                <td><button class="btn btn-ghost small-btn" data-start="${r.quiz_id}">Retake</button></td>
            </tr>`).join("")}</table></div>`;
    } catch (err) {
        box.innerHTML = `<div class="empty">${esc(cleanError(err))}</div>`;
    }
}


/* ---------- 6. TEACHER QUIZZES ---------- */

async function generateQuiz() {
    showError("studioError", "");
    const topic = $("quizTopic").value.trim();
    if (!topic) { showError("studioError", "Please enter a quiz topic."); return; }

    const btn = $("generateBtn");
    btn.disabled = true;
    btn.textContent = "⏳ Generating… this can take a minute";
    $("quizPreview").innerHTML = "";

    try {
        const data = await api("/quizzes/generate", {
            method: "POST",
            body: {
                topic,
                number_of_questions: Math.min(20, Math.max(1, parseInt($("quizCount").value, 10) || 5)),
                difficulty: $("quizDifficulty").value,
                source: $("quizSource").value,
            },
        });
        state.draft = data;
        renderEditor(data);
    } catch (err) {
        showError("studioError", cleanError(err));
    } finally {
        btn.disabled = false;
        btn.textContent = "✨ Generate Quiz";
    }
}

function editorCard(q, i) {
    const correctIdx = q.options.indexOf(q.correct_answer);
    return `
        <div class="q-card" data-q>
            <button class="btn btn-danger small-btn remove-q" data-act="remove-q">Remove</button>
            <label class="editor-label q-label">Question ${i + 1}</label>
            <textarea class="e-q">${esc(q.question)}</textarea>
            <p class="editor-hint">Select the radio button next to the correct answer.</p>
            ${q.options.map((o, j) => `
                <div class="opt-row">
                    <input type="radio" name="c${i}" ${j === correctIdx ? "checked" : ""}>
                    <input type="text" class="e-opt" value="${esc(o)}">
                </div>`).join("")}
            <label class="editor-label" style="margin-top:12px">Explanation</label>
            <textarea class="e-exp">${esc(q.explanation || "")}</textarea>
        </div>`;
}

function renderEditor(quiz) {
    $("quizPreview").innerHTML = `
        <div class="editor">
            <h3 class="section-title">Review &amp; edit before publishing</h3>
            <div class="panel-card" style="margin-top:0">
                <label class="editor-label">Quiz title</label>
                <input type="text" id="edTitle" value="${esc(quiz.title)}">
            </div>
            <div id="edQuestions">${quiz.questions.map(editorCard).join("")}</div>
            <div class="publish-bar">
                <button class="btn btn-primary" data-act="publish">🚀 Publish to students</button>
                <button class="btn btn-ghost" data-act="draft">Save as draft</button>
                <button class="btn btn-ghost" data-act="discard">Discard</button>
            </div>
        </div>`;
    $("quizPreview").scrollIntoView({ behavior: "smooth" });
}

function collectQuiz() {
    const title = $("edTitle").value.trim();
    if (!title) throw new Error("Please give the quiz a title.");

    const cards = [...document.querySelectorAll("#edQuestions .q-card")];
    if (!cards.length) throw new Error("The quiz has no questions.");

    const questions = cards.map((card, i) => {
        const question = card.querySelector(".e-q").value.trim();
        const options = [...card.querySelectorAll(".e-opt")].map(o => o.value.trim());
        const idx = [...card.querySelectorAll('input[type=radio]')].findIndex(r => r.checked);

        if (!question) throw new Error(`Question ${i + 1} is empty.`);
        if (options.some(o => !o)) throw new Error(`Question ${i + 1} has an empty option.`);
        if (new Set(options).size !== 4) throw new Error(`Question ${i + 1} has duplicate options.`);
        if (idx < 0) throw new Error(`Choose the correct answer for question ${i + 1}.`);

        return { question, options, correct_answer: options[idx], explanation: card.querySelector(".e-exp").value.trim() };
    });

    return { title, topic: state.draft.topic, difficulty: state.draft.difficulty, questions };
}

async function publishQuiz(published) {
    let payload;
    try { payload = collectQuiz(); }
    catch (err) { toast(err.message, { error: true }); return; }

    try {
        const res = await api("/quizzes", { method: "POST", body: { ...payload, published } });
        toast(published ? "✅ Quiz published. Students can see it now." : "Saved as draft.");
        state.draft = null;
        $("quizPreview").innerHTML = "";
        $("quizTopic").value = "";
        showPage("myquizzes");
        return res;
    } catch (err) {
        toast(cleanError(err), { error: true });
    }
}

function showMyQuizzes() {
    $("myQuizzesView").classList.remove("hidden");
    $("quizResultsView").classList.add("hidden");
    loadMyQuizzes();
}

async function loadMyQuizzes() {
    const box = $("myQuizList");
    try {
        const rows = await api("/teacher/quizzes");
        if (!rows.length) {
            box.innerHTML = `<div class="empty">You have not created any quizzes yet.<br><br>
                <button class="btn btn-primary" data-go="studio">✨ Create your first quiz</button></div>`;
            return;
        }
        box.innerHTML = rows.map(q => `
            <div class="quiz-card">
                <div class="quiz-info">
                    <h3>${esc(q.title)}</h3>
                    <div class="quiz-meta">
                        <span class="pill ${q.difficulty.toLowerCase()}">${esc(q.difficulty)}</span>
                        <span class="pill ${q.published ? "done" : "draft"}">${q.published ? "Published" : "Draft"}</span><br>
                        ${esc(q.topic)} · ${q.question_count} questions · ${q.attempts} attempt(s) · average ${pctText(q.avg_pct)}
                        · ${fmtDate(q.created_at)}
                    </div>
                </div>
                <div class="quiz-actions">
                    <button class="btn btn-primary small-btn" data-act="results" data-id="${q.id}">Results</button>
                    <button class="btn btn-ghost small-btn" data-act="toggle" data-id="${q.id}" data-pub="${q.published ? 1 : 0}">${q.published ? "Unpublish" : "Publish"}</button>
                    <button class="btn btn-danger small-btn" data-act="delete" data-id="${q.id}">Delete</button>
                </div>
            </div>`).join("");
    } catch (err) {
        box.innerHTML = `<div class="empty">${esc(cleanError(err))}</div>`;
    }
}

async function showQuizResults(id) {
    $("myQuizzesView").classList.add("hidden");
    const view = $("quizResultsView");
    view.classList.remove("hidden");
    view.dataset.id = id;
    view.innerHTML = '<div class="empty">Loading…</div>';

    try {
        const r = await api(`/quizzes/${id}/results`);
        view.innerHTML = `
            <button class="back-link" data-act="back-mine">← Back to my quizzes</button>
            <h2>${esc(r.quiz.title)}</h2>
            <p class="muted">${esc(r.quiz.topic)} · ${esc(r.quiz.difficulty)} · ${r.attempts.length} attempt(s) · class average ${pctText(r.average_pct)}</p>

            <h3 class="section-title">Student attempts</h3>
            ${r.attempts.length ? `<div class="table-wrap"><table class="data">
                <tr><th>STUDENT</th><th>SCORE</th><th></th><th>SUBMITTED</th></tr>
                ${r.attempts.map(a => `<tr>
                    <td><b>${esc(a.student_name)}</b><br><span class="quiz-meta">${esc(a.student_email)}</span></td>
                    <td>${a.score}/${a.total} (${a.percentage}%)</td>
                    <td><div class="bar"><div style="width:${a.percentage}%"></div></div></td>
                    <td>${fmtDate(a.submitted_at)}</td></tr>`).join("")}
                </table></div>` : '<div class="empty">No student has taken this quiz yet.</div>'}

            <h3 class="section-title">Question difficulty (% answered correctly)</h3>
            ${r.question_stats.map((s, i) => `
                <div class="history-item">${i + 1}. ${esc(s.question)}
                    <div class="bar" style="margin-top:8px"><div style="width:${s.correct_pct ?? 0}%"></div></div>
                    <p>${pctText(s.correct_pct)}</p></div>`).join("")}`;
    } catch (err) {
        view.innerHTML = `<button class="back-link" data-act="back-mine">← Back</button><div class="empty">${esc(cleanError(err))}</div>`;
    }
}

async function teacherAction(act, id, btn) {
    try {
        if (act === "results") return showQuizResults(id);
        if (act === "toggle") {
            await api(`/quizzes/${id}/publish`, { method: "PATCH", body: { published: btn.dataset.pub !== "1" } });
            toast(btn.dataset.pub === "1" ? "Quiz hidden from students." : "Quiz is now visible to students.");
        }
        if (act === "delete") {
            if (!confirm("Delete this quiz and all student results? This cannot be undone.")) return;
            await api("/quizzes/" + id, { method: "DELETE" });
            toast("Quiz deleted.");
        }
        loadMyQuizzes();
    } catch (err) {
        toast(cleanError(err), { error: true });
    }
}


/* ---------- 7. DOCUMENTS ---------- */

async function loadSubjects() {
    try {
        state.subjects = await api("/subjects");
        const select = $("subjectSelect");
        if (!select) return;

        select.innerHTML = state.subjects
            .map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`)
            .join("");

        if (state.selectedSubject) select.value = state.selectedSubject;
        state.selectedSubject = select.value;
    } catch (err) {
        toast(cleanError(err), { error: true });
    }
}

async function addSubject() {
    const input = $("newSubjectInput");
    const name = input.value.trim();
    if (!name) { toast("Enter a subject name.", { error: true }); return; }

    try {
        const subject = await api("/subjects", { method: "POST", body: { name } });
        input.value = "";
        state.selectedSubject = subject.id;
        await loadSubjects();
        $("subjectSelect").value = subject.id;
        toast("Subject added successfully.");
    } catch (err) {
        toast(cleanError(err), { error: true });
    }
}

async function loadDocuments() {
    const list = $("documentList");
    await loadSubjects();

    try {
        state.docs = await api("/documents");
    } catch (err) {
        list.innerHTML = `<div class="empty">${esc(cleanError(err))}</div>`;
        return;
    }

    if (!state.docs.length) {
        list.innerHTML = `<div class="empty">${isTeacher()
            ? "No documents yet. Upload some above."
            : "Your teachers have not uploaded any materials yet."}</div>`;
        return;
    }

    const grouped = {};
    state.docs.forEach(d => {
        const subject = d.subject || "General";
        (grouped[subject] = grouped[subject] || []).push(d);
    });

    list.innerHTML = Object.entries(grouped).map(([subject, docs]) => `
        <div class="panel-card" style="margin-bottom:18px;">
            <h3>📚 ${esc(subject)}</h3>
            ${docs.map(d => {
                const type = d.name.toLowerCase().endsWith(".pdf") ? "pdf" : "file";
                return `
                <div class="document-item">
                    <strong>📄 ${esc(d.name)}</strong>
                    <p>${esc(d.size)} • ${d.chunks} chunks indexed</p>
                    <button style="color:#5949bb;margin-right:14px" data-open-doc="${esc(d.id)}" data-doc-type="${type}">
                        ${type === "pdf" ? "View" : "Download"}
                    </button>
                    ${isTeacher() ? `<button data-del-doc="${esc(d.id)}">Remove</button>` : ""}
                </div>`;
            }).join("")}
        </div>`).join("");
}

// Open the original teacher-uploaded document
async function openDocument(docId, type) {
    // Open a blank tab immediately to avoid popup blockers
    const newTab = type === "pdf" ? window.open("about:blank", "_blank") : null;
    if (type === "pdf" && !newTab) {
        toast("Please allow pop-ups to view the PDF.", { error: true });
        return;
    }

    try {
        const response = await fetch(`/api/documents/${encodeURIComponent(docId)}/file`, {
            headers: { Authorization: "Bearer " + state.token },
        });

        if (!response.ok) {
            let message = "Unable to open document.";
            try { message = (await response.json()).detail || message; } catch { /* ignore */ }
            throw new Error(message);
        }

        const fileUrl = URL.createObjectURL(await response.blob());

        if (type === "pdf") {
            newTab.location.href = fileUrl;
            setTimeout(() => URL.revokeObjectURL(fileUrl), 60000);
        } else {
            const link = document.createElement("a");
            link.href = fileUrl;
            link.download = "";
            document.body.appendChild(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(fileUrl), 10000);
        }
    } catch (err) {
        if (newTab) newTab.close();
        toast(cleanError(err), { error: true });
    }
}

async function uploadFiles(files) {
    if (!files.length) return;

    if (!state.selectedSubject) {
        toast("Please select a subject first.", { error: true });
        return;
    }

    const form = new FormData();
    files.forEach(f => form.append("files", f));
    form.append("subject", state.selectedSubject);

    $("documentList").innerHTML = '<div class="empty">Uploading and processing documents…</div>';

    try {
        const res = await api("/upload", { method: "POST", form });
        if (res.errors.length) toast("Some files failed:\n" + res.errors.join("\n"), { error: true });
        else toast(`✅ ${res.uploaded.length} document(s) indexed.`);
    } catch (err) {
        toast("Upload failed: " + cleanError(err), { error: true });
    }

    loadDocuments();
}


/* ---------- 8. CHATBOT (full page + floating widget) ---------- */

const CHIPS = [
    "Summarize my study materials",
    "Help me solve a problem",
    "Explain a concept simply",
    "Make a study plan",
];

function renderChips() {
    const html = CHIPS.map(c => `<button data-chip="${esc(c)}">${esc(c)}</button>`).join("");
    $("chatSuggestions").innerHTML = html;
    $("botChips").innerHTML = html;
}

function messageHtml(m) {
    if (m.role === "user") return `<div class="user-message">${esc(m.text)}</div>`;

    let body;
    if (m.pending) body = '<div class="typing"><span></span><span></span><span></span></div>';
    else body = `<div class="md">${md(m.text)}</div>`;

    let foot = "";
    if (!m.pending && !m.error) {
        if (m.mode === "materials") {
            foot = `<div class="msg-tag">📚 Answered from your study materials</div>` +
                (m.sources?.length ? `<div class="msg-sources">Sources: ${m.sources.map(esc).join(" · ")}</div>` : "");
        } else {
            foot = `<div class="msg-tag">🌐 General knowledge (not found in uploaded materials)</div>`;
        }
    }
    return `<div class="ai-message"><b>✦ EduRAG AI</b>${body}${foot}</div>`;
}

function renderConversation() {
    const welcome = `<div class="ai-message"><b>✦ EduRAG AI</b><div class="md"><p>Hi! I'm your AI teaching assistant. I use your study materials when they're relevant, and I can help with <b>any</b> other problem too. What would you like to work on?</p></div></div>`;
    const html = welcome + state.conversation.map(messageHtml).join("");
    for (const id of ["chatMessages", "botMessages"]) {
        const el = $(id);
        el.innerHTML = html;
        el.scrollTop = el.scrollHeight;
    }
}

function saveConversation() {
    if (!state.user) return;
    const keep = state.conversation.filter(m => !m.pending).slice(-60);
    store.set("edurag_chat_" + state.user.email, JSON.stringify(keep));
}

function setBusy(b) {
    state.busy = b;
    for (const id of ["chatSend", "botSend"]) $(id).disabled = b;
}

async function askAI(question) {
    question = question.trim();
    if (!question || state.busy) return;

    const history = state.conversation
        .filter(m => !m.pending && !m.error)
        .slice(-6)
        .map(m => ({ role: m.role === "user" ? "user" : "assistant", content: m.text }));

    state.conversation.push({ role: "user", text: question });
    const reply = { role: "ai", text: "", pending: true };
    state.conversation.push(reply);
    setBusy(true);
    renderConversation();

    try {
        const data = await api("/chat", { method: "POST", body: { question, history } });
        Object.assign(reply, { text: data.answer, mode: data.mode, sources: data.sources, pending: false });
    } catch (err) {
        Object.assign(reply, { text: "Sorry, something went wrong: " + cleanError(err), pending: false, error: true });
    }

    setBusy(false);
    saveConversation();
    renderConversation();
}

function toggleBot(open) {
    const panel = $("botPanel");
    const show = open ?? panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !show);
    $("botFab").textContent = show ? "✕" : "✦";
    if (show) { renderConversation(); $("botInput").focus(); }
}

function newChat() {
    if (state.busy) return;
    state.conversation = [];
    saveConversation();
    renderConversation();
}

function renderHistory() {
    const qs = [...new Set(state.conversation.filter(m => m.role === "user").map(m => m.text))].reverse();
    $("historyList").innerHTML = qs.length
        ? qs.map(q => `<div class="history-item clickable" data-reask="${esc(q)}">💬 ${esc(q)}</div>`).join("")
        : '<div class="empty">No conversations yet.</div>';
}


/* ---------- 9. LIVE UPDATES ---------- */

async function pollQuizzes(initial = false) {
    try {
        const latest = await api("/quizzes");
        const ids = new Set(latest.map(q => q.id));

        const added = state.known
            ? latest.filter(q => !state.known.has(q.id) && q.attempts === 0)
            : [];
        const changed = !state.known || added.length > 0 ||
            latest.length !== state.quizzes.length;

        state.known = ids;
        state.quizzes = latest;
        updateBadges();

        added.forEach(q => toast(`📝 New quiz: ${q.title}`, { onClick: () => showPage("quizzes") }));

        if (changed && !initial) {
            if (state.page === "quizzes" && !state.runner) renderQuizList();
            else if (state.page === "dashboard") loadDashboard();
        }
    } catch { /* offline or signed out: ignore */ }
}

function pollTick() {
    if (!state.user || document.hidden) return;

    if (!isTeacher()) { pollQuizzes(); return; }

    // Teachers: keep results fresh while they are looking at them
    if (state.page === "dashboard") loadDashboard();
    else if (state.page === "myquizzes") {
        const view = $("quizResultsView");
        if (!view.classList.contains("hidden")) showQuizResults(view.dataset.id);
        else loadMyQuizzes();
    }
}


/* ---------- 10. STARTUP ---------- */

function bindEvents() {
    // Login / register
    // Student and Teacher role selection
document.querySelectorAll(".role").forEach(button => {
    button.addEventListener("click", function (event) {
        event.preventDefault();

        selectRole(this.dataset.role);
    });
});
    $("toRegister").addEventListener("click", e => { e.preventDefault(); switchAuth(true); });
    $("toSignIn").addEventListener("click", e => { e.preventDefault(); switchAuth(false); });
    $("loginForm").addEventListener("submit", handleLogin);
    $("registerForm").addEventListener("submit", handleRegister);
    $("logoutBtn").addEventListener("click", () => signOut());

    $("forgotPasswordLink").addEventListener("click", function (e) {
    e.preventDefault();

    const email = $("email").value.trim();

    if (!email) {
        alert("Please enter your registered email address first.");
        $("email").focus();
        return;
    }

    alert("Password reset functionality will be available soon.");
});

    // Navigation
    document.querySelectorAll(".menu-item").forEach(b => b.addEventListener("click", () => showPage(b.dataset.page)));
    $("bellBtn").addEventListener("click", () => showPage(isTeacher() ? "myquizzes" : "quizzes"));
    $("newChatBtn").addEventListener("click", newChat);

    // Quiz generation + documents
    $("generateBtn").addEventListener("click", generateQuiz);
    $("fileInput").addEventListener("change", e => { uploadFiles([...e.target.files]); e.target.value = ""; });
    $("subjectSelect").addEventListener("change", e => { state.selectedSubject = e.target.value; });
    $("addSubjectBtn").addEventListener("click", addSubject);

    // Chat inputs
    $("chatSend").addEventListener("click", () => { const v = $("question").value; $("question").value = ""; askAI(v); });
    $("question").addEventListener("keydown", e => { if (e.key === "Enter") $("chatSend").click(); });
    $("botSend").addEventListener("click", () => { const v = $("botInput").value; $("botInput").value = ""; askAI(v); });
    $("botInput").addEventListener("keydown", e => { if (e.key === "Enter") $("botSend").click(); });
    $("botFab").addEventListener("click", () => toggleBot());
    $("botClose").addEventListener("click", () => toggleBot(false));
    $("botNew").addEventListener("click", newChat);

    // One delegated handler for every dynamically rendered control
    document.addEventListener("click", e => {
        const t = e.target.closest(
            "[data-go],[data-start],[data-act],[data-explain],[data-chip],[data-reask],[data-del-doc],[data-open-doc]"
        );
        if (!t) return;

        if (t.dataset.go) return showPage(t.dataset.go);
        if (t.dataset.start) return startQuiz(t.dataset.start);

        if (t.dataset.chip) {
            if (state.page === "chat") showPage("chat"); else toggleBot(true);
            return askAI(t.dataset.chip);
        }
        if (t.dataset.reask) { showPage("chat"); return askAI(t.dataset.reask); }

        if (t.dataset.explain !== undefined) {
            const q = state.runner.review[+t.dataset.explain];
            toggleBot(true);
            return askAI(
                `I got this quiz question wrong. Please explain it clearly.\n\nQuestion: ${q.question}\n` +
                `My answer: ${q.your_answer || "(skipped)"}\nCorrect answer: ${q.correct_answer}`);
        }

        if (t.dataset.openDoc) {
            return openDocument(t.dataset.openDoc, t.dataset.docType);
        }

        if (t.dataset.delDoc) {
            if (!confirm("Remove this document from the AI's knowledge?")) return;
            api("/documents/" + t.dataset.delDoc, { method: "DELETE" })
                .then(loadDocuments).catch(err => toast(cleanError(err), { error: true }));
            return;
        }

        switch (t.dataset.act) {
            case "back": return showQuizList();
            case "submit": return submitQuiz();
            case "remove-q": {
                t.closest(".q-card").remove();
                document.querySelectorAll("#edQuestions .q-label").forEach((l, i) => l.textContent = "Question " + (i + 1));
                return;
            }
            case "publish": return publishQuiz(true);
            case "draft": return publishQuiz(false);
            case "discard":
                state.draft = null; $("quizPreview").innerHTML = ""; return;
            case "back-mine": return showMyQuizzes();
            case "results": case "toggle": case "delete":
                return teacherAction(t.dataset.act, t.dataset.id, t);
        }
    });

    // Quiz answer selection (delegated)
    $("quizRunner").addEventListener("change", e => {
        const r = e.target;
        if (!r.matches("input[data-q]") || !state.runner) return;
        const i = +r.dataset.q, j = +r.dataset.o;
        state.runner.answers[i] = state.runner.quiz.questions[i].options[j];
        r.closest(".q-card").querySelectorAll(".opt").forEach(o =>
            o.classList.toggle("selected", o.contains(r)));
        updateProgress();
    });

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && state.user && !isTeacher()) pollQuizzes();
    });
}

async function init() {
    bindEvents();

    try { state.config = await api("/config"); } catch { /* defaults */ }
    setupGoogle();
    selectRole("student");

    const saved = store.get("edurag_token");
    if (!saved) return;

    state.token = saved;
    try {
        enterApp(await api("/auth/me"));
    } catch {
        state.token = null;
        store.del("edurag_token");
    }
}

init();
