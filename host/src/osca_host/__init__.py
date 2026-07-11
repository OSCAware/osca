"""OSCA 运行框架 Host 参考实现（架构 §4，七组件齐）。

控制平面确定性常驻、本体无 LLM；LLM 只活在剧集（认知平面，runner）。M2 周切片：
- W1：Loader（复用 cli 装载核心）+ 注册表 + 控制通道 + 包停
- W2：触发表（定时器/轮询器）+ 闸门
- W3：剧集装配器
- W4：Policy 拦截器 + Connector 代理
- W5：剧集执行 + 对账器 + replay（cli 侧）← 本周，M2 收官
"""

__version__ = "0.2.0"
