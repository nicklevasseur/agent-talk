"""Preview server for the talk page: serves the real index.html with made-up agents, voices and replies, so the page
can be looked at in a desktop browser without Tailscale, a microphone or a real agent turn.

    py -3 preview.py        then open http://127.0.0.1:2598/

Localhost only. It runs no agent and keeps nothing."""
import io, json, math, struct, time, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PAGE = Path(__file__).with_name("index.html")
PORT = 2598
AGENTS = [{"id": "assistant", "name": "Assistant", "voice": "am_michael"}, {"id": "ledger", "name": "Ledger", "voice": "af_heart"},
          {"id": "scout", "name": "Scout", "voice": "af_bella"}, {"id": "planner", "name": "Planner", "voice": "am_fenrir"},
          {"id": "helper", "name": "Helper", "voice": "af_aoede"}]
VOICES = [{"id": v, "label": l} for v, l in [("am_michael", "Michael (US man)"), ("am_fenrir", "Fenrir (US man)"),
          ("af_heart", "Heart (US woman)"), ("af_bella", "Bella (US woman)"), ("af_aoede", "Aoede (US woman)")]]
REPLY = ("Row three boxes: 6310, 6210, 4685 and 5050 grams. That puts the day at 47530 grams in so far. "
         "Want me to log row four next?")


def tone(secs=1.2, sr=24000):
    """A short hum in place of a spoken reply, so the page has sound to move with."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 180 * i / sr)
                                                     * (0.5 + 0.5 * math.sin(2 * math.pi * 3 * i / sr))))
                               for i in range(int(secs * sr))))
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        p = urlsplit(self.path).path
        if p == "/":
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if p == "/agents":
            return self.send(200, AGENTS)
        if p == "/voices":
            return self.send(200, VOICES)
        if p == "/last":
            return self.send(200, {"pending": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if p == "/say":
            return self.send(200, tone(), "audio/wav")
        if p in ("/new", "/tochat"):
            return self.send(200, {"ok": True, "note": "Nothing new to send."})
        if p == "/dictate":
            return self.send(200, {"text": "", "total": "Shelf 538, bin 674."})
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
        emit = lambda ev: (self.wfile.write((json.dumps(ev) + "\n").encode()), self.wfile.flush())
        heard = body.decode("utf-8", "replace") if p == "/text" else "Row three boxes, 6310, 6210, 4685 and 5050."
        emit({"heard": heard, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        time.sleep(1.5)
        for i in range(0, len(REPLY), 9):
            emit({"text": REPLY[i:i + 9]}); time.sleep(0.04)
        emit({"done": True, "reply": REPLY})


if __name__ == "__main__":
    print(f"Talk preview: http://127.0.0.1:{PORT}/")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
