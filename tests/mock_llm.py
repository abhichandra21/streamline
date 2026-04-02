"""Shared mock LLMClient for tests."""

from unittest.mock import MagicMock
from recommender.llm import LLMClient


def make_mock_llm(response_text: str) -> MagicMock:
    """Create a mock LLMClient that returns the given text from generate()."""
    client = MagicMock(spec=LLMClient)
    client.provider = "mock"
    client.generate.return_value = response_text
    return client


def make_mock_llm_sequence(responses: list[str]) -> MagicMock:
    """Create a mock LLMClient that returns texts in sequence from generate()."""
    client = MagicMock(spec=LLMClient)
    client.provider = "mock"
    client.generate.side_effect = responses
    return client
