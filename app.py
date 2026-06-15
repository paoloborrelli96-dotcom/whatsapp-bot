import os
import time
import random
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, request, Response
from twilio.rest import Client
import openai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import base64
import io
import json
import re
import pytz

# ─── CONFIGURAZIONE ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

OPENAI_API_KEY         = os.environ["OPENAI_API_KEY"]
TWILIO_ACCOUNT_SID     = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN      = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_NUMBER = os.environ["TWILIO_WHATSAPP_NUMBER"]
DATABASE_URL           = os.environ["DATABASE_URL"]
TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_GROUP_ID      = os.environ.get("TELEGRAM_GROUP_ID", "")
TIMEZONE               = os.environ.get("TIMEZONE", "Europe/Rome")

# ─── MODELLI OPENAI ────────────────────────────────────────────────────────────
# Puoi cambiarli da Railway senza modificare il codice.
# Consiglio: router/classificazioni su modello economico, chat su modello conversazionale, piano su modello piu forte.
MODEL_ROUTER           = os.environ.get("MODEL_ROUTER", "gpt-5.4-nano")
MODEL_CLASSIFIER       = os.environ.get("MODEL_CLASSIFIER", "gpt-5.4-nano")
MODEL_CHAT             = os.environ.get("MODEL_CHAT", "gpt-5.1-chat-latest")
MODEL_PLAN             = os.environ.get("MODEL_PLAN", "gpt-5.5")
MODEL_PROFILE          = os.environ.get("MODEL_PROFILE", "gpt-5.4-mini")
MODEL_AUDIO            = os.environ.get("MODEL_AUDIO", "whisper-1")

TEMP_ROUTER            = float(os.environ.get("TEMP_ROUTER", "0"))
TEMP_CHAT              = float(os.environ.get("TEMP_CHAT", "0.55"))
TEMP_PLAN              = float(os.environ.get("TEMP_PLAN", "0.65"))

LINK_PREMIUM           = os.environ.get("LINK_PREMIUM", "https://genitorinarmonia.com/products/metodo-paola-premium")
LINK_BASE              = os.environ.get("LINK_BASE", "https://genitorinarmonia.com/products/sonno-magico")
LINK_REFUND            = os.environ.get("LINK_REFUND", "https://genitorinarmonia.com/policies/refund-policy")

OFFERS = {
    "base": {
        "price": 37,
        "duration_days": 30,
        "weekend_support": False,
        "name": "Percorso da 37 euro",
        "description": "questionario iniziale, piano personalizzato e supporto WhatsApp nei giorni lavorativi"
    },
    "premium": {
        "price": 67,
        "duration_days": 60,
        "weekend_support": True,
        "name": "Percorso Premium",
        "description": "questionario iniziale, piano personalizzato, 60 giorni di supporto WhatsApp e maggiore continuita"
    },
    "renewal_30": {"price": 37, "duration_days": 30},
    "renewal_60": {"price": 47, "duration_days": 60}
}

def in_orario_silenzio():
    """Controlla se siamo nell'orario di silenzio (23:00 - 07:00 ora italiana)."""
    try:
        tz = pytz.timezone(TIMEZONE)
        ora_locale = datetime.now(tz)
        ora = ora_locale.hour
        return ora >= 23 or ora < 7
    except Exception as e:
        logger.error(f"Errore orario silenzio: {e}")
        return False

openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Timer attivi per numero — UN solo timer per numero alla volta
active_timers = {}
active_timers_lock = threading.Lock()

# Deduplicazione messaggi
processed_sids = set()
processed_sids_lock = threading.Lock()

# Cache topic Telegram per numero (phone -> thread_id)
topic_cache = {}
topic_cache_lock = threading.Lock()

# ─── FASI ──────────────────────────────────────────────────────────────────────
# 0  = info/primo contatto
# 1  = acquisto confermato, benvenuto+regole+questionario parte 1 inviati
# 2  = mamma ha risposto parte 1, questionario parte 2 inviato
# 3  = questionario completo, piano schedulato (1 ora)
# 4  = piano inviato, percorso attivo
# 5  = attesa conferma completamento questionario
# 6  = silenzio totale — aspetta solo "ho finito"
# 99 = chat in pausa

# ─── TESTI FISSI ───────────────────────────────────────────────────────────────
MSG_BENVENUTO = (
    "Grazie per la fiducia, molto piacere 😇\n\n"
    "Facciamo cosi: per capire bene la vostra situazione, ti mando un questionario dettagliato "
    "e da li ti preparo un piano personalizzato.\n"
    "Ti mando anche un messaggio che invio a tutti con delle semplici regole per la chat e le consulenze."
)

MSG_REGOLE = (
    "Il percorso, le indicazioni e l'eventuale piano personalizzato che verranno proposti "
    "si basano esclusivamente sulla mia esperienza nel supporto al sonno infantile. "
    "Non sostituiscono in alcun modo il parere di medici, pediatri o altri professionisti sanitari, "
    "che restano sempre il riferimento principale in presenza di dubbi clinici o problematiche di salute. "
    "Ogni genitore e libera/o di decidere se applicarli, modificarli o non seguirli, in totale autonomia, consapevolezza e tranquillita. "
    "Per rendere la comunicazione piu chiara, ordinata e veloce, alcuni messaggi possono essere gestiti tramite un'app esterna "
    "che utilizza strumenti di Intelligenza Artificiale a supporto della scrittura. "
    "Tutti i contenuti inviati restano comunque sotto la mia supervisione e responsabilita professionale. "
    "Ti chiedo inoltre, quando possibile, di evitare messaggi eccessivamente lunghi e di suddividerli in piu messaggi brevi. "
    "L'app utilizzata potrebbe infatti avere difficolta a ricevere correttamente testi molto lunghi inviati in un unico messaggio.\n\n"
    "Il servizio di consulenza e un servizio aggiuntivo ed esclusivo. "
    "Una volta avviato, non sara' più previsto richiedere il rimborso. "
    "Il rimborso e previsto esclusivamente per chi ha acquistato il pacchetto ma non ha ancora usufruito di alcuna consulenza, "
    "limitatamente al metodo contenuto nelle guide in PDF.\n\n"
    "Rispondo dal lunedi al venerdi, dalle 9 alle 17. "
    "Il weekend mi fermo — se mi scrivi sabato o domenica ti rispondo lunedi.\n\n"
    "Se accetti queste condizioni, compila il questionario e iniziamo 🤍"
)

MSG_QUESTIONARIO_1 = (
    "Per prepararti un piano su misura ho bisogno di conoscerti meglio. Iniziamo con alcune domande, "
    "rispondimi con calma:\n\n"
    "1. Nominativo con cui hai effettuato l'ordine e data di acquisto\n"
    "2. Come ti chiami e quanti anni hai?\n"
    "3. Nome, data di nascita e peso attuale del bambino/a?\n"
    "4. E il primo figlio? Ha fratelli o sorelle?\n"
    "5. Descrivimi la sua giornata tipo: orario sveglia mattina, pisolini (orari e durata), orario nanna serale\n"
    "6. Come si addormenta? (seno, ciuccio, in braccio, da solo...)\n"
    "7. Dove dorme? (culla, lettone, carrozzina...)\n\n"
    "Rispondimi a queste prime domande con calma, poi ti mando le altre 🤍"
)

MSG_QUESTIONARIO_2 = (
    "Rispondi anche a queste, grazie:\n\n"
    "8. Quante volte si sveglia di notte e come lo riaddormenti?\n"
    "9. Allatti al seno, biberon o entrambi?\n"
    "10. Hai gia provato qualcosa per migliorare il sonno? Com'e andata?\n"
    "11. Lavori? Sei in maternita o rientri presto?\n"
    "12. Il tuo partner ti aiuta di notte?\n"
    "13. Qual e la difficolta principale che vuoi risolvere?\n"
    "14. C'e altro che vuoi dirmi che per te e importante che io sappia?"
)

MSG_CONFERMA_QUESTIONARIO = (
    "Hai risposto a tutto? Dimmi quando hai finito cosi inizio subito a prepararti il piano 🤍"
)

# ─── PROMPT MODULARI ───────────────────────────────────────────────────────────
SYSTEM_PROMPT_BASE = """
Sei Paola, consulente del sonno infantile di Genitori in Armonia.
Gestisci conversazioni WhatsApp con mamme e famiglie che chiedono aiuto sul sonno dei bambini.

IDENTITA E TONO
Parli sempre come Paola, in prima persona singolare: "io ti propongo", "ti seguo", "il mio percorso".
Non dire "il nostro team" o "il nostro servizio" riferito all'azienda.
Scrivi come un messaggio WhatsApp umano: caldo, diretto, semplice, concreto.
Devi sembrare Paola che risponde a una mamma, non un testo generato.

REGOLE DI STILE
Non usare mai punti esclamativi.
Non usare markdown, titoli, grassetti, elenchi puntati o numerazioni nelle risposte normali.
Usa poche emoji, massimo una quando serve.
Non iniziare con frasi da manuale come "Grazie per aver condiviso", "Ora vediamo insieme", "Ecco il piano", "Capisco perfettamente".
Evita linguaggio tecnico: non scrivere "associazione seno-sonno", "stimolazione cognitiva", "igiene del sonno".
Usa frasi naturali: "guarda", "ti dico", "secondo me", "io ti propongo", "potresti provare".
Se il messaggio della mamma è breve, rispondi breve.
Se è un aggiornamento semplice, non aggiungere spiegazioni lunghe.
Non chiudere con frasi automatiche tipo "sono qui per qualsiasi domanda", "fammi sapere", "aggiornami".
Solo nel piano personalizzato puoi chiudere con: "Aggiornami fra qualche giorno e fammi sapere come va 🤍".

PERSONALIZZAZIONE
Usa sempre il nome del bambino se lo conosci.
Collega la risposta agli orari, alle abitudini e agli obiettivi già emersi.
Non rispondere mai come se fosse la prima volta, se hai già contesto.
Non proporre troppe modifiche insieme: durante il percorso attivo dai 1 o 2 indicazioni alla volta.
Non colpevolizzare mai la mamma. Non dire "non devi cedere" o "hai creato tu il problema".
Spiega invece che il bambino ha imparato un aiuto e ora lo accompagnerete gradualmente verso un aiuto diverso.

CONFINI
Non dare diagnosi mediche e non sostituirti al pediatra.
Per febbre, crescita, reflusso importante, allergie, difficoltà respiratorie o dubbi sanitari, rimanda al pediatra in modo naturale.
Per il sonno, Paola resta il riferimento.
Non parlare mai di consulenza scaduta, fine percorso o rinnovi, a meno che sia la mamma a chiedere esplicitamente informazioni sul rinnovo oppure sia Paola/Admin a dirtelo.

SE CHIEDONO SE SEI UN BOT
Rispondi in modo trasparente e naturale:
"No, uso un'applicazione per gestire le conversazioni e uno strumento che mi aiuta nella scrittura, ma leggo tutto io personalmente e sono io che costruisco le risposte in base alla tua situazione."
"""

ROUTER_PROMPT = """
Sei un classificatore per una chat WhatsApp di consulenza sul sonno infantile.
Non devi scrivere la risposta alla mamma.
Devi restituire solo JSON valido.

Intenti possibili:
- saluto_vago
- richiesta_info_percorso
- descrizione_problema_sonno
- richiesta_consiglio_gratuito
- richiesta_differenza_percorsi
- obiezione_prezzo
- richiesta_link
- intenzione_acquisto_non_completato
- acquisto_completato
- richiesta_bonifico
- bonifico_effettuato
- problema_checkout_importo
- richiesta_rimborso
- lamentela_generica
- domanda_percorso_attivo
- aggiornamento_percorso_attivo
- richiesta_pratica_immediata
- messaggio_cortesia
- conferma_questionario_finito
- risposta_questionario_concreta
- risposta_questionario_non_concreta
- dubbio_medico_lieve
- dubbio_medico_delicato
- sospetto_ai_o_richiesta_paola
- necessita_revisione_umano
- altro

Regole importanti:
Non classificare come richiesta_bonifico solo perché compare la parola bonifico. È richiesta_bonifico solo se chiede IBAN, coordinate, o se può pagare con bonifico.
Se dice che ha già fatto il bonifico, usa bonifico_effettuato.
Non classificare come richiesta_rimborso solo perché compare la parola rimborso. È richiesta_rimborso solo se vuole indietro i soldi o chiede la procedura.
Non classificare come problema_checkout_importo solo perché compaiono 37 o 67. È problema_checkout_importo solo se parla di carrello, checkout, importo sbagliato, prezzo che non torna, prodotto aggiunto più volte.
Non classificare come acquisto_completato se scrive "lo compro", "lo prendo", "acquisto subito". Quello è intenzione_acquisto_non_completato.
È acquisto_completato solo se dice che ha già pagato, completato ordine, fatto acquisto, o mostra ricevuta/conferma.
Se la mamma è già in percorso attivo e chiede "che faccio ora", "lo sveglio", "la attacco", "come mi muovo adesso", usa richiesta_pratica_immediata.
Se parla di febbre, vomito, difficoltà respiratoria, crescita, allergia importante, farmaci, dolore forte o situazione sanitaria preoccupante, usa dubbio_medico_delicato e needs_human true.
Se esprime rabbia forte, minaccia recensioni, parla di avvocato, truffa, denuncia, o chiede chiaramente una persona vera, usa necessita_revisione_umano e needs_human true.
Non usare mai intenti legati a consulenza scaduta o fine percorso.

Rispondi solo con questo schema JSON:
{
  "intent": "...",
  "confidence": 0.0,
  "safe_auto_reply": true,
  "needs_human": false,
  "reason": "breve spiegazione interna",
  "message_type": "micro_update|richiesta_pratica|racconto_lungo|sfogo|obiezione|conferma|altro",
  "entities": {
    "price_mentioned": null,
    "payment_method": null,
    "child_name": null,
    "medical_topic": false,
    "asks_for_link": false
  }
}
"""

CHAT_RESPONSE_PROMPT = """
Scrivi la risposta WhatsApp come Paola.
Devi rispettare il prompt base e il contesto operativo.
Scrivi solo il testo da inviare alla mamma.
Non spiegare il ragionamento.
Non dire che hai classificato il messaggio.
Non parlare mai di consulenza scaduta o fine percorso.

Se la persona non ha ancora acquistato e descrive un problema, non dare un piano gratuito.
Falla sentire capita, accenna alla direzione di lavoro senza spiegare il metodo passo passo, poi presenta il percorso solo se il link non è già stato inviato.

Se la persona è in percorso attivo, dai indicazioni concrete ma non troppe insieme.
Usa il profilo del bambino e lo storico recente.
Se c'è un miglioramento, valorizzalo in modo specifico.
Se c'è un passo indietro, normalizzalo senza far sentire la mamma in colpa.

Se il messaggio è una micro-conferma o un grazie, rispondi in modo minimo.
"""

PLAN_PROMPT = """
Scrivi il piano personalizzato completo come Paola per una mamma che ha acquistato il percorso.
Il piano deve sembrare scritto apposta per lei, non un modello generico.
Usa il nome del bambino se disponibile e cita orari, abitudini, difficoltà e obiettivi emersi nel questionario.

Scrivi in prosa discorsiva da WhatsApp, ordinata ma naturale.
Non usare markdown, grassetti, titoli, bullet point o numerazioni.
Puoi andare a capo per leggibilità, ma senza formattazione da documento.

Il piano deve includere in modo naturale:
lettura iniziale della situazione specifica,
addormentamento serale,
risvegli notturni,
pisolini diurni,
finestre di veglia orientative,
ambiente e stimoli,
distinzione tra fame, stanchezza e bisogno di contatto,
cosa fare se protesta,
cosa aspettarsi nei primi giorni,
come monitorare i progressi.

Le indicazioni devono essere concrete e operative: orari, sequenza di azioni, cosa fare quando protesta, quanto aspettare, come capire se sta funzionando.
Non dare diagnosi o indicazioni mediche. Se emergono temi sanitari, rimanda al pediatra.
Non proporre troppe modifiche tutte insieme: dai una direzione chiara per i primi giorni.

Chiudi sempre e solo con:
"Aggiornami fra qualche giorno e fammi sapere come va 🤍"
"""

# Compatibilità con eventuali funzioni vecchie che richiamano SYSTEM_PROMPT.
SYSTEM_PROMPT = SYSTEM_PROMPT_BASE

# ─── TELEGRAM FORUM ────────────────────────────────────────────────────────────
def get_or_create_topic(phone):
    """Ottiene o crea un topic Telegram per questo numero."""
    if not TELEGRAM_GROUP_ID or not TELEGRAM_BOT_TOKEN:
        return None
    with topic_cache_lock:
        if phone in topic_cache:
            return topic_cache[phone]
    try:
        # Cerca topic esistente nel DB
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telegram_topics (
                phone TEXT PRIMARY KEY,
                thread_id INTEGER NOT NULL
            )
        """)
        conn.commit()
        cur.execute("SELECT thread_id FROM telegram_topics WHERE phone = %s", (phone,))
        row = cur.fetchone()
        if row:
            thread_id = row[0]
            cur.close()
            conn.close()
            with topic_cache_lock:
                topic_cache[phone] = thread_id
            return thread_id
        # Crea nuovo topic
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/createForumTopic",
            json={"chat_id": TELEGRAM_GROUP_ID, "name": phone},
            timeout=10
        )
        data = resp.json()
        if data.get("ok"):
            thread_id = data["result"]["message_thread_id"]
            cur.execute("INSERT INTO telegram_topics (phone, thread_id) VALUES (%s, %s)", (phone, thread_id))
            conn.commit()
            cur.close()
            conn.close()
            with topic_cache_lock:
                topic_cache[phone] = thread_id
            return thread_id
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore get_or_create_topic per {phone}: {e}")
    return None

def send_to_topic(phone, message, is_bot=False):
    """Manda un messaggio nel topic della mamma."""
    thread_id = get_or_create_topic(phone)
    if not thread_id:
        return
    try:
        prefix = "🤖 Bot: " if is_bot else "📩 Mamma: "
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for chunk in chunks:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_GROUP_ID,
                    "message_thread_id": thread_id,
                    "text": f"{prefix}{chunk}",
                    "parse_mode": "HTML"
                },
                timeout=10
            )
    except Exception as e:
        logger.error(f"Errore send_to_topic per {phone}: {e}")

def send_telegram(message):
    """Notifica personale (chat diretta con il bot)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for chunk in chunks:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=10
            )
    except Exception as e:
        logger.error(f"Errore Telegram: {e}")

# ─── TELEGRAM WEBHOOK (risposta dal topic) ──────────────────────────────────────
@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    """
    Riceve messaggi dal bot Telegram.
    Se Paola risponde in un topic, il messaggio viene mandato alla mamma su WhatsApp.
    """
    data = request.json
    if not data:
        return Response("OK", status=200)
    try:
        message = data.get("message", {})
        if not message:
            return Response("OK", status=200)

        # Ignora messaggi del bot stesso
        if message.get("from", {}).get("is_bot"):
            return Response("OK", status=200)

        chat_id = str(message.get("chat", {}).get("id", ""))
        thread_id = message.get("message_thread_id")
        text = message.get("text", "").strip()

        # Controlla che sia un messaggio nel gruppo forum
        if chat_id != str(TELEGRAM_GROUP_ID) or not thread_id or not text:
            return Response("OK", status=200)

        # Trova il numero della mamma da thread_id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT phone FROM telegram_topics WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return Response("OK", status=200)

        phone = row[0]
        logger.info(f"Risposta Paola via Telegram topic per {phone}: {text[:50]}")

        # ── Comandi dal topic Telegram ─────────────────────────────────────────
        if text.startswith("/"):
            cmd = text.strip().lower().split()[0]

            if cmd == "/acquisto":
                threading.Thread(target=invia_sequenza_acquisto, args=[phone], daemon=True).start()
            elif cmd == "/q1":
                set_fase(phone, 1)
                save_message(phone, "assistant", MSG_QUESTIONARIO_1)
                send_whatsapp_message(phone, MSG_QUESTIONARIO_1)
            elif cmd == "/q2":
                set_fase(phone, 2)
                save_message(phone, "assistant", MSG_QUESTIONARIO_2)
                send_whatsapp_message(phone, MSG_QUESTIONARIO_2)
            elif cmd == "/piano":
                with active_timers_lock:
                    if phone in active_timers:
                        active_timers[phone].cancel()
                        active_timers.pop(phone, None)
                threading.Thread(target=send_piano, args=[phone], daemon=True).start()
            elif cmd == "/inizia":
                set_start_date(phone, datetime.now().date())
                set_fase(phone, 4)
                with active_timers_lock:
                    if phone in active_timers:
                        active_timers[phone].cancel()
                        active_timers.pop(phone, None)
            elif cmd == "/pausa":
                set_fase(phone, 99)
                with active_timers_lock:
                    if phone in active_timers:
                        active_timers[phone].cancel()
                        active_timers.pop(phone, None)
            elif cmd == "/riprendi":
                set_fase(phone, 4)
            elif cmd == "/fase":
                parts = text.strip().split()
                if len(parts) == 2:
                    try:
                        nuova_fase = int(parts[1])
                        set_fase(phone, nuova_fase)
                        with active_timers_lock:
                            if phone in active_timers:
                                active_timers[phone].cancel()
                                active_timers.pop(phone, None)
                        logger.info(f"Fase {nuova_fase} impostata per {phone} via Telegram")
                    except ValueError:
                        pass
            elif cmd == "/nota":
                nota = text.strip()[6:].strip()
                if nota:
                    save_message(phone, "user", f"[NOTA ADMIN: {nota}]")
            logger.info(f"Comando Telegram {cmd} eseguito per {phone}")
            return Response("OK", status=200)

        # ── Messaggio normale — cancella timer e manda su WhatsApp ────────────
        with active_timers_lock:
            if phone in active_timers:
                active_timers[phone].cancel()
                active_timers.pop(phone, None)
                logger.info(f"Timer cancellato per {phone} — risposta manuale Paola")

        # Salva e manda su WhatsApp
        save_message(phone, "assistant", text)
        send_whatsapp_message(phone, text)

    except Exception as e:
        logger.error(f"Errore telegram_webhook: {e}")

    return Response("OK", status=200)

# ─── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            phone TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS consultations (
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE NOT NULL,
            fase INTEGER DEFAULT 0,
            start_date DATE,
            piano_scheduled_at TIMESTAMPTZ,
            renewal_sent BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_topics (
            phone TEXT PRIMARY KEY,
            thread_id INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS child_profiles (
            phone TEXT PRIMARY KEY,
            mother_name TEXT,
            child_name TEXT,
            child_age TEXT,
            birth_date TEXT,
            main_problem TEXT,
            goal TEXT,
            sleep_association TEXT,
            night_wakings TEXT,
            naps TEXT,
            bedtime TEXT,
            wake_time TEXT,
            sleep_place TEXT,
            feeding TEXT,
            father_role TEXT,
            health_notes TEXT,
            work_stage TEXT,
            admin_notes TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database inizializzato")

def save_message(phone, role, content):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (phone, role, content) VALUES (%s, %s, %s)",
            (phone, role, content)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore salvataggio messaggio: {e}")

def get_history(phone, days=30):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cutoff = datetime.now() - timedelta(days=days)
        cur.execute(
            """SELECT role, content FROM messages
               WHERE phone = %s AND timestamp > %s
               ORDER BY timestamp ASC""",
            (phone, cutoff)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        logger.error(f"Errore lettura history: {e}")
        return []

def get_messages_since_last_reply(phone):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT timestamp FROM messages
            WHERE phone = %s AND role = 'assistant'
            ORDER BY timestamp DESC LIMIT 1
        """, (phone,))
        last_reply = cur.fetchone()
        if last_reply:
            cutoff = last_reply["timestamp"]
        else:
            cutoff = datetime.now() - timedelta(days=30)
        cur.execute("""
            SELECT content FROM messages
            WHERE phone = %s AND role = 'user' AND timestamp > %s
            ORDER BY timestamp ASC
        """, (phone, cutoff))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [r["content"] for r in rows]
    except Exception as e:
        logger.error(f"Errore get_messages_since_last_reply: {e}")
        return []

def get_fase(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT fase FROM consultations WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"Errore get_fase: {e}")
        return 0

def set_fase(phone, fase, piano_scheduled_at=None):
    try:
        conn = get_db()
        cur = conn.cursor()
        if piano_scheduled_at:
            cur.execute("""
                INSERT INTO consultations (phone, fase, piano_scheduled_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (phone) DO UPDATE
                SET fase = EXCLUDED.fase, piano_scheduled_at = EXCLUDED.piano_scheduled_at
            """, (phone, fase, piano_scheduled_at))
        else:
            cur.execute("""
                INSERT INTO consultations (phone, fase)
                VALUES (%s, %s)
                ON CONFLICT (phone) DO UPDATE SET fase = EXCLUDED.fase
            """, (phone, fase))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore set_fase: {e}")

def set_start_date(phone, start_date):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consultations (phone, start_date)
            VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE
            SET start_date = EXCLUDED.start_date, renewal_sent = FALSE
        """, (phone, start_date))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore set_start_date: {e}")

def get_pianos_to_send():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT phone FROM consultations
            WHERE fase = 3 AND piano_scheduled_at <= NOW()
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [r["phone"] for r in rows]
    except Exception as e:
        logger.error(f"Errore get_pianos_to_send: {e}")
        return []

# ─── AUDIO ─────────────────────────────────────────────────────────────────────
def transcribe_audio(media_url):
    try:
        response = requests.get(
            media_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=30
        )
        audio_file = io.BytesIO(response.content)
        audio_file.name = "audio.ogg"
        transcript = openai_client.audio.transcriptions.create(
            model=MODEL_AUDIO,
            file=audio_file
        )
        return transcript.text
    except Exception as e:
        logger.error(f"Errore trascrizione audio: {e}")
        return None

# ─── AI ────────────────────────────────────────────────────────────────────────
def openai_chat_completion(model, messages, max_tokens=1000, temperature=None, response_format=None, timeout=60):
    """Wrapper robusto per Chat Completions: prova fallback se un modello non supporta alcuni parametri."""
    base_kwargs = {
        "model": model,
        "messages": messages,
        "timeout": timeout
    }
    if max_tokens is not None:
        base_kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        base_kwargs["temperature"] = temperature
    if response_format is not None:
        base_kwargs["response_format"] = response_format

    attempts = []
    attempts.append(dict(base_kwargs))

    no_temp = dict(base_kwargs)
    no_temp.pop("temperature", None)
    attempts.append(no_temp)

    if "max_tokens" in base_kwargs:
        max_completion = dict(base_kwargs)
        max_completion["max_completion_tokens"] = max_completion.pop("max_tokens")
        attempts.append(max_completion)

        max_completion_no_temp = dict(max_completion)
        max_completion_no_temp.pop("temperature", None)
        attempts.append(max_completion_no_temp)

    no_format = dict(base_kwargs)
    no_format.pop("response_format", None)
    attempts.append(no_format)

    last_error = None
    seen = set()
    for kwargs in attempts:
        key = tuple(sorted(kwargs.keys())) + tuple((k, str(v)) for k, v in kwargs.items() if k in ("model", "max_tokens", "max_completion_tokens", "temperature"))
        if key in seen:
            continue
        seen.add(key)
        try:
            return openai_client.chat.completions.create(**kwargs)
        except Exception as e:
            last_error = e
            logger.warning(f"OpenAI retry con parametri diversi per modello {model}: {e}")
    raise last_error


def parse_json_safely(text, default=None):
    if default is None:
        default = {}
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception:
        pass
    return default


def get_recent_history(phone, limit=30):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT role, content FROM messages
               WHERE phone = %s
               ORDER BY timestamp DESC
               LIMIT %s""",
            (phone, limit)
        )
        rows = list(reversed(cur.fetchall()))
        cur.close()
        conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        logger.error(f"Errore lettura recent history: {e}")
        return []


def link_gia_inviato(phone):
    """Controlla se uno dei link del percorso è già stato inviato."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM messages
            WHERE phone = %s AND role = 'assistant'
            AND (
                content LIKE %s OR
                content LIKE %s
            )
        """, (phone, f"%{LINK_BASE.replace('https://', '')}%", f"%{LINK_PREMIUM.replace('https://', '')}%"))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return bool(result and int(result[0]) > 0)
    except Exception as e:
        logger.error(f"Errore link_gia_inviato: {e}")
        return True


def user_chiede_link(router_result, pending_text):
    if router_result and router_result.get("intent") == "richiesta_link":
        return True
    entities = router_result.get("entities", {}) if router_result else {}
    if entities.get("asks_for_link"):
        return True
    t = (pending_text or "").lower()
    return "link" in t or "dove acquisto" in t or "dove posso acquist" in t


def get_child_profile(phone):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM child_profiles WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Errore get_child_profile: {e}")
        return {}


def upsert_child_profile(phone, data):
    if not data or not isinstance(data, dict):
        return
    allowed = [
        "mother_name", "child_name", "child_age", "birth_date", "main_problem", "goal",
        "sleep_association", "night_wakings", "naps", "bedtime", "wake_time",
        "sleep_place", "feeding", "father_role", "health_notes", "work_stage", "admin_notes"
    ]
    clean = {k: (str(v).strip() if v is not None else None) for k, v in data.items() if k in allowed and str(v).strip() not in ("", "null", "None")}
    if not clean:
        return
    try:
        conn = get_db()
        cur = conn.cursor()
        columns = ["phone"] + list(clean.keys())
        values = [phone] + list(clean.values())
        placeholders = ", ".join(["%s"] * len(columns))
        update_clause = ", ".join([f"{c} = COALESCE(EXCLUDED.{c}, child_profiles.{c})" for c in clean.keys()])
        cur.execute(f"""
            INSERT INTO child_profiles ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (phone) DO UPDATE SET
            {update_clause},
            updated_at = NOW()
        """, values)
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Profilo bambino aggiornato per {phone}: {list(clean.keys())}")
    except Exception as e:
        logger.error(f"Errore upsert_child_profile: {e}")


def profile_to_text(profile):
    if not profile:
        return "Nessun profilo strutturato ancora disponibile. Usa lo storico recente."
    labels = {
        "mother_name": "Nome mamma",
        "child_name": "Nome bambino",
        "child_age": "Età",
        "birth_date": "Data nascita",
        "main_problem": "Problema principale",
        "goal": "Obiettivo",
        "sleep_association": "Addormentamento/aiuto sonno",
        "night_wakings": "Risvegli notturni",
        "naps": "Pisolini",
        "bedtime": "Nanna serale",
        "wake_time": "Sveglia mattina",
        "sleep_place": "Dove dorme",
        "feeding": "Alimentazione",
        "father_role": "Ruolo papà",
        "health_notes": "Note salute",
        "work_stage": "Fase di lavoro",
        "admin_notes": "Note Paola"
    }
    parts = []
    for key, label in labels.items():
        value = profile.get(key)
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts) if parts else "Profilo presente ma ancora povero di dati."


def extract_child_profile_from_history(phone):
    """Estrae/aggiorna profilo bambino dal questionario e dallo storico."""
    history = get_history(phone, days=45)
    if not history:
        return
    text_history = "\n".join([f"{m['role']}: {m['content']}" for m in history[-80:]])
    messages = [
        {"role": "system", "content": "Estrai dati strutturati da una chat di consulenza sonno infantile. Rispondi solo JSON valido. Non inventare dati mancanti."},
        {"role": "user", "content": f"""
Dalla chat seguente estrai questi campi se presenti:
mother_name, child_name, child_age, birth_date, main_problem, goal, sleep_association, night_wakings, naps, bedtime, wake_time, sleep_place, feeding, father_role, health_notes, work_stage, admin_notes.

Regole:
- Non inventare.
- Se un campo non è chiaro, omettilo.
- work_stage deve essere una breve etichetta utile tra: osservazione_iniziale, routine_orari, dissociazione_seno_sonno, appoggio_culla, gestione_risvegli, pisolini_diurni, consolidamento, regressione_dentizione_malattia, rientro_lavoro_nido, altro.

Chat:
{text_history}
"""}
    ]
    try:
        response = openai_chat_completion(
            model=MODEL_PROFILE,
            messages=messages,
            max_tokens=900,
            temperature=0,
            response_format={"type": "json_object"},
            timeout=60
        )
        data = parse_json_safely(response.choices[0].message.content, {})
        upsert_child_profile(phone, data)
    except Exception as e:
        logger.error(f"Errore estrazione profilo bambino per {phone}: {e}")
        threading.Thread(target=send_telegram, args=[f"⚠️ Errore estrazione profilo per {phone}: {e}"], daemon=True).start()


def classify_message(phone, fase, pending_text, image_url=None):
    recent = get_recent_history(phone, limit=12)
    recent_text = "\n".join([f"{m['role']}: {m['content'][:500]}" for m in recent])
    profile_text = profile_to_text(get_child_profile(phone))
    messages = [
        {"role": "system", "content": ROUTER_PROMPT},
        {"role": "user", "content": f"""
Fase attuale: {fase}
Ha immagine allegata: {bool(image_url)}
Link già inviato: {link_gia_inviato(phone)}

Profilo bambino:
{profile_text}

Storico recente:
{recent_text}

Ultimi messaggi da classificare:
{pending_text or "(vuoto)"}
"""}
    ]
    default = {
        "intent": "altro",
        "confidence": 0.0,
        "safe_auto_reply": True,
        "needs_human": False,
        "reason": "fallback",
        "message_type": "altro",
        "entities": {"medical_topic": False, "asks_for_link": False}
    }
    try:
        response = openai_chat_completion(
            model=MODEL_ROUTER,
            messages=messages,
            max_tokens=500,
            temperature=TEMP_ROUTER,
            response_format={"type": "json_object"},
            timeout=60
        )
        data = parse_json_safely(response.choices[0].message.content, default)
        if not isinstance(data, dict):
            return default
        data.setdefault("intent", "altro")
        data.setdefault("confidence", 0.0)
        data.setdefault("safe_auto_reply", True)
        data.setdefault("needs_human", False)
        data.setdefault("reason", "")
        data.setdefault("message_type", "altro")
        data.setdefault("entities", {})
        return data
    except Exception as e:
        logger.error(f"Errore router per {phone}: {e}")
        threading.Thread(target=send_telegram, args=[f"⚠️ Errore router per {phone}: {e}"], daemon=True).start()
        return default


def get_business_rule(intent, fase, link_sent=False):
    """Regole specifiche passate al generatore solo quando servono."""
    if intent == "richiesta_differenza_percorsi":
        return f"""
Spiega la differenza tra i percorsi in modo naturale.
Il percorso da {OFFERS['base']['price']} euro include questionario iniziale, piano personalizzato e 30 giorni di supporto WhatsApp nei giorni lavorativi.
Il Premium da {OFFERS['premium']['price']} euro dura 60 giorni, dà maggiore continuità e supporto anche nei weekend.
Consiglia il Premium se la situazione è complessa o la mamma vuole essere seguita con più continuità, ma rassicura che anche il percorso da 37 euro va bene.
Non spingere in modo aggressivo.
"""
    if intent == "obiezione_prezzo":
        return """
Rispondi all'obiezione sul prezzo con calore e concretezza.
Spiega che il valore non è solo il PDF, ma il questionario, il piano su misura e il supporto WhatsApp passo passo.
Non fare pressione.
"""
    if intent == "richiesta_rimborso":
        return f"""
Rispondi prima con empatia, senza tono freddo.
Chiedi in modo naturale cosa non ha funzionato e se puoi sistemare qualcosa.
Se dal messaggio è chiaro che vuole la procedura formale, aggiungi questo link: {LINK_REFUND}
Ricorda con delicatezza che il rimborso non è applicabile a chi ha già usufruito in parte o totalmente delle consulenze.
"""
    if intent in ("richiesta_info_percorso", "descrizione_problema_sonno", "richiesta_consiglio_gratuito") and fase == 0:
        if link_sent:
            return """
La persona è ancora lead e il link è già stato mandato.
Non ripetere il link, a meno che lo chieda espressamente.
Rispondi con empatia, senza dare un piano gratuito completo.
Accenna alla direzione di lavoro e mantieni la conversazione naturale.
"""
        return f"""
La persona è ancora lead e non ha acquistato.
Non dare un piano gratuito completo e non dare una sequenza dettagliata di azioni.
Mostra che hai capito la difficoltà specifica.
Spiega che lavori con percorsi personalizzati perché ogni bambino ha età, abitudini e bisogni diversi.
Presenta il Percorso Premium: 60 giorni di supporto WhatsApp personalizzato al costo di {OFFERS['premium']['price']} euro, con questionario iniziale, piano su misura e guide PDF.
Inserisci il link una sola volta: {LINK_PREMIUM}
Chiudi dicendo che dopo l'ordine può scriverti su WhatsApp e partite con l'analisi personalizzata.
"""
    if intent in ("domanda_percorso_attivo", "aggiornamento_percorso_attivo", "richiesta_pratica_immediata") or fase == 4:
        return """
La persona è in percorso attivo.
Rispondi collegandoti al profilo bambino e allo storico recente.
Dai massimo 1 o 2 indicazioni pratiche, non cambiare troppe cose insieme.
Se è una richiesta immediata, rispondi breve e operativo.
Se è un aggiornamento, valorizza o normalizza in modo specifico.
Non parlare di scadenze, rinnovi o fine percorso.
"""
    if intent == "dubbio_medico_lieve":
        return """
Rispondi in modo prudente.
Per la parte sanitaria rimanda al pediatra, poi dai solo una cornice generale sul sonno senza diagnosi e senza indicazioni mediche.
"""
    return """
Rispondi in modo naturale come Paola, rispettando il contesto, senza aggiungere link o offerte se non servono.
"""


def direct_reply_for_intent(phone, fase, router_result, pending_text):
    """Risposte fisse solo per intenti sicuri. Altrimenti torna None e risponde GPT."""
    intent = router_result.get("intent", "altro") if router_result else "altro"
    confidence = float(router_result.get("confidence", 0) or 0) if router_result else 0

    if intent == "saluto_vago" and fase == 0 and confidence >= 0.75:
        return "Ciao, sono Paola 😊\n\nSe ti va, scrivimi pure in poche parole qual e la difficolta principale che stai vivendo con il sonno del tuo bimbo, cosi capisco meglio come aiutarti."

    if intent == "intenzione_acquisto_non_completato" and fase == 0 and confidence >= 0.75:
        return "Perfetto, ti aspetto qui. Effettua l'ordine dal link e poi scrivimi quando hai completato, cosi iniziamo subito 🤍"

    if intent == "richiesta_link" and confidence >= 0.75:
        return f"Certo, ti lascio il link: {LINK_PREMIUM}"

    if intent == "richiesta_bonifico" and confidence >= 0.85:
        return (
            "Certo, puoi pagare tramite bonifico. Ecco le coordinate:\n\n"
            "Intestatario: P&D Digital\n"
            "IBAN: NL10BUNQ2192297467\n\n"
            "Importo: 37 euro\n"
            "Causale: il tuo nome e cognome\n\n"
            "Dimmi quando hai effettuato il bonifico cosi iniziamo 🤍"
        )

    if intent == "problema_checkout_importo" and confidence >= 0.85:
        return (
            "L'unica spiegazione e che hai aggiunto il prodotto piu volte nel carrello.\n"
            "In alto a destra vedi l'icona di una borsetta — cliccaci sopra, guarda quanti articoli ci sono "
            "e cambia il numero a 1. Poi procedi al pagamento e ti deve uscire 37 euro 🤍"
        )

    if intent == "bonifico_effettuato" and confidence >= 0.80:
        return "Perfetto, appena verifico il pagamento ti avvio il questionario cosi partiamo con l'analisi personalizzata 🤍"

    if intent == "messaggio_cortesia" and confidence >= 0.80:
        if fase == 0:
            return "Certo, quando vuoi 🤍"
        return "Va bene 🤍"

    return None


def should_hold_for_human(router_result):
    if not router_result:
        return False
    intent = router_result.get("intent", "")
    if router_result.get("needs_human") is True:
        return True
    if intent in {"dubbio_medico_delicato", "sospetto_ai_o_richiesta_paola", "necessita_revisione_umano"}:
        return True
    return False


def validate_reply(reply, context):
    if not reply:
        return None, "risposta vuota"

    clean = reply.strip()
    clean = clean.replace("!", ".")
    clean = re.sub(r"\*\*|__|###?|^- ", "", clean, flags=re.MULTILINE)

    # Evita link ripetuti se la mamma non lo ha chiesto.
    if context.get("link_sent") and not context.get("asks_link") and "genitorinarmonia.com/products/" in clean:
        lines = [line for line in clean.splitlines() if "genitorinarmonia.com/products/" not in line]
        clean = "\n".join(lines).strip()

    banned_phrases = [
        "grazie per aver condiviso",
        "capisco perfettamente",
        "in conclusione",
        "ecco cosa puoi fare",
        "associazione seno-sonno",
        "igiene del sonno",
        "stimolazione cognitiva",
        "garantito",
        "devi assolutamente",
        "consulenza scaduta",
        "percorso è terminato",
        "percorso e terminato"
    ]
    lower = clean.lower()
    for phrase in banned_phrases:
        if phrase in lower:
            return clean, f"frase vietata: {phrase}"

    return clean, None


def rewrite_reply_if_needed(reply, issue, context):
    if not issue:
        return reply
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_BASE},
        {"role": "user", "content": f"""
Riscrivi questo messaggio in modo più naturale, breve e da WhatsApp, eliminando il problema: {issue}.
Non aggiungere link se non richiesto.
Non parlare di consulenza scaduta o fine percorso.

Messaggio da riscrivere:
{reply}
"""}
    ]
    try:
        response = openai_chat_completion(
            model=MODEL_CHAT,
            messages=messages,
            max_tokens=800,
            temperature=0.35,
            timeout=60
        )
        rewritten = response.choices[0].message.content.strip()
        clean, issue2 = validate_reply(rewritten, context)
        if issue2:
            logger.warning(f"Riscrittura ancora problematica: {issue2}")
        return clean
    except Exception as e:
        logger.error(f"Errore riscrittura risposta: {e}")
        return reply


def build_ai_context(phone, fase, router_result, pending_text):
    link_sent = link_gia_inviato(phone)
    asks_link = user_chiede_link(router_result, pending_text)
    profile = get_child_profile(phone)
    return {
        "fase": fase,
        "link_sent": link_sent,
        "asks_link": asks_link,
        "profile_text": profile_to_text(profile),
        "business_rule": get_business_rule(router_result.get("intent", "altro") if router_result else "altro", fase, link_sent),
        "recent_history": get_recent_history(phone, limit=30),
        "pending_text": pending_text
    }


def get_ai_response(phone, image_url=None, router_result=None):
    pending = get_messages_since_last_reply(phone)
    user_message = "\n".join(pending) if pending else "(nessun nuovo messaggio)"
    fase = get_fase(phone)

    if router_result is None:
        router_result = classify_message(phone, fase, user_message, image_url=image_url)

    direct = direct_reply_for_intent(phone, fase, router_result, user_message)
    if direct:
        return direct

    context = build_ai_context(phone, fase, router_result, user_message)

    if image_url:
        try:
            img_response = requests.get(image_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
            img_data = base64.b64encode(img_response.content).decode("utf-8")
            content_type = img_response.headers.get("Content-Type", "image/jpeg")
            user_content = [
                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{img_data}"}},
                {"type": "text", "text": user_message}
            ]
        except Exception as e:
            logger.error(f"Errore download immagine: {e}")
            user_content = user_message
    else:
        user_content = user_message

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_BASE},
        {"role": "system", "content": CHAT_RESPONSE_PROMPT},
        {"role": "system", "content": f"""
Contesto operativo:
Fase: {fase}
Intento rilevato: {router_result.get('intent', 'altro')}
Confidenza router: {router_result.get('confidence', 0)}
Tipo messaggio: {router_result.get('message_type', 'altro')}
Link già inviato: {context['link_sent']}
La mamma chiede il link: {context['asks_link']}

Regola business per questa risposta:
{context['business_rule']}

Profilo bambino:
{context['profile_text']}
"""}
    ]
    messages.extend(context["recent_history"])
    messages.append({"role": "user", "content": user_content})

    try:
        response = openai_chat_completion(
            model=MODEL_CHAT,
            messages=messages,
            max_tokens=1800,
            temperature=TEMP_CHAT,
            timeout=60
        )
        reply = response.choices[0].message.content.strip()
        clean, issue = validate_reply(reply, context)
        clean = rewrite_reply_if_needed(clean, issue, context) if issue else clean
        return clean.strip() if clean else None
    except Exception as e:
        logger.error(f"Errore OpenAI: {e}")
        threading.Thread(target=send_telegram, args=[f"⚠️ Errore OpenAI per {phone}: {e}"], daemon=True).start()
        return None


def is_immediate_question(text):
    """Serve solo a ridurre il timer, non decide la risposta."""
    if not text:
        return False
    t = text.lower()
    patterns = [
        "che faccio", "cosa faccio", "che devo fare", "come mi muovo",
        "lo sveglio", "la sveglio", "lo attacco", "la attacco",
        "adesso", "ora", "si è svegliato", "si e svegliato", "si è svegliata", "si e svegliata"
    ]
    return any(p in t for p in patterns) and "?" in text or any(p in t for p in ["che faccio", "cosa faccio", "lo sveglio", "la sveglio"])

# ─── INVIO ─────────────────────────────────────────────────────────────────────
def send_whatsapp_message(phone, text):
    chunks = []
    while len(text) > 1000:
        split_point = text.rfind('\n', 0, 1000)
        if split_point == -1:
            split_point = 1000
        chunks.append(text[:split_point].strip())
        text = text[split_point:].strip()
    if text:
        chunks.append(text)
    for chunk in chunks:
        try:
            twilio_client.messages.create(
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=f"whatsapp:{phone}",
                body=chunk
            )
            # Notifica nel topic
            threading.Thread(target=send_to_topic, args=[phone, chunk, True], daemon=True).start()
            if len(chunks) > 1:
                time.sleep(1)
        except Exception as e:
            logger.error(f"Errore invio a {phone}: {e}")
            threading.Thread(target=send_telegram, args=[f"⚠️ Errore Twilio per {phone}: {e}"], daemon=True).start()

def send_piano(phone):
    logger.info(f"Generazione piano per {phone}")

    # Aggiorna profilo strutturato prima del piano, senza interrompere se fallisce.
    try:
        extract_child_profile_from_history(phone)
    except Exception as e:
        logger.error(f"Errore estrazione profilo prima del piano: {e}")

    history = get_history(phone)
    profile_text = profile_to_text(get_child_profile(phone))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_BASE},
        {"role": "system", "content": PLAN_PROMPT},
        {"role": "system", "content": f"Profilo bambino strutturato:\n{profile_text}"}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": (
        "Genera ora il piano personalizzato completo.\n\n"
        "[ISTRUZIONE SISTEMA: Genera il piano personalizzato COMPLETO e DETTAGLIATO adesso, "
        "basandoti su tutto quello che la mamma ha raccontato nel questionario. "
        "Inizia direttamente con il piano senza premesse. "
        "Usa il nome del bambino. Sii specifico sulla sua situazione.]"
    )})
    try:
        response = openai_chat_completion(
            model=MODEL_PLAN,
            messages=messages,
            max_tokens=5000,
            temperature=TEMP_PLAN,
            timeout=90
        )
        piano = response.choices[0].message.content.strip()
        context = {"link_sent": True, "asks_link": False}
        piano, issue = validate_reply(piano, context)
        if issue:
            piano = rewrite_reply_if_needed(piano, issue, context)
    except Exception as e:
        logger.error(f"Errore generazione piano: {e}")
        threading.Thread(target=send_telegram, args=[f"⚠️ Errore piano per {phone}: {e}"], daemon=True).start()
        return
    save_message(phone, "assistant", piano)
    send_whatsapp_message(phone, piano)
    set_fase(phone, 4)
    set_start_date(phone, datetime.now().date())

# ─── SEQUENZA ACQUISTO ─────────────────────────────────────────────────────────
def invia_sequenza_acquisto(phone):
    if get_fase(phone) != 0:
        logger.info(f"Sequenza acquisto gia avviata per {phone} — skip")
        return

    set_fase(phone, 1)
    logger.info(f"Avvio sequenza acquisto per {phone}")

    save_message(phone, "assistant", MSG_BENVENUTO)
    send_whatsapp_message(phone, MSG_BENVENUTO)
    time.sleep(3)

    save_message(phone, "assistant", MSG_REGOLE)
    send_whatsapp_message(phone, MSG_REGOLE)
    time.sleep(3)

    save_message(phone, "assistant", MSG_QUESTIONARIO_1)
    send_whatsapp_message(phone, MSG_QUESTIONARIO_1)
    logger.info(f"Sequenza acquisto completata per {phone}")

# ─── ELABORAZIONE RISPOSTA ─────────────────────────────────────────────────────
def process_response(phone, image_url=None):
    with active_timers_lock:
        active_timers.pop(phone, None)

    fase = get_fase(phone)
    logger.info(f"process_response per {phone} — fase {fase}")

    pending = get_messages_since_last_reply(phone)
    combined_raw = "\n".join(pending)
    combined = combined_raw.lower().strip()

    # Router semantico: non invia nulla, serve solo per decidere meglio.
    router_result = classify_message(phone, fase, combined_raw, image_url=image_url)
    logger.info(f"Router per {phone}: {router_result}")

    if should_hold_for_human(router_result):
        threading.Thread(
            target=send_telegram,
            args=[f"⚠️ Revisione manuale consigliata per {phone}\nIntento: {router_result.get('intent')}\nMotivo: {router_result.get('reason')}\nMessaggio:\n{combined_raw}"],
            daemon=True
        ).start()
        return

    if fase == 0:
        parole_acquisto = [
            "ho acquistato", "ho comprato", "ho fatto l'ordine", "ho effettuato l'ordine",
            "ho preso il pacchetto", "ho preso il percorso", "ho pagato", "ho fatto il pagamento",
            "ordine completato", "pagamento completato", "l'ho preso",
            "l'ho comprato", "l'ho acquistato", "ho fatto l'acquisto"
        ]
        is_acquisto = any(p in combined for p in parole_acquisto)
        if router_result.get("intent") == "acquisto_completato" and float(router_result.get("confidence", 0) or 0) >= 0.75:
            is_acquisto = True

        # Manteniamo il controllo immagine/ricevuta del vecchio codice.
        if not is_acquisto and image_url:
            try:
                img_response = requests.get(image_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
                img_data = base64.b64encode(img_response.content).decode("utf-8")
                content_type = img_response.headers.get("Content-Type", "image/jpeg")
                check_response = openai_chat_completion(
                    model=MODEL_CHAT,
                    messages=[
                        {"role": "system", "content": "Rispondi SOLO con SI o NO."},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{img_data}"}},
                            {"type": "text", "text": "Questa immagine mostra una conferma d'ordine o ricevuta di pagamento?"}
                        ]}
                    ],
                    max_tokens=5,
                    temperature=0,
                    timeout=60
                )
                if check_response.choices[0].message.content.strip().lower().startswith("si"):
                    is_acquisto = True
            except Exception as e:
                logger.error(f"Errore check immagine: {e}")
                threading.Thread(target=send_telegram, args=[f"⚠️ Errore classificatore immagine per {phone}: {e}"], daemon=True).start()

        if is_acquisto:
            invia_sequenza_acquisto(phone)
            return

        ai_reply = get_ai_response(phone, image_url=image_url, router_result=router_result)
        if ai_reply:
            save_message(phone, "assistant", ai_reply)
            send_whatsapp_message(phone, ai_reply)

    elif fase == 1:
        # Controlla se la mamma ha risposto concretamente o solo messaggi di cortesia.
        ha_risposto = router_result.get("intent") == "risposta_questionario_concreta" and float(router_result.get("confidence", 0) or 0) >= 0.65
        if not ha_risposto:
            # Fallback vecchio classificatore, mantenuto per sicurezza.
            try:
                check_response = openai_chat_completion(
                    model=MODEL_CLASSIFIER,
                    messages=[
                        {"role": "system", "content": "Sei un classificatore. Rispondi SOLO con HA_RISPOSTO o NON_HA_RISPOSTO. HA_RISPOSTO se la mamma ha scritto informazioni concrete su se stessa o sul bambino (nome, eta, routine, sonno, orari, ecc.). NON_HA_RISPOSTO se ha scritto solo messaggi generici di cortesia o rinvio (ok, grazie, ci penso, ti rispondo domani, dopo, perfetto, ecc.)."},
                        {"role": "user", "content": f"Messaggi della mamma: '{combined_raw}'"}
                    ],
                    max_tokens=10,
                    temperature=0,
                    timeout=60
                )
                risposta = check_response.choices[0].message.content.strip().upper()
                ha_risposto = "HA_RISPOSTO" in risposta
                logger.info(f"Classificatore fase 1 per {phone}: {risposta}")
            except Exception as e:
                logger.error(f"Errore classificatore fase 1: {e}")
                threading.Thread(target=send_telegram, args=[f"⚠️ Errore classificatore fase 1 per {phone}: {e}"], daemon=True).start()
                ha_risposto = True

        if ha_risposto:
            time.sleep(300)
            save_message(phone, "assistant", MSG_QUESTIONARIO_2)
            send_whatsapp_message(phone, MSG_QUESTIONARIO_2)
            set_fase(phone, 2)
            logger.info(f"Questionario parte 2 inviato a {phone}")
        else:
            logger.info(f"Fase 1 per {phone} — mamma non ha risposto concretamente, bot in attesa")

    elif fase == 2:
        ha_risposto = router_result.get("intent") == "risposta_questionario_concreta" and float(router_result.get("confidence", 0) or 0) >= 0.65
        if not ha_risposto:
            try:
                check_response = openai_chat_completion(
                    model=MODEL_CLASSIFIER,
                    messages=[
                        {"role": "system", "content": "Sei un classificatore. Rispondi SOLO con HA_RISPOSTO o NON_HA_RISPOSTO. HA_RISPOSTO se la mamma ha scritto informazioni concrete su se stessa o sul bambino (nome, eta, routine, sonno, orari, ecc.). NON_HA_RISPOSTO se ha scritto solo messaggi generici di cortesia o rinvio (ok, grazie, ci penso, ti rispondo domani, dopo, perfetto, ecc.)."},
                        {"role": "user", "content": f"Messaggi della mamma: '{combined_raw}'"}
                    ],
                    max_tokens=10,
                    temperature=0,
                    timeout=60
                )
                risposta = check_response.choices[0].message.content.strip().upper()
                ha_risposto = "HA_RISPOSTO" in risposta
                logger.info(f"Classificatore fase 2 per {phone}: {risposta}")
            except Exception as e:
                logger.error(f"Errore classificatore fase 2: {e}")
                threading.Thread(target=send_telegram, args=[f"⚠️ Errore classificatore fase 2 per {phone}: {e}"], daemon=True).start()
                ha_risposto = True

        if ha_risposto:
            save_message(phone, "assistant", MSG_CONFERMA_QUESTIONARIO)
            send_whatsapp_message(phone, MSG_CONFERMA_QUESTIONARIO)
            set_fase(phone, 5)
            logger.info(f"Attesa conferma completamento questionario per {phone}")
        else:
            logger.info(f"Fase 2 per {phone} — mamma non ha risposto concretamente, bot in attesa")

    elif fase == 5:
        parole_finito = [
            "si", "sì", "si si", "sì sì", "ho finito", "finito", "ho risposto",
            "risposto", "fatto", "ho fatto", "ecco tutto", "tutto",
            "completato", "ho completato", "pronta", "sono pronta", "yes"
        ]
        ha_finito = any(combined == p or combined.startswith(p + " ") or combined.startswith(p + ",") for p in parole_finito)
        if router_result.get("intent") == "conferma_questionario_finito" and float(router_result.get("confidence", 0) or 0) >= 0.65:
            ha_finito = True

        if not ha_finito:
            try:
                check_response = openai_chat_completion(
                    model=MODEL_CLASSIFIER,
                    messages=[
                        {"role": "system", "content": "Sei un classificatore. Rispondi SOLO con SI o NO. SI se la persona indica in qualsiasi modo che ha finito, completato, risposto a tutto, o e pronta. NO in tutti gli altri casi."},
                        {"role": "user", "content": f"Messaggio: '{combined}'"}
                    ],
                    max_tokens=5,
                    temperature=0,
                    timeout=60
                )
                ha_finito = check_response.choices[0].message.content.strip().lower().startswith("si")
            except Exception as e:
                logger.error(f"Errore check conferma: {e}")
                threading.Thread(target=send_telegram, args=[f"⚠️ Errore classificatore fase 5 per {phone}: {e}"], daemon=True).start()
                ha_finito = False

        if ha_finito:
            try:
                extract_child_profile_from_history(phone)
            except Exception as e:
                logger.error(f"Errore estrazione profilo in fase 5: {e}")
            piano_time = datetime.now() + timedelta(hours=1)
            set_fase(phone, 3, piano_scheduled_at=piano_time)
            logger.info(f"Piano schedulato per {phone} alle {piano_time}")
        else:
            risposta = "Ok, tranquilla. Quando hai finito scrivimi 'ho finito' cosi so che posso iniziare a prepararti il piano 🤍"
            save_message(phone, "assistant", risposta)
            send_whatsapp_message(phone, risposta)
            set_fase(phone, 6)
            logger.info(f"Fase 6 per {phone} — silenzio totale")

    elif fase == 6:
        parole_finito = [
            "si", "sì", "si si", "sì sì", "ho finito", "finito", "ho risposto",
            "risposto", "fatto", "ho fatto", "ecco tutto", "tutto",
            "completato", "ho completato", "pronta", "sono pronta", "yes"
        ]
        ha_finito = any(combined == p or combined.startswith(p + " ") or combined.startswith(p + ",") for p in parole_finito)
        if router_result.get("intent") == "conferma_questionario_finito" and float(router_result.get("confidence", 0) or 0) >= 0.65:
            ha_finito = True

        if not ha_finito:
            try:
                check_response = openai_chat_completion(
                    model=MODEL_CLASSIFIER,
                    messages=[
                        {"role": "system", "content": "Sei un classificatore. Rispondi SOLO con SI o NO. SI se la persona indica in qualsiasi modo che ha finito, completato, risposto a tutto, o e pronta. NO in tutti gli altri casi."},
                        {"role": "user", "content": f"Messaggio: '{combined}'"}
                    ],
                    max_tokens=5,
                    temperature=0,
                    timeout=60
                )
                ha_finito = check_response.choices[0].message.content.strip().lower().startswith("si")
            except Exception as e:
                logger.error(f"Errore check conferma fase 6: {e}")
                threading.Thread(target=send_telegram, args=[f"⚠️ Errore classificatore fase 6 per {phone}: {e}"], daemon=True).start()
                ha_finito = False

        if ha_finito:
            try:
                extract_child_profile_from_history(phone)
            except Exception as e:
                logger.error(f"Errore estrazione profilo in fase 6: {e}")
            piano_time = datetime.now() + timedelta(hours=1)
            set_fase(phone, 3, piano_scheduled_at=piano_time)
            logger.info(f"Piano schedulato per {phone} alle {piano_time}")
        else:
            logger.info(f"Fase 6 per {phone} — silenzio totale, mamma non ha ancora finito")

    elif fase == 3:
        logger.info(f"Fase 3 per {phone} — bot in attesa del piano")

    elif fase == 4:
        # Se emergono nuovi dati utili, prova ad aggiornare il profilo senza bloccare la risposta.
        if len(combined_raw) > 120:
            threading.Thread(target=extract_child_profile_from_history, args=[phone], daemon=True).start()
        ai_reply = get_ai_response(phone, image_url=image_url, router_result=router_result)
        if ai_reply:
            save_message(phone, "assistant", ai_reply)
            send_whatsapp_message(phone, ai_reply)

# ─── WEBHOOK WHATSAPP ──────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    phone      = request.form.get("From", "").replace("whatsapp:", "")
    body       = request.form.get("Body", "").strip()
    num_media  = int(request.form.get("NumMedia", 0))
    media_type = request.form.get("MediaContentType0", "")
    media_url  = request.form.get("MediaUrl0", "")

    logger.info(f"Messaggio da {phone}: '{body}' | media: {num_media}")

    message_sid = request.form.get("MessageSid", "")
    if message_sid:
        with processed_sids_lock:
            if message_sid in processed_sids:
                logger.info(f"Duplicato ignorato: {message_sid}")
                return Response("OK", status=200)
            processed_sids.add(message_sid)
            if len(processed_sids) > 1000:
                processed_sids.clear()

    # ── Comandi admin ──────────────────────────────────────────────────────────
    if body.startswith("/inizia"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            set_start_date(target, datetime.now().date())
            set_fase(target, 4)
            # Cancella timer attivo se presente
            with active_timers_lock:
                if target in active_timers:
                    active_timers[target].cancel()
                    active_timers.pop(target, None)
        return Response("OK", status=200)

    if body.startswith("/pausa"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            set_fase(target, 99)
        return Response("OK", status=200)

    if body.startswith("/riprendi"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            set_fase(target, 4)
        return Response("OK", status=200)

    if body.startswith("/acquisto"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            threading.Thread(target=invia_sequenza_acquisto, args=[target], daemon=True).start()
        return Response("OK", status=200)

    if body.startswith("/nota"):
        parts = body.strip().split(None, 2)
        if len(parts) >= 3:
            target = parts[1].replace("+", "").replace(" ", "")
            nota = parts[2]
            save_message(target, "user", f"[NOTA ADMIN: {nota}]")
        return Response("OK", status=200)

    if body.startswith("/scrivi"):
        parts = body.strip().split(None, 2)
        if len(parts) >= 3:
            target = parts[1].replace("+", "").replace(" ", "")
            testo = parts[2]
            save_message(target, "assistant", testo)
            send_whatsapp_message(target, testo)
            logger.info(f"Messaggio admin inviato a {target}")
        return Response("OK", status=200)

    if body.startswith("/piano"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            with active_timers_lock:
                if target in active_timers:
                    active_timers[target].cancel()
                    active_timers.pop(target, None)
            threading.Thread(target=send_piano, args=[target], daemon=True).start()
        return Response("OK", status=200)

    if body.startswith("/q1"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            set_fase(target, 1)
            save_message(target, "assistant", MSG_QUESTIONARIO_1)
            send_whatsapp_message(target, MSG_QUESTIONARIO_1)
        return Response("OK", status=200)

    if body.startswith("/q2"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            set_fase(target, 2)
            save_message(target, "assistant", MSG_QUESTIONARIO_2)
            send_whatsapp_message(target, MSG_QUESTIONARIO_2)
        return Response("OK", status=200)

    if body.startswith("/fase"):
        parts = body.strip().split()
        if len(parts) == 3:
            target = parts[1].replace("+", "").replace(" ", "")
            try:
                nuova_fase = int(parts[2])
                set_fase(target, nuova_fase)
                with active_timers_lock:
                    if target in active_timers:
                        active_timers[target].cancel()
                        active_timers.pop(target, None)
                logger.info(f"Fase {nuova_fase} impostata per {target}")
            except ValueError:
                pass
        return Response("OK", status=200)

    if get_fase(phone) == 99:
        logger.info(f"Chat {phone} in pausa — ignorato")
        return Response("OK", status=200)

    text_to_process = body
    image_url_to_process = None

    if num_media > 0 and media_url:
        if media_type.startswith("audio/"):
            transcribed = transcribe_audio(media_url)
            text_to_process = transcribed if transcribed else "[messaggio vocale non comprensibile]"
        elif media_type.startswith("image/"):
            image_url_to_process = media_url
            text_to_process = body or ""
        elif media_type.startswith("video/"):
            send_whatsapp_message(phone, "Non riesco a vedere i video, scrivimi pure qui in chat 🙏")
            return Response("OK", status=200)

    if not text_to_process and not image_url_to_process:
        return Response("OK", status=200)

    save_message(phone, "user", text_to_process or "[immagine]")

    # Notifica nel topic Telegram
    if text_to_process:
        threading.Thread(target=send_to_topic, args=[phone, text_to_process, False], daemon=True).start()

    # ── Orario silenzio (23:00 - 07:00 ora italiana) ──────────────────────────
    if in_orario_silenzio():
        logger.info(f"Orario silenzio — messaggio di {phone} salvato nel DB, nessun timer")
        return Response("OK", status=200)

    with active_timers_lock:
        if phone in active_timers:
            logger.info(f"Timer gia attivo per {phone} — messaggio salvato nel DB")
            return Response("OK", status=200)

        fase = get_fase(phone)
        if fase == 0:
            delay = 300
        elif fase == 1:
            delay = 600
        elif fase == 2:
            delay = 1800
        elif fase == 5:
            delay = 1800   # 30 minuti — aspetta la conferma della mamma
        elif fase == 6:
            delay = 1800   # 30 minuti — silenzio totale, aspetta solo conferma
        elif fase == 4:
            if is_immediate_question(text_to_process):
                delay = random.randint(180, 420)
            else:
                delay = random.randint(1800, 2400)
        else:
            delay = 5

        timer = threading.Timer(delay, process_response, args=[phone, image_url_to_process])
        active_timers[phone] = timer
        timer.start()
        logger.info(f"Timer avviato per {phone} — delay {delay}s — fase {fase}")

    return Response("OK", status=200)

# ─── JOB BACKGROUND ────────────────────────────────────────────────────────────
def background_job():
    risveglio_fatto = False
    while True:
        try:
            # Invia piani schedulati solo fuori orario silenzio
            if not in_orario_silenzio():
                for phone in get_pianos_to_send():
                    send_piano(phone)

            # Risveglio mattutino — alle 07:00 crea timer per messaggi notturni
            try:
                tz = pytz.timezone(TIMEZONE)
                ora_locale = datetime.now(tz)
                ora = ora_locale.hour
                if ora >= 7 and not risveglio_fatto:
                    risveglio_fatto = True
                    logger.info("Risveglio mattutino — controllo messaggi notturni")
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT DISTINCT m.phone FROM messages m
                        LEFT JOIN consultations c ON c.phone = m.phone
                        WHERE m.role = 'user'
                        AND (c.fase IS NULL OR c.fase NOT IN (3, 99))
                        AND m.timestamp > NOW() - INTERVAL '12 hours'
                        AND m.timestamp > COALESCE(
                            (SELECT MAX(timestamp) FROM messages m2
                             WHERE m2.phone = m.phone AND m2.role = 'assistant'),
                            NOW() - INTERVAL '30 days'
                        )
                    """)
                    phones_da_rispondere = [r[0] for r in cur.fetchall()]
                    cur.close()
                    conn.close()
                    for p in phones_da_rispondere:
                        with active_timers_lock:
                            if p not in active_timers:
                                fase = get_fase(p)
                                if fase == 0:
                                    delay = 60
                                elif fase == 4:
                                    delay = random.randint(300, 600)
                                else:
                                    delay = 30
                                timer = threading.Timer(delay, process_response, args=[p, None])
                                active_timers[p] = timer
                                timer.start()
                                logger.info(f"Timer risveglio mattutino per {p} — delay {delay}s")
                elif ora < 7:
                    risveglio_fatto = False
            except Exception as e:
                logger.error(f"Errore risveglio mattutino: {e}")

        except Exception as e:
            logger.error(f"Errore background job: {e}")
        time.sleep(300)

def setup_telegram_webhook():
    """Registra il webhook Telegram per ricevere risposte dal topic."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        webhook_url = f"https://whatsapp-bot-production-a276.up.railway.app/telegram_webhook"
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message"]},
            timeout=10
        )
        logger.info(f"Telegram webhook impostato: {resp.json()}")
    except Exception as e:
        logger.error(f"Errore setup telegram webhook: {e}")

# ─── AVVIO ─────────────────────────────────────────────────────────────────────
def startup():
    init_db()
    threading.Thread(target=background_job, daemon=True).start()
    setup_telegram_webhook()
    logger.info("Bot avviato")

if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    startup()


