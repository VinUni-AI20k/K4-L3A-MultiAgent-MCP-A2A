from __future__ import annotations

import logging
from typing import Any

from ..contracts import Contracts

logger = logging.getLogger(__name__)


class Verifier:
    """Chốt chặn thẩm định: Kiểm tra JSON Schema, tính nhất quán (Consistency) và Hard Gates."""

    def __init__(self, contracts: Contracts) -> None:
        self.contracts = contracts

    def verify(self, output: dict[str, Any]) -> dict[str, Any]:
        # 1. Kiểm tra tính nhất quán tài chính (Consistency)
        fin = output.get("financial_resolution", {})
        total_refund = float(fin.get("recommended_refund_brl", 0.0))
        lines = fin.get("refund_lines", [])
        sum_lines = round(sum(float(line.get("amount_brl", 0.0)) for line in lines), 2)

        if abs(total_refund - sum_lines) > 0.01:
            logger.warning(f"Adjusting refund lines total: {total_refund} vs sum {sum_lines}")
            fin["recommended_refund_brl"] = sum_lines

        # 2. Nếu status là no_action thì bắt buộc tiền hoàn = 0
        assessment = output.get("assessment", {})
        if assessment.get("case_status") == "no_action":
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []

        # 3. Lọc unique cho resolution_actions và evidence_refs
        output["resolution_actions"] = list(dict.fromkeys(output.get("resolution_actions", [])))
        output["evidence_refs"] = list(dict.fromkeys(output.get("evidence_refs", [])))

        # 4. Thẩm định qua JSON Schema chuẩn của Day09
        self.contracts.validate_output(output, f"Case {output.get('case_id')}")
        return output
