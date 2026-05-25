"""
Portfolio Tracker — Windows Desktop Launcher
Opens Edge or Chrome in --app mode: looks like a native desktop app,
no address bar, no browser chrome, just like any Windows program.
"""
import os
import sys
import threading
import time
import subprocess
import webbrowser

from updater import start_update_check

# ── Paths ───────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS          # PyInstaller temp extract folder
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(os.path.expanduser('~'), 'Documents', 'PortfolioTracker')
os.makedirs(DATA_DIR, exist_ok=True)

# ── Redirect stdout/stderr to a log file in windowed (.exe) mode ──
# --windowed builds have no console: sys.stdin/stdout are None,
# so print() and input() raise RuntimeError: lost sys.stdin
if getattr(sys, 'frozen', False):
    _log_path = os.path.join(DATA_DIR, 'app.log')
    _log = open(_log_path, 'a', buffering=1, encoding='utf-8')
    sys.stdout = _log
    sys.stderr = _log
    sys.stdin  = open(os.devnull, 'r')

os.environ.setdefault('DB_PATH', os.path.join(DATA_DIR, 'portfolio.db'))

PORT = 5088

# ── Flask ────────────────────────────────────────────────────
from app import app as flask_app  # noqa: E402

def _run_flask():
    flask_app.run(
        host='127.0.0.1', port=PORT,
        debug=False, threaded=True, use_reloader=False,
    )

def _wait_ready(timeout=20):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{PORT}/', timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False

def _alert(title, msg):
    """Show a GUI error dialog — works even with no console."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, msg)
        root.destroy()
    except Exception:
        pass   # tkinter unavailable — silently exit

# ── Find Edge or Chrome ──────────────────────────────────────
def _find_browser():
    candidates = [
        # Edge (pre-installed on Windows 10/11)
        os.path.expandvars(r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%LocalAppData%\Microsoft\Edge\Application\msedge.exe'),
        # Chrome fallback
        os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%LocalAppData%\Google\Chrome\Application\chrome.exe'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

# ── Main ─────────────────────────────────────────────────────
if __name__ == '__main__':
    print('Portfolio Tracker starting…')

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    if not _wait_ready():
        msg = ('Portfolio Tracker could not start.\n\n'
               f'Check the log for details:\n{DATA_DIR}\\app.log')
        print('ERROR: Flask server did not start in time.')
        _alert('Portfolio Tracker — Error', msg)
        sys.exit(1)

    url         = f'http://127.0.0.1:{PORT}'
    browser     = _find_browser()
    profile_dir = os.path.join(DATA_DIR, 'browser-profile')

    if browser:
        print(f'Opening with: {os.path.basename(browser)}')
        proc = subprocess.Popen([
            browser,
            f'--app={url}',
            '--window-size=1440,880',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-extensions',
            f'--user-data-dir={profile_dir}',
        ])
        # Check for updates silently in background (shows dialog if new version found)
        start_update_check(browser_proc=proc)
        # Block until the browser window is closed, then the process exits
        proc.wait()
    else:
        # Fallback — open in default browser
        print(f'Edge/Chrome not found. Opening {url} in default browser.')
        webbrowser.open(url)
        # Keep Flask alive until the process is killed externally
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    print('Portfolio Tracker closed.')
