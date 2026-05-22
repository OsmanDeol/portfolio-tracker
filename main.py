"""
Portfolio Tracker — Windows Desktop App
Wraps the Flask web app in a native desktop window using pywebview.
Run:  python main.py
Build .exe:  build.bat
"""
import os
import sys
import threading
import time

# ── Path setup for PyInstaller frozen builds ───────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE_DIR)

# ── Data directory: user's Documents/PortfolioTracker ──────
DATA_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'PortfolioTracker')
os.makedirs(DATA_DIR, exist_ok=True)

os.environ.setdefault('DB_PATH',     os.path.join(DATA_DIR, 'portfolio.db'))
os.environ.setdefault('SECRET_KEY',  'desktop-pt-secret-!xA9kZ2mQ7nB')
os.environ.setdefault('FLASK_ENV',   'production')

PORT = 5088   # Internal port; not exposed to the network

# ── Import Flask app ────────────────────────────────────────
from app import app as flask_app   # noqa: E402  (after env vars are set)

def _run_flask():
    flask_app.run(
        host='127.0.0.1',
        port=PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )

def _wait_for_flask(timeout=15):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{PORT}/auth', timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


if __name__ == '__main__':
    import webview

    # Start Flask in a background daemon thread
    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    print('Starting Portfolio Tracker…')
    if not _wait_for_flask():
        print('ERROR: Flask did not start in time.')
        sys.exit(1)

    # Create the native desktop window
    window = webview.create_window(
        title      = 'Portfolio Tracker',
        url        = f'http://127.0.0.1:{PORT}',
        width      = 1440,
        height     = 880,
        min_size   = (900, 600),
        resizable  = True,
        text_select= True,
        background_color='#04080f',
    )

    webview.start(
        debug=False,
        # On Windows, use EdgeChromium for best rendering
        # gui='edgechromium',   # uncomment if you want to force Edge
    )
