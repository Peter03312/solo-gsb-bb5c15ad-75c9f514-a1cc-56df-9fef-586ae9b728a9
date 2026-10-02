"""发运后复核（已发运批次风险复核）测试。

覆盖：
- 复核看板：按交接号逐批并列交接冻结快照与当前版本/结论/作废状态；
  无变化时全部 NOT_REQUIRED；
- 复核事项形成：裁决版本变化（即使结论仍为 RELEASE）、出货资格版本变化
  （批号待检锁定）、批次作废——任一即形成复核事项（PENDING_REVIEW）；
- 处置：凭所见两类当前版本与作废状态记录“需召回/无需召回”+ 非空原因，
  记入只追加历史后显示已复核；后续版本变化或新作废再次待复核；
- 同一状态不得重复处置（409 DUPLICATE_REVIEW_DISPOSITION）；
- 拒绝且不写入：未知交接 404、非其批次 404、过期所见状态 409、空原因 400、
  非法处置结论 400、当前无复核事项 409、缺字段/非法版本 422；
- 交接快照与原核验流程保持不变：复核看板/处置不改写交接记录与核验单状态；
- 旧库启动幂等补齐复核表，既有交接可正常复核。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def confirm(client, verification_no):
    resp = client.post(f"/api/verifications/{verification_no}/confirm")
    assert resp.status_code == 200, resp.text
    return resp.json()


def ship(client, handover_no, verification_no, batch_nos, lot="RM-M"):
    """登记批次、创建并确认核验单、完成装车交接，返回交接响应体。"""
    create = client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": list(batch_nos)},
    )
    assert create.status_code == 201, create.text
    doc = confirm(client, verification_no)
    resp = client.post(
        "/api/handovers",
        json={
            "handover_no": handover_no,
            "verification_no": verification_no,
            "expected_revision": doc["revision"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def release_batches(client, *batch_nos, line_id="L1", lot="RM-M"):
    """登记一个含 milk 的批号与若干全部 RELEASE/SHIPPABLE 的批次。"""
    add_lot(client, lot, "奶粉", ["milk"])
    for bn in batch_nos:
        add_batch(client, bn, line_id=line_id, lots=[lot], declared=["milk"])


def review_board(client, handover_no):
    resp = client.get(f"/api/handovers/{handover_no}/review")
    assert resp.status_code == 200, resp.text
    return resp.json()


def board_entry(board, batch_no):
    return next(b for b in board["batches"] if b["batch_no"] == batch_no)


def dispose(client, handover_no, batch_no, decision, reason, state):
    resp = client.post(
        f"/api/handovers/{handover_no}/review",
        json={
            "batch_no": batch_no,
            "decision": decision,
            "reason": reason,
            "expected_adjudication_version": state["adjudication_version"],
            "expected_shipment_version": state["shipment_version"],
            "expected_voided": state["voided"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def current_state(client, handover_no, batch_no):
    return board_entry(review_board(client, handover_no), batch_no)["current_state"]


def review_row_count(client, handover_no):
    with client.app.state.store.read() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM shipment_handover_review WHERE handover_no=?",
            (handover_no,),
        ).fetchone()["n"]


# ---------------------------------------------------------------- 复核看板


def test_review_board_no_changes_all_not_required(client):
    release_batches(client, "B1", "B2")
    ship(client, "H1", "V1", ["B2", "B1"])

    board = review_board(client, "H1")
    # 交接冻结信息并列展示
    assert board["handover_no"] == "H1"
    assert board["verification_no"] == "V1"
    assert board["verification_revision"] == 2
    assert board["batch_count"] == 2
    assert board["review_summary"] == {
        "total": 2,
        "not_required": 2,
        "pending_review": 0,
        "reviewed": 0,
    }
    # 逐批：交接冻结快照与当前版本/结论/作废状态并列，保序
    assert [b["batch_no"] for b in board["batches"]] == ["B2", "B1"]
    for b in board["batches"]:
        assert b["review_required"] is False
        assert b["review_status"] == "NOT_REQUIRED"
        assert b["review_reasons"] == []
        assert b["dispositions"] == []
        assert b["latest_disposition"] is None
        assert b["handover_snapshot"]["adjudication"]["version"] == 1
        assert b["handover_snapshot"]["adjudication"]["disposition"] == "RELEASE"
        assert b["handover_snapshot"]["shipment_eligibility"]["version"] == 1
        assert b["handover_snapshot"]["shipment_eligibility"]["status"] == "SHIPPABLE"
        assert b["current"]["adjudication"]["version"] == 1
        assert b["current"]["shipment_eligibility"]["version"] == 1
        assert b["current"]["voided"] is False
        assert b["current"]["status"] == "ACTIVE"
        assert b["current_state"] == {
            "adjudication_version": 1,
            "shipment_version": 1,
            "voided": False,
        }


def test_review_board_unknown_handover_404(client):
    resp = client.get("/api/handovers/NOPE/review")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HANDOVER_NOT_FOUND"


# ---------------------------------------------------------------- 复核事项形成


def test_adjudication_version_change_forms_review_item_even_still_release(client):
    """裁决版本变化即形成复核事项，即使结论仍为 RELEASE。"""
    release_batches(client, "B1", "B2")  # L1：更正 B1 会按线重算 B1 及其后的 B2
    add_batch(client, "B3", line_id="L2", lots=["RM-M"], declared=["milk"])
    ship(client, "H1", "V1", ["B1", "B2", "B3"])

    # 声明扩充（仍 RELEASE）：B1 及同线后续 B2 各追加一个裁决版本
    resp = client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    assert resp.status_code == 200

    board = review_board(client, "H1")
    assert board["review_summary"]["pending_review"] == 2
    assert board["review_summary"]["not_required"] == 1

    b1 = board_entry(board, "B1")
    assert b1["review_required"] is True
    assert b1["review_status"] == "PENDING_REVIEW"
    # 并列：交接快照 v1/RELEASE vs 当前 v2/RELEASE
    assert b1["handover_snapshot"]["adjudication"]["version"] == 1
    assert b1["current"]["adjudication"]["version"] == 2
    assert b1["current"]["adjudication"]["disposition"] == "RELEASE"
    assert b1["current_state"]["adjudication_version"] == 2
    reasons = b1["review_reasons"]
    assert [r["code"] for r in reasons] == ["ADJUDICATION_VERSION_CHANGED"]
    assert reasons[0]["from_version"] == 1
    assert reasons[0]["to_version"] == 2
    assert reasons[0]["trigger"] == "CORRECT_BATCH"

    # 同线后续的 B2 被重算追加版本（结论仍 RELEASE），同样形成复核事项
    b2 = board_entry(board, "B2")
    assert b2["review_status"] == "PENDING_REVIEW"
    assert b2["current"]["adjudication"]["disposition"] == "RELEASE"

    # 另一条线的 B3 未被重算：无复核事项
    b3 = board_entry(board, "B3")
    assert b3["review_status"] == "NOT_REQUIRED"


def test_shipment_version_change_by_lot_lock_forms_review_item(client):
    """原料批号待检锁定使出货资格版本递增，形成复核事项（裁决不变）。"""
    release_batches(client, "B1", "B2")
    ship(client, "H1", "V1", ["B1", "B2"])

    resp = client.post("/api/material-lots/RM-M/lock", json={"reason": "供应商通报待检"})
    assert resp.status_code == 200

    board = review_board(client, "H1")
    assert board["review_summary"]["pending_review"] == 2
    for bn in ("B1", "B2"):
        b = board_entry(board, bn)
        assert b["review_status"] == "PENDING_REVIEW"
        # 裁决版本未变，出货资格版本 v1 -> v2（BLOCKED）
        assert b["handover_snapshot"]["shipment_eligibility"]["version"] == 1
        assert b["current"]["adjudication"]["version"] == 1
        assert b["current"]["shipment_eligibility"]["version"] == 2
        assert b["current"]["shipment_eligibility"]["status"] == "BLOCKED"
        assert b["current"]["shipment_eligibility"]["locked_lots"] == ["RM-M"]
        assert b["current_state"] == {
            "adjudication_version": 1,
            "shipment_version": 2,
            "voided": False,
        }
        codes = [r["code"] for r in b["review_reasons"]]
        assert codes == ["SHIP_ELIGIBILITY_VERSION_CHANGED"]
        assert b["review_reasons"][0]["trigger"] == "LOT_LOCK"


def test_voided_batch_forms_review_item(client):
    """已发运批次被作废：作废标记本身形成复核事项（版本冻结不变）。"""
    release_batches(client, "B1", "B2")
    ship(client, "H1", "V1", ["B1", "B2"])

    resp = client.post("/api/batches/B1/void", json={"reason": "计划误登记，实际未投产"})
    assert resp.status_code == 200

    board = review_board(client, "H1")
    b1 = board_entry(board, "B1")
    assert b1["review_required"] is True
    assert b1["review_status"] == "PENDING_REVIEW"
    assert b1["current"]["status"] == "VOIDED"
    assert b1["current"]["voided"] is True
    assert b1["current"]["voided_reason"] == "计划误登记，实际未投产"
    assert b1["current_state"]["voided"] is True
    # 作废批次裁决/资格版本冻结，与交接快照一致；复核事项来自作废标记
    assert b1["current_state"]["adjudication_version"] == 1
    assert b1["current_state"]["shipment_version"] == 1
    codes = [r["code"] for r in b1["review_reasons"]]
    assert codes == ["BATCH_VOIDED"]
    assert b1["review_reasons"][0]["voided_reason"] == "计划误登记，实际未投产"

    # 作废触发同线重算：B2 追加裁决版本，也形成复核事项
    b2 = board_entry(board, "B2")
    assert b2["review_status"] == "PENDING_REVIEW"
    assert b2["current_state"]["adjudication_version"] == 2
    assert [r["code"] for r in b2["review_reasons"]] == ["ADJUDICATION_VERSION_CHANGED"]


# ---------------------------------------------------------------- 处置与只追加历史


def test_record_disposition_marks_reviewed_with_append_only_history(client):
    release_batches(client, "B1")
    ship(client, "H1", "V1", ["B1"])
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})

    state = current_state(client, "H1", "B1")
    body = dispose(client, "H1", "B1", "NO_RECALL_NEEDED", "仅声明扩充，无新增风险", state)
    assert body["handover_no"] == "H1"
    assert body["batch_no"] == "B1"
    assert body["review_status"] == "REVIEWED"
    disp = body["disposition"]
    assert disp["decision"] == "NO_RECALL_NEEDED"
    assert disp["decision_label"] == "无需召回"
    assert disp["reason"] == "仅声明扩充，无新增风险"
    assert disp["state"] == state
    assert disp["decided_at"] is not None

    b1 = board_entry(review_board(client, "H1"), "B1")
    assert b1["review_status"] == "REVIEWED"
    assert b1["latest_disposition"]["id"] == disp["id"]
    assert len(b1["dispositions"]) == 1
    assert review_board(client, "H1")["review_summary"]["reviewed"] == 1


def test_new_change_after_disposition_pending_again_and_history_appends(client):
    """处置后再次版本变化 -> 再次待复核；再次处置追加历史，旧记录保留。"""
    release_batches(client, "B1")
    ship(client, "H1", "V1", ["B1"])

    # 第一次变化与处置
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    state1 = current_state(client, "H1", "B1")
    dispose(client, "H1", "B1", "NO_RECALL_NEEDED", "第一次复核：无新增风险", state1)
    assert board_entry(review_board(client, "H1"), "B1")["review_status"] == "REVIEWED"

    # 第二次变化（批号锁定 -> 出货资格版本递增）-> 再次待复核
    client.post("/api/material-lots/RM-M/lock", json={"reason": "待检"})
    b1 = board_entry(review_board(client, "H1"), "B1")
    assert b1["review_status"] == "PENDING_REVIEW"
    assert b1["current_state"] == {
        "adjudication_version": 2,
        "shipment_version": 2,
        "voided": False,
    }
    # 旧处置保留在历史中
    assert len(b1["dispositions"]) == 1
    assert b1["dispositions"][0]["state"] == state1

    # 针对新状态再次处置（需召回）-> 已复核，历史两条只追加
    body = dispose(
        client, "H1", "B1", "RECALL_REQUIRED", "原料待检锁定，启动召回评估", b1["current_state"]
    )
    assert body["disposition"]["decision"] == "RECALL_REQUIRED"
    assert body["disposition"]["decision_label"] == "需召回"
    b1 = board_entry(review_board(client, "H1"), "B1")
    assert b1["review_status"] == "REVIEWED"
    assert len(b1["dispositions"]) == 2
    assert b1["dispositions"][0]["decision"] == "NO_RECALL_NEEDED"
    assert b1["dispositions"][1]["decision"] == "RECALL_REQUIRED"
    assert b1["latest_disposition"]["decision"] == "RECALL_REQUIRED"

    # 新作废同样再次待复核
    client.post("/api/material-lots/RM-M/unlock", json={"reason": "复检合格"})
    client.post("/api/batches/B1/void", json={"reason": "核实为误登记"})
    b1 = board_entry(review_board(client, "H1"), "B1")
    assert b1["review_status"] == "PENDING_REVIEW"
    assert b1["current_state"]["voided"] is True
    dispose(client, "H1", "B1", "RECALL_REQUIRED", "批次作废但已发运，需召回", b1["current_state"])
    assert board_entry(review_board(client, "H1"), "B1")["review_status"] == "REVIEWED"
    assert len(board_entry(review_board(client, "H1"), "B1")["dispositions"]) == 3


def test_duplicate_disposition_same_state_rejected(client):
    """同一状态不得重复处置：409 且不写入。"""
    release_batches(client, "B1")
    ship(client, "H1", "V1", ["B1"])
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    state = current_state(client, "H1", "B1")
    dispose(client, "H1", "B1", "NO_RECALL_NEEDED", "首次处置", state)
    assert review_row_count(client, "H1") == 1

    resp = client.post(
        "/api/handovers/H1/review",
        json={
            "batch_no": "B1",
            "decision": "RECALL_REQUIRED",
            "reason": "重复提交",
            "expected_adjudication_version": state["adjudication_version"],
            "expected_shipment_version": state["shipment_version"],
            "expected_voided": state["voided"],
        },
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "DUPLICATE_REVIEW_DISPOSITION"
    # details 回显既有处置
    assert err["details"][0]["decision"] == "NO_RECALL_NEEDED"
    assert err["details"][0]["reason"] == "首次处置"
    assert review_row_count(client, "H1") == 1
    assert board_entry(review_board(client, "H1"), "B1")["review_status"] == "REVIEWED"


# ---------------------------------------------------------------- 拒绝且不写入


def test_stale_seen_state_rejected(client):
    """过期所见状态（任一版本或作废标记不符）-> 409，不写入。"""
    release_batches(client, "B1")
    ship(client, "H1", "V1", ["B1"])
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})

    # 裁决版本过期（当前 v2，提交 v1）
    resp = client.post(
        "/api/handovers/H1/review",
        json={
            "batch_no": "B1",
            "decision": "NO_RECALL_NEEDED",
            "reason": "基于过期状态",
            "expected_adjudication_version": 1,
            "expected_shipment_version": 1,
            "expected_voided": False,
        },
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "REVIEW_STATE_CONFLICT"
    detail = err["details"][0]
    assert detail["submitted_state"] == {
        "adjudication_version": 1,
        "shipment_version": 1,
        "voided": False,
    }
    assert detail["current_state"] == {
        "adjudication_version": 2,
        "shipment_version": 1,
        "voided": False,
    }

    # 出货资格版本过期
    resp = client.post(
        "/api/handovers/H1/review",
        json={
            "batch_no": "B1",
            "decision": "NO_RECALL_NEEDED",
            "reason": "出货资格版本不符",
            "expected_adjudication_version": 2,
            "expected_shipment_version": 99,
            "expected_voided": False,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVIEW_STATE_CONFLICT"

    # 作废标记不符
    resp = client.post(
        "/api/handovers/H1/review",
        json={
            "batch_no": "B1",
            "decision": "NO_RECALL_NEEDED",
            "reason": "作废标记不符",
            "expected_adjudication_version": 2,
            "expected_shipment_version": 1,
            "expected_voided": True,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVIEW_STATE_CONFLICT"

    assert review_row_count(client, "H1") == 0
    assert board_entry(review_board(client, "H1"), "B1")["review_status"] == "PENDING_REVIEW"


def test_unknown_handover_and_batch_not_in_handover_rejected(client):
    release_batches(client, "B1", "B2")
    ship(client, "H1", "V1", ["B1"])
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    client.put("/api/batches/B2", json={"declared_allergens": ["milk", "egg"]})

    payload = {
        "decision": "NO_RECALL_NEEDED",
        "reason": "x",
        "expected_adjudication_version": 2,
        "expected_shipment_version": 1,
        "expected_voided": False,
    }
    # 未知交接
    resp = client.post("/api/handovers/NOPE/review", json={**payload, "batch_no": "B1"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "HANDOVER_NOT_FOUND"

    # 非其批次（B2 已登记但不在 H1 中）
    resp = client.post("/api/handovers/H1/review", json={**payload, "batch_no": "B2"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_IN_HANDOVER"

    # 完全未知的批次同样按“非其批次”拒绝
    resp = client.post("/api/handovers/H1/review", json={**payload, "batch_no": "GHOST"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_IN_HANDOVER"

    assert review_row_count(client, "H1") == 0


def test_empty_reason_and_invalid_decision_rejected(client):
    release_batches(client, "B1")
    ship(client, "H1", "V1", ["B1"])
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    state = current_state(client, "H1", "B1")
    base = {
        "batch_no": "B1",
        "expected_adjudication_version": state["adjudication_version"],
        "expected_shipment_version": state["shipment_version"],
        "expected_voided": state["voided"],
    }

    # 空原因 / 纯空白原因 -> 400
    for reason in ("", "   "):
        resp = client.post(
            "/api/handovers/H1/review",
            json={**base, "decision": "NO_RECALL_NEEDED", "reason": reason},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"

    # 非法处置结论 -> 400
    resp = client.post(
        "/api/handovers/H1/review",
        json={**base, "decision": "MAYBE", "reason": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"

    # 缺字段 / 非法版本号 -> 422（FastAPI 模型校验）
    assert client.post(
        "/api/handovers/H1/review",
        json={k: v for k, v in {**base, "decision": "NO_RECALL_NEEDED", "reason": "x"}.items()
              if k != "expected_voided"},
    ).status_code == 422
    assert client.post(
        "/api/handovers/H1/review",
        json={**base, "decision": "NO_RECALL_NEEDED", "reason": "x",
              "expected_adjudication_version": 0},
    ).status_code == 422

    assert review_row_count(client, "H1") == 0


def test_disposition_without_review_item_rejected(client):
    """相较交接快照无任何变化：当前无复核事项，处置拒绝且不写入。"""
    release_batches(client, "B1")
    ship(client, "H1", "V1", ["B1"])

    resp = client.post(
        "/api/handovers/H1/review",
        json={
            "batch_no": "B1",
            "decision": "NO_RECALL_NEEDED",
            "reason": "无变化也提交",
            "expected_adjudication_version": 1,
            "expected_shipment_version": 1,
            "expected_voided": False,
        },
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "REVIEW_NOT_REQUIRED"
    assert err["details"][0]["current_state"] == {
        "adjudication_version": 1,
        "shipment_version": 1,
        "voided": False,
    }
    assert review_row_count(client, "H1") == 0


# ---------------------------------------------------------------- 交接快照与原核验流程不变


def test_review_activity_does_not_rewrite_handover_or_verification(client):
    """复核看板查询与处置均不改写交接记录、不改变核验单状态与修订号。"""
    release_batches(client, "B1")
    ship(client, "H1", "V1", ["B1"])
    before_handover = client.get("/api/handovers/H1").json()
    before_doc = client.get("/api/verifications/V1").json()

    # 更正使单据转待复核，再复核处置
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"
    state = current_state(client, "H1", "B1")
    dispose(client, "H1", "B1", "RECALL_REQUIRED", "评估后需召回", state)

    # 交接记录与其逐批冻结快照保持原样
    after_handover = client.get("/api/handovers/H1").json()
    assert after_handover == before_handover
    assert after_handover["batches"][0]["adjudication"]["version"] == 1
    # 归属追溯仍指向 H1
    trace = client.get("/api/batches/B1/handover").json()
    assert trace["shipped"] is True
    assert trace["handover"]["handover_no"] == "H1"

    # 原核验流程不变：待复核 -> 重新核验 -> 再次确认
    doc = client.get("/api/verifications/V1").json()
    assert doc["status"] == "PENDING_REVIEW"
    assert doc["revision"] == before_doc["revision"]  # 复核处置不推进单据修订号
    assert client.post("/api/verifications/V1/reverify").status_code == 200
    doc = confirm(client, "V1")
    assert doc["status"] == "CONFIRMED"
    # 历次确认快照只追加保留
    assert len(doc["confirmations"]) == 2


def test_review_board_reflects_multiple_independent_batches(client):
    """同一交接内不同批次各自独立形成复核事项与处置状态。"""
    # 分线登记，避免同线重算连带：B1(L1) 裁决版本变化；B2(L2) 出货资格版本变化；B3(L3) 不变
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", line_id="L1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", line_id="L2", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B3", line_id="L3", lots=["RM-M"], declared=["milk"])
    ship(client, "H1", "V1", ["B1", "B2", "B3"])

    add_lot(client, "RM-X", "可可", [])
    client.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
    client.put("/api/batches/B2", json={"material_lots": ["RM-M", "RM-X"]})
    client.post("/api/material-lots/RM-X/lock", json={"reason": "待检"})

    board = review_board(client, "H1")
    assert board["review_summary"] == {
        "total": 3,
        "not_required": 1,
        "pending_review": 2,
        "reviewed": 0,
    }
    # 只处置 B1，B2 仍待复核
    dispose(
        client, "H1", "B1", "NO_RECALL_NEEDED", "声明扩充无风险",
        board_entry(board, "B1")["current_state"],
    )
    board = review_board(client, "H1")
    assert board["review_summary"] == {
        "total": 3,
        "not_required": 1,
        "pending_review": 1,
        "reviewed": 1,
    }
    assert board_entry(board, "B1")["review_status"] == "REVIEWED"
    assert board_entry(board, "B2")["review_status"] == "PENDING_REVIEW"
    assert board_entry(board, "B3")["review_status"] == "NOT_REQUIRED"


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_without_review_table_upgrades_and_works(tmp_path):
    """无复核表的旧库启动后幂等建表，既有交接可查询看板并正常处置。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE allergen (code TEXT PRIMARY KEY, name_zh TEXT NOT NULL);
        CREATE TABLE material_lot (lot_no TEXT PRIMARY KEY, material TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE line (line_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE batch (batch_no TEXT PRIMARY KEY, line_id TEXT NOT NULL,
            seq INTEGER NOT NULL, material_lots TEXT NOT NULL DEFAULT '[]',
            declared TEXT NOT NULL DEFAULT '[]', cleaned TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL);
        CREATE TABLE batch_material (batch_no TEXT NOT NULL, lot_no TEXT NOT NULL,
            PRIMARY KEY (batch_no, lot_no));
        CREATE TABLE material_lot_lock (lot_no TEXT PRIMARY KEY, locked INTEGER NOT NULL,
            reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_no TEXT NOT NULL, version INTEGER NOT NULL, locked INTEGER NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL, locked_lots TEXT NOT NULL DEFAULT '[]',
            reasons TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
            locked_lots TEXT NOT NULL DEFAULT '[]', reasons TEXT NOT NULL DEFAULT '[]',
            trigger TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE adjudication (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
            actual TEXT NOT NULL DEFAULT '[]', undeclared TEXT NOT NULL DEFAULT '[]',
            disposition TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
            sources TEXT NOT NULL DEFAULT '{}', decided_at TEXT NOT NULL);
        CREATE TABLE adjudication_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, line_id TEXT NOT NULL, seq INTEGER NOT NULL,
            version INTEGER NOT NULL, actual TEXT NOT NULL DEFAULT '[]',
            undeclared TEXT NOT NULL DEFAULT '[]', disposition TEXT NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '{}',
            trigger TEXT NOT NULL, decided_at TEXT NOT NULL);
        CREATE TABLE verification (verification_no TEXT PRIMARY KEY, status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE verification_batch (verification_no TEXT NOT NULL, batch_no TEXT NOT NULL,
            position INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]', snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (verification_no, batch_no));
        CREATE TABLE verification_confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE verification_confirmation_batch (confirmation_id INTEGER NOT NULL,
            verification_no TEXT NOT NULL, position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_disposition TEXT NOT NULL,
            snapshot_actual TEXT NOT NULL DEFAULT '[]', snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]');
        INSERT INTO allergen(code,name_zh) VALUES ('milk','乳');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        store = app.state.store
        with store.read() as db:
            names = {
                r["name"]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "shipment_handover_review" in names

        release_batches(c, "B1")
        ship(c, "H1", "V1", ["B1"])
        board = review_board(c, "H1")
        assert board["review_summary"]["not_required"] == 1

        c.put("/api/batches/B1", json={"declared_allergens": ["milk", "egg"]})
        state = current_state(c, "H1", "B1")
        body = dispose(c, "H1", "B1", "NO_RECALL_NEEDED", "旧库升级后正常处置", state)
        assert body["review_status"] == "REVIEWED"
