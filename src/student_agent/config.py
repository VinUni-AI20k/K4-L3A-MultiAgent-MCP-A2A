from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TEAM_KEY_PATTERN = re.compile(r"^sk-team-[A-Za-z0-9_-]{16,128}$")
MODEL_SIZE_PATTERN = re.compile(r"(?:^|[:_-])(\d+(?:\.\d+)?)b(?:$|[-_])", re.IGNORECASE)


def _model_parameter_count(model: str, environment_name: str) -> float:
    explicit = os.getenv(environment_name, "").strip()
    if explicit:
        value = float(explicit)
    else:
        match = MODEL_SIZE_PATTERN.search(model)
        if match is None:
            raise ValueError(f"{environment_name} is required when model size is not in its name")
        value = float(match.group(1))
    if value <= 0:
        raise ValueError(f"{environment_name} must be a positive number")
    return value


@dataclass(frozen=True)
class Settings:
    competition_api_url: str
    team_api_key: str
    mcp_endpoint: str
    llm_base_url: str
    llm_api_key: str
    coordinator_model: str
    order_payment_model: str
    shipment_seller_model: str
    policy_resolution_model: str
    verifier_model: str
    coordinator_params_b: float
    order_payment_params_b: float
    shipment_seller_params_b: float
    policy_resolution_params_b: float
    verifier_params_b: float
    llm_num_ctx: int
    llm_max_parallel_specialists: int
    llm_timeout_seconds: float
    root: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        llm_base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1").strip().rstrip("/")
        llm_api_key = os.getenv("LLM_API_KEY", "ollama").strip()
        coordinator_model = os.getenv("LLM_COORDINATOR_MODEL", "qwen3:1.7b").strip()
        order_payment_model = os.getenv("LLM_ORDER_PAYMENT_MODEL", "qwen3:1.7b").strip()
        shipment_seller_model = os.getenv("LLM_SHIPMENT_SELLER_MODEL", "qwen3:1.7b").strip()
        policy_resolution_model = os.getenv("LLM_POLICY_RESOLUTION_MODEL", "qwen3:1.7b").strip()
        verifier_model = os.getenv("LLM_VERIFIER_MODEL", "qwen3:1.7b").strip()
        num_ctx_text = os.getenv("LLM_NUM_CTX", "4096").strip()
        parallel_text = os.getenv("LLM_MAX_PARALLEL_SPECIALISTS", "3").strip()
        timeout_text = os.getenv("LLM_TIMEOUT_SECONDS", "180").strip()
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if not llm_base_url.startswith(("http://", "https://")):
            errors.append("LLM_BASE_URL must be an absolute HTTP(S) URL")
        if not all((
            llm_api_key, coordinator_model, order_payment_model, shipment_seller_model,
            policy_resolution_model, verifier_model,
        )):
            errors.append("LLM API key and model names must be non-empty")
        try:
            parameter_counts = (
                _model_parameter_count(coordinator_model, "LLM_COORDINATOR_PARAMS_B"),
                _model_parameter_count(order_payment_model, "LLM_ORDER_PAYMENT_PARAMS_B"),
                _model_parameter_count(shipment_seller_model, "LLM_SHIPMENT_SELLER_PARAMS_B"),
                _model_parameter_count(
                    policy_resolution_model, "LLM_POLICY_RESOLUTION_PARAMS_B"
                ),
                _model_parameter_count(verifier_model, "LLM_VERIFIER_PARAMS_B"),
            )
            if sum(parameter_counts) >= 10:
                errors.append("The conservative per-agent model budget must be below 10B")
        except ValueError as exc:
            errors.append(str(exc))
            parameter_counts = (1.7, 1.7, 1.7, 1.7, 1.7)
        try:
            llm_num_ctx = int(num_ctx_text)
            if not 2048 <= llm_num_ctx <= 32768:
                raise ValueError
        except ValueError:
            errors.append("LLM_NUM_CTX must be an integer between 2048 and 32768")
            llm_num_ctx = 4096
        try:
            llm_max_parallel_specialists = int(parallel_text)
            if not 1 <= llm_max_parallel_specialists <= 3:
                raise ValueError
        except ValueError:
            errors.append("LLM_MAX_PARALLEL_SPECIALISTS must be between 1 and 3")
            llm_max_parallel_specialists = 3
        try:
            llm_timeout_seconds = float(timeout_text)
            if not 1 <= llm_timeout_seconds <= 900:
                raise ValueError
        except ValueError:
            errors.append("LLM_TIMEOUT_SECONDS must be between 1 and 900")
            llm_timeout_seconds = 180.0
        if errors:
            raise ValueError("; ".join(errors))
        return cls(
            api_url,
            team_key,
            mcp_endpoint,
            llm_base_url,
            llm_api_key,
            coordinator_model,
            order_payment_model,
            shipment_seller_model,
            policy_resolution_model,
            verifier_model,
            *parameter_counts,
            llm_num_ctx,
            llm_max_parallel_specialists,
            llm_timeout_seconds,
            resolved_root,
        )
