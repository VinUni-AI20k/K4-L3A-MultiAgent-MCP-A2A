from __future__ import annotations

from typing import Any
import os
import json
import asyncio

from google import genai
from google.genai import types

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .models import L3AOutput

async def fetch_evidence(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    tool_name: str,
    actor_name: str,
    **kwargs
) -> dict:
    """Wrapper to call MCP tool and emit trace event safely."""
    evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
    
    evidence_ref = evidence.get("evidence_ref")
    data = evidence.get("data", {})
    
    # EMIT TRACE EVENT (Rule #4)
    if evidence_ref:
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor_name,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
    
    return {"ref": evidence_ref, "data": data}

async def solve_case(
    case: dict, gateway: EvidenceGateway, trace: TraceWriter
) -> dict:
    """Implement the L3A coordinator and specialist-agent workflow here."""
    case_id = case.get("case_id", "UNKNOWN")
    customer_req = case.get("customer_request", {})
    customer_message = customer_req.get("message", "")
    order_id = customer_req.get("claimed_order_id")
    
    print(f"\\n[Coordinator] Analyzing case {case_id}")
    print(f"Message: {customer_message}")
    
    if not os.environ.get("MISTRAL_API_KEY"):
        raise ValueError("MISTRAL_API_KEY environment variable is required.")
        
    import time
    
    mistral_model = "ministral-3b-2512"
    
    # 1. Coordinator: Extract order_id using LLM if not provided
    if not order_id:
        print("[Coordinator] Extracting entities...")
        extract_prompt = f"Trích xuất mã đơn hàng (order_id) từ tin nhắn sau của khách hàng. Trả về đúng định dạng JSON: \"{{\"order_id\": \"MÃ_ĐƠN_HÀNG\"}}\". Nếu không có, trả về \"{{\"order_id\": null}}\". Tin nhắn: {customer_message}"
        
        import httpx
        import asyncio
        extract_response = None
        headers = {"Authorization": f"Bearer {os.environ.get('MISTRAL_API_KEY')}", "Content-Type": "application/json"}
        payload = {"model": mistral_model, "messages": [{"role": "user", "content": extract_prompt}], "response_format": {"type": "json_object"}, "temperature": 0.0}
        
        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as hc:
                    resp = await hc.post("https://api.mistral.ai/v1/chat/completions", json=payload, headers=headers, timeout=30.0)
                    resp.raise_for_status()
                    extract_response = resp.json()
                break
            except Exception as e:
                print(f"[Coordinator] Mistral Error, retrying ({attempt+1}/3)... {e}")
                await asyncio.sleep(2)
                    
        if not extract_response:
            raise RuntimeError("Mistral failed after 3 retries.")
            
        extracted = json.loads(extract_response["choices"][0]["message"]["content"])
        order_id = extracted.get("order_id")
        
    if not order_id:
        print("[Coordinator] No order_id found. Fallback to empty string.")
        order_id = ""
        
    print(f"[Coordinator] Found order_id: {order_id}")
    
    # 2. Specialist Agents collect evidence
    print("[Specialists] Gathering evidence...")
    evidence_refs = []
    gathered_data = {}
    
    # 2.1 Order Agent
    if order_id:
        try:
            order_ev = await fetch_evidence(gateway, trace, case_id, "get_order", "order_agent", order_id=order_id)
            gathered_data["order"] = order_ev["data"]
            evidence_refs.append(order_ev["ref"])
        except Exception as e:
            print(f"[Order Agent] Error: {e}")
            
        # 2.2 Payment Agent
        try:
            payment_ev = await fetch_evidence(gateway, trace, case_id, "get_order_payments", "payment_agent", order_id=order_id)
            gathered_data["payments"] = payment_ev["data"]
            evidence_refs.append(payment_ev["ref"])
        except Exception as e:
            print(f"[Payment Agent] Error: {e}")
            
        # 2.3 Shipment Agent
        try:
            shipment_ev = await fetch_evidence(gateway, trace, case_id, "get_shipment_summary", "shipment_agent", order_id=order_id)
            gathered_data["shipment"] = shipment_ev["data"]
            evidence_refs.append(shipment_ev["ref"])
        except Exception as e:
            print(f"[Shipment Agent] Error: {e}")
        
    # 2.4 Policy Agent
    try:
        policy_topic = "refund"
        claims = customer_req.get("claims", [])
        if claims:
            policy_topic = claims[0].get("topic", "refund")
            
        policy_version = case.get("policy_version", "EC_POLICY_V1")
        policy_ev = await fetch_evidence(gateway, trace, case_id, "get_policy", "policy_agent", topic=policy_topic, policy_version=policy_version)
        gathered_data["policy"] = policy_ev["data"]
        evidence_refs.append(policy_ev["ref"])
    except Exception as e:
        print(f"[Policy Agent] Error: {e}")
        
    # 3. Verifier / Policy Agent: Generate Output
    print("[Policy Agent] Analyzing evidence and generating resolution...")
    
    schema_json = json.dumps(L3AOutput.model_json_schema(), indent=2)
    
    analysis_prompt = f"""
    Bạn là một chuyên gia giải quyết khiếu nại thương mại điện tử.
    Dưới đây là thông tin khiếu nại của khách hàng và các bằng chứng đã thu thập được từ hệ thống:
    
    Case ID: {case_id}
    Tin nhắn: {customer_message}
    Order ID: {order_id}
    
    Bằng chứng hệ thống (Dữ liệu JSON):
    {json.dumps(gathered_data, indent=2)}
    
    Danh sách các Reference ID (bắt buộc sử dụng trong output, không được bịa thêm):
    {evidence_refs}
    
    Hãy phân tích nguyên nhân gốc rễ, và đưa ra quyết định giải quyết khiếu nại.
    Trường hợp lỗi do người bán hoặc nền tảng, hoàn tiền (currency: BRL) cho khách.
    Trường hợp lỗi từ khách hàng, có thể từ chối hoặc cần điều tra thêm.
    
    LƯU Ý QUAN TRỌNG:
    - CHỈ SỬ DỤNG CÁC EVIDENCE_REFS ĐƯỢC CUNG CẤP Ở TRÊN. KHÔNG TỰ BỊA RA EVIDENCE_REF NÀO KHÁC.
    - CÁC chuỗi string trong mảng resolution_actions KHÔNG ĐƯỢC DÀI QUÁ 80 KÝ TỰ! Phải viết thật ngắn gọn.
    - Mã nguyên nhân (cause_code) phải khớp format ^[A-Z][A-Z0-9_]{{2,79}}$ (ví dụ: SELLER_DELAY, LOGISTICS_LOST).
    - Output trả về CẦN PHẢI TUÂN THỦ CHÍNH XÁC SCHEMA SAU (trả về dưới dạng JSON hợp lệ):
    {schema_json}
    """
    
    final_response = None
    import httpx
    import asyncio
    headers = {"Authorization": f"Bearer {os.environ.get('MISTRAL_API_KEY')}", "Content-Type": "application/json"}
    payload = {"model": mistral_model, "messages": [{"role": "user", "content": analysis_prompt}], "response_format": {"type": "json_object"}, "temperature": 0.0}
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient() as hc:
                resp = await hc.post("https://api.mistral.ai/v1/chat/completions", json=payload, headers=headers, timeout=60.0)
                resp.raise_for_status()
                final_response = resp.json()
            break
        except Exception as e:
            print(f"[Policy Agent] Mistral Error, retrying ({attempt+1}/3)... {e}")
            await asyncio.sleep(2)
    
    if not final_response:
        raise RuntimeError("Mistral failed after 3 retries.")
        
    output_dict = json.loads(final_response["choices"][0]["message"]["content"])
    
    # Force schema version
    output_dict["schema_version"] = "day09-l3a-output-v2"
    output_dict["case_id"] = case_id
    
    # 4. Final Verification Layer
    safe_refs = []
    for ref in output_dict.get("evidence_refs", []):
        if ref in evidence_refs:
            safe_refs.append(ref)
    output_dict["evidence_refs"] = safe_refs
    
    if "claim_assessments" in output_dict:
        if output_dict["claim_assessments"] is None:
            del output_dict["claim_assessments"]
        elif isinstance(output_dict["claim_assessments"], list):
            for claim in output_dict["claim_assessments"]:
                safe_claim_refs = [r for r in claim.get("evidence_refs", []) if r in evidence_refs]
                claim["evidence_refs"] = safe_claim_refs
                
    # Fallback for required assessment
    if "assessment" not in output_dict or not isinstance(output_dict.get("assessment"), dict):
        output_dict["assessment"] = {
            "primary_issue": "unsupported_claim",
            "case_status": "needs_investigation",
            "confidence": 0.5
        }
    else:
        ass = output_dict["assessment"]
        valid_issues = [
            "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
            "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
            "duplicate_charge", "refund_pending", "refund_failed",
            "unsupported_claim", "insufficient_evidence"
        ]
        if ass.get("primary_issue") not in valid_issues:
            ass["primary_issue"] = "unsupported_claim"
        if ass.get("case_status") not in ["action_required", "no_action", "needs_investigation"]:
            ass["case_status"] = "needs_investigation"
        if not isinstance(ass.get("confidence"), (int, float)):
            ass["confidence"] = 0.5

    if "$defs" in output_dict:
        del output_dict["$defs"]
        
    if "affected_entities" not in output_dict:
        output_dict["affected_entities"] = {}
        
    for key in ["order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids"]:
        if key not in output_dict["affected_entities"]:
            output_dict["affected_entities"][key] = []
            
    if "root_cause_analysis" in output_dict:
        for party in output_dict["root_cause_analysis"].get("responsible_parties", []):
            if "party_id" not in party:
                party["party_id"] = None
                
    if "data_conflicts" in output_dict:
        if output_dict["data_conflicts"] is None:
            output_dict["data_conflicts"] = []
        else:
            for dc in output_dict["data_conflicts"]:
                if "selected_source" not in dc:
                    dc["selected_source"] = None
                
    if "financial_resolution" in output_dict:
        if output_dict["financial_resolution"].get("refund_lines") is None:
            output_dict["financial_resolution"]["refund_lines"] = []
        for line in output_dict["financial_resolution"].get("refund_lines", []):
            if "entity_id" not in line:
                line["entity_id"] = None
                
    if output_dict.get("resolution_actions") is None:
        output_dict["resolution_actions"] = []
                
    print("[Verifier] Case completed successfully.")
    return output_dict


