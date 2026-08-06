import pytest
from unittest.mock import patch

from app import (
    MSG_BENVENUTO,
    acquisto_dichiarato,
    acquisto_dichiarato_in_contesto,
    conversation_has_purchase_context,
    is_acquisto_confermato,
)


@pytest.mark.parametrize("text", [
    "Acquisto",
    "Acquisto ☺️",
    "Pagato",
    "Comprato",
    "ordine fatto",
    "Comprato quello da 47€",
    "ho acquistato",
    "abbiamo acquistato il Premium",
    "abbiamo iniziato a leggere le guide",
    "ho preso il 67",
    "preso il premium",
    "ho preso il base",
    "preso quello da 47",
])
def test_acquisto_dichiarato_positive(text):
    assert acquisto_dichiarato(text) is True


@pytest.mark.parametrize("text", [
    "Vorrei acquistare",
    "Non ho ancora acquistato",
    "Lo compro domani",
    "Quanto costa quello da 47€?",
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


def test_msg_benvenuto_is_non_empty():
    assert len(MSG_BENVENUTO.strip()) > 20
