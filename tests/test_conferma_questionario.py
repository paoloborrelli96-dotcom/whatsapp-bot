import pytest
from unittest.mock import MagicMock, patch

from app import (
    REASONING_TOKEN_HEADROOM,
    _is_output_limit_error,
    is_explicit_finish_confirmation,
    openai_chat_completion,
)


@pytest.mark.parametrize("text", [
    "si",
    "Sì sì fatto",
    "tutto fatto",
    "Ho risposto a tutto",
    "ho finito grazie",
    "esatto",
    "confermo",
    # Caso reale monitor +393469449456: prima restava bloccata in fase 5.
    "Si ho risposto a questo su diego",
    "Sì ho risposto a tutto su Diego",
    "si ho scritto tutto",
    "si ti ho mandato tutto",
])
def test_finish_confirmation_positive(text):
    assert is_explicit_finish_confirmation(text) is True


@pytest.mark.parametrize("text", [
    "non ancora",
    "devo finire",
    "ti rispondo domani",
    "ok",
    "grazie mille",
    "si ho risposto solo alla prima",
    "manca ancora qualcosa",
    "si ma posso mandarti un vocale?",
    "",
])
def test_finish_confirmation_negative(text):
    assert is_explicit_finish_confirmation(text) is False


def test_finish_confirmation_ignora_racconto_lungo():
    lungo = (
        "si allora ieri notte diego si e svegliato tre volte e poi la mattina "
        "si e alzato presto quindi volevo aggiungere anche questa cosa al quadro"
    )
    assert is_explicit_finish_confirmation(lungo) is False


def test_is_output_limit_error():
    err = (
        "Error code: 400 - {'message': 'Could not finish the message because "
        "max_tokens or model output limit was reached.'}"
    )
    assert _is_output_limit_error(err) is True
    assert _is_output_limit_error("Error code: 401 - invalid api key") is False


def test_reasoning_model_riceve_headroom():
    fake = MagicMock()
    with patch("app.openai_client") as client:
        client.chat.completions.create.return_value = fake
        openai_chat_completion(model="gpt-5-nano", messages=[], max_tokens=260)

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 260 + REASONING_TOKEN_HEADROOM
    assert "max_tokens" not in kwargs


def test_retry_con_budget_maggiore_su_output_limit():
    fake = MagicMock()
    errore = Exception(
        "Error code: 400 - Could not finish the message because max_tokens "
        "or model output limit was reached."
    )

    with patch("app.openai_client") as client:
        client.chat.completions.create.side_effect = [errore, fake]
        result = openai_chat_completion(model="gpt-5-nano", messages=[], max_tokens=260)

    assert result is fake
    primo, secondo = client.chat.completions.create.call_args_list
    assert secondo.kwargs["max_completion_tokens"] > primo.kwargs["max_completion_tokens"]
