#!/usr/bin/env python3
"""Tailscale message inbox — a tiny phone → Claude bridge for the 0dte project.

Serves a mobile chat page on :8001 (reachable from the phone over Tailscale). Sending a message
appends it to data/inbox/log.jsonl; a Claude Code session running the `/loop` poller reads new user
messages, acts, and appends its replies to the same file. The page polls GET /messages with JS and
updates only the conversation — it NEVER reloads (so it can't wipe what you're typing). Sending uses
fetch too (no navigation). Serves ONLY the inbox (no repo files). Tailscale-private, no public URL.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(REPO, "data", "inbox")
LOG = os.path.join(INBOX, "log.jsonl")
PORT = 8001
os.makedirs(INBOX, exist_ok=True)


def _read_log():
    if not os.path.isfile(LOG):
        return []
    out = []
    for line in open(LOG):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _append(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>0dte · message Claude</title><style>
 body{background:#0d1117;color:#e6edf3;font:15px -apple-system,Roboto,sans-serif;margin:0;padding:12px 12px 96px}
 h1{font-size:16px;margin:4px 0 14px}
 .msg{max-width:82%;margin:8px 0;padding:8px 12px;border-radius:13px;line-height:1.35;word-wrap:break-word}
 .me{background:#1f6feb;margin-left:auto} .claude{background:#21262d;border:1px solid #30363d}
 .t{font-size:10px;color:#8b949e;margin-top:3px} .empty{color:#8b949e;text-align:center;margin-top:48px}
 form{position:fixed;bottom:0;left:0;right:0;display:flex;gap:8px;padding:10px;background:#161b22;border-top:1px solid #30363d}
 textarea{flex:1;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:9px;padding:11px;font-size:16px;resize:none;height:46px}
 button{background:#238636;color:#fff;border:0;border-radius:9px;padding:0 18px;font-size:16px;font-weight:600}
</style></head><body>
<h1>💬 message Claude (0dte)</h1>
<div id="convo"><div class="empty">Loading…</div></div>
<form id="f">
 <textarea id="t" name="text" placeholder="e.g. arm the bullish CALL · close all · how's the GEX?" autofocus></textarea>
 <button>Send</button>
</form>
<script>
 function esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
 var lastLen=-1;
 async function load(){
   try{
     var msgs=await (await fetch('/messages',{cache:'no-store'})).json();
     if(msgs.length===lastLen) return;          // nothing new; don't touch the DOM
     var grew=msgs.length>lastLen; lastLen=msgs.length;
     document.getElementById('convo').innerHTML = msgs.length ? msgs.map(function(m){
       var cls=m.from==='user'?'me':'claude', t=(m.ts||'').slice(11,16);
       return '<div class="msg '+cls+'"><div class="txt">'+esc(m.text).replace(/\\n/g,'<br>')+
              '</div><div class="t">'+m.from+' '+t+'</div></div>';
     }).join('') : '<div class="empty">No messages yet — say something.</div>';
     if(grew) window.scrollTo(0,document.body.scrollHeight);
   }catch(e){}
 }
 document.getElementById('f').addEventListener('submit', async function(e){
   e.preventDefault();
   var ta=document.getElementById('t'), text=ta.value.trim(); if(!text) return;
   ta.value='';
   await fetch('/send',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
                        body:'text='+encodeURIComponent(text)});
   load();
 });
 load(); setInterval(load, 4000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/messages"):
            self._send(json.dumps(_read_log()[-100:]), "application/json")
        else:
            self._send(PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        text = parse_qs(self.rfile.read(n).decode()).get("text", [""])[0].strip()
        if text:
            _append({"from": "user", "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text})
        self._send('{"ok":true}', "application/json")


if __name__ == "__main__":
    print(f"inbox server on 0.0.0.0:{PORT}  → log {LOG}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
