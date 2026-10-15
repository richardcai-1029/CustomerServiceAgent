# 智能客服 Agent 设计说明

本文档记录项目启动前与需求方逐项确认的设计决策。代码、注释、知识库内容、UI 与 Agent 回复均为英文；文档为中文。

## 1. 定位

- 学习 / 作品集项目：架构清晰、可演示，业务后端通过服务层隔离，便于日后替换。
- 业务场景：虚构数码电商 **NovaTech Store**（耳机、智能手表、笔记本、平板、配件）。
- 语言：全部英文。用户即使用中文提问，Agent 也用英文回复。

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 运行时 | Python 3.12，`uv`，`src/cs_agent/` 布局 |
| 编排 | LangChain v1 + LangGraph v1（自建 StateGraph） |
| 主 LLM | 默认 `qwen-plus`（DashScope OpenAI 兼容接口），环境变量可切换 DeepSeek |
| 情绪分类 | `qwen-turbo`（结构化输出） |
| Embedding / Rerank | `text-embedding-v4` / `gte-rerank-v2`（可开关） |
| 业务库 | MySQL（Homebrew），SQLAlchemy 2.x + PyMySQL；单测用 SQLite 内存库 |
| 向量库 | Chroma（本地持久化，cosine） |
| 会话记忆 | LangGraph SQLite checkpointer |
| 接口 | FastAPI + SSE + 原生 HTML/JS 单页；另有 CLI |
| 可观测 | LangSmith，仅环境变量开关 |

## 3. 对话主图

```
START ─┬─(等待验证码 且 输入为 6 位数字)──▶ otp_gate ──▶ agent / END
       ├─(退货流程进行中)────────────────▶ returns_flow
       └─(其他)──▶ [faq_match ∥ emotion] ──▶ decide
                                              ├─ handoff      （要求人工 / 连续愤怒 / 多次答不上）
                                              ├─ faq_answer   （FAQ 命中且用户不愤怒）
                                              └─ agent ◀──▶ tools ──▶ returns_flow（start_return_request 触发）
returns_flow ──(槽位齐全)──▶ returns_confirm（interrupt 确认）──▶ END
```

- **otp_gate**：确定性校验验证码，不经过 LLM；验证码在历史消息中被替换为 `[verification code redacted]`。
- **FAQ 分层匹配**：归一化精确匹配 → BM25 召回 + 词覆盖率校验 → Embedding 相似度兜底，每层有阈值；消息含订单号时跳过 FAQ，避免拦截业务办理请求。
- **情绪**：每轮并行调用小模型输出 `calm | frustrated | angry` 与 `wants_human`，用于调整语气和触发转人工。
- **agent**：工具调用 Agent，负责 RAG、业务查询 / 办理、闲聊安抚；缺参数时依靠工具 schema + System Prompt 追问。`parallel_tool_calls=False`，保证一次最多一个待确认操作。
- **退货退款子图**：显式槽位状态机：订单 → 商品 → 原因 → 退款方式 → `interrupt` 确认 → 生成退货单。用户离题或放弃时退出流程。

## 4. 认证

- 网页登录面板（手机号 + 模拟短信验证码，dev 固定码 `123456`）→ JWT（HS256，24h）。
- 对话内：`send_verification_code` 工具发码 → `otp_gate` 校验 → 通过 SSE `auth` 事件下发同样的 JWT。
- 验证码 5 分钟有效、单码最多 5 次尝试、同号 60 秒限发。
- FAQ / RAG / 闲聊免认证；所有个人数据操作需认证。工具只从会话状态读取 `user_id`，不接受 LLM 传入的用户标识。
- 线程认证后绑定该用户；携带其他用户 Token 访问时拒绝；登出即开新线程。
- 不做“绑定账号”。

## 5. 业务规则（Mock，存 MySQL）

| 能力 | 规则 | 确认 |
|---|---|---|
| 查询订单 / 物流 / 余额 / 退款进度 / 预约 | 只读 | 否 |
| 取消订单 | 仅 `pending_payment` / `paid`；已付款自动原路退款（余额支付即时到账） | 是 |
| 修改收货地址 | 仅发货前 | 是 |
| 退货退款 | 签收后 7 天内；子图办理；退款方式：原路 / 账户余额 | 是 |
| 预约 / 取消服务 | 维修、上门安装；模拟时段 | 是 |
| 重置密码 | 需认证；向绑定手机发送重置链接（模拟），Agent 不接触密码 | 否 |
| 转人工 | 生成模拟工单号 | 否 |

订单状态：`pending_payment → paid → shipped → delivered`，另有 `cancelled`、`return_requested`、`refunded`。种子数据：3 个客户，每人 4–6 个订单，覆盖全部状态；日期相对初始化时刻生成，`cs-agent reset-db` 可重置。

## 6. RAG

- 文档目录 `data/docs/`，支持 PDF / Markdown / TXT。
- `cs-agent ingest` 或 `POST /api/admin/reindex`：按文件 SHA-256 增量入库，删除文件同步删除向量。
- 检索：向量召回 top-k（相关度阈值）→ 可选 DashScope rerank → top-n 片段。
- 回答必须附引用（文件名 + 页码 / 小节），前端展示来源；检索不到时明确说明并建议转人工。

## 7. 测试

- pytest 单元测试：服务层规则、认证、FAQ、入库、对话图（脚本化假模型，覆盖 interrupt / 退货子图 / 转人工），不调用真实 LLM。
- `scripts/e2e_conversations.py`：调用真实 LLM 的脚本化对话，手动运行。
