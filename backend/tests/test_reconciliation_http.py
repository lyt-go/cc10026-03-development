"""账单差额复核 HTTP 端到端：通过 FastAPI 路由走完整核对→确认→补记链路。"""
from __future__ import annotations

import pytest

BILLING_CYCLE = "2026-11"
CALLER = "华北零售门户"
API_NAME = "库存查询API"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.store import store
    from app.services.call_audit import CallAuditService

    usage_id = max((int(r["id"]) for r in store.rows("usage_billing"))) + 1
    record_no = f"USAG-H{usage_id:03d}"
    store.rows("usage_billing").append({
        "id": usage_id,
        "记录编号": record_no,
        "调用方": CALLER,
        "API名称": API_NAME,
        "调用次数": "0",
        "数据行数": "0",
        "计费周期": BILLING_CYCLE,
        "费用金额": 0.0,
        "结算状态": "计量中",
        "status": "计量中",
        "pending": True,
        "abnormal": False,
    })

    audit = CallAuditService()
    # 明确成功
    audit.record_api_call(
        CALLER, API_NAME, params="sku=S1", response_code="200",
        latency_ms=30.0, result_rows=2,
        timestamp="2026-11-16T10:00:00+08:00",
    )
    # 超时后重试成功
    audit.record_api_call(
        CALLER, API_NAME, params="sku=S2", response_code="504",
        latency_ms=30000.0, result_rows=0,
        timestamp="2026-11-16T10:05:00+08:00",
    )
    audit.record_api_call(
        CALLER, API_NAME, params="sku=S2", response_code="200",
        latency_ms=48.0, result_rows=1,
        timestamp="2026-11-16T10:05:50+08:00",
    )
    # 孤立超时，无成功关联
    audit.record_api_call(
        CALLER, API_NAME, params="sku=S3", response_code="504",
        latency_ms=30000.0, result_rows=0,
        timestamp="2026-11-16T10:20:00+08:00",
    )

    with TestClient(app) as test_client:
        yield test_client, record_no


def test_full_reconciliation_flow_over_http(client):
    client, record_no = client
    # 1. 生成差额
    resp = client.post("/api/reconciliation/diffs/build", json={
        "billing_cycle": BILLING_CYCLE,
        "caller": CALLER,
        "api_name": API_NAME,
        "unit_amount": 8.0,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    diffs = body["items"]

    # 2. 待查差额不能确认 → 400
    investigating = next(d for d in diffs if d["status"] == "待查")
    blocked = client.post(
        f"/api/reconciliation/diffs/{investigating['diff_id']}/confirm",
        json={"reviewer": "复核员B"},
    )
    assert blocked.status_code == 400
    assert "关联仍不明确" in blocked.json()["detail"]

    # 3. 两笔证据明确的差额依次确认、补记
    clear = [d for d in diffs if d["status"] == "待确认"]
    assert len(clear) == 2
    for diff in clear:
        ok = client.post(
            f"/api/reconciliation/diffs/{diff['diff_id']}/confirm",
            json={"reviewer": "复核员B"},
        )
        assert ok.status_code == 200
        done = client.post(
            f"/api/reconciliation/diffs/{diff['diff_id']}/supplement"
        )
        assert done.status_code == 200
        supplement = done.json()["entry"]
        # 补记结果携带已确认的调用关联
        assert supplement["confirmed_record_nos"]
        assert supplement["added_amount"] == 8.0

    # 4. 汇总：待查 8 元仍未参与计费，已核定并补记 16 元
    summary = client.get(
        f"/api/reconciliation/summary?billing_cycle={BILLING_CYCLE}"
    ).json()
    assert summary["diff_total"] == 3
    assert summary["recorded_count"] == 2
    assert summary["investigating_count"] == 1
    assert summary["investigating_amount"] == 8.0
    assert summary["recorded_amount"] == 16.0
    assert summary["unbilled_amount"] == 8.0

    # 5. 补记清单包含两条且都带关联记录编号
    supplements = client.get("/api/reconciliation/supplements").json()
    assert supplements["total"] == 2
    assert all(s["confirmed_record_nos"] for s in supplements["items"])

    # 6. 通用资源路由未被专用路径抢占
    assert client.get("/api/reconciliation").status_code == 404
    listed = client.get("/api/usage_billing").json()
    assert listed["total"] >= 1
    reconciled_usage = next(
        item for item in listed["items"] if item["记录编号"] == record_no
    )
    # 两笔已补记 → 调用次数 2、金额 16
    assert reconciled_usage["调用次数"] == "2"
    assert float(reconciled_usage["费用金额"]) == 16.0
