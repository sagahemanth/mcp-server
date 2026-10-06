import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import domain
from fastapi import HTTPException

from airline_mcp import (
    authorize_customer_resolution,
    get_flight_service_info,
    get_refund_status,
    issue_refund,
    issue_lounge_access_pass,
    open_baggage_case,
)
from data_log import CsvDataLog
from domain import BOOKINGS, PASSENGERS, POLICIES, get_cases, is_verification_valid, verify_passenger
from main import (
    app,
    CaseRequest,
    ChatRequest,
    DemoTicketRequest,
    analyze_with_model,
    classify_intent,
    confirmation_choice,
    create_handoff,
    create_case,
    direct_case_action_disabled,
    execute_resolution_tool,
    extract_last_name,
    find_booking_reference,
    is_sensitive_request,
    model_configuration,
    needs_refund_cause_clarification,
    process_chat_turn,
    generate_demo_ticket,
    run_model_agent,
    track_case,
    select_read_mcp_tool,
    select_stage_mcp_tool,
    validate_selected_mcp_tool,
)


def events_from(queue):
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


class WorkflowRuleTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch.object(domain, "DATA_LOG", CsvDataLog(temp_dir.name))
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_csv_history_survives_reopen_and_excludes_passenger_identifiers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CsvDataLog(temp_dir)
            store.log_flights([{
                "flight": "AR123",
                "origin_airport": "JFK",
                "arrival_airport": "ORD",
                "date": "2026-10-08",
                "flight_status": "Scheduled",
                "fare_type": "standard",
                "currency": "USD",
                "boarding_pass_status": "available",
                "passenger_name": "Private Passenger",
                "booking_reference": "PNRSECRET",
            }])
            store.log_case({
                "case_id": "CS-PRIVATECASEIDENTIFIER",
                "intent": "refund",
                "status": "submitted",
                "channel": "web_chat",
                "message": "Private customer message",
            })
            store.log_action({
                "audit_id": "ACTIONREF123",
                "case_id": "CS-PRIVATECASEIDENTIFIER",
                "actor": "resolution-agent",
                "action": "issue_refund",
                "outcome": "submitted",
                "details": {"booking_reference": "PNRSECRET"},
            })

            reopened = CsvDataLog(temp_dir)
            dashboard = reopened.dashboard()
            self.assertEqual(dashboard["counts"], {"flights": 1, "cases": 1, "actions": 1})
            self.assertEqual(dashboard["flights"][0]["flight"], "AR123")
            stored_csv = "\n".join(path.read_text(encoding="utf-8") for path in reopened.directory.glob("*.csv"))
            for private_value in (
                "Private Passenger",
                "PNRSECRET",
                "CS-PRIVATECASEIDENTIFIER",
                "Private customer message",
            ):
                self.assertNotIn(private_value, stored_csv)

    def test_domain_case_and_audit_writes_are_persisted_without_raw_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CsvDataLog(temp_dir)
            with patch.object(domain, "DATA_LOG", store):
                domain.record_audit(
                    case_id="CS-PRIVATECASEIDENTIFIER",
                    actor="policy-decision-agent",
                    action="evaluate_eligibility",
                    outcome="eligible",
                    details={"booking_reference": "PNRSECRET", "message": "Private text"},
                )
                domain.save_case({
                    "case_id": "CS-PRIVATECASEIDENTIFIER",
                    "intent": "refund",
                    "status": "submitted",
                    "channel": "web_chat",
                    "transaction_status": "pending_approval",
                    "message": "Private text",
                    "customer": {"name": "Private Passenger"},
                })
            history = CsvDataLog(temp_dir).dashboard()
            self.assertEqual(history["counts"]["actions"], 1)
            self.assertEqual(history["counts"]["cases"], 1)
            stored_csv = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path(temp_dir).glob("*.csv")
            )
            for private_value in (
                "Private Passenger",
                "PNRSECRET",
                "CS-PRIVATECASEIDENTIFIER",
                "Private text",
            ):
                self.assertNotIn(private_value, stored_csv)

    def test_classifies_supported_request_types(self):
        self.assertEqual(classify_intent("Please refund my cancelled flight")[0], "refund")
        self.assertEqual(classify_intent("My bag is missing")[0], "baggage")
        self.assertEqual(classify_intent("My flight is delayed")[0], "delay_support")
        self.assertEqual(classify_intent("Please rebook my flight")[0], "rebooking")
        self.assertEqual(classify_intent("I have a question")[0], "general")
        self.assertEqual(classify_intent("Please send me my boarding pass")[0], "boarding_pass")
        self.assertEqual(classify_intent("Which terminal and gate?")[0], "flight_info")
        self.assertEqual(classify_intent("Can you check my refund status?")[0], "refund_status")

    def test_extracts_booking_reference_from_request(self):
        self.assertEqual(
            find_booking_reference(CaseRequest(message="Please help with booking PNR482")),
            "PNR482",
        )
        self.assertEqual(
            find_booking_reference(CaseRequest(message="Booking PNRAB12CD34 please")),
            "PNRAB12CD34",
        )
        flight_booking = {
            "booking_reference": "PNR-FLIGHT-LOOKUP",
            "flight": "AR987",
            "customer_id": "TEMP",
        }
        BOOKINGS[flight_booking["booking_reference"]] = flight_booking
        self.addCleanup(BOOKINGS.pop, flight_booking["booking_reference"], None)
        self.assertEqual(
            find_booking_reference(CaseRequest(message="Please check flight AR987")),
            flight_booking["booking_reference"],
        )
        self.assertIsNone(find_booking_reference(CaseRequest(message="Please help with my trip")))

    def test_extracts_identity_response_without_overmatching(self):
        self.assertEqual(extract_last_name("Rivera"), "Rivera")
        self.assertEqual(extract_last_name("My last name is Patel."), "Patel")
        self.assertIsNone(extract_last_name("My name is Casey Patel and my flight was delayed"))

    def test_parses_explicit_confirmation_and_decline(self):
        self.assertTrue(confirmation_choice("Yes, please proceed."))
        self.assertFalse(confirmation_choice("No, cancel this request."))
        self.assertIsNone(confirmation_choice("Tell me more"))

    def test_asks_for_cancellation_type_before_selecting_refund_policy(self):
        self.assertTrue(needs_refund_cause_clarification("refund", "I need a refund"))
        self.assertFalse(
            needs_refund_cause_clarification("refund", "The airline cancelled my flight")
        )
        self.assertFalse(
            needs_refund_cause_clarification("refund", "I want to cancel my own booking")
        )

    def test_marks_sensitive_requests_for_handoff(self):
        self.assertTrue(is_sensitive_request("I need help after an injury on board"))
        self.assertTrue(is_sensitive_request("I want to report a data breach"))
        self.assertTrue(is_sensitive_request("sexy"))
        self.assertTrue(is_sensitive_request("sexual harassment"))
        self.assertTrue(is_sensitive_request("u r an idiot"))
        self.assertTrue(is_sensitive_request("fuck"))
        self.assertTrue(is_sensitive_request("You are fucking rude"))
        self.assertTrue(is_sensitive_request("idiots"))
        self.assertTrue(is_sensitive_request("f.u.c.k"))
        self.assertTrue(is_sensitive_request("This is bullshit"))
        self.assertFalse(is_sensitive_request("The flight leaves Sussex"))
        self.assertFalse(is_sensitive_request("I need assistance"))
        self.assertFalse(is_sensitive_request("My flight is delayed"))

    def test_model_tool_selection_is_allowlisted_and_intent_scoped(self):
        self.assertEqual(
            validate_selected_mcp_tool(
                "get_refund_status",
                intent="refund_status",
                available_names={"get_refund_status"},
            ),
            "get_refund_status",
        )
        with self.assertRaises(HTTPException):
            validate_selected_mcp_tool(
                "issue_refund",
                intent="refund",
                available_names={"issue_refund"},
            )
        with self.assertRaises(HTTPException):
            validate_selected_mcp_tool(
                "get_refund_status",
                intent="boarding_pass",
                available_names={"get_refund_status"},
            )

    def test_verification_token_is_scoped_to_case_and_booking(self):
        verified = verify_passenger("PNR482", "Rivera", "CS-VERIFY-TEST")
        self.assertTrue(verified["verified"])
        self.assertTrue(
            is_verification_valid(verified["verification_token"], "CS-VERIFY-TEST", "PNR482")
        )
        self.assertFalse(
            is_verification_valid(verified["verification_token"], "CS-OTHER", "PNR482")
        )
        self.assertFalse(
            is_verification_valid(verified["verification_token"], "CS-VERIFY-TEST", "PNR739")
        )
        self.assertFalse(verify_passenger("PNR482", "Wrong", "CS-VERIFY-TEST")["verified"])

    def test_verified_service_reads_itinerary_and_reports_simulated_refund_state(self):
        case_id = "CS-SERVICE01"
        verification = verify_passenger("PNR615", "Patel", case_id)
        token = verification["verification_token"]
        boarding = get_flight_service_info(
            "PNR615", "boarding_pass", case_id, token
        )
        refund = get_refund_status("PNR615", case_id, token)
        self.assertTrue(boarding["found"])
        self.assertEqual(boarding["arrival_airport"], "MIA")
        self.assertTrue(boarding["boarding_pass_reference"].startswith("BP-"))
        self.assertTrue(boarding["qr_code_data_uri"].startswith("data:image/png;base64,"))
        self.assertGreater(
            len(base64.b64decode(boarding["qr_code_data_uri"].split(",", 1)[1])),
            100,
        )
        self.assertFalse(boarding["valid_for_travel"])
        self.assertEqual(refund["airline_status"], "not_issued")
        self.assertEqual(refund["bank_status"], "not_started")

    def test_missing_bag_trace_routes_to_destination_airport_demo_desk(self):
        case_id = "CS-BAGGAGE1"
        authorization = authorize_customer_resolution(
            case_id=case_id,
            booking_reference="PNR615",
            action="baggage_case",
            customer_confirmed=True,
        )
        result = open_baggage_case(
            "PNR615",
            case_id,
            "Checked bag did not arrive",
            authorization["authorization_token"],
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["airport_code"], "MIA")
        self.assertIn("MIA Baggage Service Desk", result["airport_desk"])
        self.assertIn("No real airport was contacted", result["message"])

    def test_payment_tool_requires_confirmation_and_is_idempotent(self):
        case_id = "CS-IDEMPOTENCY-TEST"
        unauthorized = issue_refund(
            booking_reference="PNR482",
            amount=BOOKINGS["PNR482"]["payment_amount"],
            case_id=case_id,
            reason="airline_cancelled_flight",
            authorization_token="not-authorized",
        )
        self.assertFalse(unauthorized["success"])

        auth = authorize_customer_resolution(
            case_id=case_id,
            booking_reference="PNR482",
            action="refund",
            customer_confirmed=True,
        )
        first = issue_refund(
            booking_reference="PNR482",
            amount=BOOKINGS["PNR482"]["payment_amount"],
            case_id=case_id,
            reason="airline_cancelled_flight",
            authorization_token=auth["authorization_token"],
        )
        self.assertEqual(first["transaction_status"], "submitted")

        replay_auth = authorize_customer_resolution(
            case_id=case_id,
            booking_reference="PNR482",
            action="refund",
            customer_confirmed=True,
        )
        replay = issue_refund(
            booking_reference="PNR482",
            amount=BOOKINGS["PNR482"]["payment_amount"],
            case_id=case_id,
            reason="airline_cancelled_flight",
            authorization_token=replay_auth["authorization_token"],
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["action_id"], first["action_id"])
        verification = verify_passenger("PNR482", "Rivera", case_id)
        refund_status = get_refund_status(
            "PNR482",
            case_id,
            verification["verification_token"],
        )
        self.assertEqual(refund_status["airline_status"], "initiated")
        self.assertEqual(refund_status["bank_status"], "pending_approval")

    def test_lounge_pass_requires_confirmation_and_meets_delay_threshold(self):
        case_id = "CS-LOUNGE-PASS-TEST"
        unauthorized = issue_lounge_access_pass(
            "PNR739",
            case_id,
            "delay_over_180_minutes",
            "not-authorized",
        )
        self.assertFalse(unauthorized["success"])

        authorization = authorize_customer_resolution(
            case_id=case_id,
            booking_reference="PNR739",
            action="lounge_access",
            customer_confirmed=True,
        )
        result = issue_lounge_access_pass(
            "PNR739",
            case_id,
            "delay_over_180_minutes",
            authorization["authorization_token"],
        )
        self.assertTrue(result["success"])
        self.assertRegex(result["lounge_pass_id"], r"^LA-[A-F0-9]{10}$")
        self.assertEqual(result["action_id"], result["lounge_pass_id"])
        self.assertIn("not valid for entry", result["message"])


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_log_patch = patch.object(domain, "DATA_LOG", CsvDataLog(self.temp_dir.name))
        self.data_log_patch.start()
        self.addCleanup(self.data_log_patch.stop)
        self.delay_patch = patch("main.SIMULATED_SYSTEM_DELAY_SECONDS", 0)
        self.delay_patch.start()
        self.addCleanup(self.delay_patch.stop)
        self.api_key_patch = patch("main.OPENAI_API_KEY", "")
        self.api_key_patch.start()
        self.addCleanup(self.api_key_patch.stop)
        self.model_requirement_patch = patch("main.REQUIRE_CONFIGURED_MODEL", False)
        self.model_requirement_patch.start()
        self.addCleanup(self.model_requirement_patch.stop)

    async def process_turn(self, request, tool_handler):
        queue = asyncio.Queue()
        with patch("main.call_mcp_tool", new=AsyncMock(side_effect=tool_handler)) as mcp_tool:
            await process_chat_turn(request, queue)
        return events_from(queue), mcp_tool

    async def test_stage_tool_selection_uses_live_catalog_and_model_choice(self):
        catalog = [
            {"name": "get_refund_status", "description": "Refund status", "input_schema": {}},
            {"name": "get_applicable_policy", "description": "Policy details", "input_schema": {}},
        ]
        with patch("main.OPENAI_API_KEY", "configured-test-key"), patch(
            "main.discover_mcp_tools", new=AsyncMock(return_value=catalog)
        ) as discover, patch(
            "main.run_model_agent",
            new=AsyncMock(return_value={"tool_name": "get_refund_status"}),
        ) as model:
            selected, source = await select_stage_mcp_tool(
                stage="verified_read",
                intent="refund_status",
                fallback_name="get_refund_status",
                eligible_names={"get_refund_status", "get_applicable_policy"},
                request_text="Track my refund",
                case_id="CS-DYNAMIC-SELECT",
            )
        self.assertEqual(selected, "get_refund_status")
        self.assertEqual(source, "model")
        discover.assert_awaited_once_with({"get_refund_status", "get_applicable_policy"})
        model.assert_awaited_once()
        self.assertEqual(model.await_args.args[0], "tool-selection")

    async def test_stage_tool_selection_rejects_tool_outside_eligible_set(self):
        with patch("main.OPENAI_API_KEY", "configured-test-key"), patch(
            "main.discover_mcp_tools",
            new=AsyncMock(return_value=[
                {"name": "get_refund_status", "description": "Refund status", "input_schema": {}},
            ]),
        ), patch(
            "main.run_model_agent",
            new=AsyncMock(return_value={"tool_name": "issue_refund"}),
        ):
            with self.assertRaises(HTTPException):
                await select_stage_mcp_tool(
                    stage="verified_read",
                    intent="refund_status",
                    fallback_name="get_refund_status",
                    eligible_names={"get_refund_status"},
                    request_text="Track my refund",
                    case_id="CS-DYNAMIC-INVALID",
                )

    async def test_random_demo_ticket_is_linked_to_profile_and_flight_fixture(self):
        case_count = len(get_cases())
        result = await generate_demo_ticket(
            DemoTicketRequest(first_name="Jordan", last_name="O'Neil")
        )
        tickets = result["tickets"]
        self.assertEqual(len(tickets), 3)
        self.assertEqual(sum(ticket["status"] == "cancelled_by_airline" for ticket in tickets), 1)
        bookings = [BOOKINGS[ticket["booking_reference"]] for ticket in tickets]
        customer_ids = {booking["customer_id"] for booking in bookings}
        self.assertEqual(len(customer_ids), 1)
        customer_id = next(iter(customer_ids))
        passenger = PASSENGERS[customer_id]
        self.addCleanup(PASSENGERS.pop, customer_id, None)
        for ticket in tickets:
            self.addCleanup(BOOKINGS.pop, ticket["booking_reference"], None)
            self.assertEqual(ticket["passenger_name"], "Jordan O'Neil")
            self.assertTrue(ticket["origin_airport"])
            self.assertTrue(ticket["arrival_airport"])
            self.assertTrue(ticket["terminal"])
            self.assertTrue(ticket["gate"])
            self.assertTrue(ticket["boarding_pass_reference"])
            self.assertNotIn("scenario", ticket)
        self.assertEqual(passenger["last_name"], "O'Neil")
        self.assertEqual(set(passenger["bookings"]), {ticket["booking_reference"] for ticket in tickets})
        self.assertEqual(len(get_cases()), case_count)
        cancelled_ticket = next(
            ticket for ticket in tickets if ticket["status"] == "cancelled_by_airline"
        )
        verification = verify_passenger(
            cancelled_ticket["booking_reference"],
            "O'Neil",
            "CS-CANCELLED-BOARDING-TEST",
        )
        cancelled_pass = get_flight_service_info(
            cancelled_ticket["booking_reference"],
            "boarding_pass",
            "CS-CANCELLED-BOARDING-TEST",
            verification["verification_token"],
        )
        self.assertFalse(cancelled_pass["found"])

        for ticket in tickets:
            case_id = f"CS-{ticket['booking_reference']}"
            authorization = authorize_customer_resolution(
                case_id, ticket["booking_reference"], "refund", True
            )
            refund = issue_refund(
                booking_reference=ticket["booking_reference"],
                amount=BOOKINGS[ticket["booking_reference"]]["payment_amount"],
                case_id=case_id,
                reason="airline_cancelled_flight",
                authorization_token=authorization["authorization_token"],
            )
            self.assertEqual(refund["success"], ticket["status"] == "cancelled_by_airline")

    async def test_random_demo_ticket_rejects_non_name_characters(self):
        with self.assertRaises(HTTPException) as raised:
            await generate_demo_ticket(
                DemoTicketRequest(first_name="Jordan1", last_name="O'Neil")
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_delay_resolution_routes_to_lounge_access_mcp_action(self):
        expected = {
            "success": True,
            "transaction_status": "issued",
            "lounge_pass_id": "LA-TEST123456",
        }
        with patch("main.call_mcp_tool", new=AsyncMock(return_value=expected)) as tool:
            result = await execute_resolution_tool(
                "delay_support",
                "PNR739",
                "CS-LOUNGE-ROUTE-TEST",
                "Flight delayed",
                dict(BOOKINGS["PNR739"]),
                "confirmed-lounge-token",
            )
        self.assertEqual(result, expected)
        tool.assert_awaited_once()
        self.assertEqual(tool.await_args.args[0], "issue_lounge_access_pass")
        self.assertEqual(
            tool.await_args.args[1],
            {
                "booking_reference": "PNR739",
                "case_id": "CS-LOUNGE-ROUTE-TEST",
                "reason": "delay_over_180_minutes",
                "authorization_token": "confirmed-lounge-token",
            },
        )

    async def test_case_tracking_returns_current_case_and_audit(self):
        case_id = "CS-A1B2C3D4E5F60718"
        case = {"case_id": case_id, "status": "submitted"}
        audit_events = [{"case_id": case_id, "action": "issue_refund"}]
        with patch("main.get_case", return_value=case), patch(
            "main.get_audit_log", return_value=audit_events
        ):
            result = await track_case(case_id)
        self.assertEqual(result, {"case": case, "audit": audit_events})

    async def test_case_tracking_rejects_unknown_and_malformed_references(self):
        with self.assertRaises(HTTPException) as missing:
            await track_case("CS-A1B2C3D4E5F60718")
        self.assertEqual(missing.exception.status_code, 404)
        with self.assertRaises(HTTPException) as malformed:
            await track_case("anything")
        self.assertEqual(malformed.exception.status_code, 404)

    async def test_case_and_audit_endpoints_do_not_expose_unscoped_records(self):
        self.assertFalse(
            any(
                getattr(route, "path", None) == "/api/cases"
                and "GET" in getattr(route, "methods", set())
                for route in app.routes
            )
        )
        from main import audit

        with self.assertRaises(HTTPException) as missing_id:
            await audit("")
        self.assertEqual(missing_id.exception.status_code, 404)

    async def test_verified_boarding_pass_request_uses_booking_not_ticket_generation(self):
        booking = dict(BOOKINGS["PNR482"])
        passenger = {
            key: value for key, value in PASSENGERS["CUST1001"].items() if key != "last_name"
        }
        service_result = {
            "found": True,
            "boarding_pass_status": "available",
            "boarding_pass_reference": "BP-TEST",
            "passenger_name": "Jamie Rivera",
            "flight": booking["flight"],
            "departure_time": "08:00",
            "origin_airport": "JFK",
            "terminal": "4",
            "gate": "B12",
        }

        async def tools(name, _arguments):
            if name == "get_customer_context":
                return {"found": True, "booking": booking, "passenger": passenger}
            if name == "get_flight_service_info":
                return service_result
            self.fail(f"Unexpected MCP call: {name}")

        with patch("main.call_mcp_tool", new=AsyncMock(side_effect=tools)):
            result = await create_case(
                CaseRequest(message="Please send my boarding pass", booking_reference="PNR482"),
                case_id="CS-BOARDING1",
                intent_override="boarding_pass",
                verification_token="verified-demo-token",
            )
        self.assertEqual(result["case"]["status"], "resolved")
        self.assertEqual(result["case"]["service_info"]["boarding_pass_reference"], "BP-TEST")
        self.assertIn("not valid for travel", result["case"]["response"])

    async def test_verified_refund_tracking_reports_pending_bank_not_connected(self):
        case_id = "CS-REFUND01"
        booking = dict(BOOKINGS["PNR482"])
        booking["booking_reference"] = "PNR482"
        passenger = {
            key: value for key, value in PASSENGERS["CUST1001"].items() if key != "last_name"
        }

        async def tools(name, _arguments):
            if name == "get_customer_context":
                return {"found": True, "booking": booking, "passenger": passenger}
            if name == "get_refund_status":
                return {
                    "found": True,
                    "airline_status": "issued",
                    "bank_status": "pending",
                    "action_id": "RF-TEST",
                    "amount": 428.5,
                    "currency": "USD",
                }
            self.fail(f"Unexpected MCP call: {name}")

        with patch("main.call_mcp_tool", new=AsyncMock(side_effect=tools)):
            result = await create_case(
                CaseRequest(message="Please check my refund status", booking_reference="PNR482"),
                case_id=case_id,
                intent_override="refund_status",
                verification_token="verified-demo-token",
            )
        self.assertEqual(result["case"]["service_info"]["bank_status"], "pending")
        self.assertIn("bank processing is pending", result["case"]["response"].lower())
        self.assertIn("not connected", result["case"]["response"].lower())

    async def start_refund_conversation(self):
        first_events, _ = await self.process_turn(
            ChatRequest(message="My flight was cancelled by the airline. I want a refund."),
            AsyncMock(),
        )
        question = next(event for event in first_events if event["type"] == "question")
        self.assertIn("booking reference", question["message"].lower())
        conversation_id = question["conversation_id"]

        second_events, second_tool = await self.process_turn(
            ChatRequest(message="PNR482", conversation_id=conversation_id),
            AsyncMock(),
        )
        question = next(event for event in second_events if event["type"] == "question")
        self.assertIn("last name", question["message"].lower())
        second_tool.assert_not_awaited()
        return conversation_id

    async def test_workflow_verifies_investigates_offers_then_waits_for_confirmation(self):
        conversation_id = await self.start_refund_conversation()

        async def investigation_tools(name, arguments):
            if name == "verify_customer_identity":
                self.assertEqual(arguments["last_name"], "Rivera")
                return {"verified": True, "verification_token": "test-verification-token"}
            if name == "get_customer_context":
                self.assertEqual(arguments["verification_token"], "test-verification-token")
                return {
                    "found": True,
                    "booking": dict(BOOKINGS["PNR482"]),
                    "passenger": {
                        key: value
                        for key, value in PASSENGERS["CUST1001"].items()
                        if key != "last_name"
                    },
                }
            if name == "get_applicable_policy":
                return {"found": True, "key": "involuntary_refund", **POLICIES["involuntary_refund"]}
            self.fail(f"Unexpected MCP tool call during investigation: {name}")

        offer_events, investigation_tool = await self.process_turn(
            ChatRequest(message="Rivera", conversation_id=conversation_id),
            investigation_tools,
        )
        offer = next(event for event in offer_events if event["type"] == "offer")
        self.assertEqual(offer["payload"]["case"]["status"], "awaiting_confirmation")
        self.assertEqual(offer["payload"]["case"]["transaction_status"], "not_started")
        self.assertIn("USD 428.50", offer["message"])
        self.assertIn("No action has been taken", offer["message"])
        called_tools = [call.args[0] for call in investigation_tool.await_args_list]
        self.assertEqual(
            called_tools,
            ["verify_customer_identity", "get_customer_context", "get_applicable_policy"],
        )
        self.assertNotIn("issue_refund", called_tools)
        self.assertNotIn("authorize_customer_resolution", called_tools)

        async def confirmation_tools(name, arguments):
            if name == "get_customer_context":
                return {
                    "found": True,
                    "booking": dict(BOOKINGS["PNR482"]),
                    "passenger": {
                        key: value
                        for key, value in PASSENGERS["CUST1001"].items()
                        if key != "last_name"
                    },
                }
            if name == "get_applicable_policy":
                return {"found": True, "key": "involuntary_refund", **POLICIES["involuntary_refund"]}
            if name == "authorize_customer_resolution":
                self.assertTrue(arguments["customer_confirmed"])
                return {"authorized": True, "authorization_token": "one-time-action-token"}
            if name == "issue_refund":
                self.assertEqual(arguments["authorization_token"], "one-time-action-token")
                return {
                    "success": True,
                    "transaction_status": "submitted",
                    "action_id": "RF-TEST",
                    "amount": 428.50,
                    "currency": "USD",
                    "estimated_processing_time": "5-7 business days",
                    "message": "Your refund request has been submitted.",
                }
            self.fail(f"Unexpected MCP tool call after confirmation: {name}")

        final_events, _ = await self.process_turn(
            ChatRequest(message="Yes, please proceed.", conversation_id=conversation_id),
            confirmation_tools,
        )
        result = next(event["payload"] for event in final_events if event["type"] == "result")
        self.assertEqual(result["case"]["status"], "submitted")
        self.assertEqual(result["case"]["transaction_status"], "submitted")
        self.assertEqual(result["case"]["resolution"]["action_id"], "RF-TEST")
        self.assertIn("5-7 business days", result["case"]["response"])
        self.assertIn("Bank approval is pending", result["case"]["response"])

    async def test_declining_offer_never_authorizes_or_submits_action(self):
        conversation_id = await self.start_refund_conversation()

        async def investigation_tools(name, _arguments):
            if name == "verify_customer_identity":
                return {"verified": True, "verification_token": "verified"}
            if name == "get_customer_context":
                return {
                    "found": True,
                    "booking": dict(BOOKINGS["PNR482"]),
                    "passenger": {"name": "Jamie Rivera", "loyalty_tier": "Gold"},
                }
            if name == "get_applicable_policy":
                return {"found": True, "key": "involuntary_refund", **POLICIES["involuntary_refund"]}
            self.fail(name)

        offer_events, _ = await self.process_turn(
            ChatRequest(message="Rivera", conversation_id=conversation_id),
            investigation_tools,
        )
        self.assertTrue(any(event["type"] == "offer" for event in offer_events))
        events, tool = await self.process_turn(
            ChatRequest(message="No, cancel this request.", conversation_id=conversation_id),
            AsyncMock(),
        )
        result = next(event["payload"] for event in events if event["type"] == "result")
        self.assertEqual(result["case"]["status"], "cancelled")
        self.assertEqual(result["case"]["transaction_status"], "not_started")
        tool.assert_not_awaited()

    async def test_uncertain_tool_result_is_reconciled_without_retry(self):
        conversation_id = await self.start_refund_conversation()

        async def investigation_tools(name, _arguments):
            if name == "verify_customer_identity":
                return {"verified": True, "verification_token": "verified-reconcile"}
            if name == "get_customer_context":
                return {
                    "found": True,
                    "booking": dict(BOOKINGS["PNR482"]),
                    "passenger": {"name": "Jamie Rivera", "loyalty_tier": "Gold"},
                }
            if name == "get_applicable_policy":
                return {"found": True, "key": "involuntary_refund", **POLICIES["involuntary_refund"]}
            self.fail(name)

        offer_events, _ = await self.process_turn(
            ChatRequest(message="Rivera", conversation_id=conversation_id),
            investigation_tools,
        )
        self.assertTrue(any(event["type"] == "offer" for event in offer_events))

        async def status_tools(name, _arguments):
            if name == "get_customer_context":
                return {
                    "found": True,
                    "booking": dict(BOOKINGS["PNR482"]),
                    "passenger": {"name": "Jamie Rivera", "loyalty_tier": "Gold"},
                }
            if name == "get_applicable_policy":
                return {"found": True, "key": "involuntary_refund", **POLICIES["involuntary_refund"]}
            if name == "authorize_customer_resolution":
                return {"authorized": True, "authorization_token": "one-time-reconcile"}
            if name == "get_resolution_status":
                return {
                    "found": True,
                    "result": {
                        "success": True,
                        "transaction_status": "submitted",
                        "action_id": "RF-RECONCILED",
                        "amount": 428.50,
                        "currency": "USD",
                        "estimated_processing_time": "5-7 business days",
                        "message": "Your refund request has been submitted.",
                    },
                }
            self.fail(f"Unexpected status reconciliation tool: {name}")

        with patch("main.logger.exception"), patch(
            "main.execute_resolution_tool",
            new=AsyncMock(side_effect=HTTPException(
                status_code=502, detail="Simulated lost MCP response"
            )),
        ) as action, patch(
            "main.call_mcp_tool", new=AsyncMock(side_effect=status_tools)
        ) as tools:
            events = asyncio.Queue()
            await process_chat_turn(
                ChatRequest(message="Yes, proceed.", conversation_id=conversation_id),
                events,
            )
        result = next(
            event["payload"] for event in events_from(events) if event["type"] == "result"
        )
        self.assertEqual(result["case"]["status"], "submitted")
        self.assertEqual(result["case"]["resolution"]["action_id"], "RF-RECONCILED")
        action.assert_awaited_once()
        self.assertEqual(
            [call.args[0] for call in tools.await_args_list],
            ["get_customer_context", "get_applicable_policy", "authorize_customer_resolution", "get_resolution_status"],
        )

    async def test_identity_failure_escalates_without_context_access(self):
        conversation_id = await self.start_refund_conversation()
        for attempt in range(1, 4):
            async def verification_tools(name, _arguments):
                if name == "verify_customer_identity":
                    return {"verified": False, "verification_token": None}
                if name == "create_handoff_case":
                    return {
                        "success": True,
                        "handoff_id": "HD-IDENTITY",
                        "queue": "identity_support",
                        "status": "submitted",
                    }
                self.fail(f"Unexpected tool on identity failure: {name}")

            events, tool = await self.process_turn(
                ChatRequest(message="Wrong", conversation_id=conversation_id),
                verification_tools,
            )
            if attempt < 3:
                self.assertTrue(any(event["type"] == "question" for event in events))
                self.assertNotIn("get_customer_context", [call.args[0] for call in tool.await_args_list])
            else:
                result = next(event["payload"] for event in events if event["type"] == "result")
                self.assertEqual(result["case"]["status"], "escalated")
                self.assertEqual(result["case"]["transaction_status"], "not_started")
                self.assertIn("identity_support", result["case"]["escalation"]["queue"])
                self.assertNotIn("get_customer_context", [call.args[0] for call in tool.await_args_list])

    async def test_sensitive_issue_creates_handoff_without_booking_access(self):
        async def handoff_tool(name, _arguments):
            self.assertEqual(name, "create_handoff_case")
            return {
                "success": True,
                "handoff_id": "HD-SENSITIVE",
                "queue": "sensitive_case_support",
                "status": "submitted",
            }

        events, tool = await self.process_turn(
            ChatRequest(message="I had a medical emergency on board."),
            handoff_tool,
        )
        result = next(event["payload"] for event in events if event["type"] == "result")
        self.assertEqual(result["case"]["status"], "escalated")
        self.assertEqual([call.args[0] for call in tool.await_args_list], ["create_handoff_case"])

    async def test_sensitive_word_stops_before_model_or_booking_lookup(self):
        async def handoff_tool(name, _arguments):
            self.assertEqual(name, "create_handoff_case")
            return {
                "success": True,
                "handoff_id": "HD-SEXY",
                "queue": "sensitive_case_support",
                "status": "submitted",
            }

        with patch("main.OPENAI_API_KEY", "configured-test-key"), patch(
            "main.analyze_with_model", new=AsyncMock()
        ) as intent_model, patch(
            "main.run_model_agent", new=AsyncMock()
        ) as escalation_model:
            events, tool = await self.process_turn(
                ChatRequest(message="sexy"),
                handoff_tool,
            )
        result = next(event["payload"] for event in events if event["type"] == "result")
        self.assertEqual(result["case"]["escalation"]["queue"], "sensitive_case_support")
        self.assertIn("Sensitive content was detected", result["case"]["response"])
        self.assertEqual([call.args[0] for call in tool.await_args_list], ["create_handoff_case"])
        intent_model.assert_not_awaited()
        escalation_model.assert_not_awaited()

    async def test_sensitive_request_is_flagged_even_when_model_configuration_is_required(self):
        async def handoff_tool(name, _arguments):
            self.assertEqual(name, "create_handoff_case")
            return {
                "success": True,
                "handoff_id": "HD-SENSITIVE-NO-MODEL",
                "queue": "sensitive_case_support",
                "status": "submitted",
            }

        with patch("main.OPENAI_API_KEY", ""), patch(
            "main.REQUIRE_CONFIGURED_MODEL", True
        ):
            events, _ = await self.process_turn(
                ChatRequest(message="This is a sexual harassment report."),
                handoff_tool,
            )
        result = next(event["payload"] for event in events if event["type"] == "result")
        self.assertEqual(result["case"]["escalation"]["queue"], "sensitive_case_support")

    async def test_refund_is_not_escalated_solely_for_high_amount(self):
        first_events, _ = await self.process_turn(
            ChatRequest(message="My flight was cancelled by the airline; please refund me."),
            AsyncMock(),
        )
        conversation_id = next(
            event["conversation_id"] for event in first_events if event["type"] == "question"
        )
        await self.process_turn(
            ChatRequest(message="PNR901", conversation_id=conversation_id),
            AsyncMock(),
        )

        async def high_value_tools(name, arguments):
            if name == "verify_customer_identity":
                self.assertEqual(arguments["last_name"], "Kim")
                return {"verified": True, "verification_token": "verified-high-value"}
            if name == "get_customer_context":
                return {
                    "found": True,
                    "booking": dict(BOOKINGS["PNR901"]),
                    "passenger": {"name": "Jordan Kim", "loyalty_tier": "Platinum"},
                }
            if name == "get_applicable_policy":
                return {"found": True, "key": "involuntary_refund", **POLICIES["involuntary_refund"]}
            if name == "create_handoff_case":
                return {
                    "success": True,
                    "handoff_id": "HD-HIGHVALUE",
                    "queue": arguments["queue"],
                    "status": "submitted",
                }
            self.fail(f"Unexpected high-value action: {name}")

        events, tool = await self.process_turn(
            ChatRequest(message="Kim", conversation_id=conversation_id),
            high_value_tools,
        )
        offer = next(event["payload"]["case"] for event in events if event["type"] == "offer")
        self.assertEqual(offer["status"], "awaiting_confirmation")
        self.assertEqual(offer["proposed_resolution"]["amount"], BOOKINGS["PNR901"]["payment_amount"])
        self.assertNotIn("issue_refund", [call.args[0] for call in tool.await_args_list])
        self.assertNotIn("authorize_customer_resolution", [call.args[0] for call in tool.await_args_list])

    async def test_direct_case_endpoint_cannot_bypass_verification_or_confirmation(self):
        with self.assertRaises(HTTPException) as raised:
            await direct_case_action_disabled(CaseRequest(message="Refund PNR482"))
        self.assertEqual(raised.exception.status_code, 409)

    async def test_rule_based_demo_asks_questions_and_identifies_model_status(self):
        queue = asyncio.Queue()
        with patch("main.OPENAI_API_KEY", ""):
            await process_chat_turn(ChatRequest(message="My checked bag did not arrive."), queue)
        events = events_from(queue)
        question = next(event for event in events if event["type"] == "question")
        self.assertIn("booking reference", question["message"].lower())
        self.assertFalse(question["model"]["enabled"])
        self.assertIn("api key required", model_configuration()["label"].lower())
        self.assertTrue(model_configuration()["setup_required"])
        self.assertEqual(model_configuration()["agents"][0]["model"], "gpt-5.6-luna")
        self.assertIn("inactive", model_configuration()["agents"][0]["mode"])

    async def test_missing_model_fails_closed_when_model_is_required(self):
        queue = asyncio.Queue()
        with patch("main.OPENAI_API_KEY", ""), patch("main.REQUIRE_CONFIGURED_MODEL", True):
            await process_chat_turn(
                ChatRequest(message="My flight was cancelled by the airline."),
                queue,
            )
        events = events_from(queue)
        error = next(event for event in events if event["type"] == "error")
        self.assertIn("no agent workflow was started", error["message"].lower())
        self.assertFalse(any(event["type"] in {"question", "offer", "result"} for event in events))

    async def test_unsupported_ica_api_format_is_reported_in_configuration(self):
        with patch("main.ICA_API_STYLE", "anthropic"):
            config = model_configuration()
        self.assertFalse(config["enabled"])
        self.assertTrue(config["setup_required"])
        self.assertIn("ICA_API_STYLE=openai-compatible", config["configuration_error"])

    async def test_model_assignments_cover_all_agents_except_deterministic_identity_gate(self):
        with patch("main.OPENAI_API_KEY", "test-key"):
            config = model_configuration()
        self.assertTrue(config["enabled"])
        self.assertIn("5 agents assigned", config["label"])
        self.assertEqual(
            [agent["id"] for agent in config["agents"] if agent["model"]],
            ["intent", "context", "policy", "resolution", "escalation"],
        )
        self.assertEqual(config["agents"][1]["mode"], "deterministic")

    async def test_configured_model_runs_context_policy_and_resolution_agents(self):
        async def model_response(agent_id, _prompt, _context):
            if agent_id == "tool-selection":
                eligible = _context.get("eligible_mcp_tools")
                if eligible:
                    return {"tool_name": eligible[0]["name"]}
                return {"tool_name": "get_applicable_policy"}
            return {
                "context": {"summary": "Verified booking and passenger context."},
                "policy": {"explanation": "The cancellation meets the refund rule."},
                "resolution": {"execution_note": "Submit the confirmed refund."},
            }[agent_id]

        with patch("main.OPENAI_API_KEY", "test-key"), patch(
            "main.analyze_with_model",
            new=AsyncMock(
                return_value={
                    "intent": "refund",
                    "booking_reference": None,
                    "clarification_question": "",
                }
            ),
        ), patch("main.run_model_agent", new=AsyncMock(side_effect=model_response)) as agent, patch(
            "main.discover_mcp_tools",
            new=AsyncMock(return_value=[
                {"name": "verify_customer_identity", "description": "Verify identity", "input_schema": {}},
                {"name": "get_customer_context", "description": "Read context", "input_schema": {}},
                {"name": "get_flight_service_info", "description": "Flight data", "input_schema": {}},
                {"name": "get_refund_status", "description": "Refund status", "input_schema": {}},
                {"name": "get_applicable_policy", "description": "Policy lookup", "input_schema": {}},
                {"name": "authorize_customer_resolution", "description": "Authorize action", "input_schema": {}},
                {"name": "create_handoff_case", "description": "Create handoff", "input_schema": {}},
                {"name": "get_resolution_status", "description": "Read action status", "input_schema": {}},
                {"name": "issue_refund", "description": "Refund", "input_schema": {}},
                {"name": "issue_lounge_access_pass", "description": "Lounge pass", "input_schema": {}},
                {"name": "open_baggage_case", "description": "Baggage case", "input_schema": {}},
            ]),
        ) as discovery:
            conversation_id = await self.start_refund_conversation()

            async def verified_offer_tools(name, _arguments):
                if name == "verify_customer_identity":
                    return {"verified": True, "verification_token": "verified-model-workflow"}
                if name == "get_customer_context":
                    return {
                        "found": True,
                        "booking": dict(BOOKINGS["PNR482"]),
                        "passenger": {
                            "name": "Alex Rivera",
                            "loyalty_tier": "Gold",
                        },
                    }
                if name == "get_applicable_policy":
                    return {
                        "found": True,
                        "key": "involuntary_refund",
                        **POLICIES["involuntary_refund"],
                    }
                self.fail(f"Unexpected model workflow tool: {name}")

            offer_events, _ = await self.process_turn(
                ChatRequest(message="Rivera", conversation_id=conversation_id),
                verified_offer_tools,
            )
            self.assertTrue(any(event["type"] == "offer" for event in offer_events))

            async def confirmed_tools(name, _arguments):
                if name == "get_customer_context":
                    return {
                        "found": True,
                        "booking": dict(BOOKINGS["PNR482"]),
                        "passenger": {"name": "Alex Rivera", "loyalty_tier": "Gold"},
                    }
                if name == "get_applicable_policy":
                    return {
                        "found": True,
                        "key": "involuntary_refund",
                        **POLICIES["involuntary_refund"],
                    }
                if name == "authorize_customer_resolution":
                    return {"authorized": True, "authorization_token": "confirmed-model-token"}
                if name == "issue_refund":
                    return {
                        "success": True,
                        "transaction_status": "submitted",
                        "action_id": "RF-MODEL-TEST",
                        "amount": 428.5,
                        "currency": "USD",
                        "estimated_processing_time": "5-7 business days",
                        "message": "Refund submitted.",
                    }
                self.fail(f"Unexpected confirmed model workflow tool: {name}")

            result_events, _ = await self.process_turn(
                ChatRequest(message="Yes, confirm", conversation_id=conversation_id),
                confirmed_tools,
            )
        result = next(event["payload"]["case"] for event in result_events if event["type"] == "result")
        self.assertEqual(result["status"], "submitted")
        routed_stages = {
            call.args[2]["workflow_stage"]
            for call in agent.await_args_list
            if call.args[0] == "tool-selection" and "workflow_stage" in call.args[2]
        }
        self.assertTrue({
            "identity_verification",
            "verified_customer_context",
            "policy_lookup",
            "post_confirmation_authorization",
            "confirmed_resolution",
        }.issubset(routed_stages))
        self.assertGreaterEqual(discovery.await_count, 7)
        self.assertIn("context", [call.args[0] for call in agent.await_args_list])
        self.assertIn("policy", [call.args[0] for call in agent.await_args_list])
        self.assertIn("resolution", [call.args[0] for call in agent.await_args_list])
        self.assertEqual(result["resolution_agent_note"], "Submit the confirmed refund.")

    async def test_escalation_agent_prepares_audited_handoff_note(self):
        case = {
            "case_id": "CS-ESCALATION-TEST",
            "intent": "rebooking",
            "transaction_status": "not_started",
            "booking": None,
        }
        async def handoff_model_response(agent_id, _prompt, context):
            if agent_id == "tool-selection":
                return {"tool_name": context["eligible_mcp_tools"][0]["name"]}
            return {"handoff_note": "Please review the requested flight change."}

        with patch("main.OPENAI_API_KEY", "test-key"), patch(
            "main.run_model_agent", new=AsyncMock(side_effect=handoff_model_response)
        ) as agent, patch(
            "main.discover_mcp_tools",
            new=AsyncMock(return_value=[
                {"name": "create_handoff_case", "description": "Create handoff", "input_schema": {}},
            ]),
        ), patch(
            "main.call_mcp_tool",
            new=AsyncMock(return_value={
                "success": True,
                "handoff_id": "HD-ESCALATION",
                "queue": "flight_changes",
                "status": "submitted",
            }),
        ) as tool:
            result = await create_handoff(
                case,
                queue="flight_changes",
                summary="The customer requested a rebooking.",
                response="A specialist will help.",
                progress=None,
            )
        self.assertIn("AI-generated handoff note", result["case"]["escalation"]["summary"])
        self.assertIn("Please review the requested flight change", tool.await_args.args[1]["summary"])
        self.assertEqual(agent.await_count, 2)

    async def test_model_intent_analysis_uses_configured_model(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "output_text": '{"intent":"baggage","booking_reference":"PNR615","clarification_question":""}'
                }

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, headers, json):
                self.request = {"url": url, "headers": headers, "json": json}
                return FakeResponse()

        fake_client = FakeClient()
        with patch("main.OPENAI_API_KEY", "test-key"), patch(
            "main.ICA_API_STYLE", "responses"
        ), patch("main.ICA_REASONING_EFFORT", "xhigh"), patch(
            "main.OPENAI_BASE_URL", "https://api.servicesessentials.ibm.com/v1"
        ), patch("main.httpx.AsyncClient", return_value=fake_client):
            analysis = await analyze_with_model([
                {"role": "user", "content": "My checked bag is missing, PNR615"}
            ])
        self.assertEqual(analysis["intent"], "baggage")
        self.assertEqual(analysis["booking_reference"], "PNR615")
        self.assertEqual(fake_client.request["json"]["model"], "gpt-5.6-luna")
        self.assertTrue(fake_client.request["url"].endswith("/responses"))
        self.assertIn("input", fake_client.request["json"])
        self.assertEqual(fake_client.request["headers"]["Authorization"], "Bearer test-key")

    async def test_context_agent_uses_configured_model_and_bearer_auth(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"output_text": '{"summary":"Verified context"}'}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, headers, json):
                self.request = {"url": url, "headers": headers, "json": json}
                return FakeResponse()

        fake_client = FakeClient()
        with patch("main.OPENAI_API_KEY", "test-key"), patch(
            "main.ICA_API_STYLE", "responses"
        ), patch("main.ICA_REASONING_EFFORT", "xhigh"), patch(
            "main.OPENAI_BASE_URL", "https://api.servicesessentials.ibm.com/v1"
        ), patch("main.httpx.AsyncClient", return_value=fake_client):
            result = await run_model_agent(
                "context",
                "Summarize verified context.",
                {"booking_reference": "PNR482"},
            )
        self.assertEqual(result, {"summary": "Verified context"})
        self.assertEqual(fake_client.request["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(fake_client.request["json"]["model"], "gpt-5.6-luna")
        self.assertEqual(
            fake_client.request["url"],
            "https://api.servicesessentials.ibm.com/v1/responses",
        )
        self.assertEqual(fake_client.request["json"]["reasoning"]["effort"], "xhigh")
        self.assertIn("input", fake_client.request["json"])
        self.assertNotIn("test-key", str(model_configuration()))

    async def test_provider_auth_error_is_actionable_without_echoing_key(self):
        class FakeResponse:
            def raise_for_status(self):
                request = httpx.Request("POST", "https://provider.invalid")
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        with patch("main.OPENAI_API_KEY", "private-test-key"), patch(
            "main.httpx.AsyncClient", return_value=FakeClient()
        ):
            with self.assertRaises(HTTPException) as raised:
                await run_model_agent("intent", "Prompt", {"message": "test"})
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("rejected", raised.exception.detail)
        self.assertNotIn("private-test-key", raised.exception.detail)

    async def test_ica_auth_error_names_ica_configuration_without_echoing_key(self):
        class FakeResponse:
            def raise_for_status(self):
                request = httpx.Request("POST", "https://provider.invalid")
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, *_args, **_kwargs):
                return FakeResponse()

        with patch("main.OPENAI_API_KEY", "private-ica-test-key"), patch(
            "main.MODEL_PROVIDER", "IBM Services Essentials"
        ), patch("main.httpx.AsyncClient", return_value=FakeClient()):
            with self.assertRaises(HTTPException) as raised:
                await run_model_agent("intent", "Prompt", {"message": "test"})
        self.assertIn("ICA_API_KEY", raised.exception.detail)
        self.assertIn("Open Code", raised.exception.detail)
        self.assertNotIn("private-ica-test-key", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
