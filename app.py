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

APP_BUILD = "2026-08-06-clarification-depth-v20"


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
META_TEMPLATE_CONSULENZA = os.environ.get("META_TEMPLATE_CONSULENZA", "consulenza").strip()
META_TEMPLATE_CONSULENZA_LANG = os.environ.get("META_TEMPLATE_CONSULENZA_LANG", "it").strip()

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
RECENT_HISTORY_LIMIT = int(os.environ.get("RECENT_HISTORY_LIMIT", "40"))
MEMORY_GAP_DAYS = int(os.environ.get("MEMORY_GAP_DAYS", "3"))
MEMORY_SUMMARY_MIN_MESSAGES = int(os.environ.get("MEMORY_SUMMARY_MIN_MESSAGES", "4"))
MEMORY_SUMMARY_REFRESH_MESSAGES = int(os.environ.get("MEMORY_SUMMARY_REFRESH_MESSAGES", "8"))
MEMORY_QUESTIONNAIRE_MAX_CHARS = int(os.environ.get("MEMORY_QUESTIONNAIRE_MAX_CHARS", "10000"))
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
Sii sempre empatica con le mamme: riconosci la fatica, lo sforzo e le emozioni dietro ciò che scrivono, anche quando dai indicazioni pratiche.
L'empatia deve essere autentica e specifica al loro messaggio, non una frase fatta ripetuta a ogni turno.
Non usare punti esclamativi.
Non usare "cara". Usa "mamma" solo quando suona naturale.
Usa al massimo una emoji quando serve.
Non usare markdown, titoli, grassetto, tabelle o elenchi nelle risposte ordinarie.
Evita formule automatiche come "Grazie per aver condiviso", "Capisco perfettamente", "Vediamo insieme" o "Ecco cosa devi fare".
Evita termini tecnici come "associazione seno-sonno", "igiene del sonno" o "stimolazione cognitiva".
Puoi usare espressioni naturali come "guarda", "ti dico", "secondo me", "io manterrei", "per ora farei così".

FASE 4
Usa questionario, piano di Paola, profilo, riepilogo memoria, note interne e storico recente come base di lavoro.
Il piano consegnato è il punto di partenza, non un copione fisso: agisci come una vera consulente del sonno.
Se cambiano le circostanze (miglioramenti, peggioramenti, malattia, viaggio, cambio routine, nuovi dubbi), adatta le indicazioni al momento.
Non devi attenersi rigidamente al piano se la situazione reale richiede un aggiustamento prudente e graduale.
Non generare un nuovo piano completo da zero e non ricominciare l'analisi da zero.
Rispondi prima a ciò che la mamma ha realmente scritto.
Non sei obbligata a dare un consiglio in ogni messaggio.
Se racconta un miglioramento, valorizzalo in modo specifico e fermati se non serve altro.
Se dopo alcuni giorni dall'applicazione del metodo la mamma dice di essere soddisfatta e racconta miglioramenti importanti e concreti, puoi proporle con naturalezza, senza pressione, se vuole lasciare una recensione che per te ha un grande valore, e le lasci il link.
Non fare questa richiesta dopo un solo giorno né al primo segnale positivo isolato: proponila solo quando sono passati diversi giorni di applicazione e lei stessa racconta progressi significativi e soddisfazione reale.
Link recensione: https://www.trustpilot.com/review/genitorinarmonia.com
Se racconta un piccolo passo indietro, normalizzalo senza colpevolizzarla.
Se si sfoga, riconosci prima la fatica reale e non trasformare lo sfogo in un interrogatorio.
Se fa una domanda pratica e hai già abbastanza contesto, rispondi direttamente.
Se per dare un'indicazione appropriata ti mancano dettagli sulla dinamica attuale, puoi fare una o due domande mirate per capire meglio e poi valutare con più completezza.
Non fare domande in ogni messaggio: chiedi solo dove è davvero necessario e ti sembra opportuno; dove non serve, rispondi normalmente senza interrogare.
Dai massimo una o due indicazioni alla volta e non cambiare troppi elementi insieme.
Mantieni gradualità e coerenza, ma correggi la rotta quando serve.
Non chiedere mai dati già presenti nel questionario, nel piano, nel profilo o nello storico.
Evita domande a catena, domande generiche o di abitudine alla fine del messaggio (tipo "aggiornami", "fammi sapere", "come è andata?", "a che ora?").

LUNGHEZZA
Adatta sempre la lunghezza della risposta al messaggio della mamma.
Se scrive un messaggio lungo e completo, con più dettagli e contesto, puoi rispondere in modo più articolato e completo.
Se scrive un messaggio breve su una cosa del momento, rispondi in modo breve e puntuale.
Non allungare artificialmente risposte semplici e non troncare con superficialità messaggi articolati che meritano più attenzione.
Se per rispondere bene ti mancano informazioni, fai una o due domande naturali e mirate; poi, con il quadro più chiaro, dai un'indicazione più completa.
Un aggiornamento breve richiede normalmente 1-3 frasi.
Un messaggio articolato può richiedere una risposta media o più sviluppata, sempre leggibile su WhatsApp.
Quando la mamma chiede chiarimenti su un'indicazione precedente o esprime dubbi su orari e regole, puoi usare 5-8 frasi per spiegare il ragionamento e offrire scenari possibili.
Taglia ripetizioni e rassicurazioni generiche, ma non tagliare le spiegazioni del perché quando la mamma ha bisogno di capire come applicare un consiglio nella pratica.

DOMANDE DI CHIARIMENTO
Quando la mamma chiede chiarimenti su qualcosa che le hai già detto, esprime confusione su orari o regole, o dice "come faccio a...?", non limitarti a una risposta secca con un solo consiglio operativo.
Riconosci che il dubbio è normale e spiega brevemente il ragionamento dietro l'indicazione: non è una regola rigida, ma un riferimento per evitare un effetto indesiderato (es. troppa stanchezza serale o un riposo troppo tardo che sposta il sonno notturno).
Poi offri 2 scenari possibili basati su come potrebbe andare oggi (es. pisolino breve vs lungo, stanchezza nel tardo pomeriggio vs no), con indicazioni diverse ma coerenti.
Chiudi rassicurando che nei primi giorni l'obiettivo è osservare i ritmi reali del bambino e adattarsi, senza guardare troppo l'orologio.

EMOTIVITÀ E SALUTE
Sii sempre empatica: accogli senza giudizio, valida la fatica e il vissuto della mamma prima o insieme alle indicazioni pratiche.
Usa rassicurazioni forti quando la mamma mostra ansia, senso di colpa, disperazione o forte stanchezza; anche negli altri casi mantieni un tono umano, vicino e rispettoso.
Non fare diagnosi, non interpretare prescrizioni e non consigliare farmaci o dosaggi.
Per difficoltà respiratoria, febbre importante ancora in corso, vomito persistente, disidratazione, dolore forte, crescita, farmaci o dubbi sanitari diretti, rimanda al pediatra e non dare consigli medici.
Se il tema sanitario è lieve e la domanda resta sul sonno, puoi parlare con prudenza del maggiore bisogno di contatto e del rientro graduale alla routine.

CONFINI INVISIBILI
Non parlare mai alla mamma di scadenza della consulenza, giorni rimasti, rinnovi, prezzi, offerte, alert Telegram, stato tecnico o intelligenza artificiale.
Non dire mai alla mamma che non riesci a vedere o leggere foto, video, audio, documenti o sticker.
Se arriva un contenuto multimediale senza testo chiaro, ignoralo e rispondi solo al testo o al contesto, senza nominare il file o il formato.
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
  "sufficient_current_context": true,
  "reason": "breve motivo"
}

Regole:
- Cortesie, ok, grazie ed emoji isolate sono "cortesia".
- Una domanda pratica immediata può ricevere risposta automatica breve.
- Se sono passati almeno 3 giorni dall'ultima risposta di Paola e il messaggio non aggiorna la situazione attuale con dettagli concreti su questi giorni (come sono andate le notti, addormentamento, risvegli, aiuti richiesti, cambiamenti, eventi nuovi), imposta sufficient_current_context=false.
- Domande brevi o generiche dopo una pausa lunga, senza aggiornamento sulla situazione di adesso, hanno sufficient_current_context=false.
- Se la mamma descrive già cosa è successo in questi giorni con dettagli utili, sufficient_current_context=true anche dopo una pausa lunga.
- Febbre, tosse, raffreddore, dentini o malessere lieve citati nel contesto del sonno sono tema_medico_lieve, safe_auto_reply=true.
- Difficoltà respiratoria, febbre alta in corso o peggioramento, vomito persistente, disidratazione, dolore forte, farmaci/dosaggi, richiesta di diagnosi o urgenza sono tema_medico_delicato, needs_human=true, pause_chat=true.
- Se chiede Paola o una persona reale: richiesta_paola, needs_human=true, pause_chat=true.
- Se dice che le risposte sono copiate, incoerenti, che ha perso fiducia o che il metodo non funziona: perdita_fiducia, needs_human=true, pause_chat=true.
- Rimborso, denuncia, avvocato, truffa o reclamo forte: reclamo_rimborso, needs_human=true, pause_chat=true.
- Un normale peggioramento o alcuni giorni difficili non richiedono automaticamente l'umano, salvo forte rabbia, perdita di fiducia o necessità di cambiare completamente piano.
- response_depth micro per aggiornamenti semplici; normal per domande concrete puntuali; deep per situazioni articolate, messaggi lunghi con più domande, o quando la mamma chiede chiarimenti su indicazioni precedenti, esprime dubbi su orari/regole o dice "come faccio a...?".
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
Rispondi alla domanda concreta come consulente del sonno: usa il piano come riferimento ma adatta al momento se la situazione è cambiata.
Dai massimo una o due indicazioni per oggi o stanotte.
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
Deve essere naturale, specifica, empatica, coerente col contesto e proporzionata alla richiesta; può adattare il piano se la situazione è cambiata, senza ignorare la realtà del momento.
Deve evitare diagnosi, farmaci, prezzi, rinnovi, scadenze, riferimenti al bot o a Telegram.
Non deve dire che non può vedere foto, video, audio o documenti.
Può fare una o due domande mirate se servono a capire la dinamica e dare un'indicazione migliore; non deve interrogare in ogni messaggio.
La lunghezza deve rispecchiare il messaggio della mamma: più completo il messaggio, più può essere sviluppata la risposta; messaggio breve, risposta breve.
Per domande di chiarimento o messaggi articolati con dubbi su indicazioni precedenti, una risposta più sviluppata che spiega il perché e presenta scenari possibili è corretta.
Non deve fare domande finali di abitudine, ripetere il contesto o diventare lunga senza motivo.
Metti rewrite=true se basta riscriverla.
Metti send=false soltanto se è pericolosa, contraddittoria, inventa dati importanti o non risponde alla richiesta.
Se la mamma torna dopo 3+ giorni senza aggiornamenti e la risposta fa solo 1-2 domande mirate per capire la situazione attuale, va bene così: non serve rewrite per aggiungere indicazioni operative in quel turno.
""".strip()

QUALITY_REWRITE_PROMPT = """
Riscrivi il messaggio come Paola.
Mantieni il contenuto utile, ma rendilo più naturale, empatico, specifico e proporzionato.
Non aggiungere informazioni, non cambiare tutto il piano in blocco, non fare diagnosi, non parlare di scadenze, prezzi, rinnovi, bot o Telegram.
Se serve, adatta le indicazioni alla situazione attuale come farebbe una consulente reale.
Elimina formule da intelligenza artificiale, ripetizioni, spiegazioni inutili e domande superflue o di abitudine.
Mantieni domande mirate solo se servono davvero a chiarire la situazione.
Non dire che non puoi vedere foto, video, audio o documenti.
Per domande di chiarimento, mantieni le spiegazioni del perché e gli scenari possibili; non accorciare in una risposta secca.
Altrimenti dai al massimo una o due indicazioni pratiche.
Scrivi solo il testo finale da inviare su WhatsApp.
""".strip()

CLARIFICATION_PROMPT = """
La mamma chiede chiarimenti su un'indicazione precedente o esprime confusione su come applicarla nella pratica.
In questo turno:
- Riconosci che il dubbio è normale e spiega perché vale quell'indicazione: non è un orario fisso da rispettare a tutti i costi, ma un riferimento per evitare un effetto indesiderato.
- Parti dalla situazione concreta di oggi e offri 2 scenari possibili (es. pisolino breve con stanchezza nel tardo pomeriggio vs pisolino lungo che la porta avanti bene), con indicazioni diverse ma coerenti.
- Spiega cosa si vuole evitare: arrivare alla sera troppo stanca oppure fare un riposo troppo tardo che sposta la spinta al sonno notturno.
- Chiudi rassicurando che nei primi giorni l'obiettivo è osservare i ritmi reali del bambino e adattarsi, senza guardare troppo l'orologio.
- Non limitarti a un solo orario o consiglio secco: la mamma ha bisogno di capire il ragionamento.
""".strip()

MEDIA_ONLY_INTERNAL_PROMPT = """
La mamma ha mandato un contenuto multimediale senza testo leggibile.
Rispondi in modo naturale e caldo come Paola, basandoti sul contesto della consulenza e sullo storico.
Non dire che non vedi foto, video o audio. Non chiedere screenshot o di scrivere in chat.
Resta sul vago se serve, con una o due frasi brevi, come se stessi seguendo il filo del discorso.
""".strip()

AFTER_GAP_CLARIFICATION_PROMPT = """
La mamma torna a scrivere dopo almeno 3 giorni senza aggiornamenti e la situazione attuale non è ancora chiara.
In questo turno il tuo compito principale è capire com'è andata in questi giorni, non dare subito una nuova indicazione operativa dettagliata.

Regole per questo messaggio:
- Riconosci brevemente il legame con la storia precedente, senza ripetere tutto il piano.
- Fai 1 o 2 domande mirate e pertinenti alla domanda che ha fatto, per aggiornare il quadro attuale.
- Le domande devono riguardare cosa è successo in questi giorni: notti, addormentamento, risvegli, aiuti richiesti, peggioramenti o miglioramenti, eventuali cambiamenti o eventi nuovi.
- Le domande devono essere concrete e facili da rispondere su WhatsApp.
- Non fare un interrogatorio lungo: massimo 2 domande.
- Non chiedere dati già noti da profilo, piano, riepilogo memoria o storico.
- In questo turno NON dare consigli pratici dettagliati né cambiare il piano: rimanda l'indicazione al messaggio successivo, quando avrai il quadro aggiornato.
- Se c'è un accenno emotivo o stanchezza, riconoscilo brevemente prima delle domande.
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

MEMORY_SUMMARY_PROMPT = """
Sei Paola e devi aggiornare il riepilogo interno di una consulenza sul sonno infantile già in corso.
Il riepilogo serve a ricordare tutta la storia della mamma anche se torna a scrivere dopo giorni o settimane.

Scrivi in italiano, in prosa chiara e densa, senza markdown o elenchi puntati.
Includi solo ciò che emerge dai dati: non inventare.
Copri, quando presenti:
- bambino/a, età, contesto familiare rilevante
- problema iniziale e obiettivo della consulenza
- cosa prevedeva il piano e cosa è stato davvero applicato
- miglioramenti, peggioramenti, passi indietro e momenti difficili
- indicazioni già date da Paola e cosa la mamma ha provato
- temi ricorrenti, dubbi aperti, promesse o follow-up impliciti
- eventi recenti (malattia, viaggio, cambio routine, dentini, ecc.)
- tono emotivo e livello di stanchezza o fiducia della mamma

Se esiste un riepilogo precedente, integralo con le novità senza ripetere tutto da capo.
Mantieni il riepilogo sotto circa 2500 caratteri, privilegiando ciò che serve per rispondere bene al prossimo messaggio.
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
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS conversation_summary TEXT",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS summary_updated_at TIMESTAMPTZ",

        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS plan_filename TEXT",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS plan_file_mime TEXT",
        "ALTER TABLE support_cases ADD COLUMN IF NOT EXISTS plan_file_data BYTEA",
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
        "last_alert_at", "plan_filename", "plan_file_mime", "plan_file_data",
        "conversation_summary", "summary_updated_at",
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


def is_media_placeholder(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    lower = text.lower()
    markers = (
        "[video ricevuto]",
        "[immagine]",
        "[sticker]",
        "[reazione]",
        "[messaggio vocale non comprensibile]",
        "[documento ricevuto:",
        "[messaggio whatsapp di tipo",
    )
    return any(lower == marker or lower.startswith(marker) for marker in markers)


def build_pending_user_text(pending: List[Dict[str, Any]]) -> Tuple[str, bool]:
    meaningful: List[str] = []
    has_unreadable_media = False
    for item in pending:
        content = (item.get("content") or "").strip()
        media_type = (item.get("media_type") or "").strip().lower()
        if content and not is_media_placeholder(content):
            meaningful.append(content)
        elif media_type in {"video", "image", "audio", "document", "sticker"} or is_media_placeholder(content):
            has_unreadable_media = True
    text = "\n".join(meaningful).strip()
    return text, has_unreadable_media and not text


def append_capture_buffer(phone: str, text: str) -> None:
    case = get_case(phone)
    current = case.get("capture_buffer") or ""
    new_value = f"{current}\n\n{text.strip()}".strip()
    update_case(phone, capture_buffer=new_value)


def count_whatsapp_user_messages(phone: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM messages
        WHERE phone = %s AND role = 'user' AND source = 'whatsapp'
          AND content <> %s
    """, (phone, SILENT_NO_REPLY_MARKER))
    count = int(cur.fetchone()[0])
    cur.close()
    conn.close()
    return count


def build_questionnaire_from_whatsapp_history(phone: str) -> str:
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT content, timestamp FROM messages
        WHERE phone = %s AND role = 'user' AND source = 'whatsapp'
          AND content <> %s
        ORDER BY timestamp ASC, id ASC
    """, (phone, SILENT_NO_REPLY_MARKER))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    lines: List[str] = []
    for row in rows:
        content = (row.get("content") or "").strip()
        if not content or is_media_placeholder(content):
            continue
        ts = row.get("timestamp")
        prefix = format_dt(ts) if ts else "-"
        lines.append(f"[{prefix}] {content}")
    return "\n\n".join(lines)


def refresh_questionnaire_memory(phone: str) -> str:
    text = build_questionnaire_from_whatsapp_history(phone)
    if text.strip():
        update_case(phone, questionnaire=text)
    return text


def ingest_plan_document(
    phone: str,
    data: bytes,
    filename: str,
    caption: Optional[str] = None,
) -> int:
    extracted = extract_document_text(data, filename)
    if caption:
        extracted = f"{caption}\n\n{extracted}".strip()
    updates: Dict[str, Any] = {"plan": extracted}
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        updates["plan_filename"] = filename
        updates["plan_file_mime"] = "application/pdf"
        updates["plan_file_data"] = data
    update_case(phone, **updates)
    return len(extracted)


def sync_case_memory(phone: str) -> Dict[str, int]:
    questionnaire = refresh_questionnaire_memory(phone)
    case = get_case(phone)
    return {
        "mother_messages": count_whatsapp_user_messages(phone),
        "questionnaire_chars": len(questionnaire or ""),
        "plan_chars": len((case.get("plan") or "")),
        "summary_chars": len((case.get("conversation_summary") or "")),
    }


def get_last_assistant_message_at(phone: str) -> Optional[datetime]:
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT timestamp FROM messages
        WHERE phone = %s AND role IN ('assistant', 'admin')
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
    """, (phone,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    value = row.get("timestamp")
    if value and value.tzinfo is None:
        value = pytz.UTC.localize(value)
    return value


def days_since_last_assistant_reply(phone: str) -> Optional[float]:
    last_at = get_last_assistant_message_at(phone)
    if not last_at:
        return None
    return (now_local() - last_at.astimezone(TZ)).total_seconds() / 86400


def count_messages_since(phone: str, since: Optional[datetime]) -> int:
    conn = get_db()
    cur = conn.cursor()
    if since is None:
        cur.execute("""
            SELECT COUNT(*) FROM messages
            WHERE phone = %s AND content <> %s
        """, (phone, SILENT_NO_REPLY_MARKER))
    else:
        cur.execute("""
            SELECT COUNT(*) FROM messages
            WHERE phone = %s AND timestamp > %s AND content <> %s
        """, (phone, since, SILENT_NO_REPLY_MARKER))
    count = int(cur.fetchone()[0])
    cur.close()
    conn.close()
    return count


def build_messages_for_summary(phone: str, since: Optional[datetime] = None, limit: int = 120) -> str:
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if since:
        cur.execute("""
            SELECT role, content, timestamp FROM messages
            WHERE phone = %s AND timestamp > %s AND content <> %s
            ORDER BY timestamp ASC, id ASC
            LIMIT %s
        """, (phone, since, SILENT_NO_REPLY_MARKER, limit))
    else:
        cur.execute("""
            SELECT role, content, timestamp FROM messages
            WHERE phone = %s AND content <> %s
            ORDER BY timestamp ASC, id ASC
            LIMIT %s
        """, (phone, SILENT_NO_REPLY_MARKER, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    lines: List[str] = []
    for row in rows:
        content = (row.get("content") or "").strip()
        if not content or is_media_placeholder(content):
            continue
        role = row.get("role") or "user"
        if role == "admin":
            role = "paola"
        elif role == "assistant":
            role = "paola"
        elif role == "system":
            continue
        else:
            role = "mamma"
        ts = row.get("timestamp")
        prefix = format_dt(ts) if ts else "-"
        lines.append(f"[{prefix}] {role}: {content}")
    return "\n\n".join(lines)


def truncate_text(text: str, max_chars: int) -> str:
    clean = (text or "").strip()
    if len(clean) <= max_chars:
        return clean
    return f"... (troncato, {len(clean)} caratteri totali) ...\n\n{clean[-max_chars:]}"


def build_long_term_context(case: Dict[str, Any]) -> str:
    profile = profile_to_text(case.get("profile_json") or {})
    plan = case.get("plan") or "[mancante]"
    notes = case.get("admin_notes") or "[nessuna]"
    summary = (case.get("conversation_summary") or "").strip()
    parts = [
        f"Profilo:\n{profile}",
        f"Piano scritto da Paola:\n{plan}",
        f"Note interne di Paola:\n{notes}",
    ]
    if summary:
        parts.insert(1, f"Riepilogo completo della consulenza (memoria di tutta la storia fino a ora):\n{summary}")
    else:
        questionnaire = truncate_text(case.get("questionnaire") or "", MEMORY_QUESTIONNAIRE_MAX_CHARS)
        parts.insert(1, f"Storico messaggi mamma:\n{questionnaire or '[mancante]'}")
    return "\n\n".join(parts)


def refresh_conversation_summary(phone: str, force: bool = False) -> str:
    case = get_case(phone)
    mother_messages = count_whatsapp_user_messages(phone)
    if mother_messages < MEMORY_SUMMARY_MIN_MESSAGES and not force:
        return case.get("conversation_summary") or ""

    previous = (case.get("conversation_summary") or "").strip()
    since = case.get("summary_updated_at")
    if previous and since:
        transcript = build_messages_for_summary(phone, since=since)
        if not transcript.strip() and not force:
            return previous
        user_payload = f"""
RIEPILOGO PRECEDENTE:
{previous}

NUOVI MESSAGGI DAL RIEPILOGO:
{transcript or '[nessun nuovo messaggio testuale]'}

PROFILO:
{profile_to_text(case.get('profile_json') or {})}

PIANO:
{truncate_text(case.get('plan') or '', 6000)}

NOTE INTERNE:
{case.get('admin_notes') or '[nessuna]'}
""".strip()
    else:
        transcript = build_messages_for_summary(phone, since=None, limit=160)
        questionnaire = truncate_text(case.get("questionnaire") or transcript, MEMORY_QUESTIONNAIRE_MAX_CHARS)
        user_payload = f"""
MESSAGGI E STORICO:
{questionnaire or '[mancante]'}

PROFILO:
{profile_to_text(case.get('profile_json') or {})}

PIANO:
{truncate_text(case.get('plan') or '', 6000)}

NOTE INTERNE:
{case.get('admin_notes') or '[nessuna]'}
""".strip()

    try:
        summary = ai_text(
            model=MODEL_PROFILE,
            system_prompts=[MEMORY_SUMMARY_PROMPT],
            user_text=user_payload,
            reasoning_effort="low",
            verbosity="low",
            max_output_tokens=1100,
        )
        summary = (summary or "").strip()
        if summary:
            update_case(phone, conversation_summary=summary, summary_updated_at=now_local())
            logger.info("Riepilogo memoria aggiornato per %s (%s caratteri)", phone, len(summary))
            return summary
    except Exception as exc:
        logger.exception("Errore riepilogo memoria %s: %s", phone, exc)
    return previous


def schedule_summary_refresh(phone: str) -> None:
    def _worker() -> None:
        try:
            refresh_conversation_summary(phone)
        except Exception as exc:
            logger.exception("Errore refresh memoria in background %s: %s", phone, exc)

    threading.Thread(target=_worker, daemon=True).start()


def ensure_memory_fresh(phone: str) -> None:
    case = get_case(phone)
    mother_messages = count_whatsapp_user_messages(phone)
    if mother_messages < MEMORY_SUMMARY_MIN_MESSAGES:
        return

    gap_days = days_since_last_assistant_reply(phone)
    summary = (case.get("conversation_summary") or "").strip()
    summary_at = case.get("summary_updated_at")
    new_messages = count_messages_since(phone, summary_at)

    needs_summary = not summary
    if gap_days is not None and gap_days >= MEMORY_GAP_DAYS:
        needs_summary = True
    if new_messages >= MEMORY_SUMMARY_REFRESH_MESSAGES:
        needs_summary = True

    if needs_summary:
        refresh_conversation_summary(phone, force=gap_days is not None and gap_days >= MEMORY_GAP_DAYS)
        if gap_days is not None and gap_days >= MEMORY_GAP_DAYS:
            try:
                extract_profile(phone)
            except Exception as exc:
                logger.exception("Errore refresh profilo dopo pausa %s: %s", phone, exc)


def is_clarification_question(pending_text: str, router: Dict[str, Any]) -> bool:
    if router.get("intent") != "domanda_pratica":
        return False
    text = (pending_text or "").lower()
    cues = (
        "come faccio", "come posso", "non capisco", "non ho capito",
        "mi ha detto", "mi avevi detto", "mi aveva detto", "quindi",
        "ma come", "significa che", "in teoria", "altrimenti",
        "però", "ma tu", "avevi detto", "aveva detto", "devo fare",
        "dovrei", "è normale", "non so se", "non so come",
    )
    return any(cue in text for cue in cues) or len(text) > 250


def should_clarify_after_gap(router: Dict[str, Any], gap_days: Optional[float]) -> bool:
    if gap_days is None or gap_days < MEMORY_GAP_DAYS:
        return False
    if router.get("needs_human") or router.get("pause_chat"):
        return False
    intent = router.get("intent", "")
    if intent in {
        "cortesia", "miglioramento", "sfogo", "richiesta_paola",
        "perdita_fiducia", "reclamo_rimborso", "tema_medico_delicato",
    }:
        return False
    return not bool(router.get("sufficient_current_context", True))


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
    topic_name = f"{label} · {normalize_phone(phone)}"
    try:
        result = telegram_api("createForumTopic", json_data={"chat_id": TELEGRAM_GROUP_ID, "name": topic_name})
        thread_id = int(result["message_thread_id"])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO telegram_topics (phone, thread_id, topic_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (phone) DO UPDATE SET
                thread_id = EXCLUDED.thread_id,
                topic_name = EXCLUDED.topic_name
        """, (phone, thread_id, topic_name))
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


def guess_document_mime(filename: str) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if lower.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    return bytes(value)


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
    components: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, Optional[str]]:
    recipient = phone_for_meta(phone)
    if not recipient:
        return False, "numero destinatario non valido"
    template_payload: Dict[str, Any] = {
        "name": template_name,
        "language": {"code": language},
    }
    if components:
        template_payload["components"] = components
    elif body_parameters:
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
        return False, format_meta_error(exc)


def send_consulenza_template(phone: str, body_parameters: Optional[List[str]] = None) -> Tuple[bool, Optional[str]]:
    return send_whatsapp_template(
        phone,
        META_TEMPLATE_CONSULENZA,
        META_TEMPLATE_CONSULENZA_LANG,
        body_parameters,
    )


def format_meta_error(exc: Exception) -> str:
    raw = str(exc)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return raw
    try:
        payload = json.loads(match.group(0))
        err = payload.get("error") or {}
        message = err.get("message") or raw
        details = err.get("error_data") or err.get("error_user_msg")
        code = err.get("code")
        subcode = err.get("error_subcode")
        parts = [message]
        if details:
            parts.append(str(details))
        if code:
            parts.append(f"code={code}")
        if subcode:
            parts.append(f"subcode={subcode}")
        return " | ".join(parts)
    except Exception:
        return raw


def list_meta_message_templates(waba_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    waba = (waba_id or META_WABA_ID or "").strip()
    if not waba:
        return []
    items: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {
        "fields": "name,status,language,components,category",
        "limit": limit,
    }
    while True:
        result = meta_api("GET", f"{waba}/message_templates", params=params)
        items.extend(list(result.get("data", []) or []))
        paging = result.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not after or not paging.get("next"):
            break
        params["after"] = after
    return items


def get_phone_number_waba_id() -> Optional[str]:
    for fields in ("whatsapp_business_account", "through_whatsapp_business_account"):
        try:
            data = meta_api("GET", META_PHONE_NUMBER_ID, params={"fields": fields})
            waba = data.get(fields) or {}
            if isinstance(waba, dict) and waba.get("id"):
                return str(waba["id"]).strip()
        except Exception as exc:
            logger.debug("Campo %s non disponibile sul phone number: %s", fields, exc)

    if META_WABA_ID:
        try:
            phones = list_waba_phone_numbers()
            if any(str(p.get("id")) == str(META_PHONE_NUMBER_ID) for p in phones):
                return META_WABA_ID
        except Exception as exc:
            logger.warning("Non riesco a verificare il WABA via phone_numbers: %s", exc)
    return None


def summarize_templates(templates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "name": item.get("name"),
            "status": item.get("status"),
            "language": template_language_code(item),
            "category": item.get("category"),
            "body_variables": count_template_body_variables(item),
        }
        for item in templates
    ]


def list_owned_waba_ids() -> List[str]:
    business_id = os.environ.get("META_BUSINESS_ID", "").strip()
    if not business_id:
        return []
    try:
        result = meta_api(
            "GET",
            f"{business_id}/owned_whatsapp_business_accounts",
            params={"fields": "id,name", "limit": 50},
        )
        return [str(item.get("id")).strip() for item in result.get("data", []) if item.get("id")]
    except Exception as exc:
        logger.warning("Non riesco a leggere i WABA del business %s: %s", business_id, exc)
        return []


def meta_waba_audit() -> Dict[str, Any]:
    phone_waba_id = get_phone_number_waba_id()
    configured_waba_id = META_WABA_ID or None
    waba_ids: List[str] = []
    for candidate in [configured_waba_id, phone_waba_id]:
        if candidate and candidate not in waba_ids:
            waba_ids.append(candidate)
    for candidate in list_owned_waba_ids():
        if candidate not in waba_ids:
            waba_ids.append(candidate)

    templates_by_waba: Dict[str, List[Dict[str, Any]]] = {}
    for waba_id in waba_ids:
        try:
            templates_by_waba[waba_id] = list_meta_message_templates(waba_id)
        except Exception as exc:
            templates_by_waba[waba_id] = []
            logger.warning("Errore lettura template WABA %s: %s", waba_id, exc)

    all_names: List[str] = []
    consulenza_like: List[Dict[str, Any]] = []
    for waba_id, templates in templates_by_waba.items():
        for item in templates:
            name = str(item.get("name", "")).strip()
            all_names.append(f"{name} ({template_language_code(item)}) @ {waba_id}")
            if "consulenza" in name.lower():
                consulenza_like.append({
                    "waba_id": waba_id,
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "language": template_language_code(item),
                    "body_variables": count_template_body_variables(item),
                })

    resolved = get_template_definition()
    return {
        "configured_waba_id": configured_waba_id,
        "phone_number_waba_id": phone_waba_id,
        "business_id": os.environ.get("META_BUSINESS_ID", "").strip() or None,
        "waba_ids_checked": waba_ids,
        "waba_ids_match": bool(
            configured_waba_id and phone_waba_id and configured_waba_id == phone_waba_id
        ),
        "configured_template": META_TEMPLATE_CONSULENZA,
        "configured_language": META_TEMPLATE_CONSULENZA_LANG,
        "templates_by_waba": {
            waba_id: summarize_templates(templates)
            for waba_id, templates in templates_by_waba.items()
        },
        "template_names": all_names,
        "consulenza_like_templates": consulenza_like,
        "resolved_template": {
            "name": resolved.get("name"),
            "status": resolved.get("status"),
            "language": template_language_code(resolved),
            "body_variables": count_template_body_variables(resolved),
        } if resolved else None,
    }


def count_template_variables(text: str) -> int:
    maximum = 0
    for value in re.findall(r"\{\{(\d+)\}\}", text or ""):
        maximum = max(maximum, int(value))
    return maximum


def template_language_code(template: Dict[str, Any]) -> str:
    language = template.get("language")
    if isinstance(language, dict):
        return str(language.get("code") or "").strip()
    return str(language or "").strip()


def get_template_definition() -> Optional[Dict[str, Any]]:
    """Legge da Meta il template approvato configurato (nome + lingua preferita)."""
    try:
        templates = list_meta_message_templates()
    except Exception as exc:
        logger.warning("Non riesco a leggere i template da Meta: %s", exc)
        return None

    target_name = META_TEMPLATE_CONSULENZA.lower()
    approved = [
        item for item in templates
        if str(item.get("name", "")).strip().lower() == target_name
        and str(item.get("status", "")).upper() == "APPROVED"
    ]
    if not approved:
        logger.warning(
            "Template approvato '%s' non trovato. Disponibili: %s",
            META_TEMPLATE_CONSULENZA,
            [f"{t.get('name')} ({template_language_code(t)})" for t in templates[:20]],
        )
        return None

    preferred_langs = [
        META_TEMPLATE_CONSULENZA_LANG.lower(),
        "it",
        "it_it",
        "italian",
    ]
    for preferred in preferred_langs:
        normalized = preferred.replace("_", "")
        for item in approved:
            lang = template_language_code(item).lower().replace("_", "")
            if lang == normalized:
                return item
    return approved[0]


def count_template_body_variables(template: Dict[str, Any]) -> int:
    maximum = 0
    for component in template.get("components", []) or []:
        if str(component.get("type", "")).upper() != "BODY":
            continue
        maximum = max(maximum, count_template_variables(str(component.get("text") or "")))
    return maximum


def build_template_components(
    template: Dict[str, Any],
    display_name: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Costruisce i componenti Meta in base alla definizione approvata."""
    components: List[Dict[str, Any]] = []
    for component in template.get("components", []) or []:
        ctype = str(component.get("type", "")).upper()
        if ctype == "HEADER":
            header_format = str(component.get("format", "TEXT")).upper()
            if header_format != "TEXT":
                continue
            variable_count = count_template_variables(str(component.get("text") or ""))
            if variable_count == 0:
                continue
            if variable_count > 1:
                return None, f"header con {variable_count} variabili: servono valori espliciti"
            components.append({
                "type": "header",
                "parameters": [{"type": "text", "text": display_name}],
            })
        elif ctype == "BODY":
            variable_count = count_template_variables(str(component.get("text") or ""))
            if variable_count == 0:
                continue
            if variable_count > 1:
                return None, f"body con {variable_count} variabili: servono valori espliciti"
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": display_name}],
            })
    return components, None


def send_consulenza_template_auto(
    phone: str,
    display_name: str = "Mamma",
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Invia il template provando lingua e variabili in base alla definizione Meta."""
    attempts: List[Tuple[str, Optional[List[str]], Optional[List[Dict[str, Any]]], str]] = []
    definition = get_template_definition()

    if definition:
        language = template_language_code(definition) or META_TEMPLATE_CONSULENZA_LANG
        components, component_error = build_template_components(definition, display_name)
        if component_error:
            return False, component_error, None
        attempts.append((language, None, components, "definizione Meta"))
    else:
        logger.warning(
            "Template '%s' non letto da Meta: userò tentativi di fallback",
            META_TEMPLATE_CONSULENZA,
        )

    attempts.extend([
        (META_TEMPLATE_CONSULENZA_LANG, None, None, "fallback it senza variabili"),
        (META_TEMPLATE_CONSULENZA_LANG, [display_name], None, "fallback it con nome"),
        ("it_IT", None, None, "fallback it_IT senza variabili"),
        ("it_IT", [display_name], None, "fallback it_IT con nome"),
    ])

    unique_attempts: List[Tuple[str, Optional[List[str]], Optional[List[Dict[str, Any]]], str]] = []
    seen = set()
    for language, parameters, components, label in attempts:
        key = (language, tuple(parameters or []), json.dumps(components or [], sort_keys=True))
        if key not in seen:
            seen.add(key)
            unique_attempts.append((language, parameters, components, label))

    errors: List[str] = []
    for language, parameters, components, label in unique_attempts:
        sent, error = send_whatsapp_template(
            phone,
            template_name=META_TEMPLATE_CONSULENZA,
            language=language,
            body_parameters=parameters,
            components=components,
        )
        if sent:
            logger.info(
                "Template %s inviato a %s con %s (lang=%s)",
                META_TEMPLATE_CONSULENZA,
                phone,
                label,
                language,
            )
            return True, None, f"{label} ({language})"
        errors.append(f"{label} [{language}]: {error}")

    return False, " | ".join(errors[-6:]), None


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


def upload_whatsapp_media(data: bytes, mime_type: str, filename: str) -> str:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{META_PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    files = {"file": (filename, data, mime_type)}
    form = {"messaging_product": "whatsapp", "type": mime_type}
    response = requests.post(url, headers=headers, data=form, files=files, timeout=120)
    if response.status_code >= 400:
        raise RuntimeError(f"Meta media upload {response.status_code}: {response.text[:800]}")
    payload = response.json()
    media_id = payload.get("id")
    if not media_id:
        raise RuntimeError(f"Meta media upload senza id: {payload}")
    return str(media_id)


def send_whatsapp_document(
    phone: str,
    data: bytes,
    filename: str,
    mime_type: Optional[str] = None,
    caption: Optional[str] = None,
    source: str = "paola",
) -> Tuple[bool, Optional[str]]:
    recipient = phone_for_meta(phone)
    if not recipient:
        return False, "numero destinatario non valido"
    if not data:
        return False, "file vuoto"
    safe_name = (filename or "documento.pdf").strip() or "documento.pdf"
    mime = mime_type or guess_document_mime(safe_name)
    try:
        media_id = upload_whatsapp_media(data, mime, safe_name)
        document_payload: Dict[str, Any] = {
            "id": media_id,
            "filename": safe_name,
        }
        if caption:
            document_payload["caption"] = caption[:1024]
        result = meta_api("POST", f"{META_PHONE_NUMBER_ID}/messages", json_data={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "document",
            "document": document_payload,
        })
        message_id = None
        if result.get("messages"):
            message_id = result["messages"][0].get("id")
        role = "admin" if source == "paola" else "assistant"
        label = caption or f"[documento:{safe_name}]"
        save_message(phone, role, "meta_api", label, provider_message_id=message_id)
        send_to_topic(phone, f"📎 {label}", kind="paola" if source == "paola" else "bot")
        return True, None
    except Exception as exc:
        logger.exception("Errore invio documento WhatsApp %s: %s", phone, exc)
        error = format_meta_error(exc)
        send_to_topic(phone, f"Invio documento WhatsApp non riuscito: {error}", kind="alert")
        send_private_alert(f"⚠️ Invio documento WhatsApp non riuscito per {phone}: {error}")
        return False, error


def send_saved_plan_pdf(phone: str, caption: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    case = get_case(phone)
    data = as_bytes(case.get("plan_file_data"))
    filename = (case.get("plan_filename") or "piano.pdf").strip()
    mime = (case.get("plan_file_mime") or guess_document_mime(filename)).strip()
    if not data:
        return False, "nessun PDF del piano salvato: carica un PDF nel topic prima di usare /inviapiano"
    default_caption = "Ecco il piano personalizzato per il sonno del tuo bambino 🌙"
    return send_whatsapp_document(
        phone,
        data,
        filename,
        mime_type=mime,
        caption=caption or default_caption,
        source="paola",
    )


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
        "sufficient_current_context": True,
        "reason": "fallback",
    }
    case = get_case(phone)
    gap_days = days_since_last_assistant_reply(phone)
    gap_note = ""
    if gap_days is not None and gap_days >= MEMORY_GAP_DAYS:
        gap_note = (
            f"\nGiorni dall'ultima risposta di Paola: {gap_days:.1f} "
            f"(pausa lunga: valuta se il messaggio aggiorna la situazione attuale)."
        )
    elif gap_days is not None and gap_days >= 1:
        gap_note = f"\nGiorni dall'ultima risposta di Paola: {gap_days:.1f}"
    context = f"""
Stato attuale: {case.get('status')}{gap_note}

{build_long_term_context(case)}

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
    summary = (case.get("conversation_summary") or "").strip()
    context = f"""
RIEPILOGO MEMORIA:
{summary or '[non ancora disponibile]'}

QUESTIONARIO:
{truncate_text(case.get('questionnaire') or '', MEMORY_QUESTIONNAIRE_MAX_CHARS)}

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


def generate_normal_reply(
    phone: str,
    pending_text: str,
    router: Dict[str, Any],
    forced_mode: Optional[str] = None,
    media_only: bool = False,
) -> Optional[str]:
    case = get_case(phone)
    history = get_history_before_pending(phone, RECENT_HISTORY_LIMIT)
    gap_days = days_since_last_assistant_reply(phone)
    clarify_after_gap = should_clarify_after_gap(router, gap_days)
    clarification_question = is_clarification_question(pending_text, router)
    depth = router.get("response_depth", "normal")
    if depth not in {"micro", "normal", "deep"}:
        depth = "normal"
    if clarify_after_gap:
        depth = "normal"
    elif clarification_question:
        depth = "deep"
    effort = {"micro": "low", "normal": "low", "deep": "medium"}[depth]
    verbosity = "low" if depth == "micro" else "medium"
    max_tokens = 500 if clarify_after_gap else {"micro": 350, "normal": 850, "deep": 1400}[depth]

    pending_chars = len((pending_text or "").strip())
    gap_note = ""
    if gap_days is not None and gap_days >= MEMORY_GAP_DAYS:
        gap_note = (
            f"\nLa mamma torna a scrivere dopo circa {gap_days:.0f} giorni senza aggiornamenti. "
            "Ricollega la risposta a tutta la storia della consulenza, non solo all'ultimo scambio."
        )
        if clarify_after_gap:
            gap_note += (
                "\nLa situazione attuale non è ancora chiara: in questo turno fai domande mirate "
                "per aggiornare il quadro, senza dare ancora indicazioni operative dettagliate."
            )
        else:
            gap_note += (
                "\nLa mamma ha già dato aggiornamenti sufficienti su questi giorni: "
                "puoi rispondere in modo concreto alla domanda."
            )
    operational = f"""
CONTESTO DELLA CONSULENZA
{build_long_term_context(case)}{gap_note}

Classificazione interna:
{json.dumps(router, ensure_ascii=False)}

Messaggio attuale della mamma: circa {pending_chars} caratteri.
Lunghezza richiesta: {depth}.
Se il messaggio è breve e puntuale, rispondi in modo essenziale.
Se il messaggio è lungo e articolato, puoi essere più completa senza diventare prolissa.
Se micro, usa normalmente 1-3 frasi. Se normal, resta proporzionata al messaggio. Se deep, spiega il ragionamento e offri scenari possibili quando la mamma ha dubbi.
""".strip()
    prompts = [SYSTEM_PROMPT_BASE, operational]
    if clarification_question and not clarify_after_gap:
        prompts.append(CLARIFICATION_PROMPT)
    if clarify_after_gap:
        prompts.append(AFTER_GAP_CLARIFICATION_PROMPT)
    if forced_mode == "continua":
        prompts.append(FORCED_CONTINUE_PROMPT)
    if media_only and not (pending_text or "").strip():
        prompts.append(MEDIA_ONLY_INTERNAL_PROMPT)

    image_data_url = None
    for item in reversed(get_pending_user_messages(phone)):
        if item.get("media_type") == "image" and item.get("media_id"):
            image_data_url = media_image_data_url(item.get("media_id"))
            if image_data_url:
                break
    user_text = (pending_text or "").strip()
    if media_only and not user_text:
        user_text = "Continua la conversazione in modo naturale e caldo."
    try:
        reply = ai_text(
            model=MODEL_CHAT,
            system_prompts=prompts,
            user_text=user_text,
            history=history,
            reasoning_effort=effort,
            verbosity=verbosity,
            max_output_tokens=max_tokens,
            image_data_url=image_data_url,
        )
        clean = clean_reply(reply)
        return quality_control_reply(
            phone, user_text, clean, router,
            clarify_after_gap=clarify_after_gap,
            clarification_question=clarification_question,
        )
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
    clarify_after_gap: bool = False,
    clarification_question: bool = False,
) -> Optional[str]:
    if not reply:
        return None
    case = get_case(phone)
    clarification_note = ""
    if clarify_after_gap:
        clarification_note = (
            "\nNOTA: risposta dopo pausa di 3+ giorni senza aggiornamenti sufficienti. "
            "In questo turno è corretto fare solo 1-2 domande mirate, senza indicazioni operative dettagliate."
        )
    elif clarification_question:
        clarification_note = (
            "\nNOTA: domanda di chiarimento su indicazioni precedenti. "
            "Una risposta più sviluppata che spiega il perché e presenta scenari possibili è corretta."
        )
    context = f"""
MESSAGGIO DELLA MAMMA:
{pending_text}

RISPOSTA PROPOSTA:
{reply}

CLASSIFICAZIONE:
{json.dumps(router, ensure_ascii=False)}{clarification_note}

{build_long_term_context(case)}
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
    ensure_memory_fresh(phone)
    pending = get_pending_user_messages(phone)
    pending_text, media_only = build_pending_user_text(pending)
    if not pending_text and not media_only:
        return
    if pending_text and is_obvious_closing_message(pending_text):
        mark_silent_no_reply(phone, "chiusura rilevata dal timer")
        logger.info("Chiusura/cortesia: nessuna risposta per %s", phone)
        return

    router = classify_support_message(phone, pending_text or "messaggio multimediale senza testo")
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

    reply = generate_normal_reply(phone, pending_text, router, media_only=media_only)
    if reply:
        if send_whatsapp_message(phone, reply, source="bot"):
            schedule_summary_refresh(phone)


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
    refresh_questionnaire_memory(phone)

    status = case.get("status", STATUS_PAUSED)
    if status in {STATUS_PAUSED, STATUS_REVIEW, STATUS_CLOSED}:
        return
    if status == STATUS_CHECKUP:
        if not is_obvious_closing_message(content):
            schedule_checkup_review(phone)
        return
    if status != STATUS_ACTIVE:
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
    memory = sync_case_memory(phone)
    return (
        f"Stato bot: {case.get('status')}\n"
        f"Messaggi WhatsApp mamma: {memory['mother_messages']}\n"
        f"Memoria questionario: {memory['questionnaire_chars']} caratteri\n"
        f"Memoria riepilogo: {memory['summary_chars']} caratteri\n"
        f"Memoria piano: {memory['plan_chars']} caratteri\n"
        f"PDF piano salvato: {'sì' if case.get('plan_file_data') else 'no'}\n"
        f"Attivazione: {format_dt(case.get('activated_at'))}\n"
        f"Scadenza: {format_dt(case.get('support_end_at'))}\n"
        f"Alert scadenza inviato: {'sì' if case.get('expiration_alert_sent') else 'no'}"
    )


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

    if cmd == "/inviapiano":
        sent, error = send_saved_plan_pdf(phone)
        if sent:
            send_to_topic(phone, "📎 PDF del piano inviato alla mamma su WhatsApp.", kind="system")
        else:
            send_to_topic(phone, f"PDF non inviato: {error}", kind="alert")
        return
    if cmd == "/attiva":
        memory = sync_case_memory(phone)
        if memory["mother_messages"] == 0 and memory["plan_chars"] == 0:
            send_to_topic(
                phone,
                "Attenzione: non ho ancora messaggi WhatsApp della mamma né un piano salvato. "
                "Attivo comunque il bot con la memoria disponibile.",
                kind="alert",
            )
        cancel_timer(phone, "attivazione")
        profile = extract_profile(phone)
        summary = refresh_conversation_summary(phone, force=True)
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
            capture_mode=CAPTURE_NONE,
            capture_buffer="",
        )
        send_to_topic(
            phone,
            "✅ SUPPORTO ATTIVATO\n\n"
            f"Attivazione: {format_dt(activated)}\n"
            f"Alert fine consulenza: {format_dt(end_at)}\n"
            f"Messaggi mamma in memoria: {memory['mother_messages']}\n"
            f"Questionario (da chat): {memory['questionnaire_chars']} caratteri\n"
            f"Riepilogo memoria: {len(summary or '')} caratteri\n"
            f"Piano in memoria: {memory['plan_chars']} caratteri\n"
            f"Profilo estratto: {'sì' if profile else 'parziale'}\n"
            "Il bot usa la chat WhatsApp, il riepilogo memoria e il piano caricato. Continua anche dopo l'alert dei 30 giorni.",
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
        ensure_memory_fresh(phone)
        pending = get_pending_user_messages(phone)
        pending_text, media_only = build_pending_user_text(pending)
        if not pending_text and not media_only:
            send_to_topic(phone, "Non ci sono nuovi messaggi della mamma a cui rispondere.", kind="system")
            return
        router = classify_support_message(phone, pending_text or "messaggio multimediale senza testo")
        mode = "continua" if cmd == "/continua" else None
        reply = generate_normal_reply(
            phone, pending_text, router, forced_mode=mode, media_only=media_only,
        )
        if reply and send_whatsapp_message(phone, reply, source="bot"):
            update_case(phone, status=STATUS_ACTIVE)
            schedule_summary_refresh(phone)
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
    update_case(
        phone,
        status=STATUS_PAUSED,
        questionnaire=None,
        plan=None,
        plan_filename=None,
        plan_file_mime=None,
        plan_file_data=None,
        capture_mode=CAPTURE_NONE,
        capture_buffer="",
    )

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

    document = message.get("document")
    if document:
        try:
            data, file_path = download_telegram_file(document["file_id"])
            filename = document.get("file_name") or file_path
            caption = (message.get("caption") or "").strip() or None
            lower = filename.lower()
            if lower.endswith((".pdf", ".txt", ".docx")):
                chars = ingest_plan_document(phone, data, filename, caption=caption)
                send_to_topic(
                    phone,
                    f"📎 Piano letto e salvato in memoria ({chars} caratteri).",
                    kind="system",
                )
                if lower.endswith(".pdf"):
                    cancel_timer(phone, "documento piano da Telegram")
                    sent, error = send_whatsapp_document(
                        phone, data, filename, caption=caption, source="paola",
                    )
                    if not sent:
                        send_to_topic(phone, f"PDF non inviato su WhatsApp: {error}", kind="alert")
            else:
                send_to_topic(phone, "Per il piano usa PDF, TXT o DOCX.", kind="system")
        except Exception as exc:
            send_to_topic(phone, f"Non sono riuscito a gestire il documento: {exc}", kind="alert")
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


@app.route("/admin/meta/audit", methods=["GET"])
def admin_meta_audit():
    if not admin_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        audit = meta_waba_audit()
        return jsonify({"ok": True, "audit": audit, "meta": meta_setup_status()})
    except Exception as exc:
        logger.exception("Errore audit Meta: %s", exc)
        return jsonify({"ok": False, "error": format_meta_error(exc)}), 502


@app.route("/admin/meta/templates", methods=["GET"])
def admin_meta_templates():
    if not admin_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    probe_waba = (request.args.get("waba_id") or "").strip()
    try:
        templates = list_meta_message_templates(probe_waba or None)
        summary = summarize_templates(templates)
        configured = get_template_definition() if not probe_waba else None
        return jsonify({
            "ok": True,
            "waba_id": probe_waba or META_WABA_ID,
            "configured_template": META_TEMPLATE_CONSULENZA,
            "configured_language": META_TEMPLATE_CONSULENZA_LANG,
            "resolved_template": {
                "name": configured.get("name"),
                "status": configured.get("status"),
                "language": template_language_code(configured),
                "body_variables": count_template_body_variables(configured),
            } if configured else None,
            "templates": summary,
        })
    except Exception as exc:
        logger.exception("Errore lettura template Meta: %s", exc)
        return jsonify({"ok": False, "error": format_meta_error(exc)}), 502


@app.route("/admin/meta/test", methods=["POST"])
def admin_meta_test_message():
    if not admin_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    phone = normalize_phone(payload.get("to", ""))
    text = (payload.get("text") or "Test connessione WhatsApp Cloud API - Genitori in Armonia").strip()
    use_template = payload.get("template", True)
    fallback_text = bool(payload.get("fallback_text", False))
    display_name = (payload.get("display_name") or payload.get("name") or "Mamma").strip()
    if not phone:
        return jsonify({"ok": False, "error": "Campo 'to' obbligatorio, es. +393331234567"}), 400
    if use_template:
        sent, error, mode = send_consulenza_template_auto(phone, display_name)
        if sent:
            return jsonify({
                "ok": True,
                "to": phone,
                "mode": f"template:{META_TEMPLATE_CONSULENZA}:{mode}",
                "meta": meta_setup_status(),
            })
        if not fallback_text:
            return jsonify({
                "ok": False,
                "to": phone,
                "error": error,
                "hint": "Verifica nome/lingua del template in Meta o usa fallback_text=true per provare un messaggio di testo.",
                "meta": meta_setup_status(),
            }), 502
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
        error = format_meta_error(exc)
    return jsonify({
        "ok": False,
        "to": phone,
        "error": error,
        "hint": "Aggiungi il numero come destinatario di test in Meta Passaggio 1, oppure invia prima un messaggio al numero business.",
        "meta": meta_setup_status(),
    }), 502


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
