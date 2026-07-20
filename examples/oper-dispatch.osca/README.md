# 示例经营处置下发（oper-dispatch.osca）

一个**聚焦写路径**的最小 OSCA 样例包：读经营指标 → 确定性算出待下发处置清单 → **经审批门下发到工单下发系统**。
与只读诊断样例 [`oper-diagnosis.osca`](../oper-diagnosis.osca/) 互补——那个演示判断/蒸馏/飞轮，这个演示
**真实写连接器 + 审批门 + 挂起-等批-恢复消费**。

## 它演示什么

| 机制 | 落点 |
|---|---|
| 真实写连接器 | `connectors/CON-202-工单下发.yaml`：`kind: openapi`、`permissions.write: allowed_with_approval`、写接口 `method: POST` + `path` |
| 写审批门 | `policy.yaml` 的 `approvals`：`action` **逐字等于**写接口 ref `CON-202.下发处置工单`，绑处置审批人 + TTL |
| 只读取数双闸 | `connectors/CON-201-经营指标库.yaml`：`sql_readonly` / `write: forbidden`，固化查询 param-less |
| 凭据三不 | `bindings.example.yaml`：只放 `secret_ref` 名 + 占位 endpoint，真值部署侧注入 |
| 注入前脱敏 | `policy.yaml` 的 `data.redact`：审批卡显示被写内容前脱敏（`payload_display`），`payload_digest` 仍绑原文 |

## 立身口径（诚实标注）

- 本包是**冷骨架**：`judgments/`、`cases/` 出生即空——写样例的价值在写路径，不在判断积累。
- `bindings.example.yaml` 的 endpoint / TTL 是**占位口径**，真实值与真实人审节奏由部署侧按实况定。
- 端到端真写演练（真实 openapi 执行器打本地 fake 后端）见独立集成工程；
  **测 fake 后端 = 审批闭环 + 真实执行器机制通，非真实系统写验证**（生产真连通归部署侧）。

## 装载

```
osca lint examples/oper-dispatch.osca      # 规范与账本纪律校验
```

真实 binding 由部署环境注入（`OPS_DB` / `DISPATCH_API`），永不进包。
