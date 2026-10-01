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
  let currentMsg = null;
  let currentRole = null;

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
    if (currentRole !== role || !currentMsg) {
      currentMsg = addLine(role, "");
      currentRole = role;
    }
    currentMsg.textContent += delta;
    $("log").scrollTop = $("log").scrollHeight;
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
      mic.getTracks().forEach((track) => pc.addTrack(track, mic));
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
    if (mic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    channel = null;
    currentMsg = null;
    currentRole = null;
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
