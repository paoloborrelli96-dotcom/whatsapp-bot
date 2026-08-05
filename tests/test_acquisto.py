import pytest

from app import acquisto_dichiarato, is_acquisto_confermato, MSG_BENVENUTO


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
])
def test_acquisto_dichiarato_positive(text):
    assert acquisto_dichiarato(text) is True


@pytest.mark.parametrize("text", [
    "Vorrei acquistare",
    "Non ho ancora acquistato",
    "Lo compro domani",
    "Quanto costa quello da 47€?",
    "",
])
def test_acquisto_dichiarato_negative(text):
    assert acquisto_dichiarato(text) is False


def test_is_acquisto_confermato_bonifico_effettuato():
    router = {"intent": "bonifico_effettuato", "confidence": 0.85}
    assert is_acquisto_confermato("ok grazie", router_result=router) is True


def test_is_acquisto_confermato_router_acquisto():
    router = {"intent": "acquisto_completato", "confidence": 0.80}
    assert is_acquisto_confermato("Pagato", router_result=router) is True


def test_is_acquisto_confermato_low_confidence():
    router = {"intent": "bonifico_effettuato", "confidence": 0.50}
    assert is_acquisto_confermato("ok", router_result=router) is False


def test_msg_benvenuto_is_non_empty():
    assert len(MSG_BENVENUTO.strip()) > 20
