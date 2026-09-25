"""Pure-Python A2A workflow for the Day09 L3A investigation task."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2

try:
    from openai import AsyncOpenAI
except ImportError:  # A missing optional runtime dependency must not stop the batch.
    AsyncOpenAI = None  # type: ignore[assignment,misc]

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ISSUE_TOOLS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_payment_timeline", "get_policy"),
    "unavailable_order_paid": ("get_order", "get_payment_timeline", "get_policy"),
    "late_delivery_seller": (
        "get_order", "get_order_items", "get_shipment_summary", "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order", "get_order_items", "get_shipment_summary", "get_policy",
    ),
    "valid_split_payment": (
        "get_order", "get_order_items", "get_payment_timeline", "get_policy",
    ),
    "payment_mismatch": ("get_order", "get_payment_timeline", "get_policy"),
    "duplicate_charge": (
        "get_order", "get_order_items", "get_payment_timeline", "get_policy",
    ),
    "refund_pending": ("get_payment_timeline", "get_refund_timeline", "get_policy"),
    "refund_failed": ("get_payment_timeline", "get_refund_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ),
}

ISSUE_PARTY: dict[str, str] = {
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "seller",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "valid_split_payment": "customer",
    "payment_mismatch": "payment_provider",
    "duplicate_charge": "payment_provider",
    "refund_pending": "payment_provider",
    "refund_failed": "payment_provider",
    "unsupported_claim": "customer",
    "insufficient_evidence": "unknown",
}

_LLM_CLIENT: Any | None = None
_LLM_CLIENT_KEY: tuple[str, str] | None = None


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


@dataclass(frozen=True)
class Evidence:
    tool_name: str
    evidence_ref: str
    domain: str
    data: Any


@dataclass
class CaseState:
    case: dict[str, Any]
    evidence: dict[str, Evidence] = field(default_factory=dict)
    order: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    payment: dict[str, Any] = field(default_factory=dict)
    shipment: dict[str, Any] = field(default_factory=dict)
    refund: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    issue: str = "insufficient_evidence"
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    policy_evaluation: PolicyEvaluation | None = None

    @property
    def case_id(self) -> str:
        return str(self.case["case_id"])

    @property
    def request(self) -> dict[str, Any]:
        value = self.case.get("customer_request", {})
        return value if isinstance(value, dict) else {}

    @property
    def order_id(self) -> str:
        return str(self.request.get("claimed_order_id", ""))

    def refs_for(self, tools: tuple[str, ...] | list[str]) -> list[str]:
        return _unique(
            [self.evidence[name].evidence_ref for name in tools if name in self.evidence]
        )


class SpecialistAgent:
    name = "specialist-agent"
    tools: tuple[str, ...] = ()

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def call(
        self,
        state: CaseState,
        tool_name: str,
        *,
        optional: bool = False,
        **arguments: str,
    ) -> Any:
        if tool_name not in self.tools:
            raise ValueError(f"{self.name} is not permitted to call {tool_name}")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                envelope = await asyncio.wait_for(
                    self.gateway.call(
                        tool_name, case_id=state.case_id, **arguments
                    ),
                    timeout=30.0,
                )
                evidence = Evidence(
                    tool_name=tool_name,
                    evidence_ref=envelope["evidence_ref"],
                    domain=envelope["domain"],
                    data=envelope["data"],
                )
                state.evidence[tool_name] = evidence
                self.trace.emit(
                    case_id=state.case_id,
                    event_type="tool_result_consumed",
                    actor=self.name,
                    tool_name=tool_name,
                    evidence_refs=[evidence.evidence_ref],
                    attributes={"attempt": attempt + 1, "domain": evidence.domain},
                )
                return evidence.data
            except RuntimeError as exc:
                last_error = exc
                if optional:
                    return None
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
            except (TimeoutError, httpx2.HTTPError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
        if optional:
            return None
        raise RuntimeError(f"{self.name}: required tool {tool_name} failed") from last_error


class OrderAgent(SpecialistAgent):
    name = "order-agent"
    tools = ("get_order", "get_order_items")

    async def investigate(self, state: CaseState) -> None:
        order = await self.call(state, "get_order", order_id=state.order_id)
        items = await self.call(state, "get_order_items", order_id=state.order_id)
        state.order = order if isinstance(order, dict) else {}
        state.items = _as_rows(items)
        self.trace.emit(
            case_id=state.case_id,
            event_type="handoff",
            actor=self.name,
            target="coordinator",
            decision_code="ORDER_EVIDENCE_READY",
            evidence_refs=state.refs_for(list(self.tools)),
        )


class PaymentAgent(SpecialistAgent):
    name = "payment-agent"
    tools = ("get_payment_timeline", "get_refund_timeline")

    async def investigate(self, state: CaseState) -> None:
        payment = await self.call(
            state, "get_payment_timeline", order_id=state.order_id
        )
        refund = await self.call(
            state, "get_refund_timeline", optional=True, order_id=state.order_id
        )
        state.payment = payment if isinstance(payment, dict) else {}
        state.refund = refund if isinstance(refund, dict) else {}
        self.trace.emit(
            case_id=state.case_id,
            event_type="handoff",
            actor=self.name,
            target="coordinator",
            decision_code="PAYMENT_EVIDENCE_READY",
            evidence_refs=state.refs_for(list(self.tools)),
        )


class ShipmentAgent(SpecialistAgent):
    name = "shipment-agent"
    tools = ("get_shipment_summary",)

    async def investigate(self, state: CaseState) -> None:
        shipment = await self.call(
            state, "get_shipment_summary", order_id=state.order_id
        )
        state.shipment = shipment if isinstance(shipment, dict) else {}
        self.trace.emit(
            case_id=state.case_id,
            event_type="handoff",
            actor=self.name,
            target="coordinator",
            decision_code="SHIPMENT_EVIDENCE_READY",
            evidence_refs=state.refs_for(list(self.tools)),
        )


class PolicyAgent(SpecialistAgent):
    name = "policy-agent"
    tools = ("get_policy",)

    async def investigate(self, state: CaseState) -> None:
        policy = await self.call(
            state,
            "get_policy",
            policy_version=str(state.case.get("policy_version", "")),
        )
        state.policy = policy if isinstance(policy, dict) else {}

    @staticmethod
    def delivery_delta_days(state: CaseState) -> int | None:
        delivered = _datetime(state.order.get("order_delivered_customer_date"))
        estimated = _datetime(state.order.get("order_estimated_delivery_date"))
        if delivered is None or estimated is None:
            return None
        return (delivered.date() - estimated.date()).days

    def decide(self, state: CaseState) -> dict[str, Any]:
        order_status = state.order.get("order_status")
        shipment_status = state.shipment.get("order_status")
        if order_status and shipment_status and order_status != shipment_status:
            state.conflicts.append(
                {
                    "field": "order_status",
                    "sources": ["get_order", "get_shipment_summary"],
                    "selected_source": "get_order",
                    "resolution_code": "AUTHORITATIVE_ORDER_SELECTED",
                }
            )
        state.issue = self._classify(state)
        state.policy_evaluation = evaluate_policy(state)
        rules = state.policy.get("rules", {})
        rule = rules.get(state.issue) if isinstance(rules, dict) else None
        if not isinstance(rule, dict):
            state.issue = "insufficient_evidence"
            rule = {
                "case_status": "needs_investigation",
                "recommended_action": "collect_additional_evidence",
                "refund_brl": 0.0,
                "responsible_parties": [
                    {"party_type": "unknown", "party_id": None}
                ],
            }
        policy_refs = state.refs_for(["get_policy"])
        self.trace.emit(
            case_id=state.case_id,
            event_type="policy_decided",
            actor=self.name,
            target="coordinator",
            decision_code=state.issue.upper(),
            evidence_refs=policy_refs,
            attributes={
                "delivery_delta_days": self.delivery_delta_days(state),
                "policy_decision": state.policy_evaluation.decision,
                "policy_confidence": state.policy_evaluation.confidence,
            },
        )
        return rule

    def _lifecycle_events(
        self,
        state: CaseState,
        source: dict[str, Any],
        *,
        stop_at_case_open: bool = False,
    ) -> list[dict[str, Any]]:
        events = _as_rows(source.get("events", []))
        purchased = _datetime(state.order.get("order_purchase_timestamp"))
        if purchased is None:
            return events
        lower = purchased - timedelta(days=1)
        upper = purchased + timedelta(days=45)
        opened = _datetime(state.case.get("opened_at"))
        if stop_at_case_open and opened is not None:
            upper = min(upper, opened)
        return [
            event
            for event in events
            if (when := _datetime(event.get("event_at"))) and lower <= when <= upper
        ]

    def _classify(self, state: CaseState) -> str:
        if not state.order or not state.policy:
            return "insufficient_evidence"
        payment_events = self._lifecycle_events(
            state, state.payment, stop_at_case_open=True
        )
        refund_events = self._lifecycle_events(
            state, state.refund, stop_at_case_open=True
        )
        shipment_events = self._lifecycle_events(state, state.shipment)
        captured = [
            event for event in payment_events if event.get("event_type") == "captured"
        ]
        order_status = state.order.get("order_status")
        if order_status == "canceled" and captured:
            return "canceled_order_paid"
        if order_status == "unavailable" and captured:
            return "unavailable_order_paid"
        if any(event.get("status") == "failed" for event in refund_events):
            return "refund_failed"
        if any(event.get("status") == "pending" for event in refund_events):
            return "refund_pending"
        if any(
            event.get("event_type") == "reconciliation_mismatch"
            for event in payment_events
        ):
            return "payment_mismatch"

        delivered = _datetime(state.order.get("order_delivered_customer_date"))
        estimated = _datetime(state.order.get("order_estimated_delivery_date"))
        if delivered and estimated and delivered > estimated:
            carrier_at = _datetime(
                state.order.get("order_delivered_carrier_date")
            ) or _datetime(state.shipment.get("delivered_carrier_at"))
            shipping_limits = self._shipping_limits(state)

            # Attribute the delay from business timestamps rather than relying
            # on one synthetic event name and exact timestamp equality.
            if carrier_at and shipping_limits:
                if carrier_at > max(shipping_limits):
                    return "late_delivery_seller"
                return "late_delivery_logistics"

            late = next(
                (
                    event
                    for event in shipment_events
                    if "late" in str(event.get("event_type", "")).lower()
                    and event.get("actor") in {"seller", "logistics_provider"}
                ),
                None,
            )
            if late and late.get("actor") == "seller":
                return "late_delivery_seller"
            if late and late.get("actor") == "logistics_provider":
                return "late_delivery_logistics"
            return "late_delivery_logistics"

        payment_rows = _as_rows(state.payment.get("payments", []))
        payment_types = {row.get("payment_type") for row in payment_rows}
        if len(captured) >= 2 and len(payment_types - {None}) >= 2:
            actual = sum(
                (Decimal(str(event.get("amount_brl", "0"))) for event in captured),
                Decimal("0"),
            )
            expected = self._expected_order_total(state)
            return "valid_split_payment" if expected and actual == expected else "duplicate_charge"
        return "unsupported_claim"

    @staticmethod
    def _expected_order_total(state: CaseState) -> Decimal:
        purchased = _datetime(state.order.get("order_purchase_timestamp"))
        if purchased is None:
            return Decimal("0")
        rows = []
        for row in state.items:
            limit = _datetime(row.get("shipping_limit_date"))
            if limit and purchased <= limit <= purchased + timedelta(days=15):
                rows.append(row)
        return sum(
            (
                Decimal(str(row.get("price", "0")))
                + Decimal(str(row.get("freight_value", "0")))
                for row in rows
            ),
            Decimal("0"),
        )

    @staticmethod
    def _shipping_limits(state: CaseState) -> list[datetime]:
        purchased = _datetime(state.order.get("order_purchase_timestamp"))
        if purchased is None:
            return []
        candidates = list(state.items)
        candidates.extend(_as_rows(state.shipment.get("shipping_limits", [])))
        limits: list[datetime] = []
        for row in candidates:
            if row.get("order_id") not in {None, state.order_id}:
                continue
            limit = _datetime(row.get("shipping_limit_date"))
            if limit and purchased <= limit <= purchased + timedelta(days=15):
                limits.append(limit)
        return list(dict.fromkeys(limits))


@dataclass(frozen=True)
class PolicyEvaluation:
    """Deterministic policy facts kept internal to the public L3A contract."""

    decision: str
    rationale: str
    confidence: float
    is_delivered: bool
    is_late_delivery: bool
    is_outside_return_window: bool | None
    delivery_delta_days: int | None
    complaint_delta_days: int | None
    return_window_days: int


def evaluate_policy(state: CaseState) -> PolicyEvaluation:
    """Calculate dates and the policy verdict without delegating arithmetic to an LLM."""
    delivered_at = _datetime(state.order.get("order_delivered_customer_date"))
    estimated_at = _datetime(state.order.get("order_estimated_delivery_date"))
    complaint_at = next(
        (
            value
            for value in (
                _datetime(state.case.get("complaint_timestamp")),
                _datetime(state.case.get("created_at")),
                _datetime(state.case.get("opened_at")),
            )
            if value is not None
        ),
        None,
    )
    raw_window = state.policy.get("return_window_days", 7)
    try:
        return_window_days = max(1, int(raw_window))
    except (TypeError, ValueError):
        return_window_days = 7

    status = str(state.order.get("order_status", "")).lower()
    is_delivered = status == "delivered" or delivered_at is not None
    delivery_delta = (
        (delivered_at.date() - estimated_at.date()).days
        if delivered_at and estimated_at
        else None
    )
    complaint_delta = (
        (complaint_at.date() - delivered_at.date()).days
        if complaint_at and delivered_at
        else None
    )
    outside_window = (
        complaint_delta > return_window_days if complaint_delta is not None else None
    )
    is_late = bool(delivery_delta is not None and delivery_delta > 0)
    claims = {
        str(claim.get("topic", "")).lower()
        for claim in state.request.get("claims", [])
        if isinstance(claim, dict)
    }
    message = str(state.request.get("message", "")).lower()
    late_claim = any("late_delivery" in topic for topic in claims) or any(
        token in message for token in ("giao trễ", "giao chậm", "late delivery")
    )
    past_estimate = bool(complaint_at and estimated_at and complaint_at > estimated_at)
    not_delivered = status in {"shipping", "shipped", "in_transit", "unavailable"}

    if not state.order or not state.policy or complaint_at is None:
        return PolicyEvaluation(
            "reject",
            "Dữ liệu đơn hàng, chính sách hoặc thời điểm khiếu nại chưa đầy đủ; cần điều tra thêm.",
            0.70,
            is_delivered,
            is_late,
            outside_window,
            delivery_delta,
            complaint_delta,
            return_window_days,
        )
    if state.conflicts:
        decision = (
            "approve"
            if state.issue not in {"unsupported_claim", "insufficient_evidence"}
            else "reject"
        )
        return PolicyEvaluation(
            decision,
            "Các nguồn MCP có mâu thuẫn nhẹ; phán quyết được giữ thận trọng để chờ đối soát.",
            0.75,
            is_delivered,
            is_late,
            outside_window,
            delivery_delta,
            complaint_delta,
            return_window_days,
        )
    elif is_delivered and outside_window:
        return PolicyEvaluation(
            "reject",
            f"Khiếu nại được gửi {complaint_delta} ngày sau giao hàng, "
            f"vượt cửa sổ {return_window_days} ngày.",
            0.95,
            is_delivered,
            is_late,
            outside_window,
            delivery_delta,
            complaint_delta,
            return_window_days,
        )
    elif is_late and late_claim:
        return PolicyEvaluation(
            "approve",
            f"Đơn hàng được giao trễ {delivery_delta} ngày so với ngày dự kiến.",
            0.92,
            is_delivered,
            is_late,
            outside_window,
            delivery_delta,
            complaint_delta,
            return_window_days,
        )
    elif not_delivered and past_estimate:
        return PolicyEvaluation(
            "approve",
            "Đơn chưa được giao dù thời điểm khiếu nại đã quá ngày giao dự kiến.",
            0.94,
            is_delivered,
            is_late,
            outside_window,
            delivery_delta,
            complaint_delta,
            return_window_days,
        )
    elif state.issue == "unsupported_claim":
        return PolicyEvaluation(
            "reject",
            "Dữ liệu có thẩm quyền không xác nhận lỗi giao nhận, thanh toán hoặc hoàn tiền.",
            0.85,
            is_delivered,
            is_late,
            outside_window,
            delivery_delta,
            complaint_delta,
            return_window_days,
        )
    else:
        confidence = 0.75 if state.issue == "insufficient_evidence" else 0.90

    return PolicyEvaluation(
        (
            "approve"
            if state.issue not in {"unsupported_claim", "insufficient_evidence"}
            else "reject"
        ),
        f"Bằng chứng MCP và điều khoản chính sách xác nhận vấn đề {state.issue}.",
        confidence,
        is_delivered,
        is_late,
        outside_window,
        delivery_delta,
        complaint_delta,
        return_window_days,
    )


@dataclass(frozen=True)
class Verification:
    decision: str
    rationale: str
    confidence: float


def _llm_config() -> tuple[str, str, str, str, float]:
    provider = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
    if provider not in {"openrouter", "ollama"}:
        raise ValueError("LLM_PROVIDER must be 'openrouter' or 'ollama'")
    default_url = (
        "https://openrouter.ai/api/v1"
        if provider == "openrouter"
        else "http://localhost:11434/v1"
    )
    default_model = (
        "qwen/qwen-2.5-7b-instruct"
        if provider == "openrouter"
        else "qwen2.5:7b-instruct"
    )
    base_url = os.getenv("LLM_BASE_URL", default_url).strip().rstrip("/")
    model = os.getenv("LLM_MODEL", default_model).strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key and provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key and provider == "ollama":
        api_key = "ollama"
    timeout = max(0.5, float(os.getenv("LLM_TIMEOUT", "12.0")))
    return provider, base_url, api_key, model, timeout


def _get_llm_client(base_url: str, api_key: str, timeout: float) -> Any:
    global _LLM_CLIENT, _LLM_CLIENT_KEY
    if AsyncOpenAI is None:
        raise RuntimeError("openai package is unavailable")
    key = (base_url, api_key)
    if _LLM_CLIENT is None or key != _LLM_CLIENT_KEY:
        _LLM_CLIENT = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "missing-key",
            timeout=timeout,
            max_retries=0,
        )
        _LLM_CLIENT_KEY = key
    return _LLM_CLIENT


async def call_verifier_llm(summary_data: dict[str, Any]) -> dict[str, Any]:
    """Return strict verifier JSON or a safe deterministic fallback."""
    fallback = {
        "decision": str(summary_data["rule_decision"]),
        "rationale": str(summary_data["rule_rationale"]),
        "confidence": 0.80,
        "used_fallback": True,
    }
    try:
        provider, base_url, api_key, model, timeout = _llm_config()
        if provider == "openrouter" and not api_key:
            return fallback
        client = _get_llm_client(base_url, api_key, timeout)
        prompt = (
            "Verify the deterministic policy result below. Date and money values are "
            "already computed by Python. Return JSON only with decision (approve/reject), "
            "rationale (2 concise sentences), and confidence (0.75..0.95). Do not create "
            "IDs, evidence refs, dates, or amounts.\n"
            + json.dumps(summary_data, ensure_ascii=False, separators=(",", ":"))
        )
        response = await client.chat.completions.create(
            model=model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You are a strict e-commerce policy verifier.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or ""
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        value = json.loads(match.group(0) if match else content)
        decision = str(value.get("decision", "")).lower()
        rationale = str(value.get("rationale", "")).strip()
        confidence = max(0.75, min(0.95, float(value["confidence"])))
        if decision not in {"approve", "reject"} or not rationale:
            return fallback
        return {
            "decision": decision,
            "rationale": rationale[:320],
            "confidence": confidence,
            "used_fallback": False,
        }
    except Exception:
        return fallback


async def verifier_agent(
    evaluation: PolicyEvaluation,
    facts: dict[str, Any],
) -> Verification:
    """Verify rule output while keeping deterministic facts authoritative."""
    summary = {
        **facts,
        "rule_decision": evaluation.decision,
        "rule_rationale": evaluation.rationale,
        "rule_confidence": evaluation.confidence,
        "is_delivered": evaluation.is_delivered,
        "is_late_delivery": evaluation.is_late_delivery,
        "is_outside_return_window": evaluation.is_outside_return_window,
        "delivery_delta_days": evaluation.delivery_delta_days,
        "complaint_delta_days": evaluation.complaint_delta_days,
        "return_window_days": evaluation.return_window_days,
    }
    value = await call_verifier_llm(summary)
    decision = str(value["decision"])
    if decision != evaluation.decision:
        return Verification(
            evaluation.decision,
            evaluation.rationale,
            min(evaluation.confidence, 0.75),
        )
    confidence = float(value["confidence"])
    confidence = min(evaluation.confidence, confidence)
    return Verification(decision, str(value["rationale"]), round(confidence, 2))


class VerifierAgent:
    name = "verifier"

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def verify(
        self, state: CaseState, output: dict[str, Any]
    ) -> dict[str, Any]:
        self._check_invariants(state, output)
        evaluation = state.policy_evaluation
        if evaluation is None:
            raise ValueError("policy must be evaluated before verification")
        model_review = await verifier_agent(evaluation, self._facts(state, output))
        output["assessment"]["confidence"] = model_review.confidence
        for claim in output.get("claim_assessments", []):
            claim["confidence"] = min(float(claim["confidence"]), model_review.confidence)
        self._check_invariants(state, output)
        # The CLI validates again. Validating here makes VerifierAgent the final gate.
        self.gateway._contracts.validate_output(output, f"draft/{state.case_id}.json")
        self.trace.emit(
            case_id=state.case_id,
            event_type="verification_completed",
            actor=self.name,
            target="coordinator",
            decision_code=f"POLICY_{model_review.decision.upper()}",
            evidence_refs=output["evidence_refs"][:20],
            attributes={
                "confidence": model_review.confidence,
                "policy_decision": model_review.decision,
                "invariants_passed": True,
            },
        )
        return output

    @staticmethod
    def _check_invariants(state: CaseState, output: dict[str, Any]) -> None:
        if output.get("case_id") != state.case_id:
            raise ValueError("output case_id does not match the active case")
        known_refs = {item.evidence_ref for item in state.evidence.values()}
        output_refs = set(output.get("evidence_refs", []))
        if not output_refs or not output_refs <= known_refs:
            raise ValueError("output contains missing or cross-case evidence refs")
        required = ISSUE_TOOLS.get(state.issue, ())
        if any(tool not in state.evidence for tool in required):
            raise ValueError(f"missing required evidence for {state.issue}")
        financial = output["financial_resolution"]
        case_status = output["assessment"]["case_status"]
        confidence = float(output["assessment"]["confidence"])
        if not 0.70 <= confidence <= 0.95:
            raise ValueError("confidence is outside the calibrated 0.70..0.95 range")
        line_total = round(
            sum(float(line["amount_brl"]) for line in financial["refund_lines"]), 2
        )
        refund_total = round(float(financial["recommended_refund_brl"]), 2)
        if line_total != refund_total:
            raise ValueError("refund line total is inconsistent")
        if case_status == "no_action" and refund_total != 0.0:
            raise ValueError("no_action cannot recommend a positive refund")
        if state.issue in {"valid_split_payment", "unsupported_claim"} and refund_total != 0.0:
            raise ValueError(f"{state.issue} cannot recommend a positive refund")
        actions = output.get("resolution_actions", [])
        if len(actions) != len(set(actions)):
            raise ValueError("resolution_actions contains duplicates")
        if case_status == "no_action" and actions != ["document_no_action"]:
            raise ValueError("no_action must use document_no_action")

        parties = output["root_cause_analysis"]["responsible_parties"]
        party_types = {party["party_type"] for party in parties}
        expected_party = ISSUE_PARTY[state.issue]
        if expected_party not in party_types:
            raise ValueError(
                f"{state.issue} requires responsible party {expected_party}"
            )
        if state.issue == "late_delivery_seller" and "logistics_provider" in party_types:
            raise ValueError("seller lateness cannot be assigned to logistics_provider")
        if state.issue == "late_delivery_logistics" and "seller" in party_types:
            raise ValueError("logistics lateness cannot be assigned to seller")
        for claim in output.get("claim_assessments", []):
            if not set(claim["evidence_refs"]) <= output_refs:
                raise ValueError("claim references evidence outside the output scope")

    @staticmethod
    def _facts(state: CaseState, output: dict[str, Any]) -> dict[str, Any]:
        return {
            "primary_issue": state.issue,
            "order_status": state.order.get("order_status"),
            "delivered_at": state.order.get("order_delivered_customer_date"),
            "estimated_at": state.order.get("order_estimated_delivery_date"),
            "complaint_at": state.case.get("complaint_timestamp")
            or state.case.get("created_at")
            or state.case.get("opened_at"),
            "case_status": output["assessment"]["case_status"],
            "refund_brl": output["financial_resolution"]["recommended_refund_brl"],
            "actions": output["resolution_actions"],
            "evidence_domains": sorted(
                {item.domain for item in state.evidence.values()}
            ),
        }


class Coordinator:
    name = "coordinator"

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace
        self.order_agent = OrderAgent(gateway, trace)
        self.payment_agent = PaymentAgent(gateway, trace)
        self.shipment_agent = ShipmentAgent(gateway, trace)
        self.policy_agent = PolicyAgent(gateway, trace)
        self.verifier = VerifierAgent(gateway, trace)

    async def solve(self, case: dict[str, Any]) -> dict[str, Any]:
        state = CaseState(case=case)
        if not state.order_id:
            raise ValueError(f"{state.case_id}: claimed_order_id is required for MCP scope")
        agents = (
            self.order_agent,
            self.payment_agent,
            self.shipment_agent,
            self.policy_agent,
        )
        for agent in agents:
            self.trace.emit(
                case_id=state.case_id,
                event_type="task_assigned",
                actor=self.name,
                target=agent.name,
                decision_code="DOMAIN_INVESTIGATION",
                attributes={"allowed_tools": ",".join(agent.tools)},
            )

        # Keep one MCP session deterministic and avoid connector concurrency hazards.
        await self.order_agent.investigate(state)
        await self.payment_agent.investigate(state)
        await self.shipment_agent.investigate(state)
        await self.policy_agent.investigate(state)
        rule = self.policy_agent.decide(state)
        rule = self._sanitize_rule(state, rule)
        draft = self._build_output(state, rule)
        return await self.verifier.verify(state, draft)

    @staticmethod
    def _sanitize_rule(state: CaseState, rule: dict[str, Any]) -> dict[str, Any]:
        """Make a policy rule internally consistent before building the output.

        Responsibility still describes the proven root cause. A rejected remedy
        therefore must not rewrite a seller/carrier fault as a customer fault.
        """
        sanitized = dict(rule)
        evaluation = state.policy_evaluation
        rejected_remedy = bool(
            evaluation
            and (
                evaluation.is_outside_return_window is True
                or evaluation.decision == "reject"
            )
        )
        has_positive_refund = float(sanitized.get("refund_brl", 0.0)) > 0.0
        if rejected_remedy and (
            has_positive_refund or sanitized.get("case_status") == "action_required"
        ):
            sanitized.update(
                case_status="no_action",
                recommended_action="document_no_action",
                refund_brl=0.0,
            )
        return sanitized

    def _build_output(
        self, state: CaseState, rule: dict[str, Any]
    ) -> dict[str, Any]:
        support_tools = ISSUE_TOOLS.get(state.issue, tuple(state.evidence))
        refs = state.refs_for(list(support_tools))
        item_rows = [row for row in state.items if row.get("order_id") == state.order_id]
        item_ids = _unique([str(row.get("order_item_id", "")) for row in item_rows])
        seller_ids = _unique([str(row.get("seller_id", "")) for row in item_rows])
        payment_rows = _as_rows(state.payment.get("payments", []))
        payment_refs = _unique(
            [
                f"{row.get('payment_type')}:{row.get('payment_sequential')}"
                for row in payment_rows
                if row.get("payment_type") and row.get("payment_sequential")
            ]
        )[:20]
        shipment_id = (
            state.shipment.get("shipment_id")
            or state.shipment.get("package_id")
            or state.shipment.get("tracking_number")
        )
        shipment_ids = [str(shipment_id)] if shipment_id else []
        refund_amount = round(float(rule.get("refund_brl", 0.0)), 2)
        action = str(rule.get("recommended_action", "collect_additional_evidence"))
        refund_lines = (
            [
                {
                    "reason_code": state.issue.upper(),
                    "amount_brl": refund_amount,
                    "entity_id": state.order_id,
                }
            ]
            if refund_amount > 0
            else []
        )
        claim_assessments = [
            self._assess_claim(
                state,
                claim,
                self._filter_claim_refs(state, claim.get("topic")),
                refund_amount,
                action,
            )
            for claim in state.request.get("claims", [])
            if isinstance(claim, dict) and claim.get("claim_id")
        ]
        refs = _unique(
            refs
            + [
                ref
                for assessment in claim_assessments
                for ref in assessment["evidence_refs"]
            ]
        )
        responsible = rule.get("responsible_parties", [])
        if not isinstance(responsible, list):
            responsible = [{"party_type": "unknown", "party_id": None}]
        calibrated_confidence = (
            state.policy_evaluation.confidence
            if state.policy_evaluation is not None
            else 0.70
        )
        return {
            "schema_version": "day09-l3a-output-v2",
            "case_id": state.case_id,
            "assessment": {
                "primary_issue": state.issue,
                "case_status": str(rule.get("case_status", "needs_investigation")),
                "confidence": calibrated_confidence,
            },
            "affected_entities": {
                "order_ids": [state.order_id],
                "item_ids": item_ids[:20],
                "seller_ids": seller_ids[:20],
                "payment_references": payment_refs,
                "shipment_ids": shipment_ids,
            },
            "claim_assessments": claim_assessments[:5],
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": state.issue.upper(), "rank": 1}],
                "responsible_parties": responsible[:5],
            },
            "evidence_refs": refs[:30],
            "data_conflicts": state.conflicts[:5],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": refund_amount,
                "refund_lines": refund_lines,
            },
            "resolution_actions": [action],
        }

    @staticmethod
    def _topic_matches_issue(topic: Any, issue: str) -> bool:
        normalized = str(topic or "").strip().lower()
        if normalized == issue:
            return True
        if any(token in normalized for token in ("late", "delay", "delivery")):
            return issue.startswith("late_delivery_")
        if any(token in normalized for token in ("payment", "charge")):
            return issue in {
                "payment_mismatch",
                "duplicate_charge",
                "valid_split_payment",
                "canceled_order_paid",
                "unavailable_order_paid",
            }
        if "refund" in normalized:
            return issue in {
                "refund_pending",
                "refund_failed",
                "canceled_order_paid",
            }
        if "cancel" in normalized:
            return issue in {"canceled_order_paid", "unavailable_order_paid"}
        return False

    @staticmethod
    def _filter_claim_refs(state: CaseState, topic: Any) -> list[str]:
        normalized = str(topic or "").strip().lower()
        if normalized == "requested_full_refund":
            # Entitlement depends on the proven root issue plus policy, not only
            # on the word "refund" in the customer's requested remedy.
            tools = ISSUE_TOOLS.get(state.issue, ("get_order", "get_policy"))
        elif any(
            token in normalized for token in ("late", "delay", "ship", "delivery")
        ):
            tools = (
                "get_order",
                "get_order_items",
                "get_shipment_summary",
            )
        elif any(token in normalized for token in ("pay", "payment", "charge")):
            tools = (
                "get_order",
                "get_order_items",
                "get_payment_timeline",
            )
        elif "refund" in normalized:
            tools = (
                "get_order",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_policy",
            )
        elif "cancel" in normalized:
            tools = ("get_order", "get_payment_timeline", "get_policy")
        else:
            tools = ("get_order", "get_policy")
        refs = state.refs_for(list(tools))
        return refs or state.refs_for(["get_order", "get_policy"])

    @staticmethod
    def _assess_claim(
        state: CaseState,
        claim: dict[str, Any],
        refs: list[str],
        refund_amount: float,
        action: str,
    ) -> dict[str, Any]:
        topic = claim.get("topic")
        if state.issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            confidence = 0.70
        elif Coordinator._topic_matches_issue(topic, state.issue):
            verdict = "supported"
            confidence = 0.92
        elif topic == "requested_full_refund" and refund_amount > 0:
            verdict = (
                "supported"
                if action in {"issue_refund", "retry_refund"}
                else "partially_supported"
            )
            confidence = 0.90
        else:
            verdict = "unsupported"
            confidence = 0.85
        return {
            "claim_id": str(claim["claim_id"]),
            "verdict": verdict,
            "confidence": confidence,
            "evidence_refs": refs[:10],
        }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the bounded multi-agent state machine for exactly one case."""
    return await Coordinator(gateway, trace).solve(case)
