"""Talk - push-to-talk voice line from a phone to your Claude Code agents.

The phone page records speech; Whisper (local, free) turns it into text; the agent answers through `claude -p` in its
own home folder on the Claude subscription; the page reads the reply aloud in that agent's voice. Listens on 127.0.0.1
only and is reached through `tailscale serve` (tailnet only), which adds the caller's Tailscale login; only logins
listed in settings.json may talk, and only through the host names listed there. Each agent keeps one voice conversation (its session id in state.json), separate from
its session in the Claude app.

    py -3 talk_server.py           serve on 127.0.0.1:<port> (default 2590)
    py -3 talk_server.py --check   self-check: reply streaming, Whisper on spoken numbers, the laptop voice

Replies stream: the page gets the agent's words as they are written and speaks each sentence as soon as it is
complete. Voice turns run without connectors (MCP servers), which saves ~2 s of startup.
"""
import io, json, os, queue, re, shutil, subprocess, sys, tempfile, threading, time, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
NO_WINDOW = 0x08000000


def load_settings():
    """settings.json holds everything personal to this install; see settings.example.json for every key."""
    try:
        return json.loads((HERE / "settings.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit("settings.json not found. Copy settings.example.json to settings.json and edit it.")
    except ValueError as e:
        sys.exit(f"settings.json is not valid JSON: {e}")


SET = load_settings()
PORT = int(SET.get("port", 2590))
CLAUDE = SET.get("claude_path") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
ALLOWED = {a.lower() for a in SET.get("allowed_logins", [])}   # Tailscale logins that may use the page
USER = SET.get("user_name") or "the user"
U = USER[:1].upper() + USER[1:]                        # for the start of a sentence
# The names this server may be reached by: your laptop's tailnet name, from settings.json "hosts". A web page that
# tricks a browser into calling 127.0.0.1 (DNS rebinding) arrives with its own site's name in the Host header, so it is
# refused here.
HOSTS = {h.lower() for h in SET.get("hosts", [])} | {"127.0.0.1", "localhost"}


def host_ok(header):
    h = (header or "").strip().lower()
    name, _, port = h.rpartition(":")
    return (name if port.isdigit() else h).strip("[]") in HOSTS

STATE, LOG = HERE / "state.json", HERE / "talk.log"
CLIPS, KEEP_CLIPS = HERE / "clips", int(SET.get("keep_clips", 30))   # the last recordings, so misheard ones can be checked
# Agents on the line. Each runs in its home folder, where a session is that agent; "agent" (an agent file name) would run
# that agent file instead, for one with no home session. "add_dirs" lists extra folders the agent may work in.
AGENTS = {a["id"]: dict(name=a["name"], home=Path(a["home"]), voice=a["voice"], add_dirs=a.get("add_dirs", []),
                        agent=a.get("agent")) for a in SET.get("agents", [])}
# Laptop voices: Kokoro 82M (Apache-2.0) through kokoro-onnx (MIT), model files in kokoro/. Voice ids: a = American,
# b = British; f = woman, m = man.
KOKORO = HERE / "kokoro"
ACCENT = {"af": "US woman", "am": "US man", "bf": "UK woman", "bm": "UK man"}
VOICE_NOTE = (f"This conversation is by voice. {U} speaks into a phone; their words reach you through speech-to-text, "
              "and your reply is read aloud to them. Answer in short spoken sentences: no tables, no markdown, no file "
              "paths unless they ask. When they give numbers, read them back so they can hear them, and if a number "
              "seems missing or garbled, ask rather than guess. Speech-to-text can drop words, often at the start. If "
              "what you received is a fragment, or could mean more than one thing, don't fill in the gaps or answer a "
              "guessed question: say exactly what you heard and ask them to say it again. If your playbook has you "
              "report anything at the start of a session, answer their question first and keep that report to one "
              "sentence. Keep your records and files exactly as you normally would.")
# Whisper copies the style of this prompt: example numbers make "five nine one nine" come out as 5919, not words.
# List your own jargon, names and units here (settings.json "vocabulary").
WORDS = SET.get("vocabulary", "")
# The subscription, never API credits: a key in the environment would make `claude -p` bill the API.
PLAN_ENV = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}

_model, _model_lock = None, threading.Lock()
_tts, _tts_lock = None, threading.Lock()
_agent_locks = {a: threading.Lock() for a in AGENTS}


def kokoro():
    global _tts
    if _tts is None:                                   # loaded on first use: ~1 s, and its memory only when wanted
        from kokoro_onnx import Kokoro
        _tts = Kokoro(str(KOKORO / "kokoro-v1.0.onnx"), str(KOKORO / "voices-v1.0.bin"))
    return _tts


def voices():
    with _tts_lock:
        ids = [v for v in kokoro().get_voices() if v[:2] in ACCENT]
    return [{"id": v, "label": f"{v[3:].capitalize()} ({ACCENT[v[:2]]})"} for v in ids]


def say(text, voice):
    """Speech for one chunk of a reply, as 16-bit WAV, peak-normalized so it plays loud."""
    import numpy as np
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)   # Kokoro reads 2,370 as "two, three seventy"; 2370 is right
    with _tts_lock:                                    # one at a time: each run uses every CPU core
        audio, sr = kokoro().create(text, voice=voice, speed=1.0, lang="en-gb" if voice[0] == "b" else "en-us")
    pcm = (audio / max(float(np.abs(audio).max()), 1e-6) * 0.97 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def whisper():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel("small.en", device="cpu", compute_type="int8")
        return _model


def heard_segments(audio, prompt):
    """Whisper's segments, minus ones it made up. On a quiet scrap (breath, rustle) it can invent words or repeat its
    prompt: in testing, the last 2 s of a dictation came out as a repeat of the prompt's own words, every word at probability 0.0.
    Real speech here scores well above 0.5 a word."""
    segs, _ = whisper().transcribe(audio, beam_size=5, vad_filter=True, word_timestamps=True, initial_prompt=prompt)
    for s in segs:
        if s.words and sum(w.probability for w in s.words) / len(s.words) >= MIN_WORD_P:
            yield s


MIN_WORD_P = 0.3


def transcribe(path):
    return " ".join(s.text.strip() for s in heard_segments(str(path), WORDS)).strip()


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_state_lock = threading.Lock()


def update_state(change):
    """Read, change and save state.json in one go: turns, sends to chat and New can overlap."""
    with _state_lock:
        st = load_state()
        change(st)
        STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


# Hands-free dictation: the mic stays open for minutes while the hands are busy. The phone sends a piece every 10 s;
# each is written down as it comes, so minutes of talk are safe on the laptop and the end is quick.
DICTS = {}                                             # dictation id -> its state, while it runs


def dictate_more(d, final):
    """Write down the new part of a dictation and return it. Until the end the last 2 s are held back, so a word cut
    by a piece boundary is heard whole next time; each pass starts where the last kept sentence ended."""
    from faster_whisper import decode_audio
    audio = decode_audio(str(d["path"]), sampling_rate=16000)
    d["dur"] = dur = len(audio) / 16000
    start = d["done"]
    if not final and dur - start < 4:
        return ""
    segs = heard_segments(audio[int(start * 16000):], (WORDS + " " + d["text"][-200:]).strip())
    kept, heard_any = [], False
    for s in segs:
        heard_any = True
        if not final and start + s.end > dur - 2:
            break
        kept.append(s.text.strip())
        d["done"] = start + s.end
    if not heard_any and not final:
        d["done"] = max(start, dur - 2)                # only silence so far: skip it
    new = " ".join(kept).strip()
    d["text"] = (d["text"] + " " + new).strip()
    return new


def read_log():
    rows = []
    try:
        with LOG.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def unsent(agent, rows, sent):
    """The agent's voice exchanges not yet sent to its app chat."""
    return [r for r in rows if r.get("agent") == agent and r.get("at", "") > sent.get(agent, "") and r.get("reply")]


CHAT_QUIET = int(SET.get("chat_quiet_seconds", 600))   # a voice conversation goes to the chat after this many quiet seconds


def chat_due(rows, sent, now, agents=None):
    """Agents whose unsent voice talk has been quiet for CHAT_QUIET seconds."""
    due = []
    for agent in agents or AGENTS:
        u = unsent(agent, rows, sent)
        if u and now - time.mktime(time.strptime(u[-1]["at"], "%Y-%m-%d %H:%M:%S")) >= CHAT_QUIET:
            due.append(agent)
    return due


def chat_keeper():
    """Send each voice conversation to the agent's app chat by itself, once it has been quiet for CHAT_QUIET
    seconds, so the record ends up where it belongs without anyone remembering to send it. A failed send waits 30 minutes before the next try."""
    retry_at = {}
    while True:
        time.sleep(60)
        try:
            for agent in chat_due(read_log(), load_state().get("sent", {}), time.time()):
                if time.time() < retry_at.get(agent, 0):
                    continue
                note = to_chat(agent)
                if not note.startswith("Sent"):
                    retry_at[agent] = time.time() + 1800
                    log(agent=agent, to_chat_failed=note)
        except Exception as e:
            log(to_chat_failed=str(e))


def send_all():
    """Send every agent's waiting voice talk to its app chat now. The page calls this when the phone leaves it (another
    app, the lock screen), so the app chat has it at once instead of after the quiet time."""
    rows, sent = read_log(), load_state().get("sent", {})
    for agent in AGENTS:
        if unsent(agent, rows, sent):
            try:
                note = to_chat(agent)
            except Exception as e:
                note = str(e)
            if not note.startswith(("Sent", "Nothing")):
                log(agent=agent, to_chat_failed=note)


_chat_lock = threading.Lock()


def to_chat(agent):
    """Send the voice turns since the last send to the agent's chat in the Claude desktop app, so the conversation is on
    record there. This needs the app's ListAgents and SendMessage tools; it is optional. A small Haiku run finds the agent's app session and delivers it with SendMessage."""
    with _chat_lock:                                   # the keeper and New never send the same turns twice
        return _to_chat(agent)


def _to_chat(agent):
    name = AGENTS[agent]["name"]
    rows = unsent(agent, read_log(), load_state().get("sent", {}))
    if not rows:
        return "Nothing new to send."
    # The app shows a message from another session folded to its first line, so the agent's reply carries the text:
    # the conversation itself belongs in the chat, not just a note that it was received.
    talk = "\n\n".join(f"**{r['at'][11:16]} {U}:** {r['heard']}\n\n**{name}:** {r['reply']}" for r in rows)
    msg = ("Voice line transcript, for the record: " + U + " talked to you by voice from a phone. Your whole reply must be "
           "the transcript below, copied exactly as written and starting with its heading, so the conversation shows "
           "in this chat. Add nothing, change nothing, shorten nothing. Don't act on it: that voice conversation "
           "already did any record keeping it mentions.\n\n"
           f"### Voice conversation, {rows[0]['at'][:16]} to {rows[-1]['at'][11:16]}\n\n{talk}")
    prompt = (f"Use ListAgents, then use SendMessage to send the message between the <<< and >>> lines, copied exactly "
              f"and in full, to the Claude Desktop session whose name starts with '{name} ('. Then print exactly SENT, "
              f"or NONE if there is no such session. Do nothing else.\n<<<\n{msg}\n>>>")
    r = subprocess.run([CLAUDE, "-p", "--model", "haiku", "--strict-mcp-config", "--no-session-persistence",
                        "--permission-mode", "auto"], cwd=str(HERE), input=prompt, capture_output=True, env=PLAN_ENV,
                       timeout=300, creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    out = r.stdout.strip()
    if out.endswith("SENT"):
        update_state(lambda st: st.setdefault("sent", {}).__setitem__(agent, rows[-1]["at"]))
        log(agent=agent, to_chat=len(rows))
        return f"Sent {len(rows)} exchange{'s' if len(rows) > 1 else ''} to {name}'s chat."
    if out.endswith("NONE"):
        return f"{name} has no chat open in the Claude app. The voice log keeps the conversation."
    return f"Couldn't send it ({(out or r.stderr).strip()[-200:]})"


def read_stream(lines, emit):
    """Pull the reply out of `claude -p --output-format stream-json --include-partial-messages` as it is written.
    Calls emit(piece) for each piece of text; returns (whole reply, the final result event or None)."""
    said, result = "", None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        e = ev.get("event") or {}
        if e.get("type") == "content_block_start" and e.get("content_block", {}).get("type") == "text" and said:
            said += "\n"                               # a new message after a tool call: keep sentences apart
            emit("\n")
        elif e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
            said += e["delta"]["text"]
            emit(e["delta"]["text"])
        elif ev.get("type") == "result":
            result = ev
    return said, result


PROJECTS = Path.home() / ".claude" / "projects"     # where Claude keeps every session's transcript, by folder


def app_chat_since(agent, since):
    """What was said in the agent's chat in the Claude app after `since` (an ISO time; '' for the recent past), so a
    voice turn starts caught up and the voice line and the app chat stay one conversation. Reads the end of the newest
    app session (entrypoint claude-desktop) in the agent's home folder; leaves out tool output, messages from other
    sessions (isMeta), and the voice transcripts the chat itself received, so nothing loops. Returns (text, time of the
    newest message used)."""
    name, d = AGENTS[agent]["name"], PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(AGENTS[agent]["home"]))
    chat = None
    for f in sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:8]:
        with f.open(encoding="utf-8", errors="replace") as fh:
            if any('"claude-desktop"' in line for _, line in zip(range(60), fh)):
                chat = f
                break
    if not chat:
        return "", since
    with chat.open("rb") as fh:                        # the last 4 MB is plenty: only recent talk is wanted
        fh.seek(max(0, chat.stat().st_size - 4_000_000))
        fh.readline()
        lines = fh.read().decode("utf-8", "replace").splitlines()
    said, newest = [], since
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        kind, at = e.get("type"), e.get("timestamp", "")
        if kind not in ("user", "assistant") or e.get("isMeta") or e.get("isSidechain") or at <= since:
            continue
        c = (e.get("message") or {}).get("content") or ""
        text = (c if isinstance(c, str) else
                "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")).strip()
        if not text or text.startswith(("<", "### Voice conversation", "This session is being continued")):
            continue
        said.append(f"{U if kind == 'user' else name}: {text[:800]}")
        newest = max(newest, at)
    text = "\n\n".join(said)
    return ("…" + text[-6000:] if len(text) > 6000 else text), newest


def ask(agent, text, emit):
    """One turn with the agent: emit() gets the reply as it is written; returns the whole reply. Continues the agent's
    voice conversation when there is one, after catching it up on the agent's app chat."""
    a = AGENTS[agent]
    name, home = a["name"], a["home"]
    with _agent_locks[agent]:
        st = load_state()
        sid = st.get(agent)
        # A new voice conversation gets the recent app chat; a running one, what was said there since its last turn.
        try:
            chat, seen = app_chat_since(agent, st.get("app_seen", {}).get(agent, "") if sid else "")
        except Exception:                              # catching up must never stop the turn
            chat, seen = "", ""
        if chat:
            text = ("Since your last voice turn, this was said in your chat in the Claude app. It's for context: it "
                    f"was handled there.\n\n{chat}\n\n{U}, by voice now: {text}")
        # --strict-mcp-config with no --mcp-config: no connectors. Agents work from files; saves ~2 s of startup.
        args = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--strict-mcp-config", "--permission-mode", "auto", "--append-system-prompt", VOICE_NOTE]
        for d in a["add_dirs"]:
            args += ["--add-dir", str(d)]
        if a.get("agent"):
            args += ["--agent", a["agent"]]
        if sid:
            args += ["--resume", sid]
        with tempfile.TemporaryFile() as err:
            p = subprocess.Popen(args, cwd=str(home), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                 env=PLAN_ENV, creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
            timer = threading.Timer(900, p.kill)
            timer.start()
            try:
                p.stdin.write(text)
                p.stdin.close()
                said, result = read_stream(p.stdout, emit)
                p.wait()
            finally:
                timer.cancel()
            if result is None:
                err.seek(0)
                raise RuntimeError(f"{name} didn't answer ({err.read().decode('utf-8', 'replace').strip()[:200]})")
        if result.get("is_error"):
            raise RuntimeError(f"{name} hit an error: {str(result.get('result'))[:200]}")
        def remember(st):
            if result.get("session_id"):
                st[agent] = result["session_id"]
            if seen:                                   # the app chat is caught up to here
                st.setdefault("app_seen", {})[agent] = seen
        update_state(remember)
        return said or result.get("result") or ""


def turn(agent, heard, q, info):
    """Run one turn, putting its events on q. Runs in its own thread so it finishes and is logged even if the phone
    hangs up mid-reply."""
    try:
        reply = ask(agent, heard, lambda piece: q.put({"text": piece}))
        log(agent=agent, heard=heard, reply=reply, **info)
        q.put({"done": True, "reply": reply})
    except Exception as e:
        log(agent=agent, heard=heard, error=str(e), **info)
        q.put({"error": str(e)})


def log(**row):
    row["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def dictate_piece(self, agent, q):
        """One 10 s piece of a hands-free dictation: add it, write down what's new, return the text so far."""
        did, seq = q.get("id", [""])[0], int(q.get("seq", ["-1"])[0])
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if seq == 0 and did not in DICTS:
            CLIPS.mkdir(exist_ok=True)
            ext = ".mp4" if "mp4" in self.headers.get("Content-Type", "") else ".webm"
            DICTS[did] = dict(agent=agent, path=CLIPS / (time.strftime("%Y-%m-%d %H%M%S") + " dictation" + ext),
                              seq=0, done=0.0, text="", lock=threading.Lock())
        d = DICTS.get(did)
        if not d:
            return self.send(404, {"error": "The laptop doesn't have that dictation."})
        with d["lock"]:
            if seq == d["seq"] - 1:                    # sent again after a lost reply: already have it
                return self.send(200, {"text": "", "total": d["text"]})
            if seq != d["seq"]:
                return self.send(409, {"error": f"expected piece {d['seq']}, got {seq}"})
            with d["path"].open("ab") as f:
                f.write(body)
            d["seq"] += 1
            try:
                new = dictate_more(d, False)
            except Exception:                          # the sound is kept; the end writes it down
                new = ""
        self.send(200, {"text": new, "total": d["text"]})

    def allowed(self):
        if not host_ok(self.headers.get("Host")):
            log(event="refused host", host=str(self.headers.get("Host"))[:80])
            self.send(403, {"error": "not allowed"})
            return False
        if self.headers.get("Tailscale-User-Login", "").lower() in ALLOWED:
            return True
        self.send(403, {"error": "not allowed"})
        return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self.send(200, {"ok": True})
        if not self.allowed():
            return
        if path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/agents":
            return self.send(200, [{"id": k, "name": a["name"], "voice": a["voice"]} for k, a in AGENTS.items()])
        if path == "/voices":
            return self.send(200, voices())
        if path == "/last":                            # the answer to a turn whose stream the phone lost
            q = parse_qs(urlsplit(self.path).query)
            agent, since = q.get("agent", [""])[0], q.get("since", [""])[0]
            rows = [r for r in read_log() if r.get("agent") == agent and r.get("at", "") >= since and r.get("heard")
                    and ("reply" in r or "error" in r)]
            return self.send(200, rows[-1] if rows else {"pending": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        agent = q.get("agent", [""])[0]
        if not self.allowed():
            return
        if u.path == "/say":
            voice = q.get("voice", [""])[0]
            text = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")[:1000].strip()
            if voice[:2] not in ACCENT or not text:
                return self.send(400, {"error": "bad voice or empty text"})
            try:
                return self.send(200, say(text, voice), "audio/wav")
            except Exception as e:
                return self.send(500, {"error": str(e)})
        if u.path == "/tochat" and agent == "all":     # the phone left the page: send it all, in the background
            threading.Thread(target=send_all, daemon=True).start()
            return self.send(200, {"ok": True})
        if agent not in AGENTS:
            return self.send(400, {"error": "unknown agent"})
        if u.path == "/new":
            update_state(lambda st: st.pop(agent, None))
            return self.send(200, {"ok": True})
        if u.path == "/tochat":
            try:
                return self.send(200, {"note": to_chat(agent)})
            except Exception as e:
                return self.send(200, {"note": f"Couldn't send it ({e})"})
        if u.path == "/dictate" and not q.get("final"):
            return self.dictate_piece(agent, q)
        if u.path not in ("/talk", "/text", "/dictate"):
            return self.send(404, {"error": "not found"})
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        # The answer streams back as one JSON object per line: heard, text pieces, then done (or note / error).
        # No Content-Length, so tailscale serve passes each line on as soon as it is written.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(ev):
            self.wfile.write((json.dumps(ev) + "\n").encode())
            self.wfile.flush()

        info = {}
        try:
            if u.path == "/talk":                      # recorded speech
                CLIPS.mkdir(exist_ok=True)
                ext = ".mp4" if "mp4" in self.headers.get("Content-Type", "") else ".webm"
                clip = CLIPS / (time.strftime("%Y-%m-%d %H%M%S") + ext)
                clip.write_bytes(body)
                for old in sorted(CLIPS.iterdir())[:-KEEP_CLIPS]:
                    old.unlink()
                # after the button went down: mic_ms until the mic opened, live_ms until real sound came through;
                # dev: the mic used, mics: every mic the phone listed
                info = {"clip": clip.name, **{k: q.get(p, [""])[0] for k, p in
                        (("mic_ms", "mic"), ("live_ms", "live"), ("dev", "dev"), ("mics", "mics"))}}
                heard = transcribe(clip)
            elif u.path == "/dictate":                 # the end of a hands-free dictation
                d = DICTS.pop(q.get("id", [""])[0], None)
                if not d:
                    raise RuntimeError("The laptop doesn't have that dictation. Try again.")
                with d["lock"]:
                    dictate_more(d, True)
                for old in sorted(CLIPS.iterdir())[:-KEEP_CLIPS]:
                    old.unlink()
                m, s = divmod(round(d.get("dur", 0)), 60)
                info = {"clip": d["path"].name, "dictated_s": round(d.get("dur", 0))}
                heard = f"(Dictated hands-free, {m}:{s:02d}.) {d['text']}" if d["text"] else ""
            else:                                      # typed fallback
                heard = body.decode("utf-8").strip()
        except Exception as e:                         # report to the phone instead of hanging it
            log(agent=agent, error=str(e), **info)
            return emit({"error": str(e)})
        if not heard:
            log(agent=agent, heard="", **info)
            return emit({"note": "I didn't catch anything. Try again."})
        # "at" lets the phone fetch the answer from /last if this stream is cut (a locked screen).
        emit({"heard": heard, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        events = queue.Queue()
        threading.Thread(target=turn, args=(agent, heard, events, info), daemon=True).start()
        while True:
            try:
                ev = events.get(timeout=15)
            except queue.Empty:                        # long silent work: keep the connection from going idle
                ev = {"wait": True}
            try:
                emit(ev)
            except OSError:                            # the phone hung up; the turn carries on by itself
                return
            if "done" in ev or "error" in ev:
                return


def full_speed():
    """Opt this process out of Windows efficiency mode (EcoQoS). Started by Task Scheduler, the server made speech at
    less than half the speed of the same code run by hand (15.4 s vs 6.9 s for 16 s of speech in testing); with this
    call, 6.8 s."""
    import ctypes

    class Throttle(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p      # a 64-bit handle: the default int return would truncate it
    k.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    s = Throttle(1, 1, 0)                              # control execution speed; state 0 = never throttle
    k.SetProcessInformation(k.GetCurrentProcess(), 4, ctypes.byref(s), ctypes.sizeof(s))   # 4 = ProcessPowerThrottling


def check():
    """Streaming parse; chat timing; spoken numbers in a Windows voice, encoded like a phone (webm/opus), transcribed.
    The agent-home check needs real folders, so with no agents configured, or homes that do not exist yet (the example
    settings), it prints a note and moves on instead of failing."""
    # Text pieces come out in order; a second message (after a tool call) starts on a new line; junk lines are skipped.
    start = json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}})
    def piece(t):
        return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                             "delta": {"type": "text_delta", "text": t}}})
    got = []
    said, result = read_stream([start, piece("Let me "), piece("check."), "not json", start, piece("Box 3 was 520."),
                                json.dumps({"type": "result", "is_error": False, "session_id": "x"})], got.append)
    assert said == "Let me check.\nBox 3 was 520." and "".join(got) == said and result["session_id"] == "x", said
    # The Host check: the names in settings and the two local ones pass, with or without a port; anything else is refused.
    assert host_ok("127.0.0.1:%d" % PORT) and host_ok("LOCALHOST") and not host_ok("evil.example.com") and not host_ok("")
    assert all(host_ok(h) and host_ok(h + ":8443") for h in HOSTS), HOSTS
    # Catching up from the app chat: only the user's and the agent's own words after `since`; no tool output, no
    # messages from other sessions, no voice transcripts.
    global PROJECTS
    saved = PROJECTS
    with tempfile.TemporaryDirectory() as td:
        PROJECTS = Path(td)
        AGENTS["_check"] = dict(name="Helper", home=Path(td) / "helper-home", voice="am_michael", add_dirs=[], agent=None)
        d = PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(AGENTS["_check"]["home"]))
        d.mkdir()
        rows = [{"type": "user", "entrypoint": "claude-desktop", "timestamp": "2026-01-01T10:00:00Z",
                 "message": {"content": "Before the voice turn"}},
                {"type": "user", "timestamp": "2026-01-01T11:00:00Z", "message": {"content": "Row 3 shelf 538"}},
                {"type": "assistant", "timestamp": "2026-01-01T11:00:05Z",
                 "message": {"content": [{"type": "text", "text": "Logged 538."}, {"type": "tool_use", "name": "Edit"}]}},
                {"type": "user", "isMeta": True, "timestamp": "2026-01-01T11:01:00Z",
                 "message": {"content": "Another Claude session sent a message: Voice line transcript"}},
                {"type": "assistant", "timestamp": "2026-01-01T11:01:05Z",
                 "message": {"content": [{"type": "text", "text": "### Voice conversation, 2026-01-01 10:50 to 10:55"}]}},
                {"type": "user", "timestamp": "2026-01-01T11:02:00Z",
                 "message": {"content": [{"type": "tool_result", "content": "file contents"}]}}]
        (d / "chat.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        try:
            caught_up = app_chat_since("_check", "2026-01-01T10:30:00Z")
        finally:
            PROJECTS = saved
            del AGENTS["_check"]
    assert caught_up == (f"{U}: Row 3 shelf 538\n\nHelper: Logged 538.", "2026-01-01T11:00:05Z"), caught_up
    # The chat keeper sends a conversation only once it has been quiet long enough, and never twice.
    now = time.time()
    def at(t):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    rows = [dict(agent="alpha", heard="a", reply="b", at=at(now - CHAT_QUIET - 300)),
            dict(agent="beta", heard="a", reply="b", at=at(now - 60))]
    both = ["alpha", "beta"]
    assert chat_due(rows, {}, now, both) == ["alpha"], chat_due(rows, {}, now, both)
    assert chat_due(rows, {"alpha": at(now - CHAT_QUIET - 300)}, now, both) == []
    if not AGENTS:
        print("note: no agents in settings.json; skipping the agent home and voice check")
    for k, a in AGENTS.items():                        # every agent has its home and its voice
        if a["home"].is_dir():
            assert a["voice"] in kokoro().get_voices(), f"{k}: no voice {a['voice']}"
        else:
            print(f"note: {k}: home folder {a['home']} does not exist yet (fine for the example settings); skipped")
    with tempfile.TemporaryDirectory() as d:
        wav, webm = Path(d) / "t.wav", Path(d) / "t.webm"
        words = ("Box three. Shelf five twenty. Bin five eighty. Crate five fifty. Pallet two forty. "
               "Row one: six three one zero, six two one zero.")
        ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('{words}'); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libopus", str(webm)], check=True)
        heard = transcribe(webm)
    print("heard:", heard)
    digits = re.sub(r"[,\s]", "", heard)
    for n in ("520", "580", "550", "240", "6310", "6210"):
        assert n in digits, f"{n} missing"
    # Laptop voice: the way the agents read numbers back must come out as the same numbers.
    with tempfile.TemporaryDirectory() as d:
        clip = Path(d) / "say.wav"
        clip.write_bytes(say("Row three boxes: 6,310, 6,210, 4,685 and 5,050 grams.", "am_michael"))
        back = re.sub(r"[,\s]", "", transcribe(clip))
    print("voice read back:", back)
    for n in ("6310", "6210", "4685", "5050"):
        assert n in back, f"voice: {n} missing"
    # Hands-free: written down 10 s at a time, a dictation must give the same numbers as in one go, none lost or
    # doubled at the piece boundaries.
    def nums(t):
        return [n for n in (int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\d|\d", t)) if n >= 100]
    text = ("Row one boxes: 6,310, 6,210, 4,685 and 5,050 grams. Row two boxes: 5,919, 4,469, 3,780 and "
            "6,125 grams. Row three crates: 2,370, 2,380, 1,945 and 2,010 grams. Shelf 520, bin 580, crate "
            "550, pallet 240. The day came to 47,530 grams in and 5,435 grams out.")
    with tempfile.TemporaryDirectory() as td:
        wav, full, part = Path(td) / "full.wav", Path(td) / "full.webm", Path(td) / "part.webm"
        wav.write_bytes(say(text, "am_michael"))
        secs = (wav.stat().st_size - 44) / 48000
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libopus", str(full)], check=True)
        one_go = nums(transcribe(full))
        d, t0 = dict(path=part, done=0.0, text=""), time.time()
        for cut in range(10, int(secs) + 1, 10):       # the file as it grows on the laptop, piece by piece
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(full), "-t", str(cut), "-c", "copy",
                            str(part)], check=True)
            dictate_more(d, False)
        part.write_bytes(full.read_bytes())
        dictate_more(d, True)
    print(f"dictation: {secs:.0f} s written down piece by piece in {time.time() - t0:.1f} s: {d['text']}")
    assert nums(d["text"]) == one_go, (nums(d["text"]), one_go)
    assert nums(d["text"]) == [6310, 6210, 4685, 5050, 5919, 4469, 3780, 6125, 2370, 2380, 1945, 2010, 520, 580, 550,
                               240, 47530, 5435], d["text"]
    print("check ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        full_speed()
        whisper()                                      # load once at start, not on the first question
        threading.Thread(target=chat_keeper, daemon=True).start()
        if not SET.get("hosts"):
            print('note: settings.json "hosts" is empty, so only 127.0.0.1 and localhost are answered. Add your '
                  "laptop's tailnet name there or the phone gets \"not allowed\".")
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
