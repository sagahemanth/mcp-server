from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from airline_mcp import mcp
from data_log import CSV_FILENAMES, DATA_LOG
from domain import (
    BOOKINGS,
    create_random_demo_booking,
    get_case,
    get_audit_log,
    record_audit,
    save_case,
)

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")
load_dotenv(PROJECT_DIR / ".env.ica", override=True)
logger = logging.getLogger(__name__)
OPENAI_API_KEY = (
    os.getenv("ICA_API_KEY")
    if os.getenv("ICA_API_KEY") is not None
    else os.getenv("OPENAI_API_KEY", "")
).strip()
OPENAI_MODEL = (
    os.getenv("ICA_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5.6-luna"
).strip()
OPENAI_BASE_URL = (
    os.getenv("ICA_BASE_URL")
    or os.getenv("OPENAI_BASE_URL")
    or "https://api.openai.com/v1"
).rstrip("/")
CHAT_COMPLETIONS_URL = f"{OPENAI_BASE_URL}/chat/completions"
ICA_API_STYLE = os.getenv("ICA_API_STYLE", "responses").strip().lower()
ICA_REASONING_EFFORT = os.getenv("ICA_REASONING_EFFORT", "xhigh").strip().lower()
MODEL_PROVIDER = (
    "IBM Services Essentials"
    if "api.servicesessentials.ibm.com" in OPENAI_BASE_URL.lower()
    else "OpenAI-compatible API"
)
REQUIRE_CONFIGURED_MODEL = os.getenv("REQUIRE_CONFIGURED_MODEL", "true").lower() == "true"
MODEL_TIMEOUT_SECONDS = 45
SIMULATED_SYSTEM_DELAY_SECONDS = max(
    0.0, float(os.getenv("SIMULATED_SYSTEM_DELAY_SECONDS", "0.8"))
)
MAX_IDENTITY_ATTEMPTS = 3
READ_TOOL_INTENT_MAP = {
    "get_flight_service_info": {"boarding_pass", "flight_info"},
    "get_refund_status": {"refund_status"},
    "get_applicable_policy": {"refund", "delay_support", "baggage"},
}
SAFE_READ_TOOL_NAMES = tuple(READ_TOOL_INTENT_MAP)
ACTION_TOOL_INTENT_MAP = {
    "issue_refund": {"refund"},
    "issue_lounge_access_pass": {"delay_support"},
    "open_baggage_case": {"baggage"},
}
SAFE_ACTION_TOOL_NAMES = tuple(ACTION_TOOL_INTENT_MAP)
SAFE_MCP_TOOL_NAMES = (
    "verify_customer_identity",
    "get_customer_context",
    *SAFE_READ_TOOL_NAMES,
    "authorize_customer_resolution",
    "create_handoff_case",
    "get_resolution_status",
    *SAFE_ACTION_TOOL_NAMES,
)
_conversations: dict[str, dict[str, Any]] = {}
_conversation_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Airline Customer Service Resolution Agent", lifespan=lifespan)
app.mount("/mcp", mcp.streamable_http_app())
app.mount("/static", StaticFiles(directory="static"), name="static")
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp/")


class CaseRequest(BaseModel):
    message: str = Field(min_length=4, max_length=4000)
    channel: str = Field(default="web_chat", pattern="^(web_chat|email|voice|mobile)$")
    booking_reference: str | None = Field(default=None, max_length=20)


class ChatRequest(BaseModel):
    message: str = Field(min_length=2, max_length=4000)
    channel: str = Field(default="web_chat", pattern="^(web_chat|email|voice|mobile)$")
    booking_reference: str | None = Field(default=None, max_length=20)
    conversation_id: str | None = Field(default=None, max_length=50)


class DemoTicketRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)


def new_case_id() -> str:
    return f"CS-{uuid4().hex[:16].upper()}"


async def call_mcp_tool(name: str, arguments: dict[str, Any]) -> Any:
    async with streamablehttp_client(MCP_URL) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    if result.isError:
        raise HTTPException(status_code=502, detail=f"MCP tool returned an error: {name}")
    for item in result.content:
        if hasattr(item, "text"):
            try:
                return json.loads(item.text)
            except json.JSONDecodeError:
                return item.text
    raise HTTPException(status_code=502, detail=f"MCP tool returned no content: {name}")


async def discover_mcp_tools(allowed_names: set[str]) -> list[dict[str, Any]]:
    async with streamablehttp_client(MCP_URL) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
    tools = []
    for tool in result.tools:
        if tool.name not in allowed_names:
            continue
        tools.append({
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.inputSchema,
        })
    missing = allowed_names - {tool["name"] for tool in tools}
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"Required safe MCP tools are unavailable: {', '.join(sorted(missing))}.",
        )
    return tools


def validate_selected_mcp_tool(
    selected_name: str,
    *,
    intent: str,
    available_names: set[str],
) -> str:
    if selected_name not in SAFE_READ_TOOL_NAMES or selected_name not in available_names:
        raise HTTPException(
            status_code=502,
            detail="The model selected an MCP tool that is not available for this workflow.",
        )
    if intent not in READ_TOOL_INTENT_MAP[selected_name]:
        raise HTTPException(
            status_code=502,
            detail="The model selected an MCP tool that is not eligible for the classified request.",
        )
    return selected_name


async def select_stage_mcp_tool(
    *,
    stage: str,
    intent: str,
    fallback_name: str,
    eligible_names: set[str],
    request_text: str,
    case_id: str,
    context: dict[str, Any] | None = None,
    allow_model: bool = True,
) -> tuple[str, str]:
    if fallback_name not in eligible_names or not eligible_names <= set(SAFE_MCP_TOOL_NAMES):
        raise HTTPException(
            status_code=500,
            detail="The workflow supplied an invalid MCP tool eligibility set.",
        )
    if not allow_model or not OPENAI_API_KEY:
        selected_name = fallback_name
        selection_source = "deterministic_safety_fallback"
    else:
        catalog = [
            tool
            for tool in await discover_mcp_tools(eligible_names)
            if tool["name"] in eligible_names
        ]
        if {tool["name"] for tool in catalog} != eligible_names:
            raise HTTPException(
                status_code=502,
                detail="One or more eligible MCP tools are missing from the live tool catalog.",
            )
        selection = await run_model_agent(
            "tool-selection",
            (
                "You are an MCP tool router. Choose the single tool that best performs the "
                "current workflow stage from the supplied live-discovered catalog. Return JSON "
                "with exactly one string key, tool_name, containing the exact selected tool name. "
                "Do not invent tools, provide arguments, change stages, determine policy eligibility, "
                "or authorize actions. The application enforces all safety and policy gates and "
                "supplies every tool argument."
            ),
            {
                "workflow_stage": stage,
                "classified_intent": intent,
                "customer_request": request_text,
                "workflow_context": context or {},
                "eligible_mcp_tools": catalog,
            },
        )
        selected_name = selection.get("tool_name")
        if not isinstance(selected_name, str) or selected_name not in eligible_names:
            raise HTTPException(
                status_code=502,
                detail="The model selected an MCP tool that is not eligible for this workflow stage.",
            )
        selection_source = "model"
    record_audit(
        case_id=case_id,
        actor="mcp-tool-router",
        action="select_mcp_tool",
        outcome="selected",
        details={
            "tool_name": selected_name,
            "selection_source": selection_source,
            "workflow_stage": stage,
            "classified_intent": intent,
        },
    )
    return selected_name, selection_source


async def call_stage_mcp_tool(
    fallback_name: str,
    arguments: dict[str, Any],
    *,
    stage: str,
    intent: str,
    case_id: str,
    request_text: str,
    context: dict[str, Any] | None = None,
    eligible_names: set[str] | None = None,
    allow_model: bool = True,
) -> Any:
    selected_name, _ = await select_stage_mcp_tool(
        stage=stage,
        intent=intent,
        fallback_name=fallback_name,
        eligible_names=eligible_names or {fallback_name},
        request_text=request_text,
        case_id=case_id,
        context=context,
        allow_model=allow_model,
    )
    return await call_mcp_tool(selected_name, arguments)


async def select_read_mcp_tool(
    *,
    intent: str,
    request_text: str,
    booking: dict[str, Any],
    case_id: str,
) -> tuple[str, str]:
    if not any(intent in intents for intents in READ_TOOL_INTENT_MAP.values()):
        raise HTTPException(
            status_code=500,
            detail=f"No unambiguous protected MCP read-tool set exists for intent '{intent}'.",
        )
    if not OPENAI_API_KEY:
        # The intentionally rule-based demo mode has no LLM to choose tools.
        selected = next(
            name for name, intents in READ_TOOL_INTENT_MAP.items() if intent in intents
        )
        return validate_selected_mcp_tool(
            selected,
            intent=intent,
            available_names=set(SAFE_READ_TOOL_NAMES),
        ), "deterministic_demo"

    catalog = await discover_mcp_tools(set(SAFE_READ_TOOL_NAMES))
    selection = await run_model_agent(
        "tool-selection",
        (
            "You are a tool router. Choose the single MCP read tool that best handles the "
            "current customer-service task from the supplied, live-discovered tool catalog. "
            "Return JSON with exactly one string key, tool_name, containing the exact selected "
            "tool name. Do not invent tools, provide arguments, execute actions, determine policy "
            "eligibility, or choose a tool not in the catalog. The application enforces identity "
            "verification and policy gates and supplies all tool arguments."
        ),
        {
            "customer_request": request_text,
            "classified_intent": intent,
            "verified_booking_summary": {
                "flight": booking.get("flight"),
                "flight_status": booking.get("flight_status"),
                "booking_status": booking.get("status"),
            },
            "available_mcp_tools": catalog,
        },
    )
    selected_name = selection.get("tool_name")
    if not isinstance(selected_name, str):
        raise HTTPException(
            status_code=502,
            detail="The model did not return a valid MCP tool selection.",
        )
    selected = validate_selected_mcp_tool(
        selected_name,
        intent=intent,
        available_names={tool["name"] for tool in catalog},
    )
    return selected, "model"


def classify_intent(message: str) -> tuple[str, str]:
    text = message.lower()
    if any(term in text for term in ("refund status", "track my refund", "refund tracking", "bank processing")):
        return "refund_status", ""
    if any(term in text for term in ("boarding pass", "boarding-pass", "check-in pass")):
        return "boarding_pass", ""
    if any(term in text for term in ("terminal", "gate number", "flight status", "departure time")):
        return "flight_info", ""
    if any(term in text for term in ("bag", "baggage", "luggage", "suitcase")):
        return "baggage", "baggage"
    if any(term in text for term in ("refund", "money back", "reimburse")):
        airline_cancel = re.search(
            r"\bairline\b.{0,30}\bcancel\w*|\bcancel\w*.{0,30}\bairline\b",
            text,
        )
        return "refund", "involuntary_refund" if airline_cancel else "voluntary_refund"
    if any(term in text for term in ("voucher", "delay", "delayed", "late")):
        return "delay_support", "delay_lounge_access"
    if any(term in text for term in ("rebook", "new flight", "change my flight", "reschedule")):
        return "rebooking", ""
    return "general", ""


def find_booking_reference(request: CaseRequest) -> str | None:
    if request.booking_reference:
        reference = request.booking_reference.strip().upper()
        if re.fullmatch(r"AR\d{3}", reference):
            return next(
                (
                    booking["booking_reference"]
                    for booking in BOOKINGS.values()
                    if booking["flight"].upper() == reference
                ),
                None,
            )
        return reference
    match = re.search(r"\bPNR\s*[-:#]?\s*([A-Z0-9]{3,8})\b", request.message, re.IGNORECASE)
    if match:
        return f"PNR{match.group(1).upper()}"
    flight_match = re.search(r"\bAR\d{3}\b", request.message, re.IGNORECASE)
    if flight_match:
        return find_booking_reference(
            CaseRequest(message="Flight lookup", booking_reference=flight_match.group(0))
        )
    return None


def is_sensitive_request(message: str) -> bool:
    sensitive_terms = (
        "ass",
        "asshole",
        "assholes",
        "arse",
        "arsehole",
        "bastard",
        "bastards",
        "bitch",
        "bitches",
        "bullshit",
        "crap",
        "cunt",
        "cunts",
        "damn",
        "dick",
        "dicks",
        "dumb",
        "dumbass",
        "dumbasses",
        "fool",
        "fuck",
        "fucked",
        "fucker",
        "fuckers",
        "fucking",
        "fucks",
        "hell",
        "idiot",
        "idiots",
        "jerk",
        "jerks",
        "loser",
        "losers",
        "motherfucker",
        "motherfuckers",
        "moron",
        "morons",
        "piss",
        "prick",
        "pricks",
        "shit",
        "shits",
        "shitty",
        "slut",
        "sluts",
        "stupid",
        "twat",
        "twats",
        "wanker",
        "wankers",
        "whore",
        "whores",
        "medical",
        "injury",
        "injured",
        "emergency",
        "safety",
        "harassment",
        "discrimination",
        "legal",
        "fraud",
        "data breach",
        "sex",
        "sexy",
        "sexual",
        "porn",
        "pornography",
        "nude",
        "explicit",
        "assault",
        "abuse",
        "suicide",
        "self-harm",
        "weapon",
        "threat",
    )
    normalized = re.sub(r"[^\w\s]", "", message.casefold())
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized)
        for term in sensitive_terms
    )


def model_configuration() -> dict[str, Any]:
    supported_api_style = ICA_API_STYLE in {"openai-compatible", "responses"}
    enabled = bool(OPENAI_API_KEY) and supported_api_style
    assignments = [
        ("intent", "Customer Intent Agent", "model"),
        ("identity", "Identity Verification Agent", "deterministic"),
        ("context", "Customer Context Agent", "model"),
        ("policy", "Policy Decision Agent", "model + policy rules"),
        ("resolution", "Resolution Agent", "model + controlled tools"),
        ("escalation", "Escalation Agent", "model + case tools"),
    ]
    agents = [
        {
            "id": agent_id,
            "name": name,
            "mode": (
                "deterministic"
                if agent_id == "identity"
                else mode
                if enabled
                else "inactive · API key required"
            ),
            "model": OPENAI_MODEL if agent_id != "identity" else None,
        }
        for agent_id, name, mode in assignments
    ]
    model_agents = sum(agent["model"] is not None for agent in agents)
    return {
        "enabled": enabled,
        "provider": MODEL_PROVIDER,
        "model": OPENAI_MODEL,
        "api_style": ICA_API_STYLE,
        "mcp_tool_selection": (
            "model selects from live-discovered, protected read tools"
            if enabled
            else "rule-based selection; configure a model for dynamic MCP tool selection"
        ),
        "label": (
            f"{MODEL_PROVIDER} · {OPENAI_MODEL} · {model_agents} agents assigned · key configured"
            if enabled
            else f"{MODEL_PROVIDER} target · unsupported API format: {ICA_API_STYLE}"
            if not supported_api_style
            else f"{MODEL_PROVIDER} target · {OPENAI_MODEL} · API key required"
        ),
        "agents": agents,
        "setup_required": not enabled,
        "configuration_error": (
            "Set ICA_API_STYLE=responses or ICA_API_STYLE=openai-compatible."
            if not supported_api_style
            else None
        ),
    }


async def run_model_agent(
    agent_id: str,
    system_prompt: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    if ICA_API_STYLE not in {"openai-compatible", "responses"}:
        raise HTTPException(
            status_code=503,
            detail=(
                f"API format '{ICA_API_STYLE}' is not supported by this application. "
                "Set ICA_API_STYLE=responses or ICA_API_STYLE=openai-compatible."
            ),
        )
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="IBM ICA is not configured. Add a new ICA_API_KEY to the local .env.ica file and restart the app.",
        )
    try:
        async with httpx.AsyncClient(timeout=MODEL_TIMEOUT_SECONDS) as client:
            if ICA_API_STYLE == "responses":
                endpoint = f"{OPENAI_BASE_URL}/responses"
                request_body: dict[str, Any] = {
                    "model": OPENAI_MODEL,
                    "input": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": json.dumps(context, ensure_ascii=True),
                        },
                    ],
                    "text": {"format": {"type": "json_object"}},
                    "reasoning": {"effort": ICA_REASONING_EFFORT},
                }
            else:
                endpoint = CHAT_COMPLETIONS_URL
                request_body = {
                    "model": OPENAI_MODEL,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": json.dumps(context, ensure_ascii=True),
                        },
                    ],
                }
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            response.raise_for_status()
            payload = response.json()
            if ICA_API_STYLE == "responses":
                content = payload.get("output_text")
                if content is None:
                    content = next(
                        (
                            item["text"]
                            for output in payload.get("output", [])
                            for item in output.get("content", [])
                            if item.get("type") == "output_text"
                        ),
                        None,
                    )
            else:
                content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("Model response did not contain output text.")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise TypeError("Model response must be a JSON object.")
            return result
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.error("%s model request failed with provider HTTP %s", agent_id, status_code)
        if status_code in {401, 403}:
            if MODEL_PROVIDER == "IBM Services Essentials":
                detail = (
                    f"{MODEL_PROVIDER} rejected ICA_API_KEY (HTTP {status_code}). "
                    "Check that .env.ica contains the active ICA Coding Agents Open Code key "
                    "and that it has model access."
                )
            else:
                detail = f"{MODEL_PROVIDER} rejected the API credentials (HTTP {status_code})."
        elif status_code == 404:
            detail = (
                f"{MODEL_PROVIDER} could not find the configured endpoint or model (HTTP 404). "
                "Check OPENAI_BASE_URL and the exact OPENAI_MODEL ID."
            )
        else:
            detail = f"{MODEL_PROVIDER} returned HTTP {status_code} for the {agent_id} model step."
        raise HTTPException(status_code=502, detail=detail) from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        logger.exception("%s model agent call failed", agent_id)
        raise HTTPException(
            status_code=502,
            detail=f"The configured model could not complete the {agent_id} agent step. Please retry.",
        ) from exc


def required_model_text(agent_id: str, result: dict[str, Any], key: str, limit: int) -> str:
    value = result.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=502,
            detail=f"The {agent_id} agent returned an invalid response. Please retry.",
        )
    return value.strip()[:limit]


async def analyze_with_model(messages: list[dict[str, str]]) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OpenAI model is not configured.")
    system_prompt = (
        "You are the airline customer-intent and clarification agent. Read the whole "
        "conversation and return only a JSON object with keys intent, booking_reference, "
        "and clarification_question. intent must be one of refund, refund_status, delay_support, "
        "baggage, boarding_pass, flight_info, rebooking, or general. Extract a booking reference only if the customer stated one. "
        "Ask at most one concise, useful follow-up question when a necessary detail is missing. "
        "Always ask for the booking reference before retrieving booking information if it is "
        "missing. For a baggage issue, ask for the bag-tag number only when the customer knows "
        "it; do not block opening a trace on that detail. Do not ask for sensitive payment "
        "credentials, passwords, or full card numbers. Do not make eligibility, refund, voucher, "
        "or compensation decisions: deterministic airline policy checks handle those."
    )
    try:
        async with httpx.AsyncClient(timeout=MODEL_TIMEOUT_SECONDS) as client:
            if ICA_API_STYLE == "responses":
                endpoint = f"{OPENAI_BASE_URL}/responses"
                request_body: dict[str, Any] = {
                    "model": OPENAI_MODEL,
                    "input": [
                        {"role": "system", "content": system_prompt},
                        *messages[-10:],
                    ],
                    "text": {"format": {"type": "json_object"}},
                    "reasoning": {"effort": ICA_REASONING_EFFORT},
                }
            else:
                endpoint = CHAT_COMPLETIONS_URL
                request_body = {
                    "model": OPENAI_MODEL,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": system_prompt}, *messages[-10:]],
                }
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            response.raise_for_status()
            payload = response.json()
            if ICA_API_STYLE == "responses":
                content = payload.get("output_text")
                if content is None:
                    content = next(
                        (
                            item["text"]
                            for output in payload.get("output", [])
                            for item in output.get("content", [])
                            if item.get("type") == "output_text"
                        ),
                        None,
                    )
            else:
                content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("Model response did not contain output text.")
            analysis = json.loads(content)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.error("Intent model request failed with provider HTTP %s", status_code)
        if status_code in {401, 403}:
            if MODEL_PROVIDER == "IBM Services Essentials":
                detail = (
                    f"{MODEL_PROVIDER} rejected ICA_API_KEY (HTTP {status_code}). "
                    "Check that .env.ica contains the active ICA Coding Agents Open Code key "
                    "and that it has model access."
                )
            else:
                detail = f"{MODEL_PROVIDER} rejected the API credentials (HTTP {status_code})."
        elif status_code == 404:
            detail = (
                f"{MODEL_PROVIDER} could not find the configured endpoint or model (HTTP 404). "
                "Check OPENAI_BASE_URL and the exact OPENAI_MODEL ID."
            )
        else:
            detail = f"{MODEL_PROVIDER} returned HTTP {status_code} for the intent model step."
        raise HTTPException(status_code=502, detail=detail) from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        logger.exception("%s intent analysis failed", MODEL_PROVIDER)
        raise HTTPException(
            status_code=502,
            detail="The configured model could not analyze this message. Please retry.",
        ) from exc

    intent = analysis.get("intent")
    if intent not in {
        "refund", "refund_status", "delay_support", "baggage",
        "boarding_pass", "flight_info", "rebooking", "general",
    }:
        raise HTTPException(status_code=502, detail="The model returned an unsupported intent.")
    reference = analysis.get("booking_reference")
    if reference is not None and not re.fullmatch(r"PNR[A-Z0-9]{3,8}", str(reference).upper()):
        reference = None
    question = analysis.get("clarification_question", "")
    if not isinstance(question, str):
        question = ""
    return {
        "intent": intent,
        "booking_reference": str(reference).upper() if reference else None,
        "clarification_question": question.strip()[:500],
    }


def classify_intent_from_model(intent: str, message: str = "") -> tuple[str, str]:
    policy_key = {
        "refund": classify_intent(message)[1] or "voluntary_refund",
        "refund_status": "",
        "delay_support": "delay_lounge_access",
        "baggage": "baggage",
        "boarding_pass": "",
        "flight_info": "",
        "rebooking": "",
        "general": "",
    }[intent]
    return intent, policy_key


def question_for_missing_reference(intent: str) -> str:
    subject = {
        "refund": "your refund request",
        "refund_status": "your refund status",
        "delay_support": "the delay on your flight",
        "baggage": "your baggage issue",
        "boarding_pass": "your boarding pass",
        "flight_info": "your flight or terminal information",
        "rebooking": "your flight change",
        "general": "your request",
    }[intent]
    return f"I can look into {subject}. What is your booking reference (PNR)?"


def question_for_identity() -> str:
    return "For your security, what is the passenger's last name exactly as it appears on this booking?"


def needs_refund_cause_clarification(intent: str, message: str) -> bool:
    if intent != "refund":
        return False
    text = message.lower()
    airline_cancel = re.search(
        r"\bairline\b.{0,30}\bcancel\w*|\bcancel\w*.{0,30}\bairline\b",
        text,
    )
    passenger_cancel = re.search(
        r"\b(i|we|passenger)\b.{0,25}\b(cancel\w*|cancell\w*)\b|\bmy own booking\b",
        text,
    )
    return not airline_cancel and not passenger_cancel


def question_for_refund_cause() -> str:
    return (
        "To check the right refund policy, was the flight cancelled by the airline, "
        "or are you asking to cancel your own booking?"
    )


def extract_last_name(message: str) -> str | None:
    normalized = message.strip()
    explicit = re.search(
        r"\b(?:last name|surname)\s*(?:is|:)?\s*([A-Za-z][A-Za-z'-]{1,39})\b",
        normalized,
        re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1)
    if re.fullmatch(r"[A-Za-z][A-Za-z'-]{1,39}", normalized):
        return normalized
    return None


def confirmation_choice(message: str) -> bool | None:
    normalized = re.sub(r"[^a-z ]", " ", message.lower()).strip()
    if re.search(r"\b(no|not now|cancel|decline|do not|don't)\b", normalized):
        return False
    if re.search(r"\b(yes|confirm|proceed|go ahead|approve|do it)\b", normalized):
        return True
    return None


async def process_chat_turn(
    request: ChatRequest,
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    try:
        async with _conversation_lock:
            if request.conversation_id and request.conversation_id not in _conversations:
                raise HTTPException(status_code=404, detail="This conversation has expired. Start a new request.")
            conversation_id = request.conversation_id or new_case_id()
            conversation = _conversations.setdefault(
                conversation_id,
                {
                    "messages": [],
                    "booking_reference": None,
                    "channel": request.channel,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "clarification_count": 0,
                    "identity_attempts": 0,
                    "phase": "intake",
                    "case": None,
                    "verification_token": None,
                },
            )
            previous_phase = conversation["phase"]
            conversation["channel"] = request.channel
            conversation["messages"].append({"role": "user", "content": request.message})
            if request.booking_reference:
                conversation["booking_reference"] = find_booking_reference(
                    CaseRequest(message="Booking lookup", booking_reference=request.booking_reference)
                )
            model_messages = list(conversation["messages"])

        config = model_configuration()
        transcript = "\n".join(
            message["content"] for message in model_messages if message["role"] == "user"
        )
        sensitive_input = is_sensitive_request(transcript)
        if REQUIRE_CONFIGURED_MODEL and not config["enabled"] and not sensitive_input:
            async with _conversation_lock:
                _conversations.pop(conversation_id, None)
            await emit({
                "type": "error",
                "message": (
                    config["configuration_error"]
                    or "The IBM ICA model is not configured, so no agent workflow was started. "
                    "Add a fresh ICA_API_KEY to the local .env.ica file and restart the app."
                ),
            })
            return

        async def report_progress(agent: str, message: str, state: str = "active") -> None:
            await emit({"type": "progress", "agent": agent, "message": message, "state": state})

        if sensitive_input:
            intent, _ = classify_intent(transcript)
            conversation["intent"] = intent
            summary = (
                f"Sensitive content was detected in a {request.channel} request. "
                "No booking context was accessed and no automated action was taken. "
                "A specialist should review the customer request."
            )
            sensitive_case = {
                "case_id": conversation_id,
                "channel": request.channel,
                "message": transcript,
                "intent": intent,
                "status": "investigating",
                "transaction_status": "not_started",
                "response": (
                    "Sensitive content was detected. Automated processing stopped and a specialist "
                    "handoff was created. No booking data was accessed and no automated action was taken."
                ),
                "created_at": conversation["created_at"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            record_audit(
                case_id=conversation_id,
                actor="customer-intent-agent",
                action="detect_sensitive_content",
                outcome="handoff_required",
                details={
                    "channel": request.channel,
                    "booking_context_accessed": False,
                    "automated_action_taken": False,
                },
            )
            sensitive_result = await create_handoff(
                sensitive_case,
                queue="sensitive_case_support",
                summary=summary,
                response=sensitive_case["response"],
                progress=report_progress,
            )
            async with _conversation_lock:
                _conversations.pop(conversation_id, None)
            await emit({
                "type": "result",
                "payload": sensitive_result,
                "model": config,
                "conversation_id": conversation_id,
            })
            return

        if previous_phase == "awaiting_confirmation":
            confirmed = confirmation_choice(request.message)
            if confirmed is None:
                await emit({
                    "type": "question",
                    "conversation_id": conversation_id,
                    "message": "Would you like me to proceed with the option shown above? Please answer yes to confirm or no to cancel.",
                    "model": config,
                    "intent": conversation["intent"],
                })
                return
            current_case = conversation["case"]
            if not confirmed:
                current_case["status"] = "cancelled"
                current_case["response"] = "No action was taken. Your request is closed; you can start a new request at any time."
                current_case["updated_at"] = datetime.now(timezone.utc).isoformat()
                record_audit(
                    case_id=conversation_id,
                    actor="resolution-agent",
                    action="customer_declined_resolution",
                    outcome="cancelled",
                    details={"action_taken": False, "booking_reference": conversation["booking_reference"]},
                )
                save_case(current_case)
                async with _conversation_lock:
                    _conversations.pop(conversation_id, None)
                await emit({
                    "type": "result",
                    "payload": {"case": current_case, "audit": get_audit_log(conversation_id)},
                    "model": config,
                    "conversation_id": conversation_id,
                })
                return
            conversation["phase"] = "confirmed"

        if previous_phase == "awaiting_identity":
            last_name = extract_last_name(request.message)
            if not last_name:
                await emit({
                    "type": "question",
                    "conversation_id": conversation_id,
                    "message": question_for_identity(),
                    "model": config,
                    "intent": conversation["intent"],
                })
                return
            await report_progress(
                "Identity Verification Agent",
                "Checking the supplied passenger detail against the booking without exposing booking data…",
            )
            if SIMULATED_SYSTEM_DELAY_SECONDS:
                await asyncio.sleep(SIMULATED_SYSTEM_DELAY_SECONDS)
            verification = await call_stage_mcp_tool(
                "verify_customer_identity",
                {
                    "booking_reference": conversation["booking_reference"],
                    "last_name": last_name,
                    "case_id": conversation_id,
                },
                stage="identity_verification",
                intent=conversation["intent"],
                case_id=conversation_id,
                request_text="Verify the passenger-provided last name against the supplied booking reference.",
                context={"booking_reference": conversation["booking_reference"]},
            )
            if not verification.get("verified"):
                conversation["identity_attempts"] += 1
                if conversation["identity_attempts"] >= MAX_IDENTITY_ATTEMPTS:
                    summary = (
                        f"Identity could not be verified after {MAX_IDENTITY_ATTEMPTS} attempts. "
                        f"Intent: {conversation['intent']}; booking reference: "
                        f"{conversation['booking_reference']}. No booking context was accessed and no action was taken."
                    )
                    identity_case = {
                        "case_id": conversation_id,
                        "channel": request.channel,
                        "message": "\n".join(
                            item["content"] for item in model_messages if item["role"] == "user"
                        ),
                        "intent": conversation["intent"],
                        "status": "investigating",
                        "transaction_status": "not_started",
                        "response": "I could not verify the booking details, so no booking data was accessed and no action was taken. A specialist will help you securely.",
                        "created_at": conversation["created_at"],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    identity_result = await create_handoff(
                        identity_case,
                        queue="identity_support",
                        summary=summary,
                        response=identity_case["response"],
                        progress=report_progress,
                    )
                    async with _conversation_lock:
                        _conversations.pop(conversation_id, None)
                    await emit({
                        "type": "result",
                        "payload": identity_result,
                        "model": config,
                        "conversation_id": conversation_id,
                    })
                    return
                await emit({
                    "type": "question",
                    "conversation_id": conversation_id,
                    "message": "Those details did not match our booking records. Please check the passenger's last name and try again, or ask a specialist for help.",
                    "model": config,
                    "intent": conversation["intent"],
                })
                return
            conversation["verification_token"] = verification["verification_token"]
            conversation["phase"] = "investigating"
        elif previous_phase in {"awaiting_confirmation", "confirmed"}:
            pass
        else:
            transcript = "\n".join(message["content"] for message in model_messages)
            if config["enabled"]:
                await report_progress(
                    "Customer Intent Agent",
                    f"Using {OPENAI_MODEL} to identify the request and missing information…",
                )
                analysis = await analyze_with_model(model_messages)
                intent, _ = classify_intent_from_model(analysis["intent"], transcript)
            else:
                await report_progress(
                    "Customer Intent Agent",
                    "Classifying the request with demo rules; no language model is configured…",
                )
                intent, _ = classify_intent(transcript)
                analysis = {"intent": intent, "booking_reference": None}
            if request.booking_reference:
                conversation["booking_reference"] = find_booking_reference(
                    CaseRequest(message="Booking lookup", booking_reference=request.booking_reference)
                )
            elif analysis.get("booking_reference"):
                conversation["booking_reference"] = find_booking_reference(
                    CaseRequest(
                        message="Booking lookup",
                        booking_reference=str(analysis["booking_reference"]),
                    )
                )
            else:
                extracted = find_booking_reference(
                    CaseRequest(message=transcript, channel=request.channel)
                )
                if extracted:
                    conversation["booking_reference"] = extracted
            conversation["intent"] = intent
            conversation["intent_policy_key"] = classify_intent_from_model(intent, transcript)[1]
            record_audit(
                case_id=conversation_id,
                actor="customer-intent-agent",
                action="classify_intent",
                outcome="success",
                details={
                    "intent": intent,
                    "channel": request.channel,
                    "model": config["model"],
                    "booking_reference_provided": bool(conversation["booking_reference"]),
                },
            )
            if needs_refund_cause_clarification(intent, transcript):
                if conversation["clarification_count"] >= 2:
                    summary = (
                        "The customer requested a refund, but the cancellation type remained unclear after "
                        "two clarification questions. No booking context was accessed and no action was taken. "
                        f"Conversation: {transcript}"
                    )
                    clarification_case = {
                        "case_id": conversation_id,
                        "channel": request.channel,
                        "message": transcript,
                        "intent": intent,
                        "status": "investigating",
                        "transaction_status": "not_started",
                        "response": "I could not determine which refund policy applies. No booking data was accessed and no refund was submitted. A specialist will clarify the options.",
                        "created_at": conversation["created_at"],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    clarification_result = await create_handoff(
                        clarification_case,
                        queue="refund_review",
                        summary=summary,
                        response=clarification_case["response"],
                        progress=report_progress,
                    )
                    async with _conversation_lock:
                        _conversations.pop(conversation_id, None)
                    await emit({
                        "type": "result",
                        "payload": clarification_result,
                        "model": config,
                        "conversation_id": conversation_id,
                    })
                    return
                question = question_for_refund_cause()
                conversation["phase"] = "awaiting_issue_detail"
                conversation["clarification_count"] += 1
                conversation["messages"].append({"role": "assistant", "content": question})
                record_audit(
                    case_id=conversation_id,
                    actor="customer-intent-agent",
                    action="ask_follow_up",
                    outcome="awaiting_customer",
                    details={"intent": intent, "missing_information": "cancellation_type"},
                )
                await emit({
                    "type": "question",
                    "conversation_id": conversation_id,
                    "message": question,
                    "model": config,
                    "intent": intent,
                })
                return
            if not conversation["booking_reference"]:
                question = f"I can look into this. Which flight ID (for example AR123) or booking reference (PNR) should I check?"
                conversation["phase"] = "awaiting_booking"
                conversation["messages"].append({"role": "assistant", "content": question})
                record_audit(
                    case_id=conversation_id,
                    actor="customer-intent-agent",
                    action="ask_follow_up",
                    outcome="awaiting_customer",
                    details={"missing_information": "booking_reference"},
                )
                await emit({
                    "type": "question",
                    "conversation_id": conversation_id,
                    "message": question,
                    "model": config,
                    "intent": intent,
                })
                return
            conversation["phase"] = "awaiting_identity"

        if conversation["phase"] == "awaiting_booking":
            booking_reference = find_booking_reference(
                CaseRequest(message=request.message, channel=request.channel)
            )
            if not booking_reference and request.booking_reference:
                booking_reference = request.booking_reference.strip().upper()
            if not booking_reference:
                await emit({
                    "type": "question",
                    "conversation_id": conversation_id,
                    "message": "I still need your booking reference (PNR) to continue.",
                    "model": config,
                    "intent": conversation["intent"],
                })
                return
            conversation["booking_reference"] = booking_reference
            conversation["phase"] = "awaiting_identity"

        if conversation["phase"] == "awaiting_identity":
            question = question_for_identity()
            conversation["messages"].append({"role": "assistant", "content": question})
            record_audit(
                case_id=conversation_id,
                actor="identity-verification-agent",
                action="request_identity_verification",
                outcome="awaiting_customer",
                details={
                    "booking_reference": conversation["booking_reference"],
                    "verification_method": "booking_reference_and_last_name",
                },
            )
            await emit({
                "type": "question",
                "conversation_id": conversation_id,
                "message": question,
                "model": config,
                "intent": conversation["intent"],
            })
            return

        full_message = "\n".join(
            item["content"] for item in model_messages if item["role"] == "user"
        )
        case_request = CaseRequest(
            message=full_message,
            channel=request.channel,
            booking_reference=conversation["booking_reference"],
        )
        is_confirmed = conversation["phase"] == "confirmed"
        if is_confirmed:
            await report_progress(
                "Resolution Agent",
                "Passenger confirmed the displayed option. Rechecking eligibility before submitting the mock action…",
            )
        elif conversation["phase"] == "investigating":
            conversation["phase"] = "awaiting_confirmation"
        result = await create_case(
            case_request,
            case_id=conversation_id,
            progress=report_progress,
            intent_override=conversation["intent"],
            verification_token=conversation["verification_token"],
            customer_confirmed=is_confirmed,
        )
        if result["case"]["status"] == "awaiting_confirmation":
            conversation["phase"] = "awaiting_confirmation"
            conversation["case"] = result["case"]
            question = result["case"]["response"]
            conversation["messages"].append({"role": "assistant", "content": question})
            await emit({
                "type": "offer",
                "conversation_id": conversation_id,
                "message": question,
                "payload": result,
                "model": config,
                "intent": conversation["intent"],
            })
            return
        if result["case"]["status"] in {"submitted", "escalated", "cancelled", "resolved"}:
            async with _conversation_lock:
                _conversations.pop(conversation_id, None)
        await emit({
            "type": "result",
            "payload": result,
            "model": config,
            "conversation_id": conversation_id,
        })
    except HTTPException as exc:
        await emit({"type": "error", "message": exc.detail})
    except Exception:
        logger.exception("Customer conversation processing failed")
        await emit({
            "type": "error",
            "message": "The request could not be completed. Please try again or contact a specialist.",
        })
    finally:
        await queue.put({"type": "done"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/data-log")
async def data_log_page() -> FileResponse:
    return FileResponse("static/data-log.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def app_config() -> dict[str, Any]:
    return {"model": model_configuration()}


@app.get("/api/data-log")
async def data_log_summary() -> dict[str, Any]:
    return DATA_LOG.dashboard()


@app.get("/api/data-log/{kind}.csv")
async def download_data_log(kind: str) -> FileResponse:
    if kind not in CSV_FILENAMES:
        raise HTTPException(status_code=404, detail="No CSV log was found.")
    return FileResponse(
        DATA_LOG.path_for(kind),
        media_type="text/csv",
        filename=CSV_FILENAMES[kind],
    )


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(process_chat_turn(request, queue))

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, ensure_ascii=True)}\n\n"
                if event["type"] == "done":
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/cases/{case_id}")
async def track_case(case_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"CS-[A-F0-9]{16}", case_id):
        raise HTTPException(status_code=404, detail="No case was found with that reference.")
    case = get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="No case was found with that reference.")
    return {"case": case, "audit": get_audit_log(case_id)}


@app.post("/api/demo/tickets/random")
async def generate_demo_ticket(request: DemoTicketRequest) -> dict[str, Any]:
    first_name = request.first_name.strip()
    last_name = request.last_name.strip()
    if not first_name or not last_name:
        raise HTTPException(status_code=422, detail="Enter both first and last name.")
    valid_name = lambda value: value[0].isalpha() and all(
        character.isalpha() or character in " '-" for character in value
    )
    if not valid_name(first_name) or not valid_name(last_name):
        raise HTTPException(status_code=422, detail="Names may contain letters, spaces, apostrophes, and hyphens.")
    return {"tickets": create_random_demo_booking(first_name, last_name)}


@app.get("/api/audit")
async def audit(case_id: str) -> dict[str, list[dict[str, Any]]]:
    if not re.fullmatch(r"CS-[A-F0-9]{16}", case_id):
        raise HTTPException(status_code=404, detail="No case was found with that reference.")
    if not get_case(case_id):
        raise HTTPException(status_code=404, detail="No case was found with that reference.")
    return {"events": get_audit_log(case_id)}


async def create_handoff(
    case: dict[str, Any],
    *,
    queue: str,
    summary: str,
    response: str,
    progress: Callable[[str, str, str], Awaitable[None]] | None,
) -> dict[str, Any]:
    case_id = case["case_id"]
    if progress:
        message = f"Creating a complete handoff for {queue.replace('_', ' ')}…"
        if OPENAI_API_KEY and queue != "sensitive_case_support":
            message = f"Using {OPENAI_MODEL} to prepare the specialist handoff for {queue.replace('_', ' ')}…"
        await progress("Escalation Agent", message)
    if OPENAI_API_KEY and queue != "sensitive_case_support":
        escalation_result = await run_model_agent(
            "escalation",
            (
                "You are the airline escalation agent. Create a concise handoff note for a human "
                "specialist using only supplied case facts. Preserve the reason for escalation, "
                "the assigned queue, known booking/flight facts, and whether an action was taken. "
                "Do not invent facts, promise outcomes, or change routing. Return JSON with one "
                "string key, handoff_note, under 700 characters."
            ),
            {
                "queue": queue,
                "case_id": case_id,
                "intent": case.get("intent"),
                "transaction_status": case.get("transaction_status", "not_started"),
                "booking": case.get("booking"),
                "verified_context_summary": case.get("context_summary"),
                "case_summary": summary,
                "action_taken": case.get("transaction_status") in {"submitted", "resolved"},
            },
        )
        handoff_note = required_model_text(
            "escalation", escalation_result, "handoff_note", 700
        )
        summary = f"{summary}\n\nAI-generated handoff note: {handoff_note}"
        record_audit(
            case_id=case_id,
            actor="escalation-agent",
            action="summarize_handoff",
            outcome="success",
            details={"model": OPENAI_MODEL, "queue": queue},
        )
    if SIMULATED_SYSTEM_DELAY_SECONDS:
        await asyncio.sleep(SIMULATED_SYSTEM_DELAY_SECONDS)
    handoff = await call_stage_mcp_tool(
        "create_handoff_case",
        {
            "case_id": case_id,
            "queue": queue,
            "summary": summary,
            "booking_reference": (case.get("booking") or {}).get("booking_reference"),
        },
        stage="specialist_handoff",
        intent=case.get("intent", "general"),
        case_id=case_id,
        request_text="" if queue == "sensitive_case_support" else summary,
        context={"queue": queue, "transaction_status": case.get("transaction_status")},
        allow_model=queue != "sensitive_case_support",
    )
    case["status"] = "escalated"
    case["transaction_status"] = case.get("transaction_status", "not_started")
    case["escalation"] = {"queue": queue, "summary": summary}
    case["handoff"] = handoff
    case["response"] = response
    case["updated_at"] = datetime.now(timezone.utc).isoformat()
    record_audit(
        case_id=case_id,
        actor="escalation-agent",
        action="route_case",
        outcome="submitted",
        details={"queue": queue, "handoff_id": handoff.get("handoff_id")},
    )
    save_case(case)
    return {"case": case, "audit": get_audit_log(case_id)}


async def execute_resolution_tool(
    intent: str,
    booking_reference: str,
    case_id: str,
    description: str,
    booking: dict[str, Any],
    authorization_token: str,
) -> dict[str, Any]:
    if intent == "refund":
        return await call_stage_mcp_tool(
            "issue_refund",
            {
                "booking_reference": booking_reference,
                "amount": booking["payment_amount"],
                "case_id": case_id,
                "reason": "airline_cancelled_flight",
                "authorization_token": authorization_token,
            },
            stage="confirmed_resolution",
            intent=intent,
            case_id=case_id,
            request_text=description,
            context={"booking_reference": booking_reference},
            eligible_names={
                name for name, intents in ACTION_TOOL_INTENT_MAP.items() if intent in intents
            },
        )
    if intent == "delay_support":
        return await call_stage_mcp_tool(
            "issue_lounge_access_pass",
            {
                "booking_reference": booking_reference,
                "case_id": case_id,
                "reason": "delay_over_180_minutes",
                "authorization_token": authorization_token,
            },
            stage="confirmed_resolution",
            intent=intent,
            case_id=case_id,
            request_text=description,
            context={"booking_reference": booking_reference},
            eligible_names={
                name for name, intents in ACTION_TOOL_INTENT_MAP.items() if intent in intents
            },
        )
    return await call_stage_mcp_tool(
        "open_baggage_case",
        {
            "booking_reference": booking_reference,
            "case_id": case_id,
            "description": description,
            "authorization_token": authorization_token,
        },
        stage="confirmed_resolution",
        intent=intent,
        case_id=case_id,
        request_text=description,
        context={"booking_reference": booking_reference},
        eligible_names={
            name for name, intents in ACTION_TOOL_INTENT_MAP.items() if intent in intents
        },
    )


@app.post("/api/cases")
async def direct_case_action_disabled(_: CaseRequest) -> None:
    raise HTTPException(
        status_code=409,
        detail="Cases must use the verified conversational workflow. No direct action was taken.",
    )


async def create_case(
    request: CaseRequest,
    *,
    case_id: str | None = None,
    progress: Callable[[str, str, str], Awaitable[None]] | None = None,
    intent_override: str | None = None,
    verification_token: str | None = None,
    customer_confirmed: bool = False,
) -> dict[str, Any]:
    case_id = case_id or new_case_id()
    if not verification_token:
        raise HTTPException(status_code=403, detail="Identity verification is required before case investigation.")
    if progress and not intent_override:
        await progress("Customer Intent Agent", "Classifying the customer request…")
    intent, policy_key = (
        classify_intent_from_model(intent_override, request.message)
        if intent_override
        else classify_intent(request.message)
    )
    booking_reference = find_booking_reference(request)
    sensitive_request = is_sensitive_request(request.message)
    existing_case = get_case(case_id)
    base_case: dict[str, Any] = {
        "case_id": case_id,
        "channel": request.channel,
        "message": request.message,
        "intent": intent,
        "status": "investigating",
        "transaction_status": "not_started",
        "created_at": (
            existing_case["created_at"]
            if existing_case
            else datetime.now(timezone.utc).isoformat()
        ),
        "customer": None,
        "booking": None,
        "policy": None,
        "resolution": None,
        "escalation": None,
    }

    if progress:
        await progress("Customer Context Agent", "Retrieving booking, passenger, and loyalty context through MCP…")
    if SIMULATED_SYSTEM_DELAY_SECONDS:
        await asyncio.sleep(SIMULATED_SYSTEM_DELAY_SECONDS)
    context = await call_stage_mcp_tool(
        "get_customer_context",
        {
            "booking_reference": booking_reference,
            "case_id": case_id,
            "verification_token": verification_token,
        },
        stage="verified_customer_context",
        intent=intent,
        case_id=case_id,
        request_text=request.message,
        context={"booking_reference": booking_reference},
    )
    if not context.get("found"):
        return await create_handoff(
            base_case,
            queue="booking_support",
            summary=f"Unable to retrieve verified booking {booking_reference}. Customer asked: {request.message}",
            response="I could not retrieve the verified booking. No automated action was taken; a specialist will help.",
            progress=progress,
        )

    passenger = context["passenger"]
    booking = context["booking"]
    base_case["customer"] = passenger
    base_case["booking"] = booking
    if OPENAI_API_KEY:
        if progress:
            await progress(
                "Customer Context Agent",
                f"Using {OPENAI_MODEL} to synthesize the verified booking context…",
            )
        context_result = await run_model_agent(
            "context",
            (
                "You are the airline customer-context agent. Summarize only the supplied, "
                "verified mock booking facts for the next workflow stage. Return JSON with one "
                "string key, summary. Do not infer policy eligibility, recommend an action, or "
                "add facts not present in the input. Keep the summary under 500 characters."
            ),
            {
                "booking_reference": booking_reference,
                "passenger": passenger,
                "booking": booking,
            },
        )
        base_case["context_summary"] = required_model_text(
            "context", context_result, "summary", 500
        )
        record_audit(
            case_id=case_id,
            actor="customer-context-agent",
            action="summarize_verified_context",
            outcome="success",
            details={"model": OPENAI_MODEL, "summary": base_case["context_summary"]},
        )
    selected_read_tool = None
    tool_selection_source = None
    if any(intent in supported for supported in READ_TOOL_INTENT_MAP.values()):
        if progress:
            await progress(
                "Customer Intent Agent",
                "Discovering available MCP read tools and selecting the best match for this verified request…",
            )
        selected_read_tool, tool_selection_source = await select_read_mcp_tool(
            intent=intent,
            request_text=request.message,
            booking=booking,
            case_id=case_id,
        )
        record_audit(
            case_id=case_id,
            actor="customer-intent-agent",
            action="select_mcp_tool",
            outcome="selected",
            details={
                "tool_name": selected_read_tool,
                "selection_source": tool_selection_source,
                "classified_intent": intent,
            },
        )
    if selected_read_tool == "get_flight_service_info":
        service = "boarding_pass" if intent == "boarding_pass" else "flight_info"
        if progress:
            await progress(
                "Customer Context Agent",
                (
                    "Retrieving the verified boarding pass from the mock passenger system…"
                    if service == "boarding_pass"
                    else "Checking the verified flight, terminal, and gate details…"
                ),
            )
        service_result = await call_mcp_tool(
            "get_flight_service_info",
            {
                "booking_reference": booking_reference,
                "service": service,
                "case_id": case_id,
                "verification_token": verification_token,
            },
        )
        if not service_result.get("found"):
            return await create_handoff(
                base_case,
                queue="flight_service_support",
                summary=f"Could not retrieve {service} information for booking {booking_reference}.",
                response="I could not retrieve those flight details. No change was made; a service specialist can help.",
                progress=progress,
            )
        base_case["service_info"] = service_result
        base_case["status"] = "resolved"
        base_case["transaction_status"] = "not_applicable"
        if service == "boarding_pass":
            if service_result["boarding_pass_status"] == "available":
                base_case["response"] = (
                    f"Your demo boarding pass is available. Boarding pass reference "
                    f"{service_result['boarding_pass_reference']}; flight {service_result['flight']} "
                    f"departs {service_result['origin_airport']} at {service_result['departure_time']} "
                    f"from Terminal {service_result['terminal']}, Gate {service_result['gate']}. "
                    "This is a simulated boarding pass, not valid for travel."
                )
            else:
                base_case["response"] = (
                    f"A boarding pass is not available because flight {service_result['flight']} "
                    f"is {service_result['boarding_pass_status']}. Check the airline's current "
                    "booking details; this demo does not issue a travel document."
                )
        else:
            base_case["response"] = (
                f"Flight {service_result['flight']} is {service_result['flight_status']}. "
                f"Itinerary: {service_result['route']}, {service_result['date']} at "
                f"{service_result['departure_time']}; Terminal {service_result['terminal']}, "
                f"Gate {service_result['gate']}. Flight information is simulated."
            )
        base_case["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_case(base_case)
        return {"case": base_case, "audit": get_audit_log(case_id)}

    if selected_read_tool == "get_refund_status":
        if progress:
            await progress("Customer Context Agent", "Checking the verified refund and payment status…")
        refund_status = await call_mcp_tool(
            "get_refund_status",
            {
                "booking_reference": booking_reference,
                "case_id": case_id,
                "verification_token": verification_token,
            },
        )
        if not refund_status.get("found"):
            return await create_handoff(
                base_case,
                queue="refund_support",
                summary=f"Could not retrieve refund status for booking {booking_reference}.",
                response="I could not retrieve refund information. No payment action was taken; a specialist can help.",
                progress=progress,
            )
        base_case["service_info"] = refund_status
        base_case["status"] = "resolved"
        base_case["transaction_status"] = refund_status.get("bank_status", "not_started")
        if refund_status.get("airline_status") == "issued":
            base_case["response"] = (
                f"The airline issued your refund ({refund_status['currency']} "
                f"{refund_status['amount']:.2f}; reference {refund_status['action_id']}). "
                "Bank processing is pending because this demo is not connected to bank systems."
            )
        else:
            base_case["response"] = (
                "No refund has been issued for this booking yet. I did not submit a payment action."
            )
        base_case["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_case(base_case)
        return {"case": base_case, "audit": get_audit_log(case_id)}

    if intent == "general" or (intent == "rebooking"):
        queue_name = "flight_changes" if intent == "rebooking" else "customer_care"
        return await create_handoff(
            base_case,
            queue=queue_name,
            summary=(
                f"{passenger['name']} ({passenger['loyalty_tier']}). Booking {booking_reference}, "
                f"flight {booking['flight']}. Request: {request.message}"
            ),
            response="I’ve prepared your verified booking context for a service specialist, who can help with this request.",
            progress=progress,
        )

    if progress:
        await progress("Policy Decision Agent", "Looking up the applicable policy and checking eligibility…")
    if SIMULATED_SYSTEM_DELAY_SECONDS:
        await asyncio.sleep(SIMULATED_SYSTEM_DELAY_SECONDS)
    policy = await call_stage_mcp_tool(
        "get_applicable_policy",
        {
            "policy_key": policy_key,
            "booking_reference": booking_reference,
            "case_id": case_id,
        },
        stage="policy_lookup",
        intent=intent,
        case_id=case_id,
        request_text=request.message,
        context={"booking_reference": booking_reference},
    )
    if not policy.get("found"):
        return await create_handoff(
            base_case,
            queue="policy_support",
            summary=f"No policy record was returned for {intent}, booking {booking_reference}. Request: {request.message}",
            response="I could not verify the applicable policy, so no action was taken. I’ve sent the case to a policy specialist.",
            progress=progress,
        )
    base_case["policy"] = policy
    if intent == "refund" and policy_key == "involuntary_refund":
        eligible = booking["status"] == "cancelled_by_airline"
    elif intent == "refund":
        eligible = False
    elif intent == "delay_support":
        eligible = booking["delay_minutes"] >= 180
    else:
        eligible = intent == "baggage"

    policy_explanation = ""
    if OPENAI_API_KEY:
        if progress:
            await progress(
                "Policy Decision Agent",
                f"Using {OPENAI_MODEL} to explain the deterministic policy result…",
            )
        policy_result = await run_model_agent(
            "policy",
            (
                "You are the airline policy explanation agent. Deterministic policy checks "
                "have already decided eligibility. Restate the supplied policy rule and supplied "
                "decision in customer-friendly language, using only the provided facts. Do not "
                "change eligibility, calculate a different amount, or promise compensation. "
                "Return JSON with one string key, explanation, under 500 characters."
            ),
            {
                "decision": "eligible" if eligible else "not_eligible",
                "policy_name": policy.get("name"),
                "policy_reference": policy.get("reference"),
                "policy_version": policy.get("version"),
                "policy_rule": policy.get("rule"),
                "verified_context_summary": base_case.get("context_summary"),
                "intent": intent,
                "booking_status": booking.get("status"),
                "delay_minutes": booking.get("delay_minutes"),
            },
        )
        policy_explanation = required_model_text(
            "policy", policy_result, "explanation", 500
        )
        record_audit(
            case_id=case_id,
            actor="policy-decision-agent",
            action="explain_policy_decision",
            outcome="success",
            details={"model": OPENAI_MODEL, "eligible": eligible},
        )

    record_audit(
        case_id=case_id,
        actor="policy-decision-agent",
        action="evaluate_eligibility",
        outcome="eligible" if eligible else "not_eligible",
        details={
            "intent": intent,
            "booking_reference": booking_reference,
            "policy_key": policy_key,
            "policy_reference": policy.get("reference"),
            "policy_version": policy.get("version"),
        },
    )
    if not eligible:
        return await create_handoff(
            base_case,
            queue="refund_review" if intent == "refund" else "disruption_support",
            summary=(
                f"Policy exception review for {passenger['name']}; booking {booking_reference}, "
                f"flight {booking['flight']}. Policy {policy['reference']} v{policy['version']}: "
                f"{policy['name']}. Request: {request.message}"
            ),
            response=(
                f"{policy['rule']} {policy_explanation} No automated action was taken. I’ve sent your case to a service specialist "
                "to review any exceptions."
            ),
            progress=progress,
        )

    action_details = {
        "refund": {
            "action": "refund",
            "title": "Refund to the original payment method",
            "amount": booking["payment_amount"],
            "currency": booking["currency"],
            "policy_statement": policy["rule"],
        },
        "delay_support": {
            "action": "lounge_access",
            "title": "Simulated lounge access pass",
            "policy_statement": policy["rule"],
        },
        "baggage": {
            "action": "baggage_case",
            "title": "Baggage tracing request",
            "policy_statement": policy["rule"],
        },
    }[intent]
    base_case["status"] = "awaiting_confirmation"
    base_case["transaction_status"] = "not_started"
    base_case["proposed_resolution"] = action_details
    base_case["response"] = (
        f"{policy['rule']} {policy_explanation} I can submit: {action_details['title']}"
        + (
            f" for {action_details['currency']} {action_details['amount']:.2f}"
            if "amount" in action_details
            else ""
        )
        + ". No action has been taken yet. Reply yes to confirm or no to cancel."
    )
    base_case["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not customer_confirmed:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="present_resolution_option",
            outcome="awaiting_customer_confirmation",
            details={
                "action": action_details["action"],
                "amount": action_details.get("amount"),
                "currency": action_details.get("currency"),
                "customer_confirmation_required": True,
                "policy_reference": policy["reference"],
                "policy_version": policy["version"],
            },
        )
        save_case(base_case)
        return {"case": base_case, "audit": get_audit_log(case_id)}

    if OPENAI_API_KEY:
        if progress:
            await progress(
                "Resolution Agent",
                f"Using {OPENAI_MODEL} to prepare the already-confirmed action for its controlled tool…",
            )
        resolution_result = await run_model_agent(
            "resolution",
            (
                "You are the airline resolution agent preparing a confirmed action for execution. "
                "Return JSON with one string key, execution_note. State only the requested action "
                "and supplied amount/currency when present. Do not authorize, submit, claim success, "
                "or change the action. The controlled MCP tool performs execution after this step. "
                "Keep the note under 300 characters."
            ),
            {
                "customer_confirmed": True,
                "action": action_details["action"],
                "title": action_details["title"],
                "amount": action_details.get("amount"),
                "currency": action_details.get("currency"),
                "booking_reference": booking_reference,
                "policy_reference": policy.get("reference"),
                "verified_context_summary": base_case.get("context_summary"),
                "policy_explanation": policy_explanation,
            },
        )
        base_case["resolution_agent_note"] = required_model_text(
            "resolution", resolution_result, "execution_note", 300
        )
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="prepare_confirmed_resolution",
            outcome="ready_for_controlled_tool",
            details={
                "model": OPENAI_MODEL,
                "action": action_details["action"],
                "customer_confirmed": True,
            },
        )

    if progress:
        await progress("Resolution Agent", "Obtaining one-time authorization from the explicit customer confirmation…")
    authorization = await call_stage_mcp_tool(
        "authorize_customer_resolution",
        {
            "case_id": case_id,
            "booking_reference": booking_reference,
            "action": action_details["action"],
            "customer_confirmed": True,
        },
        stage="post_confirmation_authorization",
        intent=intent,
        case_id=case_id,
        request_text=request.message,
        context={
            "booking_reference": booking_reference,
            "customer_confirmed": True,
            "action": action_details["action"],
        },
    )
    if not authorization.get("authorized"):
        return await create_handoff(
            base_case,
            queue="resolution_support",
            summary=f"Action authorization failed for booking {booking_reference}. No transaction was submitted.",
            response="I could not safely authorize that action. Nothing was submitted; a specialist will review the case.",
            progress=progress,
        )
    if SIMULATED_SYSTEM_DELAY_SECONDS:
        await asyncio.sleep(SIMULATED_SYSTEM_DELAY_SECONDS)

    if progress:
        action_message = {
            "refund": "Submitting the confirmed refund through mock payments…",
            "delay_support": "Issuing the confirmed simulated lounge access pass…",
            "baggage": "Submitting the confirmed baggage trace through mock case management…",
        }[intent]
        await progress("Resolution Agent", action_message)
    try:
        resolution = await execute_resolution_tool(
            intent,
            booking_reference,
            case_id,
            request.message,
            booking,
            authorization["authorization_token"],
        )
    except (HTTPException, httpx.HTTPError, TimeoutError, OSError) as exc:
        logger.exception("Resolution tool outcome is uncertain for case %s", case_id)
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="resolution_tool_outcome_uncertain",
            outcome="unknown",
            details={
                "action": action_details["action"],
                "booking_reference": booking_reference,
                "automatic_retry": False,
                "error_type": type(exc).__name__,
            },
        )
        try:
            status_result = await call_stage_mcp_tool(
                "get_resolution_status",
                {"case_id": case_id, "action": action_details["action"]},
                stage="uncertain_outcome_reconciliation",
                intent=intent,
                case_id=case_id,
                request_text="Check the existing action status; do not retry it.",
                context={"action": action_details["action"]},
            )
        except (HTTPException, httpx.HTTPError, TimeoutError, OSError) as status_exc:
            logger.exception("Could not reconcile resolution status for case %s", case_id)
            status_result = {"found": False}
            reconciliation_error = status_exc
        else:
            reconciliation_error = None
        if not status_result.get("found"):
            base_case["status"] = "escalated"
            base_case["transaction_status"] = "unknown"
            base_case["response"] = (
                "The system did not return a reliable transaction result. I did not retry the action. "
                "A specialist must reconcile the payment/action status before any further attempt."
            )
            base_case["resolution"] = {"success": False, "transaction_status": "unknown"}
            save_case(base_case)
            try:
                return await create_handoff(
                    base_case,
                    queue="transaction_reconciliation",
                    summary=(
                        f"Transaction result is unknown for {action_details['action']} on booking "
                        f"{booking_reference}. No automatic retry was made. Check the downstream system "
                        f"before taking any further action. Case: {case_id}. "
                        + (
                            f"Status service error: {type(reconciliation_error).__name__}."
                            if reconciliation_error
                            else "No transaction record was found."
                        )
                    ),
                    response=base_case["response"],
                    progress=progress,
                )
            except (HTTPException, httpx.HTTPError, TimeoutError, OSError):
                logger.exception("Could not persist transaction reconciliation handoff for case %s", case_id)
                return {"case": base_case, "audit": get_audit_log(case_id)}
        resolution = status_result["result"]
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="reconcile_uncertain_outcome",
            outcome=resolution.get("transaction_status", "unknown"),
            details={
                "action": action_details["action"],
                "booking_reference": booking_reference,
                "automatic_retry": False,
                "status_source": "mock_transaction_ledger",
            },
        )
    base_case["resolution"] = resolution
    base_case["updated_at"] = datetime.now(timezone.utc).isoformat()
    if resolution.get("success"):
        base_case["status"] = "submitted"
        base_case["transaction_status"] = resolution.get("transaction_status", "submitted")
    else:
        base_case["status"] = "escalated"
        base_case["transaction_status"] = "failed"
    if resolution.get("success"):
        timing = (
            f" The payment system estimates {resolution['estimated_processing_time']}."
            if resolution.get("estimated_processing_time")
            else ""
        )
        if action_details["action"] == "refund":
            base_case["service_info"] = {
                "airline_status": resolution.get("airline_status", "initiated"),
                "bank_status": resolution.get("bank_status", "pending_approval"),
                "refund_destination": resolution.get("refund_destination", "original payment source"),
                "action_id": resolution.get("action_id"),
                "amount": resolution.get("amount"),
                "currency": resolution.get("currency"),
            }
            base_case["response"] = (
                f"The airline initiated the refund ({resolution['currency']} "
                f"{resolution['amount']:.2f}; reference {resolution['action_id']}). "
                "It is returning to the original payment source. Bank approval is pending; this demo "
                "is not connected to bank systems and cannot confirm settlement."
                f"{timing}"
            )
        else:
            base_case["response"] = f"{resolution['message']}{timing} Status: submitted, not yet completed."
    else:
        summary = (
            f"The confirmed action failed for booking {booking_reference}. "
            f"Tool response: {resolution.get('message', 'No reason returned.')}"
        )
        return await create_handoff(
            base_case,
            queue="resolution_support",
            summary=summary,
            response="The action was not submitted successfully. No completion is being claimed; a specialist will review the failure.",
            progress=progress,
        )

    if selected_read_tool != "get_applicable_policy":
        raise HTTPException(
            status_code=502,
            detail="The model did not select a valid MCP tool for the policy workflow.",
        )
    save_case(base_case)
    return {"case": base_case, "audit": get_audit_log(case_id)}
