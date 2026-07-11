# osca CLI（开发中）

三个命令，全部可用：

- `osca lint`——账本纪律的机器化：22 条规则，覆盖包结构、ID 与引用、账本纪律（出生证据 / supersedes 链 / trust 计数 / 回放断言）、零密钥铁律。规则清单见 [docs/OSCA-LINT-RULES.md](../docs/OSCA-LINT-RULES.md)
- `osca pack`——开发态（git 仓库）→ 交付态（zip）：lint 不过不打包；真实 bindings 拦截；生成 `indexes/checksums.txt` 完整性清单；**可复现打包**（同内容同哈希，交付件可签名）
- `osca load`——装载校验四步：完整性校验（防篡改）→ lint → binding 与部署环境比对（缺失即报错）→ 重建 `indexes/judgments.index.yaml` 签名表（索引是缓存，坏了随时重建）

## 用法

```bash
cd cli && uv sync

uv run osca lint ../examples/oper-diagnosis.osca
# ✓ 通过 · 0 错误, 0 警告 · 检查 YAML 16 个 · 规则 22 条

uv run osca pack ../examples/oper-diagnosis.osca
# 产出 demo-group-oper-diagnosis.osca.zip + 交付件 sha256

uv run osca load demo-group-oper-diagnosis.osca.zip --dest ./deploy --bindings /etc/osca/bindings.yaml
# 解压 → 完整性 → lint → binding 比对 → 重建签名表
```

退出码约定：0 通过；1 校验失败（lint 错误 / 篡改 / binding 缺失）。警告不挡通过。

## 开发

```bash
cd cli
uv sync              # 安装依赖（含 dev）
uv run osca --version
uv run pytest        # 测试
uv run ruff check .  # 代码检查
```
