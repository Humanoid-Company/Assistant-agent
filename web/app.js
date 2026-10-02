// Browser side: microphone + speaker go straight to OpenAI GPT-Live over WebRTC.
// The backend only turns our SDP offer into a session (the OpenAI key stays there) and runs
// the tools; Google sign-in is a popup to the backend.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  // Random id that keeps this browser's Google account separate from other testers'.
  let clientId = store.get("va-client-id");
  if (!clientId) {
    clientId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
    store.set("va-client-id", clientId);
  }
  let accessCode = store.get("va-access-code") || "";

  let pc = null;
  let channel = null;
  let mic = null;
  let gate = null;
  // One open bubble per speaker: user and assistant transcripts arrive interleaved when they
  // overlap, and a single "current" bubble shredded both into word fragments.
  const openMsg = { user: null, assistant: null };
  const lastDeltaAt = { user: 0, assistant: 0 };
  const TURN_PAUSE_MS = 1500;
  // Google account the current chat belongs to: undefined = not known yet.
  let chatEmail;

  const headers = () => ({
    "Content-Type": "application/json",
    "X-Client-Id": clientId,
    "X-Access-Code": accessCode,
  });

  function setStatus(text, state) {
    $("status").textContent = text;
    $("dot").className = "dot" + (state ? " " + state : "");
  }

  function addLine(role, text) {
    const empty = $("log").querySelector(".empty");
    if (empty) empty.remove();
    const p = document.createElement("p");
    p.className = "msg " + role;
    p.textContent = text;
    $("log").appendChild(p);
    $("log").scrollTop = $("log").scrollHeight;
    return p;
  }

  function appendTranscript(role, delta) {
    if (!delta) return;
    const now = Date.now();
    const other = role === "user" ? "assistant" : "user";
    // A new turn starts after a pause, or once the other side has spoken since this bubble.
    if (!openMsg[role] || now - lastDeltaAt[role] > TURN_PAUSE_MS || lastDeltaAt[other] > lastDeltaAt[role] + TURN_PAUSE_MS) {
      openMsg[role] = addLine(role, "");
    }
    lastDeltaAt[role] = now;
    openMsg[role].textContent += delta;
    $("log").scrollTop = $("log").scrollHeight;
  }

  function closeBubbles() {
    openMsg.user = openMsg.assistant = null;
    lastDeltaAt.user = lastDeltaAt.assistant = 0;
  }

  function clearLog() {
    closeBubbles();
    $("log").replaceChildren();
    const empty = document.createElement("p");
    empty.className = "muted empty";
    empty.textContent = "Тут з'являтиметься текст розмови.";
    $("log").appendChild(empty);
  }

  // Noise gate between the mic and WebRTC: background sounds (typing, TV, people nearby)
  // never reach the model, only speech louder than the room's noise floor does. While the
  // assistant is audible the bar is higher, so leftover speaker echo doesn't interrupt it.
  // It also hears the assistant's own audio (2nd input) and reports to the page when the
  // user starts/stops talking and when the assistant is audible — that drives barge-in.
  // ?gate=off turns it off. It runs on the audio thread (AudioWorklet), so a background tab,
  // where timers are throttled to once a second, doesn't chop the user's speech.
  const GATE_WORKLET = `
    class NoiseGate extends AudioWorkletProcessor {
      constructor() {
        super();
        this.frame = Math.round(sampleRate * 0.02);       // judge loudness per 20 ms
        this.ring = new Float32Array(Math.round(sampleRate * 0.06)); // 60 ms look-ahead
        this.pos = 0; this.sum = 0; this.outSum = 0; this.count = 0;
        this.floor = 0.003; this.open = false; this.loud = 0; this.quietFor = 1;
        this.gain = 0;
        this.speaking = false;                            // user, short hangover (for barge-in)
        this.audible = false; this.outQuietFor = 1;       // assistant playback
      }
      judge(level, outLevel) {
        if (outLevel > 0.01) this.outQuietFor = 0; else this.outQuietFor += 0.02;
        const audible = this.outQuietFor < 0.3;
        if (audible !== this.audible) { this.audible = audible; this.port.postMessage({ audible }); }
        const threshold = Math.max(audible ? 0.035 : 0.012, this.floor * 3.5);
        if (level > threshold) { this.loud += 1; this.quietFor = 0; }
        else { this.quietFor += 0.02; if (level < threshold * 0.6) this.loud = 0; }
        if (!this.open && this.loud >= 2) this.open = true;
        else if (this.open && this.quietFor > 0.45) this.open = false;
        const speaking = this.speaking ? this.quietFor <= 0.16 : this.loud >= 3;
        if (speaking !== this.speaking) { this.speaking = speaking; this.port.postMessage({ speaking }); }
        // Learn the room's noise only while nobody speaks, so a long sentence can't raise it.
        if (!this.open && !audible) {
          this.floor = level < this.floor ? this.floor * 0.7 + level * 0.3 : this.floor + (level - this.floor) * 0.01;
        }
      }
      process(inputs, outputs) {
        const input = inputs[0][0];
        const remote = inputs[1] && inputs[1][0];
        const output = outputs[0][0];
        if (!input || !output) return true;
        for (let i = 0; i < input.length; i++) {
          const x = input[i];
          this.sum += x * x;
          if (remote) this.outSum += remote[i] * remote[i];
          if (++this.count >= this.frame) {
            this.judge(Math.sqrt(this.sum / this.count), Math.sqrt(this.outSum / this.count));
            this.sum = 0; this.outSum = 0; this.count = 0;
          }
          const delayed = this.ring[this.pos];
          this.ring[this.pos] = x;
          this.pos = (this.pos + 1) % this.ring.length;
          this.gain += ((this.open ? 1 : 0) - this.gain) * (this.open ? 0.004 : 0.0005);
          output[i] = delayed * this.gain;
        }
        for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(output);
        return true;
      }
    }
    registerProcessor("noise-gate", NoiseGate);
  `;

  async function createNoiseGate(stream, onSignal) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx || params.get("gate") === "off") return null;
    const ctx = new AudioCtx();
    try {
      if (!ctx.audioWorklet) throw new Error("no AudioWorklet");
      const url = URL.createObjectURL(new Blob([GATE_WORKLET], { type: "application/javascript" }));
      try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
      await ctx.resume();
      const node = new AudioWorkletNode(ctx, "noise-gate", {
        numberOfInputs: 2, channelCount: 1, channelCountMode: "explicit",
      });
      node.port.onmessage = (e) => onSignal(e.data);
      const out = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(stream).connect(node, 0, 0).connect(out);
      return {
        track: out.stream.getAudioTracks()[0],
        // The assistant's audio, measured only (never played from here).
        listenTo(remote) { ctx.createMediaStreamSource(remote).connect(node, 0, 1); },
        close() { ctx.close().catch(() => {}); },
      };
    } catch {
      ctx.close().catch(() => {});
      return null; // old browser: send the mic as is
    }
  }

  // Barge-in, like the desktop app: the Live model alone reacts slowly and keeps talking over
  // the user. Voice over the assistant → its audio is ducked at once; real words, a stop word
  // or ~0.8 s of voice → muted and the model is told to stop; a cough or «угу» → volume back.
  const STOP_WORDS = /(^|[^а-яіїєґ'])(стоп|зачекай|почекай|стривай|досить|хвилинку|секунду|stop|wait)([^а-яіїєґ']|$)/i;
  const BACKCHANNEL = new Set(["угу", "ага", "так", "ммм", "мм", "м", "ок", "окей", "добре", "ну", "ого", "ага-ага", "мгм", "ясно", "зрозуміло", "да"]);
  const barge = {
    audible: false,     // assistant audio is playing right now
    speaking: false,    // user voice right now
    stage: "idle",      // idle → ducked → muted
    heard: "",          // user words since this barge-in candidate started
    timers: [],
    releaseTimer: null,
    speechEndedAt: 0,
    recentAssistant: "", // for the echo check
  };

  function bargeClear() {
    barge.timers.forEach(clearTimeout);
    barge.timers = [];
  }

  function setRemoteVolume(volume) {
    const el = $("remote");
    el.volume = volume;
    el.muted = volume === 0;
  }

  function bargeReset() {
    bargeClear();
    clearTimeout(barge.releaseTimer);
    barge.stage = "idle";
    barge.heard = "";
    barge.speaking = false;
    barge.audible = false;
    setRemoteVolume(1);
  }

  function steer(kind) {
    const content = {
      ack: "Stop your previous answer immediately. Do not continue or resume it. Reply with at most one short acknowledgement such as «Добре.» Then wait silently for the user.",
      stop: "Stop speaking immediately. The user is talking. Do not finish or resume your previous sentence. Listen and answer what they say.",
      unsure: "Pause — the user may be starting to talk. Listen. If they say something to you, answer that. If nobody actually spoke to you (noise, a cough, your own voice echoing), continue your previous answer from where you stopped, without repeating it from the beginning.",
    }[kind];
    try {
      if (channel && channel.readyState === "open") {
        channel.send(JSON.stringify({
          type: "session.instructions.append",
          content,
          delegation_id: null,
          event_id: "barge_" + Math.random().toString(16).slice(2, 10),
        }));
      }
    } catch { /* channel closing */ }
  }

  function bargeConfirm(kind) {
    if (barge.stage === "muted") return;
    bargeClear();
    barge.stage = "muted";
    setRemoteVolume(0);
    steer(kind);
    scheduleRelease();
  }

  // Unmute once the interrupted answer has stopped (assistant quiet) and the user is done, or
  // at the latest 1.5 s after the user stopped — the model may go straight into a new reply.
  function scheduleRelease() {
    clearTimeout(barge.releaseTimer);
    if (barge.stage !== "muted") return;
    if (barge.speaking) return; // re-checked when the user stops
    const sinceSpeech = Date.now() - barge.speechEndedAt;
    if (!barge.audible || sinceSpeech > 1500) {
      barge.stage = "idle";
      barge.heard = "";
      setRemoteVolume(1);
      return;
    }
    barge.releaseTimer = setTimeout(scheduleRelease, 100);
  }

  function onGateSignal(signal) {
    if ("audible" in signal) {
      barge.audible = signal.audible;
      if (barge.stage === "muted") scheduleRelease();
      return;
    }
    barge.speaking = signal.speaking;
    if (signal.speaking) {
      clearTimeout(barge.releaseTimer);
      if (barge.stage !== "idle" || !barge.audible) return;
      barge.heard = "";
      barge.timers.push(setTimeout(() => {
        if (barge.speaking && barge.stage === "idle") { barge.stage = "ducked"; setRemoteVolume(0.2); }
      }, 250));
      barge.timers.push(setTimeout(() => { if (barge.speaking) bargeConfirm("unsure"); }, 800));
    } else {
      barge.speechEndedAt = Date.now();
      if (barge.stage === "muted") { scheduleRelease(); return; }
      bargeClear();
      if (barge.stage === "ducked") {
        // A short sound: wait briefly for its transcript, else it was a cough / «угу».
        barge.timers.push(setTimeout(() => {
          if (barge.stage === "ducked" && !barge.speaking) { barge.stage = "idle"; barge.heard = ""; setRemoteVolume(1); }
        }, 400));
      }
    }
  }

  // User transcript while the assistant talks: decide by the words, not just the sound.
  function bargeOnWords(delta) {
    if (barge.stage === "muted" || !(barge.audible || barge.stage === "ducked")) return;
    barge.heard += delta;
    const text = barge.heard.toLowerCase().replace(/[ʼ’`]/g, "'");
    if (STOP_WORDS.test(text)) { bargeConfirm("ack"); return; }
    const words = text.split(/[^а-яіїєґa-z'-]+/i).filter(Boolean);
    // Words the assistant itself just said are its echo, not the user.
    const echo = new Set(barge.recentAssistant.toLowerCase().replace(/[ʼ’`]/g, "'").split(/[^а-яіїєґa-z'-]+/i));
    const meaningful = words.filter((w) => !BACKCHANNEL.has(w) && !echo.has(w));
    if (meaningful.length >= 2) bargeConfirm("stop");
  }

  function bargeOnAssistantText(delta) {
    barge.recentAssistant = (barge.recentAssistant + delta).slice(-300);
  }

  function onServerEvent(raw) {
    let event;
    try { event = JSON.parse(raw); } catch { return; }
    switch (event.type) {
      case "session.started":
        setStatus("Слухаю — говоріть", "live");
        break;
      case "session.input_transcript.delta":
        appendTranscript("user", event.delta);
        bargeOnWords(event.delta);
        touchActivity();
        break;
      case "session.output_transcript.delta":
        appendTranscript("assistant", event.delta);
        bargeOnAssistantText(event.delta);
        touchActivity();
        break;
      case "session.closed":
        stop("Розмову завершено");
        break;
      case "error":
        addLine("system", "Помилка: " + ((event.error && event.error.message) || "невідома"));
        break;
      default:
        break;
    }
  }

  // Every start() gets a number; a stop() (or a newer start) makes the old one stale, so an
  // attempt that is still awaiting the mic / backend never touches the new call's state.
  let attempt = 0;
  let dropTimer = null;
  let idleTimer = null;
  // A forgotten open call keeps billing: end it after this long with nobody speaking.
  const IDLE_LIMIT_MS = 5 * 60 * 1000;

  function touchActivity() {
    clearTimeout(idleTimer);
    if (!pc) return;
    idleTimer = setTimeout(() => {
      addLine("system", "Тиша вже 5 хвилин — розмову завершено, щоб не витрачати хвилини.");
      stop("Розмову завершено");
    }, IDLE_LIMIT_MS);
  }

  async function start() {
    if (!BACKEND) {
      addLine("system", "Не вказано адресу бекенду (web/config.js або ?backend=…).");
      return;
    }
    const my = ++attempt;
    $("talk").disabled = true;
    setStatus("Підключаюсь…", "busy");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        // Browser echo cancellation keeps the agent from hearing (and interrupting) itself.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (my !== attempt) { stream.getTracks().forEach((t) => t.stop()); return; }
      mic = stream;
      const peer = new RTCPeerConnection();
      pc = peer;
      peer.ontrack = (e) => {
        $("remote").srcObject = e.streams[0];
        if (gate) gate.listenTo(e.streams[0]);
      };
      peer.onconnectionstatechange = () => {
        if (pc !== peer) return;
        const state = peer.connectionState;
        clearTimeout(dropTimer);
        if (state === "failed") stop("З'єднання втрачено", "err");
        // "disconnected" is often a network blip that recovers by itself (Wi-Fi switch etc.).
        else if (state === "disconnected") {
          setStatus("Зв'язок перервався — відновлюю…", "busy");
          dropTimer = setTimeout(() => { if (pc === peer) stop("З'єднання втрачено", "err"); }, 8000);
        } else if (state === "connected") setStatus("Слухаю — говоріть", "live");
      };
      const newGate = await createNoiseGate(mic, onGateSignal);
      if (my !== attempt) { if (newGate) newGate.close(); return; }
      gate = newGate;
      if (gate) peer.addTrack(gate.track, mic);
      else mic.getTracks().forEach((track) => peer.addTrack(track, mic));
      channel = peer.createDataChannel("oai-events");
      channel.onmessage = (e) => { if (pc === peer) onServerEvent(e.data); };

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ sdp: offer.sdp }),
      });
      if (my !== attempt) return;
      if (res.status === 401) {
        showGate();
        throw new Error("Потрібен код доступу");
      }
      if (!res.ok) throw new Error("Сервер відповів " + res.status);
      const { sdp } = await res.json();
      if (my !== attempt) return;
      await peer.setRemoteDescription({ type: "answer", sdp });
      setStatus("З'єднуюсь…", "busy");
      $("talk").textContent = "Завершити";
      $("talk").classList.remove("primary");
      $("talk").classList.add("danger");
      $("talk").disabled = false;
      touchActivity();
    } catch (err) {
      if (my !== attempt) return;
      const msg = err && err.name === "NotAllowedError" ? "Немає доступу до мікрофона" : (err.message || String(err));
      addLine("system", msg);
      stop("Не вдалося підключитись", "err");
    }
  }

  function stop(text = "Не підключено", state = "") {
    attempt += 1;
    clearTimeout(dropTimer);
    clearTimeout(idleTimer);
    try { if (channel && channel.readyState === "open") channel.send(JSON.stringify({ type: "session.close" })); } catch { /* ignore */ }
    if (pc) { pc.close(); pc = null; }
    if (gate) { gate.close(); gate = null; }
    if (mic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    channel = null;
    closeBubbles();
    bargeReset();
    barge.recentAssistant = "";
    setStatus(text, state);
    $("talk").textContent = "Почати розмову";
    $("talk").classList.add("primary");
    $("talk").classList.remove("danger");
    $("talk").disabled = false;
  }

  async function refreshGoogle() {
    if (!BACKEND) return;
    try {
      const res = await fetch(BACKEND + "/api/me", { headers: headers() });
      if (res.status === 401) { showGate(); return; }
      if (!res.ok) return;
      const { google } = await res.json();
      onAccount(google.connected ? google.email || "" : null);
      const pill = $("googleState");
      if (!google.connected) {
        pill.textContent = "не підключено";
        pill.className = "pill";
        $("googleInfo").textContent = "Підключіть акаунт, щоб агент бачив ваш календар, пошту й нотатки.";
        $("googleConnect").textContent = "Підключити Google";
        $("googleDisconnect").hidden = true;
        return;
      }
      const all = google.calendar && google.gmail && google.notes;
      pill.textContent = all ? "підключено" : "частково";
      pill.className = "pill " + (all ? "ok" : "partial");
      const parts = [["календар", google.calendar], ["пошта", google.gmail], ["нотатки", google.notes]];
      const missing = parts.filter(([, ok]) => !ok).map(([n]) => n);
      $("googleInfo").textContent = google.email + (missing.length ? " — без дозволу: " + missing.join(", ") : " — усе доступно");
      $("googleConnect").textContent = missing.length ? "Додати дозволи" : "Змінити акаунт";
      $("googleDisconnect").hidden = false;
    } catch { /* backend asleep / offline: keep the old state */ }
  }

  // Another Google account (or signing out) means another person: the old chat and the Live
  // session that remembers it are dropped, and a running call restarts fresh. The first login
  // during a call keeps it — that's the same person, and the agent is told the login finished.
  function onAccount(email) {
    const previous = chatEmail;
    chatEmail = email;
    if (previous === undefined || previous === email || previous === null) return;
    const wasTalking = Boolean(pc);
    if (wasTalking) stop();
    clearLog();
    addLine("system", email ? "Новий користувач: " + email + " — нова розмова." : "Акаунт Google відключено — нова розмова.");
    if (wasTalking) start();
  }

  function connectGoogle() {
    const url = BACKEND + "/auth/google/start?client_id=" + encodeURIComponent(clientId) +
      "&access_code=" + encodeURIComponent(accessCode);
    const popup = window.open(url, "google-login", "width=520,height=680");
    if (!popup) { location.href = url; return; } // popup blocked → same tab
    const timer = setInterval(() => {
      refreshGoogle();
      if (popup.closed) { clearInterval(timer); refreshGoogle(); }
    }, 2000);
  }

  async function disconnectGoogle() {
    await fetch(BACKEND + "/api/google/disconnect", { method: "POST", headers: headers() }).catch(() => {});
    refreshGoogle();
  }

  function showGate() { $("gate").hidden = false; $("code").focus(); }

  async function init() {
    $("talk").addEventListener("click", () => (pc ? stop() : start()));
    $("googleConnect").addEventListener("click", connectGoogle);
    $("googleDisconnect").addEventListener("click", disconnectGoogle);
    $("saveCode").addEventListener("click", () => {
      accessCode = $("code").value.trim();
      store.set("va-access-code", accessCode);
      $("gate").hidden = true;
      refreshGoogle();
    });
    window.addEventListener("message", (e) => {
      if (e.data && e.data.type === "google-login") refreshGoogle();
    });
    // Closing or leaving the tab ends the paid Live session instead of leaving it open.
    window.addEventListener("pagehide", () => { if (pc) stop(); });
    if (!BACKEND) { addLine("system", "Не вказано адресу бекенду."); return; }
    const cfg = await wakeBackend();
    if (cfg) {
      if (cfg.access_code_required && !accessCode) showGate();
      if (!cfg.google_login) $("googleConnect").disabled = true;
      refreshGoogle();
    }
    // Free Render sleeps after ~15 min without requests and forgets everyone's Google login;
    // while the page is open and visible, keep it awake.
    setInterval(() => {
      if (document.visibilityState === "visible") fetch(BACKEND + "/healthz").catch(() => {});
    }, 10 * 60 * 1000);
  }

  // A sleeping free-tier backend takes up to a minute to start: wait for it instead of failing.
  async function wakeBackend() {
    const deadline = Date.now() + 90 * 1000;
    let notified = false;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(BACKEND + "/api/config");
        if (res.ok) {
          if (notified) setStatus("Не підключено");
          $("talk").disabled = false;
          return await res.json();
        }
      } catch { /* still waking up */ }
      if (!notified) {
        notified = true;
        $("talk").disabled = true;
        setStatus("Сервер прокидається — до хвилини…", "busy");
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
    $("talk").disabled = false;
    setStatus("Сервер не відповідає", "err");
    addLine("system", "Бекенд не відповідає. Оновіть сторінку трохи згодом.");
    return null;
  }

  init();
})();
