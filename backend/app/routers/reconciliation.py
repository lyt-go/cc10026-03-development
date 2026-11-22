"""账单差额复核路由：用量汇总与请求记录核对、差额确认与补记。

与通用资源路由不同，这里的每个端点对应复核领域服务的一个业务动作：
- 生成/查询差额明细
- 复核确认（仅证据明确的差额可核定）
- 补记到用量计量（携带已确认的调用关联）
- 按账单口径输出核对项与待查/已核定金额
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ReconcileBuildPayload, ReconcileConfirmPayload
from app.services import RECONCILIATION_SERVICE as reconciliation_service

router = APIRouter(prefix="/api/reconciliation", tags=["账单差额复核"])


@router.post("/diffs/build")
def build_diffs(payload: ReconcileBuildPayload) -> dict[str, object]:
    """按 API、调用方、账期核对用量汇总与请求记录，生成可供补记的差额。"""
    diffs = reconciliation_service.build_differences(
        billing_cycle=payload.billing_cycle,
        caller=payload.caller,
        api_name=payload.api_name,
        unit_amount=payload.unit_amount,
    )
    return {
        "ok": True,
        "message": f"核对完成，生成 {len(diffs)} 笔差额明细",
        "items": diffs,
        "total": len(diffs),
    }


@router.get("/diffs")
def list_diffs(
    billing_cycle: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, object]:
    items = reconciliation_service.list_diffs(
        billing_cycle=billing_cycle, status=status
    )
    return {"items": items, "total": len(items)}


@router.get("/diffs/{diff_id}")
def get_diff(diff_id: int) -> dict[str, object]:
    diffs = reconciliation_service.list_diffs()
    for diff in diffs:
        if diff["diff_id"] == diff_id:
            return diff
    raise HTTPException(status_code=404, detail=f"差额明细 {diff_id} 不存在")


@router.post("/diffs/{diff_id}/confirm")
def confirm_diff(diff_id: int, payload: ReconcileConfirmPayload) -> dict[str, object]:
    diff, message = reconciliation_service.confirm_diff(
        diff_id, reviewer=payload.reviewer, remark=payload.remark or ""
    )
    if diff is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "entry": diff}


@router.post("/diffs/{diff_id}/supplement")
def supplement_diff(diff_id: int) -> dict[str, object]:
    supplement, message = reconciliation_service.supplement_diff(diff_id)
    if supplement is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "entry": supplement}


@router.get("/supplements")
def list_supplements(
    billing_cycle: str | None = Query(default=None),
) -> dict[str, object]:
    items = reconciliation_service.list_supplements(billing_cycle=billing_cycle)
    return {"items": items, "total": len(items)}


@router.get("/summary")
def summary(
    billing_cycle: str | None = Query(default=None),
) -> dict[str, object]:
    """按现有账单展示核对项，区分待查金额与已核定金额。"""
    return reconciliation_service.reconciliation_summary(billing_cycle)
