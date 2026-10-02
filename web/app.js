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
  let assistantSpeaking = false;
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
    if (role === "assistant") assistantSpeaking = true;
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
  // assistant talks the bar is higher, so leftover speaker echo doesn't interrupt it.
  // ?gate=off turns it off.
  function createNoiseGate(stream) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx || params.get("gate") === "off") return null;
    const ctx = new AudioCtx();
    ctx.resume().catch(() => {});
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    // Delay the audio a little so the gate opens before the first syllable passes through.
    const delay = ctx.createDelay(0.2);
    delay.delayTime.value = 0.06;
    const gain = ctx.createGain();
    gain.gain.value = 0;
    const out = ctx.createMediaStreamDestination();
    source.connect(analyser);
    source.connect(delay).connect(gain).connect(out);

    const buf = new Float32Array(analyser.fftSize);
    let floor = 0.003;
    let open = false;
    let loudFrames = 0;
    let lastLoud = 0;
    const timer = setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const level = Math.sqrt(sum / buf.length);
      // Noise floor: follows quiet moments fast, creeps up slowly under steady noise.
      floor = level < floor ? floor * 0.7 + level * 0.3 : floor + (level - floor) * 0.003;
      const now = Date.now();
      if (assistantSpeaking && now - lastDeltaAt.assistant > 1200) assistantSpeaking = false;
      const threshold = Math.max(assistantSpeaking ? 0.035 : 0.012, floor * 3.5);
      if (level > threshold) {
        loudFrames += 1;
        lastLoud = now;
      } else if (level < threshold * 0.6) {
        loudFrames = 0;
      }
      if (!open && loudFrames >= 2) {
        open = true;
        gain.gain.setTargetAtTime(1, ctx.currentTime, 0.005);
      } else if (open && now - lastLoud > 450) {
        open = false;
        gain.gain.setTargetAtTime(0, ctx.currentTime, 0.04);
      }
    }, 20);
    return {
      track: out.stream.getAudioTracks()[0],
      close() { clearInterval(timer); ctx.close().catch(() => {}); },
    };
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
        break;
      case "session.output_transcript.delta":
        appendTranscript("assistant", event.delta);
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

  async function start() {
    if (!BACKEND) {
      addLine("system", "Не вказано адресу бекенду (web/config.js або ?backend=…).");
      return;
    }
    $("talk").disabled = true;
    setStatus("Підключаюсь…", "busy");
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        // Browser echo cancellation keeps the agent from hearing (and interrupting) itself.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      pc = new RTCPeerConnection();
      pc.ontrack = (e) => { $("remote").srcObject = e.streams[0]; };
      pc.onconnectionstatechange = () => {
        if (pc && ["failed", "disconnected"].includes(pc.connectionState)) stop("З'єднання втрачено");
      };
      gate = createNoiseGate(mic);
      if (gate) pc.addTrack(gate.track, mic);
      else mic.getTracks().forEach((track) => pc.addTrack(track, mic));
      channel = pc.createDataChannel("oai-events");
      channel.onmessage = (e) => onServerEvent(e.data);

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ sdp: offer.sdp }),
      });
      if (res.status === 401) {
        showGate();
        throw new Error("Потрібен код доступу");
      }
      if (!res.ok) throw new Error("Сервер відповів " + res.status);
      const { sdp } = await res.json();
      await pc.setRemoteDescription({ type: "answer", sdp });
      setStatus("З'єднуюсь…", "busy");
      $("talk").textContent = "Завершити";
      $("talk").classList.remove("primary");
      $("talk").classList.add("danger");
      $("talk").disabled = false;
    } catch (err) {
      const msg = err && err.name === "NotAllowedError" ? "Немає доступу до мікрофона" : (err.message || String(err));
      addLine("system", msg);
      stop("Не вдалося підключитись", "err");
    }
  }

  function stop(text = "Не підключено", state = "") {
    try { if (channel && channel.readyState === "open") channel.send(JSON.stringify({ type: "session.close" })); } catch { /* ignore */ }
    if (pc) { pc.close(); pc = null; }
    if (gate) { gate.close(); gate = null; }
    if (mic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    channel = null;
    assistantSpeaking = false;
    closeBubbles();
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
    if (!BACKEND) { addLine("system", "Не вказано адресу бекенду."); return; }
    try {
      const res = await fetch(BACKEND + "/api/config");
      const cfg = await res.json();
      if (cfg.access_code_required && !accessCode) showGate();
      if (!cfg.google_login) $("googleConnect").disabled = true;
    } catch {
      addLine("system", "Бекенд не відповідає. На безкоштовному Render він «прокидається» до хвилини — оновіть сторінку трохи згодом.");
    }
    refreshGoogle();
  }

  init();
})();
