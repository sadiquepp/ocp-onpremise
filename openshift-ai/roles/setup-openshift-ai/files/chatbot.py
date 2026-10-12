#!/usr/bin/env python3
"""A chat bot for the model this lab serves on CPU.

The test app at the end of openshift-ai/setup_openshift_ai.yaml: it talks to
an OpenAI-compatible endpoint - whatever KServe put behind
<model>-predictor.<namespace>.svc - and offers three ways in.

  GET  /              a chat page. Type, get an answer; or press the button
                      that sends the built-in demo prompt.
  POST /api/chat      {"message": "...", "history": [...]} -> the reply, as
                      JSON. What the page calls.
  GET  /api/demo      the app writes a prompt of its own
                      (DEMO_PROMPT), sends it, and returns the answer. No
                      browser needed, which is why the playbook's verify phase
                      asserts on this one.
  GET  /api/health    is the model reachable, and what does it say it serves.
  GET  /healthz       for the pod's own probes: 200 as soon as the process is
                      up, whether or not the model is.

STANDARD LIBRARY ONLY, on purpose. The image is
registry.access.redhat.com/ubi9/python-311 with nothing installed into it: no
pip install at build time, no wheel to mirror for a disconnected cluster, no
requirements.txt to drift. urllib is enough to POST JSON.

Everything comes from the environment (templates/chatbot.yaml.j2 sets it):

  MODEL_ENDPOINT   http://<svc>:8080 - no trailing /v1
  MODEL_NAME       the name the model answers under
  SYSTEM_PROMPT    prepended to every conversation
  DEMO_PROMPT      what /api/demo asks
  MAX_TOKENS       generation cap per reply
  TEMPERATURE      sampling temperature
  REQUEST_TIMEOUT  seconds to wait for the model
  PORT             default 8080
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "http://localhost:8080").rstrip("/")
MODEL_NAME = os.environ.get("MODEL_NAME", "model")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "").strip()
DEMO_PROMPT = os.environ.get("DEMO_PROMPT", "Say hello in one sentence.").strip()
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "256"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.2"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "300"))
PORT = int(os.environ.get("PORT", "8080"))

# How much of a conversation is sent back to the model. A 0.5B model on a CPU
# has a small context and every token costs wall-clock time, so the page keeps
# the last few turns rather than the whole thread.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "6"))


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def model_request(path, payload=None, timeout=None):
    """GET or POST JSON to the model. Returns (status, parsed-or-text)."""
    url = MODEL_ENDPOINT + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout or REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        # The model's own error body is the useful part - vLLM and llama.cpp
        # both explain themselves there (a context overflow, an unknown model
        # name) - so pass it through instead of just the status.
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, "cannot reach %s: %s" % (url, exc.reason)
    except TimeoutError:
        return 0, "no answer from %s within %gs" % (url, timeout or REQUEST_TIMEOUT)


def chat(message, history):
    """One turn. Returns (reply, error, seconds)."""
    messages = []
    if SYSTEM_PROMPT:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    for turn in history[-HISTORY_TURNS:]:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    started = time.monotonic()
    status, body = model_request("/v1/chat/completions", {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": False,
    })
    took = time.monotonic() - started

    if status != 200:
        return "", "model returned %s: %s" % (status or "no response", str(body)[:500]), took
    if not isinstance(body, dict):
        return "", "model returned something that is not JSON: %s" % str(body)[:500], took
    try:
        reply = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return "", "no choices in the model's reply: %s" % json.dumps(body)[:500], took
    return (reply or "").strip(), "", took


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #ffffff; --fg: #151515; --muted: #6a6e73; --line: #d2d2d2;
    --mine: #e7f1fa; --theirs: #f5f5f5; --accent: #0066cc; --bad: #a30000;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #151515; --fg: #f0f0f0; --muted: #a0a4a8; --line: #3c3f42;
      --mine: #1b3a56; --theirs: #212427; --accent: #73bcf7; --bad: #ff7468;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 "Red Hat Text", system-ui, -apple-system, sans-serif;
    display: flex; flex-direction: column; min-height: 100vh;
  }
  header { padding: 14px 16px; border-bottom: 1px solid var(--line); }
  h1 { margin: 0; font-size: 17px; font-weight: 600; }
  .meta { margin-top: 3px; font-size: 12px; color: var(--muted); }
  main { flex: 1; width: 100%; max-width: 760px; margin: 0 auto; padding: 16px; }
  #log { display: flex; flex-direction: column; gap: 10px; }
  .msg { padding: 9px 12px; border-radius: 10px; max-width: 85%; white-space: pre-wrap;
         overflow-wrap: anywhere; }
  .user { align-self: flex-end; background: var(--mine); }
  .bot { align-self: flex-start; background: var(--theirs); }
  .err { align-self: flex-start; background: var(--theirs); color: var(--bad); }
  .took { font-size: 11px; color: var(--muted); margin-top: 3px; }
  form { position: sticky; bottom: 0; background: var(--bg);
         border-top: 1px solid var(--line); padding: 12px 16px; }
  .row { display: flex; gap: 8px; max-width: 760px; margin: 0 auto; }
  input[type=text] { flex: 1; min-width: 0; padding: 9px 11px; font: inherit;
         color: var(--fg); background: var(--bg);
         border: 1px solid var(--line); border-radius: 6px; }
  button { padding: 9px 14px; font: inherit; border: 1px solid var(--accent);
         border-radius: 6px; background: var(--accent); color: #fff; cursor: pointer; }
  button.ghost { background: transparent; color: var(--accent); }
  button[disabled] { opacity: .55; cursor: default; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="meta">__META__</div>
</header>
<main><div id="log"></div></main>
<form id="f">
  <div class="row">
    <input type="text" id="m" placeholder="Ask something..." autocomplete="off" autofocus>
    <button type="submit" id="send">Send</button>
    <button type="button" id="demo" class="ghost" title="__DEMO_TITLE__">Demo prompt</button>
  </div>
</form>
<script>
const log = document.getElementById('log');
const form = document.getElementById('f');
const input = document.getElementById('m');
const send = document.getElementById('send');
const demo = document.getElementById('demo');
const demoPrompt = __DEMO_JSON__;
let history = [];
let busy = false;

function add(cls, text, took) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  if (took) {
    const t = document.createElement('div');
    t.className = 'took';
    t.textContent = took;
    d.appendChild(t);
  }
  log.appendChild(d);
  d.scrollIntoView({block: 'end'});
  return d;
}

function setBusy(v) {
  busy = v;
  send.disabled = v;
  demo.disabled = v;
}

async function ask(message) {
  if (busy || !message) return;
  setBusy(true);
  add('user', message);
  const pending = add('bot', 'thinking on a CPU, this can take a while...');
  try {
    const r = await fetch('api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: message, history: history})
    });
    const j = await r.json();
    pending.remove();
    if (j.error) {
      add('err', j.error);
    } else {
      add('bot', j.reply, j.took_seconds + 's');
      history.push({role: 'user', content: message});
      history.push({role: 'assistant', content: j.reply});
    }
  } catch (e) {
    pending.remove();
    add('err', String(e));
  } finally {
    setBusy(false);
    input.focus();
  }
}

form.addEventListener('submit', e => {
  e.preventDefault();
  const m = input.value.trim();
  input.value = '';
  ask(m);
});
demo.addEventListener('click', () => ask(demoPrompt));
</script>
</body>
</html>
"""


def render_page():
    meta = "model %s, served at %s - max %d tokens, temperature %g" % (
        MODEL_NAME, MODEL_ENDPOINT, MAX_TOKENS, TEMPERATURE)
    return (PAGE
            .replace("__TITLE__", html.escape("OpenShift AI chat bot"))
            .replace("__META__", html.escape(meta))
            .replace("__DEMO_TITLE__", html.escape(DEMO_PROMPT))
            # json.dumps for the JS literal, then < escaped as well, so a
            # prompt containing "</script>" cannot close the script element.
            .replace("__DEMO_JSON__", json.dumps(DEMO_PROMPT).replace("<", "\\u003c")))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rhoai-chatbot/1.0"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, render_page(), "text/html; charset=utf-8")
        elif path == "/healthz":
            # The app's own liveness: up, regardless of the model. A probe that
            # went through to the model would restart this pod every time the
            # model was slow to load, which is most of a cold start.
            self._send(200, {"status": "ok"})
        elif path == "/api/health":
            status, body = model_request("/v1/models", timeout=15)
            self._send(200 if status == 200 else 503, {
                "model_endpoint": MODEL_ENDPOINT,
                "model_name": MODEL_NAME,
                "model_status": status,
                "models": body,
            })
        elif path == "/api/demo":
            # The app generates the prompt. This is what the playbook's verify
            # phase asserts on, so it carries everything needed to say why a
            # failure happened.
            reply, error, took = chat(DEMO_PROMPT, [])
            self._send(200 if reply else 502, {
                "prompt": DEMO_PROMPT,
                "reply": reply,
                "error": error,
                "took_seconds": round(took, 1),
                "model_name": MODEL_NAME,
                "model_endpoint": MODEL_ENDPOINT,
            })
        else:
            self._send(404, {"error": "no such path: %s" % path})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/chat":
            self._send(404, {"error": "no such path: %s" % path})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"error": "body is not JSON: %s" % exc})
            return
        message = (payload.get("message") or "").strip()
        if not message:
            self._send(400, {"error": "no message"})
            return
        history = payload.get("history") or []
        if not isinstance(history, list):
            history = []
        reply, error, took = chat(message, history)
        self._send(200 if reply else 502, {
            "reply": reply,
            "error": error,
            "took_seconds": round(took, 1),
        })


def main():
    log("chat bot on :%d -> %s (model %s)" % (PORT, MODEL_ENDPOINT, MODEL_NAME))
    log("demo prompt: %s" % DEMO_PROMPT)
    # Threading, so a reply that takes a minute on a CPU does not block the
    # page's own requests - and so the readiness probe keeps answering while
    # somebody is waiting for a generation.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
