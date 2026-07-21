"""真实执行器参考适配器（W6-3）——测 **fake 后端**（本地 sqlite 文件 / 本地 http.server）。

立身口径（诚实标注）：这些验的是「适配器契约真生效」（只读强制 / 参数化防注入 / method-params / 非 2xx /
secret 作鉴权头不外泄 / 不跟随重定向）——**非生产库/生产 API 的真系统验证**（那属部署侧适配，1.1/部署验收）。
"""

from __future__ import annotations

import http.server
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from osca_host.executor import OpenapiExecutor, SqlReadonlyExecutor

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "oper-diagnosis.osca"  # 用真实样例 impl SQL


# ── sql_readonly 参考适配器（sqlite ro） ─────────────────────────────


def _make_fee_db(tmp_path):
    """建一份 fake 财务库（对应样例 sql/fee_detail.sql 的表结构），写入两行。"""
    db = tmp_path / "fin.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE 合并报表_费用明细(单位名称,费用科目,统计周期,环比涨幅,绝对金额)")
    conn.executemany(
        "INSERT INTO 合并报表_费用明细 VALUES(?,?,?,?,?)",
        [("甲厂", "差旅费", "2026-07", 0.3, 45), ("乙厂", "差旅费", "2026-06", 0.1, 20)],
    )
    conn.commit()
    conn.close()
    return db


def _run_sql(db, impl, params, *, pack_root=EXAMPLE, is_write=False):
    return SqlReadonlyExecutor().execute(
        endpoint=f"sql_readonly://localhost{db}",
        interface={"impl": impl},
        params=params,
        secret=None,
        is_write=is_write,
        pack_root=pack_root,
    )


def test_sql_readonly_reads_with_named_param_binding(tmp_path):
    """跑真实 impl SQL（命名参数 :统计周期/:费用科目），只读连接回结果——参数过滤生效。"""
    rows, err = _run_sql(_make_fee_db(tmp_path), "sql/fee_detail.sql", {"统计周期": "2026-07", "费用科目": None})
    assert err is None
    assert rows == [
        {"单位名称": "甲厂", "费用科目": "差旅费", "统计周期": "2026-07", "环比涨幅": 0.3, "绝对金额": 45}
    ]  # 只回 2026-07 的甲厂，乙厂 2026-06 被 :统计周期 过滤


def test_sql_readonly_params_parameterized_not_injected(tmp_path):
    """防注入：params 含 SQL 注入尝试 → 作为**绑定值**、不改查询结构（无匹配即空，不泄全表、不炸）。"""
    rows, err = _run_sql(
        _make_fee_db(tmp_path), "sql/fee_detail.sql", {"统计周期": "2026-07' OR '1'='1", "费用科目": None}
    )
    assert err is None and rows == []  # 注入串作为值 → 无匹配，未被解释为 SQL


def test_sql_readonly_rejects_write_via_connection_mode(tmp_path):
    """只读强制靠**连接模式**（mode=ro），非关键字黑名单：ro 连接对写 SQL 天然拒，且写不落地。"""
    db = _make_fee_db(tmp_path)
    # 先证 db 能开能读（排除「unable to open」假阳性）
    ok_rows, ok_err = _run_sql(db, "sql/fee_detail.sql", {"统计周期": "2026-07", "费用科目": None})
    assert ok_err is None and ok_rows  # 读得到
    # 同一 db 上跑写 SQL → ro 连接拒（不是打不开）
    impl = tmp_path / "w.sql"
    impl.write_text("INSERT INTO 合并报表_费用明细 VALUES('丙厂','x','2026-07',9,9)", encoding="utf-8")
    rows, err = _run_sql(db, "w.sql", {}, pack_root=tmp_path)
    assert rows is None and err is not None  # 写被拒
    # 写确实没落地（真拒、非静默吞）：重开只读读同筛选仍只有原来的甲厂一行
    after, _ = _run_sql(db, "sql/fee_detail.sql", {"统计周期": "2026-07", "费用科目": None})
    assert after == ok_rows  # 表未被写改


def test_sql_readonly_refuses_write_path(tmp_path):
    """写连接器不走 sql_readonly（is_write=True 直接拒）——写走写执行器 + 审批门（B.4）。"""
    rows, err = _run_sql(_make_fee_db(tmp_path), "sql/fee_detail.sql", {}, is_write=True)
    assert rows is None and "只读" in err


def test_sql_readonly_authorizer_allows_recursive_cte(tmp_path):
    """GPT 复审：授权器须放行合法 WITH RECURSIVE CTE（SQLITE_RECURSIVE，只读、不开写）——别把普通读之外误拒。"""
    db = _make_fee_db(tmp_path)
    impl = tmp_path / "rec.sql"
    impl.write_text(
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<3) SELECT n FROM c", encoding="utf-8"
    )
    rows, err = _run_sql(db, "rec.sql", {}, pack_root=tmp_path)
    assert err is None and rows == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_sql_readonly_authorizer_denies_attach_and_vacuum(tmp_path):
    """GPT 外审：`mode=ro` 只护主库——单条 VACUUM INTO / ATTACH DATABASE 能建新文件。授权器一并拒、不建文件。"""
    db = _make_fee_db(tmp_path)
    for stmt, made in (
        (f"VACUUM INTO '{tmp_path / 'v.db'}'", "v.db"),
        (f"ATTACH DATABASE '{tmp_path / 'a.db'}' AS x", "a.db"),
    ):
        impl = tmp_path / "bad.sql"
        impl.write_text(stmt, encoding="utf-8")
        rows, err = _run_sql(db, "bad.sql", {}, pack_root=tmp_path)
        assert rows is None and err is not None, stmt  # 被授权器拒
        assert not (tmp_path / made).exists(), f"{stmt} 建成了文件（授权器未拦）"


def test_sql_readonly_missing_impl_errors(tmp_path):
    rows, err = _run_sql(_make_fee_db(tmp_path), "sql/nope.sql", {})
    assert rows is None and "impl SQL 缺失" in err


def test_sql_readonly_missing_impl_field_errors(tmp_path):
    rows, err = SqlReadonlyExecutor().execute(
        endpoint=f"sql_readonly://localhost{_make_fee_db(tmp_path)}",
        interface={},  # 无 impl
        params={},
        secret=None,
        is_write=False,
        pack_root=EXAMPLE,
    )
    assert rows is None and "impl" in err and "即席 SQL" in err  # 不接受模型即席 SQL（公理 A6）


def test_sql_readonly_multistatement_impl_fails_closed(tmp_path):
    """对抗审查捉：多语句 impl（触发 sqlite3.Warning——Error 的兄弟）→ 执行器捕获成 error 回执，不抛、不改库。"""
    db = _make_fee_db(tmp_path)
    impl = tmp_path / "multi.sql"
    impl.write_text("SELECT 1; DELETE FROM 合并报表_费用明细;", encoding="utf-8")
    rows, err = _run_sql(db, "multi.sql", {}, pack_root=tmp_path)
    assert rows is None and "sql_readonly 执行失败" in err  # sqlite3.Warning/ProgrammingError 被捕获，非崩穿
    after, _ = _run_sql(db, "sql/fee_detail.sql", {"统计周期": "2026-07", "费用科目": None})
    assert after  # 多语句被 execute 层拦（+ ro 双保险）→ 表未被 DELETE


# ── openapi 参考适配器（urllib + 本地 http.server） ──────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默，别刷测试输出
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/notfound"):
            self.send_response(404)
            self.end_headers()
            return
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/data")
            self.end_headers()
            return
        if self.path.startswith("/notjson"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not json at all")
            return
        self._json(200, {"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8") if n else ""
        self._json(201, {"method": "POST", "body": body, "auth": self.headers.get("Authorization")})


@pytest.fixture
def http_addr():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    try:
        yield f"{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _run_http(addr, interface, params, *, secret=None, is_write=False):
    return OpenapiExecutor().execute(
        endpoint=f"openapi://{addr}",
        interface=interface,
        params=params,
        secret=secret,
        is_write=is_write,
        pack_root=Path("."),
    )


def test_openapi_get_reads_json_with_query(http_addr):
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/data"}, {"q": "x"})
    assert err is None and payload["method"] == "GET" and "q=x" in payload["path"]


def test_openapi_secret_becomes_bearer_header(http_addr):
    """secret → Authorization: Bearer 头（发给预期接收方=服务器）；值不进回执 error（这里由服务器回显验证已送达）。"""
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/data"}, {}, secret="TKN-abc")
    assert err is None and payload["auth"] == "Bearer TKN-abc"


def test_openapi_no_secret_no_auth_header(http_addr):
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/data"}, {}, secret=None)
    assert err is None and payload["auth"] is None


def test_openapi_read_path_rejects_write_method(http_addr):
    """GPT 外审 blocker：读路径（is_write=False）用写 method（POST/DELETE…）→ fail-closed，否则绕审批门真写。"""
    for m in ("POST", "DELETE", "PUT", "PATCH"):
        payload, err = _run_http(http_addr, {"method": m, "path": "/write"}, {"x": 1}, is_write=False)
        assert payload is None and "绕过审批门" in err, m


def test_openapi_secret_over_nonhttps_nonloopback_fails_closed():
    """GPT 外审：携带 secret 走非 https 且非本地回环 → fail-closed（凭据明文外发风险），fail-closed 前不外呼。"""
    payload, err = OpenapiExecutor().execute(
        endpoint="openapi://api.example.com",
        interface={"method": "GET", "path": "/x"},
        params={},
        secret="TKN",
        is_write=False,
        pack_root=Path("."),
    )
    assert payload is None and "https" in err


def test_openapi_secret_over_http_loopback_allowed(http_addr):
    """本地回环允许 http + secret（参考适配器本地测试面）。"""
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/data"}, {}, secret="TKN")
    assert err is None and payload["auth"] == "Bearer TKN"


def test_openapi_post_writes_body(http_addr):
    payload, err = _run_http(http_addr, {"method": "POST", "path": "/write"}, {"改价": 4.5}, is_write=True)
    assert err is None and payload["method"] == "POST" and "改价" in payload["body"]


def test_openapi_write_defaults_to_post_when_method_unspecified(http_addr):
    payload, err = _run_http(http_addr, {"path": "/write"}, {"x": 1}, is_write=True)  # 无 method
    assert err is None and payload["method"] == "POST"


def test_openapi_non_2xx_is_error_without_body(http_addr):
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/notfound"}, {})
    assert payload is None and "非 2xx" in err and "404" in err


def test_openapi_redirect_not_followed_ssrf_guard(http_addr):
    """302 不跟随——防服务器重定向到内网/未授权 host 绕过 egress 白名单（SSRF 面）。"""
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/redirect"}, {})
    assert payload is None and "非 2xx" in err  # 302 作非 2xx


def test_openapi_non_json_response_is_error(http_addr):
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/notjson"}, {})
    assert payload is None and "非 JSON" in err


def test_openapi_path_anchored_no_host_extension(http_addr):
    """对抗审查捉·blocker SSRF：manifest path 无前导 / 时被锚定为 /path，不向右延展 netloc（不改连接 host）——
    否则 path='.evil.com/x' 会把真实连接引到 <host>.evil.com、并把 secret Bearer 送过去。"""
    payload, err = _run_http(http_addr, {"method": "GET", "path": ".evil.com/exfil"}, {})
    assert err is None  # 请求确实打到 http_addr（netloc host）——server 响应了，说明 host 未被延展
    assert payload["path"] == "/.evil.com/exfil"  # path 锚定以 /，未污染 authority


def test_openapi_response_body_over_cap_fails_closed(http_addr, monkeypatch):
    """对抗审查捉：巨响应体读上限 → fail-closed（不 OOM、call() 恒回 Receipt）。"""
    import osca_host.executor as ex_mod

    monkeypatch.setattr(ex_mod, "_MAX_BODY", 5)  # 上限压到 5 字节；/data 回的 JSON 远大于此
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/data"}, {})
    assert payload is None and "超限" in err


def test_openapi_truncated_response_fails_closed_not_partial(tmp_path):
    """对抗审查捉：响应截断（Content-Length 声明 100、实发 ~7）→ fail-closed，不把半截数据当取数结果、也不炸穿。"""
    import socket
    import threading

    def serve(sock):
        conn, _ = sock.accept()
        conn.recv(65536)
        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\nContent-Type: application/json\r\n\r\n{"x":1}')
        conn.close()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    threading.Thread(target=serve, args=(s,), daemon=True).start()
    try:
        payload, err = _run_http(f"127.0.0.1:{port}", {"method": "GET", "path": "/x"}, {})
        assert payload is None and "截断" in err  # 半截 JSON 虽可解析，仍拒（取数完整性）
    finally:
        s.close()


def test_sql_readonly_impl_path_escape_rejected(tmp_path):
    """GPT Review P1 路径越界：impl 是包内 manifest 声明（不可信输入）——`../` 与绝对路径把读引出
    包根（宿主机任意可读文件被当 SQL 送执行）→ 一律拒绝，不出包根。"""
    db = _make_fee_db(tmp_path)
    outside = tmp_path / "outside.sql"
    outside.write_text("SELECT 1 AS x", encoding="utf-8")
    pack = tmp_path / "pack"
    pack.mkdir()

    rows, err = _run_sql(db, "../outside.sql", {}, pack_root=pack)
    assert rows is None and "越界" in err  # 相对逃逸

    rows, err = _run_sql(db, str(outside), {}, pack_root=pack)
    assert rows is None and "越界" in err  # 绝对路径逃逸


def test_sql_readonly_impl_symlink_escape_rejected(tmp_path):
    """包内符号链接指向包外 SQL：resolve 后落在包根之外 → 同样拒绝（链接不是白手套）。"""
    db = _make_fee_db(tmp_path)
    outside = tmp_path / "outside.sql"
    outside.write_text("SELECT 1 AS x", encoding="utf-8")
    pack = tmp_path / "pack"
    (pack / "sql").mkdir(parents=True)
    (pack / "sql" / "linked.sql").symlink_to(outside)

    rows, err = _run_sql(db, "sql/linked.sql", {}, pack_root=pack)
    assert rows is None and "越界" in err


def test_sql_readonly_impl_symlink_loop_no_traceback(tmp_path):
    """GPT 三审 P2：impl 指向符号链接环——resolve_in_root（与 lint 同一判据）收敛为回执错误，
    RuntimeError 不许炸穿执行器。"""
    db = _make_fee_db(tmp_path)
    pack = tmp_path / "pack"
    (pack / "sql").mkdir(parents=True)
    loop = pack / "sql" / "loop.sql"
    loop.symlink_to(loop.name)  # 自指链接环
    rows, err = _run_sql(db, "sql/loop.sql", {}, pack_root=pack)
    assert rows is None and err  # 越界或缺失（按解释器版本收敛），恒不 traceback


def test_openapi_write_body_equals_approved_payload_for_all_json_types(http_addr):
    """P1：审批过什么就发什么——str/数字/bool/null/list/mapping 的 wire body 必须与被批 params
    完全一致（修复前标量被静默改写成 {}，击穿「审批展示、摘要、落地内容一致」）。"""
    for params in ("字符串内容", 42, 3.14, True, False, None, [1, "a", {"n": 2}], {"改价": 4.5}):
        payload, err = _run_http(http_addr, {"method": "POST", "path": "/write"}, params, is_write=True)
        assert err is None, (params, err)
        assert json.loads(payload["body"]) == params, params


def test_openapi_write_non_serializable_params_refused_not_rewritten(http_addr):
    """非 JSON 可序列化的 params：显式拒绝（fail-closed），绝不静默改写后落地。"""
    payload, err = _run_http(http_addr, {"method": "POST", "path": "/write"}, object(), is_write=True)
    assert payload is None and "非 JSON 可序列化" in err


def test_openapi_executor_bounds_urlopen_timeout_by_deadline(monkeypatch):
    """复核 P2：openapi 单次外呼上限 = min(默认 10s, 调用方剩余预算)——预算只剩 3s 不许再吊满 10s。"""
    import osca_host.executor as ex_mod

    seen = {}

    class _Opener:
        def open(self, req, timeout=None):
            seen["timeout"] = timeout
            raise OSError("到此为止（只验 timeout 传导）")

    monkeypatch.setattr(ex_mod, "_OPENER", _Opener())
    for deadline, expected in ((3.0, 3.0), (None, 10.0), (999.0, 10.0)):
        OpenapiExecutor().execute(
            endpoint="openapi://h.internal",
            interface={"method": "GET", "path": "/x"},
            params={},
            secret=None,
            is_write=False,
            pack_root=Path("."),
            timeout=deadline,
        )
        assert seen["timeout"] == expected, (deadline, seen)


def test_sql_readonly_progress_handler_enforces_absolute_deadline(tmp_path):
    """三轮复核 P2：sqlite connect timeout 只限锁等待——长查询本身靠 progress handler 按绝对
    deadline 中断（递归 CTE 亿级迭代在 ~0.05s 处被截停,fail-closed 错误回执）。"""
    import time as time_mod

    db = _make_fee_db(tmp_path)
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "slow.sql").write_text(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 100000000)\nSELECT count(*) FROM c",
        encoding="utf-8",
    )
    started = time_mod.monotonic()
    rows, err = SqlReadonlyExecutor().execute(
        endpoint=f"sql_readonly://localhost{db}",
        interface={"impl": "sql/slow.sql"},
        params={},
        secret=None,
        is_write=False,
        pack_root=tmp_path,
        timeout=0.05,
    )
    elapsed = time_mod.monotonic() - started
    assert rows is None and err is not None  # 长查询被中断,不是跑完亿级迭代
    assert elapsed < 5.0  # 远小于查询自然完成时长


def test_openapi_total_deadline_bounds_slow_dribble_response():
    """三轮复核 P2：慢滴漏响应（每 50ms 一字节,单次 socket op 从不超时）——总 deadline 在
    分块读之间截停,socket timeout 单独关不住的总时长由绝对 deadline 兜住。"""
    import socket
    import threading
    import time as time_mod

    def serve(sock):
        conn, _ = sock.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nContent-Type: application/json\r\n\r\n")
        try:
            for _ in range(100):
                conn.sendall(b"x")
                time_mod.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    threading.Thread(target=serve, args=(s,), daemon=True).start()
    host, port = s.getsockname()
    started = time_mod.monotonic()
    try:
        payload, err = OpenapiExecutor().execute(
            endpoint=f"openapi://{host}:{port}",
            interface={"method": "GET", "path": "/slow"},
            params={},
            secret=None,
            is_write=False,
            pack_root=Path("."),
            timeout=0.4,
        )
    finally:
        s.close()
    elapsed = time_mod.monotonic() - started
    assert payload is None and err is not None  # 总 deadline/超时截停
    # 收紧上界（四轮复核 P2）：deadline 0.4s + 小调度余量——不是「< 3s」这类宽松断言;
    # 每次 read 前 socket timeout 已压到 remaining,不存在再吊满整个旧 per-op timeout 的余地
    assert elapsed < 1.0, elapsed


def test_openapi_per_read_socket_timeout_tightened_to_remaining():
    """四轮复核 P2：deadline 前启动的一次 read 不得再吊满旧 per-op timeout——首字节 0.15s 到达
    后服务器停顿,timeout=0.25:旧实现耗时 ≈ 0.15+0.25;收紧后 ≈ deadline+小误差。"""
    import socket
    import threading
    import time as time_mod

    def serve(sock):
        conn, _ = sock.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nContent-Type: application/json\r\n\r\n")
        try:
            time_mod.sleep(0.4)
            conn.sendall(b"x")  # 首字节后停顿:后续 read 在 deadline 前启动、旧 timeout 未收紧则吊满
            time_mod.sleep(5)
        except OSError:
            pass
        finally:
            conn.close()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    threading.Thread(target=serve, args=(s,), daemon=True).start()
    host, port = s.getsockname()
    started = time_mod.monotonic()
    try:
        payload, err = OpenapiExecutor().execute(
            endpoint=f"openapi://{host}:{port}",
            interface={"method": "GET", "path": "/stall"},
            params={},
            secret=None,
            is_write=False,
            pack_root=Path("."),
            timeout=0.5,
        )
    finally:
        s.close()
    elapsed = time_mod.monotonic() - started
    assert payload is None and err is not None
    # 判别性上界（六项复核 P2）：新实现 ≈ deadline(0.5)+小余量;旧实现（read 吊满整个 per-op
    # timeout）≥ 0.4+0.5=0.9,必败于此断言——上界不再宽到新旧同过
    assert elapsed < 0.75, elapsed


def test_openapi_read_timeout_set_to_remaining_each_round(monkeypatch):
    """六项复核 P2（判别性断言）：逐轮直接断言 settimeout 收到的值 == min(per_op, remaining)——
    单调收紧,不靠时序上界间接推断。"""
    import osca_host.executor as ex_mod

    recorded: list[float] = []

    class _Sock:
        def settimeout(self, value):
            recorded.append(value)

    class _Raw:
        pass

    class _FP:
        pass

    class _Resp:
        status = 200

        def __init__(self):
            self.fp = _FP()
            self.fp.raw = _Raw()
            self.fp.raw._sock = _Sock()
            self._chunks = [b'{"a"', b": 1}", b""]

        def getheader(self, name):
            return None

        def read1(self, n):
            return self._chunks.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(ex_mod, "_OPENER", type("O", (), {"open": staticmethod(lambda req, timeout=None: _Resp())})())
    payload, err = OpenapiExecutor().execute(
        endpoint="openapi://h.internal",
        interface={"method": "GET", "path": "/x"},
        params={},
        secret=None,
        is_write=False,
        pack_root=Path("."),
        timeout=5.0,
    )
    assert err is None and payload == {"a": 1}
    assert len(recorded) == 3  # 每轮 read 前各设一次
    assert all(0 < v <= 5.0 for v in recorded)
    for earlier, later in zip(recorded, recorded[1:], strict=False):
        assert later <= earlier + 1e-6  # remaining 单调收紧


def test_openapi_fail_closed_when_socket_unavailable_under_deadline(monkeypatch):
    """六项复核 P2：拿不到底层连接（非 CPython 布局/wrapper 变化）——声明 deadline 时 fail-closed,
    不静默退回旧 per-op timeout;未声明 deadline 不受影响。"""
    import osca_host.executor as ex_mod

    class _Resp:
        status = 200
        fp = None  # 无底层 socket 可及

        def getheader(self, name):
            return None

        def read1(self, n):
            return b""

        def read(self, n=None):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(ex_mod, "_OPENER", type("O", (), {"open": staticmethod(lambda req, timeout=None: _Resp())})())
    payload, err = OpenapiExecutor().execute(
        endpoint="openapi://h.internal",
        interface={"method": "GET", "path": "/x"},
        params={},
        secret=None,
        is_write=False,
        pack_root=Path("."),
        timeout=1.0,
    )
    assert payload is None and "无法获取底层连接" in err  # fail-closed

    payload, err = OpenapiExecutor().execute(
        endpoint="openapi://h.internal",
        interface={"method": "GET", "path": "/x"},
        params={},
        secret=None,
        is_write=False,
        pack_root=Path("."),
    )
    assert err is None  # 未声明 deadline:兼容路径不要求私有布局
