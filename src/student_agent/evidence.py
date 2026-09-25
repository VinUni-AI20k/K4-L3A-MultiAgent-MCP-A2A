"""Tool discovery and evidence bookkeeping shared by every specialist agent.

Tool names and argument names are never hard-coded: they are learned at
runtime from ``session.list_tools()`` (see README Sec 4, "dung tool discovery,
khong doan ten tool"). This module only encodes how to *route* a discovered
tool to the domain-owning agent and how to *dedupe* the evidence it returns.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import httpx2

from .contracts import ContractError
from .mcp_gateway import EvidenceGateway

DOMAINS = (
    "order",
    "item",
    "payment",
    "shipment",
    "seller",
    "customer",
    "product",
    "refund",
    "policy",
)

_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# Case-JSON key -> entity domain. Used only to seed which IDs are worth
# looking up; it never invents an ID that is not present in the case file.
_ID_KEY_TO_DOMAIN = {
    "order_id": "order",
    "order_ids": "order",
    "order_item_id": "item",
    "item_id": "item",
    "item_ids": "item",
    "seller_id": "seller",
    "seller_ids": "seller",
    "payment_id": "payment",
    "payment_ids": "payment",
    "payment_reference": "payment",
    "payment_references": "payment",
    "shipment_id": "shipment",
    "shipment_ids": "shipment",
    "tracking_id": "shipment",
    "customer_id": "customer",
    "customer_ids": "customer",
    "customer_unique_id": "customer",
    "product_id": "product",
    "product_ids": "product",
    "refund_id": "refund",
    "refund_ids": "refund",
}


def extract_seed_entities(case: dict[str, Any]) -> dict[str, set[str]]:
    """Walk the case JSON and bucket every *_id / *_ids value by domain."""
    buckets: dict[str, set[str]] = {domain: set() for domain in DOMAINS}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                domain = _ID_KEY_TO_DOMAIN.get(key)
                if domain is not None:
                    if isinstance(value, str) and value:
                        buckets[domain].add(value)
                    elif isinstance(value, list):
                        buckets[domain].update(v for v in value if isinstance(v, str) and v)
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(case)
    return buckets


def extract_claims(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Return up to 5 claim objects, matching the claimAssessment cap in the schema."""
    claims = case.get("claims")
    if not isinstance(claims, list):
        return []
    out = []
    for candidate in claims:
        if isinstance(candidate, dict) and isinstance(candidate.get("claim_id"), str):
            out.append(candidate)
        if len(out) == 5:
            break
    return out


def infer_domain(tool_name: str) -> str | None:
    tokens = set(_TOKEN_RE.split(tool_name.lower()))
    for domain in DOMAINS:
        if domain in tokens:
            return domain
    return None


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    domain: str | None
    required_params: tuple[str, ...]
    properties: tuple[str, ...]


async def discover_tools(gateway: EvidenceGateway) -> dict[str, list[ToolDescriptor]]:
    """List MCP tools once and group the descriptors by inferred domain."""
    by_domain: dict[str, list[ToolDescriptor]] = {domain: [] for domain in DOMAINS}
    for descriptor in await gateway.describe_tools():
        if descriptor.domain in by_domain:
            by_domain[descriptor.domain].append(descriptor)
    return by_domain


def _id_argument_name(descriptor: ToolDescriptor) -> str | None:
    required = [name for name in descriptor.required_params if name != "case_id"]
    if len(required) == 1:
        return required[0]
    candidates = (
        f"{descriptor.domain}_id",
        "id",
        "reference",
        f"{descriptor.domain}_reference",
    )
    for candidate in candidates:
        if candidate in descriptor.properties:
            return candidate
    return None


@dataclass
class EvidenceItem:
    domain: str
    entity_id: str
    tool_name: str
    evidence_ref: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass
class EvidenceBundle:
    """Accumulates validated MCP evidence across every specialist agent."""

    items: list[EvidenceItem] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)  # (domain, entity_id)

    def add(self, item: EvidenceItem) -> None:
        self.items.append(item)

    def mark_unresolved(self, domain: str, entity_id: str) -> None:
        self.unresolved.append((domain, entity_id))

    def by_domain(self, domain: str) -> list[EvidenceItem]:
        return [item for item in self.items if item.domain == domain]

    def refs(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            if item.evidence_ref not in seen:
                seen.append(item.evidence_ref)
        return seen

    def refs_for(self, domains: tuple[str, ...]) -> list[str]:
        return [item.evidence_ref for item in self.items if item.domain in domains]


# Failure-policy decision codes (ARCHITECTURE.md Sec 6). Assigned per lookup so
# the caller can surface a real trace decision_code instead of failing silently.
DECISION_NOT_FOUND = "EVIDENCE_NOT_FOUND"
DECISION_UNAVAILABLE = "MCP_UNAVAILABLE"
DECISION_TIMEOUT = "MCP_TIMEOUT"
DECISION_ENVELOPE_INVALID = "MCP_ENVELOPE_INVALID"
DECISION_LOOKUP_UNAVAILABLE = "LOOKUP_UNAVAILABLE"


@dataclass(frozen=True)
class LookupOutcome:
    entity_id: str
    item: EvidenceItem | None
    status: str  # "completed" | "not_found" | "unavailable"
    decision_code: str | None
    attempts: int


@dataclass(frozen=True)
class DomainFetchResult:
    domain: str
    items: list[EvidenceItem]
    status: str  # "completed" | "not_found" | "unavailable" | "insufficient_evidence"
    decision_code: str | None
    attempts: int


async def fetch_domain_evidence(
    gateway: EvidenceGateway,
    *,
    case_id: str,
    domain: str,
    entity_ids: set[str],
    tools: list[ToolDescriptor],
    bundle: EvidenceBundle,
    max_retries: int = 1,
) -> DomainFetchResult:
    """Call every discovered tool for one domain against every seed ID for that domain.

    Retries are limited (``max_retries``) and idempotent (evidence reads never
    mutate case state); only transport-level failures are retried, per
    ARCHITECTURE.md Sec 6. A not-found or exhausted-retry result is recorded
    as `unresolved` on the bundle -- never guessed at.

    Returns a per-domain summary (not just the raw items) so the caller --
    running concurrently with other domain fetches inside Coordinator's
    ``asyncio.gather`` -- can emit an accurate A2A handoff message without
    racing on shared bundle state.
    """
    if not entity_ids:
        return DomainFetchResult(domain, [], "completed", None, 0)
    if not tools:
        for entity_id in entity_ids:
            bundle.mark_unresolved(domain, entity_id)
        return DomainFetchResult(domain, [], "unavailable", DECISION_LOOKUP_UNAVAILABLE, 0)

    async def fetch_one(entity_id: str) -> LookupOutcome:
        id_params = [(d, _id_argument_name(d)) for d in tools]
        id_params = [(d, p) for d, p in id_params if p is not None]
        if not id_params:
            return LookupOutcome(entity_id, None, "unavailable", DECISION_LOOKUP_UNAVAILABLE, 0)

        total_attempts = 0
        for descriptor, id_param in id_params:
            attempt = 0
            while True:
                total_attempts += 1
                attempt += 1
                try:
                    evidence = await gateway.call(
                        descriptor.name, case_id=case_id, **{id_param: entity_id}
                    )
                except ContractError:
                    # Envelope failed the public MCP schema: never retry a
                    # structurally invalid response, and never use its data.
                    return LookupOutcome(
                        entity_id, None, "unavailable", DECISION_ENVELOPE_INVALID, total_attempts
                    )
                except httpx2.TimeoutException:
                    if attempt > max_retries:
                        return LookupOutcome(
                            entity_id, None, "unavailable", DECISION_TIMEOUT, total_attempts
                        )
                    continue
                except httpx2.HTTPError:
                    if attempt > max_retries:
                        return LookupOutcome(
                            entity_id, None, "unavailable", DECISION_UNAVAILABLE, total_attempts
                        )
                    continue
                except (RuntimeError, ValueError) as exc:
                    if "not found" in str(exc).lower():
                        break  # try the next candidate tool for this domain, if any
                    if attempt > max_retries:
                        return LookupOutcome(
                            entity_id, None, "unavailable", DECISION_UNAVAILABLE, total_attempts
                        )
                    continue
                item = EvidenceItem(
                    domain=domain,
                    entity_id=entity_id,
                    tool_name=descriptor.name,
                    evidence_ref=evidence["evidence_ref"],
                    data=evidence["data"],
                    warnings=tuple(evidence.get("warnings", ())),
                )
                return LookupOutcome(entity_id, item, "completed", None, total_attempts)
        return LookupOutcome(entity_id, None, "not_found", DECISION_NOT_FOUND, total_attempts)

    outcomes = await asyncio.gather(*(fetch_one(entity_id) for entity_id in sorted(entity_ids)))

    items: list[EvidenceItem] = []
    for outcome in outcomes:
        if outcome.item is not None:
            bundle.add(outcome.item)
            items.append(outcome.item)
        else:
            bundle.mark_unresolved(domain, outcome.entity_id)

    statuses = {outcome.status for outcome in outcomes}
    if statuses == {"completed"}:
        domain_status, decision_code = "completed", None
    elif "completed" not in statuses:
        # every id failed the same way (or a mix of not_found/unavailable) -- surface
        # the most actionable single status: unavailable beats not_found.
        domain_status = "unavailable" if "unavailable" in statuses else "not_found"
        decision_code = next(o.decision_code for o in outcomes if o.status == domain_status)
    else:
        domain_status, decision_code = "insufficient_evidence", None
        for outcome in outcomes:
            if outcome.decision_code is not None:
                decision_code = outcome.decision_code
                break

    return DomainFetchResult(
        domain, items, domain_status, decision_code, sum(o.attempts for o in outcomes)
    )
