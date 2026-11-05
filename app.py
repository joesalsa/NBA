import os
import re
import html
import logging
import requests
import libsql
import uvicorn
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("value-bot")

# ==========================================
# CONFIGURACIÓN
# ==========================================
TURSO_URL     = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN")
INFER_API_KEY = os.environ.get("INFER_API_KEY")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

REQUIRED = {
    "TURSO_DATABASE_URL": TURSO_URL, "TURSO_AUTH_TOKEN": TURSO_TOKEN,
    "INFER_API_KEY": INFER_API_KEY, "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
}

session = requests.Session()
session.headers.update({"User-Agent": "value-bot/2.0"})
scheduler = BackgroundScheduler(timezone="UTC")


# ==========================================
# BASE DE DATOS
# ==========================================
@contextmanager
def db():
    con = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS pregame_lines (
                event_id TEXT PRIMARY KEY,
                matchup TEXT,
                line REAL,
                kickoff_time TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS alertas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                matchup TEXT,
                regla INTEGER,
                checkpoint TEXT,
                pregame_line REAL,
                proyeccion REAL,
                target_line REAL,
                decision TEXT,
                live_line_at_alert REAL,
                cuota REAL,
                status TEXT DEFAULT 'PENDING',
                final_total INTEGER,
                created_at TEXT,
                UNIQUE(event_id, regla)
            );
            CREATE INDEX IF NOT EXISTS idx_pregame_matchup ON pregame_lines(matchup, updated_at DESC);
        """)


# ==========================================
# TRADUCTOR DE EQUIPOS
# ==========================================
def get_team_key(name):
    n = name.lower()
    if "hawks" in n: return "atlanta"
    if "celtics" in n: return "boston"
    if "nets" in n: return "brooklyn"
    if "hornets" in n: return "charlotte"
    if "bulls" in n: return "chicago"
    if "cavaliers" in n or "cavs" in n: return "cleveland"
    if "mavericks" in n or "mavs" in n: return "dallas"
    if "nuggets" in n: return "denver"
    if "pistons" in n: return "detroit"
    if "warriors" in n or "warrior" in n or "gsw" in n: return "goldenstate"
    if "rockets" in n: return "houston"
    if "pacers" in n: return "indiana"
    if "clippers" in n or "lac" in n: return "clippers"
    if "lakers" in n or "lal" in n: return "lakers"
    if "grizzlies" in n: return "memphis"
    if "heat" in n: return "miami"
    if "bucks" in n: return "milwaukee"
    if "timberwolves" in n or "wolves" in n or "min" in n: return "minnesota"
    if "pelicans" in n or "nop" in n: return "neworleans"
    if "knicks" in n or "nyk" in n: return "newyork"
    if "thunder" in n or "okc" in n: return "oklahomacity"
    if "magic" in n or "orl" in n: return "orlando"
    if "76ers" in n or "sixers" in n or "phi" in n: return "philadelphia"
    if "suns" in n or "pho" in n: return "phoenix"
    if "blazers" in n or "trail blazers" in n or "por" in n: return "portland"
    if "kings" in n or "sac" in n: return "sacramento"
    if "spurs" in n or "sas" in n: return "sanantonio"
    if "raptors" in n or "tor" in n: return "toronto"
    if "jazz" in n or "uta" in n: return "utah"
    if "wizards" in n or "was" in n: return "washington"
    return n


# ==========================================
# UTILIDADES
# ==========================================
def clock_to_seconds(clock_str):
    """Convierte 'MM:SS' a segundos. Devuelve None si no parsea."""
    if not clock_str:
        return None
    m = re.match(r"(\d+):(\d+)", clock_str.strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def enviar_telegram(msg):
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
        return False
    except requests.RequestException as e:
        log.warning("Telegram error: %s", e)
        return False


def parse_espn_game(ev):
    comp = ev.get("competitions", [{}])[0]
    cs = comp.get("competitors", [])
    if len(cs) < 2:
        return None
    home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
    away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
    q_h = [int(q.get("value") or 0) for q in home.get("linescores", [])]
    q_a = [int(q.get("value") or 0) for q in away.get("linescores", [])]
    st = ev.get("status", {})
    return {
        "id": ev.get("id"),
        "matchup": f"{get_team_key(away.get('team', {}).get('displayName', ''))}"
                   f"_vs_{get_team_key(home.get('team', {}).get('displayName', ''))}",
        "state": st.get("type", {}).get("state", ""),   # pre / in / post
        "period": int(st.get("period") or 0),
        "clock": st.get("displayClock", "") or "",
        "q_h": q_h,
        "q_a": q_a,
    }


# ==========================================
# JOB 1: RECOLECTOR PREGAME (cada 10 min)
# ==========================================
def job_recolector():
    if not INFER_API_KEY:
        return
    headers = {"Authorization": f"Bearer {INFER_API_KEY}"}
    try:
        r = session.get(
            "https://api.infersports.dev/v1/events", headers=headers,
            params={"sport": "basketball", "league": "NBA", "status": "scheduled"},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Recolector events: %s", e)
        return

    ahora = datetime.now(timezone.utc)
    for ev in r.json().get("data", []):
        kickoff_str = ev.get("scheduled_at") or ev.get("commence_time")
        if not kickoff_str:
            continue
        try:
            kickoff = datetime.fromisoformat(kickoff_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        mins = (kickoff - ahora).total_seconds() / 60
        if not (55 <= mins <= 65 or 25 <= mins <= 35):
            continue

        home = ev.get("home_team", {}).get("name", "")
        away = ev.get("away_team", {}).get("name", "")
        matchup = f"{get_team_key(away)}_vs_{get_team_key(home)}"

        try:
            r2 = session.get(
                f"https://api.infersports.dev/v1/events/{ev['id']}/odds",
                headers=headers, params={"format": "decimal"}, timeout=15,
            )
            if r2.status_code != 200:
                continue
        except requests.RequestException as e:
            log.warning("Recolector odds: %s", e)
            continue

        for odd in r2.json().get("odds", []):
            if (odd.get("market_type") == "totals"
                    and odd.get("period") == "full_time"
                    and odd.get("status") == "open"):
                linea = float(odd.get("line"))
                with db() as con:
                    con.execute("""
                        INSERT INTO pregame_lines (event_id, matchup, line, kickoff_time, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(event_id) DO UPDATE SET
                            line = excluded.line,
                            updated_at = excluded.updated_at
                    """, (ev["id"], matchup, linea, kickoff_str, ahora.isoformat()))
                log.info("Pregame %s -> %s (%.0f min)", matchup, linea, mins)
                break


# ==========================================
# JOB 2: ESCÁNER + REGLAS (cada 60s)
# ==========================================
def _evaluar_y_alerta(con, g, regla, checkpoint, pregame, proy, target, vivo, ln):
    """
    regla 1: proy > pre → UNDER | proy < pre → OVER | target = proy | filtro: UNDER si vivo>=proy
    regla 2: proy > pre → OVER  | proy < pre → UNDER| target = pre  | filtro: OVER  si vivo<=pre
    """
    if proy == pregame:
        return

    if regla == 1:
        decision = "UNDER" if proy > pregame else "OVER"
        if decision == "UNDER" and vivo < target: return
        if decision == "OVER" and vivo > target: return
    else:
        decision = "OVER" if proy > pregame else "UNDER"
        if decision == "OVER" and vivo > target: return
        if decision == "UNDER" and vivo < target: return

    cuota = ln["under"] if decision == "UNDER" else ln["over"]
    cur = con.execute("""
        INSERT OR IGNORE INTO alertas
        (event_id, matchup, regla, checkpoint, pregame_line, proyeccion, target_line,
         decision, live_line_at_alert, cuota, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (g["id"], g["matchup"], regla, checkpoint, pregame, proy, target,
          decision, vivo, cuota, datetime.now(timezone.utc).isoformat()))

    if cur.rowcount > 0:
        alert_id = cur.lastrowid
        msg = (
            f"🚨 <b>ALERTA #{alert_id} (Regla {regla})</b>\n\n"
            f"🏀 {html.escape(g['matchup'])}\n"
            f"📌 Pregame: {pregame}\n"
            f"📈 Proyección: {proy:.1f}\n"
            f"🎯 Target: {target:.1f}\n"
            f"💡 <b>{decision} {vivo}</b> @{cuota:.2f}"
        )
        if enviar_telegram(msg):
            log.info("Alerta #%s R%s: %s %s @%.2f", alert_id, regla, decision, vivo, cuota)
        else:
            log.warning("Alerta #%s R%s guardada, pero Telegram falló", alert_id, regla)


def job_escaner():
    if not INFER_API_KEY:
        return

    # 1. ESPN scoreboard
    try:
        r = session.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Escáner ESPN: %s", e)
        return

    games = [g for ev in r.json().get("events", []) if (g := parse_espn_game(ev))]

    # 2. Filtrar ventana válida
    #    Regla 1: Q4 (state=in, period=4, clock >= 2:00)
    #    Regla 2: Q3 o Q4 (state=in, period in {3,4}, clock >= 2:00 si Q4)
    #    OT (period>=5) y Final descartados
    validos = []
    for g in games:
        if g["state"] != "in":
            continue
        if g["period"] not in (3, 4):
            continue
        if g["period"] == 4:
            secs = clock_to_seconds(g["clock"])
            if secs is None or secs < 120:   # corte < 2:00
                continue
        validos.append(g)

    if not validos:
        return

    # 3. Líneas en vivo (solo si hay partidos válidos)
    headers = {"Authorization": f"Bearer {INFER_API_KEY}"}
    try:
        r_live = session.get("https://api.infersports.dev/v1/events/live", headers=headers, timeout=15)
        if r_live.status_code != 200:
            return
    except requests.RequestException as e:
        log.warning("Escáner live: %s", e)
        return

    need = {g["matchup"] for g in validos}
    live_odds = {}
    for ev in r_live.json().get("data", []):
        key = (f"{get_team_key(ev['away_team']['name'])}"
               f"_vs_{get_team_key(ev['home_team']['name'])}")
        if key not in need:
            continue
        try:
            r2 = session.get(
                f"https://api.infersports.dev/v1/events/{ev['id']}/odds",
                headers=headers, params={"format": "decimal"}, timeout=15,
            )
            if r2.status_code != 200:
                continue
        except requests.RequestException:
            continue
        for odd in r2.json().get("odds", []):
            if (odd.get("market_type") == "totals"
                    and odd.get("period") == "full_time"
                    and odd.get("status") == "open"):
                prices = odd.get("prices", {})
                live_odds[key] = {
                    "line": float(odd["line"]),
                    "over": float(prices.get("over") or 0),
                    "under": float(prices.get("under") or 0),
                }
                break

    # 4. Aplicar reglas
    for g in validos:
        ln = live_odds.get(g["matchup"])
        if not ln:
            continue
        with db() as con:
            pre_row = con.execute(
                "SELECT line FROM pregame_lines WHERE matchup=? ORDER BY updated_at DESC LIMIT 1",
                (g["matchup"],),
            ).fetchone()
            if not pre_row:
                continue
            pregame = float(pre_row[0])
            vivo = ln["line"]

            # Regla 1: solo Q4
            if g["period"] == 4:
                t3 = sum(g["q_h"][:3]) + sum(g["q_a"][:3])
                proy = t3 * 1.25
                _evaluar_y_alerta(con, g, 1, "Q4", pregame, proy, proy, vivo, ln)

            # Regla 2: Q3 o Q4
            if g["period"] in (3, 4):
                ht = sum(g["q_h"][:2]) + sum(g["q_a"][:2])
                proy = ht * 2
                chk = "Q3" if g["period"] == 3 else "Q4"
                _evaluar_y_alerta(con, g, 2, chk, pregame, proy, pregame, vivo, ln)


# ==========================================
# JOB 3: AUDITOR (cada 5 min)
# ==========================================
def job_auditor():
    try:
        with db() as con:
            pendientes = con.execute(
                "SELECT id, event_id, decision, target_line FROM alertas WHERE status='PENDING'"
            ).fetchall()
        if not pendientes:
            return

        r = session.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=15,
        )
        r.raise_for_status()
        games = {ev.get("id"): ev for ev in r.json().get("events", [])}

        updates = []
        for alert_id, event_id, decision, target in pendientes:
            g = games.get(event_id)
            if not g:
                continue
            if g.get("status", {}).get("type", {}).get("state") != "post":
                continue
            comp = g.get("competitions", [{}])[0]
            final_total = sum(int(c.get("score") or 0) for c in comp.get("competitors", []))

            if final_total == target:
                status = "PUSH"
            elif decision == "OVER":
                status = "WON" if final_total > target else "LOST"
            else:
                status = "WON" if final_total < target else "LOST"
            updates.append((status, final_total, alert_id))

        if updates:
            with db() as con:
                for u in updates:
                    con.execute("UPDATE alertas SET status=?, final_total=? WHERE id=?", u)
            for status, final_total, alert_id in updates:
                log.info("Auditor #%s: %s (final=%s)", alert_id, status, final_total)
    except Exception as e:
        log.exception("Auditor error: %s", e)


# ==========================================
# DASHBOARD
# ==========================================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        with db() as con:
            rows = con.execute("""
                SELECT id, matchup, regla, pregame_line, target_line, decision,
                       live_line_at_alert, cuota, status, final_total
                FROM alertas ORDER BY id DESC
            """).fetchall()
    except Exception as e:
        log.exception("Dashboard error")
        return HTMLResponse(f"<h1>Error</h1><p>{html.escape(str(e))}</p>", status_code=500)

    total   = len(rows)
    won     = sum(1 for r in rows if r[8] == "WON")
    lost    = sum(1 for r in rows if r[8] == "LOST")
    push    = sum(1 for r in rows if r[8] == "PUSH")
    pending = sum(1 for r in rows if r[8] == "PENDING")

    units = 0.0
    for r in rows:
        if r[8] == "WON":  units += (r[7] - 1.0)
        elif r[8] == "LOST": units -= 1.0

    win_rate = (won / (won + lost) * 100) if (won + lost) > 0 else 0
    wr_color = "#16a34a" if win_rate >= 50 else "#dc2626"
    u_color  = "#16a34a" if units >= 0 else "#dc2626"

    html_out = f"""
    <html><head><title>Alertas Value</title><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; background: #f4f6f9; padding: 20px; color: #334155; }}
        h1 {{ color: #1e293b; }}
        .cards {{ display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 20px; }}
        .card {{ background: #fff; padding: 20px; border-radius: 10px; flex: 1; min-width: 130px; text-align: center; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border-top: 4px solid #3b82f6; }}
        .card h3 {{ margin: 0 0 10px 0; color: #64748b; font-size: 13px; text-transform: uppercase; }}
        .card .val {{ font-size: 28px; font-weight: bold; }}
        table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; margin-top: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }}
        th, td {{ padding: 12px; border-bottom: 1px solid #e2e8f0; text-align: center; font-size: 14px; }}
        th {{ background: #f8fafc; color: #64748b; text-transform: uppercase; font-size: 12px; }}
        .won {{ color: #16a34a; font-weight: bold; background: #dcfce7; padding: 4px 8px; border-radius: 4px; }}
        .lost {{ color: #dc2626; font-weight: bold; background: #fee2e2; padding: 4px 8px; border-radius: 4px; }}
        .pending {{ color: #ca8a04; font-weight: bold; background: #fef08a; padding: 4px 8px; border-radius: 4px; }}
        .push {{ color: #475569; font-weight: bold; background: #e2e8f0; padding: 4px 8px; border-radius: 4px; }}
    </style></head><body>
    <h1>🎯 Alertas de Valor (Target Price)</h1>
    <div class="cards">
        <div class="card" style="border-top-color:{wr_color};"><h3>Win Rate</h3><div class="val" style="color:{wr_color};">{win_rate:.1f}%</div></div>
        <div class="card" style="border-top-color:{u_color};"><h3>Unidades</h3><div class="val" style="color:{u_color};">{units:+.2f} u</div></div>
        <div class="card" style="border-top-color:#16a34a;"><h3>Ganadas</h3><div class="val" style="color:#16a34a;">{won}</div></div>
        <div class="card" style="border-top-color:#dc2626;"><h3>Perdidas</h3><div class="val" style="color:#dc2626;">{lost}</div></div>
        <div class="card" style="border-top-color:#475569;"><h3>Push</h3><div class="val" style="color:#475569;">{push}</div></div>
        <div class="card" style="border-top-color:#ca8a04;"><h3>Pendientes</h3><div class="val" style="color:#ca8a04;">{pending}</div></div>
    </div>
    <table>
        <tr><th>ID</th><th>Partido</th><th>Regla</th><th>Pregame</th><th>Target</th><th>Señal</th><th>Línea alerta</th><th>Cuota</th><th>Final</th><th>Status</th></tr>
    """
    if not rows:
        html_out += "<tr><td colspan='10' style='padding:20px;'>No hay alertas guardadas aún.</td></tr>"
    else:
        for r in rows:
            css = {"WON": "won", "LOST": "lost", "PUSH": "push"}.get(r[8], "pending")
            html_out += (
                f"<tr><td>#{r[0]}</td>"
                f"<td style='text-align:left;'>{html.escape(r[1] or '')}</td>"
                f"<td>R{r[2]}</td><td>{r[3]}</td><td>{r[4]:.1f}</td>"
                f"<td><b>{html.escape(r[5] or '')}</b></td>"
                f"<td>{r[6]:.1f}</td><td>@{r[7]:.2f}</td>"
                f"<td>{r[9] if r[9] is not None else '-'}</td>"
                f"<td><span class='{css}'>{r[8]}</span></td></tr>"
            )
    html_out += "</table></body></html>"
    return html_out


@app.get("/health")
@app.head("/health")
def health_check():
    return {"status": "ok"}


# ==========================================
# LIFESPAN
# ==========================================
@asynccontextmanager
async def lifespan(app):
    missing = [k for k, v in REQUIRED.items() if not v]
    if missing:
        raise RuntimeError(f"Faltan variables de entorno: {missing}")
    init_db()
    scheduler.add_job(job_recolector, "interval", minutes=10, id="recolector", coalesce=True, max_instances=1)
    scheduler.add_job(job_escaner,    "interval", seconds=60, id="escaner",    coalesce=True, max_instances=1)
    scheduler.add_job(job_auditor,    "interval", minutes=5,  id="auditor",    coalesce=True, max_instances=1)
    scheduler.start()
    log.info("Bot iniciado: Recolector 10m, Escáner 60s, Auditor 5m.")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        session.close()
        log.info("Bot detenido.")


app = FastAPI(lifespan=lifespan)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
