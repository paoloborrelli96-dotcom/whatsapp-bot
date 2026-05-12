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
CHATWOOT_URL           = os.environ.get("CHATWOOT_URL", "")
CHATWOOT_API_TOKEN     = os.environ.get("CHATWOOT_API_TOKEN", "")
CHATWOOT_INBOX_ID      = os.environ.get("CHATWOOT_INBOX_ID", "")

openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Buffer messaggi (batching)
message_buffers = {}
buffer_timers   = {}
buffer_lock     = threading.Lock()

# Cache inbox Chatwoot per numero
chatwoot_conversations = {}

# ─── FASI ──────────────────────────────────────────────────────────────────────
# 0  = info/primo contatto
# 1  = acquisto confermato, questionario inviato
# 3  = questionario completo, piano schedulato
# 4  = piano inviato, percorso attivo
# 99 = chat in pausa (gestita manualmente)

# ─── TESTI FISSI ───────────────────────────────────────────────────────────────
MSG_BENVENUTO = (
    "Ciao grazie per la fiducia, molto piacere 😇\n\n"
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

MSG_QUESTIONARIO = (
    "Per prepararti un piano su misura ho bisogno di conoscerti meglio. Rispondimi con calma:\n\n"
    "1. Nominativo con cui hai effettuato l'ordine e data di acquisto\n"
    "2. Come ti chiami e quanti anni hai?\n"
    "3. Nome, data di nascita e peso attuale del bambino/a?\n"
    "4. E il primo figlio? Ha fratelli o sorelle?\n"
    "5. Descrivimi la sua giornata tipo: orario sveglia mattina, pisolini (orari e durata), orario nanna serale\n"
    "6. Come si addormenta? (seno, ciuccio, in braccio, da solo...)\n"
    "7. Dove dorme? (culla, lettone, carrozzina...)\n"
    "8. Quante volte si sveglia di notte e come lo riaddormenti?\n"
    "9. Allatti al seno, biberon o entrambi?\n"
    "10. Hai gia provato qualcosa per migliorare il sonno? Com'e andata?\n"
    "11. Lavori? Sei in maternita o rientri presto?\n"
    "12. Il tuo partner ti aiuta di notte?\n"
    "13. Qual e la difficolta principale che vuoi risolvere?\n"
    "14. C'e altro che vuoi dirmi che per te e importante che io sappia?"
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
   - "Inizierei fra una settimana" → "Nessun problema, acquista pure adesso — intanto leggi le guide e fra una settimana mi scrivi e partiamo."
   - Dubbi sul prezzo → spiega il valore: 30 giorni di supporto diretto, piano su misura, contatto quotidiano, adattamento continuo

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
GESTIONE RIMBORSI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se la mamma e scontenta o chiede un rimborso:

1. Prima empatizza genuinamente — capisce che e frustrata e vuole capire cosa non ha funzionato
2. Fai domande per capire se puoi aiutarla in modo diverso o se c'e qualcosa che non ha capito
3. Se insiste nel voler il rimborso, rispondi cosi:
   "Capisco, mi dispiace che le cose non siano andate come speravi.
    Ti lascio il link con la nostra politica di rimborso, dove trovi anche l'email per inviare la richiesta formale:
    https://genitorinarmonia.com/policies/refund-policy
    Ti ricordo pero che il rimborso non e applicabile a chi ha gia usufruito in parte o totalmente delle consulenze."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMANDI ADMIN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se ricevi messaggi che iniziano con /inizia, /pausa, /riprendi, /nota, /acquisto:
sono comandi interni. Non rispondere nulla.
"""

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
            chatwoot_conversation_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT NOW()
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

def get_chatwoot_conversation_id(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT chatwoot_conversation_id FROM consultations WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.error(f"Errore get_chatwoot_conversation_id: {e}")
        return None

def save_chatwoot_conversation_id(phone, conv_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consultations (phone, chatwoot_conversation_id)
            VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE SET chatwoot_conversation_id = EXCLUDED.chatwoot_conversation_id
        """, (phone, conv_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore save_chatwoot_conversation_id: {e}")

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

# ─── CHATWOOT ──────────────────────────────────────────────────────────────────
def chatwoot_headers():
    return {
        "api_access_token": CHATWOOT_API_TOKEN,
        "Content-Type": "application/json"
    }

def get_or_create_chatwoot_contact(phone):
    """Trova o crea un contatto su Chatwoot."""
    # Normalizza il numero — rimuovi + iniziale se presente
    phone_clean = phone.lstrip("+")
    phone_e164 = f"+{phone_clean}"

    try:
        resp = requests.get(
            f"{CHATWOOT_URL}/api/v1/accounts/1/contacts/search",
            headers=chatwoot_headers(),
            params={"q": phone_e164},
            timeout=10
        )
        logger.info(f"Chatwoot contact search: {resp.status_code} — {resp.text[:300]}")
        data = resp.json()
        if data.get("payload") and len(data["payload"]) > 0:
            return data["payload"][0]["id"]

        resp = requests.post(
            f"{CHATWOOT_URL}/api/v1/accounts/1/contacts",
            headers=chatwoot_headers(),
            json={"phone_number": phone_e164, "name": phone_e164},
            timeout=10
        )
        logger.info(f"Chatwoot contact create: {resp.status_code} — {resp.text[:300]}")
        return resp.json().get("id")
    except Exception as e:
        logger.error(f"Errore get_or_create_chatwoot_contact: {e}")
        return None

def get_or_create_chatwoot_conversation(phone):
    """Trova o crea una conversazione su Chatwoot."""
    # Prima controlla nel DB locale
    conv_id = get_chatwoot_conversation_id(phone)
    if conv_id:
        return conv_id

    try:
        inbox_id = int(CHATWOOT_INBOX_ID) if CHATWOOT_INBOX_ID else 1
        phone_clean = phone.lstrip("+")
        phone_e164 = f"+{phone_clean}"
        contact_id = get_or_create_chatwoot_contact(phone)
        if not contact_id:
            logger.error(f"Chatwoot: nessun contact_id per {phone}")
            return None

        # Cerca conversazione esistente per questo contatto
        resp = requests.get(
            f"{CHATWOOT_URL}/api/v1/accounts/1/contacts/{contact_id}/conversations",
            headers=chatwoot_headers(),
            timeout=10
        )
        if resp.status_code == 200:
            convs = resp.json().get("payload", [])
            # Filtra per inbox corretta
            for conv in convs:
                if conv.get("inbox_id") == inbox_id:
                    existing_id = conv.get("id")
                    save_chatwoot_conversation_id(phone, existing_id)
                    logger.info(f"Chatwoot: conversazione esistente trovata {existing_id}")
                    return existing_id

        # Nessuna conversazione esistente — creane una nuova
        resp = requests.post(
            f"{CHATWOOT_URL}/api/v1/accounts/1/conversations",
            headers=chatwoot_headers(),
            json={
                "inbox_id": inbox_id,
                "contact_id": contact_id,
                "source_id": f"whatsapp:{phone_e164}",
            },
            timeout=10
        )
        logger.info(f"Chatwoot conversation create: {resp.status_code} — {resp.text[:300]}")
        conv_id = resp.json().get("id")
        if conv_id:
            save_chatwoot_conversation_id(phone, conv_id)
        return conv_id
    except Exception as e:
        logger.error(f"Errore get_or_create_chatwoot_conversation: {e}")
        return None

def send_to_chatwoot(phone, message, is_outgoing=False):
    """Invia un messaggio a Chatwoot per visualizzarlo nel pannello."""
    if not CHATWOOT_URL or not CHATWOOT_API_TOKEN:
        return
    try:
        conv_id = get_or_create_chatwoot_conversation(phone)
        if not conv_id:
            return
        # Messaggi in arrivo come note private, in uscita come outgoing
        payload = {
            "content": message,
            "message_type": "outgoing" if is_outgoing else "incoming",
            "private": not is_outgoing  # in arrivo = nota privata visibile solo a te
        }
        resp = requests.post(
            f"{CHATWOOT_URL}/api/v1/accounts/1/conversations/{conv_id}/messages",
            headers=chatwoot_headers(),
            json=payload,
            timeout=10
        )
        logger.info(f"Chatwoot message sent: {resp.status_code}")
    except Exception as e:
        logger.error(f"Errore send_to_chatwoot: {e}")

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
def get_ai_response(phone, user_message, image_url=None, extra_instruction=None):
    history = get_history(phone)

    if image_url:
        try:
            img_response = requests.get(
                image_url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                timeout=30
            )
            img_data = base64.b64encode(img_response.content).decode("utf-8")
            content_type = img_response.headers.get("Content-Type", "image/jpeg")
            user_content = [
                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{img_data}"}},
                {"type": "text", "text": user_message or "Guarda questa immagine"}
            ]
        except Exception as e:
            logger.error(f"Errore download immagine: {e}")
            user_content = user_message or ""
    else:
        user_content = user_message

    if extra_instruction:
        user_content = str(user_content) + f"\n\n[ISTRUZIONE SISTEMA: {extra_instruction}]"

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=3000,
            temperature=0.85
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Errore OpenAI: {e}")
        return "Scusa, ho avuto un piccolo problema tecnico. Riprova tra qualche minuto 🙏"

# ─── INVIO ─────────────────────────────────────────────────────────────────────
def send_whatsapp_message(phone, text):
    chunks = []
    while len(text) > 1500:
        split_point = text.rfind('\n', 0, 1500)
        if split_point == -1:
            split_point = 1500
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
            # Invia anche a Chatwoot come messaggio in uscita
            threading.Thread(target=send_to_chatwoot, args=[phone, chunk, True], daemon=True).start()
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
    logger.info(f"Messaggio rinnovo inviato a {phone}")

def send_piano(phone):
    logger.info(f"Generazione piano per {phone}")
    piano = get_ai_response(
        phone,
        "Genera ora il piano personalizzato completo.",
        extra_instruction=(
            "Genera il piano personalizzato COMPLETO e DETTAGLIATO adesso, "
            "basandoti su tutto quello che la mamma ha raccontato nel questionario. "
            "Inizia direttamente con il piano senza premesse. "
            "Usa il nome del bambino. Sii specifico sulla sua situazione."
        )
    )
    save_message(phone, "assistant", piano)
    send_whatsapp_message(phone, piano)
    set_fase(phone, 4)
    set_start_date(phone, datetime.now().date())
    logger.info(f"Piano inviato a {phone}")

# ─── SEQUENZA ACQUISTO ─────────────────────────────────────────────────────────
def invia_sequenza_acquisto(phone):
    """Invia benvenuto + regole + questionario in sequenza automatica."""
    logger.info(f"Avvio sequenza acquisto per {phone}")
    save_message(phone, "assistant", MSG_BENVENUTO)
    send_whatsapp_message(phone, MSG_BENVENUTO)
    time.sleep(3)

    save_message(phone, "assistant", MSG_REGOLE)
    send_whatsapp_message(phone, MSG_REGOLE)
    time.sleep(3)

    save_message(phone, "assistant", MSG_QUESTIONARIO)
    send_whatsapp_message(phone, MSG_QUESTIONARIO)

    set_fase(phone, 1)
    logger.info(f"Sequenza acquisto completata per {phone}")

# ─── BATCHING ──────────────────────────────────────────────────────────────────
def process_batch(phone):
    with buffer_lock:
        batch = message_buffers.pop(phone, [])
        buffer_timers.pop(phone, None)

    if not batch:
        return

    combined_text = "\n".join([b["text"] for b in batch if b.get("text")])
    image_url = next((b["image_url"] for b in batch if b.get("image_url")), None)

    if not combined_text and not image_url:
        return

    save_message(phone, "user", combined_text or "[immagine]")

    fase = get_fase(phone)
    logger.info(f"Processing batch per {phone} — fase {fase}")

    # FASE 0: non ha ancora acquistato
    if fase == 0:
        testo_lower = (combined_text or "").lower()
        parole_acquisto = [
            "ho acquistato", "ho comprato", "ho fatto l'ordine", "ho effettuato l'ordine",
            "ho preso il pacchetto", "ho preso il percorso", "ho pagato", "ho fatto il pagamento",
            "ordine completato", "pagamento completato", "ho preso", "l'ho preso",
            "l'ho comprato", "l'ho acquistato", "ho fatto l'acquisto"
        ]
        is_acquisto = any(p in testo_lower for p in parole_acquisto)

        # Se non rilevato da testo ma c'e un'immagine, chiedi a gpt-4o
        if not is_acquisto and image_url:
            try:
                img_response = requests.get(
                    image_url,
                    auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                    timeout=30
                )
                img_data = base64.b64encode(img_response.content).decode("utf-8")
                content_type = img_response.headers.get("Content-Type", "image/jpeg")
                check_messages = [
                    {"role": "system", "content": "Sei un analizzatore di immagini. Rispondi SOLO con SI o NO."},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{img_data}"}},
                        {"type": "text", "text": "Questa immagine mostra una conferma d'ordine, ricevuta di pagamento o schermata di acquisto completato? Rispondi SOLO con SI o NO."}
                    ]}
                ]
                check_response = openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=check_messages,
                    max_tokens=5,
                    temperature=0
                )
                check = check_response.choices[0].message.content.strip().lower()
                if check.startswith("si") or check.startswith("si"):
                    is_acquisto = True
                    logger.info(f"Acquisto rilevato da immagine per {phone}")
            except Exception as e:
                logger.error(f"Errore check immagine acquisto: {e}")

        if is_acquisto:
            logger.info(f"Acquisto rilevato per {phone}")
            time.sleep(2)
            invia_sequenza_acquisto(phone)
            return

        # Risposta informativa — 5 minuti
        time.sleep(300)
        ai_reply = get_ai_response(phone, combined_text, image_url=image_url)
        save_message(phone, "assistant", ai_reply)
        send_whatsapp_message(phone, ai_reply)

    # FASE 1: ha risposto al questionario, schedula piano
    elif fase == 1:
        piano_time = datetime.now() + timedelta(hours=1)
        set_fase(phone, 3, piano_scheduled_at=piano_time)

    # FASE 3: piano schedulato, risponde normalmente in attesa
    elif fase == 3:
        time.sleep(2)
        ai_reply = get_ai_response(phone, combined_text, image_url=image_url)
        save_message(phone, "assistant", ai_reply)
        send_whatsapp_message(phone, ai_reply)

    # FASE 4: percorso attivo — 30-40 minuti random
    elif fase == 4:
        time.sleep(random.randint(1800, 2400))
        ai_reply = get_ai_response(phone, combined_text, image_url=image_url)
        save_message(phone, "assistant", ai_reply)
        send_whatsapp_message(phone, ai_reply)

def schedule_batch(phone):
    time.sleep(30)
    process_batch(phone)

# ─── WEBHOOK ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    phone      = request.form.get("From", "").replace("whatsapp:", "")
    body       = request.form.get("Body", "").strip()
    num_media  = int(request.form.get("NumMedia", 0))
    media_type = request.form.get("MediaContentType0", "")
    media_url  = request.form.get("MediaUrl0", "")

    logger.info(f"Messaggio da {phone}: '{body}' | media: {num_media}")

    # ── Comandi admin ──────────────────────────────────────────────────────────
    if body.startswith("/inizia"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            set_start_date(target, datetime.now().date())
            set_fase(target, 4)
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
            logger.info(f"Nota salvata per {target}: {nota}")
        return Response("OK", status=200)

    # ── Chat in pausa ─────────────────────────────────────────────────────────
    if get_fase(phone) == 99:
        logger.info(f"Chat {phone} in pausa — ignorato")
        # Invia comunque a Chatwoot per visibilita
        threading.Thread(target=send_to_chatwoot, args=[phone, body, False], daemon=True).start()
        return Response("OK", status=200)

    # ── Rilevamento acquisto IMMEDIATO con GPT (bypassa batching) ─────────────
    if get_fase(phone) == 0 and body:
        try:
            check_response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "Sei un classificatore. Rispondi SOLO con SI o NO."},
                    {"role": "user", "content": f"La persona sta comunicando in qualsiasi modo di aver acquistato, pagato, completato un ordine o effettuato una transazione? Messaggio: '{body}'"}
                ],
                max_tokens=5,
                temperature=0
            )
            risposta = check_response.choices[0].message.content.strip().lower()
            if risposta.startswith("si") or risposta.startswith("si"):
                save_message(phone, "user", body)
                threading.Thread(target=send_to_chatwoot, args=[phone, body, False], daemon=True).start()
                with buffer_lock:
                    if phone in buffer_timers:
                        buffer_timers[phone].cancel()
                        buffer_timers.pop(phone, None)
                    message_buffers.pop(phone, None)
                threading.Thread(target=invia_sequenza_acquisto, args=[phone], daemon=True).start()
                return Response("OK", status=200)
        except Exception as e:
            logger.error(f"Errore check acquisto GPT: {e}")

    # ── Gestione media ────────────────────────────────────────────────────────
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

    # Invia messaggio in arrivo a Chatwoot
    if text_to_process:
        threading.Thread(target=send_to_chatwoot, args=[phone, text_to_process, False], daemon=True).start()

    # ── Batching ──────────────────────────────────────────────────────────────
    with buffer_lock:
        if phone not in message_buffers:
            message_buffers[phone] = []
        message_buffers[phone].append({
            "text": text_to_process,
            "image_url": image_url_to_process
        })
        if phone in buffer_timers:
            buffer_timers[phone].cancel()
        timer = threading.Timer(30, process_batch, args=[phone])
        buffer_timers[phone] = timer
        timer.start()

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

# ─── AVVIO ─────────────────────────────────────────────────────────────────────
def startup():
    init_db()
    threading.Thread(target=background_job, daemon=True).start()
    logger.info("Bot avviato")

if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    startup()
