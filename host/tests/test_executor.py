"""真实执行器参考适配器（W6-3）——测 **fake 后端**（本地 sqlite 文件 / 本地 http.server）。

立身口径（诚实标注）：这些验的是「适配器契约真生效」（只读强制 / 参数化防注入 / method-params / 非 2xx /
secret 作鉴权头不外泄 / 不跟随重定向）——**非生产库/生产 API 的真系统验证**（那属部署侧适配，1.1/部署验收）。
"""

from __future__ import annotations

import http.server
import importlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from osca_host.executor import OpenapiExecutor, SqlReadonlyExecutor, _split_endpoint
from osca_host.jsongate import MAX_JSON_DEPTH  # 深度上限的真理源（与 runner 共用同一把闸）

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
        self._json(201, {"method": "POST", "path": self.path, "body": body, "auth": self.headers.get("Authorization")})


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


def _run_ep(endpoint, interface, params, *, secret=None, is_write=False):
    """带**完整 endpoint（含 path 段）** 跑执行器——测「部署侧挂载前缀 + 包内相对段」的拼接。"""
    return OpenapiExecutor().execute(
        endpoint=endpoint,
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


# ── endpoint 挂载前缀 + 包内相对 path 的拼接（造包→真写那道缝） ──────────


def test_split_endpoint_semantics_unchanged():
    """`_split_endpoint` 语义不动——`SqlReadonlyExecutor` 靠第 3 元取 sqlite 文件路径，改它会连带炸只读执行器。
    openapi 分支只是**开始用**第 3 元（原来丢弃），切法本身一字未动。"""
    assert _split_endpoint("openapi://127.0.0.1:18080") == ("openapi", "127.0.0.1:18080", "")
    assert _split_endpoint("openapi://127.0.0.1:18080/datastore/acme") == (
        "openapi",
        "127.0.0.1:18080",
        "/datastore/acme",
    )
    assert _split_endpoint("sql_readonly://localhost/tmp/fin.db") == ("sql_readonly", "localhost", "/tmp/fin.db")


def test_openapi_endpoint_without_path_url_byte_identical_regression(http_addr):
    """**向后兼容第一位**：endpoint 不带 path（今天所有既有形态——`examples/oper-dispatch.osca` 演练注入的
    `openapi://127.0.0.1:<port>` + 接口 `path: /dispatch` 就是这个形状）→ URL 的 path **逐字不变**。
    逐条钉死老行为：正常段、无 path 声明、GET query 拼接、无前导 `/` 的锚定、连写 `//` 不被全局归一。"""
    payload, err = _run_ep(f"openapi://{http_addr}", {"method": "GET", "path": "/dispatch"}, {})
    assert err is None and payload["path"] == "/dispatch"  # 无前缀、无尾斜杠、无归一副作用

    payload, err = _run_ep(f"openapi://{http_addr}", {"method": "GET"}, {})
    assert err is None and payload["path"] == "/"  # 两段皆空 → "/"（老行为）

    payload, err = _run_ep(f"openapi://{http_addr}", {"method": "GET", "path": "/dispatch"}, {"q": "x"})
    assert err is None and payload["path"] == "/dispatch?q=x"  # GET querystring 拼接未动

    payload, err = _run_ep(f"openapi://{http_addr}", {"method": "GET", "path": "data/x"}, {})
    assert err is None and payload["path"] == "/data/x"  # 无前导 / 仍被锚定成 /data/x

    payload, err = _run_ep(f"openapi://{http_addr}", {"method": "GET", "path": "/a//b"}, {})
    assert err is None and payload["path"] == "/a//b"  # 段内连写 // 逐字保留：只归一拼接缝，不做全局归一


def test_openapi_endpoint_path_prefix_joins_interface_path(http_addr):
    """造包→真写那道缝：endpoint 带**部署侧挂载前缀**（公司代号只活在这里）+ 包内**表相对段** → 真实路由。
    包与登记表都不带 host / 公司代号 / 凭据。"""
    payload, err = _run_ep(f"openapi://{http_addr}/datastore/acme", {"method": "GET", "path": "/booking"}, {})
    assert err is None and payload["path"] == "/datastore/acme/booking"

    payload, err = _run_ep(f"openapi://{http_addr}/datastore/acme", {"method": "GET", "path": "/booking"}, {"q": "x"})
    assert err is None and payload["path"] == "/datastore/acme/booking?q=x"  # query 仍拼在拼完的 path 之后


def test_openapi_write_path_joins_prefix(http_addr):
    """写路径同样拼前缀（M8 数据台形状：`openapi://<主机>/datastore/<公司>` + 包内 `/booking`）。"""
    payload, err = _run_ep(
        f"openapi://{http_addr}/datastore/acme",
        {"method": "POST", "path": "/booking"},
        {"x": 1},
        is_write=True,
    )
    assert err is None and payload["method"] == "POST" and payload["path"] == "/datastore/acme/booking"


@pytest.mark.parametrize(
    ("ep_path", "itf_path", "want"),
    [
        ("/datastore/acme", "/booking", "/datastore/acme/booking"),  # 两段都规整
        ("/datastore/acme/", "/booking", "/datastore/acme/booking"),  # 左段尾斜杠 → 缝上不出 //
        ("/datastore/acme", "booking", "/datastore/acme/booking"),  # 右段无前导斜杠 → 锚定补上
        ("/datastore/acme/", "booking", "/datastore/acme/booking"),  # 一有一无，仍恰好一个 /
        ("/datastore/acme/", "///booking", "/datastore/acme/booking"),  # 右段多余前导斜杠塌缩
        ("/", "/booking", "/booking"),  # 前缀只有根 → 不出 //booking
        ("/", "", "/"),  # 两段都退化成根
        ("/datastore/acme", "", "/datastore/acme"),  # 接口无 path → 打挂载点本身
        ("/datastore/acme/", "/", "/datastore/acme/"),  # 尾斜杠语义保留（但不成 //）
    ],
)
def test_openapi_path_join_collapses_double_slash(http_addr, ep_path, itf_path, want):
    """斜杠归一：拼完不许出现 `//`——`//x` 在某些服务器上是**另一个资源**（前缀路由/鉴权中间件按段匹配时
    尤其危险），路径开头的 `//` 更会被读成 protocol-relative authority。空段要能正确塌缩。"""
    payload, err = _run_ep(f"openapi://{http_addr}{ep_path}", {"method": "GET", "path": itf_path}, {})
    assert err is None and payload["path"] == want
    assert "//" not in payload["path"]


@pytest.mark.parametrize(
    "itf_path",
    [
        "/../admin",  # 裸上跳
        "/a/../../admin",  # 中段上跳
        "..",  # 整段就是上跳
        "/%2e%2e/admin",  # 百分号编码（小写）
        "/%2E%2E/admin",  # 百分号编码（大写）
        "/.%2e/admin",  # 混合编码
        "/%252e%252e/admin",  # 双重编码（带 decode 的代理会还原成 ..）
        "/..%2fadmin",  # 编码斜杠 + 上跳
        "..\\admin",  # 反斜杠分隔（部分服务器按 Windows 语义等同 /）
        "/a/..%5cadmin",  # 编码反斜杠
        # ── path-parameter（RFC 3986 `;`）：Tomcat/Jetty/Spring 一类**在归一化之前**剥掉 `;param`，剥完变回 `..`
        "/..;/admin",
        "/..;jsessionid=x/admin",  # 带值的 path-parameter（经典 Tomcat 形状）
        "/..%3b/admin",  # 编码分号（小写）
        "/..%3B/admin",  # 编码分号（大写）
        "/%2e%2e;/admin",  # 编码点 + 分号
        "/.%2e;a=b/admin",  # 混合编码 + 带值 path-parameter
        "/%252e%252e;/admin",  # 双重编码 + 分号
        "/a/..;\\admin",  # 分号 + 反斜杠分隔
        # ── NUL 截断/抹除：按 C 字符串语义截断的后端把 `..%00x` 读成 `..`
        "/..%00/admin",
        "/..%00x/admin",  # NUL 后还有内容（截断语义）
        "/.%00./admin",  # NUL 夹在两点之间（抹除语义）
        "/..%2500/admin",  # 双重编码的 NUL
        "/..%00;/admin",  # NUL + path-parameter 叠用
        # ── 首尾空白：Windows 文件语义会吃掉尾随空格
        "/..%20/admin",
        "/%09../admin",  # 前导 tab
    ],
)
def test_openapi_path_traversal_fails_closed(http_addr, itf_path):
    """路径穿越 fail-closed：`interface.path` 来自**包**（不可信输入），拼接一旦允许上跳，包就能从自己的
    挂载前缀跳到同一台服务上别的 API（越权、串到别家公司的表），而 egress 白名单只看主机、拦不住。
    **拒在外呼之前**——服务器本会回 200 并回显 path，这里拿到的是错误而非 payload，即证明请求没发出去。
    也**不做归一化后放行**（归一后放行 = 替不可信输入把上跳兑现掉）。"""
    payload, err = _run_ep(f"openapi://{http_addr}/datastore/acme", {"method": "GET", "path": itf_path}, {})
    assert payload is None and "上跳" in err


@pytest.mark.parametrize(
    ("itf_path", "want"),
    [
        ("/booking;v=2", "/datastore/acme/booking;v=2"),  # 正常 matrix 参数：剥掉 `;v=2` 也不是 `..`
        ("/..booking/x", "/datastore/acme/..booking/x"),  # `..` 只是段的前缀，整段不等于 `..`
        ("/a..b", "/datastore/acme/a..b"),  # 段中间的两点
        ("/.../x", "/datastore/acme/.../x"),  # 三点段：任何标准归一都不等于 `..`
        ("/./x", "/datastore/acme/./x"),  # 单点段（当前段，不上跳）
    ],
)
def test_openapi_traversal_guard_does_not_over_reject(http_addr, itf_path, want):
    """判据收紧后**不许误伤**：只有「段规范化后等于 `..`」才拒——含 `..` 字样的正常段、matrix 参数段、
    单点/三点段照发（服务器答了并回显 path 即证明请求真发出去了）。"""
    payload, err = _run_ep(f"openapi://{http_addr}/datastore/acme", {"method": "GET", "path": itf_path}, {})
    assert err is None and payload["path"] == want


def test_openapi_traversal_from_endpoint_segment_also_rejected(http_addr):
    """上跳由**部署侧 endpoint 前缀**贡献时同样拒——闸判**拼完的 path**，不认是哪一段带进来的。"""
    payload, err = _run_ep(f"openapi://{http_addr}/datastore/acme/..", {"method": "GET", "path": "/booking"}, {})
    assert payload is None and "上跳" in err


def test_openapi_traversal_error_does_not_echo_deployment_prefix(http_addr):
    """报错只回显**包内**声明的接口 path（包已在手），不回显部署侧挂载前缀（挂载点/公司代号属部署侧真值，
    不进剧集台账）；且不带异常内文、不带 secret。"""
    payload, err = _run_ep(
        f"openapi://{http_addr}/datastore/acme",
        {"method": "GET", "path": "/../admin"},
        {},
        secret="TKN-abc",
    )
    assert payload is None
    assert "/../admin" in err and "acme" not in err and "TKN-abc" not in err


def test_openapi_both_path_segments_pass_authority_gate(http_addr):
    """authority 注入闸不因为多了一段而弱化——**两段都过**锚定：
    ① interface 段形如 `.evil.com/x`（无前导 /）→ 被锚定到前缀之下，连接 host 不变（服务器答了即证明）；
    ② endpoint 段前导 `//` → 塌成单斜杠，不留 protocol-relative authority 的形。"""
    payload, err = _run_ep(f"openapi://{http_addr}/datastore/acme", {"method": "GET", "path": ".evil.com/exfil"}, {})
    assert err is None  # 打在 http_addr 上，netloc 未被向右延展
    assert payload["path"] == "/datastore/acme/.evil.com/exfil"

    payload, err = _run_ep(f"openapi://{http_addr}//evil.com", {"method": "GET", "path": "/booking"}, {})
    assert err is None and payload["path"] == "/evil.com/booking"  # 未留成 "//evil.com/booking"


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


def test_openapi_executor_ignores_environment_proxy_and_keeps_bearer_off_proxy(monkeypatch):
    """环境代理不是部署登记表的一部分：即使进程环境带代理，连接器也必须直连目标。

    这条用真 HTTP 假代理接 wire，不靠检查 opener 私有结构。目标端口刻意不可达：旧实现会把
    完整 URL 与 Bearer 交给假代理并拿到伪造成功；正确实现应直连失败，假代理一个请求都看不到。
    """
    from osca_host import executor as ex_mod

    seen: list[tuple[str, str | None]] = []

    class _Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 —— BaseHTTPRequestHandler 命名约定
            seen.append((self.path, self.headers.get("Authorization")))
            body = b'{"via":"ambient-proxy"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    try:
        with monkeypatch.context() as env:
            for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
                env.setenv(name, proxy_url)
            for name in ("NO_PROXY", "no_proxy"):
                env.delenv(name, raising=False)
            # ProxyHandler 在 opener 构造时读取环境；重载确保测试的是带代理环境下的生产默认值。
            importlib.reload(ex_mod)
            payload, error = ex_mod.OpenapiExecutor().execute(
                endpoint="openapi://127.0.0.1:9",
                interface={"method": "GET", "path": "/private"},
                params={},
                secret="fake-proxy-regression-secret",
                is_write=False,
                pack_root=Path("."),
                timeout=0.2,
            )
            assert payload is None and error == "openapi GET 调用失败（连接层错误）"
            assert seen == [], f"环境代理截获了内部请求或 Bearer：{seen!r}"
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)
        # 恢复无测试环境注入的生产模块状态，避免影响本文件后续用例。
        importlib.reload(ex_mod)


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


# ── 后端响应体过 JSON 闸（M8-T4 ⑤）─────────────────────────────
# 响应体是**第二个进口**：模型产出那个进口已在 M8-T3 堵上，而**读回执是下游写 body 的原料**，
# 同一批漏网口（NaN/±inf/重复键/深嵌套）从这个进口进来，落点一字不差（进台账、上审批卡、进 L2
# 挂起快照、原样上 wire）。这些形状 `json.dumps` 造不出来，故 fake 后端要能**逐字**回一份响应体；
# 且一律走**真 HTTP**（真实 _OPENER.open 打真 socket），不是把 json.loads 抽出来单测——
# 同构纪律：假替身的调用路径必须与真组件同构。


@pytest.fixture
def raw_addr():
    """fake 后端：原样回一份指定的响应体（Content-Length 如实声明，避开截断闸）。回 (addr, 设置器)。"""

    class _RawHandler(http.server.BaseHTTPRequestHandler):
        body = b"{}"

        def _reply(self):
            payload = type(self).body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 —— BaseHTTPRequestHandler 命名约定
            self._reply()

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._reply()

        def log_message(self, *args):  # 静音测试输出
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RawHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"127.0.0.1:{srv.server_address[1]}", _RawHandler
    finally:
        srv.shutdown()
        srv.server_close()


def _read_body(raw_addr, body: bytes, *, is_write=False):
    """让 fake 后端回 body，跑真实执行器取回来（读走 GET，写走 POST）。"""
    addr, handler = raw_addr
    handler.body = body
    itf = {"method": "POST" if is_write else "GET", "path": "/x"}
    return _run_http(addr, itf, {"n": 1} if is_write else {}, is_write=is_write)


def _nested_body(depth: int) -> bytes:
    return ("[" * depth + "]" * depth).encode("utf-8")


ILLEGAL_BODIES = {
    # ── 漏网口 ①：JSON 规范外的字面量（RFC 8259 无 NaN/Infinity，Python json 默认收） ──
    "NaN 字面量": (b'{"rows": [{"\xe9\x87\x91\xe9\xa2\x9d": NaN}]}', "NaN"),
    "Infinity 字面量": (b'{"\xe4\xb8\x8a\xe9\x99\x90": Infinity}', "Infinity"),
    "负 Infinity 字面量": (b'{"\xe4\xb8\x8b\xe9\x99\x90": -Infinity}', "-Infinity"),
    # 同一条纪律的另一条进口：合法 JSON 数字**溢出**成 ±inf（parse_constant 拦不住）
    "浮点溢出成 inf": (b'{"rows": [{"\xe9\x87\x91\xe9\xa2\x9d": 1e999}]}', "1e999"),
    "浮点溢出成 -inf": (b'{"rows": [{"\xe9\x87\x91\xe9\xa2\x9d": -1e999}]}', "-1e999"),
    # ── 漏网口 ②：重复键（json.loads 默认静默取最后一个——「取哪一份」是猜，猜错即写错内容） ──
    "顶层重复键": (b'{"rows": [1], "rows": [2]}', "rows"),
    "行内重复键": (b'{"rows": [{"amount": 100, "amount": 999999}]}', "amount"),
    # ── 漏网口 ③：深嵌套（解析放行近万层，下游最浅 330 层即炸栈） ──
    "深嵌套-越上限一层": (_nested_body(33), "嵌套"),
    "深嵌套-下游炸栈档": (_nested_body(331), "嵌套"),
}


@pytest.mark.parametrize("name", sorted(ILLEGAL_BODIES))
def test_openapi_response_body_fails_closed_at_json_gate(name, raw_addr):
    """后端响应体的三类漏网口一律 fail-closed，且报错**指名道姓**（人看得懂才查得动后端）。

    失败语义照既有分支走：`(None, error)` → connector 转 `Receipt(ok=False)` → runner 收
    「取数失败 → 剧集 failed」。**没有半解析、没有退回文本、没有「拿 None 当空回执」**——
    退回文本/空回执等于把「写步 body ＝ 一行真正要写的数据」悄悄退化回一坨来路不明的东西。"""
    body, keyword = ILLEGAL_BODIES[name]
    payload, err = _read_body(raw_addr, body)
    assert payload is None, name
    assert "JSON 闸" in err and keyword in err, (name, err)
    assert "HTTP 200" in err  # 后端答的是 200：拒的是**内容**，不是状态码


def test_openapi_response_body_depth_boundary(raw_addr):
    """深度上限与模型产出**同一个数**（jsongate.MAX_JSON_DEPTH）——因为下游是同一批消费者：
    脱敏、入台账、渲染给下游步骤、落 L2 快照、上 wire。上限层放行、上限 + 1 层拒绝。"""
    payload, err = _read_body(raw_addr, _nested_body(MAX_JSON_DEPTH))
    assert err is None and payload == json.loads(_nested_body(MAX_JSON_DEPTH))  # 恰好上限：放行

    payload, err = _read_body(raw_addr, _nested_body(MAX_JSON_DEPTH + 1))
    assert payload is None and "嵌套" in err and str(MAX_JSON_DEPTH) in err


def test_openapi_response_body_deep_enough_to_blow_parser_is_caught(raw_addr):
    """深到把**解析器自己**炸栈的那一档：RecursionError 不是 ValueError——漏捕即炸穿 execute。
    这里要的是「恒回 (None, error)、绝不抛」，报错退成笼统版可以接受（那一档已无从定点）。"""
    payload, err = _read_body(raw_addr, _nested_body(20_000))
    assert payload is None and err  # 不抛、不炸穿；fail-closed


def test_openapi_legal_response_bodies_still_pass(raw_addr):
    """闸只咬非法形状：正常行数组、浮点/整数/科学计数、深度自然的嵌套照旧放行（别把闸修成误伤真回执）。"""
    body = '{"rows": [{"单位": "甲厂", "金额": 45, "涨幅": 0.3, "占比": 1e30, "备注": null}], "total": 1}'
    payload, err = _read_body(raw_addr, body.encode("utf-8"))
    assert err is None and payload == json.loads(body)


def test_openapi_accepted_response_body_is_always_legal_json(raw_addr):
    """放行的回执**必是合法 JSON**——它是下游写 body 的原料，还要落 L2 快照、上审批卡。
    `allow_nan=False` 就是 RFC 8259 的口径：这条断言是三处下游的共同前置（闸买到的正是它）。"""
    payload, err = _read_body(raw_addr, b'{"rows": [{"amount": 1.5, "ratio": 1e30}]}')
    assert err is None
    json.dumps(payload, allow_nan=False)  # 不抛即合法


def test_openapi_write_response_body_also_gated(raw_addr):
    """写回执同一把闸：写已经落地了，拒的是「把这份回执当结果用」——宁可剧集 failed 上报由人接手，
    也不把解不清的回执喂给下游/台账（回执本身也要进台账给人看）。"""
    payload, err = _read_body(raw_addr, b'{"ticket": "WO-1", "ticket": "WO-2"}', is_write=True)
    assert payload is None and "JSON 闸" in err and "ticket" in err


def test_openapi_json_gate_generic_branch_carries_no_body_content(http_addr):
    """语法错（非本闸的定点拒绝）走原分支、报错**原文不变**，且不带响应体内文/异常内文。"""
    payload, err = _run_http(http_addr, {"method": "GET", "path": "/notjson"}, {})
    assert payload is None and err.endswith("响应非 JSON（HTTP 200）")
    assert "not json at all" not in err


def test_openapi_json_gate_reason_is_scrubbed_of_secret_at_connector(raw_addr):
    """定点报错的理由取自**响应体内容**（重复的那个键名），故反射型 API 回显 token 时理由里可能带 secret。
    这条纪律的兜底在 connector 层（回执与 error 同抹 `_scrub_secret`）——此处验组合成立：
    抹后 secret 不见了、键名这条诊断线索还在（不是把整条理由砍掉）。"""
    from osca_host.connector import _scrub_secret

    addr, handler = raw_addr
    handler.body = b'{"TKN-abc": 1, "TKN-abc": 2}'  # 反射型后端把 Bearer 值回显成键、还重复了
    payload, err = _run_http(addr, {"method": "GET", "path": "/x"}, {}, secret="TKN-abc")
    assert payload is None and "TKN-abc" in err  # 执行器这层确实带出来了

    scrubbed = _scrub_secret(err, "TKN-abc")
    assert "TKN-abc" not in scrubbed and "重复键" in scrubbed


def test_json_gate_is_one_implementation_not_two():
    """**闸只有一份实现**——两份迟早漂移，而漂移那天松的那个就是没堵的洞。
    这条测试是那句话的机器判据：两个进口引用的必须是**同一个函数对象**，且执行器里不许再有第二处
    `json.loads(` 调用（复制一份的那天，这条先红）。"""
    from osca_host import executor as ex_mod
    from osca_host import jsongate
    from osca_host import runner as runner_mod

    assert ex_mod.loads_guarded is jsongate.loads_guarded is runner_mod.loads_guarded
    assert runner_mod.exceeds_depth is jsongate.exceeds_depth
    assert runner_mod.MAX_STRUCTURED_DEPTH == jsongate.MAX_JSON_DEPTH  # 深度上限单一真理源，不是两个旋钮

    src = Path(ex_mod.__file__).read_text(encoding="utf-8")
    assert "json.loads(" not in src  # 解析后端响应体只经共用闸；json 在本模块只剩 dumps（拼写 body）
