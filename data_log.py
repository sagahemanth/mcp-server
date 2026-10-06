from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4


CSV_SCHEMAS: dict[str, tuple[str, ...]] = {
    "flights": (
        "logged_at",
        "record_ref",
        "flight",
        "origin",
        "destination",
        "travel_date",
        "flight_status",
        "fare_type",
        "currency",
        "boarding_pass_status",
    ),
    "cases": (
        "logged_at",
        "record_ref",
        "intent",
        "status",
        "channel",
        "transaction_status",
        "policy",
        "escalation_queue",
    ),
    "actions": (
        "logged_at",
        "record_ref",
        "agent",
        "action",
        "outcome",
    ),
}

CSV_FILENAMES = {
    "flights": "generated_flights.csv",
    "cases": "case_status_updates.csv",
    "actions": "agent_actions.csv",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class CsvDataLog:
    """Append-only CSV history that deliberately excludes passenger and case identifiers."""

    def __init__(self, directory: str | Path | None = None) -> None:
        configured_dir = directory or os.getenv("AIRLINE_DATA_DIR")
        self.directory = Path(configured_dir) if configured_dir else Path(__file__).resolve().parent / "data"
        self._lock = Lock()
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            for kind, filename in CSV_FILENAMES.items():
                self._ensure_file(kind, filename)

    def _ensure_file(self, kind: str, filename: str) -> None:
        path = self.directory / filename
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as csv_file:
                csv.writer(csv_file).writerow(CSV_SCHEMAS[kind])

    def _append(self, kind: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        filename = CSV_FILENAMES[kind]
        fieldnames = CSV_SCHEMAS[kind]
        with self._lock:
            self._ensure_file(kind, filename)
            with (self.directory / filename).open("a", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
                for row in rows:
                    writer.writerow({field: row.get(field, "") for field in fieldnames})

    def log_flights(self, tickets: list[dict[str, Any]]) -> None:
        logged_at = _timestamp()
        self._append(
            "flights",
            [
                {
                    "logged_at": logged_at,
                    "record_ref": uuid4().hex[:12].upper(),
                    "flight": ticket.get("flight", ""),
                    "origin": ticket.get("origin_airport", ""),
                    "destination": ticket.get("arrival_airport", ""),
                    "travel_date": ticket.get("date", ""),
                    "flight_status": ticket.get("flight_status", ""),
                    "fare_type": ticket.get("fare_type", ""),
                    "currency": ticket.get("currency", ""),
                    "boarding_pass_status": ticket.get("boarding_pass_status", ""),
                }
                for ticket in tickets
            ],
        )

    def log_case(self, case: dict[str, Any]) -> None:
        policy = case.get("policy")
        escalation = case.get("escalation")
        self._append(
            "cases",
            [
                {
                    "logged_at": case.get("updated_at") or case.get("created_at") or _timestamp(),
                    "record_ref": uuid4().hex[:12].upper(),
                    "intent": case.get("intent", ""),
                    "status": case.get("status", ""),
                    "channel": case.get("channel", ""),
                    "transaction_status": case.get("transaction_status", ""),
                    "policy": policy.get("name", "") if isinstance(policy, dict) else "",
                    "escalation_queue": escalation.get("queue", "") if isinstance(escalation, dict) else "",
                }
            ],
        )

    def log_action(self, event: dict[str, Any]) -> None:
        self._append(
            "actions",
            [
                {
                    "logged_at": event.get("timestamp") or _timestamp(),
                    "record_ref": event.get("audit_id", ""),
                    "agent": event.get("actor", ""),
                    "action": event.get("action", ""),
                    "outcome": event.get("outcome", ""),
                }
            ],
        )

    def read_recent(self, kind: str, limit: int = 100) -> list[dict[str, str]]:
        filename = CSV_FILENAMES[kind]
        with self._lock, (self.directory / filename).open(
            "r", newline="", encoding="utf-8"
        ) as csv_file:
            rows = list(csv.DictReader(csv_file))
        return list(reversed(rows[-limit:]))

    def counts(self) -> dict[str, int]:
        totals = {}
        for kind, filename in CSV_FILENAMES.items():
            with self._lock, (self.directory / filename).open(
                "r", newline="", encoding="utf-8"
            ) as csv_file:
                totals[kind] = sum(1 for _ in csv.DictReader(csv_file))
        return totals

    def path_for(self, kind: str) -> Path:
        return self.directory / CSV_FILENAMES[kind]

    def dashboard(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "flights": self.read_recent("flights"),
            "cases": self.read_recent("cases"),
            "actions": self.read_recent("actions"),
            "privacy": (
                "CSV history excludes passenger names, chat messages, PNRs, and support case IDs."
            ),
        }


DATA_LOG = CsvDataLog()
