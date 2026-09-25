"""Tool discovery and evidence bookkeeping shared by every specialist agent.

Tool names and argument names are never hard-coded: they are learned at
runtime from ``session.list_tools()`` (see README Sec 4, "dung tool discovery,
khong doan ten tool"). This module only encodes how to *route* a discovered
tool to the domain-owning agent and how to *dedupe* the evidence it returns.

The live "l3a" MCP tool profile is order-centric: `get_order`, `get_order_items`,
`get_order_payments`, `get_payment_timeline`, `get_product_context`,
`get_refund_timeline`, `get_sellers` and `get_shipment_summary` all key off the
*same* `order_id` -- there is no separate item_id/payment_reference/shipment_id
argument. Only `get_customer_history` (customer_unique_id) and `get_policy`
(policy_version) key off something else. So evidence lookup here is resolved
by matching each tool's own required parameter *name* (discovered from its
JSON Schema) against a pool of known identifier values pulled from the case --
never by assuming a domain owns "its own" id.
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

# Case-JSON key -> canonical MCP tool parameter name. Used only to seed which
# identifier values are worth trying; it never invents a value not present in
# the case file. Both singular and plural spellings are recognised since real
# case files are not guaranteed to match either convention exactly.
_KEY_TO_PARAM = {
    "order_id": "order_id",
    "order_ids": "order_id",
    "claimed_order_id": "order_id",
    "customer_id": "customer_unique_id",
    "customer_ids": "customer_unique_id",
    "customer_unique_id": "customer_unique_id",
    "policy_version": "policy_version",
    "seller_id": "seller_id",
    "seller_ids": "seller_id",
    "item_id": "item_id",
    "item_ids": "item_id",
    "order_item_id": "item_id",
    "payment_id": "payment_id",
    "payment_ids": "payment_id",
    "payment_reference": "payment_reference",
    "payment_references": "payment_reference",
    "shipment_id": "shipment_id",
    "shipment_ids": "shipment_id",
    "tracking_id": "shipment_id",
    "refund_id": "refund_id",
    "refund_ids": "refund_id",
    "product_id": "product_id",
    "product_ids": "product_id",
}


def _walk_for_ids(node: Any, buckets: dict[str, set[str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            param = _KEY_TO_PARAM.get(key)
            if param is not None:
                if isinstance(value, str) and value:
                    buckets.setdefault(param, set()).add(value)
                elif isinstance(value, list):
                    buckets.setdefault(param, set()).update(
                        v for v in value if isinstance(v, str) and v
                    )
            _walk_for_ids(value, buckets)
    elif isinstance(node, list):
        for item in node:
            _walk_for_ids(item, buckets)


def extract_known_ids(case: dict[str, Any]) -> dict[str, set[str]]:
    """Walk the case JSON and bucket every recognised identifier by the
    canonical MCP parameter name it fills (e.g. "order_id", "policy_version").
    """
    buckets: dict[str, set[str]] = {}
    _walk_for_ids(case, buckets)
    return buckets


def extract_linked_ids(data: Any) -> dict[str, set[str]]:
    """Same walk as `extract_known_ids`, applied to an evidence payload so
    identifiers surfaced only in an MCP response (e.g. a customer_unique_id
    on the order record) can seed further lookups.
    """
    buckets: dict[str, set[str]] = {}
    _walk_for_ids(data, buckets)
    return buckets


def extract_claims(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Return up to 5 claim objects, matching the claimAssessment cap in the
    schema. Claims live under `customer_request.claims` in the real L3A case
    files; a top-level `claims` is also accepted for robustness.
    """
    customer_request = case.get("customer_request")
    claims = customer_request.get("claims") if isinstance(customer_request, dict) else None
    if not isinstance(claims, list):
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
    """Match a discovered tool name to the domain it primarily returns data
    for. Real tool names compose a qualifier with a head noun (e.g.
    "get_order_items" returns item data qualified by an order; "get_sellers"
    is simply plural) -- scanning tokens right-to-left and singularizing
    trailing "s" resolves both patterns without hard-coding any tool name.
    """
    tokens = _TOKEN_RE.split(tool_name.lower())
    for token in reversed(tokens):
        candidate = token[:-1] if token.endswith("s") and token[:-1] in DOMAINS else token
        if candidate in DOMAINS:
            return candidate
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


def resolve_calls(
    tools: list[ToolDescriptor], known_ids: dict[str, set[str]]
) -> list[tuple[ToolDescriptor, str, str]]:
    """Every (tool, id_param, entity_id) combination that can actually be
    called: each tool's own required id parameter (from its JSON Schema)
    matched against whatever values are known for that parameter name.
    """
    calls: list[tuple[ToolDescriptor, str, str]] = []
    for descriptor in tools:
        id_param = _id_argument_name(descriptor)
        if id_param is None:
            continue
        for entity_id in sorted(known_ids.get(id_param, set())):
            calls.append((descriptor, id_param, entity_id))
    return calls


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
    # (domain, id_param, entity_id) -- id_param is kept so a supplementary
    # retry (Coordinator._run_supplementary_task) knows which MCP parameter
    # to refill, instead of guessing from the domain alone.
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        self.items.append(item)

    def mark_unresolved(self, domain: str, id_param: str, entity_id: str) -> None:
        self.unresolved.append((domain, id_param, entity_id))

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
    id_param: str
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
    known_ids: dict[str, set[str]],
    tools: list[ToolDescriptor],
    bundle: EvidenceBundle,
    max_retries: int = 1,
) -> DomainFetchResult:
    """Call every discovered tool for one domain, each against whichever
    known identifier value fills *that tool's own* required parameter.

    Two distinct tools in the same domain (e.g. get_order_payments and
    get_payment_timeline both under "payment") return complementary facts,
    not alternative sources of the same fact -- so every (tool, known id)
    combination that resolves is called, not just the first success.

    Retries are limited (``max_retries``) and idempotent (evidence reads never
    mutate case state); only transport-level failures are retried, per
    ARCHITECTURE.md Sec 6. A not-found or exhausted-retry result is recorded
    as `unresolved` on the bundle -- never guessed at.

    Returns a per-domain summary (not just the raw items) so the caller --
    running concurrently with other domain fetches inside Coordinator's
    ``asyncio.gather`` -- can emit an accurate A2A handoff message without
    racing on shared bundle state.
    """
    if not tools:
        return DomainFetchResult(domain, [], "unavailable", DECISION_LOOKUP_UNAVAILABLE, 0)

    calls = resolve_calls(tools, known_ids)
    if not calls:
        return DomainFetchResult(domain, [], "unavailable", DECISION_LOOKUP_UNAVAILABLE, 0)

    async def fetch_one(descriptor: ToolDescriptor, id_param: str, entity_id: str) -> LookupOutcome:
        attempt = 0
        while True:
            attempt += 1
            try:
                evidence = await gateway.call(
                    descriptor.name, case_id=case_id, **{id_param: entity_id}
                )
            except ContractError:
                # Envelope failed the public MCP schema: never retry a
                # structurally invalid response, and never use its data.
                return LookupOutcome(
                    id_param, entity_id, None, "unavailable", DECISION_ENVELOPE_INVALID, attempt
                )
            except httpx2.TimeoutException:
                if attempt > max_retries:
                    return LookupOutcome(
                        id_param, entity_id, None, "unavailable", DECISION_TIMEOUT, attempt
                    )
                continue
            except httpx2.HTTPError:
                if attempt > max_retries:
                    return LookupOutcome(
                        id_param, entity_id, None, "unavailable", DECISION_UNAVAILABLE, attempt
                    )
                continue
            except (RuntimeError, ValueError) as exc:
                if "not found" in str(exc).lower():
                    return LookupOutcome(
                        id_param, entity_id, None, "not_found", DECISION_NOT_FOUND, attempt
                    )
                if attempt > max_retries:
                    return LookupOutcome(
                        id_param, entity_id, None, "unavailable", DECISION_UNAVAILABLE, attempt
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
            return LookupOutcome(id_param, entity_id, item, "completed", None, attempt)

    outcomes = await asyncio.gather(*(fetch_one(d, p, e) for d, p, e in calls))

    items: list[EvidenceItem] = []
    for outcome in outcomes:
        if outcome.item is not None:
            bundle.add(outcome.item)
            items.append(outcome.item)
        else:
            bundle.mark_unresolved(domain, outcome.id_param, outcome.entity_id)

    statuses = {outcome.status for outcome in outcomes}
    if statuses == {"completed"}:
        domain_status, decision_code = "completed", None
    elif "completed" not in statuses:
        # every call failed the same way (or a mix of not_found/unavailable) --
        # surface the most actionable single status: unavailable beats not_found.
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
