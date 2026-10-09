// ElevenLabs test page: the microphone goes to OpenAI Realtime over WebRTC (text answers only);
// each sentence of an answer is voiced by ElevenLabs through the backend (/api/eleven/tts) and
// played here. Same client id, access code and conversation as the GPT-Live page.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  document.querySelectorAll("[data-keep-query]").forEach((a) => { a.href = a.getAttribute("href") + location.search; });

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  let clientId = store.get("va-client-id");
  if (!clientId) {
    clientId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
    store.set("va-client-id", clientId);
  }
  let accessCode = store.get("va-access-code") || "";
  const headers = () => ({ "Content-Type": "application/json", "X-Client-Id": clientId, "X-Access-Code": accessCode });

  // ElevenLabs voice settings (their names and ranges). Defaults are ElevenLabs' own.
  const SLIDERS = [
    { key: "stability", label: "Stability", min: 0, max: 1, step: 0.05, def: 0.5,
      hint: "Нижче — живіше й емоційніше, але менш передбачувано; вище — рівніше, може звучати монотонно." },
    { key: "similarity", label: "Similarity", min: 0, max: 1, step: 0.05, def: 0.75,
      hint: "Наскільки тримається тембру оригіналу. Задто високо — тягне артефакти запису." },
    { key: "style", label: "Style", min: 0, max: 1, step: 0.05, def: 0,
      hint: "Підсилює манеру голосу. Більше — виразніше, але повільніше генерується." },
    { key: "speed", label: "Speed", min: 0.7, max: 1.2, step: 0.05, def: 1,
      hint: "Темп мовлення." },
  ];
  const settings = {};
  for (const s of SLIDERS) {
    const saved = parseFloat(store.get("el-" + s.key));
    settings[s.key] = saved >= s.min && saved <= s.max ? saved : s.def;
  }
  let voices = [];
  let voiceId = store.get("el-voice") || "";
  let model = store.get("el-model") || "";
  const SAMPLE = "Привіт! Завтра в тебе вільний ранок, а о пів на третю — зустріч із Сашком. " +
    "Слухай, а що як перенести її на четвер? Ніч, щастя, сонце, шість цукерок.";

  // ── page bits ──────────────────────────────────────────────────────────────
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
  function setStatus(text, kind) {
    $("status").textContent = text;
    $("dot").className = "dot" + (kind ? " " + kind : "");
  }
  const currentVoice = () => voices.find((v) => v.id === voiceId);
  const feminine = () => (currentVoice() || {}).gender !== "male";

  function renderSliders() {
    const box = $("sliders");
    box.textContent = "";
    for (const s of SLIDERS) {
      const row = document.createElement("div");
      row.className = "label-row";
      const label = document.createElement("label");
      label.htmlFor = "s-" + s.key;
      label.textContent = s.label;
      const value = document.createElement("span");
      value.className = "muted small";
      value.textContent = settings[s.key].toFixed(2);
      row.append(label, value);
      const input = document.createElement("input");
      Object.assign(input, { type: "range", id: "s-" + s.key, min: s.min, max: s.max, step: s.step, value: settings[s.key] });
      input.oninput = () => {
        settings[s.key] = parseFloat(input.value);
        value.textContent = settings[s.key].toFixed(2);
        store.set("el-" + s.key, String(settings[s.key]));
      };
      const hint = document.createElement("p");
      hint.className = "hint slider-hint";
      hint.textContent = s.hint;
      box.append(row, input, hint);
    }
  }

  function showVoiceInfo() {
    const v = currentVoice();
    $("voiceInfo").textContent = v ? [v.gender === "male" ? "чоловічий" : v.gender === "female" ? "жіночий" : "", v.description]
      .filter(Boolean).join(" · ") : "";
  }

  async function loadVoices() {
    const res = await fetch(BACKEND + "/api/eleven/voices", { headers: headers() });
    if (res.status === 401) { $("gate").hidden = false; return; }
    if (!res.ok) { addLine("system", "Не вдалося завантажити голоси ElevenLabs (" + res.status + ")."); return; }
    const data = await res.json();
    voices = data.voices || [];
    if (!voices.some((v) => v.id === voiceId)) voiceId = data.default || (voices[0] || {}).id || "";
    const sel = $("voice");
    sel.textContent = "";
    const groups = [["female", "Жіночі"], ["male", "Чоловічі"], ["", "Інші"]];
    for (const [gender, title] of groups) {
      const list = voices.filter((v) => (gender ? v.gender === gender : v.gender !== "female" && v.gender !== "male"));
      if (!list.length) continue;
      const group = document.createElement("optgroup");
      group.label = title;
      for (const v of list) group.append(new Option(v.name, v.id, false, v.id === voiceId));
      sel.append(group);
    }
    sel.disabled = !voices.length;
    const models = data.models || {};
    if (!(model in models)) model = data.default_model in models ? data.default_model : Object.keys(models)[0];
    $("model").textContent = "";
    for (const [id, label] of Object.entries(models)) $("model").append(new Option(label, id, false, id === model));
    $("preview").disabled = !voices.length;
    $("talk").disabled = !voices.length;
    showVoiceInfo();
  }

  // ── ElevenLabs playback ─────────────────────────────────────────────────────
  // One <audio> element (it feeds the analyser that tells echo from the user); sentences are
  // fetched up to two ahead (the free plan allows few parallel requests) and played in order.
  const player = $("player");
  let queue = []; // { text, prev, blob: Promise<Blob>, fetchMs }
  let playing = null;
  let generation = 0; // bumped on barge-in / stop: everything older is dropped
  let spokenThisAnswer = [];
  let latency = null; // { from: speech end, chunk: first chunk of the answer }

  // Starts the request for item.text; item.blob resolves to the MP3, item.fetchMs is how long it took.
  function tts(item) {
    const started = performance.now();
    item.fetchMs = 0;
    item.blob = fetch(BACKEND + "/api/eleven/tts", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        text: item.text, previous_text: item.prev || "", voice_id: voiceId, model,
        stability: settings.stability, similarity: settings.similarity, style: settings.style,
        speed: settings.speed, speaker_boost: $("boost").checked,
      }),
    }).then(async (res) => {
      if (!res.ok) {
        let detail = String(res.status);
        try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
        throw new Error(detail);
      }
      const blob = await res.blob();
      item.fetchMs = Math.round(performance.now() - started);
      return blob;
    });
    item.blob.catch(() => {}); // reported when its turn comes
    return item;
  }

  function enqueue(text) {
    const prev = queue.length ? queue[queue.length - 1].text : playing ? playing.text : "";
    queue.push({ text, prev });
    pump();
  }

  function pump() {
    // Start fetches for the next two sentences.
    queue.slice(0, 2).forEach((q) => { if (!q.blob) tts(q); });
    if (playing || !queue.length) return;
    const item = queue.shift();
    playing = item;
    const gen = generation;
    item.blob.then((blob) => {
      if (gen !== generation) return;
      const url = URL.createObjectURL(blob);
      player.src = url;
      player.onended = () => { URL.revokeObjectURL(url); finishChunk(gen); };
      return player.play().then(() => {
        spokenThisAnswer.push(item.text);
        if (latency && latency.chunk === null) {
          latency.chunk = item.fetchMs;
          const total = latency.from ? Math.round(performance.now() - latency.from) : null;
          $("latency").textContent = (total !== null ? "Від кінця вашої фрази до голосу: " + total + " мс · " : "") +
            "ElevenLabs: " + item.fetchMs + " мс на перше речення.";
        }
        setStatus("Говорить", "speak");
      });
    }).catch((err) => {
      if (gen !== generation) return;
      addLine("system", "ElevenLabs: " + (err.message || err));
      finishChunk(gen);
    });
    pump();
  }

  function finishChunk(gen) {
    if (gen !== generation) return;
    playing = null;
    if (queue.length) { pump(); return; }
    quietSince = performance.now();
    if (call && call.endAfterSpeech) { stop("Розмову завершено"); return; }
    if (call) setStatus("Слухаю", "live");
  }

  function stopPlayback() {
    generation += 1;
    queue = [];
    playing = null;
    player.pause();
    player.removeAttribute("src");
    quietSince = performance.now();
  }
  const isPlaying = () => !!playing || queue.length > 0;

  // ── sentences out of the streamed answer ────────────────────────────────────
  // The first sentence goes alone (it starts the voice soonest); later ones in pieces of ~200
  // characters, which keeps the intonation joined up and the request count low.
  let pending = "";
  let firstOfAnswer = true;
  function takeText(delta, done) {
    pending += delta;
    for (;;) {
      const ends = [...pending.matchAll(/[.!?…]+[»")]*\s+/g)];
      if (!ends.length) break;
      let cut = ends[0].index + ends[0][0].length;
      if (!firstOfAnswer) {
        for (const m of ends) { if (m.index + m[0].length <= 220) cut = m.index + m[0].length; }
        if (!done && cut < 60 && pending.length < 220) break; // wait for a bit more
      }
      enqueue(pending.slice(0, cut).trim());
      pending = pending.slice(cut);
      firstOfAnswer = false;
    }
    // A long run without a full stop: cut at the last comma so the voice can start.
    if (firstOfAnswer && pending.length > 140) {
      const comma = pending.lastIndexOf(",");
      if (comma > 40) { enqueue(pending.slice(0, comma + 1).trim()); pending = pending.slice(comma + 1); firstOfAnswer = false; }
    }
    if (done && pending.trim()) { enqueue(pending.trim()); pending = ""; }
  }

  // ── call ────────────────────────────────────────────────────────────────────
  // The mic reaches OpenAI through a gain node: shut while Єва speaks (the speakers' echo would
  // otherwise be heard as the user), opened by a barge-in — the user clearly louder than her echo.
  // A 150 ms delay keeps the first sound of an interruption.
  let call = null;
  let quietSince = 0;
  // One context for the page: the player can be routed into Web Audio only once, so it stays.
  let shared = null;
  function audio() {
    if (!shared) {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const node = ctx.createMediaElementSource(player);
      const playAnalyser = ctx.createAnalyser();
      playAnalyser.fftSize = 1024;
      node.connect(playAnalyser);
      node.connect(ctx.destination);
      shared = { ctx, playAnalyser };
    }
    return shared;
  }
  const ECHO = parseFloat(params.get("echo")) || 1.5; // ?echo=3 for loud speakers

  async function start() {
    if (call) return;
    setStatus("Підключаюсь…", "busy");
    $("talk").disabled = true;
    const c = { endAfterSpeech: false };
    call = c;
    try {
      c.mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      const { ctx, playAnalyser } = audio();
      c.ctx = ctx;
      c.playAnalyser = playAnalyser;
      await ctx.resume();
      const src = ctx.createMediaStreamSource(c.mic);
      c.src = src;
      c.micAnalyser = ctx.createAnalyser();
      c.micAnalyser.fftSize = 1024;
      src.connect(c.micAnalyser);
      const delay = ctx.createDelay(0.5);
      delay.delayTime.value = 0.15;
      c.send = ctx.createGain();
      const out = ctx.createMediaStreamDestination();
      src.connect(delay).connect(c.send).connect(out);
      c.monitor = setInterval(() => watch(c), 20);

      const pc = new RTCPeerConnection();
      c.pc = pc;
      pc.addTrack(out.stream.getAudioTracks()[0], out.stream);
      c.channel = pc.createDataChannel("oai-events");
      c.channel.onmessage = (e) => { if (call === c) onEvent(JSON.parse(e.data)); };
      pc.onconnectionstatechange = () => {
        if (call === c && (pc.connectionState === "failed" || pc.connectionState === "closed")) stop("З'єднання втрачено", "err");
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const res = await fetch(BACKEND + "/api/eleven/session", {
        method: "POST", headers: headers(), body: JSON.stringify({ sdp: offer.sdp, feminine: feminine() }),
      });
      if (res.status === 401) { $("gate").hidden = false; throw new Error("Потрібен код доступу"); }
      if (!res.ok) throw new Error("Сервер відповів " + res.status);
      const { sdp } = await res.json();
      if (call !== c) return;
      await pc.setRemoteDescription({ type: "answer", sdp });
      setStatus("Слухаю", "live");
      $("talk").textContent = "Завершити розмову";
      $("talk").classList.add("danger");
      $("talk").disabled = false;
    } catch (err) {
      if (call !== c) return;
      addLine("system", err && err.name === "NotAllowedError" ? "Немає доступу до мікрофона" : (err.message || String(err)));
      stop("Не вдалося підключитись", "err");
    }
  }

  function stop(text = "Не підключено", kind = "") {
    const c = call;
    call = null;
    stopPlayback();
    if (c) {
      clearInterval(c.monitor);
      try { c.channel && c.channel.close(); } catch { /* closed */ }
      try { c.pc && c.pc.close(); } catch { /* closed */ }
      if (c.mic) c.mic.getTracks().forEach((t) => t.stop());
      if (c.src) c.src.disconnect(); // the shared context stays for the next call
    }
    setStatus(text, kind);
    $("talk").textContent = "Почати розмову";
    $("talk").classList.remove("danger");
    $("talk").disabled = !voices.length;
  }

  const rms = (analyser, buf) => {
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    return Math.sqrt(sum / buf.length);
  };
  const micBuf = new Float32Array(1024);
  const playBuf = new Float32Array(1024);
  let loudTicks = 0;

  function watch(c) {
    const mic = rms(c.micAnalyser, micBuf);
    const out = rms(c.playAnalyser, playBuf);
    const now = c.ctx.currentTime;
    if (!isPlaying()) {
      loudTicks = 0;
      // A short tail after she stops: the room's echo dies out first.
      const open = performance.now() - quietSince > 300;
      c.send.gain.setTargetAtTime(open ? 1 : 0, now, 0.02);
      return;
    }
    // While she speaks: only a voice clearly above her echo counts, for ~160 ms in a row.
    loudTicks = mic > Math.max(0.03, out * ECHO) ? loudTicks + 1 : 0;
    if (loudTicks >= 8) {
      loudTicks = 0;
      bargeIn(c);
      return;
    }
    c.send.gain.setTargetAtTime(0, now, 0.01);
  }

  function bargeIn(c) {
    const heard = spokenThisAnswer.join(" ");
    stopPlayback();
    c.send.gain.setTargetAtTime(1, c.ctx.currentTime, 0.005);
    send({ type: "response.cancel" });
    // The model thinks it said the whole answer; tell it what the user actually heard.
    if (heard) {
      send({ type: "conversation.item.create", item: { type: "message", role: "system", content: [{ type: "input_text",
        text: "The user interrupted you. Of your last answer they heard only: «" + heard.slice(-600) + "». Do not repeat it unless asked." }] } });
    }
    setStatus("Слухаю", "live");
  }

  function send(event) {
    if (call && call.channel && call.channel.readyState === "open") call.channel.send(JSON.stringify(event));
  }

  let bubble = null;
  function onEvent(event) {
    switch (event.type) {
      case "input_audio_buffer.speech_started":
        if (isPlaying()) bargeIn(call);
        setStatus("Слухаю", "live");
        break;
      case "input_audio_buffer.speech_stopped":
        latency = { from: performance.now(), chunk: null };
        setStatus("Думає", "think");
        break;
      case "conversation.item.input_audio_transcription.completed":
        if ((event.transcript || "").trim()) addLine("user", event.transcript.trim());
        break;
      case "response.created":
        bubble = null;
        pending = "";
        firstOfAnswer = true;
        spokenThisAnswer = [];
        if (!latency || latency.chunk !== null) latency = { from: 0, chunk: null };
        break;
      case "response.output_text.delta":
        if (!bubble) bubble = addLine("assistant", "");
        bubble.textContent += event.delta || "";
        $("log").scrollTop = $("log").scrollHeight;
        takeText(event.delta || "", false);
        break;
      case "response.output_text.done":
        takeText("", true);
        break;
      case "response.done": {
        const output = (event.response && event.response.output) || [];
        const calls = output.filter((o) => o.type === "function_call");
        if (calls.some((o) => o.name === "end_conversation")) call.endAfterSpeech = true;
        if (calls.length) setStatus("Працює з інструментами…", "think");
        else if (call.endAfterSpeech && !isPlaying()) stop("Розмову завершено");
        break;
      }
      case "error": {
        const code = event.error && event.error.code;
        if (code !== "response_cancel_not_active") addLine("system", "Realtime: " + ((event.error && event.error.message) || "помилка"));
        break;
      }
      default:
        break;
    }
  }

  // ── wiring ──────────────────────────────────────────────────────────────────
  $("talk").onclick = () => (call ? stop() : start());
  $("voice").onchange = () => { voiceId = $("voice").value; store.set("el-voice", voiceId); showVoiceInfo(); };
  $("model").onchange = () => { model = $("model").value; store.set("el-model", model); };
  $("boost").checked = store.get("el-boost") !== "0";
  $("boost").onchange = () => store.set("el-boost", $("boost").checked ? "1" : "0");
  $("sample").value = store.get("el-sample") || SAMPLE;
  $("sample").oninput = () => store.set("el-sample", $("sample").value);
  $("preview").onclick = async () => {
    const text = $("sample").value.trim();
    if (!text) return;
    stopPlayback();
    $("preview").disabled = true;
    const item = tts({ text, prev: "" });
    try {
      const blob = await item.blob;
      await audio().ctx.resume();
      player.src = URL.createObjectURL(blob);
      await player.play();
      $("latency").textContent = "Прослухати: ElevenLabs " + item.fetchMs + " мс (" + text.length + " символів).";
    } catch (err) {
      addLine("system", "ElevenLabs: " + (err.message || err));
    } finally {
      $("preview").disabled = false;
    }
  };
  $("newChat").onclick = async () => {
    await fetch(BACKEND + "/api/conversation", { method: "DELETE", headers: headers() }).catch(() => {});
    $("log").innerHTML = '<p class="muted empty">Тут з\'являтиметься текст розмови.</p>';
  };
  $("saveCode").onclick = () => {
    accessCode = $("code").value.trim();
    store.set("va-access-code", accessCode);
    $("gate").hidden = true;
    loadVoices().catch(() => addLine("system", "Сервер недоступний."));
  };

  renderSliders();
  (async () => {
    if (!BACKEND) { addLine("system", "Не вказано адресу бекенду (web/config.js або ?backend=…)."); return; }
    try {
      const cfg = await (await fetch(BACKEND + "/api/config")).json();
      if (!cfg.eleven) { $("off").hidden = false; return; }
      if (cfg.access_code_required && !accessCode) { $("gate").hidden = false; return; }
      await loadVoices();
    } catch {
      addLine("system", "Сервер недоступний — можливо, він прокидається (до хвилини). Оновіть сторінку.");
    }
  })();
})();
