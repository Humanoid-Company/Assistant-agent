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
  // Voice picker: the choice lives here (the server forgets it on restart) and is sent with
  // every new call. Voice is fixed for a Live session, so a change applies from the next call.
  let voices = [];
  let voice = store.get("va-voice") || "";
  let speed = store.get("va-speed") || "normal";
  let style = store.get("va-style") || "normal";
  let volume = parseFloat(store.get("va-volume") || "1");
  // Keep what is said nearby during a pause (voice/background.py); the user can turn it off.
  let bgListen = store.get("va-bg-listen") !== "0";
  // A voice picked while Єва answers: switch at once (she goes on in the new voice) or once she is done.
  let voiceNow = store.get("va-voice-now") !== "0";
  if (!(volume >= 0 && volume <= 1)) volume = 1;

  // ?prompt=v2: try the experimental delivery prompt (prompts/live_prompt.py) instead of the usual one.
  const PROMPT_VARIANT = params.get("prompt") || undefined;
  let pc = null;
  let channel = null;
  let sessionVoice = ""; // the voice the current call was opened with
  let mic = null;
  let gate = null;
  // One open bubble per speaker: user and assistant transcripts arrive interleaved when they
  // overlap, and a single "current" bubble shredded both into word fragments.
  const openMsg = { user: null, assistant: null };
  const lastDeltaAt = { user: 0, assistant: 0 };
  const TURN_PAUSE_MS = 3000; // the transcript of one utterance can pause this long mid-sentence
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
  // assistant is audible the bar also sits above its echo in the mic, learned from how loud
  // that echo really is: high on laptop speakers, near zero in headphones — so a quiet voice
  // in a headset still gets through.
  // It also hears the assistant's own audio (2nd input) and reports to the page when the
  // user starts/stops talking and when the assistant is audible — that drives barge-in.
  // ?gate=off turns it off. It runs on the audio thread (AudioWorklet), so a background tab,
  // where timers are throttled to once a second, doesn't chop the user's speech.
  const GATE_WORKLET = `
    class NoiseGate extends AudioWorkletProcessor {
      constructor(options) {
        super();
        this.frame = Math.round(sampleRate * 0.02);       // judge loudness per 20 ms
        this.ring = new Float32Array(Math.round(sampleRate * 0.06)); // 60 ms look-ahead
        this.pos = 0; this.sum = 0; this.outSum = 0; this.count = 0;
        this.floor = 0.003; this.open = false; this.loud = 0; this.quietFor = 1;
        this.gain = 0;
        // Barge-in only listens to "near" voice: the person at the mic is far louder than
        // people talking in the background, a TV or a door bang (those are short).
        this.near = (options.processorOptions || {}).near || 0.06;
        this.speaking = false; this.nearFrames = 0; this.nearQuietFor = 1;
        this.audible = false; this.outQuietFor = 1;       // assistant playback
        // Mic level per unit of assistant output level while only the assistant talks. Starts
        // as "loud speakers" and drops within a second or two when there is no echo (headset).
        this.echo = 0.5;
      }
      judge(level, outLevel) {
        if (outLevel > 0.01) this.outQuietFor = 0; else this.outQuietFor += 0.02;
        const audible = this.outQuietFor < 0.3;
        if (audible !== this.audible) { this.audible = audible; this.port.postMessage({ audible }); }
        // 0.006 ≈ quiet speech into a headset mic; the room's noise floor raises the bar.
        const echoLevel = audible ? outLevel * this.echo * 2.5 : 0;
        // Speakers leave bursts of echo that the browser's echo canceller misses; averaged into
        // this.echo they look small, and letting them through can make the model hear itself
        // and break off mid-sentence. So while the assistant talks keep the old 0.04 bar unless the mic plainly
        // hears no echo at all (headphones). this.echo starts high, so "speakers" is the default.
        const headset = this.echo < 0.05;
        const threshold = Math.max(audible && !headset ? 0.04 : 0.006, this.floor * 4, echoLevel);
        // Learn the echo while the gate is shut (the user isn't talking over the assistant).
        if (audible && !this.open && outLevel > 0.01) {
          const ratio = level / outLevel;
          this.echo += (ratio - this.echo) * (ratio < this.echo ? 0.1 : 0.03);
        }
        if (level > threshold) { this.loud += 1; this.quietFor = 0; }
        else { this.quietFor += 0.02; if (level < threshold * 0.6) this.loud = 0; }
        if (!this.open && this.loud >= 2) this.open = true;
        else if (this.open && this.quietFor > 0.45) this.open = false;
        const nearThreshold = Math.max(audible ? this.near * 1.3 : this.near, this.floor * 8, echoLevel * 1.3);
        if (level > nearThreshold) { this.nearFrames += 1; this.nearQuietFor = 0; }
        else { this.nearQuietFor += 0.02; if (this.nearQuietFor > 0.25) this.nearFrames = 0; }
        // Near voice: ~120 ms of loud frames to start (a click/bang is shorter), 250 ms gap to end.
        const speaking = this.speaking ? this.nearQuietFor <= 0.25 : this.nearFrames >= 6;
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
        // ?near=0.04 makes barge-in more sensitive (quiet voice / far mic), 0.09 less.
        processorOptions: { near: parseFloat(params.get("near")) || 0.06 },
      });
      node.port.onmessage = (e) => onSignal(e.data);
      const out = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(stream).connect(node, 0, 0).connect(out);
      let remoteSource = null;
      return {
        stream,
        track: out.stream.getAudioTracks()[0],
        // The assistant's audio, measured only (never played from here). A gate kept for the next
        // call (voice switch) drops the old call's audio first.
        listenTo(remote) {
          if (remoteSource) remoteSource.disconnect();
          remoteSource = remote ? ctx.createMediaStreamSource(remote) : null;
          if (remoteSource) remoteSource.connect(node, 0, 1);
        },
        // Still usable for the next call: not closed by the browser, and running again.
        alive() { if (ctx.state === "suspended") ctx.resume().catch(() => {}); return ctx.state !== "closed"; },
        close() { ctx.close().catch(() => {}); },
      };
    } catch {
      ctx.close().catch(() => {});
      return null; // old browser: send the mic as is
    }
  }

  // ── Єва: «Єва, скажи» wakes her (or «Привіт, Єва», «Гей, Єва», …), «Дякую, Єва» pauses ───────
  // Same rules as voice/wake_phrases.py — keep the two in sync (tests/test_eva_phrases.py has the
  // cases). The name is matched against a list (fuzzy matching a 3-letter word would accept
  // «два»), the verbs fuzzily, like Python's difflib ratio ≥ 0.75.
  const EVA_NAME_FORMS = new Set(["єва", "єво", "ева", "эва", "эво", "ево", "єфа", "ефа", "eva", "evo", "eve", "yeva", "yevo", "jeva"]);
  const EVA_WAKE_VERBS = [
    "скажи", "кажи", "скажіть", "скажи-но",
    "привіт", "привітик", "вітаю", "гей", "хей", "агов", "алло",
    "слухай", "послухай", "допоможи", "підкажи", "прокидайся",
    "hello", "hey",
  ];
  const EVA_STOP_VERBS = ["дякую", "дякуємо"]; // one pause phrase: «Дякую, Єва»
  const EVA_MAX_GAP = 2;

  function evaTokens(text) {
    const lowered = String(text || "").toLowerCase().replace(/’/g, "'").replace(/ё/g, "е");
    const words = (lowered.match(/[a-zа-яіїєґё'-]+/g) || []).map((w) => w.replace(/^['-]+|['-]+$/g, "")).filter(Boolean);
    const merged = [];
    for (const word of words) {
      const last = merged[merged.length - 1];
      if ((last === "є" || last === "е" || last === "э") && word === "ва") merged[merged.length - 1] = last + word;
      else merged.push(word);
    }
    return merged;
  }

  // difflib.SequenceMatcher.ratio(): 2·matches / total length, matches = longest common blocks.
  function matchingChars(a, b) {
    let best = 0, ai = 0, bj = 0;
    for (let i = 0; i < a.length; i++) {
      for (let j = 0; j < b.length; j++) {
        let k = 0;
        while (i + k < a.length && j + k < b.length && a[i + k] === b[j + k]) k++;
        if (k > best) { best = k; ai = i; bj = j; }
      }
    }
    if (!best) return 0;
    return best + matchingChars(a.slice(0, ai), b.slice(0, bj)) + matchingChars(a.slice(ai + best), b.slice(bj + best));
  }
  const like = (word, verbs) => verbs.some((v) => word === v || (2 * matchingChars(word, v)) / (word.length + v.length) >= 0.75);

  function evaPair(words, verbs) {
    for (let i = 0; i < words.length; i++) {
      if (!EVA_NAME_FORMS.has(words[i])) continue;
      const lo = Math.max(0, i - EVA_MAX_GAP - 1), hi = Math.min(words.length, i + EVA_MAX_GAP + 2);
      for (let j = lo; j < hi; j++) if (j !== i && like(words[j], verbs)) return [i, j];
    }
    return null;
  }

  // «Єва, скажи …» → what was said after the phrase ("" if nothing); null if no wake phrase.
  function matchWake(text) {
    const words = evaTokens(text);
    const pair = evaPair(words, EVA_WAKE_VERBS);
    return pair ? words.slice(Math.max(pair[0], pair[1]) + 1).join(" ") : null;
  }
  const isStop = (text) => evaPair(evaTokens(text), EVA_STOP_VERBS) !== null;
  // «зміни голос на …» — same pattern as VOICE_REQUEST_RE in voice/options.py; the server picks the voice.
  const VOICE_REQUEST_RE = /(змін|змин|поміня|постав|переключ|перемкн|увімкн|зроби|давай)[а-яіїєґ'a-z]*[\s,]+([^\s,]+[\s,]+){0,3}?голос(?:у|а|ом)?(?![а-яіїєґ'a-z])|(^|[^а-яіїєґ'a-z])голос\s+на\s/i;

  // mode: off → connecting → waiting («Єва, скажи»; the model is muted) ⇄ active (listening /
  // thinking / speaking); switching = reconnecting with a new voice.
  const eva = {
    mode: "off",
    pending: null,
    recentUser: "",   // rolling user transcript, for «Дякую, Єва»
    lastUserAt: 0,
    lastAudibleAt: 0,
    thinking: false,
    thinkTimer: null,
    pauseTimer: null,
    recognizer: null,
    wakeSupported: Boolean(window.SpeechRecognition || window.webkitSpeechRecognition) && params.get("wake") !== "off",
  };

  function showState() {
    const m = eva.mode;
    if (m === "off") return; // stop() sets its own text
    let state;
    if (m === "connecting") state = ["Підключаюсь…", "busy"];
    else if (m === "switching") state = ["Перемикаю голос…", "busy"];
    else if (m === "waiting") state = eva.wakeSupported
      ? [bgListen ? "Пауза — слухаю фоном, чекаю «Єва, скажи»" : "Пауза — чекаю «Єва, скажи»", "wait"]
      : ["Пауза — натисніть «Продовжити»", "wait"];
    else if (barge.audible) state = ["Говорю", "speak"];
    else if (barge.speaking) state = ["Слухаю", "live"];
    else if (eva.thinking) state = ["Думаю…", "think"];
    else state = ["Слухаю — говоріть", "live"];
    setStatus(state[0], state[1]);
  }

  function updateButtons() {
    const on = eva.mode !== "off";
    $("talk").textContent = on ? "Завершити" : "Почати розмову";
    $("talk").classList.toggle("primary", !on);
    $("talk").classList.toggle("danger", on);
    $("talk").disabled = false;
    $("resume").hidden = eva.mode !== "waiting";
    $("pause").hidden = eva.mode !== "active";
    $("wakeHint").textContent = eva.wakeSupported
      ? "Покличте Єву — «Єва, скажи», «Привіт, Єва», «Гей, Єва», «Єво, слухай». «Дякую, Єва» — пауза. Браузер попросить доступ до мікрофона."
      : "Цей браузер не вміє слухати «Єва, скажи» (потрібен Chrome, Edge або Safari) — користуйтеся кнопками «Пауза» / «Продовжити». «Дякую, Єва» працює.";
  }

  // Barge-in, like the desktop app: the Live model alone reacts slowly and keeps talking over
  // the user. Only the person at the mic counts ("near" voice — background talk, a TV, bangs
  // don't): their voice over the assistant ducks it a little; a stop word or real words from
  // them mute it and tell the model to stop. A sound alone never stops the assistant.
  // Whole words only; no «секунду»/«хвилинку» — they are everyday words, not a stop request.
  const STOP_WORDS = new Set(["стоп", "зачекай", "почекай", "стривай", "досить", "stop", "wait"]);
  const splitWords = (text) => text.toLowerCase().replace(/[ʼ’`]/g, "'").split(/[^а-яіїєґa-z'-]+/i).filter(Boolean);
  const BACKCHANNEL = new Set(["угу", "ага", "так", "ммм", "мм", "м", "ок", "окей", "добре", "ну", "ого", "ага-ага", "мгм", "ясно", "зрозуміло", "да",
    "так-так", "угу-угу", "хм", "о", "ой", "ох", "слухаю", "нічого", "собі", "та"]);
  const barge = {
    audible: false,     // assistant audio is playing right now
    speaking: false,    // user voice right now
    stage: "idle",      // idle → ducked → muted
    heard: "",          // user words since this barge-in candidate started
    burst: "",          // what she has said since her last pause
    burstAt: 0,
    timers: [],
    releaseTimer: null,
    speechEndedAt: 0,   // when the near voice last stopped (transcripts lag behind it)
    recentAssistant: "", // for the echo check
  };

  function bargeClear() {
    barge.timers.forEach(clearTimeout);
    barge.timers = [];
  }

  // level: barge-in ducking (1 / 0.5 / 0) × the user's volume; silent while Єва is paused.
  function setRemoteVolume(level) {
    const el = $("remote");
    const v = eva.mode === "waiting" ? 0 : level * volume;
    el.volume = v;
    el.muted = v === 0;
  }
  const bargeLevel = () => (barge.stage === "muted" ? 0 : barge.stage === "ducked" ? 0.5 : 1);

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
      pause: "The user said «Дякую, Єва»: stop speaking immediately and stay completely silent. Do not reply to it. You will be woken again later — then continue with full memory of this conversation.",
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
    bargeOnGate(signal);
    if ("audible" in signal) eva.lastAudibleAt = Date.now(); // when she started / stopped sounding
    if (barge.audible) eva.thinking = false;
    showState();
  }

  function bargeOnGate(signal) {
    if ("audible" in signal) {
      barge.audible = signal.audible;
      if (barge.stage === "muted") scheduleRelease();
      return;
    }
    barge.speaking = signal.speaking;
    if (signal.speaking) {
      clearTimeout(barge.releaseTimer);
      if (barge.stage !== "idle" || !barge.audible || herBackchannel()) return;
      if (Date.now() - barge.speechEndedAt > 1500) barge.heard = "";
      barge.timers.push(setTimeout(() => {
        if (barge.speaking && barge.stage === "idle") { barge.stage = "ducked"; setRemoteVolume(0.5); }
      }, 400));
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
    if (herBackchannel() && !splitWords(delta).some((w) => STOP_WORDS.has(w))) return;
    // Words count only while the near voice is on (or just ended — transcripts lag); words
    // heard with no one at the mic are background talk.
    if (!barge.speaking && Date.now() - barge.speechEndedAt > 1200) return;
    barge.heard += delta;
    const words = splitWords(barge.heard);
    // Words the assistant itself just said are its echo, not the user — stop words included.
    const echo = new Set(splitWords(barge.recentAssistant));
    const own = words.filter((w) => !echo.has(w));
    if (own.some((w) => STOP_WORDS.has(w))) { bargeConfirm("ack"); return; }
    const meaningful = own.filter((w) => !BACKCHANNEL.has(w));
    if (meaningful.length >= 3) bargeConfirm("stop");
  }

  function bargeOnAssistantText(delta) {
    barge.recentAssistant = (barge.recentAssistant + delta).slice(-300);
    if (Date.now() - barge.burstAt > 800) barge.burst = "";
    barge.burst += delta;
    barge.burstAt = Date.now();
  }

  // She is only saying «угу» / «так-так» / «ммм» while the user talks (voice/interrupt_intent.py
  // is_backchannel_utterance): that overlap is the point, not an answer to barge into.
  function herBackchannel() {
    const words = splitWords(barge.burst);
    return words.length > 0 && words.length <= 3 && words.every((w) => BACKCHANNEL.has(w));
  }

  function onServerEvent(raw) {
    let event;
    try { event = JSON.parse(raw); } catch { return; }
    switch (event.type) {
      case "session.started":
        applyStart();
        break;
      case "session.input_transcript.delta":
        if (eva.mode === "waiting") break;
        appendTranscript("user", event.delta);
        eva.lastUserAt = Date.now();
        eva.recentUser = (eva.recentUser + event.delta).slice(-80);
        // «Дякую, Єва» — also while she is talking: cut her off and pause.
        if (isStop(eva.recentUser)) { pauseEva(); break; }
        eva.thinking = true;
        clearTimeout(eva.thinkTimer);
        eva.thinkTimer = setTimeout(() => { eva.thinking = false; showState(); }, 15000);
        bargeOnWords(event.delta);
        touchActivity();
        showState();
        break;
      case "session.output_transcript.delta":
        if (eva.mode === "waiting") break; // whatever she says to «Дякую, Єва» is not played
        appendTranscript("assistant", event.delta);
        bargeOnAssistantText(event.delta);
        eva.thinking = false;
        touchActivity();
        break;
      case "session.closed":
        onSessionClosed();
        break;
      case "error": {
        const message = (event.error && event.error.message) || "невідома";
        // A voice switch closes the call while the page may still be sending: not a real error.
        if (!/session is closing/i.test(message)) addLine("system", "Помилка: " + message);
        break;
      }
      default:
        break;
    }
  }

  // Every start() gets a number; a teardown (or a newer start) makes the old one stale, so an
  // attempt that is still awaiting the mic / backend never touches the new call's state.
  let attempt = 0;
  let dropTimer = null;
  let idleTimer = null;
  // GPT-Live bills every second a session is open, muted or not ($0.05/min), so a forgotten call
  // pauses after this long with nobody speaking… (config.js: IDLE_PAUSE_SECONDS)
  const CFG = window.APP_CONFIG || {};
  const IDLE_LIMIT_MS = (CFG.IDLE_PAUSE_SECONDS ?? 90) * 1000;
  // …and a pause this long closes the Live session; «Єва, скажи» reopens it with the history.
  // (config.js: PAUSE_CLOSE_SECONDS)
  const PAUSE_CLOSE_MS = (CFG.PAUSE_CLOSE_SECONDS ?? 30) * 1000;
  // A pause never keeps a call open longer than this, even while the server asks to.
  const PAUSE_HOLD_MAX_MS = 5 * 60 * 1000;

  function touchActivity() {
    clearTimeout(idleTimer);
    if (!pc || eva.mode !== "active") return;
    idleTimer = setTimeout(() => {
      if (eva.mode !== "active") return;
      addLine("system", "Тиша вже " + Math.round(IDLE_LIMIT_MS / 1000) + " с — ставлю Єву на паузу, щоб не витрачати хвилини. Вона все пам'ятає.");
      pauseEva();
    }, IDLE_LIMIT_MS);
  }

  function sendEvent(event) {
    try {
      if (channel && channel.readyState === "open") { channel.send(JSON.stringify(event)); return true; }
    } catch { /* channel closing */ }
    return false;
  }
  const eventId = (prefix) => prefix + "_" + Math.random().toString(16).slice(2, 10);
  // Context the model acts on right away (it answers it out loud).
  const commentary = (content) =>
    sendEvent({ type: "session.commentary.append", content, delegation_id: null, event_id: eventId("say") });

  const defaultStart = () => (eva.wakeSupported ? { paused: true } : { greet: true });

  // opts: paused — connect and wait for «Єва, скажи»; say — tell the model this once started;
  // greet — a short «Слухаю» unless the user starts talking; switched — first words in a new voice.
  async function start(opts = {}) {
    if (!BACKEND) {
      addLine("system", "Не вказано адресу бекенду (web/config.js або ?backend=…).");
      return;
    }
    const my = ++attempt;
    eva.pending = opts;
    if (eva.mode !== "switching") eva.mode = "connecting";
    showState();
    updateButtons();
    try {
      // A voice switch / new conversation keeps the microphone open: no new permission, faster.
      const live = mic && mic.getAudioTracks().some((t) => t.readyState === "live");
      const stream = live ? mic : await navigator.mediaDevices.getUserMedia({
        // Browser echo cancellation keeps the agent from hearing (and interrupting) itself.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (my !== attempt) { if (stream !== mic) stream.getTracks().forEach((t) => t.stop()); return; }
      mic = stream;
      const peer = new RTCPeerConnection();
      pc = peer;
      peer.ontrack = (e) => {
        $("remote").srcObject = e.streams[0];
        if (gate) gate.listenTo(e.streams[0]);
      };
      // A voice switch keeps the gate too (it is built on the same mic): no new AudioContext.
      if (gate && (gate.stream !== mic || !gate.alive())) { gate.close(); gate = null; }
      if (!gate) {
        const newGate = await createNoiseGate(mic, onGateSignal);
        if (my !== attempt) { if (newGate) newGate.close(); return; }
        gate = newGate;
      }
      if (gate) peer.addTrack(gate.track, mic);
      else mic.getTracks().forEach((track) => peer.addTrack(track, mic));
      channel = peer.createDataChannel("oai-events");
      wireCall(peer, channel);

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      sessionVoice = voice;
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ sdp: offer.sdp, voice: voice || undefined, speed, style, prompt: PROMPT_VARIANT, user_text: opts.userText || undefined }),
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
    } catch (err) {
      if (my !== attempt) return;
      const msg = err && err.name === "NotAllowedError" ? "Немає доступу до мікрофона" : (err.message || String(err));
      addLine("system", msg);
      stop("Не вдалося підключитись", "err");
    }
  }

  // The current call's connection and server events (a standby call gets them when swapped in).
  function wireCall(peer, ch) {
    peer.onconnectionstatechange = () => {
      if (pc !== peer) return;
      const state = peer.connectionState;
      clearTimeout(dropTimer);
      // The server ends the call itself to restart it in a new voice: check before giving up.
      if (state === "failed" || state === "closed") onSessionClosed("З'єднання втрачено", "err");
      // "disconnected" is often a network blip that recovers by itself (Wi-Fi switch etc.).
      else if (state === "disconnected") {
        setStatus("Зв'язок перервався — відновлюю…", "busy");
        dropTimer = setTimeout(() => { if (pc === peer) onSessionClosed("З'єднання втрачено", "err"); }, 8000);
      } else if (state === "connected") showState();
    };
    ch.onmessage = (e) => { if (pc === peer) onServerEvent(e.data); };
    ch.onclose = () => { if (pc === peer && eva.mode !== "connecting" && eva.mode !== "switching") onSessionClosed(); };
  }

  // session.started: the call is up — pause, or talk.
  function applyStart() {
    const opts = eva.pending || {};
    eva.pending = null;
    if (opts.paused) { enterPause(false); return; }
    eva.mode = "active";
    setRemoteVolume(1);
    startPhraseListener();
    if (opts.switched) {
      // A new voice is a new session: carry on as if nothing happened — no «тепер я іншим голосом».
      // Asked by voice: nothing to say — she just listens on in the new voice.
      if (opts.interrupted) {
        commentary("Your voice was just changed while you were saying: «" + opts.interrupted.slice(-500) +
          "». Continue exactly that answer in the new voice from where it stopped (its last few words may not have been heard) — do not start it over, do not mention the voice change.");
      }
    } else if (opts.userText) {
      Promise.resolve(opts.note).then((n) => commentary(withNote(n, ANSWER_LAST)));
    } else if (opts.request) {
      Promise.resolve(opts.note).then((n) => commentary(withNote(n, opts.request)));
    } else if (opts.say) {
      commentary(opts.say);
    } else if (opts.greet) {
      // A fresh call after a wake: the user finished the phrase seconds ago — greet at once.
      greetIfQuiet(opts.heard, opts.note, opts.fresh ? 0 : 700);
    }
    showState();
    updateButtons();
    touchActivity();
  }

  // Close the call but keep Єва's mode (pause, voice switch); stop() also turns her off.
  // keepMic: a new session follows right away (voice switch, new conversation).
  function teardown({ keepMic = false } = {}) {
    attempt += 1;
    clearTimeout(dropTimer);
    clearTimeout(idleTimer);
    sendEvent({ type: "session.close" });
    if (pc) { pc.close(); pc = null; }
    if (gate && keepMic) gate.listenTo(null);
    else if (gate) { gate.close(); gate = null; }
    if (mic && !keepMic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    channel = null;
    closeBubbles();
    bargeReset();
    barge.recentAssistant = "";
  }

  function stop(text = "Не підключено", state = "") {
    dropStandby();
    teardown();
    stopPhraseListener();
    clearTimeout(eva.pauseTimer);
    eva.mode = "off";
    eva.pending = null;
    setStatus(text, state);
    updateButtons();
    refreshGoogle(); // picks up a voice the agent changed by voice during the call
  }

  // ── pause («Дякую, Єва») / wake («Єва, скажи») ──
  function enterPause(announce) {
    eva.mode = "waiting";
    eva.recentUser = "";
    eva.thinking = false;
    clearTimeout(idleTimer);
    sendEvent({ type: "session.input_audio.mute", event_id: eventId("mute") }); // the model hears nothing
    bargeReset(); // and nothing it says is played while waiting (see setRemoteVolume)
    closeBubbles();
    if (announce) {
      addLine("system", eva.wakeSupported
        ? (bgListen
          ? "Пауза. Єва слухає фоном і запам'ятовує, про що говорять поруч, — зможе нагадати. Скажіть «Єва, скажи», щоб продовжити."
          : "Пауза. Скажіть «Єва, скажи», щоб продовжити — Єва все пам'ятає.")
        : "Пауза. Натисніть «Продовжити» — Єва все пам'ятає.");
    }
    clearTimeout(eva.pauseTimer);
    schedulePauseClose(Date.now());
    startPhraseListener();
    showState();
    updateButtons();
  }

  // The paused call is closed after PAUSE_CLOSE_MS — unless a tool is still running or Єва waits
  // for «так/ні» (that confirmation belongs to this session): then ask again a bit later.
  function schedulePauseClose(pausedAt) {
    clearTimeout(eva.pauseTimer);
    eva.pauseTimer = setTimeout(async () => {
      if (eva.mode !== "waiting" || !pc) return;
      if (Date.now() - pausedAt < PAUSE_HOLD_MAX_MS) {
        let me = null;
        try {
          const res = await fetch(BACKEND + "/api/me", { headers: headers() });
          if (res.ok) me = await res.json();
        } catch { /* offline: close as usual */ }
        if (eva.mode !== "waiting" || !pc) return;
        if (me && me.keep_session) { schedulePauseClose(pausedAt); return; }
      }
      teardown({ keepMic: true }); // the wake phrase listener needs it anyway; the next wake skips getUserMedia
    }, PAUSE_CLOSE_MS);
  }

  function pauseEva() {
    if (eva.mode !== "active") return; // both recognisers may report the same «Дякую, Єва»
    steer("pause");
    enterPause(true);
  }

  function postOverheard(text) {
    fetch(BACKEND + "/api/overheard", { method: "POST", headers: headers(), body: JSON.stringify({ text }) })
      .catch(() => {});
  }

  // What she heard in the background during the pause, as a commentary ("" if nothing).
  async function overheardNote() {
    if (!bgListen) return "";
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 4000);
      const res = await fetch(BACKEND + "/api/overheard/digest", { method: "POST", headers: headers(), signal: ctrl.signal });
      clearTimeout(timer);
      return res.ok ? (await res.json()).commentary || "" : "";
    } catch { return ""; }
  }

  function showBgListen() {
    $("bgListen").checked = bgListen;
    $("bgListenInfo").textContent = bgListen
      ? "Увімкнено: Єва запам'ятовує, про що говорять поруч, і може нагадати."
      : "Вимкнено: на паузі Єва нічого не запам'ятовує.";
  }

  function toggleBgListen() {
    bgListen = $("bgListen").checked;
    store.set("va-bg-listen", bgListen ? "1" : "0");
    showBgListen();
    showState();
    // Turned off: forget what was already heard, it must not reach her.
    if (!bgListen) fetch(BACKEND + "/api/overheard", { method: "DELETE", headers: headers() }).catch(() => {});
    addLine("system", bgListen
      ? "На паузі Єва слухатиме фоном і запам'ятовуватиме, про що говорять поруч."
      : "Фонове слухання вимкнено: на паузі Єва нічого не запам'ятовує.");
  }

  // The model was muted and never heard the phrase: hand it what followed «Єва, скажи», together
  // with what she overheard during the pause (fetched while the call connects, not before it).
  const withNote = (note, text) => (note ? note + "\n\n" + text : text);
  const ANSWER_LAST = "The user woke you and asked something — it is their last message in the history. " +
    "Answer it now; do not repeat their words.";

  function wakeEva(rest, heard = "") {
    if (eva.mode !== "waiting") return;
    clearTimeout(eva.pauseTimer);
    const note = overheardNote(); // a promise: never delays the wake
    const request = rest ? "The user just said to you: «" + rest + "». Answer it." : "";
    if (rest) addLine("user", "Єва, скажи, " + rest);
    if (pc && channel && channel.readyState === "open") {
      eva.mode = "active";
      sendEvent({ type: "session.input_audio.unmute", event_id: eventId("unmute") });
      setRemoteVolume(1);
      if (request) note.then((n) => commentary(withNote(n, request)));
      else greetIfQuiet(heard, note);
      showState();
      updateButtons();
      touchActivity();
    } else {
      // The pause closed the call: reopen it right away. What they asked goes into the new call's
      // history as their own message — quoted in a commentary, GPT-Live sometimes read it back.
      start({ userText: rest ? "Єва, скажи, " + rest : "", greet: !rest, heard, note, fresh: true });
    }
  }

  // Same wording as WAKE_GREETING in voice/live_driver.py.
  const wakeGreeting = (phrase) =>
    "The user just called you: «" + phrase + "». You are back and listening — let them hear it. " +
    "Reply right away in two to four words that match it: to «привіт», «вітаю», «гей» greet back " +
    "warmly («Привіт! Що робимо?», «О, привіт! Слухаю»), otherwise a short «Так, слухаю» or " +
    "«Слухаю тебе». Vary it, then wait.";

  // A greeting tells the user Єва is on. Skipped only when they already went on talking (the model
  // answers that) or she is already speaking; a noise at the wrong moment just delays it a little.
  // note: what she overheard during the pause — delivered with the greeting, or alone if skipped.
  // note: a string or a promise of one (overheardNote) — never waited for before the greeting.
  function greetIfQuiet(heard = "", note = "", delayMs = 700) {
    const wokeAt = Date.now();
    const phrase = heard || "Єва, скажи";
    const giveUpAt = wokeAt + 4000;
    const tryGreet = async () => {
      if (eva.mode !== "active") return;
      const n = await Promise.resolve(note);
      if (eva.mode !== "active") return;
      if (eva.lastUserAt > wokeAt || barge.audible) { if (n) commentary(n); return; }
      if (barge.speaking && Date.now() < giveUpAt) { setTimeout(tryGreet, 300); return; }
      commentary(withNote(n, wakeGreeting(phrase)));
    };
    setTimeout(tryGreet, delayMs);
  }

  // The browser's own recogniser (free; Chrome, Edge, Safari) runs the whole time Єва is on: it
  // hears «Єва, скажи» while the model is muted, and «Дякую, Єва» during the conversation too —
  // a second pair of ears, as GPT-Live's transcript sometimes drops the name («Дякую»). It stops
  // by itself after a while of silence, so it is restarted.
  function startPhraseListener() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR || !eva.wakeSupported || eva.recognizer) return;
    let rec;
    try { rec = new SR(); } catch { eva.wakeSupported = false; showState(); updateButtons(); return; }
    rec.lang = "uk-UA";
    rec.continuous = true;
    rec.interimResults = true; // «Дякую, Єва» cuts her off as soon as it is heard
    rec.maxAlternatives = 3;
    let retryMs = 300;
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        for (let k = 0; k < result.length; k++) {
          const text = result[k].transcript;
          if (eva.mode === "active" && isStop(text)) { pauseEva(); return; }
          // Voice change: only the recogniser's best guess (k = 0), and not while Єва is talking —
          // this recogniser has no echo cancellation; GPT-Live's own transcript covers that case.
          if (eva.mode === "active" && result.isFinal && k === 0 && !barge.audible && VOICE_REQUEST_RE.test(text)) {
            requestVoiceChange(text);
            return;
          }
          // Wake on the final text only: it carries the whole request after «Єва, скажи».
          if (eva.mode === "waiting" && result.isFinal) {
            const rest = matchWake(text);
            if (rest !== null) { wakeEva(rest, text.trim()); return; }
          }
        }
        // Paused, not switched off: what is said nearby is kept for her (voice/background.py).
        // Not a phrase that names her: a wake phrase the recogniser mangled is the user, not background.
        if (bgListen && eva.mode === "waiting" && result.isFinal && !isStop(result[0].transcript)
            && !evaTokens(result[0].transcript).some((w) => EVA_NAME_FORMS.has(w))) {
          postOverheard(result[0].transcript);
        }
      }
    };
    rec.onerror = (e) => {
      if (["not-allowed", "service-not-allowed", "language-not-supported"].includes(e.error)) {
        eva.wakeSupported = false;
        addLine("system", "Браузер не дає слухати «Єва, скажи» — натискайте «Продовжити».");
        showState();
        updateButtons();
      } else if (e.error === "network") {
        retryMs = 3000;
      }
    };
    rec.onend = () => {
      if (eva.recognizer !== rec) return;
      eva.recognizer = null;
      if (eva.mode !== "off" && eva.wakeSupported) setTimeout(startPhraseListener, retryMs);
    };
    eva.recognizer = rec;
    try { rec.start(); } catch { eva.recognizer = null; }
  }

  function stopPhraseListener() {
    const rec = eva.recognizer;
    eva.recognizer = null;
    if (rec) { rec.onend = null; try { rec.abort(); } catch { /* already stopped */ } }
  }

  // GPT-Live voices are fixed per session: a new voice (or a fresh start) is a new session that
  // the server seeds with the conversation so far, so Єва remembers everything.
  async function restartSession(mode, opts) {
    const paused = eva.mode === "waiting";
    if (eva.mode === "off" || eva.mode === "connecting") return;
    if (paused && !pc) return; // closed during a pause: the next «Єва, скажи» opens it fresh
    dropStandby();
    clearTimeout(eva.pauseTimer);
    teardown({ keepMic: true });
    eva.mode = mode;
    await start(paused ? { paused: true } : opts);
  }

  // The server closed the call: after change_voice (asked by voice) to restart it in the new
  // voice — otherwise it really ended.
  async function onSessionClosed(text = "Розмову завершено", state = "") {
    if (eva.mode === "switching" || eva.mode === "off" || eva.mode === "connecting") return;
    const wasPaused = eva.mode === "waiting";
    dropStandby();
    teardown({ keepMic: true });
    eva.mode = "connecting";
    showState();
    let me = null;
    try {
      const res = await fetch(BACKEND + "/api/me", { headers: headers() });
      if (res.ok) me = await res.json();
    } catch { /* offline */ }
    if (eva.mode !== "connecting") return; // the user did something meanwhile
    if (me && me.reconnect && voices.some((x) => x.id === me.voice)) {
      voice = me.voice;
      store.set("va-voice", voice);
      showVoice();
      addLine("system", "Голос змінено на «" + voiceLabel(voice) + "»" + (me.voice_note ? " (" + me.voice_note + ")" : "") + ".");
      eva.mode = "switching";
      await start(wasPaused ? { paused: true } : { switched: true, afterCommand: true });
      return;
    }
    if (gate) { gate.close(); gate = null; }
    if (mic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    if (wasPaused) { eva.mode = "waiting"; startPhraseListener(); showState(); updateButtons(); return; }
    stop(text, state);
  }

  // ── voice settings ──
  const voiceLabel = (id) => (voices.find((x) => x.id === id) || { label: id }).label;

  // The picker may show a voice that isn't applied yet (to listen to it first).
  function showVoice(keepSelection = false) {
    if (!keepSelection) $("voice").value = voice;
    const picked = voices.find((x) => x.id === $("voice").value);
    $("voiceInfo").textContent = picked ? picked.description : "";
    $("voiceApply").hidden = !picked || picked.id === voice;
  }

  async function loadVoices() {
    try {
      const res = await fetch(BACKEND + "/api/voices");
      if (!res.ok) return;
      const data = await res.json();
      voices = data.voices || [];
      if (!voices.some((x) => x.id === voice)) voice = data.default;
      if (!voices.some((x) => x.id === voice) && voices.length) voice = voices[0].id;
      const group = (label, list) => {
        const g = document.createElement("optgroup");
        g.label = label;
        g.append(...list.map((x) => new Option(x.label, x.id)));
        return g;
      };
      $("voice").replaceChildren(
        group("Жіночі", voices.filter((x) => x.feminine)),
        group("Чоловічі", voices.filter((x) => !x.feminine)),
      );
      $("voice").disabled = !voices.length;
      $("voicePreview").disabled = !voices.length;
      showVoice();
    } catch { /* backend offline: picker stays disabled */ }
  }

  // What Єва is saying right now (the transcript runs slightly ahead of the audio).
  const speakingNow = () => (barge.audible && openMsg.assistant ? openMsg.assistant.textContent.trim() : "");

  async function applyVoice() {
    const id = $("voice").value;
    if (!voices.some((x) => x.id === id) || id === voice) return;
    const interrupted = eva.mode === "active" ? speakingNow() : "";
    voice = id;
    store.set("va-voice", id);
    showVoice();
    // Not awaited: the new call carries the voice itself (/api/session), this only keeps the server in step.
    fetch(BACKEND + "/api/voice", { method: "POST", headers: headers(), body: JSON.stringify({ voice: id }) }).catch(() => {});
    if (eva.mode === "active") {
      const later = !voiceNow && answering();
      addLine("system", later
        ? "Голос зміниться на «" + voiceLabel(id) + "», щойно Єва договорить."
        : "Голос змінено на «" + voiceLabel(id) + "».");
      switchVoiceFast(later);
    } else if (eva.mode === "waiting") {
      addLine("system", "Голос змінено на «" + voiceLabel(id) + "».");
      restartSession("switching", { switched: true, interrupted });
    }
  }

  // ── fast voice switch ──
  // Opening a GPT-Live session takes seconds and its voice is fixed, so the call in the new voice
  // opens beside the current one — it hears nothing and plays nothing — while Єва goes on talking.
  // Once it is up it takes over the mic and the speaker («Одразу»), or waits until she has finished
  // («Після відповіді»). What the old call said meanwhile is handed over, so nothing is lost.
  let standby = null;
  let voiceTimer = null;
  const STANDBY_WAIT_MS = 60 * 1000; // a ready call waiting for her to finish — longer: start over
  // A swap doesn't derail her (unlike speed/style), so a short silence is enough: her answer is over.
  const SWAP_QUIET_MS = 800;
  const stillAnswering = () => barge.audible || eva.thinking || Date.now() - eva.lastAudibleAt < SWAP_QUIET_MS;

  function dropStandby() {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    const s = standby;
    standby = null;
    if (!s) return;
    clearTimeout(s.timer);
    try { if (s.channel.readyState === "open") s.channel.send(JSON.stringify({ type: "session.close" })); } catch { /* closing */ }
    s.peer.close();
  }

  // The log from `mark` on: what the old call said / heard after the new one got the history.
  function logMark() {
    const last = $("log").lastElementChild;
    return { el: last, len: last ? last.textContent.length : 0 };
  }
  function logSince(mark, skip) {
    const lines = [];
    // The bubble open at the mark goes in whole: the history had only its start, and the model
    // took that for an unfinished answer and told it again.
    const whole = mark.el && mark.len < mark.el.textContent.length;
    let el = mark.el ? (whole ? mark.el : mark.el.nextElementSibling) : $("log").firstElementChild;
    for (; el; el = el.nextElementSibling) {
      if (el === skip || el.classList.contains("system") || el.classList.contains("empty")) continue;
      const text = el.textContent.trim();
      if (text) lines.push((el.classList.contains("user") ? "User: " : "You: ") + text);
    }
    return lines.join("\n");
  }

  // announce: say «Голос змінено» when it actually switches (the user was told «зміниться»).
  async function switchVoiceFast(announce = false) {
    dropStandby();
    if (!pc || !mic) { restartSession("switching", { switched: true }); return; }
    const s = { voice, mark: logMark(), ready: false, announce };
    standby = s;
    const fail = () => {
      if (standby !== s) return;
      dropStandby();
      switchVoiceWhenQuiet(s.announce); // the old way: reconnect (once she is quiet, if asked to wait)
    };
    s.timer = setTimeout(fail, 15000);
    try {
      const peer = new RTCPeerConnection();
      s.peer = peer;
      peer.ontrack = (e) => { s.stream = e.streams[0]; };
      // No mic until the swap: replaceTrack later needs no renegotiation.
      s.sender = peer.addTransceiver("audio", { direction: "sendrecv" }).sender;
      s.channel = peer.createDataChannel("oai-events");
      s.channel.onmessage = (e) => {
        if (standby !== s) return;
        let event;
        try { event = JSON.parse(e.data); } catch { return; }
        if (event.type === "session.started") { s.ready = true; s.readyAt = Date.now(); clearTimeout(s.timer); swapWhenQuiet(s); }
        else if (event.type === "session.closed") fail();
      };
      s.channel.onclose = fail;
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        // keep_old: the current call goes on until the page swaps (the server would end it).
        body: JSON.stringify({ sdp: offer.sdp, voice: s.voice, speed, style, prompt: PROMPT_VARIANT, keep_old: true }),
      });
      if (standby !== s) return;
      if (!res.ok) throw new Error("session " + res.status);
      const { sdp } = await res.json();
      if (standby !== s) return;
      await peer.setRemoteDescription({ type: "answer", sdp });
    } catch {
      fail();
    }
  }

  function swapWhenQuiet(s) {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    if (standby !== s) return;
    if (eva.mode !== "active" || !pc) { dropStandby(); if (eva.mode === "waiting") restartSession("switching", {}); return; }
    // Never mid-sentence of the user (half of it would go to each call — prod: the new call heard
    // only «…чи сьогодні день»). The gate notices speech a moment late and the transcript lags
    // more, so a user who spoke in the last second still counts. «Після відповіді» also waits for her.
    const userTalking = barge.speaking || Date.now() - barge.speechEndedAt < 1000 || Date.now() - eva.lastUserAt < 1000;
    if (userTalking || (!voiceNow && stillAnswering())) {
      if (Date.now() - s.readyAt > STANDBY_WAIT_MS) { dropStandby(); switchVoiceWhenQuiet(s.announce); return; }
      voiceTimer = setTimeout(() => swapWhenQuiet(s), 100);
      return;
    }
    swapIn(s);
  }

  async function swapIn(s) {
    standby = null;
    const open = openMsg.assistant;
    // Mid-answer also counts a short gap between her sentences (the bubble is still open).
    // «Після відповіді» swaps only once she has finished: nothing to continue then.
    const midAnswer = voiceNow && (barge.audible || Date.now() - eva.lastAudibleAt < 1500);
    const interrupted = open && midAnswer ? open.textContent.trim() : "";
    const said = logSince(s.mark, interrupted ? open : null);
    teardown({ keepMic: true }); // the old call: closed, its bubbles done
    pc = s.peer;
    channel = s.channel;
    sessionVoice = s.voice;
    wireCall(s.peer, s.channel);
    if (s.stream) {
      $("remote").srcObject = s.stream;
      if (gate) gate.listenTo(s.stream);
    }
    s.peer.ontrack = (e) => { $("remote").srcObject = e.streams[0]; if (gate) gate.listenTo(e.streams[0]); };
    try {
      await s.sender.replaceTrack(gate ? gate.track : mic.getAudioTracks()[0]);
    } catch { /* closed meanwhile: its close handler takes over */ }
    if (pc !== s.peer) return;
    setRemoteVolume(1);
    if (s.announce) addLine("system", "Голос змінено на «" + voiceLabel(s.voice) + "».");
    // The history ended when this call opened; what came after is handed over here. The model
    // answers a context message out loud, so it is told plainly when to stay silent.
    const context = said ? "After your history ended, the conversation went on (the user heard all of it; " +
      "these replies of yours are finished — never repeat or retell them):\n" + said.slice(-1500) + "\n\n" : "";
    if (interrupted) {
      commentary(context + "Your voice was just changed while you were saying: «" + interrupted.slice(-500) +
        "». Continue exactly that answer in the new voice from where it stopped (its last few words may not have been heard) — do not start it over, do not mention the voice change.");
    } else if (context) {
      sendEvent({ type: "session.instructions.append", delegation_id: null, event_id: eventId("ctx"),
        content: context + "Say nothing now — no acknowledgement. Wait silently for the user's next words." });
    }
    showState();
    updateButtons();
    touchActivity();
  }

  // Without a standby call: reconnect in the new voice, once she is quiet if asked to wait.
  function switchVoiceWhenQuiet(announce = false) {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    if (sessionVoice === voice || eva.mode === "off" || eva.mode === "connecting" || eva.mode === "switching") return; // a new call already has it
    if (eva.mode === "active" && !voiceNow && answering()) { voiceTimer = setTimeout(() => switchVoiceWhenQuiet(announce), 300); return; }
    if (announce) addLine("system", "Голос змінено на «" + voiceLabel(voice) + "».");
    restartSession("switching", { switched: true, interrupted: eva.mode === "active" ? speakingNow() : "" });
  }

  function showVoiceNow() {
    $("voiceNow").checked = voiceNow;
    $("voiceNowInfo").textContent = voiceNow
      ? "Одразу: Єва договорить відповідь уже новим голосом."
      : "Після відповіді: новий голос — коли Єва договорить.";
  }

  function toggleVoiceNow() {
    voiceNow = $("voiceNow").checked;
    store.set("va-voice-now", voiceNow ? "1" : "0");
    showVoiceNow();
    // A waiting switch happens now.
    if (voiceNow && standby && standby.ready) swapWhenQuiet(standby);
    else if (voiceNow && voiceTimer && !standby) switchVoiceWhenQuiet(true);
  }

  // Speed and manner are instructions to the model. One that arrives while she talks derails the
  // answer (instructions.append is also how a barge-in stops her), so it waits until she has been
  // quiet for a moment — the gate hears her real audio. A newer setting replaces a waiting one.
  let pendingDelivery = "";
  let deliveryTimer = null;
  const DELIVERY_QUIET_MS = 2500;
  // She is talking, thinking, or stopped only a moment ago (between sentences).
  const answering = () => barge.audible || barge.speaking || eva.thinking || Date.now() - eva.lastAudibleAt < DELIVERY_QUIET_MS;

  async function applyDelivery() {
    speed = $("speed").value;
    style = $("style").value;
    store.set("va-speed", speed);
    store.set("va-style", style);
    try {
      const res = await fetch(BACKEND + "/api/voice", { method: "POST", headers: headers(), body: JSON.stringify({ speed, style }) });
      const data = res.ok ? await res.json() : null;
      if (data && data.instruction) { pendingDelivery = data.instruction; sendDeliveryWhenQuiet(); }
    } catch { /* offline: the next session starts with these settings anyway */ }
  }

  // ?debug=1: evaDebug() in the console shows the page's state.
  if (params.get("debug") === "1") {
    window.evaDebug = () => ({
      mode: eva.mode, pendingDelivery, audible: barge.audible, speaking: barge.speaking,
      thinking: eva.thinking, quietMs: Date.now() - eva.lastAudibleAt, channel: channel && channel.readyState,
    });
  }

  function sendDeliveryWhenQuiet() {
    clearTimeout(deliveryTimer);
    if (!pendingDelivery) return;
    if (!channel || eva.mode !== "active") { pendingDelivery = ""; return; } // a new session gets them at start
    if (answering()) {
      deliveryTimer = setTimeout(sendDeliveryWhenQuiet, 300);
      return;
    }
    sendEvent({ type: "session.instructions.append", content: pendingDelivery, delegation_id: null, event_id: eventId("style") });
    pendingDelivery = "";
  }

  function showVolume() {
    $("volume").value = String(Math.round(volume * 100));
    $("volumeValue").textContent = Math.round(volume * 100) + "%";
  }

  let preview = null;
  function stopPreview() {
    if (preview) preview.pause();
    preview = null;
    $("voicePreview").textContent = "▶ Прослухати";
  }
  function togglePreview() {
    if (preview) { stopPreview(); return; }
    const audio = new Audio("samples/" + encodeURIComponent($("voice").value) + ".m4a");
    audio.volume = volume;
    audio.onended = stopPreview;
    audio.onerror = () => { stopPreview(); addLine("system", "Зразок цього голосу недоступний."); };
    preview = audio;
    $("voicePreview").textContent = "■ Стоп";
    audio.play().catch(stopPreview);
  }

  // Heard «зміни голос …» ourselves: switch at once instead of waiting for the slower Live
  // transcript (that path, on the server, stays as a fallback; the server dedups the two).
  async function requestVoiceChange(text) {
    if (eva.voiceRequestBusy) return;
    eva.voiceRequestBusy = true;
    try {
      const res = await fetch(BACKEND + "/api/voice-request", { method: "POST", headers: headers(), body: JSON.stringify({ text }) });
      const data = res.ok ? await res.json() : null;
      if (data && data.switched && eva.mode === "active" && voices.some((x) => x.id === data.voice)) {
        voice = data.voice;
        store.set("va-voice", voice);
        showVoice();
        addLine("system", "Голос змінено на «" + voiceLabel(voice) + "» (почула: «" + text.trim().slice(0, 80) + "»).");
        restartSession("switching", { switched: true });
      }
    } catch { /* offline: the server-side path may still switch */ } finally {
      eva.voiceRequestBusy = false;
    }
  }

  async function newConversation() {
    await fetch(BACKEND + "/api/conversation", { method: "DELETE", headers: headers() }).catch(() => {});
    clearLog();
    addLine("system", "Нова розмова — Єва почне з чистого аркуша.");
    restartSession("connecting", { greet: true });
  }

  async function refreshGoogle() {
    if (!BACKEND) return;
    try {
      const res = await fetch(BACKEND + "/api/me", { headers: headers() });
      if (res.status === 401) { showGate(); return; }
      if (!res.ok) return;
      const { google, voice: serverVoice, speed: serverSpeed, style: serverStyle } = await res.json();
      // Set on the server only when chosen there (e.g. asked by voice) — adopt it then.
      if (serverVoice && serverVoice !== voice && voices.some((x) => x.id === serverVoice)) {
        voice = serverVoice;
        store.set("va-voice", voice);
        showVoice();
      }
      // «Говори повільніше» by voice: show it (a restarted server reports "normal" — keep ours then).
      if (serverSpeed && serverSpeed !== "normal" && serverSpeed !== speed) { speed = serverSpeed; store.set("va-speed", speed); $("speed").value = speed; }
      if (serverStyle && serverStyle !== "normal" && serverStyle !== style) { style = serverStyle; store.set("va-style", style); $("style").value = style; }
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
    const wasOn = eva.mode !== "off";
    if (wasOn) stop();
    clearLog();
    fetch(BACKEND + "/api/conversation", { method: "DELETE", headers: headers() }).catch(() => {});
    addLine("system", email ? "Новий користувач: " + email + " — нова розмова." : "Акаунт Google відключено — нова розмова.");
    if (wasOn) start(defaultStart());
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
    $("talk").addEventListener("click", () => (eva.mode === "off" ? start(defaultStart()) : stop()));
    $("pause").addEventListener("click", pauseEva);
    $("resume").addEventListener("click", () => wakeEva(""));
    $("bgListen").addEventListener("change", toggleBgListen);
    showBgListen();
    $("voiceNow").addEventListener("change", toggleVoiceNow);
    showVoiceNow();
    $("newChat").addEventListener("click", newConversation);
    $("voice").addEventListener("change", () => { stopPreview(); showVoice(true); });
    $("voicePreview").addEventListener("click", togglePreview);
    $("voiceApply").addEventListener("click", applyVoice);
    $("speed").value = speed;
    $("style").value = style;
    $("speed").addEventListener("change", applyDelivery);
    $("style").addEventListener("change", applyDelivery);
    showVolume();
    $("volume").addEventListener("input", (e) => {
      volume = Math.min(1, Math.max(0, Number(e.target.value) / 100));
      store.set("va-volume", String(volume));
      showVolume();
      setRemoteVolume(bargeLevel());
      if (preview) preview.volume = volume;
    });
    updateButtons();
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
    window.addEventListener("pagehide", () => { if (eva.mode !== "off") stop(); });
    if (!BACKEND) { addLine("system", "Не вказано адресу бекенду."); return; }
    const cfg = await wakeBackend();
    if (cfg) {
      if (cfg.access_code_required && !accessCode) showGate();
      if (!cfg.google_login) $("googleConnect").disabled = true;
      await loadVoices();
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
