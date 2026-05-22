import os
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# Support PyInstaller frozen builds — find templates next to the exe
if getattr(sys, 'frozen', False):
    _tmpl = os.path.join(sys._MEIPASS, 'templates')
    app   = Flask(__name__, template_folder=_tmpl)
else:
    app   = Flask(__name__)

CORS(app)

# ── Single local user — no login needed ──────────────────
LOCAL_USER_ID  = 1

DB_PATH        = os.environ.get('DB_PATH', 'portfolio.db')
price_cache    = {}
sparkline_cache= {}
cache_lock     = threading.Lock()
PRICE_TTL      = 3
SPARKLINE_TTL  = 300
ET             = ZoneInfo('America/New_York')


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


def init_db():
    conn = get_db()
    conn.executescript('''
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
    # Ensure all existing rows belong to local user 1
    for table in ('positions', 'transactions', 'realized_pnl'):
        try:
            conn.execute(f'UPDATE {table} SET user_id=1 WHERE user_id IS NULL')
            conn.commit()
        except sqlite3.OperationalError:
            pass
    # Seed 5 default watchlists if none exist
    count = conn.execute('SELECT COUNT(*) FROM watchlists WHERE user_id=1').fetchone()[0]
    if count == 0:
        for i in range(1, 6):
            conn.execute('INSERT INTO watchlists (user_id,name,position) VALUES (?,?,?)',
                         (1, f'Watchlist {i}', i))
        conn.commit()
    conn.close()


def recalculate_position(conn, uid, ticker):
    """Replay all transactions to recompute position for a ticker (average cost method)."""
    txns = conn.execute(
        '''SELECT type, shares, total FROM transactions
           WHERE user_id=? AND ticker=?
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
        conn.execute('DELETE FROM positions WHERE user_id=? AND ticker=?', (uid, ticker))
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
    uid  = LOCAL_USER_ID
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
    uid  = LOCAL_USER_ID
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
    uid  = LOCAL_USER_ID
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
    uid        = LOCAL_USER_ID
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
def sell():
    uid        = LOCAL_USER_ID
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
    uid    = LOCAL_USER_ID
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
    uid  = LOCAL_USER_ID
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
    uid = LOCAL_USER_ID
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
    uid  = LOCAL_USER_ID
    conn = get_db()
    txn  = conn.execute('SELECT * FROM transactions WHERE id=? AND user_id=?',
                        (txn_id, uid)).fetchone()
    if not txn:
        conn.close()
        return jsonify({'success': False, 'error': 'Transaction not found.'}), 404
    ticker   = txn['ticker']
    txn_type = txn['type']
    conn.execute('DELETE FROM transactions WHERE id=? AND user_id=?', (txn_id, uid))
    conn.commit()
    # If it was a sell, also remove the matching realized P&L record
    if txn_type == 'sell':
        conn.execute('''
            DELETE FROM realized_pnl WHERE rowid = (
                SELECT rowid FROM realized_pnl
                WHERE user_id=? AND ticker=?
                  AND ABS(shares - ?) < 0.0001
                  AND ABS(sell_price - ?) < 0.01
                ORDER BY id DESC LIMIT 1
            )
        ''', (uid, ticker, txn['shares'], txn['price']))
        conn.commit()
    # Recalculate position from remaining transactions
    recalculate_position(conn, uid, ticker)
    conn.close()
    return jsonify({'success': True, 'recalculated': True})


@app.route('/api/market-status')
def api_market_status():
    sess = market_session()
    return jsonify(sess)


@app.route('/api/realized-pnl')
def get_realized_pnl():
    uid  = LOCAL_USER_ID
    conn = get_db()
    rows = conn.execute('''
        SELECT *,
               COALESCE(net_profit_loss, profit_loss)       AS net_profit_loss,
               COALESCE(commission, 0)                       AS commission,
               COALESCE(pnl_pct, (sell_price - buy_price)
                        / NULLIF(buy_price,0) * 100)         AS pnl_pct
        FROM realized_pnl
        WHERE user_id=?
        ORDER BY date DESC LIMIT 200
    ''', (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/transactions')
def get_transactions():
    uid  = LOCAL_USER_ID
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC LIMIT 200',
        (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────
#  WATCHLIST ROUTES
# ─────────────────────────────────────────────────────────

@app.route('/api/watchlists')
def get_watchlists():
    uid  = LOCAL_USER_ID
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
    uid  = LOCAL_USER_ID
    name = (request.json.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name required.'}), 400
    conn = get_db()
    conn.execute('UPDATE watchlists SET name=? WHERE id=? AND user_id=?', (name, wl_id, uid))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/watchlists/<int:wl_id>/items', methods=['POST'])
def add_watchlist_item(wl_id):
    uid    = LOCAL_USER_ID
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
    uid  = LOCAL_USER_ID
    conn = get_db()
    conn.execute('DELETE FROM watchlist_items WHERE watchlist_id=? AND ticker=?',
                 (wl_id, ticker.upper()))
    conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/api/watchlists/<int:wl_id>/prices')
def watchlist_prices(wl_id):
    uid  = LOCAL_USER_ID
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


# Always initialise DB (works under gunicorn too)
init_db()

if __name__ == '__main__':
    print('\n  Portfolio Tracker  —  Local')
    print('  ────────────────────────────')
    print('  Open: http://127.0.0.1:5000\n')
    app.run(debug=False, port=int(os.environ.get('PORT', 5000)), threaded=True)
