import os
import libsql
import requests
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

# ==========================================
# CONFIGURACIÓN
# ==========================================
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
INFER_API_KEY = os.environ.get("INFER_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

app = FastAPI()

# ==========================================
# BASE DE DATOS TURSO
# ==========================================
def get_db():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    con = get_db()
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
    """)
    con.commit()
    con.close()

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
# FUNCIÓN TELEGRAM
# ==========================================
def enviar_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        return r.status_code == 200
    except: 
        return False


# ==========================================
# JOB 1: RECOLECTOR PREGAME (Cada 10 minutos)
# ==========================================
def job_recolector():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Job 1: Recolector Pregame...")
    if not INFER_API_KEY:
        return
        
    headers = {'User-Agent': 'Mozilla/5.0', 'Authorization': f'Bearer {INFER_API_KEY}'}
    
    # Buscar todos los eventos programados
    r = requests.get("https://api.infersports.dev/v1/events", headers=headers,
                     params={"sport": "basketball", "league": "NBA", "status": "scheduled"}, timeout=15)
    if r.status_code != 200: return
    
    eventos = r.json().get("data", [])
    ahora = datetime.now(timezone.utc)
    
    for ev in eventos:
        kickoff_str = ev.get("scheduled_at") or ev.get("commence_time")
        if not kickoff_str: continue
        
        try:
            kickoff = datetime.fromisoformat(kickoff_str.replace('Z', '+00:00'))
        except: 
            continue
        
        mins_restantes = (kickoff - ahora).total_seconds() / 60.0
        
        # REGLA: Capturar si falta entre 55-65 mins o entre 25-35 mins
        if (55 <= mins_restantes <= 65) or (25 <= mins_restantes <= 35):
            home = ev.get("home_team", {}).get("name", "")
            away = ev.get("away_team", {}).get("name", "")
            matchup = f"{get_team_key(away)}_vs_{get_team_key(home)}"
            
            # Pedir cuota a InferSports
            r2 = requests.get(f"https://api.infersports.dev/v1/events/{ev['id']}/odds?format=decimal", headers=headers, timeout=15)
            if r2.status_code == 200:
                for odd in r2.json().get("odds", []):
                    if odd.get("market_type") == "totals" and odd.get("period") == "full_time" and odd.get("status") == "open":
                        linea = float(odd.get("line"))
                        con = get_db()
                        con.execute("""
                            INSERT INTO pregame_lines (event_id, matchup, line, kickoff_time, updated_at) 
                            VALUES (?, ?, ?, ?, ?) 
                            ON CONFLICT(event_id) DO UPDATE SET line=excluded.line, updated_at=excluded.updated_at
                        """, (ev['id'], matchup, linea, kickoff_str, ahora.isoformat()))
                        con.commit()
                        con.close()
                        print(f"   📌 Pregame Guardada: {matchup} -> {linea} (Faltan {mins_restantes:.0f} min)")
                        break


# ==========================================
# JOB 2: ESCÁNER EN VIVO Y REGLAS (Cada 60s)
# ==========================================
def job_escaner():
    if not INFER_API_KEY: return
    
    # 1. Obtener partidos en vivo de ESPN
    try:
        r = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=15)
        games = []
        for ev in r.json().get("events", []):
            comp = ev.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) < 2: continue
            home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
            away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
            q_h = [int(q.get("value", 0)) for q in home.get("linescores", [])]
            q_a = [int(q.get("value", 0)) for q in away.get("linescores", [])]
            st = ev.get("status", {})
            games.append({
                "id": ev.get("id"),
                "matchup": f"{get_team_key(away.get('team', {}).get('displayName', ''))}_vs_{get_team_key(home.get('team', {}).get('displayName', ''))}",
                "status": st.get("type", {}).get("description", ""),
                "period": int(st.get("period", 0) or 0),
                "clock": st.get("displayClock", "") or "",
                "q_h": q_h, "q_a": q_a,
                "total": sum(q_h) + sum(q_a)
            })
    except: return

    # 2. Filtrar partidos que estén en HT, Q3_END o Q4
    validos = []
    for g in games:
        is_ht = "Half" in g["status"] or (g["period"] == 2 and g["clock"] in ["0.0", "0:00"])
        is_q3_end = "End of 3rd" in g["status"] or (g["period"] == 3 and g["clock"] in ["0.0", "0:00"])
        is_q4 = (g["period"] == 4 and g["status"] != "Final")
        if is_ht or is_q3_end or is_q4:
            validos.append((g, is_ht, is_q3_end or is_q4))

    if not validos: return # Si no hay partidos válidos, no gasta InferSports

    # 3. Obtener líneas en vivo de InferSports (solo si hay partidos válidos)
    headers = {'User-Agent': 'Mozilla/5.0', 'Authorization': f'Bearer {INFER_API_KEY}'}
    r_live = requests.get("https://api.infersports.dev/v1/events/live", headers=headers, timeout=15)
    if r_live.status_code != 200: return
    
    live_odds = {}
    for ev in r_live.json().get("data", []):
        key = f"{get_team_key(ev['away_team']['name'])}_vs_{get_team_key(ev['home_team']['name'])}"
        r2 = requests.get(f"https://api.infersports.dev/v1/events/{ev['id']}/odds?format=decimal", headers=headers, timeout=15)
        if r2.status_code == 200:
            for odd in r2.json().get("odds", []):
                if odd.get("market_type") == "totals" and odd.get("period") == "full_time" and odd.get("status") == "open":
                    live_odds[key] = {"line": float(odd.get("line")), "over": float(odd["prices"].get("over",0)), "under": float(odd["prices"].get("under",0))}
                    break

    # 4. Aplicar Reglas
    con = get_db()
    for g, is_ht, is_q3_q4 in validos:
        # Verificar si ya existe pregame en Turso
        pre = con.execute("SELECT line FROM pregame_lines WHERE event_id=?", (g["id"],)).fetchone()
        if not pre: continue # Sin ancla pregame, saltar en silencio
        pregame = pre[0]
        
        ln = live_odds.get(g["matchup"])
        if not ln: continue
        vivo = ln["line"]

        # --- REGLA 1 (Q3_END o Q4) ---
        if is_q3_q4:
            t3 = sum(g["q_h"][:3]) + sum(g["q_a"][:3])
            proy = t3 * 1.25
            decision, target = None, None
            
            if proy > pregame:
                decision, target = "UNDER", proy
                if vivo >= target: pass
                else: decision = None
            elif proy < pregame:
                decision, target = "OVER", proy
                if vivo <= target: pass
                else: decision = None
                
            if decision:
                cur = con.execute("""
                    INSERT OR IGNORE INTO alertas (event_id, matchup, regla, checkpoint, pregame_line, proyeccion, target_line, decision, live_line_at_alert, cuota, created_at) 
                    VALUES (?, ?, 1, 'Q3_Q4', ?, ?, ?, ?, ?, ?, ?)
                """, (g["id"], g["matchup"], pregame, proy, target, decision, vivo, ln["under"] if decision == "UNDER" else ln["over"], datetime.now().isoformat()))
                con.commit()
                if cur.rowcount > 0:
                    alert_id = cur.lastrowid
                    msg = f"🚨 <b>ALERTA #{alert_id} (Regla 1)</b>\n\n🏀 {g['matchup']}\n📌 Pregame: {pregame}\n📈 Proyección Q3: {proy:.1f}\n🎯 Target: {target:.1f}\n💡 <b>{decision} {vivo}</b> @{ln['under'] if decision == 'UNDER' else ln['over']:.2f}"
                    enviar_telegram(msg)
                    print(f"✅ Alerta #{alert_id} enviada: {decision} {vivo} (Regla 1)")

        # --- REGLA 2 (HT) ---
        if is_ht:
            ht = sum(g["q_h"][:2]) + sum(g["q_a"][:2])
            proy = ht * 2
            decision, target = None, pregame
            
            if proy > pregame:
                decision = "OVER"
                if vivo > target: decision = None
            elif proy < pregame:
                decision = "UNDER"
                if vivo < target: decision = None
                
            if decision:
                cur = con.execute("""
                    INSERT OR IGNORE INTO alertas (event_id, matchup, regla, checkpoint, pregame_line, proyeccion, target_line, decision, live_line_at_alert, cuota, created_at) 
                    VALUES (?, ?, 2, 'HT', ?, ?, ?, ?, ?, ?, ?)
                """, (g["id"], g["matchup"], pregame, proy, target, decision, vivo, ln["under"] if decision == "UNDER" else ln["over"], datetime.now().isoformat()))
                con.commit()
                if cur.rowcount > 0:
                    alert_id = cur.lastrowid
                    msg = f"🚨 <b>ALERTA #{alert_id} (Regla 2)</b>\n\n🏀 {g['matchup']}\n📌 Pregame: {pregame}\n📈 Proyección HT: {proy:.1f}\n🎯 Target: {target:.1f}\n💡 <b>{decision} {vivo}</b> @{ln['under'] if decision == 'UNDER' else ln['over']:.2f}"
                    enviar_telegram(msg)
                    print(f"✅ Alerta #{alert_id} enviada: {decision} {vivo} (Regla 2)")
    con.close()
