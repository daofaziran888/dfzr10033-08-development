"""语义模型投产记录领域服务。

投产记录把「逐列契约校验报告 -> 投产确认 -> 可查询字段清单 -> 报告回写」串成一条
可追溯链路，并通过模块级锁保证：同一模型的并发投产请求只有一个能写入确认结果。
"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.services.contract_check import (
    ContractCheckService,
    run_column_check,
)
from app.store import store


class ModelPromotionService(BaseModuleService):
    MODULE = "model_promotion"
    ENTITY = "投产记录"
    OPERATION_TERM = "投产"
    LIST_FIELDS = [
        "投产编号", "模型编号", "模型名称", "契约编号", "契约版本",
        "关联报告", "投产结论", "可查询字段数", "阻断原因", "投产时间",
    ]
    REQUIRED_FIELDS = ["投产编号", "模型编号", "关联报告"]
    STATUS_ORDER = ["待确认", "已确认", "已阻断"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    # 同一模型串行化投产，杜绝并发写入两次确认结果
    _promotion_lock = threading.Lock()

    def __init__(self) -> None:
        self.check_svc = ContractCheckService()

    # ------------------------------------------------------------------
    def promote_with_contract(self, model_id: int) -> dict[str, Any]:
        """消费逐列契约校验报告执行投产，返回投产确认结果。

        流程：生成报告 -> 判定总体通过 -> 写投产记录 -> 回写检查清单 ->
        生成可查询字段清单 -> 更新模型/契约状态。
        """
        with self._promotion_lock:
            model = store.find("semantic_model", model_id)
            if model is None:
                return {"ok": False, "message": "语义模型不存在"}

            # 并发投产保护：已有确认记录的同一模型+同一语义版本不能再确认第二次
            duplicate = next(
                (r for r in store.rows(self.MODULE)
                 if r.get("模型id") == model_id
                 and r.get("语义版本") == str(model.get("版本序号", ""))
                 and r.get("status") == "已确认"),
                None,
            )
            if duplicate is not None:
                return {
                    "ok": False,
                    "message": (
                        f"模型{model.get('模型编号', '')}版本{model.get('版本序号', '')}"
                        f"已存在投产确认记录{duplicate.get('投产编号')}，禁止重复投产"
                    ),
                    "duplicate_promotion": duplicate.get("投产编号"),
                }

            contract = _match_contract(model)
            if contract is None:
                return {"ok": False, "message": "未找到与该语义模型匹配的数据契约，无法按契约投产"}

            # 已发布的契约按发布时冻结的版本快照复查；草稿契约使用当前定义
            snapshot = contract.get("版本快照") if contract.get("发布状态") == "已发布" else None
            frozen_contract = snapshot if isinstance(snapshot, dict) else contract
            contract_version = str(
                snapshot.get("版本序号") if isinstance(snapshot, dict) else contract.get("当前版本", "draft")
            )
            semantic_version = str(model.get("版本序号", ""))

            report = run_column_check(
                frozen_contract, model,
                contract_version=contract_version,
                semantic_version=semantic_version,
            )
            # 快照可能未携带 id，报告头始终回填当前契约主键保证链路可关联
            report["契约id"] = contract.get("id")
            report_no = self.check_svc.persist_report(report)

            rows = store.rows(self.MODULE)
            promotion_no = f"PROMO-{len(rows) + 1:04d}-{model_id:04d}"

            if report["总体通过"]:
                return self._confirm_promotion(
                    rows, promotion_no, model, contract, report, report_no,
                )
            return self._block_promotion(
                rows, promotion_no, model, contract, report, report_no,
            )

    # ------------------------------------------------------------------
    def _confirm_promotion(
        self,
        rows: list[dict[str, Any]],
        promotion_no: str,
        model: dict[str, Any],
        contract: dict[str, Any],
        report: dict[str, Any],
        report_no: str,
    ) -> dict[str, Any]:
        # 同一版约束下逐列校验通过的对象才进入可查询范围
        queryable = {
            item["检查对象"] for item in report["检查项"] if item["通过"]
        }
        promotion = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "投产编号": promotion_no,
            "模型id": model["id"],
            "模型编号": model.get("模型编号", ""),
            "模型名称": model.get("模型名称", ""),
            "契约id": contract.get("id"),
            "契约编号": contract.get("契约编号", ""),
            "契约版本": report["契约版本"],
            "语义版本": report["语义版本"],
            "关联报告": report_no,
            "关联报告清单": [report_no],
            "投产结论": "已确认",
            "可查询字段数": len(queryable),
            "可查询字段清单": sorted(queryable),
            "通过率": report["通过率"],
            "阻断原因": "",
            "投产时间": report["报告时间"],
            "status": "已确认",
            "pending": False,
            "abnormal": False,
        }
        rows.append(promotion)

        self.check_svc.writeback_promotion(report_no, promotion_no, "已确认", queryable)

        model["status"] = "已投产"
        model["pending"] = False
        model["abnormal"] = False
        model["投产编号"] = promotion_no
        model["关联报告"] = report_no
        model["可查询字段清单"] = sorted(queryable)

        contract["status"] = "已承诺"
        contract["最近投产编号"] = promotion_no
        contract["最近报告编号"] = report_no
        return {
            "ok": True,
            "promotion_no": promotion_no,
            "report_no": report_no,
            "model_id": model["id"],
            "status": "已投产",
            "contract_version": report["契约版本"],
            "pass_rate": report["通过率"],
            "queryable_fields": sorted(queryable),
            "queryable_count": len(queryable),
        }

    def _block_promotion(
        self,
        rows: list[dict[str, Any]],
        promotion_no: str,
        model: dict[str, Any],
        contract: dict[str, Any],
        report: dict[str, Any],
        report_no: str,
    ) -> dict[str, Any]:
        reasons = sorted({
            reason
            for item in report["检查项"]
            for reason in item["阻断原因"]
        })
        hard_blocks = report["硬阻断"]
        promotion = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "投产编号": promotion_no,
            "模型id": model["id"],
            "模型编号": model.get("模型编号", ""),
            "模型名称": model.get("模型名称", ""),
            "契约id": contract.get("id"),
            "契约编号": contract.get("契约编号", ""),
            "契约版本": report["契约版本"],
            "语义版本": report["语义版本"],
            "关联报告": report_no,
            "关联报告清单": [report_no],
            "投产结论": "已阻断",
            "可查询字段数": 0,
            "可查询字段清单": [],
            "通过率": report["通过率"],
            "阻断原因": "；".join(reasons),
            "投产时间": report["报告时间"],
            "status": "已阻断",
            "pending": True,
            "abnormal": True,
        }
        rows.append(promotion)

        # 阻断时没有可查询字段：进入可查询范围的列和度量均须满足同一版约束
        self.check_svc.writeback_promotion(report_no, promotion_no, "已阻断", set())

        model["status"] = "草稿"
        model["pending"] = True
        model["abnormal"] = True
        model["最近阻断报告"] = report_no

        contract["status"] = "已违约"
        contract["abnormal"] = True
        contract["最近报告编号"] = report_no
        return {
            "ok": False,
            "message": f"投产阻断: {'; '.join(hard_blocks or reasons)}",
            "promotion_no": promotion_no,
            "report_no": report_no,
            "model_id": model["id"],
            "status": "已阻断",
            "contract_version": report["契约版本"],
            "pass_rate": report["通过率"],
            "threshold_met": report["阈值达标"],
            "hard_blocks": hard_blocks,
            "block_reasons": reasons,
            "queryable_fields": [],
            "queryable_count": 0,
        }

    def list_promotions(self, model_id: int) -> list[dict[str, Any]]:
        """从投产记录视角关联模型的全部报告。"""
        return [
            r for r in store.rows(self.MODULE) if r.get("模型id") == model_id
        ]


def _match_contract(model: dict[str, Any]) -> dict[str, Any] | None:
    """按模型编号/名称/源表映射找到对应的数据契约。"""
    contracts = store.rows("data_contract")
    model_no = str(model.get("模型编号", ""))
    model_name = str(model.get("模型名称", ""))
    for contract in contracts:
        binding = str(contract.get("关联模型", ""))
        if model_no and model_no in binding:
            return contract
    for contract in contracts:
        dataset = str(contract.get("数据集", ""))
        if model_name and model_name in dataset:
            return contract
        if dataset and dataset in str(model.get("源表映射", "")):
            return contract
    return None
