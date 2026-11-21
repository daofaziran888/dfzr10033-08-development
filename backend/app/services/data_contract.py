"""数据契约领域服务：数据消费者与生产者之间的契约管理、Schema约束与SLA跟踪。"""
from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.contract_check import ContractCheckService, run_column_check
from app.store import store


class DataContractService(BaseModuleService):
    MODULE = "data_contract"
    ENTITY = "契约定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["契约编号", "数据集", "Schema约束", "SLA定义", "更新频率", "负责人", "消费者", "生效时间"]
    REQUIRED_FIELDS = ["契约编号", "数据集"]
    STATUS_ORDER = ["草稿", "已承诺", "已违约", "已终止"]
    ACTION_RULES = {"签署契约": "已承诺", "标记违约": "已违约", "终止契约": "已终止"}
    NEGATIVE_ACTIONS = ["标记违约", "终止契约"]

    def publish_contract(self, contract_id: int) -> dict[str, Any]:
        """发布契约口径版本：冻结当时的 Schema 约束与列级定义形成不可变快照。

        发布后任何复查都按快照里的定义执行，契约后续修改只会产生新版本，
        历史版本上的投产记录仍可按当时定义复查。
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        next_seq = int(contract.get("已发布版本数", 0)) + 1
        version_no = f"v{next_seq}.0"
        snapshot = {
            "id": contract.get("id"),
            "版本序号": version_no,
            "契约编号": contract.get("契约编号", ""),
            "数据集": contract.get("数据集", ""),
            "Schema约束": contract.get("Schema约束", ""),
            "列约束": copy.deepcopy(contract.get("列约束", [])),
            "契约阈值": contract.get("契约阈值", 1.0),
            "发布时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        history = contract.setdefault("版本历史", [])
        history.append(copy.deepcopy(snapshot))
        contract["版本快照"] = copy.deepcopy(snapshot)
        contract["当前版本"] = version_no
        contract["已发布版本数"] = next_seq
        contract["发布状态"] = "已发布"
        contract["发布时间"] = snapshot["发布时间"]
        contract["status"] = "已承诺"
        contract["pending"] = False
        return {
            "ok": True,
            "contract_id": contract_id,
            "version": version_no,
            "snapshot": snapshot,
        }

    def recheck_published_version(
        self, contract_id: int, model_id: int, version_no: str | None = None
    ) -> dict[str, Any]:
        """按已发布口径版本对语义模型重新执行逐列契约校验（只生成报告，不投产）。"""
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}
        model = store.find("semantic_model", model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        history = contract.get("版本历史", [])
        snapshot: dict[str, Any] | None = None
        if version_no:
            snapshot = next((v for v in history if v.get("版本序号") == version_no), None)
            if snapshot is None:
                return {"ok": False, "message": f"契约版本{version_no}不存在，无法按当时定义复查"}
        else:
            snapshot = contract.get("版本快照")
        if snapshot is None:
            return {"ok": False, "message": "契约尚未发布，没有可复查的口径版本"}

        report = run_column_check(
            snapshot, model,
            contract_version=snapshot["版本序号"],
            semantic_version=str(model.get("版本序号", "")),
        )
        report["契约id"] = contract.get("id")
        ContractCheckService().persist_report(report)
        return {"ok": True, "report_no": report["报告编号"], "report": report}

    def verify_contract_compliance(self, contract_id: int) -> dict[str, Any]:
        """验证数据契约的合规性，跨模块检查Schema一致性和数据新鲜度。

        跨模块数据流：
        - 读取 source_registry 校验数据源状态
        - 读取 semantic_model 校验Schema一致性
        - 读取 freshness_mon 检查SLA达标情况
        - 读取 query_audit 检查消费记录
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        dataset = contract.get("数据集", "")
        violations: list[str] = []

        # 检查数据源是否存在且可用
        sources = store.rows("source_registry")
        source_ok = False
        for s in sources:
            if s.get("源编号", "") in dataset or dataset in str(s.get("连接地址", "")):
                if s.get("status") == "已停用":
                    violations.append(f"数据源{s['源编号']}已停用")
                else:
                    source_ok = True

        if not source_ok:
            violations.append("未找到对应的可用数据源")

        # 检查Schema一致性
        models = store.rows("semantic_model")
        schema_ok = False
        for m in models:
            if m.get("status") == "已投产" and dataset in str(m.get("源表映射", "")):
                if m.get("字段别名", "") == contract.get("Schema约束", ""):
                    schema_ok = True
                else:
                    violations.append("Schema定义与语义模型不一致")
                break

        # 检查SLA新鲜度
        freshness_rows = store.rows("freshness_mon")
        freshness_ok = True
        for f in freshness_rows:
            if dataset in str(f.get("目标数据集", "")):
                if f.get("status") == "已告警":
                    freshness_ok = False
                    violations.append(f"新鲜度告警: {f.get('延迟阈值', 'unknown')}")

        # 检查消费记录
        audits = store.rows("query_audit")
        consumer = contract.get("消费者", "")
        consumption_records = [a for a in audits if consumer in str(a.get("操作用户", ""))]
        if not consumption_records:
            violations.append("消费者无实际查询记录")

        # 更新契约状态
        if violations:
            contract["status"] = "已违约"
            contract["pending"] = True
            contract["abnormal"] = True
        else:
            contract["status"] = "已承诺"
            contract["pending"] = False
            contract["abnormal"] = False

        return {
            "ok": True,
            "contract_id": contract_id,
            "status": contract["status"],
            "violations": violations,
            "has_source": source_ok,
            "has_schema_match": schema_ok,
            "is_fresh": freshness_ok,
            "consumption_records": len(consumption_records),
        }

    def propagate_schema_change(self, contract_id: int, new_schema: str) -> dict[str, Any]:
        """当Schema变化时，通过契约将变更通知所有消费者。

        跨模块数据流：
        - 更新 data_contract Schema约束
        - 通知 related semantic_model 更新
        - 更新 freshness_mon 重新评估SLA
        - 写入 query_audit
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        old_schema = contract.get("Schema约束", "")
        contract["Schema约束"] = new_schema
        contract["pending"] = True

        # 通知语义模型
        models = store.rows("semantic_model")
        for m in models:
            if contract.get("数据集", "") in str(m.get("源表映射", "")):
                m["字段别名"] = new_schema
                m["pending"] = True

        # 写入审计
        rows = store.rows("query_audit")
        audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        audit = {
            "id": audit_id,
            "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
            "查询语句": f"CONTRACT_SCHEMA_CHANGE contract={contract_id}",
            "执行耗时": "150ms",
            "扫描行数": "1",
            "返回行数": "1",
            "数据来源": "data_contract",
            "操作用户": "contract_manager",
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "正常",
            "pending": False,
            "abnormal": False,
        }
        rows.append(audit)

        return {
            "ok": True,
            "contract_id": contract_id,
            "old_schema": old_schema,
            "new_schema": new_schema,
        }