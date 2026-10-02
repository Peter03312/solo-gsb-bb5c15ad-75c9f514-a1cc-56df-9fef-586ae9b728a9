#!/usr/bin/env python3
"""食品换线放行服务 —— 发运后复核（已发运批次风险复核）端到端调用示例（仅标准库）。

演示流程（独立产线 LHR，交接号与核验单号带时间戳，可重复运行）：
  1. 登记原料批号与 3 个全部 RELEASE/SHIPPABLE 的批次 HR001/HR002/HR003，
     创建核验单、确认并完成装车交接（整单发运）
  2. 复核看板：按交接号逐批并列交接冻结快照与当前版本/结论/作废状态
     —— 刚交接后全部 NOT_REQUIRED
  3. 交接后更正 HR001（声明扩充，结论仍 RELEASE）-> 裁决版本递增，形成复核事项
     （即使结论仍为放行）；过期所见状态提交处置被 409 拒绝；
     凭最新所见状态记录“无需召回”-> 已复核；同一状态重复处置被 409 拒绝
  4. 原料批号待检锁定 -> HR001/HR002 出货资格版本递增，再次待复核；
     凭所见两类当前版本记录“需召回”-> 已复核，处置历史只追加
  5. 已发运的 HR003 被作废 -> 作废标记本身形成复核事项；记录“需召回”
  6. 拒绝场景：未知交接 404、非其批次 404、空原因 400、无复核事项 409
  7. 交接记录与原核验流程保持不变：交接快照冻结不改，单据可重新核验再确认

用法：
    python examples/handover_review_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/handover_review_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LHR"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> None:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        ship = body["shipment_eligibility"]
        print(f"  已登记批次 {batch_no} => 裁决 {adj['disposition']}（v{adj['version']}），"
              f"出货资格 {ship['status']}（v{ship['version']}）")
    elif status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记批次失败: {body}")


def board(handover_no: str) -> dict:
    status, body = call("GET", f"/api/handovers/{handover_no}/review")
    assert status == 200, body
    return body


def entry(b: dict, batch_no: str) -> dict:
    return next(x for x in b["batches"] if x["batch_no"] == batch_no)


def print_board_summary(b: dict) -> None:
    s = b["review_summary"]
    print(f"  看板汇总：共 {s['total']} 批，无需复核 {s['not_required']}，"
          f"待复核 {s['pending_review']}，已复核 {s['reviewed']}")
    for x in b["batches"]:
        st = x["current_state"]
        print(f"    {x['batch_no']}: {x['review_status']}"
              f"（交接快照 裁决v{x['handover_snapshot']['adjudication']['version']}"
              f"/资格v{x['handover_snapshot']['shipment_eligibility']['version']}；"
              f"当前 裁决v{st['adjudication_version']}/资格v{st['shipment_version']}"
              f"/作废={st['voided']}）")


def dispose(handover_no: str, batch_no: str, decision: str, reason: str, state: dict) -> dict:
    status, body = call("POST", f"/api/handovers/{handover_no}/review", {
        "batch_no": batch_no,
        "decision": decision,
        "reason": reason,
        "expected_adjudication_version": state["adjudication_version"],
        "expected_shipment_version": state["shipment_version"],
        "expected_voided": state["voided"],
    })
    assert status == 201, body
    return body


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    lot = f"HR-RM-MILK-{suffix}"
    v1 = f"HRV-{suffix}"
    h1 = f"HRH-{suffix}"
    b1, b2, b3 = f"HR001-{suffix}", f"HR002-{suffix}", f"HR003-{suffix}"

    section("1. 准备数据：3 个全部 RELEASE/SHIPPABLE 的批次，确认核验单并完成装车交接")
    ensure_lot(lot, "复核示例奶粉", ["milk"])
    for bn in (b1, b2, b3):
        ensure_batch(bn, {
            "batch_no": bn, "line_id": LINE,
            "material_lots": [lot],
            "declared_allergens": ["milk"], "cleaned_allergens": [],
        })
    status, body = call("POST", "/api/verifications", {
        "verification_no": v1, "batch_nos": [b1, b2, b3],
    })
    assert status == 201, body
    status, doc = call("POST", f"/api/verifications/{v1}/confirm")
    assert status == 200 and doc["status"] == "CONFIRMED"
    status, body = call("POST", "/api/handovers", {
        "handover_no": h1, "verification_no": v1, "expected_revision": doc["revision"],
    })
    assert status == 201, body
    print(f"  已交接 {h1}（核验单 {v1}，冻结修订号 v{body['verification_revision']}，"
          f"{body['batch_count']} 批整单发运）")

    section("2. 复核看板：刚交接后全部 NOT_REQUIRED（快照与当前一致）")
    b = board(h1)
    print_board_summary(b)
    assert b["review_summary"]["pending_review"] == 0
    assert all(x["review_status"] == "NOT_REQUIRED" for x in b["batches"])

    section("3. 交接后更正（结论仍 RELEASE）形成复核事项；凭所见状态处置")
    status, body = call("PUT", f"/api/batches/{b1}", {"declared_allergens": ["milk", "egg"]})
    assert status == 200, body
    b = board(h1)
    print(f"  更正 {b1} 声明（仍 RELEASE，裁决 v1->v2，同线后续批次一并重算）：")
    print_board_summary(b)
    e1 = entry(b, b1)
    assert e1["review_status"] == "PENDING_REVIEW"
    assert e1["current"]["adjudication"]["disposition"] == "RELEASE"
    assert e1["review_reasons"][0]["code"] == "ADJUDICATION_VERSION_CHANGED"

    # 过期所见状态（仍按交接时的 v1 提交）-> 409 拒绝，不写入
    status, body = call("POST", f"/api/handovers/{h1}/review", {
        "batch_no": b1, "decision": "NO_RECALL_NEEDED", "reason": "基于过期状态提交",
        "expected_adjudication_version": 1,
        "expected_shipment_version": 1,
        "expected_voided": False,
    })
    print(f"[{status}] 过期所见状态 -> {body['error']['code']}"
          f"（当前 {body['error']['details'][0]['current_state']}）")
    assert status == 409 and body["error"]["code"] == "REVIEW_STATE_CONFLICT"

    # 凭看板中的最新所见状态处置
    state = entry(board(h1), b1)["current_state"]
    body = dispose(h1, b1, "NO_RECALL_NEEDED", "仅声明扩充，未引入新过敏原，无需召回", state)
    print(f"[201] 已处置 {b1}：{body['disposition']['decision_label']}"
          f"（{body['disposition']['reason']}）-> {body['review_status']}")

    # 同一状态不得重复处置
    status, body = call("POST", f"/api/handovers/{h1}/review", {
        "batch_no": b1, "decision": "RECALL_REQUIRED", "reason": "重复提交同一状态",
        "expected_adjudication_version": state["adjudication_version"],
        "expected_shipment_version": state["shipment_version"],
        "expected_voided": state["voided"],
    })
    print(f"[{status}] 同一状态重复处置 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_REVIEW_DISPOSITION"

    section("4. 原料批号待检锁定：出货资格版本递增，再次待复核；逐批处置")
    status, body = call("POST", f"/api/material-lots/{lot}/lock",
                        {"reason": "供应商通报黄曲霉毒素风险，待复检"})
    assert status == 200, body
    b = board(h1)
    print(f"  锁定 {lot} 后（直接使用批次出货资格版本递增，再次待复核）：")
    print_board_summary(b)
    for bn in (b1, b2):
        e = entry(b, bn)
        assert e["review_status"] == "PENDING_REVIEW"
        assert e["current"]["shipment_eligibility"]["status"] == "BLOCKED"
        body = dispose(h1, bn, "RECALL_REQUIRED",
                       "直接使用批号待检锁定，启动召回评估", e["current_state"])
        print(f"[201] 已处置 {bn}：{body['disposition']['decision_label']} -> 已复核")
    b = board(h1)
    e1 = entry(b, b1)
    print(f"  {b1} 处置历史（只追加，{len(e1['dispositions'])} 条）："
          + "；".join(f"{d['decision_label']}@{d['decided_at']}" for d in e1["dispositions"]))
    assert len(e1["dispositions"]) == 2

    section("5. 已发运批次被作废：作废标记本身形成复核事项")
    status, body = call("POST", f"/api/batches/{b3}/void",
                        {"reason": "计划误登记，该批实际未投产"})
    assert status == 200, body
    b = board(h1)
    e3 = entry(b, b3)
    print(f"  {b3} 作废后：review_status={e3['review_status']}，"
          f"原因={[r['code'] for r in e3['review_reasons']]}")
    assert e3["review_status"] == "PENDING_REVIEW"
    assert "BATCH_VOIDED" in [r["code"] for r in e3["review_reasons"]]
    body = dispose(h1, b3, "RECALL_REQUIRED",
                   "批次作废但已随交接发运，需召回核实", e3["current_state"])
    print(f"[201] 已处置 {b3}：{body['disposition']['decision_label']} -> 已复核")
    print_board_summary(board(h1))

    section("6. 拒绝场景：未知交接 / 非其批次 / 空原因 / 无复核事项")
    status, body = call("GET", f"/api/handovers/NO-SUCH-HANDOVER/review")
    print(f"[{status}] 未知交接看板 -> {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "HANDOVER_NOT_FOUND"

    status, body = call("POST", f"/api/handovers/{h1}/review", {
        "batch_no": "HR-UNKNOWN", "decision": "NO_RECALL_NEEDED", "reason": "x",
        "expected_adjudication_version": 1,
        "expected_shipment_version": 1,
        "expected_voided": False,
    })
    print(f"[{status}] 非其批次 -> {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "BATCH_NOT_IN_HANDOVER"

    e1 = entry(board(h1), b1)
    status, body = call("POST", f"/api/handovers/{h1}/review", {
        "batch_no": b1, "decision": "NO_RECALL_NEEDED", "reason": "   ",
        "expected_adjudication_version": e1["current_state"]["adjudication_version"],
        "expected_shipment_version": e1["current_state"]["shipment_version"],
        "expected_voided": e1["current_state"]["voided"],
    })
    print(f"[{status}] 空原因 -> {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD"

    # 解除锁定后 b1/b2 出货资格再次递增 -> 待复核；处置后再次无事项时提交 -> 409
    call("POST", f"/api/material-lots/{lot}/unlock", {"reason": "复检合格，解除待检"})
    for bn in (b1, b2):
        e = entry(board(h1), bn)
        dispose(h1, bn, "NO_RECALL_NEEDED", "复检合格解除锁定，无需召回", e["current_state"])
    e1 = entry(board(h1), b1)
    status, body = call("POST", f"/api/handovers/{h1}/review", {
        "batch_no": b1, "decision": "NO_RECALL_NEEDED", "reason": "同一状态再次提交",
        "expected_adjudication_version": e1["current_state"]["adjudication_version"],
        "expected_shipment_version": e1["current_state"]["shipment_version"],
        "expected_voided": e1["current_state"]["voided"],
    })
    print(f"[{status}] 已复核状态重复处置 -> {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "DUPLICATE_REVIEW_DISPOSITION"

    section("7. 交接快照与原核验流程保持不变")
    status, ho = call("GET", f"/api/handovers/{h1}")
    assert status == 200
    assert all(x["adjudication"]["version"] == 1 for x in ho["batches"])
    print(f"  交接记录 {h1} 冻结：逐批确认快照裁决仍为 v1（复核处置不改写交接记录）")
    status, doc = call("GET", f"/api/verifications/{v1}")
    print(f"  核验单 {v1} 当前状态 {doc['status']}（交接后的更正/锁定使其转待复核）")
    assert doc["status"] == "PENDING_REVIEW"
    # 含已作废批次的单据重新核验/确认均拒绝（原核验流程行为不变）
    status, body = call("POST", f"/api/verifications/{v1}/reverify")
    print(f"[{status}] 含作废批次单据重新核验 -> {body['error']['code']}（原核验流程不变）")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"
    status, body = call("POST", f"/api/verifications/{v1}/confirm")
    print(f"[{status}] 含作废批次单据确认 -> {body['error']['code']}（原核验流程不变）")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"

    print("\n全部发运后复核流程演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
