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

# Marker interno: serve per segnare che il bot ha letto un messaggio di chiusura/cortesia
# senza inviare nulla alla mamma. Viene escluso dallo storico mandato a OpenAI.
SILENT_NO_REPLY_MARKER = "[SILENT_NO_REPLY]"
NO_REPLY = "__NO_REPLY__"

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
    "3. Nome del bambino/a, eta attuale precisa in mesi o anni, data di nascita e peso attuale\n"
    "4. E il primo figlio? Ha fratelli o sorelle?\n"
    "5. Descrivimi la sua giornata tipo: orario sveglia mattina, pisolini con orari e durata, orario nanna serale\n"
    "6. Come si addormenta di solito? Seno, biberon, ciuccio, braccio, dondolio, lettone, presenza, da solo o altro?\n"
    "7. Dove dorme all'inizio della notte e dove finisce la notte? Lettino, culla, next to me, lettone, braccio o altro?\n\n"
    "Rispondimi a queste prime domande con calma, poi ti mando le altre 🤍"
)

MSG_QUESTIONARIO_2 = (
    "Rispondi anche a queste, grazie:\n\n"
    "8. Quante volte si sveglia di notte circa e in che orari di solito?\n"
    "9. Quando si sveglia cosa succede esattamente? Piange subito, si gira e rigira, cerca seno/biberon/ciuccio, vuole essere preso in braccio, si alza, chiama o resta tranquillo ma non si riaddormenta?\n"
    "10. Come lo riaddormenti durante i risvegli e quanto tempo ci mette di solito?\n"
    "11. Allatti al seno, biberon o entrambi? Se prende latte di notte, quanto e quante volte circa?\n"
    "12. Hai gia provato qualcosa per migliorare il sonno? Com'e andata?\n"
    "13. Il tuo partner ti aiuta di notte o nell'addormentamento? Se si, come reagisce il bambino con lui/lei?\n"
    "14. Lavori, sei in maternita o rientri presto? Ci sono nido, vacanze o cambiamenti in arrivo?\n"
    "15. Qual e l'obiettivo principale che vuoi raggiungere? Meno risvegli, togliere seno/biberon, addormentamento piu autonomo, riuscire ad appoggiarlo, spostare gli orari o altro?\n"
    "16. C'e qualcosa che non vuoi fare o che ti mette particolarmente in difficolta? Per esempio lasciarlo piangere, togliere il seno subito, far intervenire il papa, alzarti spesso o tenerlo in braccio.\n"
    "17. C'e qualche aspetto di salute che devo sapere? Reflusso, allergie, crescita, febbre recente, dentini, farmaci, indicazioni del pediatra o altro?\n"
    "18. C'e altro che per te e importante che io sappia?"
)

MSG_CONFERMA_QUESTIONARIO = (
    "Hai risposto a tutto? Dimmi quando hai finito cosi inizio subito a prepararti il piano 🤍"
)

MSG_CHECKUP = """Ok, allora rivediamo un attimo la situazione cosi capisco bene cosa sta succedendo adesso.

1. Da quanti giorni state seguendo il piano?
2. Cosa e migliorato, anche poco?
3. Cosa invece e rimasto uguale o peggiorato?
4. Com'e l'addormentamento serale in questi giorni?
5. Quanti risvegli sta facendo circa e in che orari?
6. Nei risvegli cosa cerca per riaddormentarsi?
7. I pisolini come stanno andando?
8. C'e stato qualcosa di diverso: dentini, malattia, nido, viaggi, giornate piu stimolanti o cambiamenti?
9. Qual e la cosa che ti pesa di piu in questo momento?

Rispondimi con calma, poi rivedo il piano in base a quello che mi scrivi 🤍"""

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
- difficolta_persistente_post_piano
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
In fase 0, se il messaggio e solo vago o informativo tipo "ciao", "info", "vorrei informazioni", "quanto costa", "come funziona" e NON contiene una descrizione concreta del problema del bambino, usa richiesta_info_percorso oppure saluto_vago.
In fase 0, se il messaggio contiene gia una difficolta concreta del sonno, ad esempio risvegli, seno/latte, ciuccio, braccio, lettone, pisolini, addormentamento, pianto, orari, notti difficili, stanchezza della mamma, usa descrizione_problema_sonno anche se chiede anche informazioni sul percorso.
Se prima la persona ha ricevuto la domanda "qual e la difficolta principale" e ora risponde raccontando il problema, usa descrizione_problema_sonno.
Non dare per acquisto completato frasi come "lo compro", "vorrei acquistare", "procedo". Acquisto completato solo se dice che ha gia pagato, acquistato, scaricato o letto la guida/PDF/materiale.
Non classificare come richiesta_bonifico solo perché compare la parola bonifico. È richiesta_bonifico solo se chiede IBAN, coordinate, o se può pagare con bonifico.
Se dice che ha già fatto il bonifico, usa bonifico_effettuato.
Non classificare come richiesta_rimborso solo perché compare la parola rimborso. È richiesta_rimborso solo se vuole indietro i soldi o chiede la procedura.
Non classificare come problema_checkout_importo solo perché compaiono 37 o 67. È problema_checkout_importo solo se parla di carrello, checkout, importo sbagliato, prezzo che non torna, prodotto aggiunto più volte.
Non classificare come acquisto_completato se scrive "lo compro", "lo prendo", "acquisto subito". Quello è intenzione_acquisto_non_completato.
È acquisto_completato solo se dice che ha già pagato, completato ordine, fatto acquisto, mostra ricevuta/conferma, oppure dice di aver scaricato/letto/ricevuto la guida, il PDF, il materiale o il percorso.
Se la mamma è già in percorso attivo e chiede "che faccio ora", "lo sveglio", "la attacco", "come mi muovo adesso", usa richiesta_pratica_immediata.
Se la mamma è in percorso attivo e dice che dopo alcuni giorni non vede miglioramenti, non funziona, è peggiorato, è molto stanca o non ce la fa più, usa difficolta_persistente_post_piano. Non mettere needs_human=true solo per questo: safe_auto_reply=true e needs_human=false, salvo rabbia forte o richiesta rimborso.
Se cita febbre, tosse, raffreddore, dentini, malattia recente o malessere passato ma la domanda principale riguarda il sonno, il latte, i risvegli o il rientro alla routine, NON bloccare la risposta: usa domanda_percorso_attivo o aggiornamento_percorso_attivo, metti entities.medical_topic=true, safe_auto_reply=true e needs_human=false.
In questi casi il generatore dovrà rispondere sul sonno con prudenza, senza diagnosi e senza consigli medici.
Usa dubbio_medico_delicato con needs_human=true SOLO se ci sono segnali sanitari importanti o richieste mediche dirette: difficoltà respiratoria, febbre alta ancora in corso o peggioramento, vomito persistente, disidratazione, dolore forte, crescita/peso preoccupante, farmaci/dosaggi, richiesta di diagnosi, indicazioni del pediatra da interpretare, pronto soccorso o situazione che sembra urgente.
Se esprime rabbia forte, minaccia recensioni, parla di avvocato, truffa, denuncia, o chiede chiaramente una persona vera, usa necessita_revisione_umano e needs_human true.
Se il messaggio è solo una chiusura o cortesia breve senza domanda reale, come "ok", "va bene", "va bene grazie", "perfetto grazie", "grazie mille", "ci provo", "d'accordo", "ti aggiorno", usa messaggio_cortesia, message_type conferma, safe_auto_reply true e needs_human false.
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

In fase 0 ci sono tre casi diversi.
Se la persona scrive solo ciao, info, vorrei informazioni, quanto costa o come funziona senza raccontare il problema, non vendere subito: chiedi prima in poche parole qual e la difficolta principale che vive con il sonno del bambino.
Se invece la persona non ha ancora acquistato ma descrive gia un problema concreto del sonno, fai una prima analisi breve e personalizzata: falla sentire capita, spiega la dinamica in modo semplice, non dare un piano gratuito e non dare una sequenza completa di azioni. Poi presenta il percorso e il link se non e gia stato inviato.
Se dichiara di aver gia acquistato, il codice avvia la sequenza acquisto e non devi fare analisi commerciale.

Se la persona è in percorso attivo, dai indicazioni concrete ma non troppe insieme.
Usa il profilo del bambino e lo storico recente.
Se c'è un miglioramento, valorizzalo in modo specifico.
Se c'è un passo indietro, normalizzalo senza far sentire la mamma in colpa.

Se il messaggio è una micro-conferma o un grazie, di norma non serve rispondere. Se proprio è necessaria una risposta, deve essere minima.
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

CONTINUA_PROMPT = """
Rispondi all'ultimo messaggio della mamma come Paola.
Questo comando e stato autorizzato da Paola dopo un alert: puoi rispondere comunque, ma con cautela.

Non generare un nuovo piano.
Non proporre un checkup.
Non fare troppe modifiche.
Non scrivere una risposta solo motivazionale.

Devi:
- riconoscere la stanchezza o la difficolta se presente;
- rispondere alla domanda concreta che la mamma ha fatto;
- collegarti al piano gia dato e allo storico;
- dare massimo 1 o 2 indicazioni pratiche per oggi o per stanotte;
- se c'e un tema sanitario leggero, non dare consigli medici e rimanda al pediatra per la parte sanitaria;
- se c'e una lamentela o rimborso, rispondi con molta cautela, senza promesse e senza irrigidirti;
- se c'e sospetto AI, rispondi in modo trasparente come previsto dal prompt base.

La risposta deve sembrare un messaggio WhatsApp umano, pratico e diretto.
"""

RISPOSTA_FORZATA_PROMPT = """
Rispondi normalmente all'ultimo messaggio della mamma come Paola, anche se prima era stato generato un alert.
Non dire che c'e stato un alert.
Rispetta tutte le regole di tono, sicurezza e personalizzazione.
"""

REVISION_PROMPT = """
Scrivi una revisione aggiornata del piano come Paola.
La mamma ha gia ricevuto un piano o indicazioni precedenti: NON generare un piano iniziale da zero.

Devi partire da quello che e cambiato o da quello che non sta funzionando.
Spiega cosa manterresti, cosa correggeresti e cosa invece non toccheresti per non creare confusione.
Dai una linea concreta per i prossimi 3-5 giorni.

La revisione deve includere, se rilevante:
- lettura breve della situazione aggiornata;
- cosa e migliorato o quale segnale va valorizzato;
- cosa probabilmente sta mantenendo la difficolta;
- addormentamento serale;
- risvegli notturni;
- pisolini;
- gestione di seno, latte, biberon, ciuccio, braccio o contatto se presenti;
- cosa fare se protesta;
- cosa osservare nei prossimi giorni.

Non usare titoli, markdown, grassetti, bullet point o numerazioni.
Scrivi in prosa naturale da WhatsApp, ma ordinata e concreta.
Non dare diagnosi o consigli medici.
Non concludere con frasi automatiche, ma puoi chiudere con una frase neutra di direzione.
"""

CHECKUP_GENERATION_PROMPT = """
Genera le domande di checkup personalizzate come Paola.
La mamma ha gia ricevuto un piano o indicazioni precedenti: ora devi raccogliere informazioni mirate per capire cosa sta succedendo davvero.

Non mandare un questionario generico uguale per tutti.
Devi usare lo storico, il profilo del bambino, il piano precedente e gli ultimi messaggi per scegliere domande specifiche.

Scrivi un messaggio WhatsApp naturale, caldo e pratico.
Puoi usare numerazione per le domande, perche deve essere facile rispondere.
Fai massimo 6-9 domande.
Non dare consigli in questo messaggio: devi solo raccogliere informazioni.
Non fare domande inutili o gia chiarite nello storico.
Se conosci il nome del bambino, usalo.

Le domande devono essere concrete e pertinenti al problema attuale.
Esempi di adattamento:
- se il tema e seno, latte o biberon: chiedi quando lo cerca, in quali risvegli, cosa succede se la mamma aspetta, quanto beve/succhia, come si addormenta dopo;
- se il tema sono risvegli frequenti: chiedi orari, durata, modalita di rientro, primo risveglio, seconda parte della notte;
- se il tema sono pisolini o finestre di veglia: chiedi orari, durata, segnali di sonno, ultimo pisolino e orario nanna;
- se il tema e appoggio in culla o lettino: chiedi quando prova ad appoggiarlo, come reagisce, dopo quanti minuti, cosa accetta;
- se il tema e stanchezza della mamma: chiedi cosa pesa di piu e quale passaggio non riesce a sostenere;
- se ci sono denti, malattia, nido, viaggi o cambiamenti: chiedi solo i dettagli utili per il sonno, senza dare consigli medici.

Il messaggio deve iniziare in modo naturale, tipo: "Ok, allora rivediamo un attimo la situazione su ..." ma adattato al caso.
Deve chiudere chiedendo di rispondere con calma, senza promettere risultati.
"""

CHECKUP_CLASSIFIER_PROMPT = """
Sei un classificatore. Devi capire se la mamma ha risposto in modo sufficiente alle domande di checkup sul sonno.
Restituisci solo JSON valido.

Valori possibili per status:
- sufficient: ha dato informazioni concrete utili su almeno 2-3 aspetti tra miglioramenti, peggioramenti, addormentamento, risvegli, pisolini, latte/seno/ciuccio, eventi nuovi, difficolta principale.
- defer: scrive solo che rispondera dopo, ok, grazie, appena riesco, ti aggiorno, o altra risposta di rinvio/cortesia.
- incomplete: ha scritto qualcosa, ma e troppo poco per rivedere il piano in modo serio.

Restituisci:
{
  "status": "sufficient|defer|incomplete",
  "confidence": 0.0,
  "missing": "eventuali dati mancanti in una frase breve",
  "reason": "breve motivo"
}
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
            elif cmd in ("/checkup", "/chekup", "/check", "/ceckup"):
                with active_timers_lock:
                    if phone in active_timers:
                        active_timers[phone].cancel()
                        active_timers.pop(phone, None)
                send_checkup(phone)
            elif cmd == "/revisione":
                with active_timers_lock:
                    if phone in active_timers:
                        active_timers[phone].cancel()
                        active_timers.pop(phone, None)
                threading.Thread(target=send_revision, args=[phone, "manuale"], daemon=True).start()
            elif cmd == "/continua":
                with active_timers_lock:
                    if phone in active_timers:
                        active_timers[phone].cancel()
                        active_timers.pop(phone, None)
                threading.Thread(target=generate_forced_reply, args=[phone, "continua"], daemon=True).start()
            elif cmd == "/rispondi":
                with active_timers_lock:
                    if phone in active_timers:
                        active_timers[phone].cancel()
                        active_timers.pop(phone, None)
                threading.Thread(target=generate_forced_reply, args=[phone, "rispondi"], daemon=True).start()
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
    # Colonne aggiunte nelle versioni successive: ALTER sicuro anche su DB già esistente.
    cur.execute("ALTER TABLE consultations ADD COLUMN IF NOT EXISTS last_plan_sent_at TIMESTAMPTZ")
    cur.execute("ALTER TABLE consultations ADD COLUMN IF NOT EXISTS checkup_pending BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE consultations ADD COLUMN IF NOT EXISTS checkup_sent_at TIMESTAMPTZ")
    cur.execute("ALTER TABLE consultations ADD COLUMN IF NOT EXISTS last_post_plan_alert_at TIMESTAMPTZ")

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
               AND NOT (role = 'assistant' AND content = %s)
               ORDER BY timestamp ASC""",
            (phone, cutoff, SILENT_NO_REPLY_MARKER)
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

def set_last_plan_sent_at(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consultations (phone, last_plan_sent_at, checkup_pending)
            VALUES (%s, NOW(), FALSE)
            ON CONFLICT (phone) DO UPDATE
            SET last_plan_sent_at = NOW(), checkup_pending = FALSE
        """, (phone,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore set_last_plan_sent_at: {e}")


def get_last_plan_sent_at(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT last_plan_sent_at FROM consultations WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"Errore get_last_plan_sent_at: {e}")
        return None


def set_checkup_pending(phone, pending=True):
    try:
        conn = get_db()
        cur = conn.cursor()
        if pending:
            cur.execute("""
                INSERT INTO consultations (phone, checkup_pending, checkup_sent_at)
                VALUES (%s, TRUE, NOW())
                ON CONFLICT (phone) DO UPDATE
                SET checkup_pending = TRUE, checkup_sent_at = NOW()
            """, (phone,))
        else:
            cur.execute("""
                INSERT INTO consultations (phone, checkup_pending)
                VALUES (%s, FALSE)
                ON CONFLICT (phone) DO UPDATE
                SET checkup_pending = FALSE
            """, (phone,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore set_checkup_pending: {e}")


def is_checkup_pending(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(checkup_pending, FALSE) FROM consultations WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row and row[0])
    except Exception as e:
        logger.error(f"Errore is_checkup_pending: {e}")
        return False


def mark_post_plan_alert_sent(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO consultations (phone, last_post_plan_alert_at)
            VALUES (%s, NOW())
            ON CONFLICT (phone) DO UPDATE
            SET last_post_plan_alert_at = NOW()
        """, (phone,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore mark_post_plan_alert_sent: {e}")


def get_last_post_plan_alert_at(phone):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT last_post_plan_alert_at FROM consultations WHERE phone = %s", (phone,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"Errore get_last_post_plan_alert_at: {e}")
        return None


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
def model_prefers_max_completion_tokens(model):
    """I modelli GPT-5/reasoning usano max_completion_tokens al posto di max_tokens."""
    m = (model or "").lower()
    return m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4")


def model_prefers_default_temperature(model):
    """Alcuni modelli reasoning non accettano temperature personalizzate: meglio ometterla."""
    m = (model or "").lower()
    return m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4")


def openai_chat_completion(model, messages, max_tokens=1000, temperature=None, response_format=None, timeout=60):
    """
    Wrapper robusto per Chat Completions.
    Per i modelli GPT-5/reasoning usa direttamente max_completion_tokens
    ed evita temperature personalizzate, così Railway non riempie i log di 400 Bad Request.
    """
    base_kwargs = {
        "model": model,
        "messages": messages,
        "timeout": timeout
    }

    if max_tokens is not None:
        if model_prefers_max_completion_tokens(model):
            base_kwargs["max_completion_tokens"] = max_tokens
        else:
            base_kwargs["max_tokens"] = max_tokens

    if temperature is not None and not model_prefers_default_temperature(model):
        base_kwargs["temperature"] = temperature

    if response_format is not None:
        base_kwargs["response_format"] = response_format

    attempts = [dict(base_kwargs)]

    # Fallback 1: se response_format non fosse accettato da qualche modello, riprova senza.
    if "response_format" in base_kwargs:
        no_format = dict(base_kwargs)
        no_format.pop("response_format", None)
        attempts.append(no_format)

    # Fallback 2: compatibilità tra max_tokens e max_completion_tokens.
    # IMPORTANTE: per i modelli GPT-5/reasoning NON ritentare mai con max_tokens,
    # perché OpenAI restituisce 400: "Use max_completion_tokens instead".
    if "max_tokens" in base_kwargs:
        alt = dict(base_kwargs)
        alt["max_completion_tokens"] = alt.pop("max_tokens")
        attempts.append(alt)

    # Fallback 3: elimina temperature se un modello la rifiuta.
    if "temperature" in base_kwargs:
        no_temp = dict(base_kwargs)
        no_temp.pop("temperature", None)
        attempts.append(no_temp)

    last_error = None
    seen = set()
    for kwargs in attempts:
        key = tuple(sorted(kwargs.keys())) + tuple((k, str(v)) for k, v in kwargs.items() if k in ("model", "max_tokens", "max_completion_tokens", "temperature"))
        if key in seen:
            continue
        seen.add(key)
        try:
            response = openai_client.chat.completions.create(**kwargs)
            logger.info(
                f"OpenAI OK — model={kwargs.get('model')} — "
                f"token_param={'max_completion_tokens' if 'max_completion_tokens' in kwargs else 'max_tokens' if 'max_tokens' in kwargs else 'none'}"
            )
            return response
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
               AND NOT (role = 'assistant' AND content = %s)
               ORDER BY timestamp DESC
               LIMIT %s""",
            (phone, SILENT_NO_REPLY_MARKER, limit)
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


def acquisto_dichiarato(text):
    """Rileva a codice quando la mamma dichiara di aver già acquistato o avuto accesso al materiale.

    Questa regola viene eseguita prima del router GPT in fase 0, così frasi come
    "ho già acquistato", "ho scaricato la guida" o "ho letto il pdf" avviano
    sempre la sequenza acquisto. Evita però intenzioni future tipo "lo compro".
    """
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t).strip()

    # Frasi che indicano acquisto/pagamento già completato.
    segnali_forti = [
        "ho acquistato", "ho già acquistato", "ho gia acquistato", "ho appena acquistato",
        "ho comprato", "ho già comprato", "ho gia comprato", "ho appena comprato",
        "ho fatto l'ordine", "ho effettuato l'ordine", "ordine completato", "ordine effettuato",
        "ho fatto il pagamento", "ho effettuato il pagamento", "ho pagato",
        "pagamento completato", "pagamento effettuato", "pagamento andato a buon fine",
        "l'ho preso", "l ho preso", "l'ho comprato", "l ho comprato",
        "l'ho acquistato", "l ho acquistato", "ho fatto l'acquisto", "ho fatto acquisto",
        "ho preso il pacchetto", "ho preso il percorso", "ho acquistato il percorso",
        "ho già acquistato il percorso", "ho gia acquistato il percorso",
        "ho acquistato la consulenza", "ho preso la consulenza",
        "ho preso la guida", "ho acquistato la guida", "ho comprato la guida",
    ]
    if any(s in t for s in segnali_forti):
        return True

    # Accesso al materiale = acquisto già fatto, ma solo se vicino a parole prodotto/materiale.
    verbi_accesso = [
        "ho scaricato", "ho già scaricato", "ho gia scaricato",
        "ho letto", "ho già letto", "ho gia letto",
        "ho ricevuto", "ho già ricevuto", "ho gia ricevuto",
        "mi è arrivato", "mi e arrivato", "mi è arrivata", "mi e arrivata",
        "mi hanno mandato", "mi avete mandato", "ho accesso", "sono entrata", "sono dentro"
    ]
    parole_materiale = [
        "guida", "pdf", "manuale", "materiale", "percorso", "sonno magico",
        "metodo paola", "consulenza", "pacchetto"
    ]
    if any(v in t for v in verbi_accesso) and any(p in t for p in parole_materiale):
        return True

    # Alcune mamme scrivono solo "mi è arrivato tutto" dopo checkout: consideriamolo acquisto
    # se nel messaggio compare anche ordine/pagamento/acquisto.
    if any(x in t for x in ["mi è arrivato tutto", "mi e arrivato tutto", "ho ricevuto tutto", "ho scaricato tutto"]) and \
       any(x in t for x in ["ordine", "pagamento", "acquisto", "guida", "percorso"]):
        return True

    return False


def lead_problem_described(text):
    """Capisce se una lead in fase 0 ha gia raccontato un problema concreto del sonno.

    Serve come protezione extra: se il router classifica come richiesta_info_percorso
    ma nel testo ci sono dettagli su risvegli, seno, pisolini, pianto o addormentamento,
    il bot deve fare una prima analisi e non limitarsi a chiedere "raccontami".
    """
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) < 45:
        return False

    sleep_terms = [
        "si sveglia", "sveglia", "risvegli", "risveglio", "notte", "notti",
        "dorme", "dormire", "sonno", "nanna", "addormenta", "addormentarsi",
        "seno", "latte", "biberon", "ciuccio", "braccio", "braccia", "cull",
        "lettone", "lettino", "culla", "next to me", "pisolino", "pisolini",
        "piange", "pianto", "urla", "contatto", "ogni ora", "ogni due ore",
        "stanca", "distrutta", "non ce la faccio", "mesi", "anni"
    ]
    count = sum(1 for term in sleep_terms if term in t)
    return count >= 2 or (count >= 1 and len(t) >= 120)


def normalize_phase0_intent(router_result, pending_text):
    """Rende più coerente la fase 0 commerciale.

    - info vaghe restano info e chiedono prima la difficolta;
    - se nel testo c'e gia un problema concreto, forza descrizione_problema_sonno,
      cosi parte la prima analisi + proposta percorso.
    """
    if not router_result:
        return router_result
    intent = router_result.get("intent", "altro")
    if intent in {"saluto_vago", "richiesta_info_percorso", "altro"} and lead_problem_described(pending_text):
        r = dict(router_result)
        r["intent"] = "descrizione_problema_sonno"
        r["reason"] = (r.get("reason", "") + " | override codice: lead ha descritto un problema concreto del sonno").strip()
        r["confidence"] = max(float(r.get("confidence", 0) or 0), 0.82)
        r["safe_auto_reply"] = True
        r["needs_human"] = False
        return r
    return router_result


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
- Per child_age usa prima l'età dichiarata esplicitamente dalla mamma in mesi o anni. Non calcolare l'età dalla data di nascita se la mamma ha già scritto l'età. Se l'età non è chiara, ometti child_age.
- Se la data è ambigua, copiala come scritta dalla mamma in birth_date senza reinterpretarla.
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
    if intent == "richiesta_info_percorso" and fase == 0:
        return """
La persona e ancora lead e ha chiesto informazioni senza descrivere davvero il problema.
Non mandare subito il link e non vendere subito in modo freddo.
Rispondi breve e chiedi di raccontarti in poche parole qual e la difficolta principale con il sonno del bambino, cosi puoi capire meglio la situazione prima di consigliarle il percorso giusto.
Se chiede solo il prezzo in modo diretto, puoi accennare che il percorso personalizzato parte da 37 euro e il Premium dura 60 giorni a 67 euro, ma chiudi chiedendo la difficolta principale prima di orientarla.
"""
    if intent in ("descrizione_problema_sonno", "richiesta_consiglio_gratuito") and fase == 0:
        if link_sent:
            return """
La persona e ancora lead, ha gia raccontato una difficolta concreta e il link e gia stato mandato.
Non ripetere il link, a meno che lo chieda espressamente.
Fai una prima lettura breve e personalizzata della situazione, senza dare un piano gratuito completo.
Accenna alla direzione di lavoro e mantieni la conversazione naturale verso il percorso.
"""
        return f"""
La persona e ancora lead e ha gia descritto una difficolta concreta del sonno.
Non fare altre domande generiche: fai subito una prima analisi commerciale personalizzata.
Devi riconoscere la difficolta specifica, spiegare in modo semplice cosa potrebbe esserci dietro, senza diagnosi e senza dare un piano completo gratuito.
Accenna alla direzione di lavoro, facendo capire che andrebbe vista su orari, pisolini, addormentamento e risvegli.
Poi presenta il Percorso Premium: 60 giorni di supporto WhatsApp personalizzato al costo di {OFFERS['premium']['price']} euro, con questionario iniziale, piano su misura e guide PDF.
Inserisci il link una sola volta: {LINK_PREMIUM}
Chiudi dicendo che dopo l'ordine puo scriverti su WhatsApp e partite con l'analisi personalizzata.
"""
    if intent in ("domanda_percorso_attivo", "aggiornamento_percorso_attivo", "richiesta_pratica_immediata") or fase == 4:
        return """
La persona è in percorso attivo.
Rispondi collegandoti al profilo bambino e allo storico recente.
Dai massimo 1 o 2 indicazioni pratiche, non cambiare troppe cose insieme.
Se è una richiesta immediata, rispondi breve e operativo.
Se è un aggiornamento, valorizza o normalizza in modo specifico.
Se nel messaggio compaiono febbre, tosse, raffreddore, dentini o malattia recente, non dare consigli medici: riconosci che quando un bambino sta male può cercare più contatto/latte/braccio, invita a seguire il pediatra se non è ancora del tutto in forma, e poi dai indicazioni solo sul rientro graduale alla routine del sonno.
Non parlare di scadenze, rinnovi o fine percorso.
"""
    if intent in ("dubbio_medico_lieve", "dubbio_medico_delicato"):
        return """
Rispondi in modo prudente.
Non dare diagnosi, non parlare di farmaci, dosi, cause mediche o cure.
Per la parte sanitaria rimanda al pediatra, soprattutto se il bambino non è ancora in forma.
Poi, se la domanda riguarda il sonno, dai solo indicazioni di rientro morbido alla routine: aspettare che il bambino stia meglio, non irrigidirsi durante la malattia, riprendere gradualmente le abitudini precedenti e lavorare su latte/contatto senza forzare.
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

    if intent == "richiesta_info_percorso" and fase == 0 and confidence >= 0.70 and not lead_problem_described(pending_text):
        return "Ciao, sono Paola 😊\n\nCerto, prima di spiegarti bene il percorso mi aiuta capire la situazione: qual e la difficolta principale che stai vivendo con il sonno del tuo bimbo?"

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
        return NO_REPLY

    return None


def should_hold_for_human(router_result):
    if not router_result:
        return False
    intent = router_result.get("intent", "")
    # Non blocchiamo automaticamente ogni messaggio che cita febbre/tosse/raffreddore.
    # Il bot deve rispondere sul sonno con cautela e senza consigli medici.
    # Blocchiamo solo quando il router segnala davvero bisogno umano o casi commerciali/relazionali delicati.
    if router_result.get("needs_human") is True:
        return True
    if intent in {"sospetto_ai_o_richiesta_paola", "necessita_revisione_umano"}:
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
    if direct == NO_REPLY:
        mark_silent_no_reply(phone, f"intent={router_result.get('intent', 'messaggio_cortesia')}")
        return None
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
Tema sanitario citato: {router_result.get('entities', {}).get('medical_topic', False)}
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


def is_obvious_closing_message(text):
    """Riconosce chiusure/cortesie brevi per evitare risposte automatiche inutili.
    Non deve intercettare domande, conferme questionario o messaggi con contenuto sul sonno.
    """
    if not text:
        return False
    raw = text.strip()
    if len(raw) > 90:
        return False
    if "?" in raw:
        return False
    t = raw.lower()
    t = re.sub(r"[^\w\sàèéìòùÀÈÉÌÒÙ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    # Non bloccare parole che possono avere valore operativo o commerciale.
    important_terms = [
        "finito", "ho finito", "risposto", "completato", "pronta", "ordine", "pagato", "pagamento",
        "acquisto", "bonifico", "link", "questionario", "piano", "svegl", "dorme", "dormito",
        "piange", "seno", "latte", "biberon", "ciuccio", "febbre", "tosse", "raffreddore",
        "risvegl", "nanna", "pisolino", "orario", "come faccio", "cosa faccio"
    ]
    if any(term in t for term in important_terms):
        return False

    exact_closures = {
        "ok", "ok grazie", "ok perfetto", "ok va bene", "ok va benissimo", "ok ci provo",
        "va bene", "va bene grazie", "va benissimo", "va benissimo grazie",
        "perfetto", "perfetto grazie", "grazie", "grazie mille", "grazie cara", "grazie mille cara",
        "d accordo", "daccordo", "ci provo", "provo", "provo così", "provo cosi",
        "chiaro", "capito", "benissimo", "ottimo", "a posto", "tutto chiaro",
        "ti aggiorno", "poi ti aggiorno", "grazie ti aggiorno", "ok ti aggiorno"
    }
    if t in exact_closures:
        return True

    # Chiusure composte molto brevi, tipo "ok grazie mille" o "perfetto allora provo".
    closure_starts = ("ok", "va bene", "perfetto", "grazie", "benissimo", "capito", "chiaro")
    closure_words = {"ok", "va", "bene", "benissimo", "perfetto", "grazie", "mille", "cara", "capito", "chiaro", "provo", "cosi", "così", "allora", "ti", "aggiorno", "dopo"}
    words = set(t.split())
    if t.startswith(closure_starts) and words.issubset(closure_words):
        return True

    return False


def mark_silent_no_reply(phone, reason=""):
    """Segna nel DB che il bot non deve rispondere a una chiusura, senza inviare nulla."""
    try:
        save_message(phone, "assistant", SILENT_NO_REPLY_MARKER)
        logger.info(f"Nessuna risposta automatica per {phone} — {reason or 'chiusura/cortesia'}")
    except Exception as e:
        logger.error(f"Errore marker no-reply per {phone}: {e}")

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

def quick_commands_text(include_checkup=True):
    lines = [
        "",
        "Comandi rapidi nel topic:",
        "➜ /continua = autorizza il bot a rispondere comunque con cautela",
        "➜ /rispondi = risposta normale GPT all'ultimo messaggio",
        "➜ /pausa = gestisco io manualmente"
    ]
    if include_checkup:
        lines.extend([
            "➜ /checkup = mando domande di aggiornamento personalizzate",
            "➜ /revisione = genero revisione aggiornata"
        ])
    return "\n".join(lines)


def manual_alert_message(phone, router_result, message_text):
    return (
        f"⚠️ Revisione manuale consigliata per {phone}\n"
        f"Intento: {router_result.get('intent')}\n"
        f"Motivo: {router_result.get('reason')}\n"
        f"Messaggio:\n{message_text}"
        f"{quick_commands_text()}"
    )


def generate_personalized_checkup(phone):
    """Genera domande di checkup sempre personalizzate sulla situazione della mamma."""
    try:
        # Aggiorna il profilo prima di generare le domande, cosi il checkup parte dal contesto piu recente.
        try:
            extract_child_profile_from_history(phone)
        except Exception as e:
            logger.error(f"Errore estrazione profilo prima del checkup: {e}")

        profile_text = profile_to_text(get_child_profile(phone))
        recent_history = get_recent_history(phone, limit=45)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_BASE},
            {"role": "system", "content": CHECKUP_GENERATION_PROMPT},
            {"role": "system", "content": f"Profilo bambino strutturato:\n{profile_text}"}
        ]
        messages.extend(recent_history)
        messages.append({"role": "user", "content": (
            "Genera ora le domande di checkup PERSONALIZZATE per questa mamma. "
            "Non usare il questionario standard. Leggi bene lo storico e scegli solo le domande utili "
            "per rivedere il piano in base alla situazione attuale."
        )})

        response = openai_chat_completion(
            model=MODEL_CHAT,
            messages=messages,
            max_tokens=1800,
            temperature=TEMP_CHAT,
            timeout=120
        )
        checkup = response.choices[0].message.content.strip()
        checkup = checkup.replace("!", ".")
        return checkup
    except Exception as e:
        logger.error(f"Errore generazione checkup personalizzato per {phone}: {e}")
        threading.Thread(
            target=send_telegram,
            args=[f"⚠️ Errore checkup personalizzato per {phone}: {e}"],
            daemon=True
        ).start()
        return None


def send_checkup(phone):
    checkup = generate_personalized_checkup(phone)
    if not checkup:
        send_to_topic(phone, "⚠️ Non sono riuscito a generare il checkup personalizzato. Riprova /checkup tra poco oppure scrivi tu manualmente.", True)
        return
    save_message(phone, "assistant", checkup)
    send_whatsapp_message(phone, checkup)
    set_checkup_pending(phone, True)
    logger.info(f"Checkup personalizzato inviato a {phone} — lunghezza {len(checkup)} caratteri")


def classify_checkup_response(pending_text):
    default = {"status": "incomplete", "confidence": 0.0, "missing": "", "reason": "fallback"}
    try:
        response = openai_chat_completion(
            model=MODEL_CLASSIFIER,
            messages=[
                {"role": "system", "content": CHECKUP_CLASSIFIER_PROMPT},
                {"role": "user", "content": pending_text or ""}
            ],
            max_tokens=350,
            temperature=0,
            response_format={"type": "json_object"},
            timeout=60
        )
        data = parse_json_safely(response.choices[0].message.content, default)
        if not isinstance(data, dict):
            return default
        data.setdefault("status", "incomplete")
        data.setdefault("confidence", 0.0)
        data.setdefault("missing", "")
        data.setdefault("reason", "")
        return data
    except Exception as e:
        logger.error(f"Errore classificazione risposta checkup: {e}")
        return default


def send_revision(phone, reason="manuale"):
    logger.info(f"Generazione revisione per {phone} — motivo {reason}")
    try:
        extract_child_profile_from_history(phone)
    except Exception as e:
        logger.error(f"Errore estrazione profilo prima della revisione: {e}")

    history = get_history(phone)
    profile_text = profile_to_text(get_child_profile(phone))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_BASE},
        {"role": "system", "content": REVISION_PROMPT},
        {"role": "system", "content": f"Profilo bambino strutturato:\n{profile_text}"}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": (
        "Genera ora una revisione aggiornata del piano. "
        "Non rifare il piano iniziale da zero. Usa tutte le informazioni recenti, "
        "specialmente le risposte al checkup o le ultime difficolta raccontate dalla mamma. "
        "Sii concreta, specifica e utile per i prossimi giorni."
    )})
    try:
        response = openai_chat_completion(
            model=MODEL_PLAN,
            messages=messages,
            max_tokens=4200,
            temperature=TEMP_PLAN,
            timeout=180
        )
        revisione = response.choices[0].message.content.strip()
        logger.info(f"Revisione generata per {phone} — lunghezza {len(revisione)} caratteri")
        context = {"link_sent": True, "asks_link": False}
        revisione, issue = validate_reply(revisione, context)
        if issue:
            revisione = rewrite_reply_if_needed(revisione, issue, context)
    except Exception as e:
        logger.error(f"Errore generazione revisione: {e}")
        threading.Thread(target=send_telegram, args=[f"⚠️ Errore revisione per {phone}: {e}"], daemon=True).start()
        return

    save_message(phone, "assistant", revisione)
    send_whatsapp_message(phone, revisione)
    set_fase(phone, 4)
    set_checkup_pending(phone, False)
    set_last_plan_sent_at(phone)
    logger.info(f"Revisione inviata a {phone}")


def generate_forced_reply(phone, mode="continua"):
    pending = get_messages_since_last_reply(phone)
    pending_text = "\n".join(pending).strip()
    if not pending_text:
        send_to_topic(phone, "⚠️ Nessun messaggio nuovo in attesa a cui rispondere.", True)
        return

    fase = get_fase(phone)
    router_result = classify_message(phone, fase, pending_text, image_url=None)
    context = build_ai_context(phone, fase, router_result, pending_text)
    special_prompt = CONTINUA_PROMPT if mode == "continua" else RISPOSTA_FORZATA_PROMPT
    context_text = f"""
Contesto operativo:
Fase: {fase}
Intento rilevato: {router_result.get('intent', 'altro')}
Tipo messaggio: {router_result.get('message_type', 'altro')}
Tema sanitario citato: {router_result.get('entities', {}).get('medical_topic', False)}

Regola business per questa risposta:
{context['business_rule']}

Profilo bambino:
{context['profile_text']}
"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_BASE},
        {"role": "system", "content": CHAT_RESPONSE_PROMPT},
        {"role": "system", "content": special_prompt},
        {"role": "system", "content": context_text}
    ]
    messages.extend(context["recent_history"])
    messages.append({"role": "user", "content": pending_text})
    try:
        response = openai_chat_completion(
            model=MODEL_CHAT,
            messages=messages,
            max_tokens=1800,
            temperature=TEMP_CHAT,
            timeout=90
        )
        reply = response.choices[0].message.content.strip()
        clean, issue = validate_reply(reply, context)
        clean = rewrite_reply_if_needed(clean, issue, context) if issue else clean
        if clean:
            save_message(phone, "assistant", clean)
            send_whatsapp_message(phone, clean)
            logger.info(f"Risposta forzata {mode} inviata a {phone}")
    except Exception as e:
        logger.error(f"Errore risposta forzata {mode} per {phone}: {e}")
        threading.Thread(target=send_telegram, args=[f"⚠️ Errore /{mode} per {phone}: {e}"], daemon=True).start()


def maybe_send_post_plan_alert(phone, router_result, pending_text):
    intent = router_result.get("intent", "") if router_result else ""
    text = (pending_text or "").lower()
    difficulty_terms = [
        "non funziona", "non sta funzionando", "non vedo miglioramenti", "nessun miglioramento",
        "sono stanca", "sono distrutta", "non ce la faccio", "non ce la faccio piu", "non ce la faccio più",
        "e peggiorato", "è peggiorato", "peggiorato", "si sveglia ancora tantissimo", "risvegli continui"
    ]
    is_difficulty = intent == "difficolta_persistente_post_piano" or any(term in text for term in difficulty_terms)
    if not is_difficulty:
        return
    last_plan = get_last_plan_sent_at(phone)
    if not last_plan:
        return
    try:
        now = datetime.now(last_plan.tzinfo) if getattr(last_plan, 'tzinfo', None) else datetime.now()
        hours = (now - last_plan).total_seconds() / 3600
    except Exception:
        return
    if hours < 72:
        logger.info(f"Difficolta post-piano rilevata per {phone}, ma piano inviato da {hours:.1f} ore: nessun alert checkup")
        return
    last_alert = get_last_post_plan_alert_at(phone)
    if last_alert:
        try:
            now2 = datetime.now(last_alert.tzinfo) if getattr(last_alert, 'tzinfo', None) else datetime.now()
            alert_hours = (now2 - last_alert).total_seconds() / 3600
            if alert_hours < 24:
                return
        except Exception:
            pass
    mark_post_plan_alert_sent(phone)
    threading.Thread(
        target=send_telegram,
        args=[(
            f"⚠️ Possibile difficolta post-piano per {phone}\n"
            f"Sono passati almeno 3 giorni dal piano e la mamma segnala stanchezza, pochi miglioramenti o peggioramento.\n"
            f"Il bot rispondera comunque normalmente, ma valuta tu se usare /checkup o /continua.\n"
            f"Messaggio:\n{pending_text}"
            f"{quick_commands_text()}"
        )],
        daemon=True
    ).start()


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
            timeout=180
        )
        piano = response.choices[0].message.content.strip()
        logger.info(f"Piano generato per {phone} — lunghezza {len(piano)} caratteri")
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
    logger.info(f"Piano inviato a {phone}")
    set_fase(phone, 4)
    set_start_date(phone, datetime.now().date())
    set_checkup_pending(phone, False)
    set_last_plan_sent_at(phone)

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

    # Priorità assoluta in fase 0: se la mamma dichiara di aver già acquistato
    # o di aver scaricato/letto la guida, avvia subito la sequenza senza aspettare GPT.
    if fase == 0 and acquisto_dichiarato(combined_raw):
        logger.info(f"Acquisto dichiarato rilevato a codice per {phone}")
        invia_sequenza_acquisto(phone)
        return

    # Router semantico: non invia nulla, serve solo per decidere meglio.
    router_result = classify_message(phone, fase, combined_raw, image_url=image_url)
    if fase == 0:
        router_result = normalize_phase0_intent(router_result, combined_raw)
    logger.info(f"Router per {phone}: {router_result}")

    if should_hold_for_human(router_result):
        threading.Thread(
            target=send_telegram,
            args=[manual_alert_message(phone, router_result, combined_raw)],
            daemon=True
        ).start()
        return

    if fase == 4 and is_checkup_pending(phone):
        check = classify_checkup_response(combined_raw)
        logger.info(f"Checkup response per {phone}: {check}")
        status = check.get("status", "incomplete")
        confidence = float(check.get("confidence", 0) or 0)
        if status == "sufficient" and confidence >= 0.60:
            send_revision(phone, reason="checkup automatico")
            return
        if status == "defer" and confidence >= 0.60:
            mark_silent_no_reply(phone, "checkup in attesa: risposta di rinvio/cortesia")
            return
        missing = check.get("missing") or "qualche dettaglio su addormentamento, risvegli e pisolini"
        risposta = (
            "Ok, per rivederlo bene mi manca ancora " + missing + ". "
            "Quando riesci mandami questi dettagli, cosi posso rielaborare il piano senza andare a tentativi 🤍"
        )
        save_message(phone, "assistant", risposta)
        send_whatsapp_message(phone, risposta)
        return

    if fase == 0:
        is_acquisto = acquisto_dichiarato(combined_raw)
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
        maybe_send_post_plan_alert(phone, router_result, combined_raw)
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

    if body.startswith("/checkup") or body.startswith("/chekup") or body.startswith("/check") or body.startswith("/ceckup"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            with active_timers_lock:
                if target in active_timers:
                    active_timers[target].cancel()
                    active_timers.pop(target, None)
            send_checkup(target)
        return Response("OK", status=200)

    if body.startswith("/revisione"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            with active_timers_lock:
                if target in active_timers:
                    active_timers[target].cancel()
                    active_timers.pop(target, None)
            threading.Thread(target=send_revision, args=[target, "manuale"], daemon=True).start()
        return Response("OK", status=200)

    if body.startswith("/continua"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            with active_timers_lock:
                if target in active_timers:
                    active_timers[target].cancel()
                    active_timers.pop(target, None)
            threading.Thread(target=generate_forced_reply, args=[target, "continua"], daemon=True).start()
        return Response("OK", status=200)

    if body.startswith("/rispondi"):
        parts = body.strip().split()
        if len(parts) == 2:
            target = parts[1].replace("+", "").replace(" ", "")
            with active_timers_lock:
                if target in active_timers:
                    active_timers[target].cancel()
                    active_timers.pop(target, None)
            threading.Thread(target=generate_forced_reply, args=[target, "rispondi"], daemon=True).start()
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

    # Se la chat è in pausa, NON deve partire nessuna risposta automatica.
    # Però il messaggio della mamma deve comunque essere salvato e inoltrato nel topic Telegram,
    # così Paola può leggerlo e rispondere manualmente dal topic.
    chat_in_pausa = get_fase(phone) == 99

    text_to_process = body
    image_url_to_process = None

    if num_media > 0 and media_url:
        if media_type.startswith("audio/"):
            transcribed = transcribe_audio(media_url)
            text_to_process = transcribed if transcribed else "[messaggio vocale non comprensibile]"
        elif media_type.startswith("image/"):
            image_url_to_process = media_url
            text_to_process = body or "[immagine]"
        elif media_type.startswith("video/"):
            if chat_in_pausa:
                text_to_process = body or "[video ricevuto — non elaborato automaticamente]"
            else:
                send_whatsapp_message(phone, "Non riesco a vedere i video, scrivimi pure qui in chat 🙏")
                return Response("OK", status=200)

    if not text_to_process and not image_url_to_process:
        return Response("OK", status=200)

    saved_content = text_to_process or "[immagine]"
    save_message(phone, "user", saved_content)

    # Notifica nel topic Telegram anche se la chat è in pausa.
    threading.Thread(target=send_to_topic, args=[phone, saved_content, False], daemon=True).start()

    if chat_in_pausa:
        logger.info(f"Chat {phone} in pausa — messaggio salvato e inoltrato a Telegram, nessun timer")
        return Response("OK", status=200)

    fase_corrente = get_fase(phone)
    if fase_corrente in (0, 4) and not image_url_to_process and is_obvious_closing_message(text_to_process):
        mark_silent_no_reply(phone, "chiusura breve rilevata prima del timer")
        return Response("OK", status=200)

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


