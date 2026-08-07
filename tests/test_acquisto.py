import pytest
from unittest.mock import patch

from app import (
    MSG_BENVENUTO,
    acquisto_dichiarato,
    acquisto_dichiarato_in_contesto,
    conversation_has_purchase_context,
    FORM_LEAD_POTTY,
    FORM_LEAD_SLEEP,
    is_acquisto_confermato,
    LEAD_FLOW_POTTY_GHL,
    LEAD_FLOW_SLEEP_GHL,
    LEAD_FLOW_SLEEP_MANUAL,
    get_lead_provenance_label,
    _parse_lead_source_note,
)


@pytest.mark.parametrize("text", [
    "Acquisto",
    "Acquisto ☺️",
    "Pagato",
    "Comprato",
    "ordine fatto",
    "pagamento fatto",
    "Comprato quello da 47€",
    "ho acquistato",
    "abbiamo acquistato il Premium",
    "abbiamo iniziato a leggere le guide",
    "ho preso il 67",
    "preso il premium",
    "ho preso il base",
    "preso quello da 47",
    # Caso reale monitor: prima falliva per "appena" + articolo "il".
    "Ho appena fatto il pagamento del piano base",
    "ho fatto il pagamento",
    "ho effettuato il pagamento",
    "ho appena effettuato il bonifico",
    "abbiamo già fatto l'ordine",
    "mio marito ha fatto il pagamento",
    "ho completato l'acquisto",
])
def test_acquisto_dichiarato_positive(text):
    assert acquisto_dichiarato(text) is True


@pytest.mark.parametrize("text", [
    "Vorrei acquistare",
    "Non ho ancora acquistato",
    "Non ho ancora fatto il pagamento",
    "Lo compro domani",
    "Quanto costa quello da 47€?",
    "Vorrei fare il pagamento del piano base",
    "fatto",
    "preso",
    "",
])
def test_acquisto_dichiarato_negative(text):
    assert acquisto_dichiarato(text) is False


def test_acquisto_dichiarato_in_contesto_fatto_con_link():
    phone = "+393331234567"
    with patch("app.conversation_has_purchase_context", return_value=True):
        assert acquisto_dichiarato_in_contesto(phone, "fatto") is True
        assert acquisto_dichiarato_in_contesto(phone, "preso") is True
        assert acquisto_dichiarato_in_contesto(phone, "ok fatto") is True


def test_acquisto_dichiarato_in_contesto_fatto_senza_link():
    phone = "+393331234567"
    with patch("app.conversation_has_purchase_context", return_value=False):
        assert acquisto_dichiarato_in_contesto(phone, "fatto") is False
        assert acquisto_dichiarato_in_contesto(phone, "preso") is False


def test_conversation_has_purchase_context_da_storico():
    phone = "+393331234567"
    history = [
        {"role": "assistant", "content": f"Ti lascio il link:\nhttps://shop.genitorinarmonia.com/sonno"},
    ]
    with patch("app.link_gia_inviato", return_value=False), \
         patch("app.get_lead_meta", return_value={}), \
         patch("app.get_recent_history", return_value=history):
        assert conversation_has_purchase_context(phone) is True


def test_is_acquisto_confermato_bonifico_effettuato():
    router = {"intent": "bonifico_effettuato", "confidence": 0.85}
    assert is_acquisto_confermato("ok grazie", router_result=router) is True


def test_is_acquisto_confermato_router_acquisto():
    router = {"intent": "acquisto_completato", "confidence": 0.80}
    assert is_acquisto_confermato("Pagato", router_result=router) is True


def test_is_acquisto_confermato_router_con_contesto_soglia_bassa():
    phone = "+393331234567"
    router = {"intent": "acquisto_completato", "confidence": 0.65}
    with patch("app.conversation_has_purchase_context", return_value=True):
        assert is_acquisto_confermato("fatto", router_result=router, phone=phone) is True


def test_is_acquisto_confermato_low_confidence():
    router = {"intent": "bonifico_effettuato", "confidence": 0.50}
    assert is_acquisto_confermato("ok", router_result=router) is False


def test_is_acquisto_confermato_frase_reale_senza_router():
    # Deve partire anche senza router: gate deterministico prima di GPT.
    assert is_acquisto_confermato("Ho appena fatto il pagamento del piano base") is True


def test_is_acquisto_confermato_router_soglia_con_testo_pagamento():
    router = {"intent": "acquisto_completato", "confidence": 0.56}
    # Frase non coperta dal regex stretto, ma segnale lessicale + router.
    assert is_acquisto_confermato(
        "oggi il pagamento piano base ok fatto da me",
        router_result=router,
    ) is True


def test_msg_benvenuto_is_non_empty():
    assert len(MSG_BENVENUTO.strip()) > 20


def test_parse_lead_source_note():
    note = "origine=meta; prodotto=sonno infantile; campagna=Campagna Sonno Q1"
    parsed = _parse_lead_source_note(note)
    assert parsed["origine"] == "meta"
    assert parsed["campagna"] == "Campagna Sonno Q1"


def test_get_lead_provenance_label_meta_form():
    phone = "+393331234567"
    with patch("app.get_meta_form_state", return_value={"form_lead_type": FORM_LEAD_SLEEP}):
        assert get_lead_provenance_label(phone) == "Modulo Meta (sonno)"


def test_get_lead_provenance_label_template_ghl():
    phone = "+393331234567"
    with patch("app.get_meta_form_state", return_value={"form_lead_type": "none"}), \
         patch("app.get_lead_meta", return_value={"lead_flow": LEAD_FLOW_SLEEP_GHL, "contact_origin": "outbound_template"}), \
         patch("app.get_lead_source_note_meta", return_value={"origine": "meta", "campagna": "Campagna Sonno"}):
        assert get_lead_provenance_label(phone) == "Template GHL (sonno, meta, Campagna Sonno)"


def test_get_lead_provenance_label_template_manual():
    phone = "+393331234567"
    with patch("app.get_meta_form_state", return_value={"form_lead_type": "none"}), \
         patch("app.get_lead_meta", return_value={"lead_flow": LEAD_FLOW_SLEEP_MANUAL, "contact_origin": "outbound_template"}):
        assert get_lead_provenance_label(phone) == "Template (outreach manuale sonno)"


def test_get_lead_provenance_label_inbound():
    phone = "+393331234567"
    with patch("app.get_meta_form_state", return_value={"form_lead_type": "none"}), \
         patch("app.get_lead_meta", return_value={"lead_flow": "none", "contact_origin": "inbound_spontaneous"}):
        assert get_lead_provenance_label(phone) == "Altro (inbound spontaneo)"
