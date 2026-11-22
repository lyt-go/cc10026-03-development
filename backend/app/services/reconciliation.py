"""账单差额复核领域服务：把用量汇总与调用审计请求记录逐条核对。

背景：用量计量只按 API、调用方、账期汇总，缺少从「用量汇总 ↔ 请求记录」
回查请求差额的视图。本服务补上这条核对链路：

跨模块数据流
-----------
- 消费 usage_billing 内存表（用量汇总，按 API/调用方/账期聚合）
- 消费 call_audit 内存表 + data/audit/ 共享文件（逐笔请求记录）
- 复核确认后回写 usage_billing（补记调用次数/数据行数/费用金额）
- 差额明细、补记结果持久化到 data/reconciliation/ 共享目录

核对规则
--------
1. 同一审计请求（audit_id）在整个复核周期内只能被一个差额占用，绝不重复出现。
2. 同一次请求先失败再重试时，日志里会有两个时间接近的候选，按调用方+API+
   请求参数归并为一个「逻辑请求」，并在候选里保留成功与超时凭据。
3. 逻辑请求同时存在成功与超时凭据时，必须查清两者关系：属于同一逻辑请求的
   重试链，只按一次成功调用计费，超时笔不重复计费。
4. 找不到可证实关联的超时请求，保留全部财务调查线索（候选、时间窗、缺口说明），
   状态维持「待查」，不参与补记。
5. 差额金额区分「待查金额」与「已核定金额」；复核人员确认后才进入已核定，
   补记时把已确认的调用关联（audit 记录编号）带到补记结果里。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 差额明细状态：待确认 → 已核定 → 已补记；关联不全的长期停留在待查。
DIFF_STATUS_PENDING = "待确认"
DIFF_STATUS_INVESTIGATING = "待查"
DIFF_STATUS_VERIFIED = "已核定"
DIFF_STATUS_RECORDED = "已补记"

# 同一次请求失败后重试，两次日志间隔的归并窗口（秒）。
RETRY_WINDOW_SECONDS = 120


class ReconciliationService(BaseModuleService):
    MODULE = "reconciliation"
    ENTITY = "差额明细"

    def __init__(self) -> None:
        # 差额明细：diff_id -> 明细对象
        self._diffs: dict[int, dict[str, Any]] = {}
        # 补记结果：supplement_id -> 补记对象
        self._supplements: dict[int, dict[str, Any]] = {}
        # 已被差额占用的审计请求 id，保证同一请求不在多笔差额里重复出现
        self._occupied_audit_ids: set[int] = set()

    # ==================================================================
    # 差额核对
    # ==================================================================

    def build_differences(
        self,
        billing_cycle: str,
        caller: str | None = None,
        api_name: str | None = None,
        unit_amount: float = 0.0,
    ) -> list[dict[str, Any]]:
        """按 API、调用方、账期汇总用量，并与请求记录核对生成差额明细。

        只对存在缺口（汇总调用次数 < 可证实请求数）的用量记录生成差额；
        已被其他差额占用的审计请求不会再次进入候选。
        """
        usage_rows = self._usage_rows(billing_cycle, caller, api_name)
        created: list[dict[str, Any]] = []

        for usage in usage_rows:
            billed_calls = self._to_int(usage.get("调用次数"))
            groups = self._group_logical_requests(
                api_name=str(usage.get("API名称", "")),
                caller=str(usage.get("调用方", "")),
                billing_cycle=billing_cycle,
            )

            # 差额 = 可证实的逻辑请求数 - 已计量调用次数（不小于 0）
            gap = max(len(groups) - billed_calls, 0)
            if gap <= 0:
                continue

            # 缺口从后往前取（越晚发生、越可能尚未入账），但状态排序保证
            # 证据充分的优先进入待确认。
            candidate_groups = groups[-gap:]
            candidate_groups.sort(
                key=lambda g: 0 if g["confidence"] == "明确" else 1
            )

            for group in candidate_groups:
                # 双保险：该逻辑请求里任一审计已被占用则跳过，杜绝重复关联。
                group_ids = [c["audit_id"] for c in group["candidates"]]
                if any(aid in self._occupied_audit_ids for aid in group_ids):
                    continue
                diff = self._create_diff(usage, group, unit_amount)
                created.append(diff)
                self._occupied_audit_ids.update(group_ids)

        return created

    def _create_diff(
        self,
        usage: dict[str, Any],
        group: dict[str, Any],
        unit_amount: float,
    ) -> dict[str, Any]:
        diff_id = max(self._diffs, default=0) + 1
        has_success = group["has_success"]
        has_timeout = group["has_timeout"]
        clear = group["confidence"] == "明确"

        if has_success and has_timeout:
            relation_note = (
                "同一逻辑请求存在成功与超时凭据，已查清为重试链："
                "首笔失败/超时后重试成功，仅成功笔计费一次，超时笔不重复计费"
            )
        elif has_success:
            relation_note = "有明确成功响应证据，可直接核定"
        else:
            relation_note = (
                "仅存在超时/失败日志且找不到可证实的成功关联，"
                "保留候选与时间窗供财务继续追查，暂不参与计费"
            )

        diff = {
            "diff_id": diff_id,
            "diff_no": f"DIFF-{diff_id:04d}",
            "usage_id": usage.get("id"),
            "record_no": usage.get("记录编号"),
            "caller": usage.get("调用方"),
            "api_name": usage.get("API名称"),
            "billing_cycle": usage.get("计费周期"),
            "logical_request_key": group["key"],
            "candidate_audits": group["candidates"],
            "candidate_count": len(group["candidates"]),
            "evidence": group["evidence"],
            "confidence": group["confidence"],
            "retry_chain": group["retry_chain"],
            "relation_note": relation_note,
            "has_success_evidence": has_success,
            "has_timeout_evidence": has_timeout,
            # 计量口径：待查与已核定金额分开
            "estimated_amount": unit_amount,
            "verified_amount": 0.0,
            "call_count": 1,
            "data_rows": group["result_rows"],
            "status": DIFF_STATUS_PENDING if clear else DIFF_STATUS_INVESTIGATING,
            "confirmed_audit_ids": [],
            "confirmed_record_nos": [],
            "reviewer": "",
            "remark": "",
            "supplement_id": None,
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
        }
        self._diffs[diff_id] = diff
        self._persist_diff(diff)
        return diff

    # ==================================================================
    # 逻辑请求归并（失败 + 重试）
    # ==================================================================

    def _group_logical_requests(
        self,
        api_name: str,
        caller: str,
        billing_cycle: str,
    ) -> list[dict[str, Any]]:
        """把同一调用方/API/参数、时间接近的审计归并为一个逻辑请求。"""
        audits = [
            row for row in store.rows("call_audit")
            if str(row.get("目标API", "")) == api_name
            and str(row.get("调用方", "")) == caller
            and str(row.get("时间戳", ""))[:7] == billing_cycle
        ]
        # 按时间排序，保证重试候选时间接近且有序
        audits.sort(key=lambda r: str(r.get("时间戳", "")))

        groups: list[dict[str, Any]] = []
        for audit in audits:
            candidate = self._audit_candidate(audit)
            placed = False
            for group in groups:
                if group["key"] != candidate["key"]:
                    continue
                last_time = group["candidates"][-1]["timestamp"]
                if self._within_retry_window(last_time, candidate["timestamp"]):
                    group["candidates"].append(candidate)
                    placed = True
                    break
            if not placed:
                groups.append({
                    "key": candidate["key"],
                    "candidates": [candidate],
                })

        for group in groups:
            self._annotate_group(group)
        return groups

    def _audit_candidate(self, audit: dict[str, Any]) -> dict[str, Any]:
        code = str(audit.get("响应码", "")).strip()
        return {
            "key": "|".join([
                str(audit.get("调用方", "")),
                str(audit.get("目标API", "")),
                str(audit.get("请求参数", "")),
            ]),
            "audit_id": int(audit.get("id", 0)),
            "record_no": audit.get("记录编号"),
            "response_code": code,
            "latency_ms": self._to_float(audit.get("响应耗时")),
            "result_rows": self._to_int(audit.get("返回行数")),
            "timestamp": str(audit.get("时间戳", "")),
            "is_success": self._is_success_code(code),
            "is_timeout": self._is_timeout_code(code),
        }

    def _annotate_group(self, group: dict[str, Any]) -> None:
        candidates = group["candidates"]
        codes = [c["response_code"] for c in candidates]
        group["has_success"] = any(c["is_success"] for c in candidates)
        group["has_timeout"] = any(c["is_timeout"] for c in candidates)
        group["retry_chain"] = len(candidates) > 1
        # 成功笔的返回行数作为该逻辑请求的可计费数据行数
        success = next((c for c in candidates if c["is_success"]), None)
        group["result_rows"] = success["result_rows"] if success else 0

        if group["has_success"]:
            group["confidence"] = "明确"
            group["evidence"] = "成功响应码 " + ",".join(
                c["response_code"] for c in candidates if c["is_success"]
            )
        else:
            group["confidence"] = "关联不全"
            group["evidence"] = "仅超时/失败凭据：" + ",".join(codes)

    # ==================================================================
    # 复核确认与补记
    # ==================================================================

    def confirm_diff(
        self,
        diff_id: int,
        reviewer: str = "复核人员",
        remark: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """复核确认一笔差额。关联仍不明确（待查）的差额禁止确认。"""
        diff = self._diffs.get(diff_id)
        if diff is None:
            return None, f"差额明细 {diff_id} 不存在"
        if diff["status"] in (DIFF_STATUS_VERIFIED, DIFF_STATUS_RECORDED):
            return None, f"差额 {diff['diff_no']} 已核定，请勿重复确认"
        if diff["confidence"] != "明确":
            return (
                None,
                f"差额 {diff['diff_no']} 关联仍不明确，保留调查线索继续追查，"
                "不得在证据不足时核定",
            )

        # 只把成功凭据带进已确认关联；超时/失败笔不参与计费
        confirmed = [
            c for c in diff["candidate_audits"] if c["is_success"]
        ] or diff["candidate_audits"]
        diff["confirmed_audit_ids"] = [c["audit_id"] for c in confirmed]
        diff["confirmed_record_nos"] = [c["record_no"] for c in confirmed]
        diff["verified_amount"] = float(diff["estimated_amount"])
        diff["status"] = DIFF_STATUS_VERIFIED
        diff["reviewer"] = reviewer
        diff["remark"] = remark or diff["relation_note"]
        diff["updated_at"] = self._now_iso()
        self._persist_diff(diff)
        return diff, f"差额 {diff['diff_no']} 已核定，可发起补记"

    def supplement_diff(self, diff_id: int) -> tuple[dict[str, Any] | None, str]:
        """把已核定差额补记进用量计量，并生成带调用关联的补记结果。"""
        diff = self._diffs.get(diff_id)
        if diff is None:
            return None, f"差额明细 {diff_id} 不存在"
        if diff["status"] == DIFF_STATUS_RECORDED:
            return None, f"差额 {diff['diff_no']} 已补记，不能重复补记"
        if diff["status"] != DIFF_STATUS_VERIFIED:
            return None, f"差额 {diff['diff_no']} 尚未核定，不能补记"

        usage = store.find("usage_billing", int(diff["usage_id"]))
        if usage is None:
            return None, f"关联用量记录 {diff['record_no']} 不存在，无法补记"

        old_calls = self._to_int(usage.get("调用次数"))
        old_rows = self._to_int(usage.get("数据行数"))
        old_amount = self._to_float(usage.get("费用金额"))
        usage["调用次数"] = str(old_calls + int(diff["call_count"]))
        usage["数据行数"] = str(old_rows + int(diff["data_rows"]))
        usage["费用金额"] = round(old_amount + float(diff["verified_amount"]), 2)

        supplement_id = max(self._supplements, default=0) + 1
        supplement = {
            "supplement_id": supplement_id,
            "supplement_no": f"SUPP-{supplement_id:04d}",
            "diff_id": diff_id,
            "diff_no": diff["diff_no"],
            "usage_id": usage.get("id"),
            "record_no": usage.get("记录编号"),
            "caller": usage.get("调用方"),
            "api_name": usage.get("API名称"),
            "billing_cycle": usage.get("计费周期"),
            # 已确认的调用关联原样带入补记结果
            "confirmed_audit_ids": list(diff["confirmed_audit_ids"]),
            "confirmed_record_nos": list(diff["confirmed_record_nos"]),
            "added_call_count": int(diff["call_count"]),
            "added_data_rows": int(diff["data_rows"]),
            "added_amount": float(diff["verified_amount"]),
            "status": "已补记",
            "created_at": self._now_iso(),
        }
        self._supplements[supplement_id] = supplement

        diff["status"] = DIFF_STATUS_RECORDED
        diff["supplement_id"] = supplement_id
        diff["updated_at"] = self._now_iso()
        self._persist_diff(diff)
        self._persist_supplement(supplement)
        self._write_usage_snapshot(usage)
        return supplement, f"差额 {diff['diff_no']} 已补记到 {usage.get('记录编号')}"

    # ==================================================================
    # 查询视图
    # ==================================================================

    def list_diffs(
        self,
        billing_cycle: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        diffs = list(self._diffs.values())
        if billing_cycle:
            diffs = [d for d in diffs if d["billing_cycle"] == billing_cycle]
        if status:
            diffs = [d for d in diffs if d["status"] == status]
        return sorted(diffs, key=lambda d: d["diff_id"])

    def list_supplements(self, billing_cycle: str | None = None) -> list[dict[str, Any]]:
        items = list(self._supplements.values())
        if billing_cycle:
            items = [s for s in items if s["billing_cycle"] == billing_cycle]
        return sorted(items, key=lambda s: s["supplement_id"])

    def reconciliation_summary(
        self, billing_cycle: str | None = None
    ) -> dict[str, Any]:
        """按现有账单展示核对项，并汇总待查/已核定金额口径。"""
        diffs = self.list_diffs(billing_cycle)
        supplements = self.list_supplements(billing_cycle)

        by_usage: dict[int, dict[str, Any]] = {}
        for diff in diffs:
            key = int(diff["usage_id"])
            bucket = by_usage.setdefault(key, {
                "usage_id": diff["usage_id"],
                "record_no": diff["record_no"],
                "caller": diff["caller"],
                "api_name": diff["api_name"],
                "billing_cycle": diff["billing_cycle"],
                "diff_count": 0,
                "investigating_amount": 0.0,
                "verified_amount": 0.0,
                "recorded_amount": 0.0,
            })
            bucket["diff_count"] += 1
            if diff["status"] == DIFF_STATUS_INVESTIGATING:
                bucket["investigating_amount"] += float(diff["estimated_amount"])
            elif diff["status"] == DIFF_STATUS_VERIFIED:
                bucket["verified_amount"] += float(diff["verified_amount"])
            elif diff["status"] == DIFF_STATUS_RECORDED:
                bucket["recorded_amount"] += float(diff["verified_amount"])

        return {
            "module": self.MODULE,
            "billing_cycle": billing_cycle or "全部账期",
            "check_items": list(by_usage.values()),
            "diff_total": len(diffs),
            "pending_count": sum(1 for d in diffs if d["status"] == DIFF_STATUS_PENDING),
            "investigating_count": sum(
                1 for d in diffs if d["status"] == DIFF_STATUS_INVESTIGATING
            ),
            "verified_count": sum(
                1 for d in diffs if d["status"] == DIFF_STATUS_VERIFIED
            ),
            "recorded_count": sum(
                1 for d in diffs if d["status"] == DIFF_STATUS_RECORDED
            ),
            # 计量按差额明细区分两套金额
            "investigating_amount": round(
                sum(
                    float(d["estimated_amount"])
                    for d in diffs
                    if d["status"] == DIFF_STATUS_INVESTIGATING
                ),
                2,
            ),
            "verified_amount": round(
                sum(
                    float(d["verified_amount"])
                    for d in diffs
                    if d["status"] in (DIFF_STATUS_VERIFIED, DIFF_STATUS_RECORDED)
                ),
                2,
            ),
            "recorded_amount": round(
                sum(float(s["added_amount"]) for s in supplements), 2
            ),
            # 仍未参与计费的金额（待查 + 已核定未补记）
            "unbilled_amount": round(
                sum(
                    float(d["estimated_amount"])
                    for d in diffs
                    if d["status"] in (DIFF_STATUS_PENDING, DIFF_STATUS_INVESTIGATING)
                )
                + sum(
                    float(d["verified_amount"])
                    for d in diffs
                    if d["status"] == DIFF_STATUS_VERIFIED
                ),
                2,
            ),
            "supplement_count": len(supplements),
        }

    # ==================================================================
    # 辅助方法
    # ==================================================================

    def _usage_rows(
        self,
        billing_cycle: str,
        caller: str | None,
        api_name: str | None,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(r)
            for r in store.rows("usage_billing")
            if str(r.get("计费周期", "")) == billing_cycle
        ]
        if caller:
            rows = [r for r in rows if r.get("调用方") == caller]
        if api_name:
            rows = [r for r in rows if r.get("API名称") == api_name]
        return rows

    def _within_retry_window(self, earlier: str, later: str) -> bool:
        try:
            t1 = datetime.fromisoformat(earlier)
            t2 = datetime.fromisoformat(later)
        except ValueError:
            return False
        return timedelta(0) <= (t2 - t1) <= timedelta(seconds=RETRY_WINDOW_SECONDS)

    @staticmethod
    def _is_success_code(code: str) -> bool:
        return code in {"200", "201", "204"}

    @staticmethod
    def _is_timeout_code(code: str) -> bool:
        return code in {"504", "408", "TIMEOUT", "超时"} or "超时" in code

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _now_iso() -> str:
        from datetime import timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    # ==================================================================
    # 共享数据持久化（data/reconciliation/）
    # ==================================================================

    def _persist_diff(self, diff: dict[str, Any]) -> None:
        from app.services.data_manager import DATA_ROOT
        import json

        path = DATA_ROOT / "reconciliation" / "diffs" / f"{diff['diff_id']:06d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(diff, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _persist_supplement(self, supplement: dict[str, Any]) -> None:
        from app.services.data_manager import DATA_ROOT
        import json

        path = (
            DATA_ROOT / "reconciliation" / "supplements"
            / f"{supplement['supplement_id']:06d}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(supplement, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _write_usage_snapshot(self, usage: dict[str, Any]) -> None:
        from app.services.data_manager import write_usage

        write_usage(int(usage["id"]), {
            "usage_id": usage.get("id"),
            "record_no": usage.get("记录编号"),
            "caller": usage.get("调用方"),
            "api_name": usage.get("API名称"),
            "call_count": self._to_int(usage.get("调用次数")),
            "data_rows": self._to_int(usage.get("数据行数")),
            "amount": self._to_float(usage.get("费用金额")),
            "billing_cycle": usage.get("计费周期"),
            "settlement_status": usage.get("status"),
        })

    def reset(self) -> None:
        """清空复核内存状态（测试隔离用）。"""
        self._diffs.clear()
        self._supplements.clear()
        self._occupied_audit_ids.clear()
