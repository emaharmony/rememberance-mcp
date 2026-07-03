"""
Tests for OllamaGateBackend — LLM-based classification fallback
"""

from unittest.mock import MagicMock, patch

import pytest

from remembrance_mcp.gate.gate import GateDecision
from remembrance_mcp.gate.ollama import OllamaGateBackend


@pytest.fixture
def backend():
    return OllamaGateBackend(base_url="http://localhost:11434", model="nemotron-3-nano:4b")


class TestParseDecision:
    def test_parse_persist(self, backend):
        assert backend._parse_decision("PERSIST") == GateDecision.PERSIST

    def test_parse_active(self, backend):
        assert backend._parse_decision("ACTIVE") == GateDecision.ACTIVE

    def test_parse_cold(self, backend):
        assert backend._parse_decision("COLD") == GateDecision.COLD

    def test_parse_skip(self, backend):
        assert backend._parse_decision("SKIP") == GateDecision.SKIP

    def test_parse_lowercase(self, backend):
        assert backend._parse_decision("persist") == GateDecision.PERSIST

    def test_parse_with_extra_text(self, backend):
        assert backend._parse_decision("I think this is PERSIST because...") == GateDecision.PERSIST

    def test_parse_unknown_defaults_cold(self, backend):
        assert backend._parse_decision("MAYBE") == GateDecision.COLD

    def test_parse_empty(self, backend):
        assert backend._parse_decision("") == GateDecision.COLD


class TestHeuristicFallback:
    def test_short_text_skip(self, backend):
        result = backend._heuristic_fallback("ok")
        assert result.decision == GateDecision.SKIP

    def test_decision_keyword_persist(self, backend):
        result = backend._heuristic_fallback("Ema decided to use Go for the project")
        assert result.decision == GateDecision.PERSIST

    def test_active_keyword(self, backend):
        result = backend._heuristic_fallback("Implementing vector search for the project")
        assert result.decision == GateDecision.ACTIVE

    def test_default_cold(self, backend):
        result = backend._heuristic_fallback("The meeting was about quarterly results")
        assert result.decision == GateDecision.COLD


class TestClassifyWithMock:
    @patch("remembrance_mcp.gate.ollama.urllib.request.urlopen")
    def test_classify_via_ollama(self, mock_urlopen, backend):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"response": "PERSIST"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = backend.classify("Ema decided Prism stays domain-agnostic")
        assert result.decision == GateDecision.PERSIST
        assert result.confidence == 0.75  # Ollama classification confidence

    @patch("remembrance_mcp.gate.ollama.urllib.request.urlopen")
    def test_classify_ollama_unavailable(self, mock_urlopen, backend):
        mock_urlopen.side_effect = Exception("Connection refused")

        result = backend.classify("Ema decided to use Go")
        # Should fall back to heuristic
        assert result.decision == GateDecision.PERSIST
        assert result.confidence == 0.7  # Heuristic confidence


class TestAvailability:
    @patch("remembrance_mcp.gate.ollama.urllib.request.urlopen")
    def test_available(self, mock_urlopen, backend):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"models": [{"name": "nemotron-3-nano:4b"}]}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert backend.is_available() is True

    @patch("remembrance_mcp.gate.ollama.urllib.request.urlopen")
    def test_unavailable(self, mock_urlopen, backend):
        mock_urlopen.side_effect = Exception("Connection refused")
        assert backend.is_available() is False
