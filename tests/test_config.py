from __future__ import annotations

from pathlib import Path

import pytest

from student_agent.config import Settings


def _base_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPETITION_API_URL", "https://competition.example")
    monkeypatch.setenv("COMPETITION_TEAM_API_KEY", "sk-team-abcdefghijklmnop")
    monkeypatch.setenv("MCP_ENDPOINT", "https://mcp.example")


def test_default_five_agent_budget_is_below_ten_billion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _base_environment(monkeypatch)
    settings = Settings.load(tmp_path)
    assert settings.coordinator_model == "qwen3:1.7b"
    assert settings.order_payment_model == "qwen3:1.7b"
    assert settings.shipment_seller_model == "qwen3:1.7b"
    assert settings.policy_resolution_model == "qwen3:1.7b"
    assert settings.verifier_model == "qwen3:1.7b"
    assert (
        settings.coordinator_params_b
        + settings.order_payment_params_b
        + settings.shipment_seller_params_b
        + settings.policy_resolution_params_b
        + settings.verifier_params_b
    ) == pytest.approx(8.5)
    assert settings.llm_num_ctx == 4096
    assert settings.llm_max_parallel_specialists == 3


def test_rejects_model_budget_at_or_above_ten_billion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("LLM_VERIFIER_PARAMS_B", "3.2")
    with pytest.raises(ValueError, match="must be below 10B"):
        Settings.load(tmp_path)


def test_unknown_model_size_requires_explicit_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("LLM_ORDER_PAYMENT_MODEL", "hosted-investigator")
    monkeypatch.delenv("LLM_ORDER_PAYMENT_PARAMS_B", raising=False)
    with pytest.raises(ValueError, match="LLM_ORDER_PAYMENT_PARAMS_B is required"):
        Settings.load(tmp_path)
