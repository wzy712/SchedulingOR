# dsh-scheduling-or

把 [SchedulingOR](../README.md) 的排班内核挂载为 DSH 工具的**配置型 bundle**。

- 类型：configuration-only bundle（无 Host / Client 入口文件，无构建步骤）
- 提供：9 个 MCP 工具，模型看到的名字是 `mcp__schedulingor__*`
- 依赖环境变量 `SCHEDULINGOR_HOME`（SchedulingOR 仓库根目录）；未设置时回退到 `process.cwd()`

安装与管理见仓库根 [README](../README.md#作为-dsh-插件安装)。

## 测试

```bash
# 不消耗模型额度：按本 bundle 实际声明的配置起 MCP 并调用工具
../bin/sched-plugin-selftest

# 组合后的配置树
dsh --profile scheduling --dump-config | grep -A12 mcp-schedulingor
```

自检会验证 `!!js` 路径解析、解释器与内核存在、以及 9 个 MCP 工具的完整调用链。
