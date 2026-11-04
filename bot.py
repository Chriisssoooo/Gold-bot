"""Bot Telegram - signaux Gold (XAU/USD). Heure d'Abidjan = UTC."""
import os, sys, csv, time, datetime as dt
import requests

# ===== REGLAGES (modifiables) =====
SYMBOL = "XAU/USD"
FAST, SLOW = 9, 21          # moyennes mobiles sur bougies de 5 min
RSI_N = 14                  # periode du RSI
TP = 3.5                    # gain vise en $ (0,01 lot ~ 2000 FCFA)
SL = 1.75                   # perte max / trailing stop en $
DUREE_MIN = 25              # duree max d'un trade
AVANCE_MIN = 2              # le signal part 2 min avant
NEWS_MARGE_MIN = 30         # pas de trade 30 min avant/apres une grosse annonce USD
SPREAD = 0.3                # cout d'entree estime en $
FCFA_PAR_USD = 570          # taux approximatif
LIMITE_JOUR_FCFA = -3000    # le bot s'arrete si la perte du jour l'atteint
POLL_SEC = 30               # verification du prix toutes les 30 s
JOURNAL = "journal.csv"
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def now():
    return dt.datetime.now(dt.timezone.utc)


def send(txt):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage"
    try:
        requests.post(url, data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": txt}, timeout=20)
    except Exception as e:
        print("Telegram erreur:", e)


def get(url, params, essais=3):
    params["apikey"] = os.environ["TWELVE_KEY"]
    for _ in range(essais):
        try:
            r = requests.get(url, params=params, timeout=20).json()
            if r.get("status") != "error":
                return r
        except Exception:
            pass
        time.sleep(3)
    return None


def price():
    r = get("https://api.twelvedata.com/price", {"symbol": SYMBOL})
    try:
        return float(r["price"])
    except Exception:
        return None


def rsi(c, n=RSI_N):
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def signal():
    """BUY / SELL / NONE (les indicateurs ne sont pas d'accord) / None (pas de donnees)."""
    r = get("https://api.twelvedata.com/time_series",
            {"symbol": SYMBOL, "interval": "5min", "outputsize": 60, "timezone": "UTC"})
    try:
        c = [float(v["close"]) for v in reversed(r["values"])]
    except Exception:
        return None
    sma = lambda n: sum(c[-n:]) / n
    rs = rsi(c)
    if sma(FAST) > sma(SLOW) and 50 < rs < 70:
        return "BUY"
    if sma(FAST) < sma(SLOW) and 30 < rs < 50:
        return "SELL"
    return "NONE"


def news_bloquante(debut, fin):
    """True si une grosse annonce USD tombe pres du trade [debut, fin]."""
    try:
        evs = requests.get(NEWS_URL, timeout=20).json()
    except Exception as e:
        print("Calendrier indisponible:", e)
        return False
    marge = dt.timedelta(minutes=NEWS_MARGE_MIN)
    for e in evs:
        if e.get("country") != "USD" or e.get("impact") != "High":
            continue
        try:
            d = dt.datetime.fromisoformat(e["date"])
        except Exception:
            continue
        if d.tzinfo is None:   # heure de New York si pas de fuseau
            d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=-5)))
        if debut - marge < d < fin + marge:
            return True
    return False


def wait_until(t):
    while True:
        s = (t - now()).total_seconds()
        if s <= 0:
            return
        time.sleep(max(0.5, min(5, s)))


def ecrire(row):
    new = not os.path.exists(JOURNAL)
    with open(JOURNAL, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "heure", "sens", "entree", "sortie", "raison", "pnl_usd", "pnl_fcfa"])
        w.writerow(row)


def jour():
    today = now().strftime("%Y-%m-%d")
    if not os.path.exists(JOURNAL):
        return 0.0, []
    with open(JOURNAL, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["date"] == today]
    return sum(float(r["pnl_fcfa"]) for r in rows), rows


def trade():
    n = now()
    h = n.hour + (1 if n.minute >= 30 else 0)     # heure du trade (hh:10)
    entree_t = n.replace(hour=h, minute=10, second=0, microsecond=0)
    fin = entree_t + dt.timedelta(minutes=DUREE_MIN)
    if n > entree_t:
        send(f"⚠️ Signal {h:02d}:10 rate (retard GitHub)")
        return
    net, _ = jour()
    if net <= LIMITE_JOUR_FCFA:
        return

    wait_until(entree_t - dt.timedelta(minutes=AVANCE_MIN))
    if news_bloquante(entree_t, fin):
        send(f"⏸️ PAS DE TRADE a {h:02d}:10 (news USD)")
        return
    sens = signal()
    if sens is None:
        send("⚠️ Pas de donnees, pas de trade")
        return
    if sens == "NONE":
        send(f"⏸️ PAS DE TRADE a {h:02d}:10")
        return
    send(("🟢 ACHETE" if sens == "BUY" else "🔴 VENDS") + f" a {h:02d}:10")

    wait_until(entree_t)
    entree = price()
    if entree is None:
        send("⚠️ Prix indisponible, ignore ce trade")
        return
    d = 1 if sens == "BUY" else -1
    best, p, raison = 0.0, entree, "duree"
    while now() < fin:
        time.sleep(POLL_SEC)
        x = price()
        if x is None:
            continue
        p = x
        gain = (p - entree) * d
        best = max(best, gain)
        if gain >= TP:
            raison = "TP"
            break
        if gain <= best - SL:
            raison = "SL"
            break

    send("⚠️ FERME maintenant")
    pnl = (p - entree) * d - SPREAD
    ecrire([n.strftime("%Y-%m-%d"), f"{h:02d}:10", sens, round(entree, 2), round(p, 2),
            raison, round(pnl, 2), round(pnl * FCFA_PAR_USD)])
    net, _ = jour()
    if net <= LIMITE_JOUR_FCFA:
        send("🛑 STOP pour aujourd'hui")


def bilan():
    net, rows = jour()
    today = now().strftime("%d/%m/%Y")
    if not rows:
        send(f"📊 Bilan {today} : aucun trade")
        return
    g = sum(1 for r in rows if float(r["pnl_fcfa"]) > 0)
    send(f"📊 Bilan {today}\n{len(rows)} trades | {g} gagnants | {len(rows) - g} perdants\nNet : {net:+.0f} FCFA")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "trade"
    if cmd == "trade":
        trade()
    elif cmd == "bilan":
        bilan()
    else:
        t = now()
        nb = news_bloquante(t, t + dt.timedelta(minutes=DUREE_MIN))
        send(f"✅ Bot OK | or : {price()} $ | signal : {signal()} | news USD proche : {'oui' if nb else 'non'}")
  
