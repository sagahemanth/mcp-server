# Airline Care — Customer Service Resolution Agent

A training-focused, demo-only airline service orchestration system. The orchestrator preserves each conversation and passes data between six agents: customer intent, identity verification, passenger context, policy decision, resolution, and escalation. Five agents use the configured IBM ICA model for their assigned language tasks; identity verification remains a deterministic security gate. The model does not decide policy eligibility or execute actions. Those remain controlled by deterministic rules and confirmation-gated MCP tools. The system uses mock airline records, versioned demo policies, audited decisions, explicit passenger confirmation, and tracked specialist handoffs.

## Run locally

Requires Python 3.10 or newer.

```powershell
cd airline-resolution-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Copy .env.ica.example to .env.ica and add your IBM ICA key.
# Rotate any API key that has been pasted into chat or source control.
python -m uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>. IBM ICA values are read from the ignored local `.env.ica` file. Set `ICA_API_KEY` to an active IBM ICA key and use `ICA_API_STYLE=responses`; the app calls `https://api.servicesessentials.ibm.com/v1/responses` with model `gpt-5.6-luna` and reasoning effort `xhigh` by default. Confirm that the model and reasoning effort are enabled for your account. The header and agent panel show the target/active provider, model, and per-agent assignments at runtime and at `GET /api/config`. By default the app fails closed and starts no agent workflow without a key; set `REQUIRE_CONFIGURED_MODEL=false` only when you intentionally want the rule-based training mode. Never paste API keys into chat, source files, screenshots, or commits. Save a browser-only demo profile and generate three ordinary mock itineraries sharing the same passenger; exactly one generated flight is randomly marked cancelled by the airline, and ticket generation does not open a service case. Select a flight card to fill its PNR, or provide its flight ID; the agent asks for the passenger's last name and the user must type it to verify. A refund is eligible only for the cancelled booking. Boarding-pass requests show a QR generated from mock travel fields only, prominently marked not valid for travel; cancelled flights have no boarding pass. A baggage case is routed to a mock baggage desk for the booking's arrival airport; no real airport is contacted. Terminals and flight status are fixture data, not live airline feeds. Refunds show airline initiation, then pending bank approval to the original payment source; no bank is connected, so settlement is never claimed complete. After raising a request, the case result shows the audit-backed steps each agent performed and its unique `CS-` case ID can be entered in the private case tracker. Case IDs are bearer-style lookup tokens, not authentication; production needs authenticated ownership checks. No endpoint provides a public list of all cases or unscoped audit entries.

Generated demo flights, privacy-filtered case status snapshots, and agent action summaries are appended to CSV files in the local `data` folder. Open the **Saved data** page in the sidebar to review recent history and download each CSV. The CSVs intentionally omit passenger names, chat text, PNRs, and case lookup IDs; the demo profile remains browser-only. Set `AIRLINE_DATA_DIR` to choose a different storage folder. CSV data is local to this app instance and should be backed up separately if it must be retained. The files are excluded from version control. This is suitable for demo history, not production-grade persistence, access control, or multi-process concurrency. Firebase is not required for this local simulation; use an authentication/database provider such as Firebase only when real accounts and durable production storage are needed, and do not rely on the demo profile for production security.

## Model responsibilities

When `ICA_API_KEY` is configured in `.env.ica`, the selected `ICA_MODEL` is called through `ICA_BASE_URL` using the configured API style for these bounded tasks:

| Agent | Model responsibility | Guardrail |
| --- | --- | --- |
| Customer Intent | Classify intent, extract a stated booking reference, and help identify missing details | The orchestrator validates the intent and still requires the identity gate |
| Identity Verification | No LLM call; checks the supplied surname through the mock identity tool | Deterministic, case-bound verification before any booking context |
| Customer Context | Summarize verified booking facts for the workflow | Can only summarize data returned by the protected context tool |
| Policy Decision | Explain the policy result in customer-friendly language | Deterministic policy data and eligibility rules control the decision |
| Resolution | Prepare a note for the already-confirmed action | A one-time confirmation token and controlled MCP tool are still required to submit |
| Escalation | Produce an additional concise specialist handoff note | The deterministic queue and original case facts are preserved |

Model calls use the Responses API by default, request JSON output, have a timeout, and report errors instead of silently switching to a rule-based answer. Model output is never used as proof of identity, policy eligibility, transaction success, or permission to act. These calls incur provider usage and send the demo conversation/context to the configured provider; only use fixture data in this training app.

## Orchestration stages

1. **Receive and classify** the customer request; preserve channel, transcript, and conversation ID.
2. **Collect required details** such as a booking reference and ask the customer for the passenger's last name.
3. **Verify identity** via an MCP verification tool. The context tool requires a case-bound verification token; failed checks never reveal booking data.
4. **Retrieve context** from mock CRM/PSS/loyalty/payment records.
5. **Evaluate policy** using deterministic eligibility rules and a policy reference/version. The model cannot approve transactions.
6. **Present an option** with the amount, destination, rule explanation, and explicit statement that no action has yet been taken.
7. **Await confirmation**. Only an explicit yes leads to a one-time authorization token and a mock action.
8. **Verify and report** the tool's returned transaction state (`submitted`, not falsely described as completed), reference, and processing estimate.
9. **Reconcile or escalate** failed/uncertain outcomes without automatic retries; create a mock case-management handoff with the full summary.

The browser displays each agent's model assignment and live stage updates, asks for missing information over multiple turns, and streams workflow events. Sensitive-content screening runs before sending the conversation to IBM ICA; it includes common English profanity and insults as well as safety-related terms, and matches punctuation-separated spellings. Matching requests show a chat error and are routed to the specialist queue without reading booking context or invoking an LLM handoff summary. When IBM ICA is configured, each MCP workflow stage discovers its eligible tools live and asks the model to select a tool by name; deterministic stage, identity, policy, and confirmation gates validate the choice, and the application supplies every argument. The selected tool and selection source are recorded in the audit trail. Sensitive-content handoffs intentionally use deterministic routing so their message is never sent to the model. With no configured model, the explicitly rule-based demo mode uses deterministic routing instead.

## Demo scenarios

| Request | Booking | Expected outcome |
| --- | --- | --- |
| Airline cancelled the flight; request a refund | `PNR482` / Rivera | Identity → context → eligible offer → confirmation → refund submission |
| Delay over four hours; ask for lounge access | `PNR739` / Morgan | Policy-eligible offer, then confirmation and a simulated lounge pass ID |
| Missing checked bag | `PNR615` / Patel | Verified baggage trace offer, then confirmation and case submission |
| Refund for a basic fare | `PNR615` / Patel | Escalated for a policy exception review |
| Airline-cancellation refund | `PNR901` / Kim | Eligible cancelled booking; amount alone does not trigger a special high-value review |
| Sensitive request (medical, safety, harassment, etc.) | Any | Specialist handoff before booking context is accessed |
| Request without a booking reference | — | Agent asks for the booking reference, then verifies passenger identity |

## MCP tools

The FastMCP server is mounted at `/mcp`. With IBM ICA configured, the model dynamically selects an MCP tool at each workflow stage from the live-discovered set eligible for that stage—including identity verification, context and policy reads, resolution actions, reconciliation, and handoff. The orchestrator validates every selection against its stage and deterministic intent/policy constraints; model-generated tool arguments are never used. In particular, action tools can only be considered after policy evaluation, explicit passenger confirmation, and one-time authorization. Sensitive-content handoffs use deterministic tool routing to avoid sending their transcript to the model. In rule-based demo mode, tool routing is deterministic. A delay of at least 180 minutes qualifies the customer for a simulated lounge access pass identified by an `LA-` ID; it is not valid for entry to a real airport lounge. Actions are idempotent per case/action and recorded in the audit trail. `POST /api/chat` streams workflow progress and questions (not private model chain-of-thought) over multiple turns using `conversation_id`. Direct `POST /api/cases` action requests are deliberately rejected to prevent bypassing verification and confirmation.

## API

- `POST /api/cases` — disabled direct route; use the gated conversational workflow
- `POST /api/chat` — streamed conversational intake and resolution
- `GET /api/config` — active model/provider and per-agent assignments (never returns the API key)
- `GET /api/cases/{case_id}` — retrieve only the case addressed by its bearer-style tracking ID
- `GET /api/audit?case_id=...` — read audited decisions and tool actions
- `GET /data-log` — separate page for privacy-filtered persistent CSV history
- `GET /api/data-log` — recent data-log rows and counts, with no passenger or case IDs
- `GET /api/data-log/{flights,cases,actions}.csv` — download a CSV history file
- `POST /api/demo/tickets/random` — generate three regular mock itineraries; creates no support case
- `GET /api/health` — local health check
- `http://127.0.0.1:8000/mcp` — MCP Streamable HTTP endpoint

## Important

This is a training simulation, not an airline production system. Passenger, booking, payment, loyalty, and policy records are mock fixtures; actions and support handoffs are simulated and do not affect real airline systems. Current in-memory booking, conversation, refund, and case tracking state clears on server restart; the separate CSV logs retain only the explicitly listed privacy-filtered history. The channel selector records the request's declared source; email, voice transcription, and mobile-channel connectors are not integrated. The demo identity check is a surname match, not a production-grade identity proof. When configured, customer messages and verified mock context are sent to IBM ICA for the agent tasks listed above; do not use real passenger data. Policies are examples, not legal or carrier policy advice. Before production use, integrate an airline-authorized identity provider and sandbox, durable transaction storage, database-backed idempotency, secret management, privacy controls, monitoring, and validated carrier-approved policies.
