"""格式解析器接口：维护解析配置，覆盖提交配置、启用解析、废弃解析等动作。

解析链路额外提供：文件上传/批量上传（按文件指纹复用字段解析）、指纹结果、
映射差异文件、处理回执、配置版本与打包下载。
"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.fingerprint_validation import fingerprint_validation
from app.services.parse_pipeline import pipeline
from app.services.parser import ParserService

router = APIRouter(prefix="/api/parser", tags=["格式解析器"])

service = ParserService()

LIST_FIELDS = ["解析编号", "输入格式", "输出格式", "字段映射", "类型转换", "空值处理", "异常策略", "解析状态"]
STATUSES = ["草稿", "已配置", "生效中", "已废弃"]


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
    keyword: str | None = Query(default=None, description="按解析编号检索"),
    status: str | None = Query(default=None, description="草稿、已配置、生效中、已废弃"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、解析编号关键字与状态过滤格式解析器列表。"""
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
        headers={"Content-Disposition": 'attachment; filename="parser.csv"'})


@router.get("/fingerprints", response_model=PageResult[dict])
def list_fingerprints(
    keyword: str | None = Query(default=None, description="按文件名、文件指纹或指纹编号检索"),
    status: str | None = Query(default=None, description="待校验、通过、需复核"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """解析指纹结果列表：相同文件只保留可追溯的解析版本。"""
    items, total = pipeline.list_fingerprints(page=page, size=size, keyword=keyword, status=status)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/fingerprints/export")
def export_fingerprints() -> StreamingResponse:
    """导出解析指纹结果 CSV，并留下一条处理回执。"""
    text, receipt = pipeline.export_fingerprints_csv()
    return StreamingResponse(
        iter([text]), media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="parser-fingerprints.csv"',
            "X-Processing-Receipt": receipt["回执编号"],
        })


@router.get("/fingerprints/{fingerprint_id}", response_model=dict)
def get_fingerprint(fingerprint_id: int) -> dict:
    """读取单份解析指纹结果；复用结果会带上来源编号与结果根版本。"""
    entry = pipeline.get_fingerprint(fingerprint_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析指纹结果 {fingerprint_id} 不存在")
    return entry


@router.get("/diffs", response_model=PageResult[dict])
def list_diffs(
    keyword: str | None = Query(default=None, description="按文件名、文件指纹或差异编号检索"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """映射差异文件列表。"""
    items, total = pipeline.list_diffs(page=page, size=size, keyword=keyword)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/diffs/export")
def export_diffs() -> StreamingResponse:
    """导出映射差异文件 CSV，并留下一条处理回执。"""
    text, receipt = pipeline.export_diffs_csv()
    return StreamingResponse(
        iter([text]), media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="parser-mapping-diffs.csv"',
            "X-Processing-Receipt": receipt["回执编号"],
        })


@router.get("/diffs/{diff_id}", response_model=dict)
def get_diff(diff_id: int) -> dict:
    """读取单份映射差异文件。"""
    entry = pipeline.get_diff(diff_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"映射差异文件 {diff_id} 不存在")
    return entry


@router.get("/stats", response_model=dict)
def parse_stats() -> dict:
    """解析复用统计：导入次数、命中次数、节省耗时、映射冲突数。"""
    return pipeline.stats()


@router.get("/receipts", response_model=PageResult[dict])
def list_receipts(
    business: str | None = Query(default=None, description="文件导入、导出、打包、抽样校验"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """处理回执台账：导入、导出、下载、打包都能查到回执号。"""
    items, total = pipeline.list_receipts(page=page, size=size, business=business)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/receipts/{receipt_id}", response_model=dict)
def get_receipt(receipt_id: int) -> dict:
    """按回执号回读处理回执。"""
    entry = pipeline.get_receipt(receipt_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"处理回执 {receipt_id} 不存在")
    return entry


@router.get("/config-versions", response_model=dict)
def list_config_versions() -> dict:
    """解析配置版本：字段映射版本是复用的另一半绑定条件。"""
    return {"items": pipeline.list_config_versions()}


@router.post("/config-versions", response_model=ActionResult)
def publish_config_version(payload: dict) -> ActionResult:
    """发布新映射版本；同一基线并发提交时只允许一份落库，其余拒绝。"""
    entry, message = pipeline.publish_config_version(payload)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/upload", response_model=ActionResult)
def upload_file(payload: dict) -> ActionResult:
    """导入一份文件：先算指纹，命中则复用已完成字段解析，不重复转换。"""
    entry, message = pipeline.upload(payload)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/upload-batch", response_model=ActionResult)
def upload_batch(payload: dict) -> ActionResult:
    """同一批源文件一起上传；逐份判定指纹命中，返回每份文件的处理结论。"""
    entries, messages = pipeline.upload_batch(payload)
    if not entries:
        return ActionResult(ok=False, message="；".join(messages) or "批次中没有可解析的文件")
    return ActionResult(
        ok=True,
        message=f"批次处理完成：{len(entries)} 份文件；" + "；".join(messages),
        entry={"items": entries, "messages": messages},
    )


@router.get("/package")
def download_package() -> Response:
    """打包下载解析指纹结果、映射差异文件与处理回执。"""
    content, receipt, manifest = pipeline.build_package()
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="parser-parse-package.zip"',
            "X-Processing-Receipt": receipt["回执编号"],
        })


@router.get("/config-records", response_model=PageResult[dict])
def list_config_records(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """校验服务读取指纹结果与差异文件后生成的解析配置记录，可按记录编号回读。"""
    items, total = fingerprint_validation.list_config_records(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/config-records/{record_id}", response_model=dict)
def get_config_record(record_id: int) -> dict:
    """按记录编号回读解析配置记录。"""
    entry = fingerprint_validation.get_config_record(record_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析配置记录 {record_id} 不存在")
    return entry


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条解析配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条解析配置执行提交配置、启用解析、废弃解析；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
