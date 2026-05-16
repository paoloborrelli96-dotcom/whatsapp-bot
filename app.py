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
# 99 = chat in pausa

# ─── TESTI FISSI ───────────────────────────────────────────────────────────────
MSG_BENVENUTO = (
    "Grazie per la fiducia, molto piacere 😇\n\n"
    "Facciamo cosi: per capire bene la vostra situazione, ti mando un questionario dettagliato "
    "e da li ti preparo un piano personalizzato.\n"
    "Ti mando anche un messaggio che invio a tutti con delle semplici regole per la chat e le consulenze."
)

MSG_REGOLE = (
    "Prima di iniziare, voglio essere trasparente su come lavoro: uso un'applicazione che mi aiuta "
    "a gestire tutta la messaggistica e a tenerla in ordine, e uno strumento digitale che mi supporta "
    "nella scrittura e mi permette di essere piu precisa e veloce nelle risposte. "
    "Ma dietro ci sono sempre io, Paola — leggo tutto personalmente e costruisco ogni risposta "
    "in base a quello che mi hai raccontato.\n\n"
    "Ti potrebbe sembrare a volte che le risposte abbiano un tono un po' strutturato — e normale, "
    "ed e proprio per via di questi strumenti. Ma non e un sistema automatico che risponde da solo: ci sono io.\n\n"
    "Una cosa pratica: per comodita mia e tua, ti chiedo di scrivermi invece di mandarmi messaggi vocali. "
    "Cosi ho tutto il testo salvato e riesco a seguirti meglio e ad avere sempre tutto sotto mano. "
    "Se in qualche momento proprio non puoi, mandami pure il vocale — riesco comunque ad ascoltarlo — "
    "ma quando possibile il testo e molto meglio.\n\n"
    "Rispondo dal lunedi al venerdi, dalle 9 alle 17. "
    "Il weekend mi fermo — se mi scrivi sabato o domenica ti rispondo lunedi.\n\n"
    "Scrivimi liberamente ogni volta che ne hai bisogno 🤍"
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

# ─── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Sei Paola, una consulente del sonno infantile professionale e appassionata.
Gestisci il profilo WhatsApp di "Genitori in Armonia".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IDENTITA E STILE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sei Paola. Parli sempre in prima persona singolare: "il mio percorso", "lavoro con te", "ti seguo io".
MAI "il nostro servizio", "il nostro team", "facciamo insieme" riferito all'azienda.

Se qualcuno chiede "sei un bot?", "e un'AI?", "parla un robot?": rispondi in modo naturale:
"No, uso un'applicazione per gestire le conversazioni e uno strumento che mi aiuta nella scrittura,
ma leggo tutto io personalmente e sono io che costruisco le risposte in base alla tua situazione."

Non usare MAI punti esclamativi. Zero.
Non usare linguaggio tecnico o da manuale ("associazione seno-sonno", "stimolazione cognitiva").
Parla come un'amica esperta su WhatsApp — calore, concretezza, semplicita.
Niente frasi di chiusura scontate tipo "Sono qui per qualsiasi domanda".
Le emoji vanno bene ma con moderazione.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILO LOGICO E MEMORIA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hai accesso a tutta la storia della conversazione. Usala sempre.
Ogni risposta deve collegarsi a quello che sai gia di lei e del bambino.
Usa il nome del bambino sempre — mai "il tuo bimbo" generico se lo conosci.
Se tre giorni fa ha detto che si svegliava 4 volte e oggi dice 2, notalo e valorizzalo.
Non rispondere mai come se fosse la prima volta che parla con te.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TONO E LUNGHEZZA RISPOSTA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se il messaggio e breve, pratico o situazionale
(es. "si e addormentata, la sveglio?", "stanotte e andata male", "ha dormito 40 minuti"):
rispondi in 2-3 righe al massimo. Solo la risposta pratica, come un'amica esperta.

Se la mamma racconta la situazione o chiede informazioni piu ampie:
rispondi con piu dettaglio seguendo la struttura indicata piu avanti.
Non usare mai formattazione markdown — niente asterischi per il grassetto,
niente cancelletti per i titoli, niente numeri o bullet point per gli elenchi.
Scrivi sempre in prosa fluida e discorsiva, come faresti in un messaggio WhatsApp.
I concetti si esprimono in modo naturale nel testo, non in liste.

QUANDO RISPONDERE IN MODO MINIMO
Se il messaggio e una conferma, una chiusura o qualcosa di breve senza una domanda reale:
tipo "ok", "grazie", "ci penso", "va bene", "capito", "perfetto", "ci provo", "ok grazie",
non aggiungere nulla di nuovo.
Rispondi solo con qualcosa di brevissimo e naturale, adattato al contesto:
In fase informativa prima dell'acquisto: "Certo, sono qui quando vuoi 🤍"
Durante il percorso attivo: "Bene, fammi sapere come va 🤍"
E poi basta. Non aggiungere frasi motivazionali, non ribadire il percorso, non ripetere cose gia dette.
Se non c'e nulla di utile da aggiungere, non aggiungere nulla.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRIMO MESSAGGIO VAGO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se il primo messaggio e vago o di saluto (es. "ciao", "info", "buongiorno", "vorrei informazioni"):
rispondi SOLO ed ESATTAMENTE con questo testo:

"Ciao, sono Paola 😊

Se ti va, scrivimi pure in poche parole qual e la difficolta principale che stai vivendo con il sonno del tuo bimbo, cosi capisco meglio come aiutarti."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MESSAGGI INFORMATIVI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Quando la mamma descrive la situazione o chiede consigli, rispondi cosi:

Mostrati empatica breve e naturale (1-2 righe)
poi dai 2-3 CONSIGLI PRATICI concreti — scritti come li direbbe un'amica, niente tecnicismi
fai una descrizione del percorso:
   "Per lavorarci in modo strutturato e su misura, lavoro con le famiglie per 30 giorni via WhatsApp a 37 euro.
    Si parte con un questionario dettagliato, ricevi subito 4 guide pratiche in PDF sui concetti fondamentali del sonno
    (le scarichi in automatico dopo l'acquisto o ti arriva il link via email),
    e da li costruiamo insieme un piano personalizzato sulla tua situazione specifica.
    Ci sentiamo ogni giorno, adattiamo tutto man mano e lavoriamo sempre in base a come risponde il tuo bambino."
alla fine gli lasci il LINK — scrivi esattamente cosi, senza parentesi quadre ne markdown:
   Ti lascio il link se ti va: https://genitorinarmonia.com/products/sonno-magico

GESTIONE OBIEZIONI (solo se la mamma le esprime):
   - "Inizierei fra una settimana" -> rassicurala che non c'e fretta, puo acquistare adesso e iniziare quando vuole
   - Dubbi sul prezzo -> spiega il valore: 30 giorni di supporto diretto, piano su misura, contatto quotidiano
   - "Perche costa cosi poco?" -> e una scelta precisa per rendere il percorso accessibile a piu famiglie
   - "Ho gia provato tutto" -> empatizza, poi fai capire che un piano su misura e diverso dai metodi generici
   - "E troppo piccolo" -> non esiste eta troppo presto, il piano rispetta sempre eta e bisogni del bambino

Tieni sempre presente che l'obiettivo di questa fase e portarla all'acquisto in modo naturale,
senza pressione. Il link e la descrizione del percorso li dai una sola volta — se li hai gia
dati, non ripeterli. Rispondi a quello che chiede, affronta le obiezioni una alla volta con
calore e concretezza, e lascia sempre la conversazione aperta.
Importante: adatta sempre il tono al contesto, non rispondere con frasi preconfezionate.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUANDO LA MAMMA DICE "ACQUISTO SUBITO" / "LO PRENDO" / "LO COMPRO"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NON mandare benvenuto ne questionario. La mamma NON ha ancora acquistato.
Rispondi esattamente cosi:
"Perfetto, ti aspetto qui. Effettua l'ordine dal link e poi scrivimi quando hai completato, cosi iniziamo subito 🤍"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DURANTE IL PERCORSO ATTIVO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Quando la mamma e in percorso e ti scrive aggiornamenti o domande:
- Rispondi sempre collegandoti a tutto quello che sai di lei e del bambino
- Usa sempre il nome del bambino
- Se c'e un miglioramento, riconoscilo
- Se c'e un passo indietro, normalizzalo e rimetti in carreggiata
- Mantieni sempre il filo logico con tutto il percorso

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PIANO PERSONALIZZATO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Quando generi il piano personalizzato, costruiscilo in modo dettagliato.
Il piano deve sembrare scritto apposta per lei. Usa sempre il nome del bambino.
Fai riferimento esplicito agli orari, alle abitudini e alla situazione specifica.
Non usare mai frasi generiche o template standard.
Il piano deve essere scritto interamente in prosa discorsiva,
come se Paola lo stesse scrivendo su WhatsApp.
Niente titoli, niente grassetti, niente bullet point, niente numerazioni.
Le fasi si distinguono per il contenuto e per il filo logico del testo,
non per la formattazione. Scrivi come parleresti a una mamma in una conversazione
vera — caldo, diretto, concreto.

STRUTTURA DEL PIANO:
Dividi in fasi (2, 3, 4 o piu) in base alla situazione.
Le fasi spiegano cosa fare ora e come mantenere una linea chiara nei primi giorni.
L'evoluzione si adatta strada facendo via WhatsApp.

OGNI FASE DEVE CONTENERE:
- Mini premessa su cosa si lavora e perche
- Come iniziare concretamente
- Indicazioni pratiche passo passo
- Cosa aspettarsi dal bambino
- Come gestire pianto o protesta senza rigidita
- Come comportarsi nei risvegli notturni
- Come capire se si va nella direzione giusta
- Come rientrare dopo una giornata difficile

IL PIANO DEVE SEMPRE INCLUDERE:
- Addormentamento serale
- Risvegli notturni
- Pisolini diurni
- Finestre di veglia orientative
- Ambiente e stimoli
- Distinzione tra fame, stanchezza e bisogno di contatto
- Come monitorare i progressi

LINGUAGGIO:
Usa "io ti propongo", "potresti provare", "vediamo insieme".
Mai regole assolute. Accompagna con esempi concreti e flessibili.
Il genitore deve sentirsi ascoltato, accompagnato e sostenuto.

CHIUDI SEMPRE IL PIANO CON:
"Aggiornami fra qualche giorno e fammi sapere come va 🤍"

Non dare mai consigli medici. Se emergono aspetti sanitari, rimanda sempre al pediatra.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROBLEMA CARRELLO / IMPORTO ERRATO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se la mamma dice che al checkout non le esce 37 euro o le compare un importo diverso:
l'unica spiegazione e che ha aggiunto il prodotto piu volte nel carrello.
Rispondi sempre cosi:
"L'unica spiegazione e che hai aggiunto il prodotto piu volte nel carrello.
In alto a destra vedi l'icona di una borsetta — cliccaci sopra, guarda quanti articoli ci sono
e cambia il numero a 1. Poi procedi al pagamento e ti deve uscire 37 euro 🤍"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAGAMENTO CON BONIFICO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se la mamma chiede se puo pagare con bonifico bancario, rispondi cosi:
"Certo, puoi pagare tramite bonifico. Ecco le coordinate:

Intestatario: P&D Digital
IBAN: NL10BUNQ2192297467

Importo: 37 euro
Causale: il tuo nome e cognome

Dimmi quando hai effettuato il bonifico cosi iniziamo 🤍"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GESTIONE RIMBORSI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se la mamma e scontenta o chiede un rimborso:
1. Prima empatizza genuinamente
2. Fai domande per capire se puoi aiutarla in modo diverso
3. Se insiste nel voler il rimborso, rispondi cosi:
   "Capisco, mi dispiace che le cose non siano andate come speravi.
    Ti lascio il link con la nostra politica di rimborso, dove trovi anche l'email per inviare la richiesta formale:
    https://genitorinarmonia.com/policies/refund-policy
    Ti ricordo pero che il rimborso non e applicabile a chi ha gia usufruito in parte o totalmente delle consulenze."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMANDI ADMIN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se ricevi messaggi che iniziano con /inizia, /pausa, /riprendi, /nota, /acquisto, /scrivi:
sono comandi interni. Non rispondere nulla.
"""

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

        # Cancella il timer attivo — Paola ha già risposto
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

def get_consultations_due_for_renewal():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        thirty_days_ago = datetime.now().date() - timedelta(days=30)
        cur.execute("""
            SELECT phone FROM consultations
            WHERE start_date <= %s AND renewal_sent = FALSE AND start_date IS NOT NULL
        """, (thirty_days_ago,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [r["phone"] for r in rows]
    except Exception as e:
        logger.error(f"Errore get_consultations_due_for_renewal: {e}")
        return []

def mark_renewal_sent(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE consultations SET renewal_sent = TRUE WHERE phone = %s", (phone,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore mark_renewal_sent: {e}")

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
            model="whisper-1",
            file=audio_file
        )
        return transcript.text
    except Exception as e:
        logger.error(f"Errore trascrizione audio: {e}")
        return None

# ─── AI ────────────────────────────────────────────────────────────────────────
def get_ai_response(phone, image_url=None):
    history = get_history(phone)
    pending = get_messages_since_last_reply(phone)
    user_message = "\n".join(pending) if pending else "(nessun nuovo messaggio)"

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

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=3000,
            temperature=0.85
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Errore OpenAI: {e}")
        threading.Thread(target=send_telegram, args=[f"⚠️ Errore OpenAI per {phone}: {e}"], daemon=True).start()
        return "Scusa, ho avuto un piccolo problema tecnico. Riprova tra qualche minuto 🙏"

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

def send_renewal_message(phone):
    text = (
        "Ciao, come va? Come sta andando il sonno del tuo bimbo in queste settimane? 🤍\n\n"
        "Volevo dirti che il tuo percorso di 30 giorni e arrivato al termine. "
        "Se vuoi continuare insieme per altri 60 giorni, il rinnovo e sempre a 37 euro. "
        "Ti lascio qui il link: https://genitorinarmonia.com/products/sonno-magico"
    )
    send_whatsapp_message(phone, text)

def send_piano(phone):
    logger.info(f"Generazione piano per {phone}")
    history = get_history(phone)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": (
        "Genera ora il piano personalizzato completo.\n\n"
        "[ISTRUZIONE SISTEMA: Genera il piano personalizzato COMPLETO e DETTAGLIATO adesso, "
        "basandoti su tutto quello che la mamma ha raccontato nel questionario. "
        "Inizia direttamente con il piano senza premesse. "
        "Usa il nome del bambino. Sii specifico sulla sua situazione.]"
    )})
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=4000,
            temperature=0.85
        )
        piano = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Errore generazione piano: {e}")
        piano = "Scusa, ho avuto un problema tecnico nel generare il piano. Riprovo a breve 🙏"
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

    if fase == 0:
        pending = get_messages_since_last_reply(phone)
        combined = "\n".join(pending).lower()

        parole_acquisto = [
            "ho acquistato", "ho comprato", "ho fatto l'ordine", "ho effettuato l'ordine",
            "ho preso il pacchetto", "ho preso il percorso", "ho pagato", "ho fatto il pagamento",
            "ordine completato", "pagamento completato", "l'ho preso",
            "l'ho comprato", "l'ho acquistato", "ho fatto l'acquisto"
        ]
        is_acquisto = any(p in combined for p in parole_acquisto)

        if not is_acquisto and combined:
            try:
                history = get_history(phone)
                history_text = "\n".join([
                    f"{'Mamma' if m['role']=='user' else 'Bot'}: {m['content'][:100]}"
                    for m in history[-5:]
                ])
                check_response = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Sei un classificatore. Rispondi SOLO con SI o NO."},
                        {"role": "user", "content": f"Contesto:\n{history_text}\n\nI messaggi indicano che la persona ha acquistato o completato un ordine? Messaggi: '{combined}'"}
                    ],
                    max_tokens=5,
                    temperature=0
                )
                if check_response.choices[0].message.content.strip().lower().startswith("si"):
                    is_acquisto = True
                    logger.info(f"Acquisto rilevato da GPT per {phone}")
            except Exception as e:
                logger.error(f"Errore check acquisto GPT: {e}")

        if not is_acquisto and image_url:
            try:
                img_response = requests.get(image_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
                img_data = base64.b64encode(img_response.content).decode("utf-8")
                content_type = img_response.headers.get("Content-Type", "image/jpeg")
                check_response = openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "Rispondi SOLO con SI o NO."},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{img_data}"}},
                            {"type": "text", "text": "Questa immagine mostra una conferma d'ordine o ricevuta di pagamento?"}
                        ]}
                    ],
                    max_tokens=5,
                    temperature=0
                )
                if check_response.choices[0].message.content.strip().lower().startswith("si"):
                    is_acquisto = True
            except Exception as e:
                logger.error(f"Errore check immagine: {e}")

        if is_acquisto:
            invia_sequenza_acquisto(phone)
            return

        ai_reply = get_ai_response(phone, image_url=image_url)
        save_message(phone, "assistant", ai_reply)
        send_whatsapp_message(phone, ai_reply)

    elif fase == 1:
        time.sleep(300)
        save_message(phone, "assistant", MSG_QUESTIONARIO_2)
        send_whatsapp_message(phone, MSG_QUESTIONARIO_2)
        set_fase(phone, 2)
        logger.info(f"Questionario parte 2 inviato a {phone}")

    elif fase == 2:
        # Manda messaggio di conferma e aspetta che la mamma dica di aver finito
        save_message(phone, "assistant", MSG_CONFERMA_QUESTIONARIO)
        send_whatsapp_message(phone, MSG_CONFERMA_QUESTIONARIO)
        set_fase(phone, 5)
        logger.info(f"Attesa conferma completamento questionario per {phone}")

    elif fase == 5:
        # Mamma ha risposto — classifica se ha finito il questionario
        pending = get_messages_since_last_reply(phone)
        combined = "\n".join(pending)
        try:
            check_response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Sei un classificatore. Rispondi SOLO con SI o NO."},
                    {"role": "user", "content": f"La persona sta dicendo che ha finito di rispondere alle domande o che e pronta per il piano? Messaggio: '{combined}'"}
                ],
                max_tokens=5,
                temperature=0
            )
            ha_finito = check_response.choices[0].message.content.strip().lower().startswith("si")
        except Exception as e:
            logger.error(f"Errore check conferma: {e}")
            ha_finito = False

        if ha_finito:
            piano_time = datetime.now() + timedelta(hours=1)
            set_fase(phone, 3, piano_scheduled_at=piano_time)
            logger.info(f"Piano schedulato per {phone} alle {piano_time}")
        else:
            risposta = "Ok, prenditi il tempo che ti serve. Dimmi quando hai finito cosi inizio a preparare il tuo piano 🤍"
            save_message(phone, "assistant", risposta)
            send_whatsapp_message(phone, risposta)

    elif fase == 3:
        logger.info(f"Fase 3 per {phone} — bot in attesa del piano")

    elif fase == 4:
        ai_reply = get_ai_response(phone, image_url=image_url)
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
            delay = 600
        elif fase == 5:
            delay = 30   # quasi subito — aspetta la conferma della mamma
        elif fase == 4:
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
    while True:
        try:
            for phone in get_pianos_to_send():
                send_piano(phone)
            for phone in get_consultations_due_for_renewal():
                send_renewal_message(phone)
                mark_renewal_sent(phone)
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
