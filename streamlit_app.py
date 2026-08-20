"""
شاشة أسهم تداول الحية — نسخة سحابية (Streamlit)
================================================
نسخة موازية لتطبيق السوق الأمريكي، بس معدّلة بالكامل للسوق
السعودي (تداول): توقيت الرياض، الريال السعودي، وساعات
تداول الأحد-الخميس.
"""

from datetime import datetime, time as dtime
import json
import base64

import pandas as pd
import requests
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pytz
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from tradingview_ta import TA_Handler, Interval

SA_TZ = pytz.timezone("Asia/Riyadh")
MARKET_OPEN = dtime(10, 0)
OPENING_RANGE_MINUTES = 15
REFRESH_SECONDS = 30

# أسماء الشركات بالعربي لكل رمز
STOCK_NAMES = {
    "2222": "أرامكو السعودية",
    "1120": "مصرف الراجحي",
    "1150": "مصرف الإنماء",
    "1140": "بنك البلاد",
    "1211": "معادن",
    "2010": "سابك",
    "2020": "سابك للمغذيات الزراعية",
    "2290": "ينساب",
    "2330": "المتقدمة",
    "2310": "سبكيم العالمية",
    "7010": "stc",
    "7020": "موبايلي",
    "7030": "زين السعودية",
    "2082": "أكوا باور",
    "2083": "مرافق",
    "2280": "المراعي",
    "6010": "نادك",
    "4190": "جرير",
    "4001": "العثيم",
    "4002": "المواساة",
    "4004": "دله الصحية",
    "4013": "الحبيب الطبية",
    "4164": "النهدي",
    "2283": "المطاحن الأولى",
    "1111": "مجموعة تداول",
}

# قائمة أسهمك (كلها بورصة تداول)
WATCHLIST = list(STOCK_NAMES.keys())
CUSTOM_OPTION = "سهم آخر (اكتبه يدوياً)"

TECHNICAL_LABELS = {
    "STRONG_BUY": ("شراء قوي 🟢🟢", "#0a8a3f"),
    "BUY": ("شراء 🟢", "#22c55e"),
    "NEUTRAL": ("محايد ⚪", "#9e9e9e"),
    "SELL": ("بيع 🔴", "#ef4444"),
    "STRONG_SELL": ("بيع قوي 🔴🔴", "#b91c1c"),
}

st.set_page_config(page_title="شاشة تداول حية", layout="wide")

st_autorefresh(interval=REFRESH_SECONDS * 1000, key="auto_refresh")

st.markdown(
    """
    <style>
    body, .stApp { direction: rtl; text-align: right; font-family: Tahoma, Arial; }
    </style>
    """,
    unsafe_allow_html=True,
)

if "armed" not in st.session_state:
    st.session_state.armed = {}

NTFY_CHANNELS = ["بدون إشعارات (تعطيل)", "safar-tadawul-alerts-6284", "قناة أخرى (اكتبها يدوياً)"]
ntfy_choice = st.selectbox("قناة إشعارات ntfy", NTFY_CHANNELS, index=1)

if ntfy_choice == "قناة أخرى (اكتبها يدوياً)":
    ntfy_topic = st.text_input("اكتب اسم القناة", value="").strip()
elif ntfy_choice == "بدون إشعارات (تعطيل)":
    ntfy_topic = ""
else:
    ntfy_topic = ntfy_choice

watch_all = st.checkbox(
    "راقب كل أسهم القائمة معاً وأرسل تنبيه لأي اختراق (يحتاج قناة ntfy أعلاه)",
    value=False,
)


def fetch_raw_data(sym: str):
    """يجيب آخر 5 أيام تداول كاملة (بيانات دقيقة بدقيقة) من ياهو فاينانس.
    رموز تداول تحتاج لاحقة .SR."""
    ticker = yf.Ticker(f"{sym}.SR")
    data = ticker.history(period="5d", interval="1m")
    if data.empty:
        return None
    data.index = data.index.tz_convert(SA_TZ)
    return data


def available_trading_dates(data: pd.DataFrame):
    if data is None or data.empty:
        return []
    return sorted(set(data.index.date), reverse=True)


def extract_session_for_date(data: pd.DataFrame, target_date):
    day_start = SA_TZ.localize(datetime.combine(target_date, MARKET_OPEN))
    day_end = day_start + pd.Timedelta(hours=6)
    session = data[(data.index >= day_start) & (data.index < day_end)]
    if session.empty:
        return None, None, None

    open_end = day_start + pd.Timedelta(minutes=OPENING_RANGE_MINUTES)
    opening_window = session[session.index < open_end]
    if opening_window.empty:
        opening_window = session.iloc[:15]

    range_high = float(opening_window["High"].max())
    range_low = float(opening_window["Low"].min())
    return session, range_high, range_low


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = pd.DataFrame({
        "Open": df["Open"].resample(rule).first(),
        "High": df["High"].resample(rule).max(),
        "Low": df["Low"].resample(rule).min(),
        "Close": df["Close"].resample(rule).last(),
        "Volume": df["Volume"].resample(rule).sum(),
    }).dropna()
    return out


def fetch_and_prepare(sym: str):
    data = fetch_raw_data(sym)
    if data is None:
        return None, None, None, False

    today_sa = datetime.now(SA_TZ).date()
    session, range_high, range_low = extract_session_for_date(data, today_sa)
    is_last_trading_day = False

    if session is None:
        dates = available_trading_dates(data)
        if not dates:
            return None, None, None, False
        session, range_high, range_low = extract_session_for_date(data, dates[0])
        is_last_trading_day = True
        if session is None:
            return None, None, None, False

    return session, range_high, range_low, is_last_trading_day


@st.cache_data(ttl=120)
def get_technical_outlook(sym: str):
    """تحليل فني من تريدنج فيو لسوق تداول. لو فشل (رمز غير مدعوم
    بمكتبة التحليل)، يرجع None ويُعرض 'غير متاح' بدون أي كسر بالتطبيق."""
    try:
        handler = TA_Handler(
            symbol=sym,
            screener="saudiarabia",
            exchange="TADAWUL",
            interval=Interval.INTERVAL_15_MINUTES,
        )
        summary = handler.get_analysis().summary
        return {
            "توصية": summary.get("RECOMMENDATION", "غير متاح"),
            "شراء": summary.get("BUY", 0),
            "بيع": summary.get("SELL", 0),
            "محايد": summary.get("NEUTRAL", 0),
        }
    except Exception:
        return None


watch_data = {}
if watch_all:
    for wsym in WATCHLIST:
        w_session, w_high, w_low, _ = fetch_and_prepare(wsym)
        if w_session is None:
            continue
        w_open = float(w_session["Open"].iloc[0])
        w_last = float(w_session["Close"].iloc[-1])
        watch_data[wsym] = {
            "session": w_session, "high": w_high, "low": w_low,
            "open": w_open, "last": w_last,
        }

if "selected_symbol" not in st.session_state:
    st.session_state.selected_symbol = "2222"

if watch_all:
    st.markdown("###### 🎨 لوحة الأسهم (اضغط على أي سهم لعرضه)")
    board_cols = st.columns(4)
    style_blocks = []
    TA_ICON = {
        "STRONG_BUY": "⬆️⬆️", "BUY": "⬆️", "NEUTRAL": "➡️",
        "SELL": "⬇️", "STRONG_SELL": "⬇️⬇️",
    }
    for i, wsym in enumerate(WATCHLIST):
        with board_cols[i % 4]:
            tile_key = f"tile_{wsym}"
            ta = get_technical_outlook(wsym)
            ta_icon = TA_ICON.get(ta["توصية"], "") if ta else ""
            ta_counts = f"🟢{ta['شراء']} 🔴{ta['بيع']}" if ta else ""
            name = STOCK_NAMES.get(wsym, wsym)
            if wsym in watch_data:
                d = watch_data[wsym]
                chg_pct = ((d["last"] - d["open"]) / d["open"]) * 100 if d["open"] else 0
                bg = "#0a8a3f" if chg_pct >= 0 else "#d0332f"
                label = f"{name} ({wsym}) {ta_icon}\n{d['last']:.2f} ر.س  {chg_pct:+.1f}%\n{ta_counts}"
            else:
                bg = "#9e9e9e"
                label = f"{name} ({wsym}) {ta_icon}\n—\n{ta_counts}"

            with st.container(key=tile_key):
                if st.button(label, key=f"btn_{wsym}", use_container_width=True):
                    st.session_state.selected_symbol = wsym

            style_blocks.append(
                f".st-key-{tile_key} button {{background-color:{bg} !important; "
                f"color:white !important; border:none !important; white-space:pre-line;}}"
            )

    st.markdown(f"<style>{''.join(style_blocks)}</style>", unsafe_allow_html=True)
    st.caption(
        "⬆️ = التحليل الفني يميل للشراء — ⬇️ = يميل للبيع — ➡️ = محايد\n\n"
        "🟢/🔴 = عدد المؤشرات الفنية المؤيدة للشراء/البيع (من تريدنج فيو)"
    )

    custom_typed = st.text_input("أو اكتب رمز سهم آخر (رقم فقط)", value="")
    if custom_typed.strip():
        st.session_state.selected_symbol = custom_typed.strip()

    symbol = st.session_state.selected_symbol
else:
    col1, col2 = st.columns([2, 2])
    with col1:
        display_options = [f"{s} - {STOCK_NAMES[s]}" for s in WATCHLIST] + [CUSTOM_OPTION]
        current = st.session_state.selected_symbol
        default_label = f"{current} - {STOCK_NAMES[current]}" if current in STOCK_NAMES else CUSTOM_OPTION
        default_index = display_options.index(default_label) if default_label in display_options else 0
        choice = st.selectbox("اختر من قائمتك", display_options, index=default_index)
    with col2:
        if choice == CUSTOM_OPTION:
            typed = st.text_input("أو اكتب رمز سهم آخر (رقم فقط)", value="")
            symbol = typed.strip() or "2222"
        else:
            symbol = choice.split(" - ")[0]
    st.session_state.selected_symbol = symbol


def send_ntfy_alert(topic: str, title: str, message: str, click_url: str = "") -> bool:
    try:
        headers = {"Title": title.encode("utf-8")}
        if click_url:
            headers["Click"] = click_url
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=5,
        )
        return True
    except Exception:
        return False


def check_breakout_and_notify(sym: str, last: float, entry: float, stop: float, target: float, topic: str):
    """يرسل تنبيهاً مرة واحدة بالضبط عند لحظة الاختراق، ولا يكرره
    إلا بعد ما يرجع السعر تحت مستوى الدخول ثم يخترق من جديد."""
    name = STOCK_NAMES.get(sym, sym)
    was_armed = st.session_state.armed.get(sym, True)
    if last < entry:
        st.session_state.armed[sym] = True
        return False
    if last >= entry and was_armed:
        msg = (
            f"السعر الحالي: {last:.2f} ر.س\n"
            f"وقف الخسارة المقترح: {stop:.2f} ر.س\n"
            f"الهدف المقترح: {target:.2f} ر.س\n"
            f"⚠️ هذا تنبيه للمراجعة فقط، القرار بيدك"
        )
        if send_ntfy_alert(topic, f"اختراق صاعد ⬆️ {name} ({sym})", msg):
            st.session_state.armed[sym] = False
            st.session_state.trade_log.insert(0, {
                "الوقت": datetime.now(SA_TZ).strftime("%H:%M:%S"),
                "السهم": f"{name} ({sym})",
                "النوع": "اختراق صاعد ⬆️",
                "السعر": f"{last:.2f} ر.س",
            })
            return True
    return False


def check_stop_proximity_and_notify(sym: str, last: float, entry: float, stop: float, topic: str, key_id: str = None):
    name = STOCK_NAMES.get(sym, sym)
    key_id = key_id or sym
    halfway = entry - (entry - stop) / 2
    key = f"stopwarn_{key_id}"
    already_warned = st.session_state.armed.get(key, False)
    if last > halfway:
        st.session_state.armed[key] = False
        return False
    if last <= halfway and not already_warned:
        msg = f"السعر الحالي: {last:.2f} ر.س — اقترب من وقف الخسارة ({stop:.2f} ر.س)"
        if send_ntfy_alert(topic, f"⚠️ اقتراب من وقف الخسارة {name} ({sym})", msg):
            st.session_state.armed[key] = True
            st.session_state.trade_log.insert(0, {
                "الوقت": datetime.now(SA_TZ).strftime("%H:%M:%S"),
                "السهم": f"{name} ({sym})",
                "النوع": "⚠️ اقتراب من الوقف",
                "السعر": f"{last:.2f} ر.س",
            })
            return True
    return False


def check_target_proximity_and_notify(trade_id: str, sym: str, last: float, entry: float, target: float, topic: str):
    name = STOCK_NAMES.get(sym, sym)
    halfway = entry + (target - entry) / 2
    key = f"targetwarn_{trade_id}"
    already_warned = st.session_state.armed.get(key, False)
    if last < halfway:
        st.session_state.armed[key] = False
        return False
    if last >= halfway and not already_warned:
        msg = f"السعر الحالي: {last:.2f} ر.س — اقترب من الهدف ({target:.2f} ر.س)"
        if send_ntfy_alert(topic, f"🎯 اقتراب من الهدف {name} ({sym})", msg):
            st.session_state.armed[key] = True
            st.session_state.trade_log.insert(0, {
                "الوقت": datetime.now(SA_TZ).strftime("%H:%M:%S"),
                "السهم": f"{name} ({sym})", "النوع": "🎯 اقتراب من الهدف", "السعر": f"{last:.2f} ر.س",
            })
            return True
    return False


def check_candle_alert(trade_id: str, sym: str, candle_df: pd.DataFrame, topic: str):
    name = STOCK_NAMES.get(sym, sym)
    if candle_df is None or candle_df.empty:
        return
    last_row = candle_df.iloc[-1]
    last_ts = candle_df.index[-1]
    key = f"candle_ts_{trade_id}"
    prev_ts = st.session_state.armed.get(key)

    if prev_ts is None:
        st.session_state.armed[key] = last_ts
        return

    if last_ts != prev_ts:
        if prev_ts in candle_df.index:
            prev_row = candle_df.loc[prev_ts]
            msg_close = (
                f"إغلاق الشمعة — فتح: {prev_row['Open']:.2f} ر.س / إغلاق: {prev_row['Close']:.2f} ر.س / "
                f"أعلى: {prev_row['High']:.2f} ر.س / أدنى: {prev_row['Low']:.2f} ر.س"
            )
            send_ntfy_alert(topic, f"🕯️ إغلاق شمعة {name} ({sym})", msg_close)

        msg_open = f"بدأت شمعة جديدة — سعر الافتتاح: {last_row['Open']:.2f} ر.س"
        send_ntfy_alert(topic, f"🕯️ افتتاح شمعة {name} ({sym})", msg_open)

        st.session_state.trade_log.insert(0, {
            "الوقت": datetime.now(SA_TZ).strftime("%H:%M:%S"),
            "السهم": f"{name} ({sym})", "النوع": "🕯️ شمعة جديدة", "السعر": f"{last_row['Open']:.2f} ر.س",
        })
        st.session_state.armed[key] = last_ts


if "trade_log" not in st.session_state:
    st.session_state.trade_log = []

# صفقاتي المفتوحة — تُحفظ بشكل دائم عبر GitHub
GITHUB_REPO = "abojumanh/saudi-live-chart"
TRADES_FILE_PATH = "trades.json"


def load_trades_from_github():
    try:
        token = st.secrets["github_token_sa"]
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{TRADES_FILE_PATH}"
        headers = {"Authorization": f"token {token}"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            content = base64.b64decode(resp.json()["content"]).decode("utf-8")
            data = json.loads(content)
            return data.get("trades", []), data.get("counter", 0)
        return [], 0
    except Exception:
        return [], 0


def save_trades_to_github(trades, counter):
    try:
        token = st.secrets["github_token_sa"]
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{TRADES_FILE_PATH}"
        headers = {"Authorization": f"token {token}"}
        get_resp = requests.get(url, headers=headers, timeout=10)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None
        content_str = json.dumps({"trades": trades, "counter": counter}, ensure_ascii=False)
        content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
        payload = {"message": "تحديث الصفقات", "content": content_b64}
        if sha:
            payload["sha"] = sha
        put_resp = requests.put(url, headers=headers, json=payload, timeout=10)
        if put_resp.status_code not in (200, 201):
            st.error(f"⚠️ فشل الحفظ على GitHub — كود: {put_resp.status_code} — {put_resp.text[:300]}")
    except Exception as e:
        st.error(f"⚠️ خطأ أثناء الحفظ: {e}")


if "my_trades" not in st.session_state:
    loaded_trades, loaded_counter = load_trades_from_github()
    st.session_state.my_trades = loaded_trades
    st.session_state.trade_id_counter = loaded_counter

st.markdown("### 💼 صفقاتي المفتوحة")
with st.expander("➕ سجّل صفقة جديدة"):
    trade_options = WATCHLIST
    trade_labels = [f"{s} - {STOCK_NAMES[s]}" for s in trade_options]
    trade_choice_label = st.selectbox("السهم", trade_labels, key="new_trade_symbol_select")
    trade_symbol = trade_choice_label.split(" - ")[0]

    tc1, tc2 = st.columns(2)
    with tc1:
        trade_entry = st.number_input("سعر الدخول (ر.س)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        trade_qty = st.number_input("عدد الأسهم", min_value=0, value=0, step=1)
    with tc2:
        trade_stop = st.number_input("وقف الخسارة (ر.س) — اتركه 0 للحساب التلقائي", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        trade_target = st.number_input("الهدف (ر.س) — اتركه 0 للحساب التلقائي", min_value=0.0, value=0.0, step=0.01, format="%.2f")

    trade_candle_alerts = st.checkbox("🕯️ فعّل تنبيه فتح/إغلاق الشمعة لهذه الصفقة", value=False)

    if st.button("إضافة الصفقة"):
        if trade_entry > 0 and trade_qty > 0:
            st.session_state.trade_id_counter += 1
            st.session_state.my_trades.append({
                "id": st.session_state.trade_id_counter,
                "symbol": trade_symbol,
                "entry": trade_entry,
                "qty": trade_qty,
                "stop": trade_stop if trade_stop > 0 else None,
                "target": trade_target if trade_target > 0 else None,
                "candle_alerts": trade_candle_alerts,
            })
            save_trades_to_github(st.session_state.my_trades, st.session_state.trade_id_counter)
            st.success(f"✅ تمت إضافة صفقة {trade_symbol}")

if not st.session_state.my_trades:
    st.caption("لا توجد صفقات مسجّلة بعد. اضغط أعلاه لإضافة صفقتك الأولى.")

trades_to_remove = []
for trade in st.session_state.my_trades:
    t_session, t_high, t_low, _ = fetch_and_prepare(trade["symbol"])
    if t_session is None:
        continue
    t_last = float(t_session["Close"].iloc[-1])
    t_stop = trade["stop"] if trade["stop"] else (t_high + t_low) / 2 if t_high else trade["entry"] * 0.98
    t_target = trade["target"] if trade["target"] else trade["entry"] + 2 * (trade["entry"] - t_stop)

    pnl = (t_last - trade["entry"]) * trade["qty"]
    pnl_pct = ((t_last - trade["entry"]) / trade["entry"]) * 100 if trade["entry"] else 0
    pnl_color = "#0a8a3f" if pnl >= 0 else "#d0332f"
    name = STOCK_NAMES.get(trade["symbol"], trade["symbol"])

    tc1, tc2 = st.columns([5, 1])
    with tc1:
        st.markdown(
            f"""
            <div style="border:1px solid #ddd; border-radius:8px; padding:10px; margin-bottom:6px;">
                <b>{name} ({trade['symbol']})</b> — دخول: {trade['entry']:.2f} ر.س × {trade['qty']:.0f} سهم
                &nbsp;|&nbsp; السعر الحالي: {t_last:.2f} ر.س
                &nbsp;|&nbsp; الوقف: {t_stop:.2f} ر.س &nbsp;|&nbsp; الهدف: {t_target:.2f} ر.س<br>
                <span style="color:{pnl_color}; font-weight:bold;">
                    {'+' if pnl >= 0 else ''}{pnl:.2f} ر.س ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)
                </span>
                {' — 🕯️ تنبيه الشمعة مفعّل' if trade['candle_alerts'] else ''}
            </div>
            """,
            unsafe_allow_html=True,
        )
    with tc2:
        if st.button("🗑️ بعتها", key=f"remove_trade_{trade['id']}"):
            trades_to_remove.append(trade["id"])

    if ntfy_topic:
        check_target_proximity_and_notify(str(trade["id"]), trade["symbol"], t_last, trade["entry"], t_target, ntfy_topic)
        check_stop_proximity_and_notify(trade["symbol"], t_last, trade["entry"], t_stop, ntfy_topic, key_id=str(trade["id"]))

if trades_to_remove:
    st.session_state.my_trades = [t for t in st.session_state.my_trades if t["id"] not in trades_to_remove]
    save_trades_to_github(st.session_state.my_trades, st.session_state.trade_id_counter)
    st.rerun()

raw_data = fetch_raw_data(symbol)

if raw_data is None:
    st.warning("لا توجد بيانات متاحة لهذا السهم. تحقق من صحة الرمز.")
    st.stop()

trading_dates = available_trading_dates(raw_data)
today_sa = datetime.now(SA_TZ).date()

date_labels = []
for d in trading_dates:
    label = d.strftime("%Y-%m-%d")
    if d == today_sa:
        label += " (اليوم)"
    date_labels.append(label)

selected_label = st.selectbox("يوم التداول المعروض للتحليل", date_labels, index=0)
selected_date = trading_dates[date_labels.index(selected_label)]

session, range_high, range_low = extract_session_for_date(raw_data, selected_date)
is_last_trading_day = selected_date != today_sa

if session is None:
    st.warning("لا توجد بيانات لهذا اليوم تحديداً. جرّب يوماً آخر من القائمة.")
    st.stop()

if is_last_trading_day:
    st.info(
        f"📅 تعرض حالياً شارت يوم **{selected_date.strftime('%Y-%m-%d')}** للمراجعة والتحليل. "
        "المستويات المحسوبة (دخول/وقف/هدف) خاصة بذلك اليوم فقط."
    )

stop_loss = (range_high + range_low) / 2
entry_price = range_high
target_price = entry_price + 2 * (entry_price - stop_loss)
open_price = float(session["Open"].iloc[0])
last_price = float(session["Close"].iloc[-1])
day_high = float(session["High"].max())
day_low = float(session["Low"].min())
change = last_price - open_price
change_pct = (change / open_price) * 100 if open_price else 0
last_update = datetime.now(SA_TZ).strftime("%H:%M:%S")
company_name = STOCK_NAMES.get(symbol, symbol)

if change >= 0:
    price_color = "#0a8a3f"
    arrow = "▲"
    sign = "+"
else:
    price_color = "#d0332f"
    arrow = "▼"
    sign = ""

if ntfy_topic and not watch_all:
    if check_breakout_and_notify(symbol, last_price, entry_price, stop_loss, target_price, ntfy_topic):
        st.success(f"✅ تم إرسال إشعار الاختراق لجوالك عبر قناة {ntfy_topic}")
    check_stop_proximity_and_notify(symbol, last_price, entry_price, stop_loss, ntfy_topic)

st.markdown(
    f"<h2>شاشة {company_name} ({symbol}) الحية — تتحدث كل {REFRESH_SECONDS} ثانية تلقائياً</h2>",
    unsafe_allow_html=True,
)
st.markdown(
    f"""
    <div style="font-size:64px; font-weight:bold; color:{price_color}; line-height:1.1;">
        {last_price:.2f} ر.س <span style="font-size:36px;">{arrow}</span>
    </div>
    <div style="font-size:20px; color:{price_color}; margin-top:4px;">
        {sign}{change:.2f} ({sign}{change_pct:.2f}%)
    </div>
    <div style="font-size:15px; color:gray; margin-top:6px;">آخر تحديث: {last_update} (بتوقيت الرياض)</div>
    """,
    unsafe_allow_html=True,
)

outlook = get_technical_outlook(symbol)
if outlook:
    label, badge_color = TECHNICAL_LABELS.get(outlook["توصية"], (outlook["توصية"], "#9e9e9e"))
    st.markdown(
        f"""
        <div style="background:{badge_color}; color:white; border-radius:10px;
                    padding:10px 16px; margin-top:8px; display:inline-block;">
            <b>📊 التحليل الفني: {label}</b><br>
            <span style="font-size:13px;">
                مؤيدون للشراء: {outlook['شراء']} — مؤيدون للبيع: {outlook['بيع']} — محايد: {outlook['محايد']}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("💡 تحليل فني تلقائي من تريدنج فيو، يتحدث كل دقيقتين تقريباً — للمراجعة فقط، وليس توصية استثمارية")
else:
    st.caption("📊 التحليل الفني غير متاح حالياً لهذا السهم")

risk_amount = entry_price - stop_loss
reward_amount = target_price - entry_price
rr_ratio = reward_amount / risk_amount if risk_amount > 0 else 0
st.info(f"⚖️ نسبة المخاطرة إلى العائد: **1 : {rr_ratio:.1f}** — (مخاطرة {risk_amount:.2f} ر.س مقابل عائد محتمل {reward_amount:.2f} ر.س)")

st.markdown("---")
st.markdown("##### 📊 مستويات اليوم")
lv1, lv2, lv3, lv4 = st.columns(4)
lv1.metric("أعلى سعر اليوم", f"{day_high:.2f} ر.س")
lv2.metric("أدنى سعر اليوم", f"{day_low:.2f} ر.س")
lv3.metric("نطاق الافتتاح (أعلى)", f"{range_high:.2f} ر.س")
lv4.metric("نطاق الافتتاح (أدنى)", f"{range_low:.2f} ر.س")

st.markdown("##### 🕯️ الشارت")
candle_interval = st.radio("حجم الشمعة", ["1 دقيقة", "5 دقائق", "15 دقيقة"], horizontal=True, index=0)
rule_map = {"1 دقيقة": "1min", "5 دقائق": "5min", "15 دقيقة": "15min"}
chart_df = resample_ohlc(session, rule_map[candle_interval]) if candle_interval != "1 دقيقة" else session

fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)
fig.add_trace(go.Candlestick(
    x=chart_df.index, open=chart_df["Open"], high=chart_df["High"],
    low=chart_df["Low"], close=chart_df["Close"], name=symbol,
), row=1, col=1)
fig.add_hline(y=entry_price, line_dash="dash", line_color="blue", annotation_text="دخول", row=1, col=1)
fig.add_hline(y=stop_loss, line_dash="dash", line_color="red", annotation_text="وقف", row=1, col=1)
fig.add_hline(y=target_price, line_dash="dash", line_color="green", annotation_text="هدف", row=1, col=1)
fig.add_trace(go.Bar(x=chart_df.index, y=chart_df["Volume"], name="حجم التداول"), row=2, col=1)
fig.update_layout(height=550, xaxis_rangeslider_visible=False, showlegend=False, margin=dict(l=10, r=10, t=10, b=10))
st.plotly_chart(fig, use_container_width=True)

if ntfy_topic:
    for trade in st.session_state.my_trades:
        if trade["candle_alerts"] and trade["symbol"] == symbol:
            check_candle_alert(str(trade["id"]), symbol, chart_df, ntfy_topic)

if st.session_state.trade_log:
    st.markdown("##### 🔔 سجل التنبيهات (هذه الجلسة)")
    st.dataframe(pd.DataFrame(st.session_state.trade_log), use_container_width=True, hide_index=True)
