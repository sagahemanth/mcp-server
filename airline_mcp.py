from __future__ import annotations

import base64
from io import BytesIO
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
import qrcode

from domain import (
    BOOKINGS,
    AIRPORT_BAGGAGE_DESKS,
    POLICIES,
    authorize_resolution,
    consume_resolution_authorization,
    get_booking,
    get_passenger,
    get_refund_record,
    get_resolution_result,
    is_verification_valid,
    record_audit,
    save_resolution_result,
    save_refund_record,
    verify_passenger,
)


mcp = FastMCP("airline-customer-service", stateless_http=True)
mcp.settings.streamable_http_path = "/"


@mcp.tool()
def verify_customer_identity(
    booking_reference: str, last_name: str, case_id: str
) -> dict[str, Any]:
    """Verify booking ownership without disclosing booking or passenger details."""
    return verify_passenger(booking_reference, last_name, case_id)


@mcp.tool()
def get_customer_context(
    booking_reference: str, case_id: str, verification_token: str
) -> dict[str, Any]:
    """Retrieve passenger and booking data after case-bound identity verification."""
    if not is_verification_valid(verification_token, case_id, booking_reference):
        record_audit(
            case_id=case_id,
            actor="customer-context-agent",
            action="get_customer_context",
            outcome="rejected",
            details={"booking_reference": booking_reference.upper(), "reason": "identity_not_verified"},
        )
        return {"found": False, "message": "Identity verification is required before context retrieval."}
    booking = get_booking(booking_reference)
    if not booking:
        record_audit(
            case_id=case_id,
            actor="customer-context-agent",
            action="get_customer_context",
            outcome="not_found",
            details={"booking_reference": booking_reference},
        )
        return {"found": False, "message": "No booking matched that reference."}
    passenger = get_passenger(booking["customer_id"])
    if passenger:
        passenger.pop("last_name", None)
    record_audit(
        case_id=case_id,
        actor="customer-context-agent",
        action="get_customer_context",
        outcome="success",
        details={
            "systems": ["mock_crm", "mock_pss", "mock_loyalty", "mock_payments"],
            "booking_reference": booking_reference.upper(),
        },
    )
    return {"found": True, "passenger": passenger, "booking": booking}


@mcp.tool()
def get_flight_service_info(
    booking_reference: str,
    service: str,
    case_id: str,
    verification_token: str,
) -> dict[str, Any]:
    """Read boarding-pass or terminal details only after identity verification."""
    if not is_verification_valid(verification_token, case_id, booking_reference):
        record_audit(
            case_id=case_id,
            actor="customer-context-agent",
            action="get_flight_service_info",
            outcome="rejected",
            details={"booking_reference": booking_reference.upper(), "reason": "identity_not_verified"},
        )
        return {"found": False, "message": "Identity verification is required."}
    booking = get_booking(booking_reference)
    if not booking:
        return {"found": False, "message": "No booking matched that reference."}
    passenger = get_passenger(booking["customer_id"])
    if not passenger:
        return {"found": False, "message": "Passenger information is unavailable for this booking."}
    if service == "boarding_pass":
        if booking.get("boarding_pass_status") != "available":
            record_audit(
                case_id=case_id,
                actor="customer-context-agent",
                action="retrieve_boarding_pass",
                outcome="unavailable",
                details={"booking_reference": booking_reference.upper(), "reason": "flight_cancelled"},
            )
            return {
                "found": False,
                "message": "A boarding pass is unavailable because this flight was cancelled by the airline.",
            }
        info = {
            "boarding_pass_status": booking.get("boarding_pass_status", "available"),
            "boarding_pass_reference": booking.get("boarding_pass_reference", "BP-DEMO"),
            "passenger_name": passenger["name"],
            "seat": booking.get("seat", "Not assigned"),
            "boarding_group": booking.get("boarding_group", "Not assigned"),
            "flight": booking["flight"],
            "route": booking.get("route"),
            "date": booking["date"],
            "departure_time": booking.get("departure_time", "To be confirmed"),
            "terminal": booking.get("terminal", "To be confirmed"),
            "gate": booking.get("gate", "To be confirmed"),
            "origin_airport": booking.get("origin_airport"),
            "arrival_airport": booking.get("arrival_airport"),
            "valid_for_travel": False,
        }
        qr_payload = "\n".join(
            (
                "DEMO BOARDING PASS - NOT VALID FOR TRAVEL",
                f"Reference: {info['boarding_pass_reference']}",
                f"Booking: {booking_reference.upper()}",
                f"Flight: {booking['flight']}",
                f"Date: {booking['date']}",
                f"From: {booking.get('origin_airport', '')}",
                f"To: {booking.get('arrival_airport', '')}",
                f"Seat: {info['seat']}",
            )
        )
        image = qrcode.make(qr_payload)
        qr_buffer = BytesIO()
        image.save(qr_buffer, format="PNG")
        info["qr_code_data_uri"] = (
            "data:image/png;base64,"
            + base64.b64encode(qr_buffer.getvalue()).decode("ascii")
        )
        action = "retrieve_boarding_pass"
    elif service == "flight_info":
        info = {
            "flight": booking["flight"],
            "route": booking.get("route"),
            "date": booking["date"],
            "departure_time": booking.get("departure_time", "To be confirmed"),
            "flight_status": booking.get("flight_status", booking["status"]),
            "terminal": booking.get("terminal", "To be confirmed"),
            "gate": booking.get("gate", "To be confirmed"),
            "origin_airport": booking.get("origin_airport"),
            "arrival_airport": booking.get("arrival_airport"),
        }
        action = "retrieve_flight_terminal_info"
    else:
        return {"found": False, "message": "Unsupported flight service request."}
    record_audit(
        case_id=case_id,
        actor="customer-context-agent",
        action=action,
        outcome="success",
        details={"booking_reference": booking_reference.upper(), "service": service, "source": "mock_pss"},
    )
    return {"found": True, **info}


@mcp.tool()
def get_refund_status(
    booking_reference: str,
    case_id: str,
    verification_token: str,
) -> dict[str, Any]:
    """Read simulated airline and bank refund status after identity verification."""
    if not is_verification_valid(verification_token, case_id, booking_reference):
        record_audit(
            case_id=case_id,
            actor="customer-context-agent",
            action="get_refund_status",
            outcome="rejected",
            details={"booking_reference": booking_reference.upper(), "reason": "identity_not_verified"},
        )
        return {"found": False, "message": "Identity verification is required."}
    booking = get_booking(booking_reference)
    if not booking:
        return {"found": False, "message": "No booking matched that reference."}
    refund = get_refund_record(booking_reference)
    if refund is None:
        refund = {
            "airline_status": "not_issued",
            "bank_status": "not_started",
            "message": "No refund has been issued for this booking.",
        }
    record_audit(
        case_id=case_id,
        actor="customer-context-agent",
        action="check_refund_status",
        outcome=refund["airline_status"],
        details={
            "booking_reference": booking_reference.upper(),
            "airline_status": refund["airline_status"],
            "bank_status": refund["bank_status"],
            "bank_integration": "not_connected_demo_pending",
        },
    )
    return {"found": True, **refund}


@mcp.tool()
def authorize_customer_resolution(
    case_id: str,
    booking_reference: str,
    action: str,
    customer_confirmed: bool,
) -> dict[str, Any]:
    """Issue a one-time action token only after explicit customer confirmation."""
    allowed_actions = {"refund", "lounge_access", "baggage_case"}
    if action not in allowed_actions or not customer_confirmed:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="authorize_resolution",
            outcome="rejected",
            details={"action": action, "customer_confirmed": customer_confirmed},
        )
        return {"authorized": False}
    token = authorize_resolution(case_id, booking_reference, action, customer_confirmed)
    record_audit(
        case_id=case_id,
        actor="resolution-agent",
        action="authorize_resolution",
        outcome="authorized",
        details={
            "action": action,
            "booking_reference": booking_reference.upper(),
            "customer_confirmed": True,
            "token_logged": False,
        },
    )
    return {"authorized": True, "authorization_token": token}


@mcp.tool()
def create_handoff_case(
    case_id: str,
    queue: str,
    summary: str,
    booking_reference: str | None = None,
) -> dict[str, Any]:
    """Create a specialist handoff record in the mock case-management system."""
    handoff_id = f"HD-{case_id[-6:].upper()}"
    record_audit(
        case_id=case_id,
        actor="escalation-agent",
        action="create_handoff_case",
        outcome="submitted",
        details={
            "system": "mock_case_management",
            "handoff_id": handoff_id,
            "queue": queue,
            "booking_reference": booking_reference,
            "summary": summary[:1000],
        },
    )
    return {
        "success": True,
        "handoff_id": handoff_id,
        "queue": queue,
        "status": "submitted",
    }


@mcp.tool()
def get_resolution_status(case_id: str, action: str) -> dict[str, Any]:
    """Reconcile a possibly timed-out mock transaction without retrying it."""
    if action not in {"refund", "lounge_access", "baggage_case"}:
        return {"found": False, "message": "Unsupported transaction type."}
    result = get_resolution_result(case_id, action)
    record_audit(
        case_id=case_id,
        actor="resolution-agent",
        action="reconcile_resolution_status",
        outcome="found" if result else "not_found",
        details={"action": action, "transaction_status": result.get("transaction_status") if result else None},
    )
    return {"found": bool(result), "result": result}


@mcp.tool()
def get_applicable_policy(
    policy_key: str, booking_reference: str, case_id: str
) -> dict[str, Any]:
    """Retrieve the applicable policy rule for a booking and audit the lookup."""
    policy = POLICIES.get(policy_key)
    if not policy:
        record_audit(
            case_id=case_id,
            actor="policy-decision-agent",
            action="get_applicable_policy",
            outcome="not_found",
            details={"policy_key": policy_key, "booking_reference": booking_reference},
        )
        return {"found": False, "message": "No matching policy was found."}
    record_audit(
        case_id=case_id,
        actor="policy-decision-agent",
        action="get_applicable_policy",
        outcome="success",
        details={"policy_key": policy_key, "policy_name": policy["name"]},
    )
    return {"found": True, "key": policy_key, **policy}


@mcp.tool()
def issue_refund(
    booking_reference: str,
    amount: float,
    case_id: str,
    reason: str,
    authorization_token: str,
) -> dict[str, Any]:
    """Submit a previously confirmed airline-cancellation refund through mock payments."""
    authorized, prior_result = consume_resolution_authorization(
        authorization_token, case_id, booking_reference, "refund"
    )
    if not authorized:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="issue_refund",
            outcome="rejected",
            details={"booking_reference": booking_reference, "reason": "confirmation_required"},
        )
        return {"success": False, "message": "Passenger confirmation is required before this refund."}
    if prior_result:
        return {**prior_result, "idempotent_replay": True}
    booking = BOOKINGS.get(booking_reference.upper())
    if not booking or booking["status"] != "cancelled_by_airline":
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="issue_refund",
            outcome="rejected",
            details={"booking_reference": booking_reference, "reason": "not_eligible"},
        )
        return {"success": False, "message": "This booking is not eligible for an automatic refund."}
    if amount != booking["payment_amount"]:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="issue_refund",
            outcome="rejected",
            details={"booking_reference": booking_reference, "reason": "amount_mismatch"},
        )
        return {"success": False, "message": "The refund amount did not match the original payment."}
    result = {
        "success": True,
        "transaction_status": "submitted",
        "airline_status": "initiated",
        "bank_status": "pending_approval",
        "action_id": f"RF-{case_id[-6:].upper()}",
        "amount": amount,
        "currency": booking["currency"],
        "estimated_processing_time": "5-7 business days",
        "refund_destination": "original payment source",
        "message": (
            "The airline has initiated the refund to your original payment source. "
            "Bank approval is pending; the bank is not connected to this demo."
        ),
    }
    save_resolution_result(case_id, "refund", result)
    save_refund_record(
        booking_reference,
        {
            "airline_status": "initiated",
            "bank_status": "pending_approval",
            "action_id": result["action_id"],
            "amount": amount,
            "currency": booking["currency"],
            "message": (
                "The airline initiated the refund to the original payment source. "
                "Bank approval is pending; the bank is not connected to this demo."
            ),
        },
    )
    record_audit(
        case_id=case_id,
        actor="resolution-agent",
        action="issue_refund",
        outcome="submitted",
        details={
            "system": "mock_payments",
            "booking_reference": booking_reference.upper(),
            "amount": amount,
            "currency": booking["currency"],
            "reason": reason,
            "transaction_status": "submitted",
            "estimated_processing_time": result["estimated_processing_time"],
        },
    )
    return result


@mcp.tool()
def issue_lounge_access_pass(
    booking_reference: str,
    case_id: str,
    reason: str,
    authorization_token: str,
) -> dict[str, Any]:
    """Issue a simulated lounge access pass after confirmation and delay eligibility checks."""
    authorized, prior_result = consume_resolution_authorization(
        authorization_token, case_id, booking_reference, "lounge_access"
    )
    if not authorized:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="issue_lounge_access_pass",
            outcome="rejected",
            details={"booking_reference": booking_reference, "reason": "confirmation_required"},
        )
        return {"success": False, "message": "Passenger confirmation is required before issuing a lounge pass."}
    if prior_result:
        return {**prior_result, "idempotent_replay": True}
    booking = BOOKINGS.get(booking_reference.upper())
    if not booking or booking["delay_minutes"] < 180:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="issue_lounge_access_pass",
            outcome="rejected",
            details={"booking_reference": booking_reference, "reason": "not_eligible"},
        )
        return {"success": False, "message": "This booking does not meet the lounge access delay threshold."}
    lounge_pass_id = f"LA-{uuid4().hex[:10].upper()}"
    result = {
        "success": True,
        "transaction_status": "issued",
        "action_id": lounge_pass_id,
        "lounge_pass_id": lounge_pass_id,
        "message": (
            f"Your simulated lounge access pass is ready. Pass ID: {lounge_pass_id}. "
            "This demo ID is not valid for entry to a real airport lounge."
        ),
    }
    save_resolution_result(case_id, "lounge_access", result)
    record_audit(
        case_id=case_id,
        actor="resolution-agent",
        action="issue_lounge_access_pass",
        outcome="submitted",
        details={
            "system": "mock_lounge_access",
            "booking_reference": booking_reference.upper(),
            "lounge_pass_id": lounge_pass_id,
            "delay_minutes": booking["delay_minutes"],
            "reason": reason,
            "real_lounge_access": False,
        },
    )
    return result


@mcp.tool()
def open_baggage_case(
    booking_reference: str, case_id: str, description: str, authorization_token: str
) -> dict[str, Any]:
    """Create a baggage trace and simulated handoff to its destination airport desk."""
    authorized, prior_result = consume_resolution_authorization(
        authorization_token, case_id, booking_reference, "baggage_case"
    )
    if not authorized:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="open_baggage_case",
            outcome="rejected",
            details={"booking_reference": booking_reference, "reason": "confirmation_required"},
        )
        return {"success": False, "message": "Passenger confirmation is required before creating this case."}
    if prior_result:
        return {**prior_result, "idempotent_replay": True}
    booking = BOOKINGS.get(booking_reference.upper())
    if not booking:
        record_audit(
            case_id=case_id,
            actor="resolution-agent",
            action="open_baggage_case",
            outcome="rejected",
            details={"booking_reference": booking_reference, "reason": "booking_not_found"},
        )
        return {"success": False, "message": "We could not verify the booking for a baggage case."}
    airport_code = booking.get("arrival_airport", "AIRPORT")
    airport_desk = AIRPORT_BAGGAGE_DESKS.get(
        airport_code,
        f"{airport_code} Baggage Service Desk (demo)",
    )
    result = {
        "success": True,
        "transaction_status": "submitted",
        "action_id": f"BG-{case_id[-6:].upper()}",
        "airport_code": airport_code,
        "airport_desk": airport_desk,
        "message": (
            f"Your baggage trace was submitted to the simulated {airport_desk} "
            f"for arrival airport {airport_code}. No real airport was contacted."
        ),
    }
    save_resolution_result(case_id, "baggage_case", result)
    record_audit(
        case_id=case_id,
        actor="resolution-agent",
        action="open_baggage_case",
        outcome="submitted",
        details={
            "system": "mock_case_management",
            "booking_reference": booking_reference.upper(),
            "description": description[:240],
            "arrival_airport": airport_code,
            "airport_desk": airport_desk,
            "external_contact": False,
        },
    )
    return result
