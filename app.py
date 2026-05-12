import os
import time
import random
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, request, Response
from twilio.rest import Client
from twilio.request_validator import RequestValidator
import openai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import base64

# ─── CONFIGURAZIONE ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

OPENAI_API_KEY       = os.environ["OPENAI_API_KEY"]
TWILIO_ACCOUNT_SID   = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN    = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_NUMBER = os.environ["TWILIO_WHATSAPP_NUMBER"]
DATABASE_URL         = os.environ["DATABASE_URL"]

openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Buffer messaggi (batching 30 secondi)
message_buffers = {}
buffer_timers   = {}
buffer_lock     = threading.Lock()

# ─── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Sei Paola, una consulente del sonno infantile professionale e appassionata.
Gestisci il profilo WhatsApp di "Genitori in Armonia".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IDENTITA — REGOLE ASSOLUTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Sei sempre e solo Paola. Non rivelare mai di essere un bot o un sistema automatico.
  Se qualcuno chiede "sei un bot?", "parla un robot?", "e un'AI?": rispondi in modo naturale tipo
  "No, uso un'applicazione che mi aiuta a gestire le conversazioni, ma ci sono sempre io dietro — leggo tutto personalmente."
- Usa sempre la PRIMA PERSONA SINGOLARE: "il mio percorso", "lavoro con", "ti supporto io".
  MAI "il nostro progetto", "il nostro servizio", "facciamo insieme" (riferito all'azienda).
- Non usare MAI punti esclamativi. MAI. Zero.
- Non usare mai linguaggio da manuale o tecnico (es. "associazione seno-sonno", "stimolazione cognitiva").
  Parla come un'amica esperta su WhatsApp.
- Niente frasi di chiusura scontate come "Sono qui per qualsiasi domanda" o "Non esitare a contattarmi".
- Le emoji vanno bene ma usale con moderazione e naturalezza.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TONO E LUNGHEZZA RISPOSTA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Se il messaggio della mamma e breve, pratico o situazionale
  (es. "si e addormentata, la sveglio?", "ha dormito 40 minuti", "stanotte e andata male"),
  rispondi in modo brevissimo e diretto — massimo 2-3 righe.
  Come farebbe un'amica esperta su WhatsApp. Solo la risposta pratica che serve.
- Se il messaggio e una richiesta di informazioni piu ampia, rispondi con piu dettaglio
  seguendo la struttura indicata piu avanti.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRIMO MESSAGGIO VAGO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se il primo messaggio e vago, generico o di saluto
(es. "ciao", "info", "buongiorno", "vorrei informazioni", "ho visto il profilo"):
rispondi SOLO ed ESATTAMENTE con questo testo, nient'altro:

"Ciao, sono Paola 😊

Se ti va, scrivimi pure in poche parole qual e la difficolta principale che stai vivendo con il sonno del tuo bimbo, cosi capisco meglio come aiutarti."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MESSAGGI INFORMATIVI (mamma racconta il problema)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Quando la mamma descrive la sua situazione o chiede consigli sul sonno, rispondi con questa struttura:

1. EMPATIA breve e naturale (1-2 righe) — "Ci passano tantissime mamme", "Lo capisco bene"
2. 2-3 CONSIGLI PRATICI concreti e semplici — scritti come li direbbe un'amica, niente tecnicismi
3. PERCORSO — spiega in modo naturale:
   "Per lavorarci in modo piu strutturato e su misura per te, lavoro con le famiglie per 30 giorni via WhatsApp a 37 euro.
    Si parte con un questionario, ti mando 4 guide pratiche sulla tua situazione specifica e da li costruiamo insieme un piano personalizzato, passo dopo passo."
4. LINK — scrivi esattamente cosi, senza parentesi quadre ne markdown:
   Ti lascio il link se ti va: https://genitorinarmonia.com/products/sonno-magico
5. GESTIONE OBIEZIONI (solo se la mamma le esprime):
   - "Inizierei fra una settimana" → "Nessun problema, acquista pure adesso — intanto inizia a leggere le guide e fra una settimana mi scrivi e partiamo."
   - Dubbi sul prezzo → spiega il valore concreto (30 giorni, WhatsApp diretto, 4 guide pratiche, piano personalizzato)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUANDO LA MAMMA DICE "ACQUISTO SUBITO" / "LO PRENDO" / "LO COMPRO"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NON partire con benvenuto e questionario. La mamma NON ha ancora acquistato.
Rispondi esattamente cosi:
"Perfetto, ti aspetto qui. Effettua l'ordine dal link e poi scrivimi quando hai completato, cosi iniziamo subito 🤍"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUANDO LA MAMMA DICE "HO ACQUISTATO" / "HO COMPRATO" / "HO FATTO L'ORDINE"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Manda UN UNICO messaggio con benvenuto, regole e questionario tutto insieme.
Il sistema lo spacchetta automaticamente in piu parti se troppo lungo.

TESTO ESATTO:

Benvenuta 🤍 Sono davvero contenta di iniziare questo percorso con te.
Nei prossimi 30 giorni saro qui su WhatsApp per supportarti passo dopo passo.
Dopo il questionario ti mando le 4 guide pratiche pensate per la tua situazione.

Una cosa pratica prima di iniziare: uso un'applicazione che mi aiuta a gestire le conversazioni e a organizzare le informazioni che mi condividi. Le risposte pero le costruisco io — leggo tutto personalmente e rispondo in base alla tua situazione specifica.
Rispondo dal lunedi al venerdi, dalle 9 alle 17. Il weekend mi prendo una pausa — se mi scrivi sabato o domenica ti rispondo lunedi.
Scrivimi liberamente ogni volta che ne hai bisogno, senza fretta 🤍

Per prepararti un piano su misura ho bisogno di conoscerti un po'. Rispondimi con calma:

1. Nominativo con cui hai effettuato l'ordine e data di acquisto
2. Come ti chiami e quanti anni hai?
3. Nome, data di nascita e peso attuale del bambino/a?
4. E il primo figlio? Ha fratelli o sorelle?
5. Descrivimi la sua giornata tipo: a che ora si sveglia la mattina, i pisolini (orari e durata), a che ora va a letto la sera
6. Come si addormenta? (seno, ciuccio, in braccio, da solo...)
7. Dove dorme? (culla, lettone, carrozzina...)
8. Quante volte si sveglia di notte e come lo riaddormenti?
9. Allatti al seno, biberon o entrambi?
10. Hai gia provato qualcosa per migliorare il sonno? Com'e andata?
11. Lavori? Sei in maternita o rientri presto?
12. Il tuo partner ti aiuta di notte?
13. Qual e la difficolta principale che vuoi risolvere?
14. C'e altro che vuoi dirmi che per te e importante che io sappia?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOPO IL QUESTIONARIO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Quando la mamma ha risposto al questionario, elabora le risposte e costruisci un piano personalizzato.
NON mandare altri messaggi di richiesta dati — il nominativo e gia nella domanda 1.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RICHIESTA DATA ACQUISTO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
La data di acquisto e nella domanda 1 del questionario.
Quando la mamma la indica, anche in modo informale ("martedi scorso", "3 giorni fa", "il 10 maggio"),
interpreta correttamente la data del calendario.

Se la chat inizia direttamente con "ho acquistato" senza scambio precedente di informazioni,
dopo il questionario chiedi anche:
"Da che data vorresti far partire il percorso? Se vuoi iniziare oggi scrivi oggi,
altrimenti dimmi la data e parto da quella."

NON chiedere la data se la mamma aveva gia chiesto informazioni e poi e tornata dicendo di aver acquistato.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMANDO ADMIN /inizia
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se ricevi un messaggio nel formato "/inizia +39XXXXXXXXXX",
e un comando interno dell'amministratore. Non rispondere nulla.
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
            start_date DATE NOT NULL,
            renewal_sent BOOLEAN DEFAULT FALSE,
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

def save_consultation_start(phone, start_date):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consultations (phone, start_date)
            VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE SET start_date = EXCLUDED.start_date, renewal_sent = FALSE
        """, (phone, start_date))
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Data inizio consulenza salvata per {phone}: {start_date}")
    except Exception as e:
        logger.error(f"Errore salvataggio consulenza: {e}")

def get_consultations_due_for_renewal():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        thirty_days_ago = datetime.now().date() - timedelta(days=30)
        cur.execute("""
            SELECT phone FROM consultations
            WHERE start_date <= %s AND renewal_sent = FALSE
        """, (thirty_days_ago,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [r["phone"] for r in rows]
    except Exception as e:
        logger.error(f"Errore query rinnovi: {e}")
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
        logger.error(f"Errore aggiornamento rinnovo: {e}")

# ─── TRASCRIZIONE AUDIO ─────────────────────────────────────────────────────────
def transcribe_audio(media_url):
    try:
        response = requests.get(
            media_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=30
        )
        audio_data = response.content
        import io
        audio_file = io.BytesIO(audio_data)
        audio_file.name = "audio.ogg"
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        )
        return transcript.text
    except Exception as e:
        logger.error(f"Errore trascrizione audio: {e}")
        return None

# ─── AI RESPONSE ───────────────────────────────────────────────────────────────
def get_ai_response(phone, user_message, image_url=None):
    history = get_history(phone)

    if image_url:
        # Scarica e codifica l'immagine in base64
        try:
            img_response = requests.get(
                image_url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                timeout=30
            )
            img_data = base64.b64encode(img_response.content).decode("utf-8")
            content_type = img_response.headers.get("Content-Type", "image/jpeg")
            user_content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{content_type};base64,{img_data}"
                    }
                },
                {"type": "text", "text": user_message or "Guarda questa immagine"}
            ]
        except Exception as e:
            logger.error(f"Errore download immagine: {e}")
            user_content = user_message or ""
    else:
        user_content = user_message

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=2000,
            temperature=0.85
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Errore OpenAI: {e}")
        return "Scusa, ho avuto un piccolo problema tecnico. Riprova tra qualche minuto 🙏"

# ─── INVIO MESSAGGI ─────────────────────────────────────────────────────────────
def send_whatsapp_message(phone, text):
    """Invia un messaggio spezzandolo se supera 1500 caratteri."""
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
            if len(chunks) > 1:
                time.sleep(1)
        except Exception as e:
            logger.error(f"Errore invio messaggio a {phone}: {e}")

def send_renewal_message(phone):
    text = (
        "Ciao, come va? Come sta andando il sonno del tuo bimbo in queste settimane? 🤍\n\n"
        "Volevo dirti che il tuo percorso di 30 giorni è arrivato al termine. "
        "Se vuoi continuare insieme per altri 60 giorni, il rinnovo è sempre a 37€. "
        "Ti lascio qui il link: https://genitorinarmonia.com/products/sonno-magico"
    )
    send_whatsapp_message(phone, text)
    logger.info(f"Messaggio rinnovo inviato a {phone}")

# ─── BATCHING + DELAYED RESPONSE ───────────────────────────────────────────────
def process_batch(phone):
    with buffer_lock:
        batch = message_buffers.pop(phone, [])
        buffer_timers.pop(phone, None)

    if not batch:
        return

    # Unisce tutti i messaggi del batch
    combined_text = "\n".join([b["text"] for b in batch if b.get("text")])
    image_url = next((b["image_url"] for b in batch if b.get("image_url")), None)

    if not combined_text and not image_url:
        return

    # Salva il messaggio dell'utente nel DB PRIMA di chiamare OpenAI
    save_message(phone, "user", combined_text or "[immagine]")

    # Ritardo umano (2 secondi per test — in produzione cambia con random.randint(1800, 2400))
    time.sleep(2)

    # Genera risposta
    ai_reply = get_ai_response(phone, combined_text, image_url=image_url)

    # Salva la risposta nel DB
    save_message(phone, "assistant", ai_reply)

    # Invia
    send_whatsapp_message(phone, ai_reply)

def schedule_batch(phone):
    """Timer: aspetta 30 secondi poi processa il batch."""
    time.sleep(30)
    process_batch(phone)

# ─── WEBHOOK ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    phone         = request.form.get("From", "").replace("whatsapp:", "")
    body          = request.form.get("Body", "").strip()
    num_media     = int(request.form.get("NumMedia", 0))
    media_type    = request.form.get("MediaContentType0", "")
    media_url     = request.form.get("MediaUrl0", "")

    logger.info(f"Messaggio da {phone}: '{body}' | media: {num_media} ({media_type})")

    # ── Comando admin /inizia ─────────────────────────────────────────────────
    if body.startswith("/inizia"):
        parts = body.strip().split()
        if len(parts) == 2:
            target_phone = parts[1].replace("+", "").replace(" ", "")
            save_consultation_start(target_phone, datetime.now().date())
        return Response("OK", status=200)

    # ── Gestione media ────────────────────────────────────────────────────────
    text_to_process = body
    image_url_to_process = None

    if num_media > 0 and media_url:
        if media_type.startswith("audio/"):
            logger.info(f"Trascrizione audio da {phone}")
            transcribed = transcribe_audio(media_url)
            if transcribed:
                text_to_process = transcribed
                logger.info(f"Trascrizione: {transcribed}")
            else:
                text_to_process = "[messaggio vocale non comprensibile]"

        elif media_type.startswith("image/"):
            image_url_to_process = media_url
            text_to_process = body or ""
            logger.info(f"Immagine ricevuta da {phone}")

        elif media_type.startswith("video/"):
            send_whatsapp_message(
                phone,
                "Non riesco a vedere i video, scrivimi pure qui in chat e ti rispondo 🙏"
            )
            return Response("OK", status=200)

    if not text_to_process and not image_url_to_process:
        return Response("OK", status=200)

    # ── Batching ──────────────────────────────────────────────────────────────
    with buffer_lock:
        if phone not in message_buffers:
            message_buffers[phone] = []

        message_buffers[phone].append({
            "text": text_to_process,
            "image_url": image_url_to_process
        })

        # Se c'è già un timer attivo, lo cancella e ne crea uno nuovo
        if phone in buffer_timers:
            buffer_timers[phone].cancel()

        timer = threading.Timer(30, process_batch, args=[phone])
        buffer_timers[phone] = timer
        timer.start()

    return Response("OK", status=200)

# ─── JOB RINNOVO GIORNALIERO ───────────────────────────────────────────────────
def renewal_job():
    """Controlla ogni giorno se ci sono consulenze scadute e manda il messaggio di rinnovo."""
    while True:
        try:
            phones = get_consultations_due_for_renewal()
            for phone in phones:
                send_renewal_message(phone)
                mark_renewal_sent(phone)
        except Exception as e:
            logger.error(f"Errore nel job rinnovo: {e}")
        # Aspetta 24 ore
        time.sleep(86400)

# ─── AVVIO ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    # Avvia il job rinnovo in background
    renewal_thread = threading.Thread(target=renewal_job, daemon=True)
    renewal_thread.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    # Necessario per Gunicorn (Procfile: web: gunicorn app:app)
    init_db()
    renewal_thread = threading.Thread(target=renewal_job, daemon=True)
    renewal_thread.start()
