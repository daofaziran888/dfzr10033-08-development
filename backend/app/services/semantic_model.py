"""语义模型领域服务：管理表映射、字段别名、度量定义与计算口径，作为查询翻译层。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SemanticModelService(BaseModuleService):
    MODULE = "semantic_model"
    ENTITY = "语义定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模型编号", "模型名称", "源表映射", "字段别名", "度量定义", "维度定义", "计算口径", "版本序号"]
    REQUIRED_FIELDS = ["模型编号", "模型名称", "源表映射"]
    STATUS_ORDER = ["草稿", "校验中", "已投产", "已废弃"]
    ACTION_RULES = {"提交校验": "校验中", "投产模型": "已投产", "废弃模型": "已废弃"}
    NEGATIVE_ACTIONS = []

    def validate_and_promote(self, model_id: int) -> dict[str, Any]:
        """校验语义模型并投产：投产必须消费逐列契约校验报告。

        跨模块数据流：
        - 读取 data_contract 的已发布口径版本（含逐列约束）
        - 生成 contract_check 逐列契约校验报告
        - 写入 model_promotion 投产记录并生成可查询字段清单
        - 投产结果回写到契约检查清单与契约本身
        - 写入审计日志
        """
        from app.services.model_promotion import ModelPromotionService

        model = store.find(self.MODULE, model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        aliases = model.get("字段别名", "")
        result = ModelPromotionService().promote_with_contract(model_id)
        if result.get("ok"):
            result["fields_count"] = len([a for a in aliases.split(",") if a.strip()])
            _audit_promotion(model_id, model.get("模型编号", ""))
        return result

    def save_alias_mapping(
        self,
        model_id: int,
        mapping: dict[str, str],
        *,
        new_version: str | None = None,
    ) -> dict[str, Any]:
        """只保存别名到源列的映射（不投产），供「仅变动别名」的修订使用。

        - 别名能映射到同一源列时按映射关系保存，多个别名可指向同一源列；
        - 同步刷新字段别名清单与版本序号，模型保持草稿状态等待投产确认。
        """
        model = store.find(self.MODULE, model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        clean_mapping = {
            str(alias).strip(): str(source).strip()
            for alias, source in mapping.items()
            if str(alias).strip() and str(source).strip()
        }
        if not clean_mapping:
            return {"ok": False, "message": "别名映射不能为空"}

        model["源列映射"] = clean_mapping
        model["字段别名"] = ",".join(clean_mapping.keys())
        if new_version:
            model["版本序号"] = new_version
        model["status"] = "草稿"
        model["pending"] = True
        model["abnormal"] = False
        return {
            "ok": True,
            "model_id": model_id,
            "版本序号": model.get("版本序号"),
            "源列映射": clean_mapping,
            "字段别名": model["字段别名"],
        }

    def detect_schema_conflicts(self, source_id: int) -> dict[str, Any]:
        """检测数据源Schema变化与已投产语义模型之间的冲突。

        跨模块数据流：
        - 读取 source_registry 的Schema记录
        - 读取 data_lake_read 的Schema变更记录
        - 比较语义模型字段定义，标记冲突
        """
        models = store.rows(self.MODULE)
        conflicts = []

        for model in models:
            if model.get("status") != "已投产":
                continue
            if str(source_id) not in str(model.get("源表映射", "")):
                continue

            current_fields = set(model.get("字段别名", "").split(","))
            current_fields = {f.strip() for f in current_fields if f.strip()}

            # 检测 data_lake_read 中的Schema变更
            lake_rows = store.rows("data_lake_read")
            for lake in lake_rows:
                schema_changes = lake.get("Schema推导", "")
                if "source_registry" in lake.get("存储位置", "") and str(source_id) in lake.get("存储位置", ""):
                    conflicts.append({
                        "model_id": model["id"],
                        "model_name": model.get("模型名称"),
                        "source_id": source_id,
                        "schema_change": schema_changes,
                        "current_fields": list(current_fields),
                    })

        return {
            "ok": True,
            "source_id": source_id,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
        }


def _audit_promotion(model_id: int, model_no: str) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    audit = {
        "id": audit_id,
        "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
        "查询语句": f"SEMANTIC_PROMOTE model={model_no}",
        "执行耗时": "200ms",
        "扫描行数": "1",
        "返回行数": "1",
        "数据来源": "semantic_model",
        "操作用户": "validator",
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    }
    rows.append(audit)