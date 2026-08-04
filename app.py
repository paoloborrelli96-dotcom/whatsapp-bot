import os
import re
import io
import json
import time
import hmac
import html
import base64
import random
import hashlib
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz
import requests
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from flask import Flask, request, Response, jsonify
from openai import OpenAI

try:
    from pypdf import PdfReader
except Exception:  # optional dependency until a PDF is uploaded
    PdfReader = None

try:
    from docx import Document
except Exception:  # optional dependency until a DOCX is uploaded
    Document = None


# =============================================================================
# CONFIGURAZIONE
# =============================================================================
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("supporto_fase4")
app = Flask(__name__)

APP_BUILD = "2026-08-04-template-telegram-v5"


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variabile obbligatoria mancante: {name}")
    return value


DATABASE_URL = env_required("DATABASE_URL")
OPENAI_API_KEY = env_required("OPENAI_API_KEY")

TELEGRAM_BOT_TOKEN = env_required("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = env_required("TELEGRAM_GROUP_ID")
TELEGRAM_CHAT_ID = env_required("TELEGRAM_CHAT_ID")
TELEGRAM_ALLOWED_ADMIN_IDS = {
    int(x.strip())
    for x in env_required("TELEGRAM_ALLOWED_ADMIN_IDS").split(",")
    if x.strip()
}
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()

META_ACCESS_TOKEN = env_required("META_ACCESS_TOKEN")
META_APP_SECRET = env_required("META_APP_SECRET")
META_VERIFY_TOKEN = env_required("META_VERIFY_TOKEN")
META_PHONE_NUMBER_ID = env_required("META_PHONE_NUMBER_ID")
META_WABA_ID = os.environ.get("META_WABA_ID", "").strip()
META_APP_ID = os.environ.get("META_APP_ID", "").strip()
META_API_VERSION = os.environ.get("META_API_VERSION", "v22.0").strip()
ADMIN_SETUP_SECRET = os.environ.get("ADMIN_SETUP_SECRET", "").strip()
# Template Meta approvato: configurato direttamente nel codice.
META_TEMPLATE_CONSULENZA = "consulenza"
META_TEMPLATE_CONSULENZA_LANG = "it"

TIMEZONE = os.environ.get("TIMEZONE", "Europe/Rome")
TZ = pytz.timezone(TIMEZONE)

MODEL_CHAT = os.environ.get("MODEL_CHAT", "gpt-5.6-sol")
MODEL_CHECKUP = os.environ.get("MODEL_CHECKUP", MODEL_CHAT)
MODEL_REVISION = os.environ.get("MODEL_REVISION", MODEL_CHAT)
MODEL_PROFILE = os.environ.get("MODEL_PROFILE", "gpt-5.6-terra")
MODEL_ROUTER = os.environ.get("MODEL_ROUTER", "gpt-5.6-luna")
MODEL_CLASSIFIER = os.environ.get("MODEL_CLASSIFIER", MODEL_ROUTER)
MODEL_AUDIO = os.environ.get("MODEL_AUDIO", "gpt-4o-mini-transcribe")

IMMEDIATE_DELAY_MIN_SECONDS = int(os.environ.get("IMMEDIATE_DELAY_MIN_SECONDS", "180"))
IMMEDIATE_DELAY_MAX_SECONDS = int(os.environ.get("IMMEDIATE_DELAY_MAX_SECONDS", "420"))
NORMAL_DELAY_MIN_SECONDS = int(os.environ.get("NORMAL_DELAY_MIN_SECONDS", "1800"))
NORMAL_DELAY_MAX_SECONDS = int(os.environ.get("NORMAL_DELAY_MAX_SECONDS", "2400"))
CHECKUP_REVIEW_DELAY_SECONDS = int(os.environ.get("CHECKUP_REVIEW_DELAY_SECONDS", "1800"))
QUIET_HOURS_START = int(os.environ.get("QUIET_HOURS_START", "23"))
QUIET_HOURS_END = int(os.environ.get("QUIET_HOURS_END", "7"))
SUPPORT_DURATION_DAYS = int(os.environ.get("SUPPORT_DURATION_DAYS", "30"))
EXPIRATION_POLL_SECONDS = int(os.environ.get("EXPIRATION_POLL_SECONDS", "900"))
RECENT_HISTORY_LIMIT = int(os.environ.get("RECENT_HISTORY_LIMIT", "30"))
RECOVERY_POLL_SECONDS = int(os.environ.get("RECOVERY_POLL_SECONDS", "300"))
RECOVERY_DELAY_MIN_SECONDS = int(os.environ.get("RECOVERY_DELAY_MIN_SECONDS", "300"))
RECOVERY_DELAY_MAX_SECONDS = int(os.environ.get("RECOVERY_DELAY_MAX_SECONDS", "600"))

STATUS_PAUSED = "paused"
STATUS_ACTIVE = "active"
STATUS_REVIEW = "review"
STATUS_CHECKUP = "checkup"
STATUS_CLOSED = "closed"

CAPTURE_NONE = "none"
CAPTURE_QUESTIONNAIRE = "questionnaire"
CAPTURE_PLAN = "plan"
SILENT_NO_REPLY_MARKER = "[SILENT_NO_REPLY]"

openai_client = OpenAI(api_key=OPENAI_API_KEY)

active_timers: Dict[str, threading.Timer] = {}
active_timers_lock = threading.Lock()
expiration_worker_started = False
expiration_worker_lock = threading.Lock()
recovery_worker_started = False
recovery_worker_lock = threading.Lock()


# =============================================================================
# PROMPT: IDENTITÀ E FASE 4
# =============================================================================
SYSTEM_PROMPT_BASE = """
Sei Paola, consulente di Genitori in Armonia.
Gestisci esclusivamente il supporto WhatsApp successivo alla consegna del piano personalizzato sul sonno infantile.
La mamma ha già compilato il questionario, ha già ricevuto il piano e conosce Paola.

IDENTITÀ
Scrivi sempre in prima persona singolare come Paola.
Devi sembrare Paola che continua una conversazione reale, non un assistente, un manuale o un testo generato.
Non presentarti di nuovo e non dire "il nostro team".
Non inventare esperienze personali, fatti o dettagli che non sono nel contesto.

STILE WHATSAPP
Tono caldo, diretto, semplice, concreto e naturale.
Non usare punti esclamativi.
Non usare "cara". Usa "mamma" solo quando suona naturale.
Usa al massimo una emoji quando serve.
Non usare markdown, titoli, grassetto, tabelle o elenchi nelle risposte ordinarie.
Evita formule automatiche come "Grazie per aver condiviso", "Capisco perfettamente", "Vediamo insieme" o "Ecco cosa devi fare".
Evita termini tecnici come "associazione seno-sonno", "igiene del sonno" o "stimolazione cognitiva".
Puoi usare espressioni naturali come "guarda", "ti dico", "secondo me", "io manterrei", "per ora farei così".

FASE 4
Usa questionario, piano di Paola, profilo, note interne e storico recente.
Non generare un nuovo piano e non ricominciare l'analisi da zero.
Rispondi prima a ciò che la mamma ha realmente scritto.
Non sei obbligata a dare un consiglio in ogni messaggio.
Se racconta un miglioramento, valorizzalo in modo specifico e fermati se non serve altro.
Se racconta un piccolo passo indietro, normalizzalo senza colpevolizzarla.
Se si sfoga, riconosci prima la fatica reale e non trasformare lo sfogo in un interrogatorio.
Se fa una domanda pratica, rispondi direttamente.
Dai massimo una o due indicazioni alla volta e non cambiare troppi elementi insieme.
Mantieni la gradualità e la direzione del piano già consegnato.
Fai una sola domanda soltanto quando manca un dato indispensabile per evitare una risposta sbagliata.
Non chiedere mai dati già presenti nel questionario, nel piano, nel profilo o nello storico.
Non terminare automaticamente con "aggiornami", "fammi sapere", "come è andata?", "a che ora?" o formule simili.

LUNGHEZZA
Adatta sempre la lunghezza alla richiesta reale.
Un aggiornamento breve richiede normalmente 1-3 frasi.
Una domanda concreta richiede normalmente una risposta breve o media.
Approfondisci solo quando il problema è davvero articolato.
Una risposta di qualità non deve essere lunga.
Taglia introduzioni, ripetizioni, rassicurazioni generiche e spiegazioni che la mamma conosce già.

EMOTIVITÀ E SALUTE
Usa rassicurazioni forti solo se la mamma mostra davvero ansia, senso di colpa, disperazione o forte stanchezza.
Non fare diagnosi, non interpretare prescrizioni e non consigliare farmaci o dosaggi.
Per difficoltà respiratoria, febbre importante ancora in corso, vomito persistente, disidratazione, dolore forte, crescita, farmaci o dubbi sanitari diretti, rimanda al pediatra e non dare consigli medici.
Se il tema sanitario è lieve e la domanda resta sul sonno, puoi parlare con prudenza del maggiore bisogno di contatto e del rientro graduale alla routine.

CONFINI INVISIBILI
Non parlare mai alla mamma di scadenza della consulenza, giorni rimasti, rinnovi, prezzi, offerte, alert Telegram, stato tecnico o intelligenza artificiale.
Anche dopo il trentesimo giorno continua normalmente finché Paola non mette la chat in pausa o la chiude.
Solo Paola può proporre rinnovi o offerte.
""".strip()

ROUTER_PROMPT = """
Sei un classificatore prudente per una conversazione di supporto sul sonno infantile già attiva.
Restituisci SOLO JSON valido con questo schema:
{
  "intent": "miglioramento|aggiornamento|domanda_pratica|difficolta_persistente|sfogo|tema_medico_lieve|tema_medico_delicato|richiesta_paola|perdita_fiducia|reclamo_rimborso|cortesia|altro",
  "confidence": 0.0,
  "safe_auto_reply": true,
  "needs_human": false,
  "pause_chat": false,
  "response_depth": "micro|normal|deep",
  "reason": "breve motivo"
}

Regole:
- Cortesie, ok, grazie ed emoji isolate sono "cortesia".
- Una domanda pratica immediata può ricevere risposta automatica breve.
- Febbre, tosse, raffreddore, dentini o malessere lieve citati nel contesto del sonno sono tema_medico_lieve, safe_auto_reply=true.
- Difficoltà respiratoria, febbre alta in corso o peggioramento, vomito persistente, disidratazione, dolore forte, farmaci/dosaggi, richiesta di diagnosi o urgenza sono tema_medico_delicato, needs_human=true, pause_chat=true.
- Se chiede Paola o una persona reale: richiesta_paola, needs_human=true, pause_chat=true.
- Se dice che le risposte sono copiate, incoerenti, che ha perso fiducia o che il metodo non funziona: perdita_fiducia, needs_human=true, pause_chat=true.
- Rimborso, denuncia, avvocato, truffa o reclamo forte: reclamo_rimborso, needs_human=true, pause_chat=true.
- Un normale peggioramento o alcuni giorni difficili non richiedono automaticamente l'umano, salvo forte rabbia, perdita di fiducia o necessità di cambiare completamente piano.
- response_depth micro per aggiornamenti semplici; normal per domande concrete; deep solo per situazioni articolate.
""".strip()

CHECKUP_PROMPT = """
Genera un checkup personalizzato come Paola per una mamma che ha già ricevuto un piano sul sonno.
Leggi questionario, piano, profilo e storico recente.
Non inviare un questionario generico uguale per tutti e non chiedere dati già presenti.
Scegli massimo 6-8 domande mirate sul problema attuale: giorni di applicazione, cosa è migliorato, cosa è peggiorato, addormentamento, primo risveglio, seconda parte della notte, aiuto richiesto, pisolini, eventi recenti e ciò che pesa di più.
Non dare consigli in questo messaggio.
Puoi numerare le domande perché devono essere facili da compilare.
Tono naturale, caldo e pratico da WhatsApp.
Chiudi chiedendo di rispondere con calma, senza promettere risultati.
""".strip()

CHECKUP_CLASSIFIER_PROMPT = """
Valuta se la mamma ha risposto in modo sufficiente al checkup sul sonno.
Restituisci SOLO JSON valido:
{
  "status": "sufficient|incomplete|defer",
  "confidence": 0.0,
  "missing": "eventuali dati davvero indispensabili",
  "reason": "breve motivo"
}
Sufficient: sono presenti informazioni concrete su almeno 2-3 aspetti tra miglioramenti/peggioramenti, addormentamento, risvegli, aiuto richiesto, pisolini, eventi nuovi e difficoltà principale.
Defer: dice solo che risponderà dopo, ok, grazie o simili.
Incomplete: ha scritto qualcosa ma non basta per una revisione seria.
""".strip()

REVISION_PROMPT = """
Scrivi una revisione aggiornata del piano come Paola.
La mamma ha già ricevuto il piano iniziale: non ricominciare da zero.
Usa questionario, piano, profilo, storico recente e risposte al checkup.
Spiega in prosa naturale cosa mantenere, cosa correggere e cosa non toccare per non creare confusione.
Dai una sola direzione concreta per i prossimi 3-5 giorni.
Quando pertinente tratta addormentamento, risvegli, pisolini e gestione di seno, latte, ciuccio, braccio o contatto.
Non usare titoli, markdown, elenchi o numerazioni.
Non dare diagnosi o indicazioni mediche.
La revisione può essere più completa delle risposte normali, ma deve restare leggibile su WhatsApp e non ripetere tutto il piano.
""".strip()

FORCED_CONTINUE_PROMPT = """
Rispondi all'ultimo messaggio come Paola dopo un alert autorizzato da Paola.
Non dire che c'è stato un alert.
Rispondi alla domanda concreta, collegati al piano e dai massimo una o due indicazioni per oggi o stanotte.
Se il tema sanitario è delicato, rimanda al pediatra e non dare indicazioni mediche.
Tono prudente, umano, diretto e non eccessivamente lungo.
""".strip()

QUALITY_PROMPT = """
Sei il controllo qualità finale di una risposta WhatsApp scritta come Paola durante una consulenza sul sonno già attiva.
Restituisci SOLO JSON valido:
{
  "send": true,
  "rewrite": false,
  "reason": "breve motivo"
}

Valuta la risposta rispetto a messaggio della mamma, piano, profilo e storico.
Deve essere naturale, specifica, coerente con il piano, proporzionata alla richiesta e non sembrare generata.
Deve evitare diagnosi, farmaci, prezzi, rinnovi, scadenze, riferimenti al bot o a Telegram.
Non deve fare domande finali di abitudine, ripetere il contesto, dare più di 1-2 indicazioni o diventare lunga senza motivo.
Metti rewrite=true se basta riscriverla.
Metti send=false soltanto se è pericolosa, contraddittoria, inventa dati importanti o non risponde alla richiesta.
""".strip()

QUALITY_REWRITE_PROMPT = """
Riscrivi il messaggio come Paola.
Mantieni il contenuto utile, ma rendilo più naturale, specifico e proporzionato.
Non aggiungere informazioni, non cambiare il piano, non fare diagnosi, non parlare di scadenze, prezzi, rinnovi, bot o Telegram.
Elimina formule da intelligenza artificiale, ripetizioni, spiegazioni inutili e domande finali non indispensabili.
Dai al massimo una o due indicazioni pratiche.
Scrivi solo il testo finale da inviare su WhatsApp.
""".strip()

PROFILE_PROMPT = """
Estrai un profilo strutturato da questionario, piano e storico di una consulenza sul sonno infantile.
Restituisci SOLO JSON valido. Non inventare dati mancanti.
Campi possibili:
mother_name, child_name, child_age, birth_date, main_problem, goal, sleep_association,
night_wakings, naps, bedtime, wake_time, sleep_place, feeding, father_role,
health_notes, work_stage, admin_notes.
Ometti i campi non chiari.
""".strip()


# =============================================================================
# DATABASE
# =============================================================================
def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    """Crea le tabelle e aggiorna gli schemi PostgreSQL già esistenti."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_cases (
            phone TEXT PRIMARY KEY,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'paused',
            questionnaire TEXT,
            plan TEXT,
            profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            admin_notes TEXT,
            capture_mode TEXT NOT NULL DEFAULT 'none',
            capture_buffer TEXT NOT NULL DEFAULT '',
            activated_at TIMESTAMPTZ,
            support_end_at TIMESTAMPTZ,
            expiration_alert_sent BOOLEAN NOT NULL DEFAULT FALSE,
            checkup_started_at TIMESTAMPTZ,
            checkup_ready_alert_sent BOOLEAN NOT NULL DEFAULT FALSE,
            last_alert_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            provider_message_id TEXT UNIQUE,
            phone TEXT NOT NULL,
            role TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            media_id TEXT,
            media_type TEXT,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_topics (
            phone TEXT PRIMARY KEY,
            thread_id BIGINT NOT NULL UNIQUE,
            topic_name TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id BIGSERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        )
    """)
    conn.commit()

    migrations = [
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS display_name TEXT",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'paused'",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS questionnaire TEXT",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS plan TEXT",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS profile_json JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS admin_notes TEXT",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS capture_mode TEXT DEFAULT 'none'",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS capture_buffer TEXT DEFAULT ''",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS support_end_at TIMESTAMPTZ",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS expiration_alert_sent BOOLEAN DEFAULT FALSE",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS checkup_started_at TIMESTAMPTZ",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS checkup_ready_alert_sent BOOLEAN DEFAULT FALSE",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS last_alert_at TIMESTAMPTZ",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",

        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS provider_message_id TEXT",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'legacy'",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_id TEXT",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type TEXT",
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ DEFAULT NOW()",

        "ALTER TABLE telegram_topics ADD COLUMN IF NOT EXISTS topic_name TEXT",
        "ALTER TABLE telegram_topics ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",

        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'legacy'",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS severity TEXT DEFAULT 'info'",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS reason TEXT",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
    ]

    for statement in migrations:
        try:
            cur.execute(statement)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.warning("Migrazione non applicata (%s): %s", statement, exc)

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_messages_phone_time ON messages(phone, timestamp)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_provider_message_id ON messages(provider_message_id)",
    ]
    for statement in indexes:
        try:
            cur.execute(statement)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.warning("Indice non applicato (%s): %s", statement, exc)

    cur.close()
    conn.close()
    logger.info("Database inizializzato e schema aggiornato")


def ensure_case(phone: str, display_name: Optional[str] = None) -> Dict[str, Any]:
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        INSERT INTO support_cases (phone, display_name)
        VALUES (%s, %s)
        ON CONFLICT (phone) DO UPDATE SET
            display_name = COALESCE(EXCLUDED.display_name, support_cases.display_name),
            updated_at = NOW()
        RETURNING *
    """, (phone, display_name))
    row = dict(cur.fetchone())
    conn.commit()
    cur.close()
    conn.close()
    return row


def get_case(phone: str) -> Dict[str, Any]:
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM support_cases WHERE phone = %s", (phone,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else ensure_case(phone)


def update_case(phone: str, **fields: Any) -> None:
    allowed = {
        "display_name", "status", "questionnaire", "plan", "profile_json", "admin_notes",
        "capture_mode", "capture_buffer", "activated_at", "support_end_at",
        "expiration_alert_sent", "checkup_started_at", "checkup_ready_alert_sent",
        "last_alert_at",
    }
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return
    ensure_case(phone)
    assignments = []
    values: List[Any] = []
    for key, value in clean.items():
        assignments.append(f"{key} = %s")
        if key == "profile_json" and isinstance(value, dict):
            values.append(Json(value))
        else:
            values.append(value)
    values.append(phone)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE support_cases SET {', '.join(assignments)}, updated_at = NOW() WHERE phone = %s", values)
    conn.commit()
    cur.close()
    conn.close()


def save_message(
    phone: str,
    role: str,
    source: str,
    content: str,
    provider_message_id: Optional[str] = None,
    media_id: Optional[str] = None,
    media_type: Optional[str] = None,
) -> bool:
    content = (content or "").strip()
    if not content:
        return False
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO messages (provider_message_id, phone, role, source, content, media_id, media_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider_message_id) DO NOTHING
        """, (provider_message_id, phone, role, source, content, media_id, media_type))
        inserted = cur.rowcount == 1
        conn.commit()
        cur.close()
        conn.close()
        return inserted
    except Exception as exc:
        logger.exception("Errore salvataggio messaggio: %s", exc)
        return False


def get_recent_history(phone: str, limit: int = RECENT_HISTORY_LIMIT) -> List[Dict[str, str]]:
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT role, content, source FROM messages
        WHERE phone = %s
          AND content <> %s
        ORDER BY timestamp DESC, id DESC
        LIMIT %s
    """, (phone, SILENT_NO_REPLY_MARKER, limit))
    rows = list(reversed(cur.fetchall()))
    cur.close()
    conn.close()
    history = []
    for row in rows:
        role = row["role"]
        if role == "admin":
            role = "assistant"
        if role not in ("user", "assistant"):
            continue
        history.append({"role": role, "content": row["content"]})
    return history


def mark_silent_no_reply(phone: str, reason: str = "") -> None:
    saved = save_message(phone, "assistant", "system", SILENT_NO_REPLY_MARKER)
    if saved:
        logger.info("Chiusura registrata senza risposta per %s: %s", phone, reason)


def get_history_before_pending(phone: str, limit: int = RECENT_HISTORY_LIMIT) -> List[Dict[str, str]]:
    history = get_recent_history(phone, limit + 20)
    while history and history[-1].get("role") == "user":
        history.pop()
    return history[-limit:]


def get_pending_user_messages(phone: str) -> List[Dict[str, Any]]:
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT timestamp FROM messages
        WHERE phone = %s AND role IN ('assistant', 'admin')
        ORDER BY timestamp DESC, id DESC LIMIT 1
    """, (phone,))
    row = cur.fetchone()
    cutoff = row["timestamp"] if row else datetime.now(TZ) - timedelta(days=45)
    cur.execute("""
        SELECT content, media_id, media_type, timestamp FROM messages
        WHERE phone = %s AND role = 'user' AND timestamp > %s
        ORDER BY timestamp ASC, id ASC
    """, (phone, cutoff))
    rows = [dict(x) for x in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def append_capture_buffer(phone: str, text: str) -> None:
    case = get_case(phone)
    current = case.get("capture_buffer") or ""
    new_value = f"{current}\n\n{text.strip()}".strip()
    update_case(phone, capture_buffer=new_value)


def add_alert(phone: str, category: str, severity: str, reason: str) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO alerts (phone, category, severity, reason) VALUES (%s, %s, %s, %s)",
        (phone, category, severity, reason),
    )
    conn.commit()
    cur.close()
    conn.close()


# =============================================================================
# UTILITÀ
# =============================================================================
def now_local() -> datetime:
    return datetime.now(TZ)


def normalize_phone(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    return f"+{digits}"


def phone_for_meta(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def phone_last4(phone: str) -> str:
    digits = phone_for_meta(phone)
    return digits[-4:] if digits else "----"


def is_quiet_hours() -> bool:
    hour = now_local().hour
    if QUIET_HOURS_START < QUIET_HOURS_END:
        return QUIET_HOURS_START <= hour < QUIET_HOURS_END
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def is_immediate_question(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        "che faccio", "cosa faccio", "che devo fare", "come mi muovo",
        "lo sveglio", "la sveglio", "lo attacco", "la attacco",
        "adesso", "ora", "si è svegliato", "si e svegliato",
        "si è svegliata", "si e svegliata",
    ]
    return ("?" in t and any(p in t for p in patterns)) or any(
        p in t for p in ["che faccio", "cosa faccio", "lo sveglio", "la sveglio"]
    )


def is_obvious_closing_message(text: str) -> bool:
    if not text:
        return False
    raw = text.strip()
    if len(raw) > 100 or "?" in raw:
        return False
    normalized = re.sub(r"[^\w\sàèéìòù]+", " ", raw.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:  # emoji/reazione isolata
        return True
    important = [
        "svegl", "dorme", "dormito", "piange", "seno", "latte", "biberon",
        "ciuccio", "febbre", "tosse", "raffreddore", "risvegl", "nanna",
        "pisolino", "orario", "come faccio", "cosa faccio", "piano", "questionario",
    ]
    if any(term in normalized for term in important):
        return False
    exact = {
        "ok", "ok grazie", "ok perfetto", "ok va bene", "va bene", "va bene grazie",
        "perfetto", "perfetto grazie", "grazie", "grazie mille", "d accordo",
        "daccordo", "ci provo", "provo", "provo così", "provo cosi", "chiaro",
        "capito", "benissimo", "ottimo", "a posto", "tutto chiaro", "ti aggiorno",
        "poi ti aggiorno", "ok ti aggiorno",
    }
    if normalized in exact:
        return True
    closure_words = {
        "ok", "va", "bene", "benissimo", "perfetto", "grazie", "mille", "capito",
        "chiaro", "provo", "cosi", "così", "allora", "ti", "aggiorno", "dopo",
    }
    return set(normalized.split()).issubset(closure_words)


def smart_split(text: str, max_chars: int = 3500) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = window.rfind("\n\n")
        if split_at < max_chars // 3:
            split_at = max(window.rfind(". "), window.rfind("? "), window.rfind("\n"), window.rfind(" "))
        if split_at < max_chars // 3:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def safe_json_loads(text: str, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return dict(default)


def profile_to_text(profile: Dict[str, Any]) -> str:
    if not profile:
        return "Profilo non ancora strutturato. Usa questionario, piano e storico."
    labels = {
        "mother_name": "Nome mamma", "child_name": "Nome bambino", "child_age": "Età",
        "birth_date": "Data nascita", "main_problem": "Problema iniziale", "goal": "Obiettivo",
        "sleep_association": "Aiuto per addormentarsi", "night_wakings": "Risvegli",
        "naps": "Pisolini", "bedtime": "Nanna serale", "wake_time": "Sveglia mattina",
        "sleep_place": "Dove dorme", "feeding": "Alimentazione", "father_role": "Ruolo papà",
        "health_notes": "Note salute", "work_stage": "Fase di lavoro", "admin_notes": "Note interne",
    }
    lines = [f"{labels.get(k, k)}: {v}" for k, v in profile.items() if v not in (None, "", [], {})]
    return "\n".join(lines) or "Profilo vuoto."


def cancel_timer(phone: str, reason: str = "") -> None:
    with active_timers_lock:
        timer = active_timers.pop(phone, None)
        if timer:
            timer.cancel()
            logger.info("Timer cancellato per %s: %s", phone, reason)


# =============================================================================
# TELEGRAM
# =============================================================================
def telegram_api(method: str, *, json_data: Optional[Dict[str, Any]] = None, files=None, timeout: int = 45):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    response = requests.post(url, json=json_data, files=files, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"Telegram {method}: {response.status_code} {response.text[:500]}")
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data.get("result")


def get_thread_phone(thread_id: int) -> Optional[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT phone FROM telegram_topics WHERE thread_id = %s", (thread_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def get_or_create_topic(phone: str, display_name: Optional[str] = None) -> Optional[int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT thread_id FROM telegram_topics WHERE phone = %s", (phone,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return int(row[0])

    label = (display_name or "Mamma").strip()
    label = re.sub(r"[\r\n\t]+", " ", label)[:80]
    topic_name = f"{label} · {phone_last4(phone)}"
    try:
        result = telegram_api("createForumTopic", json_data={"chat_id": TELEGRAM_GROUP_ID, "name": topic_name})
        thread_id = int(result["message_thread_id"])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO telegram_topics (phone, thread_id)
            VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE SET thread_id = EXCLUDED.thread_id
        """, (phone, thread_id))
        conn.commit()
        cur.close()
        conn.close()
        send_to_topic(phone, "🟡 Nuova chat creata in pausa. Paola gestisce manualmente finché non usa /attiva.", kind="system")
        return thread_id
    except Exception as exc:
        logger.exception("Errore creazione topic %s: %s", phone, exc)
        send_private_alert(f"⚠️ Non riesco a creare il topic per {phone}: {exc}")
        return None


def send_to_topic(phone: str, text: str, kind: str = "system") -> bool:
    case = get_case(phone)
    thread_id = get_or_create_topic(phone, case.get("display_name"))
    if not thread_id:
        return False
    prefixes = {
        "mother": "📩 Mamma: ",
        "bot": "🤖 Bot: ",
        "paola": "👩‍💼 Paola: ",
        "alert": "⚠️ ALERT: ",
        "system": "ℹ️ ",
    }
    prefix = prefixes.get(kind, "")
    try:
        for chunk in smart_split(text, 3900):
            telegram_api("sendMessage", json_data={
                "chat_id": TELEGRAM_GROUP_ID,
                "message_thread_id": thread_id,
                "text": f"{prefix}{chunk}",
            })
        return True
    except Exception as exc:
        logger.exception("Errore invio topic %s: %s", phone, exc)
        return False


def send_private_alert(text: str) -> bool:
    try:
        for chunk in smart_split(text, 3900):
            telegram_api("sendMessage", json_data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
        return True
    except Exception as exc:
        logger.exception("Errore alert privato: %s", exc)
        return False


def download_telegram_file(file_id: str) -> Tuple[bytes, str]:
    info = telegram_api("getFile", json_data={"file_id": file_id})
    file_path = info["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    response = requests.get(url, timeout=90)
    response.raise_for_status()
    return response.content, file_path


def extract_document_text(data: bytes, filename: str) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".txt"):
        return data.decode("utf-8", errors="replace").strip()
    if lower.endswith(".pdf"):
        if PdfReader is None:
            raise RuntimeError("Dipendenza pypdf non installata")
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    if lower.endswith(".docx"):
        if Document is None:
            raise RuntimeError("Dipendenza python-docx non installata")
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
    raise RuntimeError("Formato non supportato. Usa TXT, PDF o DOCX.")


# =============================================================================
# META CLOUD API
# =============================================================================
def verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    if not META_APP_SECRET:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(META_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


def meta_api(
    method: str,
    path: str,
    *,
    json_data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
):
    url = f"https://graph.facebook.com/{META_API_VERSION}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    response = requests.request(method, url, headers=headers, json=json_data, params=params, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"Meta API {response.status_code}: {response.text[:800]}")
    if not response.text.strip():
        return {}
    return response.json()


def public_base_url() -> str:
    explicit = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway:
        return f"https://{railway}"
    return ""


def meta_webhook_url() -> str:
    base = public_base_url()
    return f"{base}/meta_webhook" if base else "/meta_webhook"


def admin_authorized() -> bool:
    secret = ADMIN_SETUP_SECRET or META_VERIFY_TOKEN
    received = request.headers.get("X-Admin-Secret", "")
    return bool(secret) and hmac.compare_digest(received, secret)


def list_waba_phone_numbers() -> List[Dict[str, Any]]:
    if not META_WABA_ID:
        return []
    data = meta_api(
        "GET",
        f"{META_WABA_ID}/phone_numbers",
        params={"fields": "id,display_phone_number,verified_name,quality_rating,code_verification_status"},
    )
    return data.get("data") or []


def get_configured_phone_number() -> Dict[str, Any]:
    return meta_api(
        "GET",
        META_PHONE_NUMBER_ID,
        params={"fields": "id,display_phone_number,verified_name,quality_rating,code_verification_status"},
    )


def is_waba_subscribed() -> bool:
    if not META_WABA_ID:
        return False
    data = meta_api("GET", f"{META_WABA_ID}/subscribed_apps")
    return bool(data.get("data"))


def subscribe_waba_to_app() -> Dict[str, Any]:
    if not META_WABA_ID:
        raise RuntimeError("META_WABA_ID mancante")
    return meta_api("POST", f"{META_WABA_ID}/subscribed_apps")


def meta_setup_status() -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "api_version": META_API_VERSION,
        "webhook_url": meta_webhook_url(),
        "phone_number_id": META_PHONE_NUMBER_ID,
        "waba_id": META_WABA_ID or None,
        "app_id": META_APP_ID or None,
        "waba_subscribed": False,
        "configured_phone": None,
        "waba_phones": [],
        "phone_id_matches_waba": None,
        "errors": [],
    }
    try:
        status["configured_phone"] = get_configured_phone_number()
    except Exception as exc:
        status["errors"].append(f"phone_number: {exc}")
    if META_WABA_ID:
        try:
            status["waba_subscribed"] = is_waba_subscribed()
        except Exception as exc:
            status["errors"].append(f"waba_subscription: {exc}")
        try:
            phones = list_waba_phone_numbers()
            status["waba_phones"] = phones
            configured_id = str(META_PHONE_NUMBER_ID)
            status["phone_id_matches_waba"] = any(str(p.get("id")) == configured_id for p in phones)
        except Exception as exc:
            status["errors"].append(f"waba_phones: {exc}")
    return status


def ensure_meta_subscriptions() -> None:
    if not META_WABA_ID:
        logger.warning("META_WABA_ID non impostato: salto subscribe WABA")
        return
    try:
        if not is_waba_subscribed():
            subscribe_waba_to_app()
            logger.info("App iscritta al WABA %s", META_WABA_ID)
        else:
            logger.info("App già iscritta al WABA %s", META_WABA_ID)
    except Exception as exc:
        logger.exception("Impossibile iscrivere l'app al WABA: %s", exc)


def send_whatsapp_template(
    phone: str,
    template_name: str = META_TEMPLATE_CONSULENZA,
    language: str = META_TEMPLATE_CONSULENZA_LANG,
    body_parameters: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    recipient = phone_for_meta(phone)
    if not recipient:
        return False, "numero destinatario non valido"
    template_payload: Dict[str, Any] = {
        "name": template_name,
        "language": {"code": language},
    }
    if body_parameters:
        template_payload["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_parameters],
        }]
    try:
        result = meta_api("POST", f"{META_PHONE_NUMBER_ID}/messages", json_data={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "template",
            "template": template_payload,
        })
        message_id = None
        if result.get("messages"):
            message_id = result["messages"][0].get("id")
        save_message(phone, "assistant", "meta_api", f"[template:{template_name}]", provider_message_id=message_id)
        send_to_topic(phone, f"[template:{template_name}]", kind="bot")
        return True, None
    except Exception as exc:
        logger.exception("Errore invio template WhatsApp %s: %s", phone, exc)
        return False, str(exc)


def send_consulenza_template(phone: str, body_parameters: Optional[List[str]] = None) -> Tuple[bool, Optional[str]]:
    return send_whatsapp_template(
        phone,
        META_TEMPLATE_CONSULENZA,
        META_TEMPLATE_CONSULENZA_LANG,
        body_parameters,
    )



def get_template_definition() -> Optional[Dict[str, Any]]:
    """Prova a leggere da Meta lingua e variabili del template approvato."""
    if not META_WABA_ID:
        return None
    try:
        result = meta_api(
            "GET",
            f"{META_WABA_ID}/message_templates",
            params={
                "fields": "name,status,language,components",
                "limit": 100,
            },
        )
        for item in result.get("data", []) or []:
            if (
                str(item.get("name", "")).strip() == META_TEMPLATE_CONSULENZA
                and str(item.get("status", "")).upper() == "APPROVED"
            ):
                return item
    except Exception as exc:
        logger.warning("Non riesco a leggere il template da Meta: %s", exc)
    return None


def count_template_body_variables(template: Dict[str, Any]) -> int:
    maximum = 0
    for component in template.get("components", []) or []:
        if str(component.get("type", "")).upper() != "BODY":
            continue
        body_text = str(component.get("text") or "")
        for value in re.findall(r"\{\{(\d+)\}\}", body_text):
            maximum = max(maximum, int(value))
    return maximum


def send_consulenza_template_auto(
    phone: str,
    display_name: str = "Mamma",
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Invia il template provando automaticamente lingua e presenza variabili."""
    attempts: List[Tuple[str, Optional[List[str]], str]] = []
    definition = get_template_definition()

    if definition:
        language = str(definition.get("language") or META_TEMPLATE_CONSULENZA_LANG)
        variable_count = count_template_body_variables(definition)
        if variable_count == 0:
            attempts.append((language, None, "definizione Meta senza variabili"))
        elif variable_count == 1:
            attempts.append((language, [display_name], "definizione Meta con nome"))
        else:
            return (
                False,
                f"Il template approvato contiene {variable_count} variabili nel corpo. "
                "Servono i valori esatti per tutte le variabili.",
                None,
            )

    # Fallback: funziona sia per template senza variabili sia con una variabile nome.
    attempts.extend([
        (META_TEMPLATE_CONSULENZA_LANG, None, "fallback it senza variabili"),
        (META_TEMPLATE_CONSULENZA_LANG, [display_name], "fallback it con nome"),
        ("it_IT", None, "fallback it_IT senza variabili"),
        ("it_IT", [display_name], "fallback it_IT con nome"),
    ])

    unique_attempts: List[Tuple[str, Optional[List[str]], str]] = []
    seen = set()
    for language, parameters, label in attempts:
        key = (language, tuple(parameters or []))
        if key not in seen:
            seen.add(key)
            unique_attempts.append((language, parameters, label))

    errors: List[str] = []
    for language, parameters, label in unique_attempts:
        sent, error = send_whatsapp_template(
            phone,
            template_name=META_TEMPLATE_CONSULENZA,
            language=language,
            body_parameters=parameters,
        )
        if sent:
            logger.info(
                "Template %s inviato a %s con %s",
                META_TEMPLATE_CONSULENZA,
                phone,
                label,
            )
            return True, None, label
        errors.append(f"{label}: {error}")

    return False, " | ".join(errors[-4:]), None


def send_whatsapp_message(phone: str, text: str, source: str = "bot") -> bool:
    recipient = phone_for_meta(phone)
    if not recipient:
        return False
    sent = False
    for chunk in smart_split(text, 3500):
        try:
            result = meta_api("POST", f"{META_PHONE_NUMBER_ID}/messages", json_data={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            })
            message_id = None
            if result.get("messages"):
                message_id = result["messages"][0].get("id")
            role = "admin" if source == "paola" else "assistant"
            save_message(phone, role, "meta_api", chunk, provider_message_id=message_id)
            send_to_topic(phone, chunk, kind="paola" if source == "paola" else "bot")
            sent = True
            time.sleep(0.6)
        except Exception as exc:
            logger.exception("Errore invio WhatsApp %s: %s", phone, exc)
            send_to_topic(phone, f"Invio WhatsApp non riuscito: {exc}", kind="alert")
            send_private_alert(f"⚠️ Invio WhatsApp non riuscito per {phone}: {exc}")
            return False
    return sent


def get_meta_media(media_id: str) -> Tuple[bytes, str]:
    info = meta_api("GET", media_id)
    url = info["url"]
    mime_type = info.get("mime_type", "application/octet-stream")
    response = requests.get(url, headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"}, timeout=90)
    response.raise_for_status()
    return response.content, mime_type


def transcribe_audio(media_id: str) -> Optional[str]:
    try:
        data, mime = get_meta_media(media_id)
        extension = "ogg" if "ogg" in mime else "mp4" if "mp4" in mime else "audio"
        audio_file = io.BytesIO(data)
        audio_file.name = f"audio.{extension}"
        result = openai_client.audio.transcriptions.create(model=MODEL_AUDIO, file=audio_file)
        return (result.text or "").strip()
    except Exception as exc:
        logger.exception("Errore trascrizione audio: %s", exc)
        return None


def media_image_data_url(media_id: str) -> Optional[str]:
    try:
        data, mime = get_meta_media(media_id)
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as exc:
        logger.exception("Errore download immagine: %s", exc)
        return None


def extract_inbound_content(message: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
    msg_type = message.get("type", "")
    if msg_type == "text":
        return message.get("text", {}).get("body", "").strip(), None, None
    if msg_type == "audio":
        media_id = message.get("audio", {}).get("id")
        transcript = transcribe_audio(media_id) if media_id else None
        return transcript or "[messaggio vocale non comprensibile]", media_id, "audio"
    if msg_type == "image":
        image = message.get("image", {})
        caption = image.get("caption", "").strip()
        return caption or "[immagine]", image.get("id"), "image"
    if msg_type == "document":
        doc = message.get("document", {})
        return doc.get("caption", "").strip() or f"[documento ricevuto: {doc.get('filename', 'file')}]", doc.get("id"), "document"
    if msg_type == "video":
        video = message.get("video", {})
        return video.get("caption", "").strip() or "[video ricevuto]", video.get("id"), "video"
    if msg_type == "sticker":
        return "[sticker]", message.get("sticker", {}).get("id"), "sticker"
    if msg_type == "reaction":
        return "[reazione]", None, "reaction"
    return f"[messaggio WhatsApp di tipo {msg_type or 'sconosciuto'}]", None, msg_type or None


def iter_field_changes(payload: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            yield change.get("field", ""), change.get("value", {}) or {}


def extract_echo_messages(value: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parser tollerante per smb_message_echoes.

    Meta può evolvere la forma del payload. Il parser cerca in modo ricorsivo liste
    chiamate messages/message_echoes/echoes e restituisce i dizionari messaggio.
    Il tecnico dovrà validarlo su un payload reale della WABA dopo l'onboarding.
    """
    found: List[Dict[str, Any]] = []

    def walk(node: Any, parent_key: str = "") -> None:
        if isinstance(node, dict):
            if parent_key in {"messages", "message_echoes", "echoes"} and (
                "id" in node or "message_id" in node or "text" in node
            ):
                found.append(node)
            for key, val in node.items():
                walk(val, key)
        elif isinstance(node, list):
            if parent_key in {"messages", "message_echoes", "echoes"}:
                for item in node:
                    if isinstance(item, dict):
                        found.append(item)
                    else:
                        walk(item, parent_key)
            else:
                for item in node:
                    walk(item, parent_key)

    walk(value)
    unique = []
    seen = set()
    for item in found:
        key = item.get("id") or item.get("message_id") or json.dumps(item, sort_keys=True)[:200]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def echo_phone_and_text(message: Dict[str, Any], value: Dict[str, Any]) -> Tuple[str, str, str]:
    message_id = str(message.get("id") or message.get("message_id") or "")
    text_obj = message.get("text")
    if isinstance(text_obj, dict):
        text = str(text_obj.get("body") or "")
    else:
        text = str(text_obj or message.get("body") or message.get("message") or "")

    possible = [
        message.get("to"), message.get("recipient"), message.get("recipient_id"),
        message.get("wa_id"), message.get("customer_phone"),
    ]
    contacts = value.get("contacts") or []
    if contacts and isinstance(contacts, list):
        possible.append((contacts[0] or {}).get("wa_id"))
    phone = next((normalize_phone(x) for x in possible if x), "")
    return phone, text.strip(), message_id


# =============================================================================
# OPENAI RESPONSES API
# =============================================================================
def ai_text(
    *,
    model: str,
    system_prompts: List[str],
    user_text: str,
    history: Optional[List[Dict[str, str]]] = None,
    reasoning_effort: str = "low",
    verbosity: str = "low",
    max_output_tokens: int = 1200,
    image_data_url: Optional[str] = None,
) -> str:
    inputs: List[Dict[str, Any]] = []
    for prompt in system_prompts:
        inputs.append({"role": "system", "content": prompt})
    for item in history or []:
        inputs.append({"role": item["role"], "content": item["content"]})
    if image_data_url:
        inputs.append({
            "role": "user",
            "content": [
                {"type": "input_text", "text": user_text},
                {"type": "input_image", "image_url": image_data_url},
            ],
        })
    else:
        inputs.append({"role": "user", "content": user_text})

    response = openai_client.responses.create(
        model=model,
        input=inputs,
        reasoning={"effort": reasoning_effort},
        text={"verbosity": verbosity},
        max_output_tokens=max_output_tokens,
    )
    return (response.output_text or "").strip()


def classify_support_message(phone: str, pending_text: str) -> Dict[str, Any]:
    default = {
        "intent": "altro", "confidence": 0.0, "safe_auto_reply": True,
        "needs_human": False, "pause_chat": False, "response_depth": "normal",
        "reason": "fallback",
    }
    case = get_case(phone)
    context = f"""
Stato attuale: {case.get('status')}
Profilo:
{profile_to_text(case.get('profile_json') or {})}

Ultimi messaggi della mamma:
{pending_text}
""".strip()
    try:
        text = ai_text(
            model=MODEL_ROUTER,
            system_prompts=[ROUTER_PROMPT],
            user_text=context,
            reasoning_effort="none",
            verbosity="low",
            max_output_tokens=450,
        )
        result = safe_json_loads(text, default)
        for key, value in default.items():
            result.setdefault(key, value)
        return result
    except Exception as exc:
        logger.exception("Errore router %s: %s", phone, exc)
        return default


def extract_profile(phone: str) -> Dict[str, Any]:
    case = get_case(phone)
    history = get_recent_history(phone, 50)
    context = f"""
QUESTIONARIO:
{case.get('questionnaire') or ''}

PIANO:
{case.get('plan') or ''}

NOTE INTERNE:
{case.get('admin_notes') or ''}

STORICO RECENTE:
{json.dumps(history, ensure_ascii=False)}
""".strip()
    try:
        text = ai_text(
            model=MODEL_PROFILE,
            system_prompts=[PROFILE_PROMPT],
            user_text=context,
            reasoning_effort="low",
            verbosity="low",
            max_output_tokens=900,
        )
        profile = safe_json_loads(text, {})
        if isinstance(profile, dict):
            update_case(phone, profile_json=profile)
            return profile
    except Exception as exc:
        logger.exception("Errore profilo %s: %s", phone, exc)
    return case.get("profile_json") or {}


def generate_normal_reply(phone: str, pending_text: str, router: Dict[str, Any], forced_mode: Optional[str] = None) -> Optional[str]:
    case = get_case(phone)
    history = get_history_before_pending(phone, RECENT_HISTORY_LIMIT)
    profile = case.get("profile_json") or {}
    depth = router.get("response_depth", "normal")
    if depth not in {"micro", "normal", "deep"}:
        depth = "normal"
    effort = {"micro": "low", "normal": "low", "deep": "medium"}[depth]
    verbosity = "low" if depth in {"micro", "normal"} else "medium"
    max_tokens = {"micro": 350, "normal": 850, "deep": 1400}[depth]

    operational = f"""
CONTESTO DELLA CONSULENZA
Profilo:
{profile_to_text(profile)}

Questionario iniziale:
{case.get('questionnaire') or '[mancante]'}

Piano scritto da Paola:
{case.get('plan') or '[mancante]'}

Note interne di Paola:
{case.get('admin_notes') or '[nessuna]'}

Classificazione interna:
{json.dumps(router, ensure_ascii=False)}

Lunghezza richiesta: {depth}.
Se micro, usa normalmente 1-3 frasi. Se normal, resta essenziale. Se deep, approfondisci solo il necessario.
""".strip()
    prompts = [SYSTEM_PROMPT_BASE, operational]
    if forced_mode == "continua":
        prompts.append(FORCED_CONTINUE_PROMPT)

    image_data_url = None
    for item in reversed(get_pending_user_messages(phone)):
        if item.get("media_type") == "image" and item.get("media_id"):
            image_data_url = media_image_data_url(item["media_id"])
            break
    try:
        reply = ai_text(
            model=MODEL_CHAT,
            system_prompts=prompts,
            user_text=pending_text,
            history=history,
            reasoning_effort=effort,
            verbosity=verbosity,
            max_output_tokens=max_tokens,
            image_data_url=image_data_url,
        )
        clean = clean_reply(reply)
        return quality_control_reply(phone, pending_text, clean, router)
    except Exception as exc:
        logger.exception("Errore risposta AI %s: %s", phone, exc)
        send_to_topic(phone, f"Errore OpenAI: {exc}", kind="alert")
        send_private_alert(f"⚠️ Errore OpenAI per {phone}: {exc}")
        return None


def quality_control_reply(
    phone: str,
    pending_text: str,
    reply: str,
    router: Dict[str, Any],
) -> Optional[str]:
    if not reply:
        return None
    case = get_case(phone)
    context = f"""
MESSAGGIO DELLA MAMMA:
{pending_text}

RISPOSTA PROPOSTA:
{reply}

CLASSIFICAZIONE:
{json.dumps(router, ensure_ascii=False)}

PROFILO:
{profile_to_text(case.get('profile_json') or {})}

PIANO:
{case.get('plan') or ''}
""".strip()
    default = {"send": True, "rewrite": False, "reason": "fallback"}
    try:
        result_text = ai_text(
            model=MODEL_CLASSIFIER,
            system_prompts=[QUALITY_PROMPT],
            user_text=context,
            reasoning_effort="none",
            verbosity="low",
            max_output_tokens=300,
        )
        result = safe_json_loads(result_text, default)
        if not bool(result.get("send", True)):
            send_to_topic(phone, f"Risposta bloccata dal controllo qualità: {result.get('reason', '')}", kind="alert")
            return None
        if not bool(result.get("rewrite", False)):
            return reply
        rewritten = ai_text(
            model=MODEL_CHAT,
            system_prompts=[SYSTEM_PROMPT_BASE, QUALITY_REWRITE_PROMPT],
            user_text=f"""Messaggio mamma:
{pending_text}

Risposta da correggere:
{reply}""",
            reasoning_effort="low",
            verbosity="low",
            max_output_tokens=900,
        )
        return clean_reply(rewritten)
    except Exception as exc:
        logger.exception("Errore controllo qualità %s: %s", phone, exc)
        return reply


def clean_reply(reply: str) -> str:
    clean = (reply or "").strip().replace("!", ".")
    clean = re.sub(r"\*\*|__|^#{1,6}\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\bcara\b", "mamma", clean, flags=re.IGNORECASE)
    forbidden = [
        "consulenza scaduta", "consulenza terminata", "rinnovo", "offerta",
        "alert telegram", "sono un'intelligenza artificiale", "sono una intelligenza artificiale",
    ]
    if any(term in clean.lower() for term in forbidden):
        logger.warning("Risposta bloccata per frase vietata: %s", clean[:300])
        return ""
    return clean


def generate_checkup(phone: str) -> Optional[str]:
    case = get_case(phone)
    context = f"""
PROFILO:
{profile_to_text(case.get('profile_json') or {})}

QUESTIONARIO:
{case.get('questionnaire') or ''}

PIANO:
{case.get('plan') or ''}

STORICO RECENTE:
{json.dumps(get_recent_history(phone, 45), ensure_ascii=False)}
""".strip()
    try:
        return clean_reply(ai_text(
            model=MODEL_CHECKUP,
            system_prompts=[SYSTEM_PROMPT_BASE, CHECKUP_PROMPT],
            user_text=context,
            reasoning_effort="medium",
            verbosity="medium",
            max_output_tokens=1500,
        ))
    except Exception as exc:
        logger.exception("Errore checkup %s: %s", phone, exc)
        return None


def classify_checkup(phone: str) -> Dict[str, Any]:
    default = {"status": "incomplete", "confidence": 0.0, "missing": "", "reason": "fallback"}
    case = get_case(phone)
    started = case.get("checkup_started_at")
    if not started:
        return default
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT content FROM messages
        WHERE phone = %s AND role = 'user' AND timestamp > %s
        ORDER BY timestamp ASC, id ASC
    """, (phone, started))
    text = "\n".join(row[0] for row in cur.fetchall())
    cur.close()
    conn.close()
    try:
        output = ai_text(
            model=MODEL_CLASSIFIER,
            system_prompts=[CHECKUP_CLASSIFIER_PROMPT],
            user_text=text,
            reasoning_effort="none",
            verbosity="low",
            max_output_tokens=350,
        )
        result = safe_json_loads(output, default)
        for key, val in default.items():
            result.setdefault(key, val)
        return result
    except Exception as exc:
        logger.exception("Errore classificazione checkup %s: %s", phone, exc)
        return default


def generate_revision(phone: str) -> Optional[str]:
    case = get_case(phone)
    context = f"""
PROFILO:
{profile_to_text(case.get('profile_json') or {})}

QUESTIONARIO:
{case.get('questionnaire') or ''}

PIANO INIZIALE:
{case.get('plan') or ''}

NOTE INTERNE:
{case.get('admin_notes') or ''}

STORICO E RISPOSTE AL CHECKUP:
{json.dumps(get_recent_history(phone, 70), ensure_ascii=False)}
""".strip()
    try:
        return clean_reply(ai_text(
            model=MODEL_REVISION,
            system_prompts=[SYSTEM_PROMPT_BASE, REVISION_PROMPT],
            user_text=context,
            reasoning_effort="high",
            verbosity="medium",
            max_output_tokens=2400,
        ))
    except Exception as exc:
        logger.exception("Errore revisione %s: %s", phone, exc)
        return None


# =============================================================================
# TIMER E RISPOSTE
# =============================================================================
def schedule_response(phone: str, latest_text: str, recovery: bool = False) -> None:
    if is_quiet_hours():
        logger.info("Orario silenzio: nessun timer per %s", phone)
        return
    with active_timers_lock:
        if phone in active_timers:
            logger.info("Timer già attivo per %s", phone)
            return
        if recovery:
            delay = random.randint(RECOVERY_DELAY_MIN_SECONDS, RECOVERY_DELAY_MAX_SECONDS)
        elif is_immediate_question(latest_text):
            delay = random.randint(IMMEDIATE_DELAY_MIN_SECONDS, IMMEDIATE_DELAY_MAX_SECONDS)
        else:
            delay = random.randint(NORMAL_DELAY_MIN_SECONDS, NORMAL_DELAY_MAX_SECONDS)
        timer = threading.Timer(delay, process_response, args=(phone,))
        timer.daemon = True
        active_timers[phone] = timer
        timer.start()
        logger.info("Timer avviato per %s: %ss", phone, delay)


def schedule_checkup_review(phone: str) -> None:
    with active_timers_lock:
        if phone in active_timers:
            return
        timer = threading.Timer(CHECKUP_REVIEW_DELAY_SECONDS, process_checkup_review, args=(phone,))
        timer.daemon = True
        active_timers[phone] = timer
        timer.start()


def process_checkup_review(phone: str) -> None:
    with active_timers_lock:
        active_timers.pop(phone, None)
    case = get_case(phone)
    if case.get("status") != STATUS_CHECKUP:
        return
    result = classify_checkup(phone)
    status = result.get("status")
    confidence = float(result.get("confidence") or 0)
    if status == "defer" and confidence >= 0.55:
        return
    if status == "sufficient" and confidence >= 0.55 and not case.get("checkup_ready_alert_sent"):
        update_case(phone, status=STATUS_REVIEW, checkup_ready_alert_sent=True, last_alert_at=now_local())
        text = (
            "📝 CHECKUP COMPLETATO\n\n"
            "Le risposte sono sufficienti per una revisione. La revisione non è stata inviata automaticamente.\n\n"
            "Usa /revisione per inviarla, /continua per tornare al supporto normale oppure rispondi manualmente."
        )
        send_to_topic(phone, text, kind="alert")
        send_private_alert(f"📝 Checkup pronto per {phone}. Apri il topic e scegli /revisione o /continua.")
        add_alert(phone, "checkup_ready", "info", result.get("reason", ""))
    elif status == "incomplete":
        send_to_topic(
            phone,
            f"Checkup ancora incompleto. Dati mancanti secondo il controllo: {result.get('missing') or 'non specificati'}. Il bot resta in attesa.",
            kind="system",
        )


def process_response(phone: str) -> None:
    with active_timers_lock:
        active_timers.pop(phone, None)
    case = get_case(phone)
    if case.get("status") != STATUS_ACTIVE:
        logger.info("Nessuna risposta per %s: stato %s", phone, case.get("status"))
        return
    pending = get_pending_user_messages(phone)
    pending_text = "\n".join(item["content"] for item in pending).strip()
    if not pending_text:
        return
    if is_obvious_closing_message(pending_text):
        mark_silent_no_reply(phone, "chiusura rilevata dal timer")
        logger.info("Chiusura/cortesia: nessuna risposta per %s", phone)
        return

    router = classify_support_message(phone, pending_text)
    if router.get("needs_human") or router.get("pause_chat"):
        update_case(phone, status=STATUS_REVIEW, last_alert_at=now_local())
        category = router.get("intent", "alert")
        reason = router.get("reason", "")
        add_alert(phone, category, "high", reason)
        alert = (
            f"{category}\n\nMotivo: {reason}\n\nMessaggio della mamma:\n{pending_text}\n\n"
            "La chat è stata messa in pausa. Puoi rispondere manualmente, usare /continua oppure /riprendi."
        )
        send_to_topic(phone, alert, kind="alert")
        send_private_alert(f"⚠️ Alert per {phone}: {category}. La chat è in pausa.")
        return

    reply = generate_normal_reply(phone, pending_text, router)
    if reply:
        send_whatsapp_message(phone, reply, source="bot")


def expiration_worker() -> None:
    while True:
        try:
            conn = get_db()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                UPDATE support_cases
                SET expiration_alert_sent = TRUE, updated_at = NOW()
                WHERE phone IN (
                    SELECT phone FROM support_cases
                    WHERE support_end_at IS NOT NULL
                      AND support_end_at <= NOW()
                      AND expiration_alert_sent = FALSE
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING phone, display_name, activated_at, support_end_at, status
            """)
            expired = [dict(row) for row in cur.fetchall()]
            conn.commit()
            cur.close()
            conn.close()
            for item in expired:
                phone = item["phone"]
                text = (
                    "⏰ CONSULENZA TERMINATA\n\n"
                    f"Mamma: {item.get('display_name') or '-'}\n"
                    f"Telefono: {phone}\n"
                    f"Attivazione: {format_dt(item.get('activated_at'))}\n"
                    f"Scadenza: {format_dt(item.get('support_end_at'))}\n\n"
                    "Il bot continua a rispondere normalmente. La mamma non ha ricevuto alcun avviso. "
                    "Intervieni tu quando vuoi fare l'offerta."
                )
                send_to_topic(phone, text, kind="alert")
                send_private_alert(text)
                add_alert(phone, "support_expired", "info", "30 giorni trascorsi; bot ancora attivo")
        except Exception as exc:
            logger.exception("Errore controllo scadenze: %s", exc)
        time.sleep(EXPIRATION_POLL_SECONDS)


def pending_recovery_worker() -> None:
    while True:
        try:
            if not is_quiet_hours():
                conn = get_db()
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute("""
                    SELECT c.phone, c.status
                    FROM support_cases c
                    WHERE c.status IN (%s, %s)
                      AND EXISTS (
                          SELECT 1 FROM messages u
                          WHERE u.phone = c.phone
                            AND u.role = 'user'
                            AND u.timestamp > COALESCE(
                                (SELECT MAX(a.timestamp) FROM messages a
                                 WHERE a.phone = c.phone AND a.role IN ('assistant', 'admin')),
                                NOW() - INTERVAL '45 days'
                            )
                      )
                """, (STATUS_ACTIVE, STATUS_CHECKUP))
                rows = [dict(r) for r in cur.fetchall()]
                cur.close()
                conn.close()
                for row in rows:
                    phone = row["phone"]
                    with active_timers_lock:
                        has_timer = phone in active_timers
                    if has_timer:
                        continue
                    if row["status"] == STATUS_CHECKUP:
                        schedule_checkup_review(phone)
                    else:
                        pending = get_pending_user_messages(phone)
                        pending_text = "\n".join(x.get("content", "") for x in pending).strip()
                        if pending_text:
                            if is_obvious_closing_message(pending_text):
                                mark_silent_no_reply(phone, "recupero messaggio notturno di cortesia")
                            else:
                                schedule_response(phone, pending_text, recovery=True)
        except Exception as exc:
            logger.exception("Errore recupero timer pendenti: %s", exc)
        time.sleep(RECOVERY_POLL_SECONDS)


def start_recovery_worker_once() -> None:
    global recovery_worker_started
    with recovery_worker_lock:
        if recovery_worker_started:
            return
        thread = threading.Thread(target=pending_recovery_worker, daemon=True, name="pending-recovery-worker")
        thread.start()
        recovery_worker_started = True


def start_expiration_worker_once() -> None:
    global expiration_worker_started
    with expiration_worker_lock:
        if expiration_worker_started:
            return
        thread = threading.Thread(target=expiration_worker, daemon=True, name="expiration-worker")
        thread.start()
        expiration_worker_started = True


def format_dt(value: Optional[datetime]) -> str:
    if not value:
        return "-"
    if value.tzinfo is None:
        value = pytz.UTC.localize(value)
    return value.astimezone(TZ).strftime("%d/%m/%Y %H:%M")


# =============================================================================
# HANDLER MESSAGGI META
# =============================================================================
def handle_inbound_message(message: Dict[str, Any], value: Dict[str, Any]) -> None:
    phone = normalize_phone(message.get("from"))
    if not phone:
        return
    contacts = value.get("contacts") or []
    display_name = None
    if contacts:
        display_name = ((contacts[0] or {}).get("profile") or {}).get("name")
    case = ensure_case(phone, display_name)
    get_or_create_topic(phone, display_name)

    content, media_id, media_type = extract_inbound_content(message)
    provider_id = message.get("id")
    inserted = save_message(
        phone, "user", "whatsapp", content,
        provider_message_id=provider_id, media_id=media_id, media_type=media_type,
    )
    if not inserted:
        return
    send_to_topic(phone, content, kind="mother")

    status = case.get("status", STATUS_PAUSED)
    if status in {STATUS_PAUSED, STATUS_REVIEW, STATUS_CLOSED}:
        return
    if status == STATUS_CHECKUP:
        if not is_obvious_closing_message(content):
            schedule_checkup_review(phone)
        return
    if status != STATUS_ACTIVE:
        return
    if media_type == "video":
        send_whatsapp_message(phone, "Non riesco a vedere i video, scrivimi pure qui in chat.", source="bot")
        return
    if is_obvious_closing_message(content) or media_type in {"sticker", "reaction"}:
        with active_timers_lock:
            has_timer = phone in active_timers
        if not has_timer:
            pending = get_pending_user_messages(phone)
            pending_text = "\n".join(x.get("content", "") for x in pending).strip()
            if pending_text and is_obvious_closing_message(pending_text):
                mark_silent_no_reply(phone, "chiusura breve senza timer attivo")
        return
    schedule_response(phone, content)


def handle_manual_echo(value: Dict[str, Any]) -> None:
    messages = extract_echo_messages(value)
    for item in messages:
        phone, text, message_id = echo_phone_and_text(item, value)
        if not phone or not text:
            continue
        ensure_case(phone)
        cancel_timer(phone, "risposta manuale dall'app WhatsApp Business")
        inserted = save_message(phone, "admin", "whatsapp_business_app", text, provider_message_id=message_id or None)
        if inserted:
            send_to_topic(phone, text, kind="paola")


def process_meta_payload(payload: Dict[str, Any]) -> None:
    for field, value in iter_field_changes(payload):
        if field == "messages":
            for message in value.get("messages", []) or []:
                handle_inbound_message(message, value)
        elif field == "smb_message_echoes":
            handle_manual_echo(value)
        elif field == "history":
            logger.info("Webhook history ricevuto; non importato automaticamente in questa versione")


# =============================================================================
# COMANDI TELEGRAM
# =============================================================================
def telegram_admin_allowed(message: Dict[str, Any]) -> bool:
    sender_id = int((message.get("from") or {}).get("id") or 0)
    return sender_id in TELEGRAM_ALLOWED_ADMIN_IDS


def case_status_text(phone: str) -> str:
    case = get_case(phone)
    return (
        f"Stato bot: {case.get('status')}\n"
        f"Questionario: {'presente' if case.get('questionnaire') else 'mancante'}\n"
        f"Piano: {'presente' if case.get('plan') else 'mancante'}\n"
        f"Attivazione: {format_dt(case.get('activated_at'))}\n"
        f"Scadenza: {format_dt(case.get('support_end_at'))}\n"
        f"Alert scadenza inviato: {'sì' if case.get('expiration_alert_sent') else 'no'}\n"
        f"Modalità caricamento: {case.get('capture_mode')}"
    )


def handle_capture_message(phone: str, message: Dict[str, Any]) -> bool:
    case = get_case(phone)
    mode = case.get("capture_mode", CAPTURE_NONE)
    if mode == CAPTURE_NONE:
        return False
    text = (message.get("text") or message.get("caption") or "").strip()
    document = message.get("document")
    if document:
        try:
            data, file_path = download_telegram_file(document["file_id"])
            filename = document.get("file_name") or file_path
            extracted = extract_document_text(data, filename)
            if text:
                extracted = f"{text}\n\n{extracted}".strip()
            append_capture_buffer(phone, extracted)
            send_to_topic(phone, f"Documento aggiunto a {mode}: {len(extracted)} caratteri.", kind="system")
        except Exception as exc:
            send_to_topic(phone, f"Non sono riuscito a leggere il documento: {exc}", kind="alert")
        return True
    if text and not text.startswith("/"):
        append_capture_buffer(phone, text)
        send_to_topic(phone, f"Parte aggiunta a {mode}: {len(text)} caratteri.", kind="system")
        return True
    return False


def handle_telegram_command(phone: str, text: str) -> None:
    command, *rest = text.strip().split(maxsplit=1)
    cmd = command.lower().split("@", 1)[0]
    argument = rest[0].strip() if rest else ""

    if cmd == "/template":
        case = get_case(phone)
        display_name = (case.get("display_name") or "Mamma").strip()
        sent, error, mode = send_consulenza_template_auto(phone, display_name)
        if sent:
            send_to_topic(
                phone,
                f"✅ Template {META_TEMPLATE_CONSULENZA} inviato ({mode}).",
                kind="system",
            )
        else:
            send_to_topic(
                phone,
                f"Template non inviato. Errore Meta: {error}",
                kind="alert",
            )
        return

    if cmd == "/questionario":
        update_case(phone, capture_mode=CAPTURE_QUESTIONNAIRE, capture_buffer="")
        send_to_topic(phone, "Modalità questionario attiva. Incolla uno o più messaggi oppure un file TXT/PDF/DOCX, poi usa /fine.", kind="system")
        return
    if cmd == "/piano":
        update_case(phone, capture_mode=CAPTURE_PLAN, capture_buffer="")
        send_to_topic(phone, "Modalità piano attiva. Incolla uno o più messaggi oppure un file TXT/PDF/DOCX, poi usa /fine.", kind="system")
        return
    if cmd == "/fine":
        case = get_case(phone)
        mode = case.get("capture_mode")
        buffer = (case.get("capture_buffer") or "").strip()
        if mode not in {CAPTURE_QUESTIONNAIRE, CAPTURE_PLAN}:
            send_to_topic(phone, "Non c'è un caricamento attivo.", kind="system")
            return
        if not buffer:
            send_to_topic(phone, "Non ho ricevuto contenuti. Il caricamento resta aperto.", kind="alert")
            return
        field = "questionnaire" if mode == CAPTURE_QUESTIONNAIRE else "plan"
        update_case(phone, **{field: buffer, "capture_mode": CAPTURE_NONE, "capture_buffer": ""})
        send_to_topic(phone, f"✅ {('Questionario' if field == 'questionnaire' else 'Piano')} salvato: {len(buffer)} caratteri.", kind="system")
        return
    if cmd == "/attiva":
        case = get_case(phone)
        missing = []
        if not (case.get("questionnaire") or "").strip():
            missing.append("questionario")
        if not (case.get("plan") or "").strip():
            missing.append("piano")
        if missing:
            send_to_topic(phone, f"Non posso attivare: manca {', '.join(missing)}.", kind="alert")
            return
        cancel_timer(phone, "attivazione")
        profile = extract_profile(phone)
        activated = now_local()
        end_at = activated + timedelta(days=SUPPORT_DURATION_DAYS)
        update_case(
            phone,
            status=STATUS_ACTIVE,
            activated_at=activated,
            support_end_at=end_at,
            expiration_alert_sent=False,
            checkup_started_at=None,
            checkup_ready_alert_sent=False,
        )
        send_to_topic(
            phone,
            "✅ SUPPORTO ATTIVATO\n\n"
            f"Attivazione: {format_dt(activated)}\n"
            f"Alert fine consulenza: {format_dt(end_at)}\n"
            f"Profilo estratto: {'sì' if profile else 'parziale'}\n"
            "Il bot continuerà a rispondere anche dopo l'alert dei 30 giorni.",
            kind="system",
        )
        return
    if cmd == "/pausa":
        cancel_timer(phone, "pausa manuale")
        update_case(phone, status=STATUS_PAUSED)
        send_to_topic(phone, "⏸️ Chat in pausa. I messaggi vengono salvati e copiati qui, ma il bot non risponde.", kind="system")
        return
    if cmd == "/riprendi":
        cancel_timer(phone, "ripresa")
        update_case(phone, status=STATUS_ACTIVE)
        send_to_topic(phone, "▶️ Supporto automatico ripreso.", kind="system")
        return
    if cmd == "/checkup":
        cancel_timer(phone, "checkup")
        extract_profile(phone)
        checkup = generate_checkup(phone)
        if not checkup:
            send_to_topic(phone, "Non sono riuscito a generare il checkup. Riprova tra poco.", kind="alert")
            return
        if send_whatsapp_message(phone, checkup, source="bot"):
            update_case(
                phone,
                status=STATUS_CHECKUP,
                checkup_started_at=now_local(),
                checkup_ready_alert_sent=False,
            )
            send_to_topic(phone, "Checkup inviato. Il bot raccoglie le risposte e non invierà una revisione senza /revisione.", kind="system")
        return
    if cmd == "/revisione":
        cancel_timer(phone, "revisione")
        extract_profile(phone)
        revision = generate_revision(phone)
        if not revision:
            send_to_topic(phone, "Non sono riuscito a generare la revisione.", kind="alert")
            return
        if send_whatsapp_message(phone, revision, source="bot"):
            update_case(phone, status=STATUS_ACTIVE, checkup_started_at=None, checkup_ready_alert_sent=False)
            send_to_topic(phone, "✅ Revisione inviata e supporto normale riattivato.", kind="system")
        return
    if cmd in {"/continua", "/rispondi"}:
        cancel_timer(phone, cmd)
        pending = get_pending_user_messages(phone)
        pending_text = "\n".join(item["content"] for item in pending).strip()
        if not pending_text:
            send_to_topic(phone, "Non ci sono nuovi messaggi della mamma a cui rispondere.", kind="system")
            return
        router = classify_support_message(phone, pending_text)
        mode = "continua" if cmd == "/continua" else None
        reply = generate_normal_reply(phone, pending_text, router, forced_mode=mode)
        if reply and send_whatsapp_message(phone, reply, source="bot"):
            update_case(phone, status=STATUS_ACTIVE)
        return
    if cmd in {"/rinnova30", "/rinnova60"}:
        days = 30 if cmd == "/rinnova30" else 60
        start = now_local()
        end_at = start + timedelta(days=days)
        update_case(
            phone,
            status=STATUS_ACTIVE,
            activated_at=start,
            support_end_at=end_at,
            expiration_alert_sent=False,
        )
        send_to_topic(phone, f"🔁 Rinnovo registrato per {days} giorni. Nuovo alert: {format_dt(end_at)}.", kind="system")
        return
    if cmd == "/stato":
        send_to_topic(phone, case_status_text(phone), kind="system")
        return
    if cmd == "/nota":
        if not argument:
            send_to_topic(phone, "Scrivi /nota seguito dal testo.", kind="system")
            return
        case = get_case(phone)
        current = case.get("admin_notes") or ""
        new_notes = f"{current}\n{format_dt(now_local())}: {argument}".strip()
        update_case(phone, admin_notes=new_notes)
        save_message(phone, "system", "telegram_note", f"[NOTA PAOLA: {argument}]")
        send_to_topic(phone, "Nota interna salvata.", kind="system")
        return
    if cmd == "/chiudi":
        cancel_timer(phone, "chiusura")
        update_case(phone, status=STATUS_CLOSED)
        send_to_topic(phone, "⛔ Supporto automatico chiuso. Nessun messaggio è stato inviato alla mamma.", kind="system")
        return
    send_to_topic(phone, f"Comando non riconosciuto: {cmd}", kind="system")



def send_to_telegram_context(message: Dict[str, Any], text: str) -> bool:
    payload: Dict[str, Any] = {
        "chat_id": TELEGRAM_GROUP_ID,
        "text": text,
    }
    thread_id = message.get("message_thread_id")
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    try:
        telegram_api("sendMessage", json_data=payload)
        return True
    except Exception as exc:
        logger.exception("Errore risposta al comando Telegram: %s", exc)
        return False


def get_topic_thread_id(phone: str) -> Optional[int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT thread_id FROM telegram_topics WHERE phone = %s", (phone,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return int(row[0]) if row else None


def configured_sender_phone() -> str:
    """Restituisce il numero mittente Meta, quando leggibile."""
    try:
        data = get_configured_phone_number()
        return normalize_phone(data.get("display_phone_number"))
    except Exception:
        return ""


def handle_new_case_command(message: Dict[str, Any], text: str) -> None:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        send_to_telegram_context(
            message,
            "Uso corretto: /nuova +393331234567 NomeMamma",
        )
        return

    phone = normalize_phone(parts[1])
    display_name = parts[2].strip() if len(parts) >= 3 else "Mamma"

    if len(phone_for_meta(phone)) < 10:
        send_to_telegram_context(
            message,
            "Numero non valido. Usa il prefisso internazionale, per esempio +393331234567.",
        )
        return

    current_thread = message.get("message_thread_id")
    if current_thread and get_thread_phone(int(current_thread)):
        send_to_telegram_context(
            message,
            "Scrivi /nuova nel topic Generale, non nel topic di una mamma.",
        )
        return

    sender_phone = configured_sender_phone()
    if sender_phone and phone_for_meta(sender_phone) == phone_for_meta(phone):
        send_to_telegram_context(
            message,
            "Questo è lo stesso numero mittente collegato a Meta. "
            "Per provare il template usa un altro numero WhatsApp.",
        )
        return

    ensure_case(phone, display_name)
    update_case(phone, status=STATUS_PAUSED)

    thread_id = get_or_create_topic(phone, display_name)
    if not thread_id:
        send_to_telegram_context(
            message,
            f"Non sono riuscito a creare o recuperare il topic per {phone}.",
        )
        return

    sent, error, mode = send_consulenza_template_auto(phone, display_name)
    if not sent:
        send_to_telegram_context(
            message,
            f"✅ Topic creato per {display_name}, ma il template non è partito.\n\n"
            f"Errore Meta:\n{error}",
        )
        send_to_topic(
            phone,
            f"Template non inviato. Errore Meta: {error}\n"
            "Puoi riprovare da questo topic con /template.",
            kind="alert",
        )
        return

    send_to_telegram_context(
        message,
        f"✅ {display_name} creata in pausa.\n"
        f"Topic Telegram pronto.\n"
        f"Template {META_TEMPLATE_CONSULENZA} inviato a {phone} ({mode}).",
    )
    send_to_topic(
        phone,
        "Template iniziale inviato. La chat resta in pausa finché non usi /attiva.",
        kind="system",
    )


def process_telegram_update(update: Dict[str, Any]) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    if not message or (message.get("from") or {}).get("is_bot"):
        return

    chat = message.get("chat") or {}
    if str(chat.get("id")) != str(TELEGRAM_GROUP_ID):
        return
    if not telegram_admin_allowed(message):
        return

    text = (message.get("text") or message.get("caption") or "").strip()
    command = text.split(maxsplit=1)[0].lower().split("@", 1)[0] if text else ""

    if command == "/version":
        send_to_telegram_context(message, f"Versione attiva: {APP_BUILD}")
        return

    if command == "/nuova":
        handle_new_case_command(message, text)
        return

    thread_id = message.get("message_thread_id")
    if not thread_id:
        return

    phone = get_thread_phone(int(thread_id))
    if not phone:
        return

    if text.startswith("/"):
        handle_telegram_command(phone, text)
        return
    if handle_capture_message(phone, message):
        return

    if text:
        cancel_timer(phone, "risposta manuale da Telegram")
        send_whatsapp_message(phone, text, source="paola")


def setup_telegram_webhook_if_configured() -> None:
    base = public_base_url()
    if not base:
        logger.info("PUBLIC_BASE_URL non impostata: webhook Telegram da configurare manualmente")
        return
    try:
        payload: Dict[str, Any] = {
            "url": f"{base}/telegram_webhook",
            "allowed_updates": ["message", "edited_message"],
            "drop_pending_updates": False,
        }
        if TELEGRAM_WEBHOOK_SECRET:
            payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
        telegram_api("setWebhook", json_data=payload)
        logger.info("Webhook Telegram configurato su %s/telegram_webhook", base)
    except Exception as exc:
        logger.exception("Errore configurazione webhook Telegram: %s", exc)


# =============================================================================
# ROUTES
# =============================================================================
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "supporto-fase4-meta",
        "build": APP_BUILD,
        "time": now_local().isoformat(),
        "meta_webhook": meta_webhook_url(),
        "phone_number_id": META_PHONE_NUMBER_ID,
    })


@app.route("/meta_webhook", methods=["GET"])
def meta_webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return Response(challenge or "", status=200, mimetype="text/plain")
    return Response("Forbidden", status=403)


@app.route("/meta_webhook", methods=["POST"])
def meta_webhook_receive():
    raw = request.get_data(cache=True)
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_meta_signature(raw, signature):
        logger.warning("Firma Meta non valida")
        return Response("Forbidden", status=403)
    payload = request.get_json(silent=True) or {}
    threading.Thread(target=process_meta_payload, args=(payload,), daemon=True).start()
    return Response("EVENT_RECEIVED", status=200)


@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    if TELEGRAM_WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(received, TELEGRAM_WEBHOOK_SECRET):
            return Response("Forbidden", status=403)
    update = request.get_json(silent=True) or {}
    threading.Thread(target=process_telegram_update, args=(update,), daemon=True).start()
    return Response("OK", status=200)


@app.route("/admin/meta/status", methods=["GET"])
def admin_meta_status():
    if not admin_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify({"ok": True, "meta": meta_setup_status()})


@app.route("/admin/meta/setup", methods=["POST"])
def admin_meta_setup():
    if not admin_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        result = subscribe_waba_to_app()
        return jsonify({"ok": True, "subscribed": result, "meta": meta_setup_status()})
    except Exception as exc:
        logger.exception("Errore setup Meta: %s", exc)
        return jsonify({"ok": False, "error": str(exc), "meta": meta_setup_status()}), 500


@app.route("/admin/meta/test", methods=["POST"])
def admin_meta_test_message():
    if not admin_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    phone = normalize_phone(payload.get("to", ""))
    text = (payload.get("text") or "Test connessione WhatsApp Cloud API - Genitori in Armonia").strip()
    use_template = payload.get("template", True)
    template_params = payload.get("template_params") or payload.get("parameters")
    if template_params and not isinstance(template_params, list):
        template_params = None
    if not phone:
        return jsonify({"ok": False, "error": "Campo 'to' obbligatorio, es. +393331234567"}), 400
    error = None
    if use_template:
        sent, error = send_consulenza_template(phone, template_params)
        if sent:
            return jsonify({
                "ok": True,
                "to": phone,
                "mode": f"template:{META_TEMPLATE_CONSULENZA}:{META_TEMPLATE_CONSULENZA_LANG}",
                "meta": meta_setup_status(),
            })
    recipient = phone_for_meta(phone)
    try:
        meta_api("POST", f"{META_PHONE_NUMBER_ID}/messages", json_data={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        })
        return jsonify({"ok": True, "to": phone, "mode": "text", "meta": meta_setup_status()})
    except Exception as exc:
        error = str(exc)
    return jsonify({"ok": False, "to": phone, "error": error, "hint": "Aggiungi il numero come destinatario di test in Meta Passaggio 1, oppure invia prima un messaggio al numero +1 555-647-0518", "meta": meta_setup_status()}), 502


@app.route("/admin/reload_profile/<path:phone>", methods=["POST"])
def admin_reload_profile(phone: str):
    # Endpoint tecnico: proteggerlo a livello Railway/rete se viene usato.
    normalized = normalize_phone(phone)
    if not normalized:
        return jsonify({"ok": False, "error": "phone non valido"}), 400
    return jsonify({"ok": True, "profile": extract_profile(normalized)})


# =============================================================================
# AVVIO
# =============================================================================
logger.info("Avvio build %s", APP_BUILD)
init_db()
setup_telegram_webhook_if_configured()
start_expiration_worker_once()
start_recovery_worker_once()
ensure_meta_subscriptions()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
