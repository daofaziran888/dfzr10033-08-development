"""逐列契约校验与语义模型投产的端到端测试。

覆盖业务规则：
1. 契约发布冻结口径版本，之后可按当时定义复查；
2. 别名映射到同一源列时按映射逐列校验；
3. 必填度量依赖缺列是硬阻断，列通过率达标也不能判通过；
4. 进入可查询范围的列和度量必须满足同一版约束；
5. 并发投产同一模型只能写入一次确认结果；
6. 仅变动别名的映射先保存并投产，再提交缺少度量输入的版本被阻断；
7. 投产记录关联两次报告，可核对通过字段与阻断原因，并回写检查清单。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store
from app.services.contract_check import (
    ContractCheckService,
    parse_alias_mapping,
    parse_contract_columns,
    run_column_check,
)
from app.services.data_contract import DataContractService
from app.services.model_promotion import ModelPromotionService
from app.services.semantic_model import SemanticModelService

client = TestClient(app)


def _next_id(module: str) -> int:
    return max((int(r.get("id", 0)) for r in store.rows(module)), default=0) + 1


def _make_contract(threshold: float = 0.8) -> dict:
    rows = store.rows("data_contract")
    contract = {
        "id": _next_id("data_contract"),
        "契约编号": f"DATA-TEST-{len(rows) + 1:03d}",
        "数据集": "contract_test_ds",
        "Schema约束": "order_id,order_amount,region",
        "列约束": [
            {"列名": "order_id", "类型": "bigint", "必填": True, "依赖列": []},
            {"列名": "order_amount", "类型": "decimal(18,2)", "必填": True,
             "依赖列": ["order_id"]},
            {"列名": "region", "类型": "varchar(32)", "必填": False, "依赖列": []},
            {"度量名": "gmv", "类型": "", "必填": True,
             "依赖列": ["order_amount"], "类别": "度量"},
        ],
        "契约阈值": threshold,
        "关联模型": "SEMA-CC-001",
        "SLA定义": "每小时更新", "更新频率": "3600", "负责人": "tester",
        "消费者": "dashboard", "生效时间": "2026-11-21",
        "status": "草稿", "pending": True, "abnormal": False,
    }
    rows.append(contract)
    return contract


def _make_model(version: str = "v1.0", *, with_measure: bool = True) -> dict:
    rows = store.rows("semantic_model")
    measures = "gmv=SUM(order_amount),cnt=COUNT(order_id)" if with_measure else "cnt=COUNT(order_id)"
    model = {
        "id": _next_id("semantic_model"),
        "模型编号": "SEMA-CC-001",
        "模型名称": "契约测试模型",
        "源表映射": "source_registry:1",
        "字段别名": "订单编号,订单金额,区域",
        "源列映射": {
            "订单编号": "order_id",
            "订单金额": "order_amount",
            "区域": "region",
        },
        "字段类型": {
            "订单编号": "bigint",
            "订单金额": "decimal(18,2)",
            "区域": "varchar(32)",
        },
        "度量定义": measures,
        "维度定义": "区域",
        "计算口径": "标准口径",
        "版本序号": version,
        "status": "草稿", "pending": True, "abnormal": False,
    }
    rows.append(model)
    return model


@pytest.fixture()
def ctx():
    """记录新增数据，测试后清理，避免用例间互相污染。"""
    before = {name: len(store.rows(name)) for name in store.module_names()}
    contract = _make_contract()
    model = _make_model()
    yield {
        "contract": contract,
        "model": model,
        "contract_svc": DataContractService(),
        "check_svc": ContractCheckService(),
        "promotion_svc": ModelPromotionService(),
        "semantic_svc": SemanticModelService(),
    }
    for name in store.module_names():
        rows = store.rows(name)
        del rows[before.get(name, 0):]


class TestColumnCheckEngine:
    def test_alias_mapping_to_same_source_column(self, ctx):
        """两个别名映射到同一源列时，两个别名都按同一源列校验通过。"""
        model = ctx["model"]
        model["源列映射"] = {
            "订单编号": "order_id",
            "订单ID": "order_id",
            "订单金额": "order_amount",
            "区域": "region",
        }
        mapping = parse_alias_mapping(model)
        assert mapping["订单编号"] == "order_id"
        assert mapping["订单ID"] == "order_id"
        # 契约列通过源列名反查别名，不要求别名与契约列同名
        report = run_column_check(
            ctx["contract"], model, contract_version="v1.0", semantic_version="v1.0"
        )
        by_name = {item["检查对象"]: item for item in report["检查项"]}
        assert by_name["order_id"]["通过"]
        assert by_name["order_id"]["映射源列"] == "order_id"

    def test_type_mismatch_detected_per_column(self, ctx):
        """逐列类型不匹配要落到具体检查项。"""
        ctx["model"]["字段类型"]["订单金额"] = "varchar(64)"
        report = run_column_check(
            ctx["contract"], ctx["model"],
            contract_version="v1.0", semantic_version="v1.0",
        )
        amount = next(i for i in report["检查项"] if i["检查对象"] == "order_amount")
        assert not amount["通过"]
        assert any("类型不匹配" in r for r in amount["阻断原因"])

    def test_missing_dependency_column_fails_required_column(self, ctx):
        """依赖列缺失时必填列判失败，非必填列只拉低通过率不构成硬阻断。"""
        # 移除 order_id 的映射
        ctx["model"]["源列映射"].pop("订单编号")
        report = run_column_check(
            ctx["contract"], ctx["model"],
            contract_version="v1.0", semantic_version="v1.0",
        )
        by_name = {i["检查对象"]: i for i in report["检查项"]}
        assert not by_name["order_id"]["通过"]
        assert by_name["order_id"]["硬阻断"]
        # order_amount 依赖 order_id，同样命中依赖缺列
        assert any("依赖列缺失" in r for r in by_name["order_amount"]["阻断原因"])

    def test_required_measure_missing_input_is_hard_block_even_threshold_met(self, ctx):
        """必填度量依赖缺列：列通过率达标也必须硬阻断。"""
        contract = ctx["contract"]
        # 契约只要求 1 列 + 1 个必填度量，阈值 0.5 -> 列通过率 1.0 达标
        contract["列约束"] = [
            {"列名": "order_id", "类型": "bigint", "必填": True, "依赖列": []},
            {"度量名": "gmv", "必填": True, "依赖列": ["order_amount"], "类别": "度量"},
        ]
        contract["契约阈值"] = 0.5
        # 模型缺度量 gmv，且没有 order_amount 映射
        ctx["model"]["度量定义"] = "cnt=COUNT(order_id)"
        report = run_column_check(
            contract, ctx["model"], contract_version="v1.0", semantic_version="v1.0",
        )
        assert report["阈值达标"] is True
        assert report["总体通过"] is False
        gmv = next(i for i in report["检查项"] if i["检查对象"] == "gmv")
        assert gmv["硬阻断"]
        assert any("必填度量" in r for r in gmv["阻断原因"])

    def test_downstream_metric_formula_flagged_on_missing_column(self, ctx):
        """缺列影响下游衍生指标公式时，阻断原因要带出受影响指标。"""
        # 准备一个引用 order_amount 的下游指标
        metric_rows = store.rows("metric_factory")
        metric_rows.append({
            "id": _next_id("metric_factory"),
            "指标编号": "METR-CC-DOWN",
            "指标名称": "GMV衍生指标",
            "数据来源": "semantic_model:SEMA-CC-001",
            "计算公式": "order_amount * 1.1",
            "聚合窗口": "daily", "精度模式": "exact", "依赖立方": "1",
            "最后产出": "-", "status": "草稿", "pending": True, "abnormal": False,
        })
        ctx["model"]["度量定义"] = "gmv=SUM(order_amount)"
        ctx["model"]["源列映射"].pop("订单金额")
        report = run_column_check(
            ctx["contract"], ctx["model"],
            contract_version="v1.0", semantic_version="v1.0",
        )
        gmv = next(i for i in report["检查项"] if i["检查对象"] == "gmv")
        assert gmv["硬阻断"]
        assert any("METR-CC-DOWN" in r and "公式" in r for r in gmv["阻断原因"])

    def test_structured_column_spec_parsing(self, ctx):
        specs = parse_contract_columns(ctx["contract"])
        names = {s["列名"] for s in specs}
        assert {"order_id", "order_amount", "region", "gmv"} <= names
        measure = next(s for s in specs if s["类别"] == "度量")
        assert measure["依赖列"] == ["order_amount"]


class TestContractVersioning:
    def test_publish_freezes_snapshot_and_recheck_uses_snapshot(self, ctx):
        """发布后修改契约定义，复查仍按发布时的版本快照执行。"""
        svc = ctx["contract_svc"]
        published = svc.publish_contract(ctx["contract"]["id"])
        assert published["ok"]
        v1 = published["version"]

        # 发布后再收紧契约（增加新列），快照不应被污染
        ctx["contract"]["列约束"] = [
            {"列名": "order_id", "类型": "bigint", "必填": True, "依赖列": []},
            {"列名": "new_strict_col", "类型": "varchar(8)", "必填": True, "依赖列": []},
        ]
        recheck = svc.recheck_published_version(
            ctx["contract"]["id"], ctx["model"]["id"], v1
        )
        assert recheck["ok"]
        snapshot_cols = {i["检查对象"] for i in recheck["report"]["检查项"]}
        assert "new_strict_col" not in snapshot_cols
        assert "gmv" in snapshot_cols

        # 再次发布形成 v2，两个版本都可复查
        v2 = svc.publish_contract(ctx["contract"]["id"])["version"]
        assert v2 == "v2.0"
        recheck_v2 = svc.recheck_published_version(
            ctx["contract"]["id"], ctx["model"]["id"], v2
        )
        v2_cols = {i["检查对象"] for i in recheck_v2["report"]["检查项"]}
        assert "new_strict_col" in v2_cols

    def test_recheck_unknown_version_rejected(self, ctx):
        ctx["contract_svc"].publish_contract(ctx["contract"]["id"])
        result = ctx["contract_svc"].recheck_published_version(
            ctx["contract"]["id"], ctx["model"]["id"], "v9.9"
        )
        assert not result["ok"] and "不存在" in result["message"]


class TestPromotionFlow:
    def test_alias_only_change_saved_then_promoted(self, ctx):
        """先保存仅变动别名的映射，再完成投产：报告通过并生成可查询清单。"""
        contract = ctx["contract"]
        ctx["contract_svc"].publish_contract(contract["id"])
        model = ctx["model"]

        # 仅保存别名映射（不投产），模型保持草稿
        saved = ctx["semantic_svc"].save_alias_mapping(
            model["id"],
            {
                "订单编号": "order_id",
                "订单金额": "order_amount",
                "区域": "region",
                "GMV口径": "order_amount",  # 新别名映射到已存在的同一源列
            },
            new_version="v1.1",
        )
        assert saved["ok"]
        assert model["status"] == "草稿"
        assert model["版本序号"] == "v1.1"
        assert parse_alias_mapping(model)["GMV口径"] == "order_amount"

        result = ctx["promotion_svc"].promote_with_contract(model["id"])
        assert result["ok"], result
        report_no = result["report_no"]
        assert model["status"] == "已投产"

        # 可查询字段清单只包含同一版约束下校验通过的对象
        queryable = set(result["queryable_fields"])
        assert {"order_id", "order_amount", "region", "gmv"} <= queryable
        assert result["queryable_count"] == len(queryable)

        # 投产结果回写到契约检查清单
        report = ctx["check_svc"].get_report(report_no)
        assert all(item["投产结论"] == "已确认" for item in report["检查项"])
        assert all(item["可查询"] for item in report["检查项"])

    def test_resubmit_missing_measure_input_is_blocked(self, ctx):
        """再次提交缺少度量输入的版本：硬阻断，不产生可查询字段。"""
        ctx["contract_svc"].publish_contract(ctx["contract"]["id"])
        model = ctx["model"]

        first = ctx["promotion_svc"].promote_with_contract(model["id"])
        assert first["ok"]

        # 提交新版本：度量定义不再包含必填度量 gmv
        model["版本序号"] = "v2.0"
        model["度量定义"] = "cnt=COUNT(order_id)"
        second = ctx["promotion_svc"].promote_with_contract(model["id"])
        assert not second["ok"]
        assert second["status"] == "已阻断"
        assert second["queryable_fields"] == []
        assert any("gmv" in b for b in second["hard_blocks"])

        # 阻断回写：检查清单结论为已阻断且不可查询
        report = ctx["check_svc"].get_report(second["report_no"])
        gmv_item = next(i for i in report["检查项"] if i["检查对象"] == "gmv")
        assert gmv_item["投产结论"] == "已阻断"
        assert gmv_item["可查询"] is False

    def test_promotion_records_link_both_reports(self, ctx):
        """从投产记录可关联到两次报告，并核对通过字段与阻断原因。"""
        ctx["contract_svc"].publish_contract(ctx["contract"]["id"])
        model = ctx["model"]

        first = ctx["promotion_svc"].promote_with_contract(model["id"])
        model["版本序号"] = "v2.0"
        model["源列映射"].pop("订单金额")
        model["度量定义"] = "gmv=SUM(order_amount)"
        second = ctx["promotion_svc"].promote_with_contract(model["id"])

        records = ctx["promotion_svc"].list_promotions(model["id"])
        assert len(records) == 2
        linked_reports = {r["关联报告"] for r in records}
        assert linked_reports == {first["report_no"], second["report_no"]}

        first_record = next(r for r in records if r["投产结论"] == "已确认")
        second_record = next(r for r in records if r["投产结论"] == "已阻断")
        assert "order_amount" in first_record["可查询字段清单"]
        assert second_record["可查询字段清单"] == []
        assert "order_amount" in second_record["阻断原因"]

        # 报告详情可分别复查
        detail_ok = ctx["check_svc"].get_report(first["report_no"])
        detail_bad = ctx["check_svc"].get_report(second["report_no"])
        assert all(i["约束结果"] == "通过" for i in detail_ok["检查项"])
        failed = {i["检查对象"] for i in detail_bad["检查项"] if i["约束结果"] == "未通过"}
        assert "order_amount" in failed and "gmv" in failed

    def test_partial_fields_below_threshold_cannot_open_partial_scope(self, ctx):
        """部分字段达标但阈值不满足时不能提前开放可查询范围。"""
        contract = ctx["contract"]
        contract["契约阈值"] = 0.99
        ctx["contract_svc"].publish_contract(contract["id"])
        # 三列里缺一列 -> 通过率 2/3 < 0.99
        ctx["model"]["源列映射"].pop("区域")
        result = ctx["promotion_svc"].promote_with_contract(ctx["model"]["id"])
        assert not result["ok"]
        assert result["threshold_met"] is False
        assert result["queryable_fields"] == []

    def test_concurrent_promotion_writes_single_confirmation(self, ctx):
        """并发投产同一模型同一版本：只能有一次确认结果。"""
        ctx["contract_svc"].publish_contract(ctx["contract"]["id"])
        model_id = ctx["model"]["id"]
        barrier = threading.Barrier(2)
        outcomes: list[dict] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            result = ctx["promotion_svc"].promote_with_contract(model_id)
            with lock:
                outcomes.append(result)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(outcomes) == 2
        successes = [o for o in outcomes if o.get("ok")]
        rejects = [o for o in outcomes if not o.get("ok")]
        assert len(successes) == 1
        assert len(rejects) == 1
        assert "禁止重复投产" in rejects[0]["message"]
        promotions = [
            r for r in store.rows("model_promotion")
            if r.get("模型id") == model_id and r.get("status") == "已确认"
        ]
        assert len(promotions) == 1


class TestApiEndpoints:
    def test_publish_check_promote_api_chain(self, ctx):
        contract_id = ctx["contract"]["id"]
        model_id = ctx["model"]["id"]

        pub = client.post(f"/api/data_contract/{contract_id}/publish", json={})
        assert pub.status_code == 200
        assert pub.json()["version"] == "v1.0"

        # 复查接口生成报告但不投产
        check = client.post(
            f"/api/data_contract/{contract_id}/column-check",
            json={"model_id": model_id},
        )
        assert check.status_code == 200
        report_no = check.json()["report_no"]
        report = client.get(f"/api/contract_check/report/{report_no}")
        assert report.status_code == 200
        assert report.json()["总体通过"]

        # 仅保存别名映射
        mapping = client.post(
            f"/api/semantic_model/{model_id}/alias-mapping",
            json={"mapping": {"订单编号": "order_id", "订单金额": "order_amount",
                              "区域": "region"}, "new_version": "v1.2"},
        )
        assert mapping.status_code == 200

        promote = client.post(f"/api/semantic_model/{model_id}/promote")
        assert promote.status_code == 200
        body = promote.json()
        assert body["status"] == "已投产"
        assert body["queryable_count"] >= 4

        promotions = client.get(f"/api/model_promotion/by-model/{model_id}")
        assert promotions.status_code == 200
        assert promotions.json()["total"] == 1

    def test_blocked_promotion_api_returns_409(self, ctx):
        client.post(f"/api/data_contract/{ctx['contract']['id']}/publish", json={})
        ctx["model"]["度量定义"] = "cnt=COUNT(order_id)"  # 缺必填度量 gmv
        resp = client.post(f"/api/semantic_model/{ctx['model']['id']}/promote")
        assert resp.status_code == 409
        body = resp.json()
        # 阻断响应体仍携带报告编号与阻断明细，便于前端核对
        assert "投产阻断" in body["message"]
        assert body["report_no"]
        assert body["queryable_fields"] == []

    def test_duplicate_promotion_api_returns_409(self, ctx):
        client.post(f"/api/data_contract/{ctx['contract']['id']}/publish", json={})
        mid = ctx["model"]["id"]
        first = client.post(f"/api/semantic_model/{mid}/promote")
        assert first.status_code == 200
        second = client.post(f"/api/semantic_model/{mid}/promote")
        assert second.status_code == 409
        assert "禁止重复投产" in second.json()["message"]

    def test_persisted_report_keeps_contract_and_model_link(self, ctx):
        """报告落表后必须保留契约与模型主键，支撑投产记录反查。"""
        ctx["contract_svc"].publish_contract(ctx["contract"]["id"])
        result = ctx["promotion_svc"].promote_with_contract(ctx["model"]["id"])
        rows = [
            r for r in store.rows("contract_check")
            if r.get("报告编号") == result["report_no"]
        ]
        assert rows
        assert all(r["契约id"] == ctx["contract"]["id"] for r in rows)
        assert all(r["模型id"] == ctx["model"]["id"] for r in rows)

    def test_contract_check_list_available_as_module(self, ctx):
        """新模块可以走统一资源列表接口。"""
        ctx["contract_svc"].publish_contract(ctx["contract"]["id"])
        ctx["promotion_svc"].promote_with_contract(ctx["model"]["id"])
        resp = client.get("/api/contract_check")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] >= 4  # 每份报告 4 个检查项
        resp2 = client.get("/api/model_promotion")
        assert resp2.status_code == 200
        assert resp2.json()["total"] >= 1
