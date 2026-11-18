"""格式解析指纹复用：按文件内容指纹 + 解析配置版本复用字段解析结果。

设计要点：
- 文件指纹 = 原始内容字节的 SHA-256，内容不变才可能命中；
- 复用必须同时绑定指纹结果与解析配置版本，命中后 `结果版本` / `结果根版本`
  指向真正产生字段结果的那一次解析，导出时不会混入其他版本；
- 同一内容、映射配置升级时有两个成立边界：
  * `fingerprint_first`（文件指纹优先）：复用旧字段结果，只单独标记映射变化；
  * `mapping_first`（映射版本优先）：按新映射重解析，并保留旧结果用于比较；
- 每次导入都会生成解析指纹结果、映射差异文件和处理回执；导出/下载/打包
  也会再写一条处理回执，保证输出可回查。
"""
from __future__ import annotations

import base64
import binascii
import copy
import csv
import hashlib
import io
import json
import time
import zipfile
from datetime import datetime
from threading import RLock
from typing import Any

STRATEGY_FINGERPRINT_FIRST = "fingerprint_first"
STRATEGY_MAPPING_FIRST = "mapping_first"

STRATEGY_LABELS = {
    STRATEGY_FINGERPRINT_FIRST: "文件指纹优先",
    STRATEGY_MAPPING_FIRST: "映射版本优先",
}

DEFAULT_MAPPING_V1: dict[str, str] = {
    "order_id": "订单编号",
    "amount": "金额",
    "customer.name": "客户名称",
}

DEFAULT_MAPPING_V2: dict[str, str] = {
    "order_id": "订单编号",
    "amount": "金额(元)",
    "customer.name": "客户名称",
    "status": "订单状态",
}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _deep(value: Any) -> Any:
    return copy.deepcopy(value)


def normalize_strategy(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text in {"mapping_first", "mapping-first", "mapping", "映射优先", "映射版本优先", "重解析", "reparse"}:
        return STRATEGY_MAPPING_FIRST
    return STRATEGY_FINGERPRINT_FIRST


def normalize_mapping(value: Any) -> dict[str, str]:
    """把字段映射统一成 {源字段: 目标字段}。

    兼容 dict、JSON 字符串、`a:b,b:c` / `a->b` 文本，以及
    `[{来源字段, 目标字段}]` 列表；空目标字段时沿用源字段名。
    """
    if value is None:
        return {}
    parsed: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pairs: list[dict[str, str]] = []
            for chunk in text.replace("\n", ",").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                separator = "->" if "->" in chunk else (":" if ":" in chunk else None)
                if separator is None:
                    pairs.append({"来源字段": chunk, "目标字段": chunk})
                else:
                    left, right = chunk.split(separator, 1)
                    pairs.append({"来源字段": left.strip(), "目标字段": right.strip()})
            parsed = pairs

    if isinstance(parsed, dict):
        return {str(key): str(target or key) for key, target in parsed.items() if str(key).strip()}

    if isinstance(parsed, list):
        mapping: dict[str, str] = {}
        for item in parsed:
            if isinstance(item, dict):
                source = (
                    item.get("来源字段") or item.get("source") or item.get("from")
                    or item.get("字段") or item.get("name")
                )
                target = (
                    item.get("目标字段") or item.get("target") or item.get("to")
                    or item.get("映射字段") or source
                )
                if source:
                    mapping[str(source)] = str(target or source)
            elif isinstance(item, str):
                mapping[item] = item
        return mapping
    return {}


def detect_format(file_name: str, content: str, declared: str | None = None) -> str:
    declared_text = str(declared or "").strip().casefold()
    if declared_text in {"json", "csv", "text", "txt"}:
        return "text" if declared_text == "txt" else declared_text
    suffix = file_name.rsplit(".", 1)[-1].casefold() if "." in file_name else ""
    if suffix == "json":
        return "json"
    if suffix == "csv":
        return "csv"
    if suffix in {"txt", "text", "log"}:
        return "text"
    stripped = content.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    first_line = content.splitlines()[0] if content.splitlines() else ""
    if "," in first_line and len(content.splitlines()) > 1:
        return "csv"
    return "text"


def _lookup(record: Any, path: str) -> Any:
    if isinstance(record, dict) and path in record:
        return record.get(path)
    current = record
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def parse_records(content: str, file_name: str, input_format: str | None,
                  mapping: dict[str, str]) -> list[dict[str, Any]]:
    """只做一次字段转换；调用方负责在指纹命中时跳过这里。"""
    fmt = detect_format(file_name, content, input_format)
    if fmt == "json":
        data = json.loads(content)
        if isinstance(data, dict):
            for key in ("records", "items", "data", "rows"):
                if isinstance(data.get(key), list):
                    raw_records = data[key]
                    break
            else:
                raw_records = [data]
        elif isinstance(data, list):
            raw_records = data
        else:
            raw_records = [{"value": data}]
    elif fmt == "csv":
        raw_records = list(csv.DictReader(io.StringIO(content)))
    else:
        raw_records = []
        record: dict[str, Any] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line:
                if record:
                    raw_records.append(record)
                    record = {}
                continue
            separator = "=" if "=" in line else (":" if ":" in line else None)
            if separator is None:
                record[f"field_{len(record) + 1}"] = line
            else:
                key, value = line.split(separator, 1)
                record[key.strip()] = value.strip()
        if record:
            raw_records.append(record)
        if not raw_records:
            raw_records = [{"value": content}]

    mapped: list[dict[str, Any]] = []
    for raw in raw_records:
        if not mapping:
            if isinstance(raw, dict):
                mapped.append({str(key): _deep(value) for key, value in raw.items()})
            else:
                mapped.append({"value": _deep(raw)})
            continue
        output: dict[str, Any] = {}
        for source, target in mapping.items():
            output[target or source] = _deep(_lookup(raw, source))
        mapped.append(output)
    return mapped


def mapping_diff_details(old_mapping: dict[str, str],
                         new_mapping: dict[str, str]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for field in sorted(set(old_mapping) | set(new_mapping)):
        old_target = old_mapping.get(field)
        new_target = new_mapping.get(field)
        if old_target is None and new_target is not None:
            change = "新增"
        elif old_target is not None and new_target is None:
            change = "删除"
        elif old_target != new_target:
            change = "修改"
        else:
            continue
        details.append({
            "字段": field,
            "旧映射": old_target,
            "新映射": new_target,
            "差异类型": change,
        })
    return details


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        text = "'" + text
    return text


def _csv_text(headers: list[str], rows: list[list[Any]]) -> str:
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_cell(value) for value in row])
    return output.getvalue()


class ParsePipelineService:
    """内存态解析指纹账本；真实项目里换成数据库表即可。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._fingerprints: list[dict[str, Any]] = []
        self._diffs: list[dict[str, Any]] = []
        self._receipts: list[dict[str, Any]] = []
        self._config_versions: list[dict[str, Any]] = [
            {
                "id": 1,
                "version": "v1",
                "mapping": dict(DEFAULT_MAPPING_V1),
                "input_format": "json",
                "status": "历史",
                "active": False,
                "created_at": "2026-11-01T09:00:00",
                "note": "初始字段映射",
            },
            {
                "id": 2,
                "version": "v2",
                "mapping": dict(DEFAULT_MAPPING_V2),
                "input_format": "json",
                "status": "生效中",
                "active": True,
                "created_at": "2026-11-15T09:00:00",
                "note": "金额字段改名并新增订单状态",
            },
        ]
        self._tokens: dict[str, int] = {}

    # ------------------------------------------------------------------ 配置版本
    def list_config_versions(self) -> list[dict[str, Any]]:
        with self._lock:
            return _deep(self._config_versions)

    def current_config_version(self) -> dict[str, Any] | None:
        with self._lock:
            active = [row for row in self._config_versions if row.get("active")]
            return _deep(active[-1]) if active else None

    def _resolve_config(self, values: dict[str, Any]) -> dict[str, Any]:
        version_text = str(
            values.get("config_version") or values.get("解析配置版本") or ""
        ).strip()
        mapping_override = values.get("mapping", values.get("字段映射"))
        has_override = mapping_override not in (None, "", {}, [])
        if has_override:
            mapping = normalize_mapping(mapping_override)
            version = version_text or "custom"
            return {
                "version": version,
                "mapping": mapping,
                "input_format": values.get("input_format") or values.get("输入格式"),
                "source": "inline",
            }
        if version_text:
            for row in self._config_versions:
                if str(row["version"]).casefold() == version_text.casefold():
                    return _deep(row)
            raise ValueError(f"解析配置版本 {version_text} 不存在，请先发布该版本")
        current = self.current_config_version()
        if current is None:
            raise ValueError("当前没有生效中的解析配置版本")
        if values.get("input_format") or values.get("输入格式"):
            current["input_format"] = values.get("input_format") or values.get("输入格式")
        return current

    def publish_config_version(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """发布新映射版本；base_version 不匹配时拒绝，保证并发提交只落一份。"""
        with self._lock:
            mapping = normalize_mapping(
                values.get("mapping", values.get("字段映射", values.get("values", {})))
            )
            if not mapping:
                return None, "字段映射不能为空，本次修改未落库"
            current = next((row for row in self._config_versions if row.get("active")), None)
            base_version = str(
                values.get("base_version") or values.get("基线版本") or ""
            ).strip()
            if base_version and current and base_version != current["version"]:
                return None, (
                    f"版本冲突：基线版本 {base_version} 已被 {current['version']} 取代，"
                    "本次修改拒绝落库，请刷新后重试"
                )
            if current is not None and normalize_mapping(current["mapping"]) == mapping:
                return None, f"字段映射与当前版本 {current['version']} 一致，未生成新版本"

            version = str(values.get("version") or values.get("版本") or "").strip()
            if not version:
                version = f"v{max(int(str(row['version']).lstrip('v')) if str(row['version']).lstrip('v').isdigit() else 0 for row in self._config_versions) + 1}"
            if any(str(row["version"]).casefold() == version.casefold() for row in self._config_versions):
                return None, f"配置版本 {version} 已存在，本次修改拒绝落库"

            for row in self._config_versions:
                if row.get("active"):
                    row["active"] = False
                    row["status"] = "历史"
            entry = {
                "id": _next_id(self._config_versions),
                "version": version,
                "mapping": mapping,
                "input_format": str(values.get("input_format") or values.get("输入格式") or "json"),
                "status": "生效中",
                "active": True,
                "created_at": now_text(),
                "note": str(values.get("note") or values.get("说明") or ""),
            }
            self._config_versions.append(entry)
            return _deep(entry), f"解析配置版本 {version} 已发布，后续上传将绑定该版本"

    # ------------------------------------------------------------------ 内容与记录
    @staticmethod
    def _extract_content(values: dict[str, Any]) -> tuple[str, bytes]:
        encoded = values.get("content_base64") or values.get("base64")
        if encoded:
            raw = base64.b64decode(str(encoded))
            return raw.decode("utf-8", errors="replace"), raw
        content = values.get("content")
        if content is None and "文件内容" in values:
            content = values["文件内容"]
        if content is None:
            raise ValueError("缺少文件内容，无法计算文件指纹")
        if isinstance(content, (dict, list)):
            text = json.dumps(content, ensure_ascii=False)
        else:
            text = str(content)
        return text, text.encode("utf-8")

    def _latest_same_hash(self, digest: str) -> dict[str, Any] | None:
        matches = [row for row in self._fingerprints if row.get("文件指纹") == digest]
        return matches[-1] if matches else None

    def _latest_same_name(self, file_name: str) -> dict[str, Any] | None:
        matches = [row for row in self._fingerprints if row.get("文件名") == file_name]
        return matches[-1] if matches else None

    def _allocate_receipt(self, business: str, *, file_name: str = "", digest: str = "",
                          fingerprint_id: int | None = None, diff_id: int | None = None,
                          detail: str = "") -> dict[str, Any]:
        receipt_id = _next_id(self._receipts)
        entry = {
            "id": receipt_id,
            "回执编号": f"RCPT-{receipt_id:06d}",
            "业务类型": business,
            "操作": business,
            "文件名": file_name,
            "文件指纹": digest,
            "指纹结果编号": fingerprint_id,
            "映射差异编号": diff_id,
            "结果": "成功",
            "说明": detail,
            "可回读": True,
            "创建时间": now_text(),
        }
        self._receipts.append(entry)
        return entry

    def create_receipt(self, business: str, *, file_name: str = "", digest: str = "",
                       fingerprint_id: int | None = None, diff_id: int | None = None,
                       detail: str = "") -> dict[str, Any]:
        with self._lock:
            return _deep(self._allocate_receipt(
                business, file_name=file_name, digest=digest,
                fingerprint_id=fingerprint_id, diff_id=diff_id, detail=detail,
            ))

    def upload(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """导入一份文件，返回指纹结果；指纹命中时不执行字段转换。"""
        with self._lock:
            try:
                content, raw_bytes = self._extract_content(values)
            except (ValueError, binascii.Error) as error:
                return None, str(error)
            if not raw_bytes:
                return None, "文件内容为空，无法计算文件指纹"
            file_name = str(
                values.get("file_name") or values.get("文件名") or "upload.json"
            ).strip() or "upload.json"
            token = str(
                values.get("client_token") or values.get("request_id") or values.get("提交令牌") or ""
            ).strip()
            if token and token in self._tokens:
                return None, (
                    f"重复提交：令牌 {token} 已于记录 {self._tokens[token]} 落库，"
                    "本次提交拒绝写入"
                )
            try:
                config = self._resolve_config(values)
            except ValueError as error:
                return None, str(error)
            mapping = normalize_mapping(config.get("mapping"))
            input_format = detect_format(
                file_name, content, config.get("input_format") or values.get("input_format")
            )
            strategy = normalize_strategy(values.get("strategy") or values.get("复用策略"))
            digest = hashlib.sha256(raw_bytes).hexdigest()
            same_hash = self._latest_same_hash(digest)
            same_name = self._latest_same_name(file_name)
            reference = same_hash or same_name
            old_mapping = normalize_mapping(reference.get("映射快照")) if reference else {}
            mapping_changed = bool(reference) and _canonical(old_mapping) != _canonical(mapping)
            content_changed = bool(same_name) and same_name.get("文件指纹") != digest
            version_changed = bool(reference) and str(reference.get("结果版本")) != str(config["version"])

            if same_hash is None:
                reuse = False
            elif mapping_changed:
                reuse = strategy == STRATEGY_FINGERPRINT_FIRST
            else:
                # 指纹相同、映射也相同，直接复用，避免重复转换全部字段。
                reuse = True

            started = time.perf_counter()
            if reuse:
                fields = _deep(same_hash.get("字段结果") or [])
                parse_ms = 0
                result_version = same_hash.get("结果版本")
                root_id = same_hash.get("结果根编号") or same_hash.get("id")
                root_version = same_hash.get("结果根版本") or same_hash.get("结果版本")
                # 复用结果由旧映射产生，快照必须跟着结果走，避免下一次比较基准偏移。
                result_mapping = normalize_mapping(same_hash.get("映射快照")) or _deep(mapping)
                result_source = "复用"
                if mapping_changed:
                    hit_type = "指纹命中-映射变更复用旧结果"
                elif version_changed:
                    hit_type = "指纹命中-配置版本变化复用"
                else:
                    hit_type = "指纹命中-同版本复用"
                saved_ms = int(same_hash.get("解析耗时毫秒") or 0)
            else:
                try:
                    fields = parse_records(content, file_name, input_format, mapping)
                except (json.JSONDecodeError, csv.Error, UnicodeError) as error:
                    return None, f"文件内容无法按 {input_format} 解析：{error}"
                parse_ms = max(1, int((time.perf_counter() - started) * 1000))
                result_version = config["version"]
                result_mapping = _deep(mapping)
                result_source = "重解析" if reference else "首次解析"
                root_id = None  # 落库拿到 id 后补
                root_version = result_version
                saved_ms = 0
                if same_hash is not None and mapping_changed:
                    hit_type = "映射变更-按新映射重解析"
                elif same_hash is not None:
                    hit_type = "指纹命中-强制重解析"
                elif content_changed:
                    hit_type = "内容变化重解析"
                else:
                    hit_type = "首次解析"

            if same_hash is not None and not mapping_changed and not version_changed:
                diff_type = "无变化"
            elif mapping_changed and reuse:
                diff_type = "映射变更-复用旧字段结果"
            elif mapping_changed:
                diff_type = "映射变更-按新映射重解析"
            elif content_changed:
                diff_type = "内容变化-重新解析"
            elif version_changed:
                diff_type = "配置版本变化-字段映射一致"
            else:
                diff_type = "首次解析"
            details = mapping_diff_details(old_mapping, mapping)
            if not reference:
                details = mapping_diff_details({}, mapping)

            fingerprint_id = _next_id(self._fingerprints)
            diff_id = _next_id(self._diffs)
            receipt = self._allocate_receipt(
                "文件导入",
                file_name=file_name,
                digest=digest,
                fingerprint_id=fingerprint_id,
                diff_id=diff_id,
                detail=f"{hit_type}；结果版本 {result_version}",
            )

            diff_entry = {
                "id": diff_id,
                "差异编号": f"DIFF-{diff_id:06d}",
                "文件指纹": digest,
                "指纹结果编号": fingerprint_id,
                "文件名": file_name,
                "旧配置版本": reference.get("结果版本") if reference else None,
                "新配置版本": config["version"],
                "差异类型": diff_type,
                "变更字段数": len(details),
                "明细": details,
                "映射变更": mapping_changed,
                "处理决策": "复用旧结果" if reuse else "重新解析",
                "可导出": True,
                "处理回执号": receipt["回执编号"],
                "生成时间": now_text(),
            }
            self._diffs.append(diff_entry)

            entry = {
                "id": fingerprint_id,
                "指纹编号": f"FP-{fingerprint_id:06d}",
                "文件名": file_name,
                "文件指纹": digest,
                "内容指纹": digest,
                "文件大小": len(raw_bytes),
                "输入格式": input_format,
                "解析配置版本": config["version"],
                "请求配置版本": config["version"],
                "结果版本": result_version,
                "结果来源": result_source,
                "是否复用": reuse,
                "指纹命中": same_hash is not None,
                "是否重解析": not reuse,
                "命中类型": hit_type,
                "复用来源编号": same_hash.get("id") if reuse and same_hash else None,
                "旧结果版本": reference.get("结果版本") if reference else None,
                "旧字段结果": _deep(reference.get("字段结果")) if reference else None,
                "字段结果": fields,
                "解析结果": fields,
                "字段数": len(fields[0]) if fields and isinstance(fields[0], dict) else len(result_mapping),
                "记录数": len(fields),
                "映射快照": _deep(result_mapping),
                "请求映射快照": _deep(mapping),
                "映射变更": mapping_changed,
                "内容变化": content_changed,
                "策略": strategy,
                "策略名称": STRATEGY_LABELS[strategy],
                "映射差异编号": diff_id,
                "映射差异": diff_entry,
                "处理回执号": receipt["回执编号"],
                "结果根编号": root_id if root_id is not None else fingerprint_id,
                "结果根版本": root_version,
                "状态": "已解析",
                "校验状态": "待校验",
                "可追溯": True,
                "解析耗时毫秒": parse_ms,
                "复用节省毫秒": saved_ms,
                "创建时间": now_text(),
            }
            self._fingerprints.append(entry)
            if token:
                self._tokens[token] = fingerprint_id

            if reuse:
                message = (
                    f"指纹命中，已复用 {same_hash['指纹编号']} 的字段结果"
                    f"（结果版本 {result_version}）；未重复执行字段转换"
                )
            elif content_changed:
                message = f"内容已变化，文件指纹不一致，已按新内容重新解析（结果版本 {result_version}）"
            elif reference and mapping_changed:
                message = f"映射配置已升级，已按新映射重新解析并保留旧结果（结果版本 {result_version}）"
            else:
                message = f"首次导入，已完成字段解析（结果版本 {result_version}）"
            return _deep(entry), message

    def upload_batch(self, values: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """同一批源文件一起上传；逐份判定指纹复用。"""
        files = values.get("files") or values.get("源文件") or []
        results: list[dict[str, Any]] = []
        messages: list[str] = []
        if not isinstance(files, list) or not files:
            return [], ["批次中缺少源文件"]
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                messages.append(f"第 {index} 份文件格式不正确，已跳过")
                continue
            payload = dict(values)
            payload.pop("files", None)
            payload.pop("源文件", None)
            payload.update(item)
            entry, message = self.upload(payload)
            if entry is None:
                messages.append(f"第 {index} 份文件：{message}")
            else:
                results.append(entry)
                messages.append(f"第 {index} 份文件：{message}")
        return results, messages

    # ------------------------------------------------------------------ 查询
    def list_fingerprints(self, *, page: int = 1, size: int = 20,
                          keyword: str | None = None, status: str | None = None,
                          file_name: str | None = None) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(reversed(self._fingerprints))
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("文件名", "")).casefold()
                or needle in str(row.get("文件指纹", "")).casefold()
                or needle in str(row.get("指纹编号", "")).casefold()
            ]
        if status:
            rows = [row for row in rows if row.get("校验状态") == status]
        if file_name:
            rows = [row for row in rows if file_name in str(row.get("文件名", ""))]
        total = len(rows)
        start = (page - 1) * size
        return _deep(rows[start:start + size]), total

    def get_fingerprint(self, fingerprint_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._fingerprints:
                if int(row.get("id", 0)) == fingerprint_id:
                    return _deep(row)
        return None

    def list_diffs(self, *, page: int = 1, size: int = 20,
                   keyword: str | None = None) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(reversed(self._diffs))
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("文件名", "")).casefold()
                or needle in str(row.get("文件指纹", "")).casefold()
                or needle in str(row.get("差异编号", "")).casefold()
            ]
        total = len(rows)
        start = (page - 1) * size
        return _deep(rows[start:start + size]), total

    def get_diff(self, diff_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._diffs:
                if int(row.get("id", 0)) == diff_id:
                    return _deep(row)
        return None

    def get_diff_for_fingerprint(self, fingerprint_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in reversed(self._diffs):
                if int(row.get("指纹结果编号") or 0) == fingerprint_id:
                    return _deep(row)
        return None

    def list_receipts(self, *, page: int = 1, size: int = 20,
                      business: str | None = None) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(reversed(self._receipts))
        if business:
            rows = [row for row in rows if business in str(row.get("业务类型", ""))]
        total = len(rows)
        start = (page - 1) * size
        return _deep(rows[start:start + size]), total

    def get_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._receipts:
                if int(row.get("id", 0)) == receipt_id:
                    return _deep(row)
        return None

    def mark_validation(self, fingerprint_id: int, status: str,
                        record_id: int | None = None) -> None:
        with self._lock:
            for row in self._fingerprints:
                if int(row.get("id", 0)) == fingerprint_id:
                    row["校验状态"] = status
                    if record_id is not None:
                        row["校验记录编号"] = record_id
                    break

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = len(self._fingerprints)
            parsed = sum(1 for row in self._fingerprints if row.get("是否重解析"))
            reused = sum(1 for row in self._fingerprints if row.get("是否复用"))
            conflicts = sum(1 for row in self._fingerprints if row.get("映射变更"))
            saved = sum(int(row.get("复用节省毫秒") or 0) for row in self._fingerprints)
            pending = sum(1 for row in self._fingerprints if row.get("校验状态") == "待校验")
            return {
                "文件导入次数": total,
                "解析执行次数": parsed,
                "指纹命中次数": reused,
                "复用次数": reused,
                "避免重复解析": reused,
                "节省耗时毫秒": saved,
                "映射冲突数": conflicts,
                "映射差异文件数": len(self._diffs),
                "处理回执数": len(self._receipts),
                "待校验数": pending,
                "历史记录数": total,
                "配置版本数": len(self._config_versions),
            }

    # ------------------------------------------------------------------ 导出/打包
    def export_fingerprints_csv(self) -> tuple[str, dict[str, Any]]:
        with self._lock:
            rows = _deep(list(self._fingerprints))
            receipt = self._allocate_receipt(
                "导出", detail=f"导出解析指纹结果 {len(rows)} 条", digest="",
            )
        headers = [
            "id", "指纹编号", "文件名", "文件指纹", "解析配置版本", "结果版本",
            "结果来源", "是否复用", "命中类型", "字段数", "记录数",
            "映射差异编号", "处理回执号", "结果根版本", "校验状态", "创建时间",
        ]
        body = [[
            row.get("id"), row.get("指纹编号"), row.get("文件名"), row.get("文件指纹"),
            row.get("解析配置版本"), row.get("结果版本"), row.get("结果来源"),
            "是" if row.get("是否复用") else "否", row.get("命中类型"),
            row.get("字段数"), row.get("记录数"), row.get("映射差异编号"),
            row.get("处理回执号"), row.get("结果根版本"), row.get("校验状态"),
            row.get("创建时间"),
        ] for row in rows]
        return _csv_text(headers, body), receipt

    def export_diffs_csv(self) -> tuple[str, dict[str, Any]]:
        with self._lock:
            rows = _deep(list(self._diffs))
            receipt = self._allocate_receipt(
                "导出", detail=f"导出映射差异文件 {len(rows)} 条", digest="",
            )
        headers = [
            "id", "差异编号", "文件名", "文件指纹", "指纹结果编号", "旧配置版本",
            "新配置版本", "差异类型", "变更字段数", "字段", "旧映射", "新映射",
            "明细差异类型", "处理决策", "处理回执号", "生成时间",
        ]
        body: list[list[Any]] = []
        for row in rows:
            details = row.get("明细") or [{}]
            for detail in details:
                body.append([
                    row.get("id"), row.get("差异编号"), row.get("文件名"),
                    row.get("文件指纹"), row.get("指纹结果编号"),
                    row.get("旧配置版本"), row.get("新配置版本"),
                    row.get("差异类型"), row.get("变更字段数"),
                    detail.get("字段"), detail.get("旧映射"), detail.get("新映射"),
                    detail.get("差异类型"), row.get("处理决策"),
                    row.get("处理回执号"), row.get("生成时间"),
                ])
        return _csv_text(headers, body), receipt

    def build_package(self) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        with self._lock:
            fingerprints = _deep(list(self._fingerprints))
            diffs = _deep(list(self._diffs))
            receipts = _deep(list(self._receipts))
            receipt = self._allocate_receipt(
                "打包", detail="打包解析指纹结果、映射差异与处理回执",
            )
        fingerprint_csv, _ = _csv_text(
            ["id", "指纹编号", "文件名", "文件指纹", "解析配置版本", "结果版本",
             "结果来源", "是否复用", "处理回执号", "校验状态"],
            [[row.get("id"), row.get("指纹编号"), row.get("文件名"), row.get("文件指纹"),
              row.get("解析配置版本"), row.get("结果版本"), row.get("结果来源"),
              "是" if row.get("是否复用") else "否", row.get("处理回执号"),
              row.get("校验状态")] for row in fingerprints],
        ), receipt
        diff_csv, _ = _csv_text(
            ["id", "差异编号", "文件指纹", "指纹结果编号", "旧配置版本", "新配置版本",
             "差异类型", "变更字段数", "处理回执号"],
            [[row.get("id"), row.get("差异编号"), row.get("文件指纹"),
              row.get("指纹结果编号"), row.get("旧配置版本"), row.get("新配置版本"),
              row.get("差异类型"), row.get("变更字段数"), row.get("处理回执号")]
             for row in diffs],
        ), receipt
        manifest = {
            "包名": "parser-parse-package.zip",
            "解析指纹结果数": len(fingerprints),
            "映射差异文件数": len(diffs),
            "处理回执数": len(receipts) + 1,
            "本次打包回执号": receipt["回执编号"],
            "生成时间": now_text(),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("解析指纹结果.csv", fingerprint_csv)
            archive.writestr("映射差异文件.csv", diff_csv)
            archive.writestr(
                "处理回执.json",
                json.dumps(receipts + [receipt], ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        return buffer.getvalue(), receipt, manifest


pipeline = ParsePipelineService()
