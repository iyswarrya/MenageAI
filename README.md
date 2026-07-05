# MenageAI: AI-powered household purchase memory

Simple ReAct agent
Agent generated with `agents-cli` version `1.0.0`

## Project Structure

```
menage-ai-agent/
├── app/         # Core agent code
│   ├── agent.py               # Main agent logic & graph definitions
│   ├── interfaces.py          # Service Protocols & shared schemas
│   ├── services.py            # SQLite/Gemini default implementations
│   ├── db.py                  # Direct SQLite DB queries
│   ├── pii.py                 # Regex PII scrubber
│   ├── fast_api_app.py        # FastAPI Backend server
│   └── app_utils/             # App utilities and helpers
├── deployment/  # Google Cloud deployment files & Terraform templates
├── tests/                     # Unit, integration, and load tests
├── Dockerfile                 # Cloud Run deployment Dockerfile
├── docker-compose.yml         # Local container orchestration
├── GEMINI.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
```

## Architecture & Interface Design

The `menage-ai-agent` uses an interface-driven architecture defined via Python `typing.Protocol` classes. All core external dependencies—such as database access, deals clients, LLM receipt parsing, and PII redactors—are resolved through a central service registry. 

This design:
1. **Prevents Vendor Lock-in**: Decouples the ADK graph workflow logic from SQLite or specific LLM parsing helpers. We can easily swap SQLite with Firestore/Cloud SQL, mock deals with a live deals provider or MCP Client, and PII masking with Google Cloud DLP without editing the workflow.
2. **Allows Offline Testing & Mocking**: Unit tests can run completely offline, bypassing Vertex AI/Gemini API calls by registering mock services that return hardcoded structures.

### Key Interfaces (`app/interfaces.py`)

- **`ReceiptParser`**: Converts receipt text into `ReceiptData`.
- **`PurchaseMemoryRepository`**: Handles saving receipts, duplicate check warnings, and search history query execution.
- **`DealsClient`**: Looks up deals and price drop alerts.
- **`SecurityRedactor`**: Scrubs Personally Identifiable Information (PII) from user messages.
- **`AgentRunLogger`**: Structured logging of step inputs, outputs, errors, and tool calls.

The active implementations are registered in the global `registry` object defined in `app/interfaces.py` and populated with default implementations on startup in `app/agent.py`.

> 💡 **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)


## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        || [A2A Inspector](https://github.com/a2aproject/a2a-inspector) | Launch A2A Protocol Inspector                                                        |

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `app/agent.py` and test with `agents-cli playground` - it auto-reloads on save.

## Deployment

For comprehensive deployment instructions, required GCP APIs, environment variables, permissions, and manual setup guides, please refer to the detailed:
👉 **[deployment/README.md](deployment/README.md)**

To add CI/CD and Terraform infrastructure configurations, run:
```bash
agents-cli scaffold enhance
```

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use the [A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test interoperability.
See the [A2A Inspector docs](https://github.com/a2aproject/a2a-inspector) for details.

## Model Context Protocol (MCP) Deals Server

The agent integrates a local MCP server named `family-retail-mcp-server` to decouple deal lookups and return policy checks from direct database code.

### Why MCP is used for Deals
By moving deals and store-specific retail queries to an MCP server:
- We can easily swap out the mock database lookup with real live deals APIs, web scraping tools, or third-party retail connections without touching the core ADK agent workflow.
- It conforms to the Model Context Protocol (MCP) standard, allowing other MCP-compatible clients or agents to reuse these retail tools.

### How the ADK Agent Calls MCP
The workflow depends on the `DealsClient` interface. When `USE_MCP_DEALS=true` is set in the environment:
1. `MCPDealsClient` acts as the active registry implementation.
2. It spawns the MCP server subprocess using stdio transport:
   ```bash
   uv run python mcp_server/retail_server.py
   ```
3. It calls the MCP server's tools (`lookup_price` and `check_price_drop`) and returns the parsed structured alerts.

### How to Run the MCP Server Standalone
You can run the MCP server standalone in stdio mode:
```bash
uv run python mcp_server/retail_server.py
```
You can also run or test it using MCP inspector tools or add it to your desktop LLM client configuration (like Claude Desktop) using standard stdio parameters.

### Disabling MCP / SQLite Fallback
- **Disabling MCP**: Set `USE_MCP_DEALS=false` in your `.env` file. The agent will bypass the MCP client entirely and use `SqliteDealsClient` (which queries the SQLite database directly).
- **Auto Fallback**: If `USE_MCP_DEALS=true` but the MCP server crashes, is missing python dependencies, or the command fails, `MCPDealsClient` automatically catches the exception, logs a warning, and falls back to `SqliteDealsClient` so the agent keeps running without failing.


## 💬 Twilio WhatsApp Integration

The application includes complete support for mobile WhatsApp interaction via Twilio webhooks:
* **Webhook Endpoint**: `POST /twilio/webhook`
* When an image (photo of a receipt) or text query is sent to your Twilio WhatsApp number, the webhook asynchronously parses the receipt, registers the transaction, and sends back the coordinator response.
* **Security & Sandboxing**: Messages and ledgers are sandboxed under the Twilio family ID so that different users' ledger data is kept secure.
* **Setup**: Configure the Twilio WhatsApp Sandbox or Production number webhook URL to point to `https://<your-deployed-service-url>/twilio/webhook`.


## 🐳 Docker Containerization & Submissions

The project is fully productionized with Docker and docker-compose configurations.

### Standalone Local Running (One Command)
To build and run the entire suite (FastAPI Agent service, retail MCP server service, and databases) locally in containers with a single command:
```bash
docker-compose up --build
```
This starts:
- **FastAPI Web App** on `http://localhost:8000/` serving both the JSON API endpoints and the Interactive Demo Web Dashboard.
- **Standalone MCP Server** service (for modular verification / debugging).

---

## ⚡ Demo endpoints & Observability Dashboard

When the app is running (via `docker-compose` or `uv run python -m app.fast_api_app`), visit the root URL in your browser:
👉 **[http://localhost:8000/](http://localhost:8000/)**

The Interactive Web UI allows you to:
1. **Log Receipts**: Paste arbitrary receipt text (like "Whole Foods receipt, July 2, 2026. Bread $3.99. Total $3.99").
2. **Ask Memory Questions**: Chat with the agent (like "When did we buy bread?").
3. **Inspect Logs**: View structured JSON runs (logs showing user ID, intent, route, tools called, PII redacted count, and MCP status).
4. **View Purchase memory**: View past purchases list.

### Health Check Endpoints
- **Liveness/Health Probe**: `GET http://localhost:8000/health` (asserts database read/write and MCP server tools lookup check).
- **Readiness Probe**: `GET http://localhost:8000/ready` (confirms components are ready to receive user messages).

### Demo Script / Manual Verification
1. Open the Web Dashboard at `http://localhost:8000/`.
2. Paste the following receipt into the text area and press **Send Message**:
   ```
   Safeway Receipt
   Date: 2026-07-02
   Organic Coffee: $9.99
   Total: $9.99
   Card: 4111 1111 1111 1111 Phone: 555-0199
   ```
3. Observe:
   - The card number and phone are automatically redacted (PII count = 2 in the execution log).
   - The coordinator alerts the family about a mock price drop: *"We bought Coffee for $9.99, but Safeway has it for $7.99!"* (fetched from database mock deals).
4. Ask a memory question: *"When did we buy coffee?"*
5. The coordinator instantly searches historical SQLite memory and replies: *"We bought coffee on July 2nd, 2026, at Safeway for $9.99."*


