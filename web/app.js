"use strict";
// Bol frontend: plain JS, hash routes, same screens as the MVP blueprint prototype.
// Setup → Interview → Feedback → Compare → Progress (+ My voice, Settings). All AI calls happen on the server.

const view = document.getElementById("view");
const MAX_SECONDS = 180;
const LANG_LABELS = {
  "hi-IN": "हिन्दी (Hindi)", "en-IN": "English", "ta-IN": "தமிழ் (Tamil)", "te-IN": "తెలుగు (Telugu)",
  "kn-IN": "ಕನ್ನಡ (Kannada)", "bn-IN": "বাংলা (Bengali)", "mr-IN": "मराठी (Marathi)", "gu-IN": "ગુજરાતી (Gujarati)",
  "ml-IN": "മലയാളം (Malayalam)", "pa-IN": "ਪੰਜਾਬੀ (Punjabi)",
};
const SHORT_ROUND = { technical: "project deep-dive", hr: "HR", behavioural: "behavioural", campus: "campus HR" };
const TAGS = "#GnaniAI #GreatIndianAIInternshipChallenge";
let me = null;
let cleanup = null; // stops mic, timers and audio when leaving a page
let nowPlaying = null;

// ---------- helpers ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const $ = (sel, root = view) => root.querySelector(sel);
const langName = (code) => LANG_LABELS[code] || me.languages[code] || code;
const hasVoice = (code) => code === "en-IN" || me.feedback_voices.includes(code);
const sign = (d) => (d > 0 ? "+" : d < 0 ? "−" : "±") + Math.abs(d);
const localDay = (iso) => { const d = new Date(iso); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const dayLabel = (day) => new Date(day + "T00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });

// ---------- data ----------
// No sign-in and no server-side storage: everything lives in this browser (web/store.js). api() keeps the same
// /api/... paths the pages always used; the store answers from local storage and calls the server only for work.

const api = (path, opts = {}) => Store.handle(path, opts);
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function flash(msg, where, kind = "") {
  const box = document.createElement("div");
  box.className = "alert " + kind;
  box.textContent = msg;
  (where || view).prepend(box);
  setTimeout(() => box.remove(), 9000);
}
const showError = (err, where) => flash(err.message || String(err), where, "bad");

async function saveMe(patch) {
  me = { ...me, ...(await api("/api/me", json("PATCH", patch))) };
  sidebar();
}

function sidebar(points) {
  const el = (id) => document.getElementById(id);
  el("plan-lang").textContent = "Feedback in " + langName(me.feedback_language).split(" (")[0];
  if (points) {
    const days = new Set(points.map((p) => localDay(p.at)));
    let streak = 0;
    const d = new Date();
    if (!days.has(localDay(d.toISOString()))) d.setDate(d.getDate() - 1); // today not practised yet: count from yesterday
    while (days.has(localDay(d.toISOString()))) { streak++; d.setDate(d.getDate() - 1); }
    el("plan-streak").textContent = streak;
    el("plan-streak-label").textContent = streak === 1 ? "day streak" : "days streak";
  }
  const left = me.interview_date
    ? Math.ceil((new Date(me.interview_date + "T00:00") - new Date(localDay(new Date().toISOString()) + "T00:00")) / 864e5) : null;
  if (left === null || left < 0) {
    el("plan-countdown").textContent = "–";
    el("plan-countdown-label").innerHTML = '<a href="#/settings">set interview date</a>';
  } else {
    el("plan-countdown").textContent = left;
    el("plan-countdown-label").textContent = left === 0 ? "interview today" : left === 1 ? "day to interview" : "days to interview";
  }
  const banner = document.getElementById("banner");
  const missing = [!me.claude && "ANTHROPIC_API_KEY (sample coaching)", !me.speech && "VACHANA_API_KEY (no speech or voices, type answers)"].filter(Boolean);
  const paused = [me.claude_paused && "this month's coaching budget is used up, so coaching is a sample",
    me.speech_paused && "speech-to-text is paused this month, type your answers",
    me.voices_paused && "voices are paused this month"].filter(Boolean);
  banner.hidden = !missing.length && !paused.length;
  banner.textContent = paused.length ? "Heads up: " + paused.join(" · ") : "Demo mode · not set: " + missing.join(" · ");
}
const refreshStreak = () => api("/api/progress").then((p) => sidebar(p.points)).catch(() => {});

function langOptions(selected) {
  const order = ["hi-IN", "en-IN", "ta-IN", "te-IN", "kn-IN", "bn-IN", "mr-IN", "gu-IN", "ml-IN", "pa-IN"];
  return order.filter((c) => me.languages[c]).map((c) =>
    `<option value="${c}" ${c === selected ? "selected" : ""}>${esc(langName(c))}${hasVoice(c) ? "" : " · text only"}</option>`).join("");
}

function fillerHtml(text, fillers) {
  let html = esc(text);
  for (const w of Object.keys(fillers || {}).sort((a, b) => b.length - a.length)) {
    const re = new RegExp(`(^|[^\\p{L}\\p{M}])(${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})(?=$|[^\\p{L}\\p{M}])`, "giu");
    html = html.replace(re, '$1<span class="filler">$2</span>');
  }
  return html;
}

// ---------- audio ----------

function encodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data");
  v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

async function toWav(blob) {
  const ctx = new AudioContext();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  await ctx.close();
  const rate = 16000;
  const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
  const src = offline.createBufferSource();
  src.buffer = decoded; src.connect(offline.destination); src.start();
  const rendered = await offline.startRendering();
  return encodeWav(rendered.getChannelData(0), rate);
}

// ---------- streamed speech ----------
// Gnani sends raw 16-bit mono PCM while it speaks; each piece is scheduled right after the previous one, so playback
// starts with the first piece instead of after the whole clip.

let playCtx = null;
function audioCtx() { // call from a click first: browsers only allow sound after a user gesture
  if (!playCtx) playCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (playCtx.state === "suspended") playCtx.resume().catch(() => {});
  return playCtx;
}

function makePcmPlayer(rate, onEnd) {
  const ctx = audioCtx();
  const LEAD = 0.25; // seconds buffered before starting, so a slow network doesn't stutter at once
  let at = 0, first = 0, started = false, finished = false, stopped = false, playing = 0, carry = null;
  let waiting = [], waitingSeconds = 0;
  const sources = [];
  const done = () => { if (!stopped) { stopped = true; onEnd?.(); } };
  const schedule = (floats) => {
    const buffer = ctx.createBuffer(1, floats.length, rate);
    buffer.copyToChannel(floats, 0);
    const src = ctx.createBufferSource();
    src.buffer = buffer; src.connect(ctx.destination);
    if (at < ctx.currentTime + 0.02) at = ctx.currentTime + 0.05;
    if (!first) first = at;
    src.start(at); at += buffer.duration; playing++;
    src.onended = () => { playing--; if (finished && !playing) done(); };
    sources.push(src);
  };
  const flush = () => { started = true; waiting.forEach(schedule); waiting = []; };
  return {
    push(bytes) {
      if (stopped) return;
      let data = bytes;
      if (carry) { data = new Uint8Array(carry.length + bytes.length); data.set(carry); data.set(bytes, carry.length); carry = null; }
      if (data.length % 2) { carry = data.slice(-1); data = data.slice(0, -1); }
      if (!data.length) return;
      const pcm = new Int16Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.length));
      const floats = new Float32Array(pcm.length);
      for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
      if (started) schedule(floats);
      else { waiting.push(floats); waitingSeconds += floats.length / rate; if (waitingSeconds >= LEAD) flush(); }
    },
    end() { if (finished) return; finished = true; if (!started) flush(); if (!playing) done(); },
    stop() { stopped = true; sources.forEach((s) => { try { s.stop(); } catch { /* not started */ } }); },
    progress() { return first && at > first ? Math.min(1, Math.max(0, (ctx.currentTime - first) / (at - first))) : 0; },
  };
}

// Plays a streamed audio response as it arrives. onEnd runs when the last piece has played.
function playPcm(res, rate, { onEnd, track } = {}) {
  let ticker = null;
  const player = makePcmPlayer(rate, () => { clearInterval(ticker); onEnd?.(); });
  const reader = res.body.getReader();
  if (track) ticker = setInterval(() => { track.style.width = (100 * player.progress()) + "%"; }, 120);
  (async () => {
    try {
      for (;;) { const { value, done } = await reader.read(); if (done) break; player.push(value); }
    } catch { /* stopped or dropped: play what arrived */ }
    player.end();
  })();
  return { stop() { player.stop(); reader.cancel().catch(() => {}); clearInterval(ticker); } };
}

// The feedback spoken while Claude writes it (see Store.scoreAnswer). It keeps playing when the page changes to
// the feedback screen, where the Listen button becomes its Stop button.
function liveVoice() {
  let player = null;
  const voice = {
    live: true, button: null, label: "",
    start(rate) { player = makePcmPlayer(rate, () => voice.finish()); },
    push(bytes) { player?.push(bytes); },
    end() { player ? player.end() : voice.finish(); },
    cancel() { player?.stop(); voice.finish(); },
    stop() { player?.stop(); voice.finish(); },
    finish() {
      if (voice.button) { voice.button.innerHTML = voice.label; voice.button.disabled = false; }
      if (nowPlaying === voice) nowPlaying = null;
    },
    attach(button) { // show "Stop" on the feedback page's Listen button while it plays
      voice.button = button; voice.label = button.innerHTML; button.innerHTML = "■ Stop"; button.disabled = false;
    },
  };
  nowPlaying?.stop();
  nowPlaying = voice;
  return voice;
}

// Mic recorder. Reports level (dBFS), long pauses and elapsed time while recording; onStop gets a 16 kHz WAV.
function makeRecorder({ maxSeconds, onTick, onStop }) {
  let stream, recorder, chunks, ctx, raf, started, tick;
  const stopAll = () => {
    cancelAnimationFrame(raf); clearInterval(tick);
    if (ctx) ctx.close().catch(() => {});
    if (stream) stream.getTracks().forEach((t) => t.stop());
  };
  async function start() {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      throw new Error("Bol needs microphone permission to hear your answer. You can type the answer instead.");
    }
    recorder = new MediaRecorder(stream); chunks = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = async () => {
      const seconds = (Date.now() - started) / 1000;
      stopAll();
      try { onStop(await toWav(new Blob(chunks, { type: recorder.mimeType })), seconds); }
      catch (err) { showError(err); }
    };
    ctx = new AudioContext();
    const analyser = ctx.createAnalyser(); analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const data = new Float32Array(analyser.fftSize);
    let floor = -50, heard = false, quietSince = null, pauses = 0, level = -90;
    const loop = () => {
      analyser.getFloatTimeDomainData(data);
      let sum = 0; for (const x of data) sum += x * x;
      level = 20 * Math.log10(Math.sqrt(sum / data.length) + 1e-9);
      floor = Math.min(floor + 0.02, level); // noise floor: follows quiet moments, rises slowly
      const speaking = level > Math.max(floor + 15, -55);
      const now = performance.now();
      if (speaking) {
        if (heard && quietSince && now - quietSince > 700) pauses++; // same 0.7 s rule as the server
        heard = true; quietSince = null;
      } else if (quietSince === null) quietSince = now;
      raf = requestAnimationFrame(loop);
    };
    loop();
    started = Date.now();
    tick = setInterval(() => {
      const s = (Date.now() - started) / 1000;
      onTick({ seconds: s, level, pauses });
      if (s >= maxSeconds) stop();
    }, 80);
    recorder.start();
  }
  function stop() { if (recorder && recorder.state === "recording") recorder.stop(); }
  const recording = () => !!recorder && recorder.state === "recording";
  return { start, stop, recording, abort: () => { if (recording()) { recorder.onstop = null; recorder.stop(); } stopAll(); } };
}

// Rolling bar waveform on a canvas (real mic level).
function makeWave(canvas) {
  const ctx = canvas.getContext("2d");
  const levels = new Array(60).fill(-90);
  const color = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function draw(active) {
    const w = canvas.width, h = canvas.height, bw = w / levels.length;
    ctx.clearRect(0, 0, w, h);
    if (active) { // live voice: a gradient sweep, matching the buttons
      const g = ctx.createLinearGradient(0, 0, w, 0);
      g.addColorStop(0, color("--accent")); g.addColorStop(1, color("--accent-2"));
      ctx.fillStyle = g;
    } else ctx.fillStyle = color("--line");
    levels.forEach((db, i) => {
      const amp = active ? Math.max(0.06, Math.min(1, (db + 60) / 50)) : 0.08;
      const bh = Math.max(3, amp * (h - 8));
      if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(i * bw + 1, (h - bh) / 2, bw - 3, bh, 2); ctx.fill(); }
      else ctx.fillRect(i * bw + 1, (h - bh) / 2, bw - 3, bh);
    });
  }
  draw(false);
  return { push(db) { levels.shift(); levels.push(db); draw(true); }, reset() { levels.fill(-90); draw(false); } };
}

// Play server audio (TTS). Clicking again stops it. Optional progress bar element.
async function play(url, button, track) {
  if (nowPlaying) {
    const was = nowPlaying.button; nowPlaying.stop();
    if (was === button) return;
  }
  const label = button.innerHTML;
  button.disabled = true; button.innerHTML = '<span class="spinner"></span> Loading';
  audioCtx(); // unlock sound while we're still inside the click
  try {
    const res = await api(url);
    const rate = Number(((res.headers.get("content-type") || "").match(/^audio\/pcm.*rate=(\d+)/) || [])[1]);
    if (rate) { // streaming from Gnani: starts with the first piece
      let handle = null;
      const stop = () => {
        handle?.stop(); button.innerHTML = label; button.disabled = false;
        if (track) track.style.width = "0%";
        if (nowPlaying?.button === button) nowPlaying = null;
      };
      handle = playPcm(res, rate, { onEnd: stop, track });
      nowPlaying = { stop, button };
      button.innerHTML = "■ Stop"; button.disabled = false;
      return;
    }
    const audio = new Audio(URL.createObjectURL(await res.blob()));
    const stop = () => {
      audio.pause(); button.innerHTML = label; button.disabled = false;
      if (track) track.style.width = "0%";
      nowPlaying = null;
    };
    nowPlaying = { stop, button };
    audio.ontimeupdate = () => { if (track && audio.duration) track.style.width = (100 * audio.currentTime / audio.duration) + "%"; };
    audio.onended = stop;
    button.innerHTML = "■ Stop"; button.disabled = false;
    await audio.play();
  } catch (err) {
    button.innerHTML = label; button.disabled = false; showError(err);
  }
}

// ---------- 1 Setup ----------

function matchCard(p) {
  if (!p) {
    return `<div class="card"><span class="label">Resume ↔ JD match</span>
      <div class="row between"><span class="big" style="font-size:2rem">–</span><span class="note">Waiting for your resume and JD</span></div>
      <div class="bar" aria-hidden="true"><b style="width:0%"></b></div>
      <p class="note">Bol reads both, shows which requirements your resume covers, and asks hardest about the gaps.</p>
      <span class="label" style="margin-top:8px">How a round works</span>
      <ol class="steps">
        <li><span><b>5 questions</b> from your own resume claims and the job's gaps</span></li>
        <li><span><b>Answer out loud</b>, Hinglish is fine; up to 3 minutes each</span></li>
        <li><span><b>Scores and tips</b> on content, fluency and delivery, in your language</span></li>
        <li><span><b>Hear a better answer</b> in your own cloned voice, then try again</span></li>
      </ol></div>`;
  }
  const covered = p.matched || [], gaps = p.gaps || [];
  const total = covered.length + gaps.length;
  const pct = total ? Math.round(100 * covered.length / total) : 0;
  const vague = (p.claims || []).filter((c) => c.strength === "vague").length;
  return `<div class="card"><span class="label">Resume ↔ JD match</span>
    <div class="row between"><span class="big" style="font-size:2rem">${total ? pct + "%" : "–"}</span>
      <span class="note">${covered.length} of ${total} requirements covered</span></div>
    <div class="bar" aria-hidden="true"><b style="width:${pct}%"></b></div>
    <div class="match" style="display:grid;gap:8px">
      <span class="note">Covered</span>
      <div class="pillset">${covered.map((m) => `<span class="pill ok">${esc(m)}</span>`).join("") || '<span class="note">None found</span>'}</div>
      <span class="note">Missing or weak</span>
      <div class="pillset">${gaps.map((m) => `<span class="pill miss">${esc(m)}</span>`).join("")}${vague ? `<span class="pill warn">${vague} claim${vague > 1 ? "s" : ""} without numbers</span>` : ""}${!gaps.length && !vague ? '<span class="note">No obvious gaps</span>' : ""}</div>
    </div>
    ${p.resume_tips?.length ? `<span class="label">Resume fixes for this job</span><ul class="list">${p.resume_tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
    <p class="note">Bol will ask hardest about your weak spots.</p></div>`;
}

async function setupPage() {
  const sessions = await api("/api/sessions");
  const rounds = Object.entries(me.rounds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  view.innerHTML = `
    <div><h1>New practice session</h1><p class="note">Bol builds questions from your resume and the job you're targeting.</p></div>
    <form class="grid2" id="setup">
      <div class="card">
        <div class="field"><span>Resume</span>
          <label class="drop" id="drop"><input type="file" name="resume_file" accept="application/pdf">
            <span class="file-pill" id="file-name">Choose PDF</span><span class="note" id="file-note">up to 3 pages · or drop it here</span></label>
          <button type="button" class="linkbtn" id="paste-toggle">or paste resume text</button>
          <textarea name="resume_text" id="resume-text" hidden placeholder="Paste your resume here"></textarea>
        </div>
        <div class="field"><label for="jd">Job description</label>
          <textarea id="jd" name="jd_text" required placeholder="Paste the job description from the placement portal or careers page"></textarea></div>
        <div class="field"><label for="role">Role</label><input id="role" name="target_role" placeholder="e.g. SDE-1, Backend"></div>
        <div class="grid2">
          <div class="field"><label for="round">Round</label><select id="round" name="round_type">${rounds}</select></div>
          <div class="field"><label for="lang">Feedback language</label><select id="lang">${langOptions(me.feedback_language)}</select></div>
        </div>
        <p class="note">Phone numbers, emails and links are removed before analysis. Your practice is saved only in this browser.</p>
      </div>
      ${matchCard(null)}
    </form>
    <div class="row"><button class="btn primary" form="setup" type="submit" id="go">Read my resume and JD</button><span class="note" id="status">Takes about 10 seconds</span></div>
    ${sessions.length ? `<div class="card"><span class="label">Earlier sessions</span><ul class="qlist">${sessions.map((s, i) => `
      <li><span class="n">${sessions.length - i}</span><span class="q"><b>${esc(s.target_role || "Practice")}</b>
        <span class="note"> · ${esc(me.rounds[s.round_type] || s.round_type)} · ${new Date(s.created_at).toLocaleDateString()}</span></span>
        <a class="btn small" href="#/s/${s.id}">Open</a></li>`).join("")}</ul></div>` : ""}`;

  const file = $("input[type=file]"), drop = $("#drop");
  file.onchange = () => {
    const f = file.files[0];
    $("#file-name").textContent = f ? f.name : "Choose PDF";
    $("#file-note").textContent = f ? `${Math.round(f.size / 1024)} KB · ready` : "up to 3 pages · or drop it here";
  };
  ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files.length) { file.files = e.dataTransfer.files; file.onchange(); } });
  $("#paste-toggle").onclick = () => { const t = $("#resume-text"); t.hidden = !t.hidden; if (!t.hidden) t.focus(); };
  $("#lang").onchange = (e) => saveMe({ feedback_language: e.target.value }).catch(showError);
  $("#setup").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#go"), status = $("#status");
    btn.disabled = true; status.innerHTML = '<span class="spinner"></span> Reading your resume and the job…';
    try {
      const s = await api("/api/sessions", { method: "POST", body: new FormData(e.target) });
      location.hash = `#/s/${s.id}`;
    } catch (err) { showError(err); btn.disabled = false; status.textContent = "Takes about 10 seconds"; }
  };
}

async function sessionPage(id) {
  const s = await api(`/api/sessions/${id}`);
  const p = s.profile;
  const mains = s.questions.filter((q) => q.kind === "main");
  const next = s.questions.find((q) => !q.attempts) || s.questions[0];
  const done = s.questions.filter((q) => q.attempts).length;
  view.innerHTML = `
    <div class="row between"><div><h1>${esc(s.target_role || "Practice session")}</h1>
      <p class="note">${esc(s.round_label)} · ${done} of ${s.questions.length} answered · created ${new Date(s.created_at).toLocaleDateString()}</p></div>
      <a class="btn" href="#/">+ New session</a></div>
    ${(p.notes || []).map((n) => `<div class="alert">${esc(n)}</div>`).join("")}
    <div class="grid2">
      <div class="card">
        <div class="field"><span>Resume</span>
          <div class="drop" style="cursor:default"><span class="file-pill">resume · parsed</span>
            <span class="note">${(p.skills || []).length} skills · ${(p.claims || []).length} claims found${p.source === "claude" ? "" : " · template mode"}${p.usage?.usd != null && p.usage.calls ? ` · Claude ≈ $${p.usage.usd.toFixed(4)}` : ""}</span></div></div>
        <div class="field"><label for="jd-view">Job description</label><textarea id="jd-view" readonly>${esc(s.jd_text)}</textarea></div>
        <div class="grid2">
          <div class="field"><label>Round</label><select disabled><option>${esc(s.round_label)}</option></select></div>
          <div class="field"><label for="lang">Feedback language</label><select id="lang">${langOptions(me.feedback_language)}</select></div>
        </div>
      </div>
      ${matchCard(p)}
    </div>
    <div class="row"><a class="btn primary" href="#/q/${next.id}">${done ? "Continue interview" : "Start interview"}</a>
      <span class="note">About ${mains.length * 2} minutes · ${mains.length} questions</span></div>
    <div class="card"><span class="label">Your questions</span><ul class="qlist">${s.questions.map((q) => `
      <li class="${q.kind === "follow_up" ? "follow" : ""}"><span class="n">${q.kind === "follow_up" ? "↳" : q.position + 1}</span>
        <span class="q">${esc(q.text)}<br><span class="note">${esc(q.why)}</span></span>
        <span class="row" style="gap:8px;flex-wrap:nowrap">${q.attempts ? `<span class="pill ${q.best >= 70 ? "ok" : q.best >= 45 ? "warn" : "miss"}">${q.best}</span>` : ""}
          <a class="btn small" href="#/q/${q.id}">${q.attempts ? "Retry" : "Answer"}</a></span></li>`).join("")}</ul></div>`;
  $("#lang").onchange = (e) => saveMe({ feedback_language: e.target.value }).catch(showError);
}

// ---------- 2 Interview ----------

async function interviewPage(id) {
  const q = await api(`/api/questions/${id}`);
  const roundShort = SHORT_ROUND[Object.keys(me.rounds).find((k) => me.rounds[k] === q.round_label)] || q.round_label;
  view.innerHTML = `
    <div class="row between">
      <span class="label">${q.kind === "follow_up" ? `Follow-up to question ${q.number}` : `Question ${q.number} of ${q.total}`} · ${esc(roundShort)}${q.attempts ? ` · attempt ${q.attempts + 1}` : ""}</span>
      <span class="status" id="status"><span class="led"></span><span id="status-text">Ready</span></span>
    </div>
    <div class="q-card">
      <div class="who"><span class="avatar">AI</span>Interviewer · voice: ${esc(me.interviewer_voice)} (Timbre)
        <button class="btn small" id="hear" ${me.speech ? "" : "disabled title='Needs VACHANA_API_KEY'"}>▶ Hear question</button></div>
      <p class="q">${esc(q.text)}</p>
      <p class="note">Asked because: ${esc(q.why)}</p>
    </div>
    <div class="live">
      <div class="card">
        <canvas class="wave" id="wave" width="600" height="72" aria-label="Voice level"></canvas>
        <div class="transcript" id="transcript" aria-live="polite">
          <p class="note">Your words appear here when you end the answer. Pause silently instead of saying "umm".</p>
        </div>
      </div>
      <div class="card meters">
        <div class="meter"><div class="top"><span>Volume</span><span class="val" id="m-vol">–</span></div><div class="bar"><b id="b-vol" style="width:0%"></b></div></div>
        <div class="meter"><div class="top"><span>Pace</span><span class="val" id="m-pace">after answer</span></div><div class="bar"><b id="b-pace" style="width:0%"></b></div></div>
        <div class="meter"><div class="top"><span>Filler words</span><span class="val" id="m-fill">after answer</span></div></div>
        <div class="meter"><div class="top"><span>Long pauses (&gt;0.7 s)</span><span class="val" id="m-pause">0</span></div></div>
        <div class="meter"><div class="top"><span>Elapsed</span><span class="val" id="m-time">0:00 / ${fmtTime(MAX_SECONDS)}</span></div></div>
      </div>
    </div>
    <div class="row">
      <button class="btn rec" id="btn-start">● Start answer</button>
      <button class="btn" id="btn-end" disabled>End answer</button>
      <button class="btn" id="btn-type">Type instead</button>
      <span class="note">Your audio is analysed and never stored.</span>
    </div>
    <form class="card" id="typed" hidden>
      <div class="field"><label for="answer">Your answer</label><textarea id="answer" name="text" maxlength="3000" placeholder="Type what you would say"></textarea></div>
      <p class="note">Typed answers get content and language feedback, but no pace or voice feedback.</p>
      <div class="row"><button class="btn primary" type="submit">Get feedback</button></div>
    </form>
    ${q.history.length ? `<p class="note"><a href="#/a/${q.last_attempt_id}">See feedback on your last attempt</a></p>` : ""}`;

  const status = $("#status"), statusText = $("#status-text");
  const setStatus = (text, cls) => { statusText.textContent = text; status.className = "status " + (cls || ""); };
  const wave = makeWave($("#wave"));
  const start = $("#btn-start"), end = $("#btn-end");

  $("#hear").onclick = (e) => play(`/api/questions/${id}/audio`, e.currentTarget);
  $("#btn-type").onclick = () => { $("#typed").hidden = false; $("#answer").focus(); };

  const submit = async (body, spoken) => {
    start.disabled = end.disabled = true;
    setStatus("Scoring…", "busy");
    $("#transcript").innerHTML = `<p class="note"><span class="spinner"></span> ${spoken ? "Prisma is transcribing, then your answer is scored…" : "Scoring your answer…"}</p>`;
    // Only if the student switched it on in Settings: the feedback is read aloud while Claude writes it. Otherwise
    // nothing plays until they press Listen (which streams, so it starts in about a second).
    const voice = me.speech && !me.voices_paused && me.autoplay_feedback === true && hasVoice(me.feedback_language)
      ? liveVoice() : null;
    const live = {
      voice,
      onTranscript: (text) => {
        setStatus(voice ? "Coaching · your feedback is starting" : "Coaching…", "busy");
        $("#transcript").innerHTML = `<span class="label">${spoken ? "What you said · Gnani Prisma" : "What you typed"}</span>
          <div class="seg">${esc(text)}</div>
          <p class="note"><span class="spinner"></span> Claude is scoring your answer${voice ? " and your coach is about to speak" : ""}…</p>`;
      },
    };
    // The same key on a retry tells the server it's the same answer, so it's never scored (or paid for) twice.
    const send = (key) => api(`/api/questions/${id}/attempts`, { method: "POST", body, headers: { "Idempotency-Key": key }, live });
    try {
      const key = crypto.randomUUID();
      let a;
      try { a = await send(key); } catch (err) {
        if (!(err instanceof TypeError)) throw err; // network dropped: send once more
        a = await send(key);
      }
      refreshStreak();
      location.hash = `#/a/${a.id}`;
    } catch (err) {
      voice?.cancel();
      showError(err);
      setStatus("Ready"); start.disabled = false;
      $("#transcript").innerHTML = '<p class="note">Try again, or type your answer.</p>';
    }
  };

  const rec = makeRecorder({
    maxSeconds: MAX_SECONDS,
    onTick: ({ seconds, level, pauses }) => {
      wave.push(level);
      const pct = Math.max(0, Math.min(100, (level + 60) * 1.8));
      $("#b-vol").style.width = pct + "%";
      $("#b-vol").style.background = level < -45 ? "var(--warn)" : "var(--accent)";
      $("#m-vol").textContent = level < -50 ? "silent" : level < -40 ? "too quiet" : level > -8 ? "very loud" : "good";
      $("#m-pause").textContent = pauses;
      $("#m-time").textContent = `${fmtTime(seconds)} / ${fmtTime(MAX_SECONDS)}`;
    },
    onStop: (wav, seconds) => {
      wave.reset();
      if (seconds < 3) { setStatus("Ready"); start.disabled = false; end.disabled = true; flash("That was very short. Answer for at least a few seconds."); return; }
      const fd = new FormData(); fd.append("audio_file", wav, "answer.wav"); submit(fd, true);
    },
  });
  start.onclick = async () => {
    try {
      await rec.start();
      setStatus("Listening", "on"); start.disabled = true; end.disabled = false;
    } catch (err) { showError(err); }
  };
  // audioCtx() inside the click: the feedback voice starts later, and browsers only allow sound after a gesture.
  end.onclick = () => { audioCtx(); end.disabled = true; rec.stop(); };
  $("#typed").onsubmit = (e) => { e.preventDefault(); audioCtx(); submit(new FormData(e.target), false); };
  cleanup = () => rec.abort();
}

// ---------- 3 Feedback ----------

function whatToFix(a) {
  const L = a.language, items = [];
  for (const n of L.delivery_notes || []) items.push([n.startsWith("Good") ? "good" : "", n]);
  for (const n of L.fluency_notes || []) items.push(["", n]);
  (a.tips || []).forEach((t, i) => items.unshift([i === 0 ? "bad" : "", t]));
  if (a.content.strength) items.push(["good", "Good: " + a.content.strength.replace(/^Good:\s*/i, "")]);
  return items.map(([cls, t]) => `<li class="${cls}">${esc(t)}</li>`).join("");
}

async function feedbackPage(id) {
  const a = await api(`/api/attempts/${id}`);
  const sc = a.scores, L = a.language, ac = a.acoustics || {};
  const content12 = ["answered", "structure", "specifics", "jd_fit"].reduce((s, k) => s + (a.content[k]?.score || 0), 0);
  const crit = [["answered", "Answered the question"], ["structure", "Structure (STAR)"], ["specifics", "Specifics and numbers"], ["jd_fit", "Fit to the job"]];
  const delta = a.previous && sc.overall != null && a.previous.scores.overall != null ? sc.overall - a.previous.scores.overall : null;
  const sub = (label, value, text) => `<div class="card sub"><span class="label">${label}</span><span class="n">${value ?? "–"}${text || ""}</span>
    <div class="bar"><b style="width:${value ?? 0}%"></b></div></div>`;
  const nextQ = a.question.next_question_id;
  view.innerHTML = `
    <div class="row between"><span class="label">Attempt ${a.number} · feedback · question ${a.question.number} of ${a.question.total}</span>
      <a class="note" href="#/s/${a.session_id}">All questions</a></div>
    <h2>${esc(a.question.text)}</h2>
    ${(a.notes || []).map((n) => `<div class="alert">${esc(n)}</div>`).join("")}
    <div class="card"><span class="label">${ac.speech_s ? "What you said · transcribed by Gnani Prisma" : "What you typed"}</span>
      <div class="seg">${fillerHtml(a.transcript, L.fillers)}</div>
      ${L.filler_count ? `<p class="note">${L.filler_count} filler${L.filler_count > 1 ? "s" : ""} highlighted.</p>` : ""}</div>
    <div class="score-row">
      <div class="card"><span class="label">Overall</span><span class="big">${sc.overall ?? "–"}</span>
        <span class="note">out of 100${delta != null ? ` · <b class="${delta >= 0 ? "up" : "down"}">${sign(delta)}</b> vs attempt ${a.previous.number}` : ""}</span></div>
      <div class="card sub"><span class="label">Content</span><span class="n">${content12} / 12</span><div class="bar"><b style="width:${sc.content ?? 0}%"></b></div></div>
      ${sub("Fluency", sc.fluency)}
      ${sc.delivery == null ? `<div class="card sub"><span class="label">Delivery</span><span class="n">–</span><span class="note">typed answer, nothing to measure</span></div>` : sub("Delivery", sc.delivery)}
    </div>
    <p class="note mono">${[L.wpm && `${L.wpm} wpm`, L.fillers_per_min != null && `${L.fillers_per_min} fillers/min`, ac.long_pauses != null && `${ac.long_pauses} long pauses`,
      ac.trailing_off_db != null && `${ac.trailing_off_db} dB end-of-sentence drop`, L.english_share != null && `${Math.round(L.english_share)}% English words`,
      L.hesitations ? `${L.hesitations} umm/uh heard` : "",
      a.content.usage?.calls ? `Claude ${(a.content.usage.input_tokens + a.content.usage.output_tokens).toLocaleString()} tokens${a.content.usage.usd != null ? ` ≈ $${a.content.usage.usd.toFixed(4)}` : ""}` : ""].filter(Boolean).join(" · ")}</p>
    <div class="grid2">
      <div class="card">
        <span class="label">What to fix</span>
        <ul class="list">${whatToFix(a)}</ul>
        ${L.indian_english?.length ? `<span class="label">Indian-English</span>${L.indian_english.map((h) =>
          `<p class="fix" title="${esc(h.why)}"><s>${esc(h.said)}</s> → <ins>${esc(h.better)}</ins></p>`).join("")}` : ""}
        <span class="label">Content rubric</span>
        <table class="mini"><tbody>${crit.map(([k, label]) => `<tr><td>${label}<br><span class="note">${esc(a.content[k].reason)}</span></td>
          <td class="num">${a.content[k].score} / 3</td></tr>`).join("")}</tbody></table>
      </div>
      <div class="card">
        <span class="label">Feedback in ${esc(langName(a.feedback_language).split(" (").pop().replace(")", ""))}${hasVoice(a.feedback_language) ? " (spoken by Timbre)" : ""}</span>
        <p class="native" lang="${a.feedback_language.slice(0, 2)}">${esc(a.native_feedback)}</p>
        <div class="row"><button class="btn small" id="hear-fb" ${me.speech && hasVoice(a.feedback_language) ? "" : "disabled"}>▶ Listen</button>
          ${me.speech ? (hasVoice(a.feedback_language) ? "" : '<span class="note">No voice for this language yet</span>') : '<span class="note">Voices need VACHANA_API_KEY</span>'}</div>
        <span class="label">Better answer</span>
        <p class="quote">“${esc(a.rewrite).replace(/\[([^\]]+)\]/g, '<span class="ph">[$1]</span>')}”</p>
        ${a.content.unverified?.length ? `<p class="note">Check before you use it: ${a.content.unverified.map(esc).join(", ")}.</p>` : ""}
        <div class="player">
          <button class="btn primary" id="hear-model" ${me.speech ? "" : "disabled"} aria-describedby="voice-note">▶ ${me.has_voice_clone ? "Hear it in your voice" : "Hear it"}</button>
          <div class="track" aria-hidden="true"><b id="voice-track"></b></div>
        </div>
        <p class="note" id="voice-note">${me.has_voice_clone ? "Uses your voice sample (with your consent)." : `Plays in the interviewer's voice. <a href="#/voice">Add your voice sample</a> to hear yourself say it.`}
          Fill in anything in [brackets] with your real details.</p>
      </div>
    </div>
    <div class="row">
      <a class="btn primary" href="#/q/${a.question.id}">Try again</a>
      ${a.previous ? `<a class="btn" href="#/c/${a.id}">Compare with attempt ${a.previous.number}</a>` : ""}
      ${a.follow_up_question_id ? `<a class="btn" href="#/q/${a.follow_up_question_id}">Answer the follow-up</a>` : ""}
      ${nextQ ? `<a class="btn" href="#/q/${nextQ}">Next question</a>` : `<a class="btn" href="#/progress">See progress</a>`}
    </div>`;
  $("#hear-fb").onclick = (e) => play(`/api/attempts/${id}/feedback-audio`, e.currentTarget);
  $("#hear-model").onclick = (e) => play(`/api/attempts/${id}/model-answer-audio`, e.currentTarget, $("#voice-track"));
  if (nowPlaying?.attach) nowPlaying.attach($("#hear-fb")); // the coach is still speaking this feedback
}

// ---------- 4 Compare ----------

async function comparePage(id) {
  const b = await api(`/api/attempts/${id}`);
  if (!b.previous) { location.hash = `#/a/${id}`; return; }
  const a = await api(`/api/attempts/${b.previous.id}`);
  const c12 = (x) => ["answered", "structure", "specifics", "jd_fit"].reduce((s, k) => s + (x.content[k]?.score || 0), 0);
  const inPace = (w) => w >= 120 && w <= 160;
  // [label, value A, value B, format, which direction is better]
  const rows = [
    ["Overall", a.scores.overall, b.scores.overall, (v) => v, "up"],
    ["Content rubric", c12(a), c12(b), (v) => `${v} / 12`, "up"],
    ["Fillers per minute", a.language.fillers_per_min, b.language.fillers_per_min, (v) => v.toFixed(1), "down"],
    ["Pace (wpm)", a.language.wpm, b.language.wpm, (v) => v, "pace"],
    ["Long pauses", a.acoustics?.long_pauses, b.acoustics?.long_pauses, (v) => v, "down"],
    ["End-of-sentence drop", a.acoustics?.trailing_off_db, b.acoustics?.trailing_off_db, (v) => `${v} dB`, "down"],
    ["English words", a.language.english_share, b.language.english_share, (v) => `${Math.round(v)}%`, "up"],
  ].filter(([, x, y]) => x != null && y != null);
  const change = ([, x, y, f, dir]) => {
    if (dir === "pace") return inPace(y) ? `<span class="up">in range</span>` : `<span class="${Math.abs(y - 140) < Math.abs(x - 140) ? "up" : "down"}">${sign(y - x)}</span>`;
    const d = Math.round((y - x) * 10) / 10;
    const better = dir === "up" ? d > 0 : d < 0;
    return d === 0 ? "same" : `<span class="${better ? "up" : "down"}">${sign(d)}</span>`;
  };
  const delta = (b.scores.overall ?? 0) - (a.scores.overall ?? 0);
  const shareText = `Same interview question, two attempts on Bol: ${a.scores.overall} → ${b.scores.overall}.` +
    (a.language.fillers_per_min != null && b.language.fillers_per_min != null ? ` Fillers ${a.language.fillers_per_min} → ${b.language.fillers_per_min} per minute.` : "") + ` ${TAGS}`;
  view.innerHTML = `
    <div class="row between"><span class="label">Same question · two attempts</span><span class="delta ${delta >= 0 ? "up" : "down"}">${sign(delta)}</span></div>
    <h2>${esc(b.question.text)}</h2>
    <div class="cmp">
      <div class="card"><span class="label">Attempt ${a.number} · ${a.scores.overall ?? "–"}</span><p style="font-size:.92rem">${fillerHtml(a.transcript, a.language.fillers)}</p></div>
      <div class="card"><span class="label">Attempt ${b.number} · ${b.scores.overall ?? "–"}</span><p style="font-size:.92rem">${fillerHtml(b.transcript, b.language.fillers)}</p></div>
    </div>
    <div class="card"><table class="mini">
      <thead><tr><th>Measure</th><th class="num">Attempt ${a.number}</th><th class="num">Attempt ${b.number}</th><th class="num">Change</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td>${r[0]}</td><td class="num">${r[3](r[1])}</td><td class="num">${r[3](r[2])}</td><td class="num">${change(r)}</td></tr>`).join("")}</tbody>
    </table></div>
    <div class="row"><a class="btn primary" href="#/progress">See progress</a><button class="btn" id="share">Share this improvement</button>
      <a class="btn" href="#/a/${b.id}">Back to feedback</a></div>`;
  $("#share").onclick = async (e) => {
    try { await navigator.clipboard.writeText(shareText); e.currentTarget.textContent = "Copied ✓"; }
    catch { flash(shareText); }
  };
}

// ---------- 5 Progress ----------

function chartSvg(days) {
  const x0 = 58, x1 = 395, y = (v) => 170 - (Math.max(40, v) - 40) * 2.5; // y: 40..100 → 170..20
  const xs = days.map((_, i) => (days.length === 1 ? (x0 + x1) / 2 : x0 + i * (x1 - x0) / (days.length - 1)));
  const pts = days.map((d, i) => `${xs[i]},${y(d.overall)}`);
  const last = days[days.length - 1];
  const every = Math.ceil(days.length / 7);
  return `<svg viewBox="0 0 420 200" role="img" aria-label="Overall score per day, latest ${last.overall}">
    <g font-family="IBM Plex Mono, monospace" font-size="10" fill="var(--muted)">
      ${[100, 80, 60, 40].map((v) => `<text x="30" y="${y(v) + 4}" text-anchor="end">${v}</text>`).join("")}</g>
    <g stroke="var(--line)" stroke-width="1">${[100, 80, 60, 40].map((v) => `<line x1="38" y1="${y(v)}" x2="410" y2="${y(v)}"/>`).join("")}</g>
    ${days.length > 1 ? `<path fill="var(--accent-soft)" d="M${xs[0]},170 L${pts.join(" L")} L${xs[xs.length - 1]},170 Z"/>
    <path fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linejoin="round" d="M${pts.join(" L")}"/>` : ""}
    ${days.map((d, i) => `<circle cx="${xs[i]}" cy="${y(d.overall)}" r="${i === days.length - 1 ? 5 : 3}" fill="var(--accent)"/>`).join("")}
    <g font-family="IBM Plex Mono, monospace" font-size="10" fill="var(--muted)" text-anchor="middle">
      ${days.map((d, i) => (i % every === 0 || i === days.length - 1 ? `<text x="${xs[i]}" y="190">${dayLabel(d.day)}</text>` : "")).join("")}</g>
    <text x="${xs[xs.length - 1] - 8}" y="${y(last.overall) - 10}" text-anchor="end" font-family="IBM Plex Mono, monospace" font-size="11" fill="var(--ink)">${last.overall}</text>
  </svg>`;
}

async function progressPage() {
  const { points } = await api("/api/progress");
  sidebar(points);
  const scored = points.filter((p) => p.overall != null);
  const byDay = {};
  for (const p of scored) (byDay[localDay(p.at)] ||= []).push(p);
  const avg = (xs) => (xs.length ? Math.round(xs.reduce((s, v) => s + v, 0) / xs.length * 10) / 10 : null);
  const days = Object.keys(byDay).sort().map((day) => ({
    day, overall: Math.round(avg(byDay[day].map((p) => p.overall))),
    fillers: avg(byDay[day].map((p) => p.fillers_per_min).filter((v) => v != null)),
  }));
  const interviewCard = () => {
    if (!me.interview_date) {
      return `<div class="card"><span class="label">Interview date</span><p>When is your interview? Bol counts down and suggests what to practise each day.</p>
        <div class="row"><input type="date" id="idate" class="btn" style="font-weight:400"><button class="btn primary" id="save-date">Save</button></div></div>`;
    }
    const left = Math.ceil((new Date(me.interview_date + "T00:00") - new Date(localDay(new Date().toISOString()) + "T00:00")) / 864e5);
    return `<div class="card"><span class="label">Interview date</span>
      <p>${dayLabel(me.interview_date)} · ${left > 0 ? `${left} day${left > 1 ? "s" : ""} left` : left === 0 ? "today, good luck!" : "done"}. Today: answer 3 questions, and retry the weakest one.</p>
      <div class="row"><a class="btn primary" href="#/">Start today's session</a><a class="linkbtn" href="#/settings">Change date</a></div></div>`;
  };

  if (!scored.length) {
    // Sessions without answers are common (questions generated, none answered yet): point to them, not a blank page.
    const sessions = await api("/api/sessions");
    const saved = sessions.length
      ? `<p>You have ${sessions.length} saved session${sessions.length > 1 ? "s" : ""} but no answers yet. Scores appear here once you answer a question.</p>
         <ul class="list">${sessions.slice(0, 5).map((s) => `<li class="good"><span><a href="#/s/${s.id}">${esc(s.target_role || "Practice session")}</a>
           <span class="note">· ${new Date(s.created_at).toLocaleDateString()}</span></span></li>`).join("")}</ul>`
      : `<p>Answer a few questions and your scores will show up here, day by day.</p>`;
    view.innerHTML = `<div class="row between"><h1>Your progress</h1></div>
      <div class="card">${saved}<a class="btn primary" href="#/" style="justify-self:start">Start practising</a>
        <p class="note">Practice is saved in this browser, for this web address only. If you practised on another device, browser or address, use Settings → Download a backup there and restore it here.</p></div>
      <div class="grid2">${interviewCard()}</div>`;
    bindDate();
    return;
  }
  const first = days[0], last = days[days.length - 1];
  const parts = ["content", "fluency", "delivery"].map((k) => [k, avg(scored.map((p) => p[k]).filter((v) => v != null))]).filter(([, v]) => v != null);
  const [weakKey, weakVal] = parts.sort((x, y) => x[1] - y[1])[0];
  const weakText = {
    content: "Answer content: structure (STAR), specifics and numbers, and linking to the job.",
    fluency: "Fluency: fillers, repeats and switching to Hindi mid-answer.",
    delivery: "Delivery: pace, long pauses, volume and ending sentences strongly.",
  }[weakKey];
  const weakest = scored.filter((p) => p[weakKey] != null).sort((x, y) => x[weakKey] - y[weakKey])[0];
  const fillersLine = first.fillers != null && last.fillers != null && days.length > 1 ? ` Fillers ${first.fillers} → ${last.fillers} per minute.` : "";
  const shareText = `${days.length} day${days.length > 1 ? "s" : ""} of interview practice on Bol: ${first.overall} → ${last.overall}.${fillersLine} ${TAGS}`;

  view.innerHTML = `
    <div class="row between"><h1>Your progress</h1><span class="note">${scored.length} answers · ${days.length} day${days.length > 1 ? "s" : ""}</span></div>
    <div class="grid2">
      <div class="card chart"><span class="label">Overall score per day · ${dayLabel(first.day)}${days.length > 1 ? `–${dayLabel(last.day)}` : ""}</span>${chartSvg(days)}</div>
      <div class="share">
        <span class="label">Shareable card</span>
        <span class="big">${first.overall} → ${last.overall}</span>
        <p>${days.length} day${days.length > 1 ? "s" : ""} of interview practice on Bol.${fillersLine}</p>
        <p class="mono" style="font-size:.8rem">${TAGS}</p>
        <button class="btn small" id="copy-share">Copy text</button>
      </div>
    </div>
    <div class="grid2">
      <div class="card"><span class="label">Weakest area</span><p>${weakText} Average ${Math.round(weakVal)} / 100.</p>
        <a class="btn" style="justify-self:start" href="#/a/${weakest.attempt_id}">Practise this</a></div>
      ${interviewCard()}
    </div>
    <div class="card"><span class="label">Every answer</span><table class="mini">
      <thead><tr><th>Date</th><th>Question</th><th class="num">Overall</th><th class="num">Fillers/min</th><th class="num">Pace</th></tr></thead>
      <tbody>${points.slice().reverse().map((p) => `<tr><td class="note">${new Date(p.at).toLocaleDateString()}</td>
        <td><a href="#/a/${p.attempt_id}">${esc(p.question)}</a></td><td class="num"><b>${p.overall ?? "–"}</b></td>
        <td class="num">${p.fillers_per_min ?? "–"}</td><td class="num">${p.wpm ?? "–"}</td></tr>`).join("")}</tbody></table></div>`;
  $("#copy-share").onclick = async (e) => {
    try { await navigator.clipboard.writeText(shareText); e.currentTarget.textContent = "Copied ✓"; } catch { flash(shareText); }
  };
  bindDate();
}

function bindDate() {
  const btn = $("#save-date");
  if (!btn) return;
  btn.onclick = async () => {
    const v = $("#idate").value;
    if (!v) { flash("Pick a date first."); return; }
    try { await saveMe({ interview_date: v }); route(); } catch (err) { showError(err); }
  };
}

// ---------- My voice ----------

const VOICE_SCRIPT = "Hello, my name is ____. I am in my final year of engineering. In my last project, I built a small " +
  "web app with my team. I handled the backend and the database. I enjoy solving problems step by step, and I am " +
  "working every day to speak more clearly and confidently.";

async function voicePage() {
  if (me.has_voice_clone) {
    view.innerHTML = `
      <div><h1>Your voice</h1><p class="note">Better answers play in your own voice, so you can hear yourself at your best.</p></div>
      <div class="grid2"><div class="card"><span class="label">Status</span><p><b>Your voice is set up.</b> On any feedback screen, press “Hear it in your voice”.</p>
        <button class="btn" id="del" style="justify-self:start">Delete my voice</button></div>
        <div class="card"><span class="label">How it's used</span><ul class="list"><li>Only to read your own better answers back to you.</li>
          <li>Never shared, never used for anything else.</li><li class="good">Delete it any time; it is gone straight away.</li></ul></div></div>`;
    $("#del").onclick = async () => {
      try { await api("/api/voice-clone", { method: "DELETE" }); me.has_voice_clone = false; voicePage(); } catch (err) { showError(err); }
    };
    return;
  }
  view.innerHTML = `
    <div><h1>Hear yourself say it</h1><p class="note">Record about 20 seconds of your own voice. Bol then reads your better answers in your voice.</p></div>
    <div class="grid2">
      <div class="card">
        <span class="label">Read this aloud (say your own name)</span>
        <p class="readme">${esc(VOICE_SCRIPT)}</p>
        <label class="check"><input type="checkbox" id="consent">
          <span>This is my own voice, and I agree to Bol making a copy of it for my practice. I can delete it any time.</span></label>
        <canvas class="wave" id="wave" width="600" height="60" aria-label="Voice level"></canvas>
        <div class="row between"><span class="status" id="status"><span class="led"></span><span id="status-text">${me.speech ? "Tick the box to start" : "Needs VACHANA_API_KEY"}</span></span>
          <span class="mono note" id="timer">0:00 / 0:30</span></div>
        <div class="row"><button class="btn rec" id="btn-start" disabled>● Start recording</button><button class="btn" id="btn-end" disabled>Stop</button></div>
      </div>
      <div class="card"><span class="label">How it's used</span><ul class="list">
        <li>Your recording goes to Gnani once to create your voice. Bol does not keep the recording.</li>
        <li>Only your own voice, only with your consent.</li>
        <li>Used only to read your better answers back to you.</li>
        <li class="good">Delete it any time from this page.</li></ul></div>
    </div>`;
  const start = $("#btn-start"), end = $("#btn-end"), status = $("#status"), statusText = $("#status-text");
  const wave = makeWave($("#wave"));
  $("#consent").onchange = (e) => {
    start.disabled = !(e.target.checked && me.speech);
    if (me.speech) statusText.textContent = e.target.checked ? "Ready" : "Tick the box to start";
  };
  const rec = makeRecorder({
    maxSeconds: 30,
    onTick: ({ seconds, level }) => { wave.push(level); $("#timer").textContent = `${fmtTime(seconds)} / 0:30`; },
    onStop: async (wav, seconds) => {
      wave.reset(); end.disabled = true;
      if (seconds < 6) { statusText.textContent = "Record at least 6 seconds"; status.className = "status"; start.disabled = false; return; }
      status.className = "status busy"; statusText.textContent = "Creating your voice…";
      const fd = new FormData(); fd.append("sample", wav, "sample.wav"); fd.append("consent", "true");
      try { await api("/api/voice-clone", { method: "POST", body: fd }); me.has_voice_clone = true; voicePage(); }
      catch (err) { showError(err); status.className = "status"; statusText.textContent = "Try again"; start.disabled = false; }
    },
  });
  start.onclick = async () => {
    try { await rec.start(); status.className = "status on"; statusText.textContent = "Recording"; start.disabled = true; end.disabled = false; }
    catch (err) { showError(err); }
  };
  end.onclick = () => rec.stop();
  cleanup = () => rec.abort();
}

// ---------- Settings ----------

async function settingsPage() {
  view.innerHTML = `
    <div><h1>Settings</h1><p class="note">Saved as soon as you change them.</p></div>
    <div class="grid2">
      <div class="card">
        <div class="field"><label for="name">Your name</label><input id="name" value="${esc(me.name)}" placeholder="First name"></div>
        <div class="field"><label for="lang">Feedback language</label><select id="lang">${langOptions(me.feedback_language)}</select></div>
        <div class="field"><label for="idate">Interview date</label><input type="date" id="idate" value="${me.interview_date || ""}"></div>
        <label class="check"><input type="checkbox" id="autospeak" ${me.autoplay_feedback === true ? "checked" : ""}>
          <span>Read my feedback aloud automatically as soon as I finish answering (otherwise press Listen)</span></label>
        <span class="note" id="saved">&nbsp;</span>
      </div>
      <div class="card"><span class="label">Services</span><dl class="kv">
        <dt>Coaching</dt><dd>${me.claude ? `Claude · <span class="mono">${esc(me.claude_model)}</span>` : "Sample coaching (no ANTHROPIC_API_KEY)"}</dd>
        <dt>Speech-to-text</dt><dd>${me.speech ? "Gnani Prisma" : "Off (no VACHANA_API_KEY)"}</dd>
        <dt>Voices</dt><dd>${me.speech ? `Gnani Timbre · interviewer ${esc(me.interviewer_voice)}` : "Off"}</dd>
        <dt>Your voice</dt><dd>${me.has_voice_clone ? "Set up" : "Not set up"} · <a href="#/voice">manage</a></dd>
      </dl><p class="note">API keys stay on the server, never in the browser.</p></div>
      <div class="card"><span class="label">Your data</span>
        <p>Everything stays in this browser: your sessions, answers, scores and voice. Bol's server keeps nothing, and there is no account.</p>
        <p class="note">Resumes and answers are sent to the server only while they're being analysed, then forgotten. Recordings are never saved anywhere.</p>
        <div class="row">
          <button class="btn" id="backup">Download a backup</button>
          <label class="btn" for="restore-file">Restore a backup</label>
          <input type="file" id="restore-file" accept=".json,application/json" hidden>
        </div>
        <p class="note">Another browser or device starts empty: download a backup here and restore it there. The file holds your practice and voice data, so keep it private.</p>
        <div class="row"><button class="btn" id="delete-me">Start fresh: delete everything</button></div>
        <p class="note">Clearing this browser's site data does the same.</p>
      </div>
    </div>`;
  $("#backup").onclick = () => {
    const link = Object.assign(document.createElement("a"), {
      href: URL.createObjectURL(Store.backup()), download: `bol-backup-${localDay(new Date().toISOString())}.json` });
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };
  $("#restore-file").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (!confirm("Replace the practice saved in this browser with this backup?")) { e.target.value = ""; return; }
    try {
      Store.restore(await file.text());
      me = null; location.hash = "#/progress"; route();
    } catch (err) { showError(err); e.target.value = ""; }
  };
  $("#delete-me").onclick = async () => {
    if (!confirm("Delete everything Bol saved in this browser? Every session, answer, score and your cloned voice will be removed. This can't be undone.")) return;
    try {
      await api("/api/me", { method: "DELETE" });
      me = null; location.hash = "#/"; route();
    } catch (err) { showError(err); }
  };
  const saved = () => { $("#saved").textContent = "Saved ✓"; setTimeout(() => { const s = $("#saved"); if (s) s.innerHTML = "&nbsp;"; }, 1500); };
  $("#name").onchange = (e) => saveMe({ name: e.target.value }).then(saved).catch(showError);
  $("#lang").onchange = (e) => saveMe({ feedback_language: e.target.value }).then(saved).catch(showError);
  $("#idate").onchange = (e) => saveMe({ interview_date: e.target.value }).then(saved).catch(showError);
  $("#autospeak").onchange = (e) => saveMe({ autoplay_feedback: e.target.checked }).then(saved).catch(showError);
}

// ---------- motion: page enter, bars filling, numbers counting up ----------
// Short and purposeful (about 300 ms), and skipped when the system asks for reduced motion.

const calm = matchMedia("(prefers-reduced-motion: reduce)");
// Grey placeholders in the shape of a page; they fade in only if loading takes more than 150 ms.
const SKELETON = `<div class="skeleton" aria-label="Loading"><span class="sk sk-title"></span>
  <div class="grid2"><span class="sk sk-card"></span><span class="sk sk-card"></span></div><span class="sk sk-wide"></span></div>`;

function countUp(el) {
  const m = el.textContent.trim().match(/^(\d{1,3})(%?)$/);
  if (!m || +m[1] === 0) return;
  const end = +m[1], suffix = m[2], start = performance.now(), ms = 700;
  const step = (now) => {
    const k = Math.min(1, (now - start) / ms);
    el.textContent = Math.round(end * (1 - Math.pow(1 - k, 3))) + suffix; // ease-out
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function animateIn() {
  void view.offsetWidth; // restart the CSS entrance on every page
  view.classList.add("enter");
  if (calm.matches) return;
  view.querySelectorAll(".bar > b").forEach((b) => {
    const target = b.style.width;
    if (!target || target === "0%") return;
    b.style.transition = "none"; b.style.width = "0%";
    void b.offsetWidth;
    b.style.transition = ""; b.style.width = target;
  });
  view.querySelectorAll(".big").forEach(countUp);
}

// ---------- router ----------

async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const [, page, id] = location.hash.split("/");
  // Leaving a page stops its audio, except the coach's live feedback on the way to its own feedback screen.
  if (nowPlaying && !(nowPlaying.live && !nowPlaying.button && page === "a")) nowPlaying.stop();
  const nav = { progress: "progress", voice: "voice", settings: "settings" }[page] || "practice";
  document.querySelectorAll(".side nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === nav));
  view.classList.remove("enter");
  view.innerHTML = SKELETON;
  try {
    if (!me) { me = await api("/api/me"); sidebar(); refreshStreak(); }
    if (page === "s") await sessionPage(id);
    else if (page === "q") await interviewPage(id);
    else if (page === "a") await feedbackPage(id);
    else if (page === "c") await comparePage(id);
    else if (page === "progress") await progressPage();
    else if (page === "voice") await voicePage();
    else if (page === "settings") await settingsPage();
    else await setupPage();
    animateIn();
    window.scrollTo(0, 0);
  } catch (err) {
    view.innerHTML = `<div class="card"><p>${esc(err.message)}</p><a class="btn" href="#/" style="justify-self:start">Go to Practice</a></div>`;
  }
}
window.addEventListener("hashchange", route);
route();
