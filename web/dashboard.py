# web/dashboard.py
#
# Replaces bot/main.py's bare "OK" health-check server with a small
# status dashboard - so a human (not just Render / an uptime monitor)
# can open the service's public URL and immediately see:
#   - live current time
#   - last keep-alive self-ping, and whether it succeeded
#   - next scheduled self-ping
#   - a running check/failure count and process uptime
#
# Uptime monitors that expect a plain 200 on GET or HEAD still get one -
# this only adds a nicer HTML body for GET "/"; every status code stays
# the same as the server this replaces. GET "/status" returns the same
# data as plain JSON if you ever want to poll it programmatically.

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from jobs import keepalive

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Crypto Telegram Bot - Status</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: radial-gradient(circle at top, #0f1729 0%, #05070d 70%);
    color: #e6ecff;
  }}
  .card {{
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px; padding: 40px 48px; width: min(92vw, 460px);
    box-shadow: 0 20px 60px rgba(0,0,0,0.5); backdrop-filter: blur(8px);
  }}
  h1 {{ font-size: 20px; margin: 0 0 4px; display:flex; align-items:center; gap:10px; }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; background: {dot_color}; box-shadow: 0 0 12px {dot_color}; }}
  .sub {{ color: #8a93b8; font-size: 13px; margin-bottom: 28px; }}
  .row {{ display: flex; justify-content: space-between; padding: 14px 0; border-top: 1px solid rgba(255,255,255,0.07); }}
  .row:first-of-type {{ border-top: none; }}
  .label {{ color: #8a93b8; font-size: 13px; }}
  .value {{ font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .footer {{ margin-top: 24px; font-size: 11px; color: #4d5578; text-align: center; }}
</style>
</head>
<body>
  <div class="card">
    <h1><span class="dot"></span>Bot Status</h1>
    <div class="sub">crypto-telegram-bot &middot; self keep-alive</div>
    <div class="row"><span class="label">Current time (UTC)</span><span class="value" id="now">{now}</span></div>
    <div class="row"><span class="label">Last check</span><span class="value">{last_check}</span></div>
    <div class="row"><span class="label">Next check</span><span class="value">{next_check}</span></div>
    <div class="row"><span class="label">Checks so far</span><span class="value">{total_checks} ({total_failures} failed)</span></div>
    <div class="row"><span class="label">Uptime since restart</span><span class="value">{uptime}</span></div>
    <div class="footer">Auto-refreshes every 30s &middot; /status for JSON</div>
  </div>
<script>
  function tick() {{
    var el = document.getElementById('now');
    var d = new Date();
    el.textContent = d.toISOString().slice(0,19).replace('T',' ');
  }}
  setInterval(tick, 1000);
  setTimeout(function() {{ location.reload(); }}, 30000);
</script>
</body>
</html>"""


def _fmt(dt):
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _uptime_str(started_at):
    delta = datetime.now(timezone.utc) - started_at
    total = int(delta.total_seconds())
    d, r = divmod(total, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


class _Handler(BaseHTTPRequestHandler):
    def _render_html(self) -> bytes:
        state = keepalive.get_state()
        ok = state.get("last_check_ok")
        dot_color = "#3ddc84" if ok else ("#e6c34a" if ok is None else "#e05252")
        html = _PAGE_TEMPLATE.format(
            dot_color=dot_color,
            now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            last_check=_fmt(state.get("last_check_at")),
            next_check=_fmt(state.get("next_check_at")),
            total_checks=state.get("total_checks", 0),
            total_failures=state.get("total_failures", 0),
            uptime=_uptime_str(state.get("started_at")),
        )
        return html.encode("utf-8")

    def do_GET(self):
        if self.path.startswith("/status"):
            state = keepalive.get_state()
            body = json.dumps({
                "last_check_at": state["last_check_at"].isoformat() if state.get("last_check_at") else None,
                "next_check_at": state["next_check_at"].isoformat() if state.get("next_check_at") else None,
                "last_check_ok": state.get("last_check_ok"),
                "total_checks": state.get("total_checks", 0),
                "total_failures": state.get("total_failures", 0),
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return

        body = self._render_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Uptime monitors (UptimeRobot etc.) send HEAD by default.
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # silence access logs, same as the server this replaces


def start_dashboard_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()