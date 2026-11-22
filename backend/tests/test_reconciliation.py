"""账单差额复核测试：用量汇总 × 请求记录核对、差额确认与补记。

场景覆盖题目要求：
1. 一条有明确成功证据的调用 → 可核定、可补记，金额进入计费；
2. 一条先失败再重试、两个时间接近候选的请求 → 归并为同一逻辑请求，
   查清成功与超时凭据关系，只按成功笔计费一次；
3. 一笔找不到可证实关联的超时请求 → 保留财务调查线索，维持待查，
   不参与计费；
4. 同一请求不能在多笔差额里重复出现；
5. 待查金额与已核定金额分开计量，补记结果携带已确认的调用关联。
"""
from __future__ import annotations

BILLING_CYCLE = "2026-11"
CALLER = "华东分销平台"
API_NAME = "客户画像查询API"
UNIT_AMOUNT = 10.0


class TestReconciliation:
    def setup_method(self):
        # 内存仓库与共享目录由 conftest.isolated_data_root 统一重置
        pass

    def _seed_usage(self, call_count: int = 0, amount: float = 0.0):
        from app.store import store

        rows = store.rows("usage_billing")
        usage_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        usage = {
            "id": usage_id,
            "记录编号": f"USAG-R{usage_id:03d}",
            "调用方": CALLER,
            "API名称": API_NAME,
            "调用次数": str(call_count),
            "数据行数": "0",
            "计费周期": BILLING_CYCLE,
            "费用金额": amount,
            "结算状态": "计量中",
            "status": "计量中",
            "pending": True,
            "abnormal": False,
        }
        rows.append(usage)
        return usage

    def _seed_audit_logs(self):
        """构造三条逻辑请求：成功、失败+重试、孤立超时。"""
        from app.services.call_audit import CallAuditService

        svc = CallAuditService()
        # 逻辑请求A：一次明确成功的调用
        svc.record_api_call(
            CALLER, API_NAME, params="cust_id=C001",
            response_code="200", latency_ms=42.0, result_rows=1,
            timestamp="2026-11-15T09:00:00+08:00",
        )
        # 逻辑请求B：先 504 超时，40 秒后重试成功（两个时间接近的候选）
        svc.record_api_call(
            CALLER, API_NAME, params="cust_id=C002",
            response_code="504", latency_ms=30000.0, result_rows=0,
            timestamp="2026-11-15T09:05:00+08:00",
        )
        svc.record_api_call(
            CALLER, API_NAME, params="cust_id=C002",
            response_code="200", latency_ms=55.0, result_rows=1,
            timestamp="2026-11-15T09:05:40+08:00",
        )
        # 逻辑请求C：超时后没有任何可证实的成功关联
        svc.record_api_call(
            CALLER, API_NAME, params="cust_id=C003",
            response_code="504", latency_ms=30000.0, result_rows=0,
            timestamp="2026-11-15T09:20:00+08:00",
        )

    def _build(self):
        from app.services import RECONCILIATION_SERVICE

        return RECONCILIATION_SERVICE.build_differences(
            billing_cycle=BILLING_CYCLE,
            caller=CALLER,
            api_name=API_NAME,
            unit_amount=UNIT_AMOUNT,
        )

    # ----------------------------------------------------------------

    def test_groups_retry_into_single_logical_request(self):
        """失败+重试的两个候选必须归并为一个逻辑请求。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE

        groups = RECONCILIATION_SERVICE._group_logical_requests(
            API_NAME, CALLER, BILLING_CYCLE
        )
        keys = {g["key"] for g in groups}
        assert keys == {
            f"{CALLER}|{API_NAME}|cust_id=C001",
            f"{CALLER}|{API_NAME}|cust_id=C002",
            f"{CALLER}|{API_NAME}|cust_id=C003",
        }

        retry_group = next(
            g for g in groups
            if g["key"] == f"{CALLER}|{API_NAME}|cust_id=C002"
        )
        assert retry_group["retry_chain"] is True
        assert len(retry_group["candidates"]) == 2
        assert retry_group["has_success"] and retry_group["has_timeout"]
        # 成功与超时凭据同时存在时关系已查清，置信度为明确
        assert retry_group["confidence"] == "明确"

    def test_build_creates_three_diffs_with_distinct_audit_ids(self):
        """三笔逻辑请求都未入账 → 三笔差额，且审计请求互不重复。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()

        diffs = self._build()
        assert len(diffs) == 3

        all_audit_ids: list[int] = []
        for diff in diffs:
            all_audit_ids.extend(
                c["audit_id"] for c in diff["candidate_audits"]
            )
        # 同一请求不能在多笔差额里重复出现
        assert len(all_audit_ids) == len(set(all_audit_ids)) == 4

        by_key = {d["logical_request_key"]: d for d in diffs}
        success_diff = next(
            d for d in by_key.values() if d["logical_request_key"].endswith("C001")
        )
        retry_diff = next(
            d for d in by_key.values() if d["logical_request_key"].endswith("C002")
        )
        timeout_diff = next(
            d for d in by_key.values() if d["logical_request_key"].endswith("C003")
        )

        # 明确成功证据 → 待确认；关联不全的超时 → 待查
        assert success_diff["status"] == "待确认"
        assert success_diff["has_success_evidence"] is True
        assert retry_diff["status"] == "待确认"
        assert timeout_diff["status"] == "待查"
        assert timeout_diff["confidence"] == "关联不全"
        # 待查项保留可供财务调查的线索
        assert timeout_diff["candidate_audits"][0]["response_code"] == "504"
        assert timeout_diff["candidate_audits"][0]["timestamp"]

    def test_investigating_diff_cannot_be_confirmed(self):
        """关联不明确的差额禁止核定，必须继续追查。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE

        diffs = self._build()
        timeout_diff = next(d for d in diffs if d["status"] == "待查")
        result, message = RECONCILIATION_SERVICE.confirm_diff(
            timeout_diff["diff_id"]
        )
        assert result is None
        assert "关联仍不明确" in message
        assert timeout_diff["status"] == "待查"

    def test_confirm_and_supplement_success_call(self):
        """明确成功调用：确认→补记，金额进入计费且携带调用关联。"""
        usage = self._seed_usage(call_count=0, amount=0.0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE
        from app.store import store

        diffs = self._build()
        success_diff = next(
            d for d in diffs if d["logical_request_key"].endswith("C001")
        )

        confirmed, message = RECONCILIATION_SERVICE.confirm_diff(
            success_diff["diff_id"], reviewer="财务复核员A"
        )
        assert confirmed is not None
        assert confirmed["status"] == "已核定"
        assert confirmed["verified_amount"] == UNIT_AMOUNT
        assert confirmed["confirmed_record_nos"] == ["CALL-0004"]
        assert "已核定" in message

        supplement, message = RECONCILIATION_SERVICE.supplement_diff(
            success_diff["diff_id"]
        )
        assert supplement is not None
        assert supplement["status"] == "已补记"
        # 已确认的调用关联带到补记结果
        assert supplement["confirmed_record_nos"] == ["CALL-0004"]
        assert supplement["added_amount"] == UNIT_AMOUNT
        assert supplement["added_call_count"] == 1

        # 用量汇总被真实回写
        updated = store.find("usage_billing", usage["id"])
        assert updated["调用次数"] == "1"
        assert float(updated["费用金额"]) == UNIT_AMOUNT
        assert success_diff["status"] == "已补记"

    def test_retry_chain_billed_once_not_twice(self):
        """成功+超时重试链核定后只计费一次，超时笔不重复计费。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE
        from app.store import store

        diffs = self._build()
        retry_diff = next(
            d for d in diffs if d["logical_request_key"].endswith("C002")
        )
        RECONCILIATION_SERVICE.confirm_diff(retry_diff["diff_id"])
        supplement, _ = RECONCILIATION_SERVICE.supplement_diff(
            retry_diff["diff_id"]
        )

        # 两个候选但只补记一次调用、一笔金额
        assert supplement["added_call_count"] == 1
        assert supplement["added_amount"] == UNIT_AMOUNT
        # 只携带成功笔关联，超时笔不进入计费关联
        assert supplement["confirmed_record_nos"] == ["CALL-0006"]
        assert retry_diff["confirmed_audit_ids"] == [6]
        assert "重试" in retry_diff["relation_note"]

    def test_summary_separates_investigating_and_verified_amount(self):
        """计量按差额明细区分待查与已核定金额，并标出未参与计费部分。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE

        self._build()
        diffs = RECONCILIATION_SERVICE.list_diffs()
        # 核定两笔证据明确的（成功 + 重试链），补记其中一笔
        clear_diffs = [d for d in diffs if d["status"] == "待确认"]
        assert len(clear_diffs) == 2
        RECONCILIATION_SERVICE.confirm_diff(clear_diffs[0]["diff_id"])
        RECONCILIATION_SERVICE.confirm_diff(clear_diffs[1]["diff_id"])
        RECONCILIATION_SERVICE.supplement_diff(clear_diffs[0]["diff_id"])

        summary = RECONCILIATION_SERVICE.reconciliation_summary(BILLING_CYCLE)
        assert summary["diff_total"] == 3
        assert summary["investigating_count"] == 1
        assert summary["verified_count"] == 1
        assert summary["recorded_count"] == 1
        # 待查金额 10，已核定（含已补记）金额 20
        assert summary["investigating_amount"] == UNIT_AMOUNT
        assert summary["verified_amount"] == UNIT_AMOUNT * 2
        assert summary["recorded_amount"] == UNIT_AMOUNT
        # 仍未参与计费：待查 10 + 已核定未补记 10 = 20
        assert summary["unbilled_amount"] == UNIT_AMOUNT * 2
        # 核对项按用量记录展示
        assert len(summary["check_items"]) == 1
        assert summary["check_items"][0]["diff_count"] == 3

    def test_rebuild_does_not_duplicate_already_associated_requests(self):
        """再次核对时，已占用的审计请求不能在新差额里重复出现。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE

        first = self._build()
        assert len(first) == 3
        # 已全部生成差额，重新核对不应再产生同一批请求的重复差额
        second = self._build()
        assert second == []

    def test_billed_calls_reduce_gap(self):
        """已计量调用次数会冲减缺口：计量 1 笔后只剩 2 笔差额。"""
        self._seed_usage(call_count=1)
        self._seed_audit_logs()

        diffs = self._build()
        assert len(diffs) == 2
        # 证据明确的优先进入待确认，待查项依然保留
        statuses = {d["status"] for d in diffs}
        assert "待确认" in statuses and "待查" in statuses

    def test_diff_persisted_to_shared_directory(self):
        """差额明细持久化到 data/reconciliation/ 供跨模块/离线追查。"""
        from app.services.data_manager import DATA_ROOT

        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        self._build()

        diff_files = list((DATA_ROOT / "reconciliation" / "diffs").glob("*.json"))
        assert len(diff_files) == 3
        # 补记目录在首次补记时落盘
        from app.services import RECONCILIATION_SERVICE

        assert not (DATA_ROOT / "reconciliation" / "supplements").exists()
        diffs = RECONCILIATION_SERVICE.list_diffs()
        clear = next(d for d in diffs if d["status"] == "待确认")
        RECONCILIATION_SERVICE.confirm_diff(clear["diff_id"])
        RECONCILIATION_SERVICE.supplement_diff(clear["diff_id"])
        supplement_files = list(
            (DATA_ROOT / "reconciliation" / "supplements").glob("*.json")
        )
        assert len(supplement_files) == 1

    def test_cannot_supplement_unverified_diff(self):
        """未核定差额不能直接补记。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE

        diffs = self._build()
        success_diff = next(
            d for d in diffs if d["logical_request_key"].endswith("C001")
        )
        result, message = RECONCILIATION_SERVICE.supplement_diff(
            success_diff["diff_id"]
        )
        assert result is None
        assert "尚未核定" in message

    def test_confirmation_idempotent_and_double_supplement_blocked(self):
        """重复确认与重复补记都被拦截。"""
        self._seed_usage(call_count=0)
        self._seed_audit_logs()
        from app.services import RECONCILIATION_SERVICE

        diffs = self._build()
        success_diff = next(
            d for d in diffs if d["logical_request_key"].endswith("C001")
        )
        RECONCILIATION_SERVICE.confirm_diff(success_diff["diff_id"])
        again, message = RECONCILIATION_SERVICE.confirm_diff(
            success_diff["diff_id"]
        )
        assert again is None and "已核定" in message

        RECONCILIATION_SERVICE.supplement_diff(success_diff["diff_id"])
        dup, message = RECONCILIATION_SERVICE.supplement_diff(
            success_diff["diff_id"]
        )
        assert dup is None and "已补记" in message
