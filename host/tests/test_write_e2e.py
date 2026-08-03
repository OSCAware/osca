"""v1.1 写路径一等样例（examples/oper-dispatch.osca）的公仓端到端（GPT Review 复审 P2）：
真实执行器打 fake 后端——真实 sql_readonly 读本地 fake sqlite + 真实 openapi POST 打本地
http.server，走完 挂起 → approve 恢复消费真写落地 / deny 回落零写。此前该写样例只被 CI lint
之外的手工检查覆盖——样例回归时 CI 仍可能全绿。

立身口径（诚实标注）：验的是「样例包 + 参考适配器的机制契约」，测 fake 后端——非生产系统写验证。
"""

from __future__ import annotations

import copy
import http.server
import json
import re
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from osca_cli.llm import LLMReply

from osca_host.connector import ConnectorProxy
from osca_host.episode import assemble
from osca_host.loader import load_for_host
from osca_host.policy import PolicyInterceptor, ledger_stats
from osca_host.runner import run_episode

DISPATCH = Path(__file__).resolve().parents[2] / "examples" / "oper-dispatch.osca"
WRITE_REF = "CON-202.下发处置工单"


@pytest.fixture
def dispatch_pack(tmp_path) -> Path:
    """样例包 tmp 副本（装载会重建索引，不许写回仓库）。"""
    assert DISPATCH.is_dir(), f"写样例包缺失：{DISPATCH}"
    root = tmp_path / DISPATCH.name
    shutil.copytree(DISPATCH, root, ignore=shutil.ignore_patterns("indexes"))
    return root


@pytest.fixture
def ops_db(tmp_path) -> Path:
    """fake 经营指标库：对应 sql/dispatch_worklist.sql 的视图结构，写入一行待下发处置。"""
    db = tmp_path / "ops.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE 待下发处置清单视图(工单标题,目标单位,处置动作,建议完成日,经办人手机)")
    conn.execute("INSERT INTO 待下发处置清单视图 VALUES('压降差旅费','甲厂','限额审批','2026-08-01','13812345678')")
    conn.commit()
    conn.close()
    return db


class _DispatchCapture(http.server.BaseHTTPRequestHandler):
    """fake 工单下发系统：捕获 POST 原文供断言（真实执行器回执无 mock 的 landed/applied 键，
    被写内容验证须在 server 侧做——与 W7-2 集成工程同手法）。"""

    received: list[tuple[str, dict]] = []

    def do_POST(self):  # noqa: N802 —— BaseHTTPRequestHandler 命名约定
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append((self.path, json.loads(body)))
        payload = json.dumps({"ticket": "WO-0001", "accepted": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静音测试输出
        pass


@pytest.fixture
def dispatch_api():
    _DispatchCapture.received = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DispatchCapture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _load_dispatch(dispatch_pack, ops_db, api_addr):
    result, loaded = load_for_host(dispatch_pack, require_bindings=False)
    assert result.ok, result.lines
    policy_file = loaded.pack.yaml_files["policy.yaml"]
    policy = PolicyInterceptor(loaded.package_id, policy_file.mapping, ledger_stats(loaded.pack))
    # 部署侧 egress 白名单等价注入：fake 后端动态端口/回环地址不进包（包内占位域见 policy.yaml 注释）
    policy.egress_allow |= {"localhost", "127.0.0.1"}
    bindings = {
        "OPS_DB": {"endpoint": f"sql_readonly://localhost{ops_db}"},
        "DISPATCH_API": {"endpoint": f"openapi://{api_addr}"},
    }
    proxy = ConnectorProxy(loaded, bindings, policy)
    aware = next(a for a in loaded.awares if a.aware_id == "AW-201")
    episode = assemble("EP-0001", loaded, aware, "AW-201/T2")
    return loaded, policy, proxy, episode


def test_dispatch_sample_approve_resumes_and_write_lands(dispatch_pack, ops_db, dispatch_api):
    """approve 线：取数（真实 sqlite ro）→ 写命中审批门挂起（零写）→ approve → 恢复消费 →
    真实 openapi POST 落地，被写内容 = 上游真取清单（params 穿透，server 侧捕获验证）。"""
    loaded, policy, proxy, episode = _load_dispatch(dispatch_pack, ops_db, dispatch_api)

    episode = run_episode(episode, loaded, proxy, policy)
    assert episode.status == "suspended_pending_approval"
    assert _DispatchCapture.received == []  # 批准前零写

    [ch] = policy.pending_challenges()
    assert ch["action"] == WRITE_REF and ch["approver"] == "处置审批人"  # OSCA025 锁的 ref 逐字对应
    ok, _ = policy.decide_challenge(ch["challenge_id"], by_name="处置审批人", by_role="approver", approve=True)
    assert ok

    episode = run_episode(episode, loaded, proxy, policy)  # 恢复重入：consume-only → 真写
    assert episode.status == "completed"
    assert next(s for s in episode.steps if s["step"] == "下发")["status"] == "done"

    [(path, body)] = _DispatchCapture.received
    assert path == "/dispatch"
    # 被写内容 = 取数步产物（params 穿透）。样例包的写步声明了 `from`（M8-T3-c），故上 wire 的是
    # 回执字典里的**那一格**——即 CON-202 接口 `params: {ref: OBJ-201}` 声明的那份清单本身。
    # 断言随之从「取字典那一格」改成「就是清单」：改的是样例包表达，钉住的仍是同一件事——
    # 上 wire 的被写内容逐字等于上游真取数结果（含脱敏口径）。
    rows = body
    assert "CON-201.拉取待下发处置清单" not in body  # 旧形状（一坨读回执）不再上 wire
    assert rows[0]["目标单位"] == "甲厂" and rows[0]["处置动作"] == "限额审批"
    assert rows[0]["经办人手机"] == "***手机号已脱敏***"  # 注入剧集前已脱敏，PII 不出域


def test_dispatch_sample_deny_falls_back_writes_nothing(dispatch_pack, ops_db, dispatch_api):
    """deny 线：驳回 → 恢复走回落保守默认（不写）——剧集 completed（非 failed）、写步记 denied、
    fake server 全程零请求。"""
    loaded, policy, proxy, episode = _load_dispatch(dispatch_pack, ops_db, dispatch_api)

    episode = run_episode(episode, loaded, proxy, policy)
    assert episode.status == "suspended_pending_approval"
    [ch] = policy.pending_challenges()
    ok, _ = policy.decide_challenge(ch["challenge_id"], by_name="处置审批人", by_role="approver", approve=False)
    assert ok

    episode = run_episode(episode, loaded, proxy, policy)
    assert episode.status == "completed"
    assert next(s for s in episode.steps if s["step"] == "下发")["status"] == "denied"
    assert _DispatchCapture.received == []  # 驳回 = 零写


# ── M8-T3：读 → agent 结构化整形 → 写（真实执行器打 fake 后端的端到端） ──────────

SHAPE_STEP = {
    "step": "整形",
    "performer": "agent",
    # input.from 收窄到那一格（T3-c）：整形步吃的是那批工单行本身，不是 {接口ref: 回执} 包装
    "input": {"ref": "OBJ-201", "from": "CON-201.拉取待下发处置清单"},
    "produces": {"ref": "待写工单行", "as": "json"},  # 结构化产出（T3-b）
}


class _ShapingLLM:
    """整形步的 LLM 替身。调用路径与真实通道同构：runner 只经 `complete(system, user, tag=, timeout=)`
    拿 LLMReply，本替身同签名同返回类型（aware 声明了 max_minutes，故必须收 timeout）。

    产出**不是常量**——它从提示词里取上游取数结果的字段再拼信封，故「结构化产物整形自上游确定性取数
    结果」这条在本测试里是走通的，不是假设的。诚实标注：这验的是管道机制，不是模型整形能力。
    """

    model = "fake-shaper"

    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, *, tag: str, timeout: float | None = None) -> LLMReply:
        self.prompts.append(user)
        picked = {k: re.search(rf"^\s*{k}:\s*(\S+)$", user, re.M) for k in ("目标单位", "处置动作", "建议完成日")}
        assert all(picked.values()), f"上游取数结果没进提示词：{user[-400:]}"
        单位, 动作, 完成日 = (m.group(1).strip("'\"") for m in picked.values())  # YAML 会给日期串加引号
        envelope = {
            "说明": f"建议对{单位}下发「{动作}」，完成日 {完成日}。",
            "数据": {"工单标题": f"{单位}·{动作}", "目标单位": 单位, "处置动作": 动作, "建议完成日": 完成日},
        }
        return LLMReply(text=json.dumps(envelope, ensure_ascii=False), tokens=180, model=self.model)


def test_shaped_write_lands_one_real_row_not_the_receipt_dict(dispatch_pack, ops_db, dispatch_api):
    """M8-T3 端到端（真实 sqlite ro 读 + 真实 openapi 写打 fake 后端）：
    读 → agent 整形 → 写，**上 wire 的 body 是一行真正要写的数据**（按列名的平铺行），
    不再是 `{接口ref: 读回执}` 那坨字典——这正是数据台按列名校验会 400 的那个形状差。
    并存与可溯源一并钉住：episode.draft 仍是人话，整形步记录带 derived_from。"""
    loaded, policy, proxy, episode = _load_dispatch(dispatch_pack, ops_db, dispatch_api)
    episode.context = copy.deepcopy(episode.context)  # structure 与包共享引用，改前先拷贝
    pipeline = episode.context["structure"]["pipeline"]
    pipeline.insert(1, copy.deepcopy(SHAPE_STEP))
    pipeline[2]["input"] = {"ref": "待写工单行"}  # 写步改吃整形产物（原声明是 {ref: OBJ-201}）
    llm = _ShapingLLM()

    episode = run_episode(episode, loaded, proxy, policy, llm=llm)
    assert episode.status == "suspended_pending_approval"
    assert _DispatchCapture.received == []  # 批准前零写

    # draft 并存：人话草稿仍在（capture/frontdesk/控制台快照的消费口径没被结构化产出夺走）
    assert episode.draft == "建议对甲厂下发「限额审批」，完成日 2026-08-01。"
    # 可溯源（机器可查）：这份结构化产物整形自哪个上游产物的哪一格
    shaped = next(s for s in episode.steps if s["step"] == "整形")
    assert shaped["derived_from"] == {"input": "OBJ-201", "from": "CON-201.拉取待下发处置清单"}
    assert shaped["produced"] == "待写工单行" and shaped["produced_as"] == "json"

    # W6-4 在真路径上兑现：审批卡呈的是那一行、审批人看得懂自己在批什么
    [ch] = policy.pending_challenges()
    assert ch["payload_display"] == {
        "工单标题": "甲厂·限额审批",
        "目标单位": "甲厂",
        "处置动作": "限额审批",
        "建议完成日": "2026-08-01",
    }
    ok, _ = policy.decide_challenge(ch["challenge_id"], by_name="处置审批人", by_role="approver", approve=True)
    assert ok

    episode = run_episode(episode, loaded, proxy, policy, llm=llm)  # 恢复重入：consume-only → 真写
    assert episode.status == "completed"

    [(path, body)] = _DispatchCapture.received
    assert path == "/dispatch"
    assert body == ch["payload_display"]  # 一行平铺、按列名——不是 {接口ref: 回执}
    assert "CON-201.拉取待下发处置清单" not in body  # 旧形状不再上 wire
