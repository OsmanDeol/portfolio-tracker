import os
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import json

import groq as groq_sdk
import yfinance as yf
from flask import Flask, jsonify, render_template, request, redirect, url_for, session
from flask_cors import CORS
from flask_login import (LoginManager, UserMixin,
                         login_user, logout_user, current_user)
from authlib.integrations.flask_client import OAuth

# Support PyInstaller frozen builds — find templates next to the exe
if getattr(sys, 'frozen', False):
    _tmpl = os.path.join(sys._MEIPASS, 'templates')
    app   = Flask(__name__, template_folder=_tmpl)
else:
    app   = Flask(__name__)

CORS(app)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-change-in-prod-please')

# ── GitHub OAuth (only active when env vars are set) ─────
_GH_CLIENT_ID     = os.environ.get('GITHUB_CLIENT_ID', '')
_GH_CLIENT_SECRET = os.environ.get('GITHUB_CLIENT_SECRET', '')
_ALLOWED_USERS    = {u.strip().lower()
                     for u in os.environ.get('ALLOWED_GITHUB_USERS', '').split(',')
                     if u.strip()}
AUTH_ENABLED      = bool(_GH_CLIENT_ID and _GH_CLIENT_SECRET)

login_manager = LoginManager(app)
login_manager.login_view = 'login'

_oauth = OAuth(app)
if AUTH_ENABLED:
    _gh = _oauth.register(
        name='github',
        client_id=_GH_CLIENT_ID,
        client_secret=_GH_CLIENT_SECRET,
        access_token_url='https://github.com/login/oauth/access_token',
        authorize_url='https://github.com/login/oauth/authorize',
        api_base_url='https://api.github.com/',
        client_kwargs={'scope': 'read:user'},
    )

DB_PATH         = os.environ.get('DB_PATH', 'portfolio.db')
price_cache     = {}
sparkline_cache = {}
cache_lock      = threading.Lock()
PRICE_TTL       = 60    # shared server cache: one yfinance call/min per ticker
SPARKLINE_TTL   = 300
ET              = ZoneInfo('America/New_York')


# ── User model ───────────────────────────────────────────
class User(UserMixin):
    def __init__(self, id, github_id, username, avatar=''):
        self.id        = id
        self.github_id = github_id
        self.username  = username
        self.avatar    = avatar

@login_manager.user_loader
def _load_user(user_id):
    conn = get_db()
    row  = conn.execute('SELECT * FROM users WHERE id=?', (int(user_id),)).fetchone()
    conn.close()
    if row:
        return User(row['id'], row['github_id'],
                    row['github_username'], row['avatar_url'] or '')
    return None

def _uid():
    """Current user's DB id — real id in auth mode, 1 in local mode."""
    if AUTH_ENABLED and current_user.is_authenticated:
        return current_user.id
    return 1


# ─────────────────────────────────────────────────────────
#  US MARKET HOLIDAYS  (NYSE / NASDAQ)
# ─────────────────────────────────────────────────────────

def _easter(year):
    """Return Easter Sunday for given year (Anonymous Gregorian)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    month = (h + l - 7*m + 114) // 31
    day   = (h + l - 7*m + 114) % 31 + 1
    return date(year, month, day)

def _observed(d):
    """Shift a holiday to observed weekday if it falls on a weekend."""
    if d.weekday() == 5: return d - timedelta(days=1)   # Sat → Fri
    if d.weekday() == 6: return d + timedelta(days=1)   # Sun → Mon
    return d

def _nth_weekday(year, month, weekday, n):
    """nth occurrence (1-based) of weekday in month."""
    first = date(year, month, 1)
    diff  = (weekday - first.weekday()) % 7
    return first + timedelta(days=diff + 7*(n-1))

def _last_weekday(year, month, weekday):
    """Last occurrence of weekday in month."""
    if month == 12: last = date(year+1, 1, 1) - timedelta(days=1)
    else:           last = date(year, month+1, 1) - timedelta(days=1)
    diff = (last.weekday() - weekday) % 7
    return last - timedelta(days=diff)

def get_market_holidays(year):
    """Return a set of NYSE/NASDAQ holiday dates for the given year."""
    MO, TH = 0, 3
    hols = {
        _observed(date(year, 1, 1)),              # New Year's Day
        _nth_weekday(year, 1, MO, 3),             # MLK Day
        _nth_weekday(year, 2, MO, 3),             # Presidents Day
        _easter(year) - timedelta(days=2),        # Good Friday
        _last_weekday(year, 5, MO),               # Memorial Day
        _observed(date(year, 6, 19)),             # Juneteenth
        _observed(date(year, 7, 4)),              # Independence Day
        _nth_weekday(year, 9, MO, 1),             # Labor Day
        _nth_weekday(year, 11, TH, 4),            # Thanksgiving
        _observed(date(year, 12, 25)),            # Christmas
    }
    return hols

def is_trading_day(d):
    if d.weekday() >= 5: return False
    return d not in get_market_holidays(d.year)

def market_session(now_et=None):
    """
    Returns dict: status, seconds_to_next, label, next_label
    status: 'open' | 'pre' | 'post' | 'closed'
    """
    if now_et is None:
        now_et = datetime.now(ET)
    today = now_et.date()

    def dt(h, m):
        return now_et.replace(hour=h, minute=m, second=0, microsecond=0)

    pre_start  = dt(4,  0)
    mkt_open   = dt(9, 30)
    mkt_close  = dt(16, 0)
    post_end   = dt(20, 0)

    def next_mkt_open(from_dt):
        d = from_dt.date()
        t_open = from_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        if is_trading_day(d) and from_dt < t_open:
            return t_open
        nxt = d + timedelta(days=1)
        while not is_trading_day(nxt):
            nxt += timedelta(days=1)
        return datetime(nxt.year, nxt.month, nxt.day, 9, 30, tzinfo=ET)

    if not is_trading_day(today):
        nxt = next_mkt_open(now_et)
        secs = int((nxt - now_et).total_seconds())
        return {'status':'closed', 'seconds_to_next': secs, 'next_label':'Market opens'}

    if now_et < pre_start:
        nxt = next_mkt_open(now_et)
        secs = int((nxt - now_et).total_seconds())
        return {'status':'closed', 'seconds_to_next': secs, 'next_label':'Market opens'}

    if now_et < mkt_open:
        secs = int((mkt_open - now_et).total_seconds())
        return {'status':'pre', 'seconds_to_next': secs, 'next_label':'Market opens'}

    if now_et < mkt_close:
        secs = int((mkt_close - now_et).total_seconds())
        return {'status':'open', 'seconds_to_next': secs, 'next_label':'Market closes'}

    if now_et < post_end:
        nxt = next_mkt_open(now_et)
        secs = int((nxt - now_et).total_seconds())
        return {'status':'post', 'seconds_to_next': secs, 'next_label':'Market opens'}

    # After post-market
    nxt = next_mkt_open(now_et)
    secs = int((nxt - now_et).total_seconds())
    return {'status':'closed', 'seconds_to_next': secs, 'next_label':'Market opens'}


# ─────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _seed_watchlists(conn, uid):
    """Create 5 default empty watchlists for a brand-new user."""
    for i in range(1, 6):
        conn.execute(
            'INSERT INTO watchlists (user_id, name, position) VALUES (?,?,?)',
            (uid, f'Watchlist {i}', i))
    conn.commit()


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            github_id       INTEGER UNIQUE NOT NULL,
            github_username TEXT    NOT NULL,
            avatar_url      TEXT,
            created_at      TEXT    DEFAULT (datetime(\'now\'))
        );
        CREATE TABLE IF NOT EXISTS positions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL DEFAULT 1,
            ticker         TEXT    NOT NULL,
            shares         REAL    NOT NULL DEFAULT 0,
            avg_buy_price  REAL    NOT NULL DEFAULT 0,
            total_invested REAL    NOT NULL DEFAULT 0,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL DEFAULT 1,
            ticker     TEXT NOT NULL,
            type       TEXT NOT NULL,
            shares     REAL NOT NULL,
            price      REAL NOT NULL,
            commission REAL NOT NULL DEFAULT 0,
            total      REAL NOT NULL,
            trade_date TEXT,
            date       TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS realized_pnl (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL DEFAULT 1,
            ticker          TEXT NOT NULL,
            shares          REAL NOT NULL,
            buy_price       REAL NOT NULL,
            sell_price      REAL NOT NULL,
            commission      REAL NOT NULL DEFAULT 0,
            profit_loss     REAL NOT NULL,
            net_profit_loss REAL NOT NULL,
            pnl_pct         REAL NOT NULL,
            trade_date      TEXT,
            date            TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS watchlists (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL DEFAULT 1,
            name     TEXT    NOT NULL DEFAULT 'Watchlist',
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS watchlist_items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            watchlist_id INTEGER NOT NULL,
            ticker       TEXT    NOT NULL,
            added        TEXT    DEFAULT (datetime('now')),
            UNIQUE(watchlist_id, ticker)
        );
    ''')
    # Safe column migrations for pre-existing DBs
    for sql in [
        "ALTER TABLE transactions ADD COLUMN user_id INTEGER",
        "ALTER TABLE transactions ADD COLUMN commission REAL DEFAULT 0",
        "ALTER TABLE transactions ADD COLUMN trade_date TEXT",
        "ALTER TABLE transactions ADD COLUMN deleted_at TEXT",
        "ALTER TABLE realized_pnl ADD COLUMN user_id INTEGER",
        "ALTER TABLE realized_pnl ADD COLUMN commission REAL DEFAULT 0",
        "ALTER TABLE realized_pnl ADD COLUMN net_profit_loss REAL",
        "ALTER TABLE realized_pnl ADD COLUMN trade_date TEXT",
        "ALTER TABLE realized_pnl ADD COLUMN deleted_at TEXT",
        "ALTER TABLE positions ADD COLUMN user_id INTEGER",
    ]:
        try:
            conn.execute(sql); conn.commit()
        except sqlite3.OperationalError:
            pass
    # Ensure all existing rows belong to local user 1
    for table in ('positions', 'transactions', 'realized_pnl'):
        try:
            conn.execute(f'UPDATE {table} SET user_id=1 WHERE user_id IS NULL')
            conn.commit()
        except sqlite3.OperationalError:
            pass
    # Seed default watchlists for local user (auth mode seeds on first login)
    if not AUTH_ENABLED:
        count = conn.execute('SELECT COUNT(*) FROM watchlists WHERE user_id=1').fetchone()[0]
        if count == 0:
            _seed_watchlists(conn, 1)
    conn.close()


def recalculate_position(conn, uid, ticker):
    """Replay all transactions to recompute position for a ticker (average cost method)."""
    txns = conn.execute(
        '''SELECT type, shares, total FROM transactions
           WHERE user_id=? AND ticker=?
             AND (deleted_at IS NULL OR deleted_at='')
           ORDER BY COALESCE(trade_date,'') ASC, id ASC''',
        (uid, ticker)
    ).fetchall()
    shares = 0.0; invested = 0.0
    for t in txns:
        if t['type'] == 'buy':
            shares   += t['shares']
            invested += t['total']
        elif t['type'] == 'sell' and shares > 0:
            avg        = invested / shares
            cost_basis = avg * min(t['shares'], shares)
            shares     = max(0.0, shares - t['shares'])
            invested   = max(0.0, invested - cost_basis)
    if shares < 0.0001:
        # Only delete if there are actual transactions (don't remove track-only 0-share entries)
        if txns:
            conn.execute('DELETE FROM positions WHERE user_id=? AND ticker=?', (uid, ticker))
        else:
            # No transactions → leave the position as-is (track-only)
            conn.execute(
                'UPDATE positions SET shares=0,avg_buy_price=0,total_invested=0 WHERE user_id=? AND ticker=?',
                (uid, ticker))
    else:
        avg_price = invested / shares
        conn.execute('''
            INSERT INTO positions (user_id,ticker,shares,avg_buy_price,total_invested)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id,ticker) DO UPDATE SET
              shares=excluded.shares,
              avg_buy_price=excluded.avg_buy_price,
              total_invested=excluded.total_invested
        ''', (uid, ticker, shares, avg_price, invested))
    conn.commit()


# ─────────────────────────────────────────────────────────
#  STOCK DATA  (shared cache)
# ─────────────────────────────────────────────────────────

def fetch_stock_data(ticker):
    ticker = ticker.upper()
    with cache_lock:
        if ticker in price_cache:
            c = price_cache[ticker]
            if time.time() - c['ts'] < PRICE_TTL:
                return c['data']
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
        prev  = info.get('previousClose') or info.get('regularMarketPreviousClose') or price
        chg   = price - prev if prev else 0
        chgp  = (chg / prev * 100) if prev else 0

        # Pre/post prices — try info dict first, then reliable 1-min history fallback
        pre_p   = info.get('preMarketPrice')
        post_p  = info.get('postMarketPrice')
        pre_cp  = info.get('preMarketChangePercent') or 0
        post_cp = info.get('postMarketChangePercent') or 0
        if pre_cp  and abs(pre_cp)  < 1: pre_cp  *= 100
        if post_cp and abs(post_cp) < 1: post_cp *= 100

        # Reliable fallback: 1-min bars with pre/post hours included.
        # Yahoo's info dict often omits preMarketPrice even during pre-market —
        # history(prepost=True) always has the actual latest tick.
        if not pre_p and not post_p:
            try:
                hist_pp = stock.history(period='1d', interval='1m', prepost=True)
                if not hist_pp.empty:
                    last_px = float(hist_pp['Close'].iloc[-1])
                    last_ts = hist_pp.index[-1]
                    last_et = (last_ts.tz_convert(ET)
                               if hasattr(last_ts, 'tz_convert') else last_ts)
                    h, m = last_et.hour, last_et.minute
                    # Pre-market: 4:00 AM – 9:29 AM ET
                    if (4 <= h < 9) or (h == 9 and m < 30):
                        pre_p  = last_px
                        pre_cp = round((last_px - prev) / prev * 100, 2) if prev else 0
                    # Post-market: 4:00 PM – 8:00 PM ET
                    elif 16 <= h < 20:
                        post_p  = last_px
                        post_cp = round((last_px - prev) / prev * 100, 2) if prev else 0
            except Exception:
                pass

        earnings_date = None
        try:
            cal = stock.calendar
            if isinstance(cal, dict):
                dates = cal.get('Earnings Date', [])
                if dates: earnings_date = str(list(dates)[0])[:10]
            elif cal is not None and hasattr(cal, 'columns'):
                col = next((c for c in cal.columns if 'Earnings' in str(c)), None)
                if col:
                    vals = cal[col].dropna()
                    if len(vals): earnings_date = str(vals.iloc[0])[:10]
        except Exception:
            pass

        # Decide effective price based on current market session
        sess   = market_session()
        status = sess['status']
        if status == 'pre'  and pre_p:
            eff_price = pre_p
            eff_chg   = pre_p - prev
            eff_chgp  = (eff_chg / prev * 100) if prev else 0
        elif status == 'post' and post_p:
            eff_price = post_p
            eff_chg   = post_p - prev
            eff_chgp  = (eff_chg / prev * 100) if prev else 0
        else:
            eff_price = price or prev
            eff_chg   = chg
            eff_chgp  = chgp

        data = {
            'ticker': ticker,
            'name': info.get('longName') or info.get('shortName', ticker),
            'price':        price,
            'prev_close':   prev,
            'change':       chg,
            'change_pct':   chgp,
            # display_price: always the official market price / last close
            # (never pre/post) — shown in the Price column
            'display_price':        price or prev,
            # effective_price: most current price including pre/post
            # — used for all portfolio value / P&L calculations
            'effective_price':      eff_price,
            'effective_change':     eff_chg,
            'effective_change_pct': eff_chgp,
            'market_status': status,
            'pre_price':    pre_p,  'pre_chg_pct':  pre_cp,
            'post_price':   post_p, 'post_chg_pct': post_cp,
            'earnings_date': earnings_date,
            'day_high':  info.get('dayHigh', 0),
            'day_low':   info.get('dayLow', 0),
            'open_price': info.get('regularMarketOpen') or info.get('open', 0),
            'volume':    info.get('volume', 0),
            'exchange':  info.get('exchange', 'NASDAQ'),
            'market_cap':     info.get('marketCap', 0),
            'week_52_high':   info.get('fiftyTwoWeekHigh', 0),
            'week_52_low':    info.get('fiftyTwoWeekLow', 0),
            'dividend_rate':  info.get('dividendRate', 0),
            'dividend_yield': round((info.get('dividendYield') or 0) * 100, 2),
            'ex_div_date':    str(date.fromtimestamp(info['exDividendDate']))
                              if info.get('exDividendDate') else None,
            'success': True,
        }
        with cache_lock:
            price_cache[ticker] = {'data': data, 'ts': time.time()}
        return data
    except Exception as e:
        return {'ticker': ticker, 'success': False, 'error': str(e)}


# ─────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────

_PUBLIC_ENDPOINTS = {'login', 'auth_github', 'auth_callback', 'static'}

@app.before_request
def _require_auth():
    """Redirect to login if auth is enabled and user is not logged in."""
    if not AUTH_ENABLED:
        return   # local mode — no login required
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return   # these routes are always accessible
    if not current_user.is_authenticated:
        return redirect(url_for('login'))


@app.route('/login')
def login():
    if AUTH_ENABLED and current_user.is_authenticated:
        return redirect(url_for('index'))
    return render_template('login.html', auth_enabled=AUTH_ENABLED, error=None)


@app.route('/auth/github')
def auth_github():
    if not AUTH_ENABLED:
        return redirect(url_for('index'))
    callback = url_for('auth_callback', _external=True)
    return _gh.authorize_redirect(callback)


@app.route('/auth/github/callback')
def auth_callback():
    if not AUTH_ENABLED:
        return redirect(url_for('index'))
    try:
        token   = _gh.authorize_access_token()
        profile = _gh.get('user', token=token).json()
    except Exception as e:
        return render_template('login.html', auth_enabled=True,
                               error=f'GitHub login failed: {e}')

    username  = profile.get('login', '')
    github_id = profile.get('id')
    avatar    = profile.get('avatar_url', '')

    # Check whitelist (empty ALLOWED_USERS = anyone with GitHub can log in)
    if _ALLOWED_USERS and username.lower() not in _ALLOWED_USERS:
        return render_template('login.html', auth_enabled=True,
                               error=f'@{username} is not on the access list. Ask the admin to add you.')

    # Find or create user record
    conn = get_db()
    row  = conn.execute('SELECT * FROM users WHERE github_id=?', (github_id,)).fetchone()
    if row:
        uid = row['id']
        conn.execute('UPDATE users SET github_username=?, avatar_url=? WHERE id=?',
                     (username, avatar, uid))
        conn.commit()
    else:
        cur = conn.execute(
            'INSERT INTO users (github_id, github_username, avatar_url) VALUES (?,?,?)',
            (github_id, username, avatar))
        conn.commit()
        uid = cur.lastrowid
        _seed_watchlists(conn, uid)   # give new user their 5 default watchlists
    conn.close()

    login_user(User(uid, github_id, username, avatar), remember=True)
    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/api/me')
def api_me():
    """Who is the current user? Used by the frontend to show avatar/username."""
    if not AUTH_ENABLED:
        return jsonify({'username': 'local', 'avatar': '', 'auth_enabled': False})
    return jsonify({
        'username':     current_user.username,
        'avatar':       current_user.avatar,
        'auth_enabled': True,
    })


# ─────────────────────────────────────────────────────────
#  MAIN APP ROUTE
# ─────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ─────────────────────────────────────────────────────────
#  STOCK / PRICE ROUTES
# ─────────────────────────────────────────────────────────

@app.route('/api/stock/<ticker>')
def get_stock(ticker):
    return jsonify(fetch_stock_data(ticker.upper()))


@app.route('/api/prices')
def get_prices():
    uid  = _uid()
    conn = get_db()
    tickers = [r['ticker'] for r in
               conn.execute('SELECT ticker FROM positions WHERE user_id=?', (uid,)).fetchall()]
    conn.close()
    results = {}
    threads = []
    def _f(t): results[t] = fetch_stock_data(t)
    for t in tickers:
        th = threading.Thread(target=_f, args=(t,), daemon=True)
        threads.append(th); th.start()
    for th in threads: th.join(timeout=10)
    return jsonify(results)


@app.route('/api/sparkline/<ticker>')
def sparkline(ticker):
    ticker = ticker.upper()
    with cache_lock:
        if ticker in sparkline_cache:
            c = sparkline_cache[ticker]
            if time.time() - c['ts'] < SPARKLINE_TTL:
                return jsonify(c['data'])
    try:
        hist = yf.Ticker(ticker).history(period='1d', interval='5m')
        if hist.empty or len(hist) < 3:
            hist = yf.Ticker(ticker).history(period='2d', interval='15m')
        prices = [round(float(p), 4) for p in hist['Close'].dropna().tolist()]
        data   = {'prices': prices, 'success': True}
        with cache_lock:
            sparkline_cache[ticker] = {'data': data, 'ts': time.time()}
        return jsonify(data)
    except Exception as e:
        return jsonify({'prices': [], 'success': False, 'error': str(e)})


@app.route('/api/sparklines')
def sparklines_batch():
    uid  = _uid()
    conn = get_db()
    tickers = [r['ticker'] for r in
               conn.execute('SELECT ticker FROM positions WHERE user_id=?', (uid,)).fetchall()]
    conn.close()
    results = {}
    threads = []
    def fetch(t):
        t = t.upper()
        with cache_lock:
            if t in sparkline_cache:
                c = sparkline_cache[t]
                if time.time() - c['ts'] < SPARKLINE_TTL:
                    results[t] = c['data']; return
        try:
            hist = yf.Ticker(t).history(period='1d', interval='5m')
            if hist.empty or len(hist) < 3:
                hist = yf.Ticker(t).history(period='2d', interval='15m')
            prices = [round(float(p), 4) for p in hist['Close'].dropna().tolist()]
            d = {'prices': prices, 'success': True}
            with cache_lock:
                sparkline_cache[t] = {'data': d, 'ts': time.time()}
            results[t] = d
        except Exception:
            results[t] = {'prices': [], 'success': False}
    for t in tickers:
        th = threading.Thread(target=fetch, args=(t,), daemon=True)
        threads.append(th); th.start()
    for th in threads: th.join(timeout=20)
    return jsonify(results)


# ─────────────────────────────────────────────────────────
#  PORTFOLIO ROUTES
# ─────────────────────────────────────────────────────────

@app.route('/api/portfolio')
def get_portfolio():
    uid  = _uid()
    conn = get_db()
    positions = conn.execute(
        'SELECT * FROM positions WHERE user_id=? ORDER BY ticker', (uid,)).fetchall()
    realized  = conn.execute(
        'SELECT ticker, SUM(net_profit_loss) as total FROM realized_pnl WHERE user_id=? GROUP BY ticker',
        (uid,)).fetchall()
    conn.close()
    r_map  = {r['ticker']: (r['total'] or 0) for r in realized}
    result = []
    for p in positions:
        row = dict(p)
        row['realized_pnl'] = r_map.get(p['ticker'], 0)
        result.append(row)
    return jsonify(result)


@app.route('/api/portfolio/buy', methods=['POST'])
def buy():
    uid        = _uid()
    d          = request.json
    ticker     = d['ticker'].upper()
    shares     = float(d.get('shares') or 0)
    price      = float(d.get('price')  or 0)
    commission = float(d.get('commission', 0))
    trade_date = d.get('trade_date', '')

    conn = get_db()

    # ── Zero-share "track-only" entry ────────────────────────
    if shares == 0:
        try:
            conn.execute(
                'INSERT INTO positions (user_id,ticker,shares,avg_buy_price,total_invested) VALUES (?,?,0,0,0)',
                (uid, ticker))
            conn.commit()
        except sqlite3.IntegrityError:
            pass   # already exists — nothing to do
        conn.close()
        return jsonify({'success': True, 'track_only': True})

    # ── Normal buy ────────────────────────────────────────────
    total_cost = shares * price + commission
    eff_avg    = total_cost / shares
    existing   = conn.execute(
        'SELECT * FROM positions WHERE user_id=? AND ticker=?', (uid, ticker)).fetchone()
    if existing:
        ns  = existing['shares']         + shares
        ni  = existing['total_invested'] + total_cost
        nav = ni / ns
        conn.execute(
            'UPDATE positions SET shares=?,avg_buy_price=?,total_invested=? WHERE user_id=? AND ticker=?',
            (ns, nav, ni, uid, ticker))
    else:
        conn.execute(
            'INSERT INTO positions (user_id,ticker,shares,avg_buy_price,total_invested) VALUES (?,?,?,?,?)',
            (uid, ticker, shares, eff_avg, total_cost))
    conn.execute(
        'INSERT INTO transactions (user_id,ticker,type,shares,price,commission,total,trade_date) VALUES (?,?,?,?,?,?,?,?)',
        (uid, ticker, 'buy', shares, price, commission, total_cost, trade_date))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/portfolio/sell', methods=['POST'])
def sell():
    uid        = _uid()
    d          = request.json
    ticker     = d['ticker'].upper()
    shares     = float(d['shares'])
    price      = float(d['price'])
    commission = float(d.get('commission', 0))
    trade_date = d.get('trade_date', '')

    conn = get_db()
    pos  = conn.execute(
        'SELECT * FROM positions WHERE user_id=? AND ticker=?', (uid, ticker)).fetchone()
    if not pos or pos['shares'] < shares - 0.0001:
        conn.close()
        return jsonify({'success': False, 'error': 'Insufficient shares'}), 400

    avg        = pos['avg_buy_price']
    gross      = shares * price
    net        = gross - commission
    cost_basis = avg * shares
    pl         = gross - cost_basis
    net_pl     = net  - cost_basis
    pl_pct     = ((price - avg) / avg * 100) if avg else 0
    new_shares = pos['shares']        - shares
    new_inv    = max(0, pos['total_invested'] - cost_basis)

    if new_shares < 0.0001:
        conn.execute('DELETE FROM positions WHERE user_id=? AND ticker=?', (uid, ticker))
    else:
        conn.execute(
            'UPDATE positions SET shares=?,total_invested=? WHERE user_id=? AND ticker=?',
            (new_shares, new_inv, uid, ticker))
    conn.execute(
        'INSERT INTO realized_pnl (user_id,ticker,shares,buy_price,sell_price,commission,profit_loss,net_profit_loss,pnl_pct,trade_date) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (uid, ticker, shares, avg, price, commission, pl, net_pl, pl_pct, trade_date))
    conn.execute(
        'INSERT INTO transactions (user_id,ticker,type,shares,price,commission,total,trade_date) VALUES (?,?,?,?,?,?,?,?)',
        (uid, ticker, 'sell', shares, price, commission, gross, trade_date))
    conn.commit(); conn.close()
    return jsonify({'success': True, 'profit_loss': pl, 'net_profit_loss': net_pl, 'pnl_pct': pl_pct})


@app.route('/api/portfolio/<ticker>', methods=['PUT'])
def edit_position(ticker):
    uid    = _uid()
    d      = request.json
    try:
        shares = float(d['shares'])
        avg    = float(d['avg_buy_price'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid data.'}), 400
    if shares <= 0 or avg <= 0:
        return jsonify({'success': False, 'error': 'Shares and price must be positive.'}), 400
    total  = shares * avg
    conn   = get_db()
    result = conn.execute(
        'UPDATE positions SET shares=?, avg_buy_price=?, total_invested=? WHERE user_id=? AND ticker=?',
        (shares, avg, total, uid, ticker.upper()))
    conn.commit(); conn.close()
    if result.rowcount == 0:
        return jsonify({'success': False, 'error': 'Position not found.'}), 404
    return jsonify({'success': True})


@app.route('/api/portfolio/<ticker>', methods=['DELETE'])
def delete_position(ticker):
    uid  = _uid()
    conn = get_db()
    conn.execute('DELETE FROM positions WHERE user_id=? AND ticker=?', (uid, ticker.upper()))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/historical-price/<ticker>/<trade_date>')
def historical_price(ticker, trade_date):
    """Return the closing price of a ticker on or before the given date."""
    try:
        target = date.fromisoformat(trade_date)
        start  = target - timedelta(days=7)
        end    = target + timedelta(days=2)
        hist   = yf.Ticker(ticker.upper()).history(start=str(start), end=str(end))
        if hist.empty:
            return jsonify({'success': False, 'error': 'No data found'})
        # Convert timezone-aware index to plain dates
        idx_dates  = [d.date() if hasattr(d, 'date') else d for d in hist.index]
        available  = [d for d in idx_dates if d <= target]
        if not available:
            return jsonify({'success': False, 'error': 'No trading data on or before that date'})
        closest    = max(available)
        close_price = round(float(hist.iloc[idx_dates.index(closest)]['Close']), 4)
        return jsonify({'success': True, 'price': close_price, 'actual_date': str(closest)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/transactions/<int:txn_id>', methods=['PUT'])
def edit_transaction(txn_id):
    uid = _uid()
    d   = request.json
    try:
        shares     = float(d['shares'])
        price      = float(d['price'])
        commission = float(d.get('commission', 0))
        trade_date = d.get('trade_date', '')
    except (KeyError, TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid data.'}), 400
    if shares <= 0 or price <= 0:
        return jsonify({'success': False, 'error': 'Shares and price must be positive.'}), 400
    conn = get_db()
    txn  = conn.execute('SELECT * FROM transactions WHERE id=? AND user_id=?',
                        (txn_id, uid)).fetchone()
    if not txn:
        conn.close()
        return jsonify({'success': False, 'error': 'Transaction not found.'}), 404
    ticker = txn['ticker']
    total  = shares * price + commission if txn['type'] == 'buy' else shares * price
    conn.execute(
        'UPDATE transactions SET shares=?,price=?,commission=?,total=?,trade_date=? WHERE id=? AND user_id=?',
        (shares, price, commission, total, trade_date, txn_id, uid))
    conn.commit()
    # Recalculate position from all transactions for this ticker
    recalculate_position(conn, uid, ticker)
    conn.close()
    return jsonify({'success': True, 'recalculated': True})


@app.route('/api/transactions/<int:txn_id>', methods=['DELETE'])
def delete_transaction(txn_id):
    uid  = _uid()
    conn = get_db()
    txn  = conn.execute('SELECT * FROM transactions WHERE id=? AND user_id=?',
                        (txn_id, uid)).fetchone()
    if not txn:
        conn.close()
        return jsonify({'success': False, 'error': 'Transaction not found.'}), 404
    ticker   = txn['ticker']
    txn_type = txn['type']
    # Soft-delete: move to recycle bin instead of hard delete
    conn.execute(
        "UPDATE transactions SET deleted_at=datetime('now') WHERE id=? AND user_id=?",
        (txn_id, uid))
    conn.commit()
    # If it was a sell, also soft-delete the matching realized P&L record
    if txn_type == 'sell':
        conn.execute('''
            UPDATE realized_pnl SET deleted_at=datetime('now') WHERE rowid = (
                SELECT rowid FROM realized_pnl
                WHERE user_id=? AND ticker=?
                  AND ABS(shares - ?) < 0.0001
                  AND ABS(sell_price - ?) < 0.01
                  AND (deleted_at IS NULL OR deleted_at='')
                ORDER BY id DESC LIMIT 1
            )
        ''', (uid, ticker, txn['shares'], txn['price']))
        conn.commit()
    # Recalculate position from remaining (non-deleted) transactions
    recalculate_position(conn, uid, ticker)
    conn.close()
    return jsonify({'success': True, 'recalculated': True})


@app.route('/api/transactions/deleted')
def get_deleted_transactions():
    """Return all soft-deleted (trashed) transactions."""
    uid  = _uid()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id=? AND deleted_at IS NOT NULL AND deleted_at!=''"
        " ORDER BY deleted_at DESC LIMIT 200",
        (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/transactions/<int:txn_id>/restore', methods=['POST'])
def restore_transaction(txn_id):
    """Restore a soft-deleted transaction from the recycle bin."""
    uid  = _uid()
    conn = get_db()
    txn  = conn.execute(
        "SELECT * FROM transactions WHERE id=? AND user_id=? AND deleted_at IS NOT NULL AND deleted_at!=''",
        (txn_id, uid)).fetchone()
    if not txn:
        conn.close()
        return jsonify({'success': False, 'error': 'Transaction not found in trash.'}), 404
    ticker   = txn['ticker']
    txn_type = txn['type']
    conn.execute('UPDATE transactions SET deleted_at=NULL WHERE id=? AND user_id=?', (txn_id, uid))
    conn.commit()
    # If it was a sell, also restore the matching realized P&L record
    if txn_type == 'sell':
        conn.execute('''
            UPDATE realized_pnl SET deleted_at=NULL WHERE rowid = (
                SELECT rowid FROM realized_pnl
                WHERE user_id=? AND ticker=?
                  AND ABS(shares - ?) < 0.0001
                  AND ABS(sell_price - ?) < 0.01
                  AND deleted_at IS NOT NULL AND deleted_at!=''
                ORDER BY id DESC LIMIT 1
            )
        ''', (uid, ticker, txn['shares'], txn['price']))
        conn.commit()
    recalculate_position(conn, uid, ticker)
    conn.close()
    return jsonify({'success': True, 'recalculated': True})


@app.route('/api/transactions/<int:txn_id>/permanent', methods=['DELETE'])
def permanent_delete_transaction(txn_id):
    """Permanently delete a transaction that is already in the recycle bin."""
    uid  = _uid()
    conn = get_db()
    txn  = conn.execute(
        "SELECT * FROM transactions WHERE id=? AND user_id=? AND deleted_at IS NOT NULL AND deleted_at!=''",
        (txn_id, uid)).fetchone()
    if not txn:
        conn.close()
        return jsonify({'success': False, 'error': 'Transaction not found in trash.'}), 404
    ticker   = txn['ticker']
    txn_type = txn['type']
    conn.execute('DELETE FROM transactions WHERE id=? AND user_id=?', (txn_id, uid))
    conn.commit()
    if txn_type == 'sell':
        conn.execute('''
            DELETE FROM realized_pnl WHERE rowid = (
                SELECT rowid FROM realized_pnl
                WHERE user_id=? AND ticker=?
                  AND ABS(shares - ?) < 0.0001
                  AND ABS(sell_price - ?) < 0.01
                  AND deleted_at IS NOT NULL AND deleted_at!=''
                ORDER BY id DESC LIMIT 1
            )
        ''', (uid, ticker, txn['shares'], txn['price']))
        conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/market-status')
def api_market_status():
    sess = market_session()
    return jsonify(sess)


@app.route('/api/realized-pnl')
def get_realized_pnl():
    uid  = _uid()
    conn = get_db()
    rows = conn.execute('''
        SELECT *,
               COALESCE(net_profit_loss, profit_loss)       AS net_profit_loss,
               COALESCE(commission, 0)                       AS commission,
               COALESCE(pnl_pct, (sell_price - buy_price)
                        / NULLIF(buy_price,0) * 100)         AS pnl_pct
        FROM realized_pnl
        WHERE user_id=? AND (deleted_at IS NULL OR deleted_at='')
        ORDER BY date DESC LIMIT 200
    ''', (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/transactions')
def get_transactions():
    uid  = _uid()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id=? AND (deleted_at IS NULL OR deleted_at='') ORDER BY date DESC LIMIT 200",
        (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────
#  WATCHLIST ROUTES
# ─────────────────────────────────────────────────────────

@app.route('/api/watchlists')
def get_watchlists():
    uid  = _uid()
    conn = get_db()
    wls  = conn.execute(
        'SELECT * FROM watchlists WHERE user_id=? ORDER BY position', (uid,)).fetchall()
    result = []
    for wl in wls:
        items = conn.execute(
            'SELECT ticker FROM watchlist_items WHERE watchlist_id=? ORDER BY added',
            (wl['id'],)).fetchall()
        result.append({'id': wl['id'], 'name': wl['name'],
                       'tickers': [i['ticker'] for i in items]})
    conn.close()
    return jsonify(result)


@app.route('/api/watchlists/<int:wl_id>', methods=['PUT'])
def rename_watchlist(wl_id):
    uid  = _uid()
    name = (request.json.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name required.'}), 400
    conn = get_db()
    conn.execute('UPDATE watchlists SET name=? WHERE id=? AND user_id=?', (name, wl_id, uid))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/watchlists/<int:wl_id>/items', methods=['POST'])
def add_watchlist_item(wl_id):
    uid    = _uid()
    ticker = (request.json.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'success': False, 'error': 'Ticker required.'}), 400
    conn = get_db()
    wl   = conn.execute('SELECT id FROM watchlists WHERE id=? AND user_id=?',
                        (wl_id, uid)).fetchone()
    if not wl:
        conn.close()
        return jsonify({'success': False, 'error': 'Watchlist not found.'}), 404
    try:
        conn.execute('INSERT INTO watchlist_items (watchlist_id,ticker) VALUES (?,?)',
                     (wl_id, ticker))
        conn.commit()
    except sqlite3.IntegrityError:
        pass   # already in list
    conn.close()
    return jsonify({'success': True})


@app.route('/api/watchlists/<int:wl_id>/items/<ticker>', methods=['DELETE'])
def remove_watchlist_item(wl_id, ticker):
    uid  = _uid()
    conn = get_db()
    conn.execute('DELETE FROM watchlist_items WHERE watchlist_id=? AND ticker=?',
                 (wl_id, ticker.upper()))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/watchlists/<int:wl_id>/prices')
def watchlist_prices(wl_id):
    uid  = _uid()
    conn = get_db()
    rows = conn.execute(
        'SELECT ticker FROM watchlist_items WHERE watchlist_id=?', (wl_id,)).fetchall()
    conn.close()
    tickers = [r['ticker'] for r in rows]
    results = {}
    threads = []
    def _f(t): results[t] = fetch_stock_data(t)
    for t in tickers:
        th = threading.Thread(target=_f, args=(t,), daemon=True)
        threads.append(th); th.start()
    for th in threads: th.join(timeout=15)
    return jsonify(results)


# ─────────────────────────────────────────────────────────
#  AI ANALYST
# ─────────────────────────────────────────────────────────

SECTOR_ETF = {
    'Technology': 'XLK', 'Healthcare': 'XLV', 'Financials': 'XLF',
    'Financial Services': 'XLF', 'Energy': 'XLE', 'Consumer Cyclical': 'XLY',
    'Consumer Defensive': 'XLP', 'Industrials': 'XLI', 'Basic Materials': 'XLB',
    'Real Estate': 'XLRE', 'Utilities': 'XLU', 'Communication Services': 'XLC',
}

MACRO_SYMBOLS = {
    'VIX (Fear Index)':    '^VIX',
    'S&P 500':             '^GSPC',
    'NASDAQ':              '^IXIC',
    'Dow Jones':           '^DJI',
    'Oil WTI (barrel)':    'CL=F',
    'Brent Oil':           'BZ=F',
    'Natural Gas':         'NG=F',
    'Gold':                'GC=F',
    'Silver':              'SI=F',
    'Copper':              'HG=F',
    '10Y Treasury Yield':  '^TNX',
    '2Y Treasury Yield':   '^IRX',
    'US Dollar Index':     'DX-Y.NYB',
    'Bitcoin':             'BTC-USD',
    'Ethereum':            'ETH-USD',
}

def _safe(v):
    try:
        f = float(v)
        return None if (f != f) else f   # NaN → None
    except Exception:
        return None

def _fmtM(v):
    v = _safe(v)
    if v is None: return 'N/A'
    if v >= 1e12: return f'${v/1e12:.2f}T'
    if v >= 1e9:  return f'${v/1e9:.2f}B'
    if v >= 1e6:  return f'${v/1e6:.0f}M'
    return f'${v:.0f}'

def _fmtP(v):
    v = _safe(v)
    return f'{v*100:.1f}%' if v is not None else 'N/A'

def _fmtV(v, d=2):
    v = _safe(v)
    return f'{v:.{d}f}' if v is not None else 'N/A'

def _quick_chg(sym):
    """Return (price, pct_chg_1d) for a symbol, or (None, None)."""
    try:
        h = yf.Ticker(sym).history(period='5d')
        if h.empty or len(h) < 2: return None, None
        cur  = float(h['Close'].iloc[-1])
        prev = float(h['Close'].iloc[-2])
        chg  = (cur - prev) / prev * 100 if prev else 0
        return round(cur, 4), round(chg, 2)
    except Exception:
        return None, None


@app.route('/api/ai/analyze', methods=['POST'])
def ai_analyze():
    """Full AI stock analysis: technicals + fundamentals + macro + news + analysts."""
    d       = request.json or {}
    ticker  = (d.get('ticker') or '').strip().upper()
    api_key = (d.get('api_key') or '').strip()
    if not ticker:
        return jsonify({'success': False, 'error': 'Ticker required.'}), 400
    if not api_key:
        return jsonify({'success': False,
                        'error': 'No API key. Add your Groq key in Settings.'}), 400

    try:
        # ── Core stock data ───────────────────────────────────
        stock = yf.Ticker(ticker)
        info  = stock.info
        if not (info.get('regularMarketPrice') or info.get('currentPrice')
                or info.get('previousClose')):
            return jsonify({'success': False,
                            'error': f'Ticker "{ticker}" not found.'}), 404

        hist = stock.history(period='1y')
        if hist.empty:
            return jsonify({'success': False,
                            'error': 'No price history available.'}), 404

        closes  = hist['Close'].dropna()
        volumes = hist['Volume'].dropna()
        cur     = float(closes.iloc[-1])

        def pct_chg(n):
            if len(closes) <= n: return None
            v = closes.pct_change(n).iloc[-1]
            return None if v != v else round(float(v) * 100, 2)

        def sma(n):
            if len(closes) < n: return None
            v = closes.rolling(n).mean().iloc[-1]
            return None if v != v else round(float(v), 2)

        sma20, sma50, sma200 = sma(20), sma(50), sma(200)

        def calc_rsi(period=14):
            if len(closes) < period + 1: return None
            dd = closes.diff()
            g  = dd.clip(lower=0).rolling(period).mean()
            l  = (-dd.clip(upper=0)).rolling(period).mean()
            rs = g / l
            v  = (100 - 100 / (1 + rs)).iloc[-1]
            return None if v != v else round(float(v), 1)

        rsi_val = calc_rsi()

        ema12 = closes.ewm(span=12).mean()
        ema26 = closes.ewm(span=26).mean()
        macd  = ema12 - ema26
        sig   = macd.ewm(span=9).mean()
        macd_v, sig_v = float(macd.iloc[-1]), float(sig.iloc[-1])
        macd_cross = 'bullish' if macd_v > sig_v else 'bearish'

        # Bollinger Bands
        bb_mid  = closes.rolling(20).mean()
        bb_std  = closes.rolling(20).std()
        bb_up   = bb_mid + 2 * bb_std
        bb_lo   = bb_mid - 2 * bb_std
        bb_pct  = round(((cur - float(bb_lo.iloc[-1])) /
                         (float(bb_up.iloc[-1]) - float(bb_lo.iloc[-1])) * 100), 1) \
                  if float(bb_up.iloc[-1]) != float(bb_lo.iloc[-1]) else 50

        avg_vol   = float(volumes.rolling(20).mean().iloc[-1]) if len(volumes) >= 20 else float(volumes.mean())
        vol_ratio = round(float(volumes.iloc[-1]) / avg_vol, 2) if avg_vol else 1

        w52h = _safe(info.get('fiftyTwoWeekHigh')) or cur
        w52l = _safe(info.get('fiftyTwoWeekLow'))  or cur
        pos52 = round((cur - w52l) / (w52h - w52l) * 100, 1) if w52h != w52l else 50

        # ── Parallel fetches: macro + sector + news + options ─
        macro_results  = {}
        sector_result  = {}
        news_lines     = []
        options_result = {}

        def _fetch_macro():
            threads = []
            def _f(name, sym):
                p, c = _quick_chg(sym)
                if p is not None:
                    macro_results[name] = {'price': p, 'chg': c}
            for name, sym in MACRO_SYMBOLS.items():
                t = threading.Thread(target=_f, args=(name, sym), daemon=True)
                threads.append(t); t.start()
            for t in threads: t.join(timeout=10)

        def _fetch_sector():
            etf = SECTOR_ETF.get(info.get('sector', ''))
            if not etf: return
            p, c = _quick_chg(etf)
            if p is not None:
                sector_result['etf'] = etf
                sector_result['price'] = p
                sector_result['chg']   = c

        def _fetch_news():
            try:
                raw_news = stock.news or []
                for n in raw_news[:10]:
                    title = (n.get('title') or '').strip()
                    pub   = n.get('publisher', '')
                    if title:
                        news_lines.append(f'• {title}  [{pub}]')
            except Exception:
                pass

        def _fetch_options():
            try:
                exps = stock.options
                if not exps: return
                chain = stock.option_chain(exps[0])
                calls_vol = chain.calls['volume'].fillna(0).sum()
                puts_vol  = chain.puts['volume'].fillna(0).sum()
                pc_ratio  = round(puts_vol / calls_vol, 2) if calls_vol else None
                avg_iv    = chain.calls['impliedVolatility'].replace(0, float('nan')).mean()
                options_result['put_call_ratio'] = pc_ratio
                options_result['avg_iv']         = round(float(avg_iv) * 100, 1) if avg_iv == avg_iv else None
                options_result['expiry']         = exps[0]
            except Exception:
                pass

        threads = [
            threading.Thread(target=_fetch_macro,   daemon=True),
            threading.Thread(target=_fetch_sector,  daemon=True),
            threading.Thread(target=_fetch_news,    daemon=True),
            threading.Thread(target=_fetch_options, daemon=True),
        ]
        for t in threads: t.start()
        for t in threads: t.join(timeout=12)

        # ── Format macro section ──────────────────────────────
        macro_lines = []
        for name, v in macro_results.items():
            chg_str = f"{v['chg']:+.2f}%" if v['chg'] is not None else ''
            macro_lines.append(f"  {name}: {v['price']}  {chg_str}")

        sector_line = ''
        if sector_result:
            sector_line = (f"Sector ETF ({sector_result['etf']}): "
                           f"{sector_result['price']}  {sector_result['chg']:+.2f}%")

        # ── Analyst consensus from yfinance ──────────────────
        analyst_lines = []
        rec_mean   = _safe(info.get('recommendationMean'))
        rec_key    = info.get('recommendationKey', '')
        n_analysts = info.get('numberOfAnalystOpinions', 0)
        tgt_mean   = _safe(info.get('targetMeanPrice'))
        tgt_hi     = _safe(info.get('targetHighPrice'))
        tgt_lo     = _safe(info.get('targetLowPrice'))
        if rec_key:
            analyst_lines.append(f"Consensus: {rec_key.upper()} (mean score {_fmtV(rec_mean)}/5, {n_analysts} analysts)")
        if tgt_mean:
            upside = ((tgt_mean - cur) / cur * 100)
            analyst_lines.append(f"Price Target: mean ${tgt_mean:.2f} (low ${tgt_lo:.2f}, high ${tgt_hi:.2f}) — {upside:+.1f}% upside")

        # ── Ownership & short interest ────────────────────────
        inst_pct   = _fmtP(info.get('heldPercentInstitutions'))
        insider_pct= _fmtP(info.get('heldPercentInsiders'))
        short_pct  = _fmtP(info.get('shortPercentOfFloat'))
        short_ratio= _fmtV(info.get('shortRatio'))

        # ── Build the mega-prompt ─────────────────────────────
        prompt = f"""=== COMPREHENSIVE STOCK ANALYSIS REQUEST: {ticker} ===
Company: {info.get('longName', ticker)}
Sector: {info.get('sector','N/A')} | Industry: {info.get('industry','N/A')}
Exchange: {info.get('exchange','N/A')} | Country: {info.get('country','N/A')}

--- PRICE & PERFORMANCE ---
Current Price: ${cur:.2f}
1-Day: {_fmtV(pct_chg(1))}% | 1-Week: {_fmtV(pct_chg(5))}% | 1-Month: {_fmtV(pct_chg(21))}%
3-Month: {_fmtV(pct_chg(63))}% | YTD: {_fmtV(pct_chg(len(closes)-1))}%
52W High: ${w52h:.2f} ({((cur/w52h-1)*100):.1f}% from high)
52W Low:  ${w52l:.2f} ({((cur/w52l-1)*100):.1f}% from low)
52W Position: {pos52:.0f}% (0=at low, 100=at high)

--- TECHNICAL INDICATORS ---
SMA-20:  ${sma20 or 'N/A'} → price {'ABOVE ✓' if sma20 and cur>sma20 else 'BELOW ✗'}
SMA-50:  ${sma50 or 'N/A'} → price {'ABOVE ✓' if sma50 and cur>sma50 else 'BELOW ✗'}
SMA-200: ${sma200 or 'N/A'} → price {'ABOVE ✓' if sma200 and cur>sma200 else 'BELOW ✗'}
RSI(14): {rsi_val or 'N/A'} {'⚠ OVERBOUGHT' if rsi_val and rsi_val>70 else '⚠ OVERSOLD' if rsi_val and rsi_val<30 else '(neutral)'}
MACD: {macd_cross.upper()} crossover (MACD={macd_v:.3f}, Signal={sig_v:.3f})
Bollinger %B: {bb_pct}% (0=at lower band, 100=at upper band)
Volume vs 20d avg: {vol_ratio}x {'(unusually high)' if vol_ratio > 2 else '(unusually low)' if vol_ratio < 0.5 else ''}

--- FUNDAMENTALS ---
Market Cap: {_fmtM(info.get('marketCap'))}
P/E (TTM): {_fmtV(info.get('trailingPE'))} | P/E (Fwd): {_fmtV(info.get('forwardPE'))} | PEG: {_fmtV(info.get('pegRatio'))}
EPS (TTM): ${_fmtV(info.get('trailingEps'))} | EPS (Fwd): ${_fmtV(info.get('forwardEps'))}
Revenue Growth YoY: {_fmtP(info.get('revenueGrowth'))} | Earnings Growth: {_fmtP(info.get('earningsGrowth'))}
Profit Margin: {_fmtP(info.get('profitMargins'))} | Gross Margin: {_fmtP(info.get('grossMargins'))} | EBITDA Margin: {_fmtP(info.get('ebitdaMargins'))}
ROE: {_fmtP(info.get('returnOnEquity'))} | ROA: {_fmtP(info.get('returnOnAssets'))}
Debt/Equity: {_fmtV(info.get('debtToEquity'))} | Current Ratio: {_fmtV(info.get('currentRatio'))} | Quick Ratio: {_fmtV(info.get('quickRatio'))}
Free Cash Flow: {_fmtM(info.get('freeCashflow'))} | Operating Cash Flow: {_fmtM(info.get('operatingCashflow'))}
Beta: {_fmtV(info.get('beta'))} | Dividend Yield: {_fmtP(info.get('dividendYield'))}
Book Value/Share: ${_fmtV(info.get('bookValue'))} | Price/Book: {_fmtV(info.get('priceToBook'))}

--- ANALYST CONSENSUS ---
{chr(10).join(analyst_lines) if analyst_lines else 'No analyst data available'}

--- OWNERSHIP & SHORT INTEREST ---
Institutional Ownership: {inst_pct} | Insider Ownership: {insider_pct}
Short % of Float: {short_pct} | Short Ratio (days to cover): {short_ratio}

--- OPTIONS MARKET SENTIMENT ---
{'Put/Call Ratio: ' + str(options_result.get('put_call_ratio','N/A')) + (' (bearish >1)' if options_result.get('put_call_ratio') and options_result['put_call_ratio']>1 else ' (bullish <1)' if options_result.get('put_call_ratio') and options_result['put_call_ratio']<1 else '') if options_result else 'Options data unavailable'}
{'Avg Implied Volatility: ' + str(options_result.get('avg_iv','N/A')) + '%' if options_result else ''}

--- GLOBAL MACRO CONTEXT ---
{chr(10).join(macro_lines) if macro_lines else 'Macro data unavailable'}

--- SECTOR PERFORMANCE ---
{sector_line if sector_line else 'Sector ETF data unavailable'}

--- RECENT NEWS HEADLINES ---
{chr(10).join(news_lines) if news_lines else 'No recent news available'}
"""

        # ── Call Groq ─────────────────────────────────────────
        client = groq_sdk.Groq(api_key=api_key)
        msg = client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            max_tokens=1500,
            temperature=0.2,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are a senior equity analyst at a top hedge fund with 20 years of experience. '
                        'You have been given comprehensive real-time data including technicals, fundamentals, '
                        'global macro conditions (oil, gold, rates, FX, fear index), sector performance, '
                        'analyst consensus, options sentiment, and recent news. '
                        'Synthesize ALL of this data holistically — macro environment and news must influence '
                        'your recommendation just as much as the stock-specific numbers. '
                        'Respond ONLY with a single valid JSON object. No markdown, no text outside the JSON.'
                    )
                },
                {
                    'role': 'user',
                    'content': (
                        f'{prompt}\n\n'
                        'Respond with exactly this JSON structure:\n'
                        '{\n'
                        '  "recommendation": "BUY" | "HOLD" | "SELL",\n'
                        '  "confidence": <integer 1-10>,\n'
                        '  "summary": "<3-4 sentence executive summary integrating macro + stock factors>",\n'
                        '  "bull_case": ["<reason 1>", "<reason 2>", "<reason 3>"],\n'
                        '  "bear_case": ["<risk 1>", "<risk 2>", "<risk 3>"],\n'
                        '  "macro_impact": "<How current macro environment (oil, rates, dollar, VIX) specifically affects this stock>",\n'
                        '  "news_impact": "<Key takeaway from recent news and its effect on outlook>",\n'
                        '  "technicals": "<Technical analysis summary>",\n'
                        '  "fundamentals": "<Fundamental analysis summary>",\n'
                        '  "price_target_low": <number>,\n'
                        '  "price_target_high": <number>,\n'
                        '  "time_horizon": "Short-term (weeks)" | "Medium-term (3-6 months)" | "Long-term (1+ year)",\n'
                        '  "valuation": "Cheap" | "Fair" | "Expensive",\n'
                        '  "momentum": "Strong" | "Neutral" | "Weak",\n'
                        '  "quality": "High" | "Medium" | "Low",\n'
                        '  "macro_risk": "Low" | "Medium" | "High"\n'
                        '}'
                    )
                }
            ]
        )

        raw = msg.choices[0].message.content.strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'): raw = raw[4:]
        analysis = json.loads(raw)
        return jsonify({'success': True, 'ticker': ticker, 'price': cur, 'analysis': analysis})

    except json.JSONDecodeError as e:
        return jsonify({'success': False, 'error': f'AI response parse error: {e}'}), 500
    except groq_sdk.AuthenticationError:
        return jsonify({'success': False,
                        'error': 'Invalid API key. Check your Groq key in Settings.'}), 401
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Always initialise DB (works under gunicorn too)
init_db()

if __name__ == '__main__':
    print('\n  Portfolio Tracker  —  Local')
    print('  ────────────────────────────')
    print('  Open: http://127.0.0.1:5000\n')
    app.run(debug=False, port=int(os.environ.get('PORT', 5000)), threaded=True)
