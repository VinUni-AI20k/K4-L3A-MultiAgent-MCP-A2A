from __future__ import annotations

from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


class OrderAgent:
    """Order & Claims Specialist Agent
    Chuyên môn: Bóc tách khiếu nại khách hàng, Điều tra Đơn hàng & Mặt hàng
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def run(self, case: dict[str, Any], state: dict[str, Any]) -> None:
        case_id = case["case_id"]
        customer_request = case["customer_request"]
        claimed_order_id = customer_request.get("claimed_order_id")

        if not claimed_order_id:
            return

        # Khởi tạo các cấu trúc dữ liệu nếu chưa có
        state.setdefault("affected_entities", {})
        state["affected_entities"].setdefault("order_ids", set())
        state["affected_entities"].setdefault("item_ids", set())
        state.setdefault("evidence_refs", [])
        state.setdefault("claim_assessments", [])

        # Thêm order_id vào danh sách ảnh hưởng
        state["affected_entities"]["order_ids"].add(claimed_order_id)

        # 1. Thu thập bằng chứng Đơn hàng
        order_evidence = None
        try:
            order_evidence = await self.gateway.call("get_order", case_id=case_id, order_id=claimed_order_id)
            state["order_data"] = order_evidence["data"]
            state["evidence_refs"].append(order_evidence["evidence_ref"])
            
            # Ghi lại dấu vết (trace)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order-agent",
                tool_name="get_order",
                evidence_refs=[order_evidence["evidence_ref"]]
            )
        except Exception as e:
            print(f"[OrderAgent] Error fetching order {claimed_order_id}: {e}")

        # 2. Thu thập bằng chứng Mặt hàng
        items_evidence = None
        try:
            items_evidence = await self.gateway.call("get_order_items", case_id=case_id, order_id=claimed_order_id)
            state["items_data"] = items_evidence["data"]
            state["evidence_refs"].append(items_evidence["evidence_ref"])
            
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order-agent",
                tool_name="get_order_items",
                evidence_refs=[items_evidence["evidence_ref"]]
            )
            
            # Thêm các item_id vào danh sách ảnh hưởng
            for item in items_evidence["data"]:
                if "order_item_id" in item:
                    state["affected_entities"]["item_ids"].add(str(item["order_item_id"]))
                
                # Gọi get_product_context cho từng product
                if "product_id" in item:
                    product_id = item["product_id"]
                    try:
                        product_evidence = await self.gateway.call("get_product_context", case_id=case_id, product_id=product_id)
                        state.setdefault("products_data", {})[product_id] = product_evidence["data"]
                        state["evidence_refs"].append(product_evidence["evidence_ref"])
                        self.trace.emit(
                            case_id=case_id,
                            event_type="tool_result_consumed",
                            actor="order-agent",
                            tool_name="get_product_context",
                            evidence_refs=[product_evidence["evidence_ref"]]
                        )
                    except Exception as e:
                        print(f"[OrderAgent] Error fetching product context for {product_id}: {e}")

        except Exception as e:
            print(f"[OrderAgent] Error fetching items for order {claimed_order_id}: {e}")

        # 3. Thu thập lịch sử khách hàng (customer_history)
        if order_evidence and "data" in order_evidence and "customer_id" in order_evidence["data"]:
            customer_id = order_evidence["data"]["customer_id"]
            try:
                customer_evidence = await self.gateway.call("get_customer_history", case_id=case_id, customer_id=customer_id)
                state["customer_data"] = customer_evidence["data"]
                state["evidence_refs"].append(customer_evidence["evidence_ref"])
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="order-agent",
                    tool_name="get_customer_history",
                    evidence_refs=[customer_evidence["evidence_ref"]]
                )
            except Exception as e:
                print(f"[OrderAgent] Error fetching customer history for {customer_id}: {e}")

        # 4. Phân tích yêu cầu và Đánh giá các vấn đề nghiệp vụ
        claims = customer_request.get("claims", [])
        
        # Danh sách các claim topics thuộc về OrderAgent
        order_topics = ["canceled_order_paid", "unavailable_order_paid", "unsupported_claim"]

        for claim in claims:
            topic = claim["topic"]
            if topic not in order_topics:
                # Các khiếu nại khác sẽ do Agent khác xử lý (Payment, Logistics...)
                continue

            claim_id = claim["claim_id"]
            verdict = "insufficient_evidence"
            confidence = 0.0
            evidence_refs = []

            # Sử dụng bằng chứng order nếu có
            if order_evidence:
                evidence_refs.append(order_evidence["evidence_ref"])
                order_status = order_evidence["data"].get("order_status")

                if topic == "canceled_order_paid":
                    if order_status == "canceled":
                        verdict = "supported"
                        confidence = 1.0
                    else:
                        verdict = "unsupported"
                        confidence = 1.0
                
                elif topic == "unavailable_order_paid":
                    if order_status == "unavailable":
                        verdict = "supported"
                        confidence = 1.0
                    else:
                        verdict = "unsupported"
                        confidence = 1.0
                        
                elif topic == "unsupported_claim":
                    # Mặc định đánh giá unsupported_claim nếu dữ liệu không khớp
                    # Tùy thuộc vào business logic bổ sung
                    verdict = "unsupported"
                    confidence = 1.0

            # Lưu đánh giá
            state["claim_assessments"].append({
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs
            })
