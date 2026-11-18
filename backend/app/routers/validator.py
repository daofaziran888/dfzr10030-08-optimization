"""采集结果校验接口：维护校验规则，覆盖提交测试、启用规则、停用规则等动作。

另提供解析指纹抽样校验、跨模块待办台账与可回读校验记录：
校验服务读取同一份「解析指纹结果 + 映射差异文件」后才生成解析配置记录。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.fingerprint_validation import fingerprint_validation
from app.services.parse_pipeline import pipeline
from app.services.validator import ValidatorService

router = APIRouter(prefix="/api/validator", tags=["采集结果校验"])

service = ValidatorService()
fingerprint_service = fingerprint_validation

LIST_FIELDS = ["规则编号", "校验字段", "校验类型", "期望值", "容错范围", "采样比例", "阻断级别", "规则状态"]
STATUSES = ["草稿", "待测试", "已启用", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待测试、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤采集结果校验列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="validator.csv"'})


@router.get("/checks", response_model=PageResult[dict])
def list_checks(
    kind: str | None = Query(default=None, description="复用文件、重解析文件"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """抽样校验记录列表：复用文件与重解析文件分别有记录。"""
    items, total = fingerprint_service.list_checks(page=page, size=size, kind=kind)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/checks", response_model=ActionResult)
def run_check(payload: dict) -> ActionResult:
    """读取解析指纹结果与映射差异文件，对复用/重解析文件分别抽样校验。"""
    entry, message = fingerprint_service.run_check(payload)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/checks/{check_id}", response_model=dict)
def get_check(check_id: int) -> dict:
    """按记录编号回读校验记录，协作业务面据此复核。"""
    entry = fingerprint_service.get_check(check_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"校验记录 {check_id} 不存在")
    return entry


@router.get("/todo-ledger", response_model=PageResult[dict])
def list_todos(
    status: str | None = Query(default=None, description="待处理、已处理"),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """跨模块待办台账：解析指纹结果与映射差异文件的核对事项都在这里。"""
    items, total = fingerprint_service.list_todos(page=page, size=size, status=status, keyword=keyword)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/todo-ledger/{todo_id}/actions", response_model=ActionResult)
def update_todo(todo_id: int, payload: ActionPayload) -> ActionResult:
    """关闭或重开一条待办。"""
    entry, message = fingerprint_service.update_todo(todo_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/config-records", response_model=ActionResult)
def create_config_record(payload: dict) -> ActionResult:
    """只有同时读取到解析指纹结果与映射差异文件，才生成解析配置记录。"""
    entry, message = fingerprint_service.create_config_record(payload)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/config-records", response_model=PageResult[dict])
def list_config_records(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """解析配置记录列表，与格式解析器的回读地址一致。"""
    items, total = fingerprint_service.list_config_records(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条校验规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"校验规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条校验规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="校验规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条校验规则执行提交测试、启用规则、停用规则；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
