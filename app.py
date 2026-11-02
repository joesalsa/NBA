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
