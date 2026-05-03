from modules.ai.client import OpenAIJSONClient
import pytest


def test_openai_client_extract_json_valid() -> None:
    client = OpenAIJSONClient()
    text = 'Here is the json: {"key": "value"}'
    assert client._extract_json(text) == {"key": "value"}


def test_openai_client_extract_json_invalid() -> None:
    client = OpenAIJSONClient()
    with pytest.raises(ValueError, match="Invalid JSON"):
        client._extract_json("No json here")
