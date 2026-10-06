from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from secrets import choice, token_hex, token_urlsafe
from threading import Lock
from typing import Any
from uuid import uuid4

from data_log import DATA_LOG

_lock = Lock()
_audit_log: list[dict[str, Any]] = []
_cases: list[dict[str, Any]] = []
_verification_tokens: dict[str, tuple[str, str]] = {}
_resolution_tokens: dict[str, tuple[str, str, str]] = {}
_resolution_results: dict[str, dict[str, Any]] = {}
_used_resolution_tokens: dict[str, tuple[str, str, str]] = {}
_refund_records: dict[str, dict[str, Any]] = {}

PASSENGERS = {
    "CUST1001": {
        "customer_id": "CUST1001",
        "name": "Jamie Rivera",
        "last_name": "Rivera",
        "loyalty_tier": "Gold",
        "previous_interactions": 1,
        "bookings": ["PNR482"],
    },
    "CUST1002": {
        "customer_id": "CUST1002",
        "name": "Taylor Morgan",
        "last_name": "Morgan",
        "loyalty_tier": "Silver",
        "previous_interactions": 3,
        "bookings": ["PNR739"],
    },
    "CUST1003": {
        "customer_id": "CUST1003",
        "name": "Casey Patel",
        "last_name": "Patel",
        "loyalty_tier": "Member",
        "previous_interactions": 0,
        "bookings": ["PNR615"],
    },
    "CUST1004": {
        "customer_id": "CUST1004",
        "name": "Jordan Kim",
        "last_name": "Kim",
        "loyalty_tier": "Platinum",
        "previous_interactions": 2,
        "bookings": ["PNR901"],
    },
}

BOOKINGS = {
    "PNR482": {
        "booking_reference": "PNR482",
        "customer_id": "CUST1001",
        "flight": "AR204",
        "route": "New York (JFK) → Chicago (ORD)",
        "origin": "New York (JFK)",
        "destination": "Chicago (ORD)",
        "origin_airport": "JFK",
        "arrival_airport": "ORD",
        "date": "2026-10-08",
        "departure_time": "08:30",
        "status": "cancelled_by_airline",
        "flight_status": "Cancelled",
        "terminal": "4",
        "gate": "B12",
        "boarding_pass_status": "unavailable",
        "boarding_pass_reference": "BP-DEMO482",
        "fare_type": "flexible",
        "payment_amount": 428.50,
        "currency": "USD",
        "delay_minutes": 0,
        "open_cases": [],
    },
    "PNR739": {
        "booking_reference": "PNR739",
        "customer_id": "CUST1002",
        "flight": "AR517",
        "route": "Los Angeles (LAX) → Seattle (SEA)",
        "origin": "Los Angeles (LAX)",
        "destination": "Seattle (SEA)",
        "origin_airport": "LAX",
        "arrival_airport": "SEA",
        "date": "2026-10-07",
        "departure_time": "10:15",
        "status": "delayed",
        "flight_status": "Delayed",
        "terminal": "6",
        "gate": "52A",
        "boarding_pass_status": "available",
        "boarding_pass_reference": "BP-DEMO739",
        "fare_type": "standard",
        "payment_amount": 219.00,
        "currency": "USD",
        "delay_minutes": 245,
        "open_cases": [],
    },
    "PNR615": {
        "booking_reference": "PNR615",
        "customer_id": "CUST1003",
        "flight": "AR809",
        "route": "Boston (BOS) → Miami (MIA)",
        "origin": "Boston (BOS)",
        "destination": "Miami (MIA)",
        "origin_airport": "BOS",
        "arrival_airport": "MIA",
        "date": "2026-10-09",
        "departure_time": "13:45",
        "status": "confirmed",
        "flight_status": "Scheduled",
        "terminal": "B",
        "gate": "B7",
        "boarding_pass_status": "available",
        "boarding_pass_reference": "BP-DEMO615",
        "fare_type": "basic",
        "payment_amount": 186.00,
        "currency": "USD",
        "delay_minutes": 0,
        "open_cases": [],
    },
    "PNR901": {
        "booking_reference": "PNR901",
        "customer_id": "CUST1004",
        "flight": "AR112",
        "route": "San Francisco (SFO) → Honolulu (HNL)",
        "origin": "San Francisco (SFO)",
        "destination": "Honolulu (HNL)",
        "origin_airport": "SFO",
        "arrival_airport": "HNL",
        "date": "2026-10-10",
        "departure_time": "07:20",
        "status": "cancelled_by_airline",
        "flight_status": "Cancelled",
        "terminal": "2",
        "gate": "D5",
        "boarding_pass_status": "unavailable",
        "boarding_pass_reference": "BP-DEMO901",
        "fare_type": "flexible",
        "payment_amount": 1250.00,
        "currency": "USD",
        "delay_minutes": 0,
        "open_cases": [],
    },
}

POLICIES = {
    "involuntary_refund": {
        "name": "Airline-cancelled flight",
        "rule": "A full refund to the original form of payment is available when the airline cancels a flight.",
        "eligible": True,
        "reference": "AIR-REFUND-INVOL-01",
        "version": "2026.1",
    },
    "delay_lounge_access": {
        "name": "Extended delay lounge access",
        "rule": "A simulated lounge access pass is available for a delay of 180 minutes or more.",
        "eligible": True,
        "reference": "AIR-DISRUPTION-180-01",
        "version": "2026.1",
    },
    "voluntary_refund": {
        "name": "Basic fare changes and refunds",
        "rule": "Basic fares are generally non-refundable. Exceptions require review by a service specialist.",
        "eligible": False,
        "reference": "AIR-FARE-BASIC-REFUND-01",
        "version": "2026.1",
    },
    "baggage": {
        "name": "Baggage tracing",
        "rule": "A baggage tracing case can be opened for a delayed or missing bag.",
        "eligible": True,
        "reference": "AIR-BAG-TRACE-01",
        "version": "2026.1",
    },
}

DEMO_ROUTES = (
    ("New York (JFK)", "Chicago (ORD)"),
    ("Los Angeles (LAX)", "Seattle (SEA)"),
    ("Boston (BOS)", "Miami (MIA)"),
    ("San Francisco (SFO)", "Honolulu (HNL)"),
    ("Dallas (DFW)", "Denver (DEN)"),
    ("Atlanta (ATL)", "Orlando (MCO)"),
)

AIRPORT_BAGGAGE_DESKS = {
    "JFK": "JFK Baggage Service Desk (demo)",
    "ORD": "ORD Baggage Service Desk (demo)",
    "LAX": "LAX Baggage Service Desk (demo)",
    "SEA": "SEA Baggage Service Desk (demo)",
    "BOS": "BOS Baggage Service Desk (demo)",
    "MIA": "MIA Baggage Service Desk (demo)",
    "SFO": "SFO Baggage Service Desk (demo)",
    "HNL": "HNL Baggage Service Desk (demo)",
    "DFW": "DFW Baggage Service Desk (demo)",
    "DEN": "DEN Baggage Service Desk (demo)",
    "ATL": "ATL Baggage Service Desk (demo)",
    "MCO": "MCO Baggage Service Desk (demo)",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_audit(
    *,
    case_id: str,
    actor: str,
    action: str,
    outcome: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    entry = {
        "audit_id": str(uuid4()),
        "timestamp": _now(),
        "case_id": case_id,
        "actor": actor,
        "action": action,
        "outcome": outcome,
        "details": details,
    }
    with _lock:
        _audit_log.append(entry)
    DATA_LOG.log_action(entry)
    return entry


def get_audit_log(case_id: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        entries = list(_audit_log)
    if case_id:
        entries = [entry for entry in entries if entry["case_id"] == case_id]
    return entries


def save_case(case: dict[str, Any]) -> None:
    with _lock:
        for index, existing in enumerate(_cases):
            if existing["case_id"] == case["case_id"]:
                _cases[index] = case
                break
        else:
            _cases.insert(0, case)
    DATA_LOG.log_case(case)


def get_cases() -> list[dict[str, Any]]:
    with _lock:
        return list(_cases)


def get_case(case_id: str) -> dict[str, Any] | None:
    with _lock:
        case = next((item for item in _cases if item["case_id"] == case_id), None)
        return dict(case) if case else None


def get_refund_record(booking_reference: str) -> dict[str, Any] | None:
    with _lock:
        record = _refund_records.get(booking_reference.upper())
        return dict(record) if record else None


def save_refund_record(booking_reference: str, record: dict[str, Any]) -> None:
    with _lock:
        _refund_records[booking_reference.upper()] = dict(record)


def create_random_demo_booking(first_name: str, last_name: str) -> list[dict[str, Any]]:
    """Create ordinary fixture flights with exactly one airline-cancelled flight."""
    cancelled_index = choice(range(3))
    tickets = []
    bookings_to_store = []
    customer_id = f"DEMO{token_hex(4).upper()}"
    while customer_id in PASSENGERS:
        customer_id = f"DEMO{token_hex(4).upper()}"
    passenger = {
        "customer_id": customer_id,
        "name": f"{first_name.strip()} {last_name.strip()}",
        "last_name": last_name.strip(),
        "loyalty_tier": "Demo",
        "previous_interactions": 0,
        "bookings": [],
    }
    generated_flights: set[str] = set()

    for index in range(3):
        booking_reference = f"PNR{token_hex(4).upper()}"
        while booking_reference in BOOKINGS or any(
            item["booking_reference"] == booking_reference for item in bookings_to_store
        ):
            booking_reference = f"PNR{token_hex(4).upper()}"
        origin, destination = DEMO_ROUTES[index % len(DEMO_ROUTES)]
        origin_code = origin.rsplit("(", 1)[-1].rstrip(")")
        destination_code = destination.rsplit("(", 1)[-1].rstrip(")")
        cancelled = index == cancelled_index
        flight = f"AR{choice(range(100, 1000))}"
        while flight in generated_flights or any(
            item.get("flight", "").upper() == flight
            for item in BOOKINGS.values()
        ):
            flight = f"AR{choice(range(100, 1000))}"
        generated_flights.add(flight)
        booking = {
            "booking_reference": booking_reference,
            "customer_id": customer_id,
            "flight": flight,
            "route": f"{origin} → {destination}",
            "origin": origin,
            "destination": destination,
            "origin_airport": origin_code,
            "arrival_airport": destination_code,
            "date": (date.today() + timedelta(days=choice(range(2, 31)))).isoformat(),
            "departure_time": f"{choice(range(5, 23)):02d}:{choice((0, 15, 30, 45)):02d}",
            "status": "cancelled_by_airline" if cancelled else "confirmed",
            "fare_type": choice(("flexible", "standard", "basic")),
            "payment_amount": round(choice(range(12_000, 55_001)) / 100, 2),
            "currency": "USD",
            "delay_minutes": 0,
            "terminal": str(choice(("1", "2", "3", "4"))),
            "gate": f"{choice(('A', 'B', 'C', 'D'))}{choice(range(1, 31))}",
            "flight_status": "Cancelled by airline" if cancelled else "Scheduled",
            "boarding_pass_status": "unavailable" if cancelled else "available",
            "boarding_pass_reference": f"BP-{token_hex(4).upper()}",
            "seat": f"{choice(range(1, 40))}{choice(('A', 'B', 'C', 'D', 'E', 'F'))}",
            "boarding_group": str(choice(range(1, 6))),
            "baggage_status": "checked_in",
            "refund_status": "not_requested",
            "open_cases": [],
        }
        passenger["bookings"].append(booking_reference)
        bookings_to_store.append(booking)
        tickets.append({
            key: booking[key]
            for key in (
                "booking_reference",
                "flight",
                "route",
                "origin",
                "destination",
                "origin_airport",
                "arrival_airport",
                "date",
                "departure_time",
                "terminal",
                "gate",
                "flight_status",
                "boarding_pass_status",
                "boarding_pass_reference",
                "seat",
                "boarding_group",
                "status",
                "fare_type",
                "currency",
            )
        } | {"passenger_name": passenger["name"]})

    with _lock:
        for booking in bookings_to_store:
            BOOKINGS[booking["booking_reference"]] = booking
        PASSENGERS[customer_id] = passenger

    for booking in bookings_to_store:
        record_audit(
            case_id=f"DEMO-{booking['booking_reference']}",
            actor="demo-ticket-generator",
            action="generate_demo_ticket",
            outcome="created",
            details={
                "booking_reference": booking["booking_reference"],
                "route": booking["route"],
                "flight_status": booking["flight_status"],
                "fixture_only": True,
            },
        )
    DATA_LOG.log_flights(tickets)
    return tickets


def get_booking(booking_reference: str) -> dict[str, Any] | None:
    booking = BOOKINGS.get(booking_reference.upper())
    return dict(booking) if booking else None


def get_passenger(customer_id: str) -> dict[str, Any] | None:
    passenger = PASSENGERS.get(customer_id.upper())
    return dict(passenger) if passenger else None


def verify_passenger(booking_reference: str, last_name: str, case_id: str) -> dict[str, Any]:
    booking = BOOKINGS.get(booking_reference.upper())
    passenger = PASSENGERS.get(booking["customer_id"]) if booking else None
    verified = bool(
        passenger
        and last_name.strip().casefold() == passenger["last_name"].casefold()
    )
    token = token_urlsafe(32) if verified else None
    if token:
        with _lock:
            _verification_tokens[token] = (case_id, booking_reference.upper())
    record_audit(
        case_id=case_id,
        actor="identity-verification-agent",
        action="verify_passenger_identity",
        outcome="verified" if verified else "verification_failed",
        details={
            "booking_reference": booking_reference.upper(),
            "verification_method": "booking_reference_and_last_name",
            "identity_value_logged": False,
        },
    )
    return {"verified": verified, "verification_token": token}


def is_verification_valid(token: str, case_id: str, booking_reference: str) -> bool:
    with _lock:
        return _verification_tokens.get(token) == (case_id, booking_reference.upper())


def authorize_resolution(
    case_id: str, booking_reference: str, action: str, customer_confirmed: bool
) -> str | None:
    if not customer_confirmed:
        return None
    token = token_urlsafe(32)
    with _lock:
        _resolution_tokens[token] = (case_id, booking_reference.upper(), action)
    return token


def consume_resolution_authorization(
    token: str, case_id: str, booking_reference: str, action: str
) -> tuple[bool, dict[str, Any] | None]:
    key = f"{case_id}:{action}"
    with _lock:
        existing = _resolution_results.get(key)
        expected = (case_id, booking_reference.upper(), action)
        if existing:
            if _used_resolution_tokens.get(token) == expected:
                return True, dict(existing)
            if _resolution_tokens.get(token) == expected:
                del _resolution_tokens[token]
                _used_resolution_tokens[token] = expected
                return True, dict(existing)
            return False, None
        if _resolution_tokens.get(token) != expected:
            return False, None
        del _resolution_tokens[token]
        _used_resolution_tokens[token] = expected
        return True, None


def save_resolution_result(case_id: str, action: str, result: dict[str, Any]) -> None:
    with _lock:
        _resolution_results[f"{case_id}:{action}"] = dict(result)


def get_resolution_result(case_id: str, action: str) -> dict[str, Any] | None:
    with _lock:
        result = _resolution_results.get(f"{case_id}:{action}")
        return dict(result) if result else None
