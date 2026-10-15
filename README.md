# NovaTech 智能客服 Agent

基于 **LangChain v1 + LangGraph** 的智能客服 Agent，服务于虚构数码电商 NovaTech Store。对话、知识库与代码注释均为英文。设计决策见 [DESIGN.md](DESIGN.md)。

## 能力一览

| 需求 | 实现 |
|---|---|
| FAQ 秒级响应 | `knowledge/faq.py`：精确 → BM25+词覆盖 → Embedding 三层匹配，命中不调用 LLM |
| 多轮意图澄清 | 工具 schema + System Prompt 追问；退货走显式槽位状态机 `agent/returns_flow.py` |
| 文档问答 (RAG) | `knowledge/ingest.py` 增量入库 PDF/MD/TXT → Chroma；`retriever.py` 召回 + DashScope rerank；回答带引用 |
| 闲聊与情绪安抚 | 每轮并行情绪分类（`qwen-turbo`），调整语气；连续愤怒 / 明确要求 / 多次答不上 → 转人工工单 |
| 身份认证 | 短信验证码（模拟）+ JWT；对话内验证由确定性节点校验，验证码不进入 LLM 上下文 |
| 业务查询 | 订单、物流、余额、退款进度、预约（MySQL） |
| 业务代办 | 取消订单、改地址、退货退款、预约/取消服务、重置密码；写操作经 `interrupt()` 用户确认 |

## 快速开始

前置：Python 3.12、[uv](https://docs.astral.sh/uv/)、MySQL（`brew install mysql && brew services start mysql`）。

```bash
cp .env.example .env
```

在 `.env` 中填入 `DASHSCOPE_API_KEY`（DashScope 控制台获取）。然后：

```bash
uv sync
```

```bash
mysql -uroot -e "CREATE DATABASE IF NOT EXISTS cs_agent CHARACTER SET utf8mb4"
```

```bash
uv run cs-agent init-db
```

```bash
uv run cs-agent serve
```

打开 http://127.0.0.1:8000 。首次启动会自动把 `data/docs/` 入库（之后只处理新增或修改的文件）。

其他命令：

| 命令 | 作用 |
|---|---|
| `uv run cs-agent chat` | 终端对话（确认操作时输入 y/N） |
| `uv run cs-agent ingest` | 手动增量同步知识库（也可 `POST /api/admin/reindex`） |
| `uv run cs-agent reset-db` | 清空并重建演示数据（日期相对当前时间生成） |
| `uv run pytest` | 单元测试（不调用 LLM，默认 SQLite） |
| `TEST_DATABASE_URL=mysql+pymysql://root@127.0.0.1:3306/cs_agent_test uv run pytest` | 在 MySQL 上跑测试 |
| `uv run python scripts/e2e_conversations.py [场景名]` | 调用真实 LLM 的脚本化对话（会重置演示库） |

切换到 DeepSeek：`.env` 中设置 `LLM_PROVIDER=deepseek` 和 `DEEPSEEK_API_KEY`（Embedding/Rerank 仍使用 DashScope）。

## 演示账号（dev 模式验证码固定为 `123456`）

| 客户 | 手机号 | 适合演示 |
|---|---|---|
| Alice Chen | 4155550101 | 退货（ORD-1001 在退货期内）、取消/改地址（ORD-1002 已付款未发货）、物流（ORD-1003）、已完成退款（ORD-1005） |
| Ben Wright | 4155550102 | 待付款订单、部分退货（ORD-2002）、退货中（ORD-2004）、取消退款记录 |
| Carla Diaz | 4155550103 | 余额支付订单取消即时到账（ORD-3003）、已有维修预约（APT-C3004） |

可以试试：

- "What's your return policy?"：FAQ 快速通道，回复带 *Instant answer* 标记
- "My Aurora X2 only plays in one ear, what should I do?"：RAG 回答，附 PDF 页码引用
- "Where is my order?"：引导验证 → 查订单 → 追问是哪一单
- "I want to return my headphones"：进入退货表单，最后弹出确认卡片
- "I'd like to talk to a person"：转人工，生成工单号

## 项目结构

```
src/cs_agent/
  agent/        graph.py（主图）· tools.py · returns_flow.py · classifiers.py · prompts.py · events.py（SSE 事件）
  knowledge/    faq.py · ingest.py · retriever.py
  services/     订单 / 退货 / 账户 / 预约 / 转人工 业务规则
  auth/         otp.py（验证码）· tokens.py（JWT）
  db/           models.py · seed.py · session.py
  api/          app.py（FastAPI + SSE）· static/（网页）
  cli.py · runtime.py · llm.py · config.py
data/           faq.yaml · docs/（知识库源文件）· runtime/（Chroma、会话 checkpoint，已 gitignore）
scripts/        make_sample_pdfs.py · e2e_conversations.py
tests/          服务层 / 认证 / 知识库 / 对话图 / API
```
