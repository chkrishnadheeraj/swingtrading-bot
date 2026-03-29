"""
Kite Connect Authentication Helper
====================================
Run this ONCE each morning before starting the bot.
Access tokens are valid for one trading day only.

Usage:
    source venv/bin/activate
    python auth.py

What it does:
    1. Prints your login URL
    2. Waits for you to log in (opens browser automatically)
    3. Auto-captures the request_token from the redirect
    4. Exchanges it for an access_token
    5. Saves it to config/.env  ← bot picks it up automatically

Redirect URL to register in Kite Connect app settings:
    http://127.0.0.1:8765
"""

import os
import sys
import webbrowser
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# ── project imports ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from config import settings

REDIRECT_PORT = 8765
REDIRECT_HOST = "127.0.0.1"
REDIRECT_URL  = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}"


# ═══════════════════════════════════════════════════════════════════════════
# Tiny HTTP server — listens for the one redirect and then shuts down
# ═══════════════════════════════════════════════════════════════════════════

class _TokenCapture(BaseHTTPRequestHandler):
    """Handles the single redirect from Zerodha after login."""

    captured_token: str = ""

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)

        if "request_token" in params and params.get("status", [""])[0] == "success":
            _TokenCapture.captured_token = params["request_token"][0]
            body = _SUCCESS_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            error = params.get("message", ["Unknown error"])[0]
            body = _ERROR_HTML.format(error=error).encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        # Signal the server to stop after this one request
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *_):
        pass   # suppress default access log noise


# ═══════════════════════════════════════════════════════════════════════════
# HTML responses
# ═══════════════════════════════════════════════════════════════════════════

_SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Auth Success</title>
  <style>
    body { font-family: -apple-system, sans-serif; background: #0d1117;
           color: #c9d1d9; display: flex; align-items: center;
           justify-content: center; height: 100vh; margin: 0; }
    .card { background: #161b22; border: 1px solid #30363d;
            border-radius: 12px; padding: 40px 48px; text-align: center;
            max-width: 420px; }
    .icon { font-size: 56px; margin-bottom: 16px; }
    h1 { color: #2ecc71; margin: 0 0 12px; font-size: 22px; }
    p  { color: #8b949e; font-size: 14px; line-height: 1.6; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h1>Authenticated!</h1>
    <p>Access token saved to <code>config/.env</code>.<br>
       You can close this tab and return to the terminal.</p>
  </div>
</body>
</html>"""

_ERROR_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Auth Failed</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: #0d1117;
            color: #c9d1d9; display: flex; align-items: center;
            justify-content: center; height: 100vh; margin: 0; }}
    .card {{ background: #161b22; border: 1px solid #30363d;
             border-radius: 12px; padding: 40px 48px; text-align: center;
             max-width: 420px; }}
    .icon {{ font-size: 56px; margin-bottom: 16px; }}
    h1 {{ color: #e74c3c; margin: 0 0 12px; font-size: 22px; }}
    p  {{ color: #8b949e; font-size: 14px; line-height: 1.6; margin: 0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">❌</div>
    <h1>Authentication Failed</h1>
    <p>{error}</p>
  </div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
# Main auth flow
# ═══════════════════════════════════════════════════════════════════════════

def _save_to_env(key: str, value: str):
    """Update or insert KEY=VALUE in config/.env"""
    env_path = Path(__file__).parent / "config" / ".env"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n")
    os.environ[key] = value


def run_auth():
    print("\n" + "═" * 54)
    print("  🔐  Kite Connect — Daily Authentication")
    print("═" * 54)

    # Validate credentials are present
    if not settings.KITE_API_KEY:
        print("\n  ❌  KITE_API_KEY not found in config/.env")
        print("      Add it and re-run.\n")
        sys.exit(1)

    if not settings.KITE_API_SECRET:
        print("\n  ❌  KITE_API_SECRET not found in config/.env")
        print("      Add it and re-run.\n")
        sys.exit(1)

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=settings.KITE_API_KEY)
    login_url = kite.login_url()

    print(f"\n  API Key  : {settings.KITE_API_KEY[:6]}{'*' * (len(settings.KITE_API_KEY) - 6)}")
    print(f"  Redirect : {REDIRECT_URL}")
    print(f"\n  Opening browser for Zerodha login …")
    print(f"  (If it doesn't open, visit this URL manually:)")
    print(f"\n  {login_url}\n")

    # Start the capture server in a background thread
    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _TokenCapture)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Open the login URL in the default browser
    webbrowser.open(login_url)

    print("  ⏳  Waiting for Zerodha login … (complete 2FA in browser)")
    print("      Press Ctrl+C to cancel.\n")

    try:
        server_thread.join(timeout=300)   # 5-minute timeout
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        sys.exit(0)

    token = _TokenCapture.captured_token
    if not token:
        print("\n  ❌  No token received within 5 minutes. Try again.\n")
        sys.exit(1)

    print(f"  ✅  request_token received: {token[:8]}…")
    print(f"  🔄  Exchanging for access_token …")

    try:
        session = kite.generate_session(token, api_secret=settings.KITE_API_SECRET)
        access_token = session["access_token"]
        user_name    = session.get("user_name", "Unknown")
        user_id      = session.get("user_id", "")
        login_time   = session.get("login_time", "")

        # Save to .env — bot auto-loads it
        _save_to_env("KITE_ACCESS_TOKEN", access_token)

        print(f"\n  ✅  Access token saved to config/.env")
        print(f"\n  👤  User   : {user_name} ({user_id})")
        print(f"  🕐  Login  : {login_time}")
        print(f"  🔑  Token  : {access_token[:8]}…{access_token[-4:]}")
        print(f"\n  ═══════════════════════════════════════════════")
        print(f"  Bot is now authenticated. Run the paper scan:")
        print(f"  source venv/bin/activate")
        print(f"  python main.py --mode paper --scan-now")
        print(f"  ═══════════════════════════════════════════════\n")

    except Exception as e:
        print(f"\n  ❌  Session generation failed: {e}")
        print("      Token may have expired — try logging in again.\n")
        sys.exit(1)


if __name__ == "__main__":
    run_auth()
