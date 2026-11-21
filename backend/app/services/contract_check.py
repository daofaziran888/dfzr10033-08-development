"""逐列契约校验领域服务。

提供两块能力：

1. 纯函数校验引擎：把数据契约的 Schema 约束（列类型、必填、依赖列、必填度量）
   与语义模型的字段别名映射、度量公式逐列对照，产出逐列检查项；
2. ``contract_check`` 表的读写服务：持久化逐列契约校验报告，并承接投产结果回写。

设计要点（对应业务规则）：

- 别名能映射到同一源列时，按 ``别名 -> 源列`` 映射校验，多个别名映射同一源列都认可；
- 必填度量的输入列在模型中无映射时属于硬阻断，即使列通过率达到契约阈值也不能判通过；
- 报告按契约版本冻结，投产复查永远对照报告生成时的同一版约束。
"""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# SQL 函数/关键字，解析度量公式输入列时剔除
_SQL_KEYWORDS = {
    "SUM", "COUNT", "AVG", "MIN", "MAX", "DISTINCT", "CASE", "WHEN", "THEN",
    "ELSE", "END", "CAST", "COALESCE", "ROUND", "IF", "NULL", "AS", "FROM",
    "SELECT", "WHERE", "GROUP", "BY", "ORDER", "AND", "OR", "NOT",
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------

def split_csv(value: Any) -> list[str]:
    """把逗号分隔字符串拆成去空白后的非空列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def parse_structured(value: Any, default: Any) -> Any:
    """兼容三种录入形态：原生 dict/list、JSON 字符串、``a:b,c:d`` 简表字符串。"""
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    text = str(value).strip()
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    # 简表：a:b,c:d
    if ":" in text:
        result: dict[str, str] = {}
        for pair in text.split(","):
            if ":" not in pair:
                continue
            key, _, val = pair.partition(":")
            key, val = key.strip(), val.strip()
            if key:
                result[key] = val
        if result:
            return result
    return default


def parse_alias_mapping(model: dict[str, Any]) -> dict[str, str]:
    """返回 别名 -> 源列 的映射；未显式声明时按别名即源列名做恒等映射。"""
    mapping = parse_structured(model.get("源列映射"), {})
    if not isinstance(mapping, dict):
        mapping = {}
    mapping = {str(k).strip(): str(v).strip() for k, v in mapping.items() if str(k).strip()}
    # 未显式声明映射的别名，默认映射到同名列
    for alias in split_csv(model.get("字段别名", "")):
        mapping.setdefault(alias, alias)
    return mapping


def parse_type_map(model: dict[str, Any]) -> dict[str, str]:
    """返回 别名/源列 -> 物理类型 的映射。"""
    type_map = parse_structured(model.get("字段类型"), {})
    if not isinstance(type_map, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in type_map.items() if str(k).strip()}


def parse_measures(model: dict[str, Any]) -> list[dict[str, Any]]:
    """解析度量定义，支持 ``gmv=SUM(order_amount)`` 与裸表达式两种写法。"""
    measures: list[dict[str, Any]] = []
    explicit_inputs = parse_structured(model.get("度量输入"), {})
    for raw in split_csv(model.get("度量定义", "")):
        expr = raw
        name = raw
        if "=" in raw and not raw.startswith(("SUM(", "COUNT(", "AVG(", "MIN(", "MAX(")):
            maybe_name, _, maybe_expr = raw.partition("=")
            maybe_name, maybe_expr = maybe_name.strip(), maybe_expr.strip()
            if maybe_name and maybe_expr:
                name, expr = maybe_name, maybe_expr
        tokens = {t for t in _IDENT_RE.findall(expr) if t.upper() not in _SQL_KEYWORDS}
        inputs = parse_structured(explicit_inputs, {}).get(name) if isinstance(explicit_inputs, dict) else None
        measures.append({
            "名称": name,
            "表达式": expr,
            "输入列": split_csv(inputs) if inputs else sorted(tokens),
        })
    return measures


def parse_contract_columns(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """返回契约的结构化列约束；缺省时由 Schema约束 字符串退化为仅列名约束。"""
    specs = parse_structured(contract.get("列约束"), [])
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list):
        specs = []
    normalized: list[dict[str, Any]] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        item = dict(spec)
        item.setdefault("列名", item.get("度量名", ""))
        item.setdefault("类型", "")
        item.setdefault("必填", False)
        item.setdefault("依赖列", [])
        item.setdefault("类别", "度量" if item.get("度量名") else "列")
        if item.get("类别") == "度量":
            item["列名"] = item.get("度量名") or item.get("列名", "")
        item["依赖列"] = split_csv(item.get("依赖列"))
        normalized.append(item)
    if normalized:
        return normalized
    return [
        {"列名": col, "类型": "", "必填": False, "依赖列": [], "类别": "列"}
        for col in split_csv(contract.get("Schema约束", ""))
    ]


def normalize_type(type_name: Any) -> str:
    return re.sub(r"\s+", "", str(type_name or "")).casefold()


# ---------------------------------------------------------------------------
# 校验引擎
# ---------------------------------------------------------------------------

def run_column_check(
    contract: dict[str, Any],
    model: dict[str, Any],
    *,
    contract_version: str,
    semantic_version: str,
) -> dict[str, Any]:
    """逐列对照契约约束与语义模型定义，返回报告头与逐列检查项。

    检查项类别：
      - ``列``：源列映射存在性、类型一致性、依赖列齐备性；
      - ``度量``：必填度量是否存在、输入列是否全部可映射（硬阻断）。
    """
    alias_mapping = parse_alias_mapping(model)
    source_to_aliases: dict[str, list[str]] = {}
    for alias, source in alias_mapping.items():
        source_to_aliases.setdefault(source, []).append(alias)
    available_sources = set(alias_mapping.values())
    type_map = parse_type_map(model)
    measures = parse_measures(model)
    measure_by_name = {m["名称"]: m for m in measures}
    specs = parse_contract_columns(contract)
    threshold = float(contract.get("契约阈值", 1.0) or 1.0)

    items: list[dict[str, Any]] = []
    column_total = 0
    column_passed = 0
    hard_blocks: list[str] = []

    for spec in specs:
        category = spec.get("类别", "列")
        if category == "度量":
            item = _check_measure(spec, measure_by_name, available_sources, model)
        else:
            column_total += 1
            item = _check_column(spec, alias_mapping, source_to_aliases, type_map, available_sources)
            if item["通过"]:
                column_passed += 1
        if item["硬阻断"]:
            hard_blocks.extend(item["阻断原因"])
        items.append(item)

    pass_rate = (column_passed / column_total) if column_total else 1.0
    threshold_met = pass_rate + 1e-9 >= threshold
    overall_passed = threshold_met and not hard_blocks

    return {
        "报告编号": f"CCR-{uuid.uuid4().hex[:8]}",
        "契约id": contract.get("id"),
        "契约编号": contract.get("契约编号", ""),
        "模型id": model.get("id"),
        "模型编号": model.get("模型编号", ""),
        "契约版本": contract_version,
        "语义版本": semantic_version,
        "报告时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "契约阈值": threshold,
        "通过率": round(pass_rate, 4),
        "阈值达标": threshold_met,
        "总体通过": overall_passed,
        "硬阻断": hard_blocks,
        "检查项": items,
    }


def _resolve_alias(column: str, alias_mapping: dict[str, str],
                   source_to_aliases: dict[str, list[str]]) -> tuple[str, str]:
    """找到契约列在模型中的别名与映射源列；找不到返回空串。"""
    # 1) 源列名直接命中（别名映射到同一源列即认可）
    aliases = source_to_aliases.get(column)
    if aliases:
        return aliases[0], column
    # 2) 别名与契约列同名
    if column in alias_mapping:
        return column, alias_mapping[column]
    return "", ""


def _check_column(spec: dict[str, Any], alias_mapping: dict[str, str],
                  source_to_aliases: dict[str, list[str]], type_map: dict[str, str],
                  available_sources: set[str]) -> dict[str, Any]:
    column = str(spec.get("列名", "")).strip()
    required = bool(spec.get("必填"))
    reasons: list[str] = []
    hard_block = False

    alias, source = _resolve_alias(column, alias_mapping, source_to_aliases)
    if not source:
        reasons.append(f"契约列[{column}]在语义模型中无别名映射到源列")
        if required:
            hard_block = True
    else:
        # 类型校验：模型未声明类型时跳过，双方都声明时必须一致
        expected = str(spec.get("类型", "")).strip()
        actual = type_map.get(alias) or type_map.get(source) or ""
        if expected and actual and normalize_type(expected) != normalize_type(actual):
            reasons.append(f"列[{column}]类型不匹配: 期望{expected} 实际{actual}")

    # 依赖列校验：依赖源列必须在模型中可映射
    missing_deps = [dep for dep in spec.get("依赖列", []) if dep not in available_sources]
    if missing_deps:
        reasons.append(f"列[{column}]依赖列缺失: {','.join(missing_deps)}")
        if required:
            hard_block = True

    return {
        "检查对象": column,
        "对象类别": "列",
        "语义别名": alias,
        "映射源列": source,
        "期望类型": str(spec.get("类型", "")),
        "实际类型": (type_map.get(alias) or type_map.get(source) or "") if source else "",
        "必填": required,
        "依赖列": list(spec.get("依赖列", [])),
        "通过": not reasons,
        "硬阻断": hard_block,
        "阻断原因": reasons,
    }


def _check_measure(spec: dict[str, Any], measure_by_name: dict[str, dict[str, Any]],
                   available_sources: set[str], model: dict[str, Any]) -> dict[str, Any]:
    name = str(spec.get("度量名") or spec.get("列名") or "").strip()
    required = bool(spec.get("必填", True))
    reasons: list[str] = []
    measure = measure_by_name.get(name)
    inputs: list[str] = []
    if measure is None:
        if required:
            reasons.append(f"必填度量[{name}]在语义模型度量定义中缺失")
    else:
        inputs = list(measure["输入列"])
        missing = [col for col in inputs if col not in available_sources]
        if missing:
            reasons.append(f"必填度量[{name}]依赖缺列: {','.join(missing)}")
            # 缺列会破坏下游衍生指标公式，附带受影响的下游指标
            for downstream in _downstream_metrics(missing, model):
                reasons.append(
                    f"缺列影响下游衍生指标[{downstream['指标编号']}]公式: {downstream['计算公式']}"
                )
    # 度量问题一律按硬阻断处理：必填度量缺列绝不能被判成通过
    hard_block = bool(reasons) and required
    return {
        "检查对象": name,
        "对象类别": "度量",
        "语义别名": measure["名称"] if measure else "",
        "映射源列": ",".join(inputs),
        "期望类型": str(spec.get("类型", "")),
        "实际类型": "",
        "必填": required,
        "依赖列": inputs,
        "通过": not reasons,
        "硬阻断": hard_block,
        "阻断原因": reasons,
    }


def _downstream_metrics(missing_columns: list[str], model: dict[str, Any]) -> list[dict[str, Any]]:
    """扫描指标工厂，找出公式引用了缺列的下游衍生指标。"""
    affected: list[dict[str, Any]] = []
    missing_set = set(missing_columns)
    for metric in store.rows("metric_factory"):
        formula = str(metric.get("计算公式", ""))
        referenced = set(_IDENT_RE.findall(formula)) - _SQL_KEYWORDS
        if referenced & missing_set:
            affected.append({
                "指标编号": metric.get("指标编号", ""),
                "计算公式": formula,
            })
    return affected


# ---------------------------------------------------------------------------
# 报告表服务
# ---------------------------------------------------------------------------

class ContractCheckService(BaseModuleService):
    MODULE = "contract_check"
    ENTITY = "逐列契约校验报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "报告编号", "契约编号", "模型编号", "契约版本", "语义版本",
        "检查对象", "对象类别", "映射源列", "约束结果", "阻断原因", "投产结论",
    ]
    REQUIRED_FIELDS = ["报告编号", "契约编号", "模型编号", "检查对象"]
    STATUS_ORDER = ["待投产", "已确认", "已阻断"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def persist_report(self, report: dict[str, Any]) -> str:
        """把一份报告的逐列检查项落表，所有行共享同一报告编号。"""
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        report_no = report["报告编号"]
        for item in report["检查项"]:
            rows.append({
                "id": next_id,
                "报告编号": report_no,
                "契约id": report["契约id"],
                "契约编号": report["契约编号"],
                "模型id": report["模型id"],
                "模型编号": report["模型编号"],
                "契约版本": report["契约版本"],
                "语义版本": report["语义版本"],
                "报告时间": report["报告时间"],
                "检查对象": item["检查对象"],
                "对象类别": item["对象类别"],
                "语义别名": item["语义别名"],
                "映射源列": item["映射源列"],
                "期望类型": item["期望类型"],
                "实际类型": item["实际类型"],
                "必填": item["必填"],
                "依赖列": ",".join(item["依赖列"]),
                "约束结果": "通过" if item["通过"] else "未通过",
                "硬阻断": item["硬阻断"],
                "阻断原因": "；".join(item["阻断原因"]),
                "通过率": report["通过率"],
                "契约阈值": report["契约阈值"],
                "总体通过": report["总体通过"],
                "投产结论": "待投产",
                "投产编号": "",
                "可查询": False,
                "status": "待投产",
                "pending": True,
                "abnormal": item["硬阻断"],
            })
            next_id += 1
        return report_no

    def get_report(self, report_no: str) -> dict[str, Any] | None:
        """按报告编号聚合报告头与逐列检查项。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("报告编号") == report_no]
        if not rows:
            return None
        head = rows[0]
        return {
            "报告编号": report_no,
            "契约编号": head.get("契约编号"),
            "模型编号": head.get("模型编号"),
            "契约版本": head.get("契约版本"),
            "语义版本": head.get("语义版本"),
            "报告时间": head.get("报告时间"),
            "通过率": head.get("通过率"),
            "契约阈值": head.get("契约阈值"),
            "总体通过": head.get("总体通过"),
            "检查项": [
                {
                    "检查对象": r.get("检查对象"),
                    "对象类别": r.get("对象类别"),
                    "语义别名": r.get("语义别名"),
                    "映射源列": r.get("映射源列"),
                    "期望类型": r.get("期望类型"),
                    "实际类型": r.get("实际类型"),
                    "必填": r.get("必填"),
                    "依赖列": r.get("依赖列"),
                    "约束结果": r.get("约束结果"),
                    "硬阻断": r.get("硬阻断"),
                    "阻断原因": r.get("阻断原因"),
                    "投产结论": r.get("投产结论"),
                    "可查询": r.get("可查询"),
                }
                for r in rows
            ],
        }

    def writeback_promotion(
        self,
        report_no: str,
        promotion_no: str,
        conclusion: str,
        queryable_objects: set[str],
    ) -> None:
        """投产结果回写到契约检查清单：结论、投产编号、可查询标记。"""
        for row in store.rows(self.MODULE):
            if row.get("报告编号") != report_no:
                continue
            row["投产结论"] = conclusion
            row["投产编号"] = promotion_no
            row["status"] = conclusion
            row["pending"] = conclusion == "待投产"
            row["abnormal"] = conclusion == "已阻断"
            # 进入可查询范围的列和度量必须是同一版约束下校验通过的对象
            row["可查询"] = conclusion == "已确认" and row.get("检查对象") in queryable_objects
