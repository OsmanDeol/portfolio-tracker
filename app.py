import os
import sqlite3
import threading
import time
from functools import wraps

import yfinance as yf
from flask import (Flask, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod-!@#xyz')

DB_PATH        = os.environ.get('DB_PATH', 'portfolio.db')
price_cache    = {}
sparkline_cache= {}
cache_lock     = threading.Lock()
PRICE_TTL      = 3
SPARKLINE_TTL  = 300


# ─────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safer for concurrent access
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            email      TEXT    UNIQUE COLLATE NOCASE,
            password   TEXT    NOT NULL,
            created    TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS positions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES users(id),
            ticker         TEXT    NOT NULL,
            shares         REAL    NOT NULL DEFAULT 0,
            avg_buy_price  REAL    NOT NULL DEFAULT 0,
            total_invested REAL    NOT NULL DEFAULT 0,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
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
            user_id         INTEGER NOT NULL REFERENCES users(id),
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
    ''')
    # Safe column migrations for pre-existing DBs
    for sql in [
        "ALTER TABLE transactions ADD COLUMN user_id INTEGER",
        "ALTER TABLE transactions ADD COLUMN commission REAL DEFAULT 0",
        "ALTER TABLE transactions ADD COLUMN trade_date TEXT",
        "ALTER TABLE realized_pnl ADD COLUMN user_id INTEGER",
        "ALTER TABLE realized_pnl ADD COLUMN commission REAL DEFAULT 0",
        "ALTER TABLE realized_pnl ADD COLUMN net_profit_loss REAL",
        "ALTER TABLE realized_pnl ADD COLUMN trade_date TEXT",
        "ALTER TABLE positions ADD COLUMN user_id INTEGER",
    ]:
        try:
            conn.execute(sql); conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()


# ─────────────────────────────────────────────────────────
#  AUTH HELPERS
# ─────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Not authenticated'}), 401
            return redirect(url_for('auth_page'))
        return f(*args, **kwargs)
    return decorated


def current_uid():
    return session['user_id']


# ─────────────────────────────────────────────────────────
#  STOCK DATA (shared cache, not per-user)
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

        pre_p   = info.get('preMarketPrice')
        post_p  = info.get('postMarketPrice')
        pre_cp  = info.get('preMarketChangePercent') or 0
        post_cp = info.get('postMarketChangePercent') or 0
        if pre_cp  and abs(pre_cp)  < 1: pre_cp  *= 100
        if post_cp and abs(post_cp) < 1: post_cp *= 100

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

        data = {
            'ticker': ticker,
            'name': info.get('longName') or info.get('shortName', ticker),
            'price': price, 'prev_close': prev,
            'change': chg, 'change_pct': chgp,
            'pre_price': pre_p, 'pre_chg_pct': pre_cp,
            'post_price': post_p, 'post_chg_pct': post_cp,
            'earnings_date': earnings_date,
            'day_high': info.get('dayHigh', 0),
            'day_low':  info.get('dayLow', 0),
            'volume':   info.get('volume', 0),
            'exchange': info.get('exchange', 'NASDAQ'),
            'success': True,
        }
        with cache_lock:
            price_cache[ticker] = {'data': data, 'ts': time.time()}
        return data
    except Exception as e:
        return {'ticker': ticker, 'success': False, 'error': str(e)}


# ─────────────────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────────────────

@app.route('/auth')
def auth_page():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('auth.html')


@app.route('/register', methods=['POST'])
def register():
    username = (request.json.get('username') or '').strip()
    email    = (request.json.get('email')    or '').strip() or None
    password =  request.json.get('password') or ''

    if not username or len(username) < 3:
        return jsonify({'success': False, 'error': 'Username must be at least 3 characters.'})
    if len(password) < 6:
        return jsonify({'success': False, 'error': 'Password must be at least 6 characters.'})

    hashed = generate_password_hash(password)
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO users (username, email, password) VALUES (?,?,?)',
            (username, email, hashed))
        conn.commit()
        user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        conn.close()
        session.permanent = True
        session['user_id'] = user['id']
        session['username'] = user['username']
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Username or email already taken.'})


@app.route('/login', methods=['POST'])
def login():
    username = (request.json.get('username') or '').strip()
    password =  request.json.get('password') or ''

    conn = get_db()
    user = conn.execute(
        'SELECT * FROM users WHERE username=?', (username,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user['password'], password):
        return jsonify({'success': False, 'error': 'Incorrect username or password.'})

    session.permanent = True
    session['user_id'] = user['id']
    session['username'] = user['username']
    return jsonify({'success': True})


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth_page'))


@app.route('/api/me')
@login_required
def me():
    return jsonify({'user_id': current_uid(), 'username': session['username']})


# ─────────────────────────────────────────────────────────
#  MAIN APP ROUTE
# ─────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html', username=session['username'])


# ─────────────────────────────────────────────────────────
#  STOCK / PRICE ROUTES
# ─────────────────────────────────────────────────────────

@app.route('/api/stock/<ticker>')
@login_required
def get_stock(ticker):
    return jsonify(fetch_stock_data(ticker.upper()))


@app.route('/api/prices')
@login_required
def get_prices():
    uid  = current_uid()
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
@login_required
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
@login_required
def sparklines_batch():
    uid  = current_uid()
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
        except:
            results[t] = {'prices': [], 'success': False}
    for t in tickers:
        th = threading.Thread(target=fetch, args=(t,), daemon=True)
        threads.append(th); th.start()
    for th in threads: th.join(timeout=20)
    return jsonify(results)


# ─────────────────────────────────────────────────────────
#  PORTFOLIO ROUTES  (all scoped to current user)
# ─────────────────────────────────────────────────────────

@app.route('/api/portfolio')
@login_required
def get_portfolio():
    uid  = current_uid()
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
@login_required
def buy():
    uid        = current_uid()
    d          = request.json
    ticker     = d['ticker'].upper()
    shares     = float(d['shares'])
    price      = float(d['price'])
    commission = float(d.get('commission', 0))
    trade_date = d.get('trade_date', '')
    total_cost = shares * price + commission
    eff_avg    = total_cost / shares

    conn     = get_db()
    existing = conn.execute(
        'SELECT * FROM positions WHERE user_id=? AND ticker=?', (uid, ticker)).fetchone()
    if existing:
        ns  = existing['shares']        + shares
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
@login_required
def sell():
    uid        = current_uid()
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


@app.route('/api/portfolio/<ticker>', methods=['DELETE'])
@login_required
def delete_position(ticker):
    uid  = current_uid()
    conn = get_db()
    conn.execute('DELETE FROM positions WHERE user_id=? AND ticker=?', (uid, ticker.upper()))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/realized-pnl')
@login_required
def realized_pnl():
    uid  = current_uid()
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM realized_pnl WHERE user_id=? ORDER BY date DESC LIMIT 200',
        (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/transactions')
@login_required
def transactions():
    uid  = current_uid()
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC LIMIT 200',
        (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────
#  ACCOUNT — change password / delete account
# ─────────────────────────────────────────────────────────

@app.route('/api/account/change-password', methods=['POST'])
@login_required
def change_password():
    uid     = current_uid()
    d       = request.json
    old_pw  = d.get('old_password', '')
    new_pw  = d.get('new_password', '')
    if len(new_pw) < 6:
        return jsonify({'success': False, 'error': 'New password must be at least 6 characters.'})
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user or not check_password_hash(user['password'], old_pw):
        conn.close()
        return jsonify({'success': False, 'error': 'Current password is incorrect.'})
    conn.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash(new_pw), uid))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/account/delete', methods=['POST'])
@login_required
def delete_account():
    uid  = current_uid()
    d    = request.json
    pw   = d.get('password', '')
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user or not check_password_hash(user['password'], pw):
        conn.close()
        return jsonify({'success': False, 'error': 'Incorrect password.'})
    for table in ('positions', 'transactions', 'realized_pnl'):
        conn.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    conn.commit(); conn.close()
    session.clear()
    return jsonify({'success': True})


# Always initialise DB (works under gunicorn / PythonAnywhere too)
init_db()

if __name__ == '__main__':
    print('\n  Portfolio Tracker  —  Multi-user')
    print('  ─────────────────────────────────')
    print('  Open: http://127.0.0.1:5000\n')
    app.run(debug=False, port=int(os.environ.get('PORT', 5000)), threaded=True)
