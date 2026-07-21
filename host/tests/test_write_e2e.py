"""v1.1 写路径一等样例（examples/oper-dispatch.osca）的公仓端到端（GPT Review 复审 P2）：
真实执行器打 fake 后端——真实 sql_readonly 读本地 fake sqlite + 真实 openapi POST 打本地
http.server，走完 挂起 → approve 恢复消费真写落地 / deny 回落零写。此前该写样例只被 CI lint
之外的手工检查覆盖——样例回归时 CI 仍可能全绿。

立身口径（诚实标注）：验的是「样例包 + 参考适配器的机制契约」，测 fake 后端——非生产系统写验证。
"""

from __future__ import annotations

import http.server
import json
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

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
    rows = body["CON-201.拉取待下发处置清单"]  # 被写内容 = 取数步产物（params 穿透）
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
