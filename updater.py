"""
Auto-updater for Portfolio Tracker
====================================
Checks GitHub Releases silently on startup.
If a newer version exists, shows a clean dialog with download progress.
Runs the Inno Setup installer silently, then exits the current app.

Usage in main.py:
    from updater import start_update_check
    start_update_check(browser_proc)   # call after browser window opens
"""

import os
import sys
import json
import time
import threading
import tempfile
import subprocess
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk

from version import __version__

GITHUB_REPO = "OsmanDeol/portfolio-tracker"
API_URL     = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_DELAY = 4   # seconds after startup before checking (let browser settle)


# ─────────────────────────────────────────────────────────
#  Version comparison
# ─────────────────────────────────────────────────────────

def _parse(v: str):
    """Turn '1.2.3' into (1, 2, 3) for comparison. Non-numeric = (0,)."""
    try:
        return tuple(int(x) for x in v.strip().lstrip('v').split('.'))
    except ValueError:
        return (0,)


# ─────────────────────────────────────────────────────────
#  GitHub release fetch
# ─────────────────────────────────────────────────────────

def _fetch_latest():
    """
    Returns (version_str, download_url, size_bytes) or (None, None, None).
    Completely silent on network errors — never crashes the app.
    """
    try:
        req = urllib.request.Request(
            API_URL,
            headers={'User-Agent': 'PortfolioTracker-Updater/1.0'},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        tag     = data.get('tag_name', '')
        latest  = tag.lstrip('v')

        if not latest:
            return None, None, None

        # Skip if current version is "dev" or already up to date
        if __version__ == 'dev':
            return None, None, None
        if _parse(latest) <= _parse(__version__):
            return None, None, None

        # Find the Setup .exe asset
        for asset in data.get('assets', []):
            name = asset.get('name', '').lower()
            if name.endswith('.exe') and 'setup' in name:
                return latest, asset['browser_download_url'], asset.get('size', 0)

    except Exception:
        pass   # GitHub down, no internet, rate-limited — silently skip

    return None, None, None


# ─────────────────────────────────────────────────────────
#  Update dialog (runs in its own thread / tkinter instance)
# ─────────────────────────────────────────────────────────

def _show_dialog(version, url, size_bytes, browser_proc):
    """
    Blocking call — runs inside a background thread.
    Creates its own tkinter root (safe on Windows).
    """
    root = tk.Tk()
    root.title("Update Available — Portfolio Tracker")
    root.geometry("420x210")
    root.resizable(False, False)
    root.attributes('-topmost', True)

    # ── Styling ──────────────────────────────────────────
    BG   = '#1a1a2e'
    FG   = '#e0e0e0'
    ACC  = '#0078d4'
    GRAY = '#888'

    root.configure(bg=BG)
    root.option_add('*Font', ('Segoe UI', 10))

    pad = {'padx': 20, 'pady': 0}

    # ── Content ──────────────────────────────────────────
    tk.Label(root, text='  Update Available',
             font=('Segoe UI', 13, 'bold'), bg=BG, fg=FG,
             anchor='w').pack(fill='x', padx=20, pady=(18, 4))

    info_text = (
        f"  Version {version} is ready to install.\n"
        f"  Current version: {__version__}   •   "
        f"Download: {size_bytes / 1024 / 1024:.1f} MB"
    )
    tk.Label(root, text=info_text, bg=BG, fg=GRAY,
             justify='left', anchor='w').pack(fill='x', **pad)

    # Progress bar (hidden until download starts)
    bar    = ttk.Progressbar(root, length=380, mode='determinate')
    status = tk.Label(root, text='', bg=BG, fg=GRAY,
                      font=('Segoe UI', 9), anchor='w')

    # ── Buttons ──────────────────────────────────────────
    btn_frame = tk.Frame(root, bg=BG)
    btn_frame.pack(side='bottom', fill='x', padx=20, pady=16)

    def do_update():
        update_btn.config(state='disabled', text='Downloading…')
        skip_btn.config(state='disabled')
        bar.pack(fill='x', padx=20, pady=(8, 0))
        status.pack(fill='x', padx=20, pady=(2, 0))
        root.geometry("420:250")   # grow slightly

        def _download():
            try:
                tmp_path = os.path.join(tempfile.gettempdir(),
                                        f'PortfolioTracker-Setup-{version}.exe')
                downloaded = [0]

                def _on_progress(count, block_size, total):
                    downloaded[0] = min(count * block_size, total)
                    pct = (downloaded[0] / total * 100) if total > 0 else 0
                    bar['value'] = pct
                    mb_done = downloaded[0] / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    status.config(
                        text=f'  Downloading… {mb_done:.1f} / {mb_total:.1f} MB  ({pct:.0f}%)'
                    )
                    root.update_idletasks()

                urllib.request.urlretrieve(url, tmp_path, _on_progress)

                status.config(text='  Installing… the app will restart.')
                root.update_idletasks()
                time.sleep(0.8)

                # Close browser window gracefully, then run installer
                if browser_proc and browser_proc.poll() is None:
                    browser_proc.terminate()
                    time.sleep(1)

                # /SILENT = progress window shown, /NORESTART = no forced reboot
                subprocess.Popen([tmp_path, '/SILENT', '/NORESTART'])
                os._exit(0)   # hard exit — installer takes over

            except Exception as e:
                status.config(text=f'  Download failed: {e}', fg='#ff6b6b')
                update_btn.config(state='normal', text='Retry')
                skip_btn.config(state='normal')

        threading.Thread(target=_download, daemon=True).start()

    update_btn = tk.Button(
        btn_frame, text='Update Now',
        command=do_update,
        bg=ACC, fg='white', activebackground='#005fa3', activeforeground='white',
        font=('Segoe UI', 10, 'bold'), relief='flat',
        padx=18, pady=7, cursor='hand2', bd=0,
    )
    update_btn.pack(side='left')

    skip_btn = tk.Button(
        btn_frame, text='Skip This Version',
        command=root.destroy,
        bg='#2d2d2d', fg=GRAY, activebackground='#3a3a3a', activeforeground=FG,
        font=('Segoe UI', 10), relief='flat',
        padx=14, pady=7, cursor='hand2', bd=0,
    )
    skip_btn.pack(side='left', padx=(10, 0))

    root.mainloop()


# ─────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────

def start_update_check(browser_proc=None):
    """
    Call once after the browser window opens.
    Runs entirely in a daemon background thread — never blocks the app.
    """
    def _worker():
        time.sleep(CHECK_DELAY)
        version, url, size = _fetch_latest()
        if version:
            _show_dialog(version, url, size, browser_proc)

    t = threading.Thread(target=_worker, daemon=True, name='updater')
    t.start()
