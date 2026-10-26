# NDC Ops Copilot

面向航空NDC对接、诊断与售后服务场景的智能助手。当前版本聚焦安全、可验证的交易编排基础能力；默认使用Mock供应商且不调用外部LLM，因此不需要真实航司账号，也不产生LLM API费用。

当前已实现以下关键能力：

- 类型化的航班搜索、核价、订单创建和订单查询
- 可替换的供应商适配层 `SupplierAdapter`
- 多供应商并行查询、价格聚合和优先级路由
- 按航线启用供应商，并在失败后自动隔离异常供应商
- Offer、核价和订单始终绑定原始供应商
- 将Agent能力导出为LLM函数工具Schema
- 带最大步数限制的模型/工具调用循环
- 创建订单工具必须经过宿主程序批准，LLM不能自我确认
- 标准MCP服务端，可供Codex、Claude Desktop等MCP客户端调用
- 会话级 Offer 白名单，禁止操作模型虚构的 OfferID
- 行程意图锁，搜索条件变化后自动清理旧 Offer
- Offer 和核价结果有效期检查
- 创建订单前的二次确认令牌
- 单次使用的确认令牌和操作事件日志
- 中文及英文命令行指令

## 架构

```text
用户命令
   |
   v
Command Parser
   |
   v
LLM Client -> ToolLoop -> AgentToolBridge
                         |
MCP Client -> MCP Server-+
                         |
                         v
NdcAgent / Session State
   |-- Offer validation
   |-- Intent lock
   |-- Confirmation gate
   |
   v
SupplierAdapter
   |
   v
MultiSupplierRouter
   |-- parallel search
   |-- route rules / priority
   |-- health / circuit breaker
   |-- offer and order affinity
   |
   +-- Travelport Mock（当前）
   +-- Airline NDC Mock（当前）
   +-- 真实供应商适配器（可扩展）
```

LLM在真实系统中应该放在命令解析和工具选择层。Offer校验、金额、订单状态以及交易确认继续由确定性代码处理。

## 快速运行

项目不需要安装第三方依赖：

```bash
cd NdcOpsCopilot
PYTHONPATH=src python3 -m ndc_ops_copilot
```

也可以安装为本地命令：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
ndc-copilot
```

## 操作示例

先搜索：

```text
ndc> 查询 SHA 到 FRA 2027-05-01 1人
```

从结果中选择一个返回的 `offer_id`，然后核价：

```text
ndc> price AIRLINE_NDC::OFF-XXXXXXXXXX
```

发起预订，Agent会返回一个五分钟有效的确认令牌：

```text
ndc> book AIRLINE_NDC::OFF-XXXXXXXXXX
```

确认并创建订单：

```text
ndc> confirm <确认令牌> ZHANG/SAN
```

查询订单：

```text
ndc> order AIRLINE_NDC::ORD-XXXXXXXXXXXX
```

Mock创建的订单状态为 `confirmed`，票证状态为 `not_ticketed`，表示已经创建订单但尚未真实出票。

查看供应商优先级和健康状态：

```text
ndc> suppliers
```

查看提供给LLM的函数工具定义：

```text
ndc> tools
```

## 多供应商路由

CLI默认配置两个Mock供应商。搜索会并行执行并按照价格排序；后续核价、预订和订单查询会回到产生该Offer的同一个供应商。

```python
from ndc_ops_copilot import MultiSupplierRouter, SupplierRegistration
from ndc_ops_copilot.providers.mock import MockSupplierAdapter


router = MultiSupplierRouter(
    [
        SupplierRegistration(
            MockSupplierAdapter("TRAVELPORT", fare_factor="1.00"),
            priority=10,
        ),
        SupplierRegistration(
            MockSupplierAdapter("AIRLINE_NDC", fare_factor="0.96"),
            priority=20,
            routes=frozenset({("SHA", "FRA"), ("SHA", "LHR")}),
        ),
    ],
    failure_threshold=3,
    circuit_break_seconds=30,
)
```

`priority`数字越小优先级越高。当`routes`为空时，供应商支持所有航线；配置后只会参与指定航线。连续失败达到阈值后，供应商会暂时熔断，其他可用供应商仍可正常返回结果。

路由器会将公开ID转换为以下格式，防止不同供应商产生相同ID：

```text
TRAVELPORT::OFF-XXXXXXXXXX
AIRLINE_NDC::OFF-XXXXXXXXXX
AIRLINE_NDC::ORD-XXXXXXXXXXXX
```

## LLM工具桥接

`AgentToolBridge`将Agent能力导出为常见的函数工具格式，目前包含：

```text
search_flights
price_offer
prepare_order
confirm_order
retrieve_order
list_suppliers
```

获取可传给模型API的工具定义：

```python
from ndc_ops_copilot import AgentToolBridge


bridge = AgentToolBridge(agent)
tools = bridge.tool_definitions()
```

执行模型返回的工具调用：

```python
from ndc_ops_copilot import ToolCall


result = bridge.execute(
    ToolCall(
        call_id="call-1",
        name="search_flights",
        arguments={
            "origin": "SHA",
            "destination": "FRA",
            "departure_date": "2027-05-01",
            "adults": 1,
        },
    )
)
```

`confirm_order`属于`commit`操作。模型即使获得确认令牌，也不能直接创建订单。用户在界面确认后，可信宿主程序需要显式批准：

```python
bridge.approve(confirm_order_call)
result = bridge.execute(confirm_order_call)
```

`approve`没有暴露为LLM工具，因此模型无法自行调用。批准记录会绑定完整工具参数，包括确认令牌和乘机人；参数被修改后必须重新让用户审核。批准使用一次后立即失效。

项目还提供`ToolLoop`，负责循环调用模型和工具，并通过`max_steps`限制失控循环。具体模型客户端只需要实现下面的接口：

```python
class MyLlmClient:
    def complete(self, messages, tools):
        # 调用OpenAI、Anthropic、Google或本地模型
        # 将响应转换成AssistantTurn
        ...
```

运行不需要真实LLM的桥接演示：

```bash
PYTHONPATH=src python3 examples/tool_bridge_demo.py
```

桥接层不会自动调用任何外部模型。只有配置具体的`LlmClient`后才会产生模型API费用。

## MCP服务端

项目提供基于标准输入/输出的MCP服务端，支持`initialize`、`ping`、`tools/list`和`tools/call`。它复用`AgentToolBridge`，因此MCP与直接LLM工具调用具有相同的参数校验、供应商绑定和会话状态。

安装项目后，可使用以下命令启动：

```bash
ndc-copilot-mcp
```

也可以不安装直接启动：

```bash
cd NdcOpsCopilot
PYTHONPATH=src python3 -m ndc_ops_copilot.mcp_server
```

MCP客户端配置示例（建议使用虚拟环境中的绝对路径）：

```json
{
  "mcpServers": {
    "ndc-ops-copilot": {
      "command": "/absolute/path/to/NdcOpsCopilot/.venv/bin/ndc-copilot-mcp",
      "args": []
    }
  }
}
```

服务端默认拒绝`confirm_order`，其他工具可以正常调用。如果MCP客户端会在调用提交类工具前向用户展示完整参数并请求人工确认，可显式启用：

```json
{
  "mcpServers": {
    "ndc-ops-copilot": {
      "command": "/absolute/path/to/NdcOpsCopilot/.venv/bin/ndc-copilot-mcp",
      "args": ["--allow-commit-tools"]
    }
  }
}
```

不要在自动批准所有工具调用的客户端中启用该参数。MCP服务使用进程内会话，搜索得到的Offer、确认令牌和订单需要在同一个服务进程中继续操作。

## 运行测试

```bash
cd NdcOpsCopilot
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 接入真实NDC接口

新增一个类并实现 `SupplierAdapter`：

```python
from ndc_ops_copilot.providers.base import SupplierAdapter


class TravelportAdapter(SupplierAdapter):
    @property
    def name(self):
        return "TRAVELPORT"

    def search(self, request):
        # 调用 AirShopping，并映射为 Offer
        ...

    def price(self, offer):
        # 调用 OfferPrice，并映射为 PricedOffer
        ...

    def create_order(self, priced_offer, passengers):
        # 调用 OrderCreate，并映射为 Order
        ...

    def retrieve_order(self, order_id):
        # 调用 OrderRetrieve，并映射为 Order
        ...
```

生产接入还应补充：认证、超时、重试、幂等键、XML Schema校验、敏感日志脱敏、支付、出票、退款及持久化会话存储。

## 当前范围与演进方向

当前版本已经完成航班搜索、核价、订单创建与查询、多供应商路由、LLM工具桥接和MCP服务端。默认供应商仍为Mock实现，因此当前结果不能作为真实库存、价格或订单状态使用。

后续将围绕NDC对接、诊断与售后服务逐步增加：

- Request、Response和Header日志解析及脱敏
- NDC Schema、版本和业务流程校验
- 航司、PCC及供应商能力注册
- 真实NDC或聚合商供应商适配器
- SeatMap、ServiceList和行李服务
- OrderReshop、改签与退款试算
- 航变通知接收和处置建议
- 持久化会话、审计、指标和生产级监控

出票、改签、退款等资金或订单状态变更操作必须继续经过确定性校验、幂等控制和人工批准，不能由LLM直接执行。
