"""采集结果校验 × 解析指纹：抽样校验、跨模块待办台账与可回读记录。

校验服务读取解析指纹结果与映射差异文件：
- 对「复用文件」和「重解析文件」分别抽样，避免只校验新解析结果；
- 每条校验记录都带记录编号，可以按编号回读原样；
- 跨模块待办统一写入待办台账（指纹结果核对、映射差异核对、版本追溯），
  同一指纹同类型待办做覆盖更新，反复校验不会堆出重复项；
- 只有同时读取到「解析指纹结果」与「映射差异文件」时才允许生成解析配置记录。
"""
from __future__ import annotations

import copy
import math
from datetime import datetime
from threading import RLock
from typing import Any

from app.services.parse_pipeline import pipeline

REUSE = "复用文件"
REPARSE = "重解析文件"
CHECK_PASS = "通过"
CHECK_REVIEW = "需复核"
OPEN = "待处理"
DONE = "已处理"


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _deep(value: Any) -> Any:
    return copy.deepcopy(value)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _numeric_like(field: str) -> bool:
    return any(token in field for token in ("金额", "数量", "价格", "数值", "amount", "qty", "price"))


def _to_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("￥", "").replace("¥", "").strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class FingerprintValidationService:
    def __init__(self) -> None:
        self._lock = RLock()
        self._checks: list[dict[str, Any]] = []
        self._todo_ledger: list[dict[str, Any]] = []
        self._config_records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ 抽样校验
    def run_check(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            ratio = values.get("sampling_ratio", values.get("采样比例", 0.34))
            try:
                ratio = float(ratio)
            except (TypeError, ValueError):
                return None, "采样比例必须是 0 到 1 之间的小数"
            if not 0 < ratio <= 1:
                return None, "采样比例必须落在 (0, 1] 区间"

            requested = values.get("fingerprint_ids", values.get("指纹结果编号"))
            if isinstance(requested, (str, int)):
                requested = [requested]
            ids = {int(item) for item in requested} if requested else None

            fingerprints, _ = pipeline.list_fingerprints(page=1, size=100000)
            if ids is not None:
                fingerprints = [row for row in fingerprints if int(row["id"]) in ids]
            if not fingerprints:
                return None, "没有可校验的解析指纹结果，请先上传文件完成解析"

            summaries: dict[str, dict[str, Any]] = {
                REUSE: {"类型": REUSE, "文件数": 0, "抽样条数": 0, "问题数": 0, "记录编号": []},
                REPARSE: {"类型": REPARSE, "文件数": 0, "抽样条数": 0, "问题数": 0, "记录编号": []},
            }
            records: list[dict[str, Any]] = []
            for fingerprint in fingerprints:
                kind = REUSE if fingerprint.get("是否复用") else REPARSE
                record = self._check_one(fingerprint, ratio)
                record["类型"] = kind
                record_id = _next_id(self._checks)
                record["id"] = record_id
                record["记录编号"] = f"FPCHK-{record_id:06d}"
                record["可回读"] = True
                record["回读地址"] = f"/api/validator/checks/{record_id}"
                self._checks.append(record)
                pipeline.mark_validation(int(fingerprint["id"]), record["校验结果"], record_id)
                bucket = summaries[kind]
                bucket["文件数"] += 1
                bucket["抽样条数"] += int(record["抽样条数"])
                bucket["问题数"] += int(record["问题数"])
                bucket["记录编号"].append(record["记录编号"])
                records.append(record)
                self._write_todos(fingerprint, record)

            summary = {
                "校验批次": f"FPBATCH-{len(self._checks):06d}",
                "采样比例": ratio,
                "文件数": len(fingerprints),
                "复用文件": summaries[REUSE],
                "重解析文件": summaries[REPARSE],
                "通过数": sum(1 for row in records if row["校验结果"] == CHECK_PASS),
                "需复核数": sum(1 for row in records if row["校验结果"] == CHECK_REVIEW),
                "记录编号": [row["记录编号"] for row in records],
                "记录数": len(records),
                "待办数": len([row for row in self._todo_ledger if row["状态"] == OPEN]),
                "生成时间": now_text(),
            }
            receipt = pipeline.create_receipt(
                "抽样校验",
                detail=(
                    f"复用文件 {summaries[REUSE]['文件数']} 个、重解析文件 "
                    f"{summaries[REPARSE]['文件数']} 个；生成待办 "
                    f"{len([row for row in self._todo_ledger if row['状态'] == OPEN])} 条"
                ),
            )
            summary["处理回执号"] = receipt["回执编号"]
            return {"summary": summary, "records": records, "receipt": receipt}, (
                f"抽样校验完成：复用文件 {summaries[REUSE]['文件数']} 个、"
                f"重解析文件 {summaries[REPARSE]['文件数']} 个，"
                f"问题 {summaries[REUSE]['问题数'] + summaries[REPARSE]['问题数']} 处，"
                f"记录 {len(records)} 条可回读"
            )

    def _check_one(self, fingerprint: dict[str, Any], ratio: float) -> dict[str, Any]:
        fields = fingerprint.get("字段结果") or []
        mapping = fingerprint.get("映射快照") or {}
        total = len(fields)
        sample_size = max(1, math.ceil(total * ratio)) if total else 0
        sample = fields[:sample_size]
        issues: list[dict[str, Any]] = []
        seen_ids: dict[str, int] = {}
        for index, record in enumerate(sample):
            if not isinstance(record, dict):
                issues.append({
                    "行号": index + 1, "字段": "-", "问题类型": "记录结构异常",
                    "说明": "解析结果不是对象", "级别": "阻断",
                })
                continue
            for source, target in mapping.items():
                value = record.get(target or source)
                if target not in record and source not in record:
                    issues.append({
                        "行号": index + 1, "字段": target or source, "问题类型": "字段缺失",
                        "说明": f"字段映射 {source} → {target or source} 未产出值", "级别": "阻断",
                    })
                elif _is_blank(value):
                    issues.append({
                        "行号": index + 1, "字段": target or source, "问题类型": "空值",
                        "说明": "抽样记录该字段为空", "级别": "提示",
                    })
                elif _numeric_like(str(target or source)) and _to_number(value) is None:
                    issues.append({
                        "行号": index + 1, "字段": target or source, "问题类型": "类型越界",
                        "说明": f"期望数值，实际为 {value!r}", "级别": "阻断",
                    })
            for key in ("订单编号", "order_id"):
                if key in record:
                    marker = str(record.get(key))
                    seen_ids[marker] = seen_ids.get(marker, 0) + 1
        for marker, count in seen_ids.items():
            if count > 1:
                issues.append({
                    "行号": "-", "字段": "订单编号", "问题类型": "抽样重复",
                    "说明": f"订单编号 {marker} 在抽样中出现 {count} 次", "级别": "阻断",
                })

        current = pipeline.current_config_version() or {}
        result_version = fingerprint.get("结果版本")
        version_behind = bool(current) and str(result_version) != str(current.get("version"))
        if version_behind and fingerprint.get("是否复用"):
            issues.append({
                "行号": "-", "字段": "解析配置版本", "问题类型": "版本落后",
                "说明": (
                    f"复用结果版本 {result_version} 落后于当前生效版本 "
                    f"{current.get('version')}，导出需按结果版本追溯"
                ),
                "级别": "提示",
            })
        if fingerprint.get("映射变更") and not (fingerprint.get("映射差异") or {}).get("可导出"):
            issues.append({
                "行号": "-", "字段": "映射差异", "问题类型": "差异不可导出",
                "说明": "映射变化缺少可导出的差异文件，导出字段无法追溯", "级别": "阻断",
            })

        passed = not any(issue["级别"] == "阻断" for issue in issues)
        return {
            "指纹结果编号": fingerprint.get("id"),
            "指纹编号": fingerprint.get("指纹编号"),
            "文件名": fingerprint.get("文件名"),
            "文件指纹": fingerprint.get("文件指纹"),
            "结果版本": result_version,
            "结果根版本": fingerprint.get("结果根版本"),
            "解析配置版本": fingerprint.get("解析配置版本"),
            "版本落后": version_behind,
            "抽样比例": ratio,
            "抽样条数": len(sample),
            "记录总数": total,
            "问题数": len(issues),
            "阻断问题数": sum(1 for issue in issues if issue["级别"] == "阻断"),
            "问题明细": issues,
            "校验结果": CHECK_PASS if passed else CHECK_REVIEW,
            "映射差异编号": fingerprint.get("映射差异编号"),
            "导入回执号": fingerprint.get("处理回执号"),
            "校验时间": now_text(),
        }

    # ------------------------------------------------------------------ 待办台账
    def _upsert_todo(self, key: str, values: dict[str, Any]) -> None:
        for row in self._todo_ledger:
            if row["_key"] == key:
                row.update(values)
                row["更新时间"] = now_text()
                if row.get("可回读记录编号") is None:
                    row["状态"] = OPEN
                return
        todo_id = _next_id(self._todo_ledger)
        entry = {
            "id": todo_id,
            "待办编号": f"TODO-{todo_id:06d}",
            "_key": key,
            "来源模块": values.get("来源模块", "采集结果校验"),
            "关联指纹编号": values.get("关联指纹编号"),
            "关联差异编号": values.get("关联差异编号"),
            "事项": values["事项"],
            "级别": values.get("级别", "提示"),
            "状态": values.get("状态", OPEN),
            "可回读记录编号": values.get("可回读记录编号"),
            "说明": values.get("说明", ""),
            "创建时间": now_text(),
            "更新时间": now_text(),
        }
        self._todo_ledger.append(entry)

    def _write_todos(self, fingerprint: dict[str, Any], record: dict[str, Any]) -> None:
        fingerprint_id = int(fingerprint["id"])
        self._upsert_todo(
            f"check-{fingerprint_id}",
            {
                "来源模块": "采集结果校验",
                "关联指纹编号": fingerprint.get("指纹编号"),
                "关联差异编号": fingerprint.get("映射差异编号"),
                "事项": f"抽样校验 {fingerprint.get('文件名')}（{record['类型']}）",
                "级别": "阻断" if record["校验结果"] == CHECK_REVIEW else "提示",
                "状态": OPEN if record["校验结果"] == CHECK_REVIEW else DONE,
                "可回读记录编号": record["记录编号"],
                "说明": f"结果版本 {record['结果版本']}，抽样 {record['抽样条数']} 条，问题 {record['问题数']} 处",
            },
        )
        self._upsert_todo(
            f"mapping-{fingerprint_id}",
            {
                "来源模块": "格式解析器",
                "关联指纹编号": fingerprint.get("指纹编号"),
                "关联差异编号": fingerprint.get("映射差异编号"),
                "事项": "核对解析指纹结果与映射差异文件",
                "级别": "提示" if fingerprint.get("映射变更") else "正常",
                "状态": OPEN if fingerprint.get("映射变更") else DONE,
                "可回读记录编号": record["记录编号"],
                "说明": (
                    f"差异类型 {fingerprint.get('命中类型')}；"
                    f"结果版本 {fingerprint.get('结果版本')}；"
                    f"映射差异 {'可导出' if (fingerprint.get('映射差异') or {}).get('可导出') else '缺失'}"
                ),
            },
        )
        self._upsert_todo(
            f"trace-{fingerprint_id}",
            {
                "来源模块": "格式解析器",
                "关联指纹编号": fingerprint.get("指纹编号"),
                "关联差异编号": fingerprint.get("映射差异编号"),
                "事项": "导出字段追溯版本核对",
                "级别": "提示",
                "状态": DONE if record.get("结果根版本") else OPEN,
                "可回读记录编号": record["记录编号"],
                "说明": (
                    f"字段结果只来自结果版本 {record.get('结果版本')}，"
                    f"结果根版本 {record.get('结果根版本')}；配置版本 {record.get('解析配置版本')}"
                ),
            },
        )

    def list_todos(self, *, page: int = 1, size: int = 20, status: str | None = None,
                   keyword: str | None = None) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(reversed(self._todo_ledger))
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if keyword:
            needle = keyword.casefold()
            rows = [row for row in rows if needle in str(row.get("事项", "")).casefold()
                    or needle in str(row.get("待办编号", "")).casefold()
                    or needle in str(row.get("关联指纹编号", "")).casefold()]
        total = len(rows)
        start = (page - 1) * size
        return _deep(rows[start:start + size]), total

    def update_todo(self, todo_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            for row in self._todo_ledger:
                if int(row["id"]) == todo_id:
                    text = action.strip()
                    if text in {"关闭", "标记已处理", "处理完成", "完成", "close"}:
                        row["状态"] = DONE
                        row["更新时间"] = now_text()
                        return _deep(row), "待办已标记为已处理"
                    if text in {"重开", "重新打开", "open"}:
                        row["状态"] = OPEN
                        row["更新时间"] = now_text()
                        return _deep(row), "待办已重新打开"
                    return None, f"动作「{action}」不在待办台账可执行范围"
        return None, f"待办 {todo_id} 不存在"

    def list_checks(self, *, page: int = 1, size: int = 20,
                    kind: str | None = None) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(reversed(self._checks))
        if kind:
            rows = [row for row in rows if row.get("类型") == kind]
        total = len(rows)
        start = (page - 1) * size
        return _deep(rows[start:start + size]), total

    def get_check(self, check_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._checks:
                if int(row["id"]) == check_id:
                    return _deep(row)
        return None

    # ------------------------------------------------------------------ 解析配置记录
    def create_config_record(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        fingerprint_id = values.get("fingerprint_id", values.get("指纹结果编号"))
        diff_id = values.get("diff_id", values.get("映射差异编号"))
        if fingerprint_id in (None, ""):
            return None, "缺少解析指纹结果编号，无法生成解析配置记录"
        if diff_id in (None, ""):
            return None, "缺少映射差异文件编号，无法生成解析配置记录"
        try:
            fingerprint_id = int(fingerprint_id)
            diff_id = int(diff_id)
        except (TypeError, ValueError):
            return None, "指纹结果编号与映射差异编号必须是整数"
        fingerprint = pipeline.get_fingerprint(fingerprint_id)
        if fingerprint is None:
            return None, f"解析指纹结果 {fingerprint_id} 不存在，无法生成解析配置记录"
        diff = pipeline.get_diff(diff_id)
        if diff is None:
            return None, f"映射差异文件 {diff_id} 不存在，无法生成解析配置记录"
        if int(diff.get("指纹结果编号") or 0) != fingerprint_id:
            return None, "映射差异文件与解析指纹结果不是同一份，拒绝生成解析配置记录"
        with self._lock:
            for row in self._config_records:
                if int(row["指纹结果编号"]) == fingerprint_id and int(row["映射差异编号"]) == diff_id:
                    return _deep(row), f"解析配置记录已存在：{row['记录编号']}"
            record_id = _next_id(self._config_records)
            record = {
                "id": record_id,
                "记录编号": f"PCR-{record_id:06d}",
                "指纹结果编号": fingerprint_id,
                "指纹编号": fingerprint.get("指纹编号"),
                "映射差异编号": diff_id,
                "差异编号": diff.get("差异编号"),
                "文件名": fingerprint.get("文件名"),
                "文件指纹": fingerprint.get("文件指纹"),
                "解析配置版本": fingerprint.get("解析配置版本"),
                "结果版本": fingerprint.get("结果版本"),
                "结果来源": fingerprint.get("结果来源"),
                "命中类型": fingerprint.get("命中类型"),
                "差异类型": diff.get("差异类型"),
                "来源": "采集结果校验",
                "可回读": True,
                "回读地址": f"/api/parser/config-records/{record_id}",
                "生成时间": now_text(),
            }
            self._config_records.append(record)
            return _deep(record), f"已读取解析指纹结果与映射差异文件，解析配置记录 {record['记录编号']} 已生成"

    def list_config_records(self, *, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(reversed(self._config_records))
        total = len(rows)
        start = (page - 1) * size
        return _deep(rows[start:start + size]), total

    def get_config_record(self, record_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._config_records:
                if int(row["id"]) == record_id:
                    return _deep(row)
        return None


fingerprint_validation = FingerprintValidationService()
