"use strict";
// Bol keeps everything in this browser: sessions, questions, answers, scores and the cloned voice live in
// localStorage, and nothing is stored on the server. Store.handle() answers the same /api/... paths the pages
// have always used, from that storage, and calls the server only for work: reading a resume and job description,
// scoring an answer, speaking a text, and cloning a voice.

const Store = (() => {
  const KEY = "bol.v1";
  const blank = () => ({ profile: { name: "", feedback_language: "hi-IN", interview_date: null }, voice: null,
    sessions: {}, questions: {}, attempts: {} });
  let db = load();
  let config = null;
  const clips = new Map(); // spoken audio for this visit, so a replay doesn't cost another Gnani call

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      return raw ? { ...blank(), ...JSON.parse(raw) } : blank();
    } catch { return blank(); } // storage blocked or corrupted: start empty rather than break
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(db)); } catch {
      throw new Error("This browser's storage is full. Use Settings → Start fresh to clear old practice.");
    }
  }
  const uid = () => crypto.randomUUID().replace(/-/g, "");
  const now = () => new Date().toISOString();
  const missing = (what) => { throw new Error(`${what} isn't in this browser. Bol keeps your practice only on the device you used.`); };

  async function server(path, opts = {}) {
    const base = (window.BOL_CONFIG && window.BOL_CONFIG.apiBase) || "";
    const res = await fetch(base + path, opts);
    const type = res.headers.get("content-type") || "";
    if (!res.ok) {
      let msg = `Request failed (${res.status})`;
      if (type.includes("json")) {
        const body = await res.json();
        msg = typeof body.detail === "string" ? body.detail : (body.detail?.[0]?.msg || msg);
      }
      throw new Error(msg);
    }
    return type.includes("json") ? res.json() : res;
  }

  // ---------- reading the stored data in the shapes the pages expect ----------

  const questionsOf = (sessionId) => Object.values(db.questions).filter((q) => q.session_id === sessionId)
    .sort((a, b) => a.position - b.position || (a.kind === "main" ? 0 : 1) - (b.kind === "main" ? 0 : 1));
  const attemptsOf = (questionId) => Object.values(db.attempts).filter((a) => a.question_id === questionId)
    .sort((a, b) => a.number - b.number);
  const followOf = (questionId) => Object.values(db.questions).find((q) => q.parent_id === questionId);

  function attemptJson(a, previous) {
    const out = { id: a.id, question_id: a.question_id, number: a.number, transcript: a.transcript,
      duration_s: a.duration_s, acoustics: a.acoustics, language: a.language, scores: a.scores, content: a.content,
      rewrite: a.rewrite, tips: a.tips, native_feedback: a.native_feedback, feedback_language: a.feedback_language,
      notes: a.notes, created_at: a.created_at };
    if (previous) {
      out.previous = { id: previous.id, number: previous.number, scores: previous.scores,
        fillers_per_min: previous.language?.fillers_per_min, wpm: previous.language?.wpm };
    }
    return out;
  }
  function questionJson(q) {
    const attempts = attemptsOf(q.id);
    const best = attempts.length ? Math.max(...attempts.map((a) => a.scores?.overall || 0)) : null;
    return { id: q.id, position: q.position, text: q.text, why: q.why, kind: q.kind, parent_id: q.parent_id,
      attempts: attempts.length, best, last_attempt_id: attempts.length ? attempts[attempts.length - 1].id : null };
  }
  function place(q) { // "Question 2 of 5" and the next main question
    const mains = questionsOf(q.session_id).filter((x) => x.kind === "main");
    const anchor = q.parent_id || q.id;
    const idx = Math.max(0, mains.findIndex((x) => x.id === anchor));
    return { number: idx + 1, total: mains.length, next_question_id: idx + 1 < mains.length ? mains[idx + 1].id : null };
  }
  const previousOf = (a) => attemptsOf(a.question_id).find((x) => x.number === a.number - 1);

  // ---------- the routes ----------

  async function me() {
    if (!config) config = await server("/api/config");
    return { ...config, ...db.profile, has_voice_clone: !!db.voice, sign_in: false, email: null };
  }

  async function updateMe(patch) {
    if (!config) config = await server("/api/config");
    if (patch.name != null) db.profile.name = String(patch.name).trim().slice(0, 80);
    if (patch.feedback_language != null) {
      if (!config.languages[patch.feedback_language]) throw new Error("Unsupported language");
      db.profile.feedback_language = patch.feedback_language;
    }
    if (patch.interview_date != null) {
      if (patch.interview_date && !/^\d{4}-\d{2}-\d{2}$/.test(patch.interview_date)) {
        throw new Error("Interview date must look like 2026-11-20");
      }
      db.profile.interview_date = patch.interview_date || null;
    }
    save();
    return me();
  }

  function sessionDetail(id) {
    const s = db.sessions[id] || missing("That practice session");
    return { id: s.id, round_type: s.round_type, round_label: s.round_label, target_role: s.target_role,
      profile: s.profile, questions: questionsOf(id).map(questionJson), jd_text: s.jd_text, created_at: s.created_at };
  }

  async function createSession(form) {
    const r = await server("/api/prepare", { method: "POST", body: form });
    const id = uid();
    db.sessions[id] = { id, round_type: r.round_type, round_label: r.round_label, target_role: r.target_role,
      profile: r.profile, resume_text: r.resume_text, jd_text: r.jd_text, created_at: now() };
    r.questions.forEach((q, i) => {
      const qid = uid();
      db.questions[qid] = { id: qid, session_id: id, position: i, kind: "main", parent_id: null,
        text: q.text, why: q.why, sig: q.sig };
    });
    if (r.first_name && !db.profile.name) db.profile.name = r.first_name;
    save();
    return sessionDetail(id);
  }

  function questionDetail(id) {
    const q = db.questions[id] || missing("That question");
    const s = db.sessions[q.session_id];
    return { ...questionJson(q), session_id: s.id, target_role: s.target_role, round_label: s.round_label,
      history: attemptsOf(q.id).map((a) => attemptJson(a)), ...place(q) };
  }

  async function scoreAnswer(questionId, opts) {
    const q = db.questions[questionId] || missing("That question");
    const s = db.sessions[q.session_id];
    const body = opts.body;
    body.set("context", JSON.stringify({ question: q.text, why: q.why, kind: q.kind, round_type: s.round_type,
      target_role: s.target_role || "", requirements: s.profile.jd_requirements || [], skills: s.profile.skills || [],
      resume_text: s.resume_text, feedback_language: db.profile.feedback_language }));
    const r = await server("/api/score", { method: "POST", body, headers: opts.headers });
    const a = { id: uid(), question_id: q.id, number: attemptsOf(q.id).length + 1, transcript: r.transcript,
      duration_s: r.duration_s, acoustics: r.acoustics, language: r.language, scores: r.scores, content: r.content,
      rewrite: r.rewrite, tips: r.tips, native_feedback: r.native_feedback, feedback_language: r.feedback_language,
      notes: r.notes, created_at: now(), sigs: r.signatures };
    db.attempts[a.id] = a;
    let follow = followOf(q.id);
    if (r.follow_up && q.kind === "main" && !follow) {
      follow = { id: uid(), session_id: s.id, position: q.position, kind: "follow_up", parent_id: q.id,
        text: r.follow_up, why: "Follow-up on what you just said.", sig: r.signatures.follow_up };
      db.questions[follow.id] = follow;
    }
    save();
    return { ...attemptJson(a, previousOf(a)), follow_up_question_id: follow ? follow.id : null, timings: r.timings };
  }

  function attemptDetail(id) {
    const a = db.attempts[id] || missing("That answer");
    const q = db.questions[a.question_id];
    const s = db.sessions[q.session_id];
    const follow = followOf(q.id);
    return { ...attemptJson(a, previousOf(a)), question: { id: q.id, text: q.text, why: q.why, kind: q.kind, ...place(q) },
      session_id: s.id, round_label: s.round_label, follow_up_question_id: follow ? follow.id : null };
  }

  async function speak(cacheKey, request) {
    if (!clips.has(cacheKey)) {
      const res = await server("/api/speak", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request) });
      clips.set(cacheKey, await res.blob());
    }
    return new Response(clips.get(cacheKey), { headers: { "content-type": "audio/wav" } });
  }

  function progress() {
    const points = Object.values(db.attempts).sort((a, b) => a.created_at.localeCompare(b.created_at)).map((a) => {
      const q = db.questions[a.question_id], s = db.sessions[q.session_id];
      return { attempt_id: a.id, at: a.created_at, question: q.text.slice(0, 80), session_id: s.id, role: s.target_role,
        overall: a.scores.overall, content: a.scores.content, fluency: a.scores.fluency, delivery: a.scores.delivery,
        fillers_per_min: a.language?.fillers_per_min, wpm: a.language?.wpm, english_share: a.language?.english_share };
    });
    return { points, count: points.length };
  }

  async function handle(path, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    let m;
    if (path === "/api/me") {
      if (method === "PATCH") return updateMe(JSON.parse(opts.body));
      if (method === "DELETE") { localStorage.removeItem(KEY); db = blank(); clips.clear(); return null; }
      return me();
    }
    if (path === "/api/sessions") {
      if (method === "POST") return createSession(opts.body);
      return Object.values(db.sessions).sort((a, b) => b.created_at.localeCompare(a.created_at))
        .map((s) => ({ id: s.id, round_type: s.round_type, target_role: s.target_role, created_at: s.created_at }));
    }
    if ((m = path.match(/^\/api\/sessions\/(\w+)$/))) return sessionDetail(m[1]);
    if ((m = path.match(/^\/api\/questions\/(\w+)$/))) return questionDetail(m[1]);
    if ((m = path.match(/^\/api\/questions\/(\w+)\/attempts$/))) return scoreAnswer(m[1], opts);
    if ((m = path.match(/^\/api\/questions\/(\w+)\/audio$/))) {
      const q = db.questions[m[1]] || missing("That question");
      return speak(`q/${q.id}`, { kind: "question", text: q.text, language: "en-IN", sig: q.sig });
    }
    if ((m = path.match(/^\/api\/attempts\/(\w+)$/))) return attemptDetail(m[1]);
    if ((m = path.match(/^\/api\/attempts\/(\w+)\/feedback-audio$/))) {
      const a = db.attempts[m[1]] || missing("That answer");
      return speak(`${a.id}/feedback`, { kind: "feedback", text: a.native_feedback, language: a.feedback_language,
        sig: a.sigs.feedback });
    }
    if ((m = path.match(/^\/api\/attempts\/(\w+)\/model-answer-audio$/))) {
      const a = db.attempts[m[1]] || missing("That answer");
      return speak(`${a.id}/answer/${db.voice ? "own" : "default"}`, { kind: "answer", text: a.rewrite,
        language: "en-IN", sig: a.sigs.answer, embedding: db.voice });
    }
    if (path === "/api/voice-clone") {
      if (method === "POST") {
        const r = await server("/api/voice-clone", { method: "POST", body: opts.body });
        db.voice = r.embedding;
      } else db.voice = null;
      save();
      for (const k of [...clips.keys()]) if (k.includes("/answer/")) clips.delete(k); // old clips were in the old voice
      return { has_voice_clone: !!db.voice };
    }
    if (path === "/api/progress") return progress();
    return server(path, opts);
  }

  return { handle };
})();
