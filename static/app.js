const form = document.querySelector("#case-form");
const messageInput = document.querySelector("#message");
const bookingInput = document.querySelector("#booking-reference");
const channelInput = document.querySelector("#channel");
const submitButton = document.querySelector("#submit-button");
const resultPanel = document.querySelector("#result-panel");
const auditList = document.querySelector("#audit-list");
const toast = document.querySelector("#toast");
const conversationLog = document.querySelector("#conversation-log");
let toastTimer;
let conversationId = null;
let activeConversation = false;
let demoProfile = null;
let generatedTicket = null;
let profileTicketReference = null;
let lastCaseId = null;
const profileStorageKey = "airlineCareDemoProfile";

try {
  const savedProfile = JSON.parse(localStorage.getItem(profileStorageKey) || "null");
  if (
    savedProfile
    && typeof savedProfile.firstName === "string"
    && savedProfile.firstName.trim()
    && typeof savedProfile.lastName === "string"
    && savedProfile.lastName.trim()
    && typeof savedProfile.email === "string"
    && savedProfile.email.trim()
  ) {
    demoProfile = savedProfile;
  }
} catch {
  demoProfile = null;
}

const escapeHtml = (value = "") =>
  String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);

const statusLabel = (status) => ({
  resolved: "Resolved",
  submitted: "Submitted — awaiting settlement",
  awaiting_confirmation: "Needs your confirmation",
  investigating: "Under investigation",
  cancelled: "Cancelled — no action taken",
  escalated: "Specialist review",
  needs_information: "Needs information",
}[status] || status);

const statusClass = (status) => ({
  resolved: "resolved",
  submitted: "submitted",
  awaiting_confirmation: "awaiting-confirmation",
  investigating: "investigating",
  cancelled: "cancelled",
  escalated: "escalated",
  needs_information: "needs-info",
}[status] || "");

function showToast(text, error = false) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.className = `toast visible${error ? " error" : ""}`;
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3500);
}

function addConversationMessage(role, message, state = "") {
  conversationLog.classList.remove("hidden");
  const bubble = document.createElement("div");
  bubble.className = `chat-message ${role}${state ? ` ${state}` : ""}`;
  const label = document.createElement("small");
  label.textContent = role === "customer" ? "YOU" : "AIRLINE CARE";
  const text = document.createElement("p");
  text.textContent = message;
  bubble.append(label, text);
  conversationLog.append(bubble);
  conversationLog.scrollTop = conversationLog.scrollHeight;
  return bubble;
}

function updateAgentProgress(event) {
  const activity = document.querySelector("#agent-activity");
  const workflowState = document.querySelector("#workflow-state");
  activity.classList.remove("hidden");
  workflowState.classList.remove("failed");
  workflowState.innerHTML = '<i></i> Working';
  document.querySelector("#model-pulse").classList.remove("hidden");
  document.querySelector("#activity-agent").textContent = event.agent;
  document.querySelector("#activity-message").textContent = event.message;
  const rows = [...document.querySelectorAll(".agent-row[data-agent]")];
  const activeIndex = rows.findIndex((row) => row.dataset.agent === event.agent);
  rows.forEach((row, index) => {
    row.classList.toggle("working", index === activeIndex);
    row.classList.toggle("completed", activeIndex > -1 && index < activeIndex);
    row.querySelector(".agent-check").textContent = index < activeIndex ? "✓" : index === activeIndex ? "…" : "·";
  });
}

function finishAgentProgress(awaitingCustomer = false) {
  const activeRows = document.querySelectorAll(".agent-row.working");
  activeRows.forEach((row) => {
    row.classList.remove("working");
    row.classList.add("completed");
    row.querySelector(".agent-check").textContent = "✓";
  });
  document.querySelector("#agent-activity").classList.add("hidden");
  document.querySelector("#model-pulse").classList.add("hidden");
  document.querySelector("#workflow-state").innerHTML = awaitingCustomer
    ? "<i></i> Waiting for you"
    : "<i></i> Ready";
}

function failAgentProgress() {
  const activeRows = document.querySelectorAll(".agent-row.working");
  activeRows.forEach((row) => {
    row.classList.remove("working");
    row.classList.add("failed");
    row.querySelector(".agent-check").textContent = "!";
  });
  document.querySelector("#agent-activity").classList.add("hidden");
  document.querySelector("#model-pulse").classList.add("hidden");
  const workflowState = document.querySelector("#workflow-state");
  workflowState.classList.add("failed");
  workflowState.innerHTML = "<i></i> Connection error";
}

async function consumeEventStream(response, onEvent) {
  if (!response.ok || !response.body) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || "The agent could not process this request.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  while (!finished) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const data = block.split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!data) continue;
      const event = JSON.parse(data);
      if (event.type === "done") {
        finished = true;
        break;
      }
      onEvent(event);
    }
    if (done) finished = true;
  }
  await reader.cancel();
}

function renderCase(result) {
  const item = result.case;
  const action = item.resolution;
  const policy = item.policy;
  const customer = item.customer;
  const audit = result.audit || [];
  lastCaseId = item.case_id;
  document.querySelector("#track-case-reference").value = item.case_id;
  const heading = {
    resolved: "Request completed",
    submitted: "Action submitted",
    awaiting_confirmation: "Review your options",
    investigating: "Investigation in progress",
    escalated: "Specialist support arranged",
    cancelled: "Request cancelled",
    needs_information: "More information needed",
  }[item.status] || "Case update";
  resultPanel.innerHTML = `
    <div class="result-head">
      <div><span class="section-kicker">CASE ${escapeHtml(item.case_id)}</span><h2>${heading}</h2></div>
      <span class="status-badge ${statusClass(item.status)}"><i></i>${escapeHtml(statusLabel(item.status))}</span>
    </div>
    <div class="result-body">
      <div class="result-message"><span class="result-icon">${item.status === "resolved" ? "✓" : item.status === "escalated" ? "⇥" : "?"}</span><div><strong>Customer response</strong><p>${escapeHtml(item.response)}</p></div></div>
      <div class="result-details">
        ${customer ? `<div><small>PASSENGER</small><strong>${escapeHtml(customer.name)} <span class="tier">${escapeHtml(customer.loyalty_tier)}</span></strong></div>` : ""}
        ${item.booking ? `<div><small>BOOKING &amp; FLIGHT</small><strong>${escapeHtml(item.booking.booking_reference)} <span>${escapeHtml(item.booking.flight)}</span></strong></div>` : ""}
        ${policy ? `<div><small>POLICY APPLIED</small><strong>${escapeHtml(policy.name)} <span>${escapeHtml(policy.reference || "")} · v${escapeHtml(policy.version || "")}</span></strong></div>` : ""}
        ${item.proposed_resolution ? `<div><small>PROPOSED OPTION · NOT YET SUBMITTED</small><strong>${escapeHtml(item.proposed_resolution.title)}${item.proposed_resolution.amount ? ` · ${escapeHtml(item.proposed_resolution.currency)} ${Number(item.proposed_resolution.amount).toFixed(2)}` : ""}</strong></div>` : ""}
        ${action?.success && action.lounge_pass_id ? `<div><small>LOUNGE ACCESS PASS · DEMO ONLY</small><strong>Pass ID: ${escapeHtml(action.lounge_pass_id)} <span>Not valid for entry to a real airport lounge</span></strong></div>` : ""}
        ${action?.success && !action.lounge_pass_id ? `<div><small>TRANSACTION STATUS / REFERENCE</small><strong>${escapeHtml(action.transaction_status || item.transaction_status || "submitted")} · ${escapeHtml(action.action_id)}${action.amount ? ` · ${escapeHtml(action.currency)} ${Number(action.amount).toFixed(2)}` : ""}</strong></div>` : ""}
        ${item.transaction_status ? `<div><small>TRANSACTION STATE</small><strong>${escapeHtml(item.transaction_status.replaceAll("_", " "))}</strong></div>` : ""}
        ${item.service_info?.boarding_pass_reference ? `<div><small>BOARDING PASS · DEMO ONLY</small><strong>${escapeHtml(item.service_info.boarding_pass_reference)} <span>${escapeHtml(item.service_info.passenger_name)} · Seat ${escapeHtml(item.service_info.seat)} · Group ${escapeHtml(item.service_info.boarding_group)}</span></strong></div>` : ""}
        ${item.service_info?.qr_code_data_uri ? `<div class="boarding-qr"><small>DEMO QR · NOT VALID FOR TRAVEL</small><img src="${escapeHtml(item.service_info.qr_code_data_uri)}" alt="Mock boarding-pass QR code" /><span>This code contains mock flight details only.</span></div>` : ""}
        ${item.service_info?.airline_status ? `<div><small>REFUND STATUS · SIMULATED</small><strong>Airline: ${escapeHtml(item.service_info.airline_status.replaceAll("_", " "))} <span>Bank: ${escapeHtml((item.service_info.bank_status || "not connected").replaceAll("_", " "))} · to ${escapeHtml(item.service_info.refund_destination || "original payment source")}</span></strong></div>` : ""}
        ${item.service_info?.airport_desk ? `<div><small>BAGGAGE HANDOFF · SIMULATED</small><strong>${escapeHtml(item.service_info.airport_desk)} <span>Arrival airport ${escapeHtml(item.service_info.airport_code)}</span></strong></div>` : ""}
        ${action?.airport_desk ? `<div><small>BAGGAGE HANDOFF · SIMULATED</small><strong>${escapeHtml(action.airport_desk)} <span>Arrival airport ${escapeHtml(action.airport_code)} · no real airport contacted</span></strong></div>` : ""}
        ${item.service_info?.flight_status ? `<div><small>FLIGHT STATUS</small><strong>${escapeHtml(item.service_info.flight_status)} <span>Terminal ${escapeHtml(item.service_info.terminal)} · Gate ${escapeHtml(item.service_info.gate)}</span></strong></div>` : ""}
        ${item.handoff?.handoff_id ? `<div><small>HANDOFF REFERENCE</small><strong>${escapeHtml(item.handoff.handoff_id)} · ${escapeHtml(item.handoff.queue.replaceAll("_", " "))}</strong></div>` : ""}
        ${item.escalation ? `<div><small>ROUTED TO</small><strong>${escapeHtml(item.escalation.queue.replaceAll("_", " "))}</strong></div>` : ""}
      </div>
    </div>
    ${renderCaseJourney(item, audit)}
    <div class="result-footer"><span>Support tracking ID: <strong>${escapeHtml(item.case_id)}</strong></span><button class="text-button" data-scroll-audit>View audit trail <b>→</b></button></div>`;
  resultPanel.classList.remove("hidden");
  resultPanel.querySelector("[data-scroll-audit]").addEventListener("click", () => {
    document.querySelector("#audit").classList.remove("hidden");
    document.querySelector("#audit").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  resultPanel.scrollIntoView({ behavior: "smooth", block: "center" });
}

function auditStep(event, item) {
  const details = event.details || {};
  const outcome = event.outcome || "recorded";
  const action = event.action || "";
  const steps = {
    classify_intent: ["Customer Intent Agent", `Classified the request as ${String(item.intent || "customer support").replaceAll("_", " ")}.`],
    ask_follow_up: ["Customer Intent Agent", "Requested a missing detail before continuing."],
    request_identity_verification: ["Identity Verification Agent", "Asked for the booking surname before accessing passenger data."],
    verify_passenger_identity: ["Identity Verification Agent", outcome === "verified" ? "Matched the surname to the booking." : "Could not match the supplied surname; passenger data remained protected."],
    get_customer_context: ["Customer Context Agent", outcome === "success" ? "Retrieved booking and passenger context after identity verification." : "Could not retrieve booking context."],
    summarize_verified_context: ["Customer Context Agent", "Summarized the verified booking details for the service workflow."],
    get_applicable_policy: ["Policy Decision Agent", outcome === "success" ? "Looked up the applicable demo policy." : "Could not find an applicable policy."],
    evaluate_eligibility: ["Policy Decision Agent", `Applied deterministic eligibility rules: ${outcome.replaceAll("_", " ")}.`],
    explain_policy_decision: ["Policy Decision Agent", "Prepared a customer-friendly explanation of the policy decision."],
    present_resolution_option: ["Resolution Agent", "Presented an option and waited for the passenger's explicit confirmation."],
    prepare_confirmed_resolution: ["Resolution Agent", "Prepared the action after the passenger confirmed."],
    authorize_resolution: ["Resolution Agent", outcome === "authorized" ? "Obtained one-time authorization after confirmation." : "Action authorization was not granted."],
    customer_declined_resolution: ["Resolution Agent", "Passenger declined; no action was taken."],
    issue_refund: ["Resolution Agent", "Submitted the confirmed refund request to the mock payment system."],
    issue_lounge_access_pass: ["Resolution Agent", "Issued the confirmed simulated lounge access pass with a reference ID."],
    open_baggage_case: ["Resolution Agent", "Submitted the confirmed baggage trace to mock case management."],
    retrieve_boarding_pass: ["Customer Context Agent", "Retrieved the simulated boarding pass after verifying the passenger."],
    retrieve_flight_terminal_info: ["Customer Context Agent", "Retrieved simulated flight, terminal, and gate information after verification."],
    check_refund_status: ["Customer Context Agent", "Checked the airline refund record; bank status is simulated because no bank system is connected."],
    create_handoff_case: ["Escalation Agent", "Created a specialist handoff with a case summary."],
    summarize_handoff: ["Escalation Agent", "Prepared a summary for the specialist team."],
    route_case: ["Escalation Agent", `Routed the request to ${String(details.queue || "a specialist team").replaceAll("_", " ")}.`],
    reconcile_resolution_status: ["Resolution Agent", "Checked the transaction ledger without retrying the action."],
    resolution_tool_outcome_uncertain: ["Resolution Agent", "Outcome was uncertain; the action was not automatically retried."],
  };
  if (steps[action]) return steps[action];
  const names = {
    "customer-intent-agent": "Customer Intent Agent",
    "identity-verification-agent": "Identity Verification Agent",
    "customer-context-agent": "Customer Context Agent",
    "policy-decision-agent": "Policy Decision Agent",
    "resolution-agent": "Resolution Agent",
    "escalation-agent": "Escalation Agent",
  };
  return [names[event.actor] || "Airline Care", `${action.replaceAll("_", " ")} — ${outcome.replaceAll("_", " ")}.`];
}

function renderCaseJourney(item, audit) {
  if (!audit.length) {
    return `<section class="case-journey"><h3>How this request was handled</h3><p>Detailed agent and decision events will appear here once the workflow records them.</p></section>`;
  }
  const events = [...audit];
  return `<section class="case-journey">
    <h3>How the agents handled this request</h3>
    <ol>${events.map((event) => {
      const [agent, explanation] = auditStep(event, item);
      return `<li><span class="journey-dot"></span><div><strong>${escapeHtml(agent)}</strong><p>${escapeHtml(explanation)}</p></div><small>${escapeHtml(String(event.outcome || "recorded").replaceAll("_", " "))}</small></li>`;
    }).join("")}</ol>
    <p class="journey-result"><strong>What solved it:</strong> ${escapeHtml(item.response || "The request is still being processed.")}</p>
  </section>`;
}

function addConfirmationButtons(bubble) {
  const actions = document.createElement("div");
  actions.className = "confirmation-actions";
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "confirm-action";
  confirm.textContent = "Confirm and submit";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "cancel-action";
  cancel.textContent = "No, cancel";
  confirm.addEventListener("click", () => {
    actions.remove();
    sendCustomerMessage("Yes, I confirm. Please proceed.");
  });
  cancel.addEventListener("click", () => {
    actions.remove();
    sendCustomerMessage("No, do not proceed. Cancel this request.");
  });
  actions.append(confirm, cancel);
  bubble.append(actions);
  conversationLog.scrollTop = conversationLog.scrollHeight;
}

function renderAudit(events) {
  document.querySelector("#audit-total").textContent = events.length;
  if (!events.length) {
    auditList.innerHTML = '<div class="empty-audit">Audit entries appear here after a case is processed.</div>';
    return;
  }
  auditList.innerHTML = events.map((event) => {
    const time = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(event.timestamp));
    return `<article class="audit-event">
      <span class="audit-dot ${event.outcome === "completed" || event.outcome === "success" || event.outcome === "eligible" ? "good" : event.outcome === "not_eligible" || event.outcome === "not_found" ? "warn" : ""}"></span>
      <div class="audit-event-main"><div><strong>${escapeHtml(event.action.replaceAll("_", " "))}</strong><span class="audit-outcome">${escapeHtml(event.outcome.replaceAll("_", " "))}</span></div><small>${escapeHtml(event.actor)} · ${escapeHtml(event.case_id)}</small>
      <details><summary>Tool details</summary><pre>${escapeHtml(JSON.stringify(event.details, null, 2))}</pre></details></div><time>${escapeHtml(time)}</time>
    </article>`;
  }).join("");
}

async function loadAudit(caseId) {
  const scopedCaseId = caseId || lastCaseId;
  if (!scopedCaseId) {
    showToast("Enter a case ID to view its audit trail.", true);
    return;
  }
  try {
    const response = await fetch(`/api/audit?case_id=${encodeURIComponent(scopedCaseId)}`);
    if (!response.ok) throw new Error("Could not load the audit trail.");
    renderAudit((await response.json()).events);
    document.querySelector("#audit").classList.remove("hidden");
    document.querySelector("#audit").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showToast(error.message, true);
  }
}

function showProfileDialog() {
  if (demoProfile) {
    document.querySelector("#profile-first-name").value = demoProfile.firstName;
    document.querySelector("#profile-last-name").value = demoProfile.lastName;
    document.querySelector("#profile-email-input").value = demoProfile.email;
  }
  document.querySelector("#profile-dialog").showModal();
}

function renderProfile() {
  const name = demoProfile ? `${demoProfile.firstName} ${demoProfile.lastName}` : "Demo profile";
  const email = demoProfile ? demoProfile.email : "Set your name and email";
  document.querySelector("#profile-name").textContent = name;
  document.querySelector("#profile-email").textContent = email;
  document.querySelector(".avatar").textContent = demoProfile
    ? `${demoProfile.firstName[0]}${demoProfile.lastName[0]}`.toUpperCase()
    : "AC";
}

function renderDemoTickets(tickets) {
  const panel = document.querySelector("#demo-ticket");
  panel.innerHTML = `
    <div><span class="section-kicker">THREE GENERATED MOCK FLIGHTS</span><strong>${escapeHtml(tickets[0]?.passenger_name || "")}</strong></div>
    <div class="demo-flight-list">${tickets.map((ticket) => `
      <article class="demo-flight ${ticket.status === "cancelled_by_airline" ? "cancelled-flight" : ""}">
        <p><b>${escapeHtml(ticket.booking_reference)}</b> · Flight ${escapeHtml(ticket.flight)} · ${escapeHtml(ticket.date)}</p>
        <p>${escapeHtml(ticket.route)} · departs ${escapeHtml(ticket.departure_time)} · Terminal ${escapeHtml(ticket.terminal)}, Gate ${escapeHtml(ticket.gate)}</p>
        <p>Flight status: <strong>${escapeHtml(ticket.flight_status)}</strong> · Boarding pass: ${escapeHtml(ticket.boarding_pass_status)}</p>
        <button class="text-button" type="button" data-use-booking="${escapeHtml(ticket.booking_reference)}">Use this flight for a request <b>→</b></button>
      </article>`).join("")}</div>`;
  panel.classList.remove("hidden");
  panel.querySelectorAll("[data-use-booking]").forEach((button) => button.addEventListener("click", () => {
    if (activeConversation) {
      showToast("Finish the current conversation before starting a new ticket.", true);
      return;
    }
    profileTicketReference = button.dataset.useBooking;
    bookingInput.value = button.dataset.useBooking;
    messageInput.value = "";
    messageInput.placeholder = `Booking ${button.dataset.useBooking} selected. Tell us what you need help with.`;
    messageInput.dispatchEvent(new Event("input"));
    messageInput.focus();
  }));
}

async function generateDemoTicket() {
  if (!demoProfile) {
    showProfileDialog();
    showToast("Save a demo profile first to create a ticket in your name.", true);
    return;
  }
  if (activeConversation) {
    showToast("Finish the current conversation before creating another ticket.", true);
    return;
  }
  const button = document.querySelector("#generate-ticket");
  button.disabled = true;
  button.textContent = "Generating ticket…";
  try {
    const response = await fetch("/api/demo/tickets/random", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        first_name: demoProfile.firstName,
        last_name: demoProfile.lastName,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Could not generate a demo ticket.");
    generatedTicket = payload.tickets;
    renderDemoTickets(generatedTicket);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = 'Generate a random demo ticket <b>↻</b>';
  }
}

async function trackCase(caseId) {
  const response = await fetch(`/api/cases/${encodeURIComponent(caseId.trim().toUpperCase())}`);
  const payload = await response.json();
  const panel = document.querySelector("#tracked-case");
  if (!response.ok) {
    panel.textContent = payload.detail || "Could not find that case.";
    panel.classList.remove("hidden");
    throw new Error(payload.detail || "Could not find that case.");
  }
  const item = payload.case;
  lastCaseId = item.case_id;
  panel.innerHTML = `
    <div class="tracked-case-head"><strong>${escapeHtml(item.case_id)}</strong><span class="status-badge ${statusClass(item.status)}"><i></i>${escapeHtml(statusLabel(item.status))}</span></div>
    <p>${escapeHtml(item.response || item.message || "Your request is being processed.")}</p>
    <small>Last updated ${escapeHtml(new Date(item.updated_at || item.created_at).toLocaleString())}${item.transaction_status ? ` · Transaction: ${escapeHtml(item.transaction_status.replaceAll("_", " "))}` : ""}</small>
    ${renderCaseJourney(item, payload.audit || [])}
    <p class="tracking-explainer">This ID tracks the support case, its current service status, and the agent/audit history. It does not show a plane's live location.</p>
    <button class="text-button" id="tracked-case-audit" type="button">View full audit <b>→</b></button>`;
  panel.classList.remove("hidden");
  panel.querySelector("#tracked-case-audit").addEventListener("click", () => loadAudit(item.case_id));
}

messageInput.addEventListener("input", () => {
  document.querySelector("#char-count").textContent = `${messageInput.value.length} / 4000`;
});

document.querySelectorAll(".sample-chip").forEach((button) => {
  button.addEventListener("click", () => {
    const match = button.dataset.message.match(/\bPNR[A-Z0-9]+\b/);
    const selectedReference = generatedTicket && profileTicketReference
      ? profileTicketReference
      : match ? match[0] : "";
    messageInput.value = selectedReference && match
      ? button.dataset.message.replace(match[0], selectedReference)
      : button.dataset.message;
    if (selectedReference) bookingInput.value = selectedReference;
    messageInput.dispatchEvent(new Event("input"));
    messageInput.focus();
  });
});

async function sendCustomerMessage(message) {
  const userMessage = message.trim();
  if (!userMessage || submitButton.disabled) return;
  if (!activeConversation) {
    conversationLog.replaceChildren();
    resultPanel.classList.add("hidden");
    conversationId = null;
    activeConversation = true;
    const workflowState = document.querySelector("#workflow-state");
    workflowState.classList.remove("failed");
    workflowState.innerHTML = "<i></i> Ready";
    document.querySelectorAll(".agent-row").forEach((row) => {
      row.classList.remove("working", "completed", "failed");
      row.querySelector(".agent-check").textContent = "·";
    });
  }
  addConversationMessage("customer", userMessage);
  messageInput.value = "";
  messageInput.dispatchEvent(new Event("input"));
  submitButton.disabled = true;
  submitButton.innerHTML = '<span class="spinner"></span><span>Thinking…</span>';
  document.querySelector("#agent-activity").classList.remove("hidden");
  document.querySelector("#activity-agent").textContent = "Airline Care";
  document.querySelector("#activity-message").textContent = "Preparing the agent workflow…";
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify({
        message: userMessage,
        channel: channelInput.value,
        booking_reference: bookingInput.value.trim() || null,
        conversation_id: conversationId,
      }),
    });
    await consumeEventStream(response, (event) => {
      if (event.type === "progress") {
        updateAgentProgress(event);
      } else if (event.type === "question") {
        conversationId = event.conversation_id;
        addConversationMessage("agent", event.message);
        finishAgentProgress(true);
        bookingInput.value = "";
      } else if (event.type === "offer") {
        conversationId = event.conversation_id;
        const bubble = addConversationMessage("agent", event.message);
        addConfirmationButtons(bubble);
        renderCase(event.payload);
        renderAudit(event.payload.audit);
        document.querySelector("#audit").classList.remove("hidden");
        finishAgentProgress(true);
      } else if (event.type === "result") {
        renderCase(event.payload);
        renderAudit(event.payload.audit);
        document.querySelector("#audit").classList.remove("hidden");
        const sensitiveHandoff = event.payload.case.escalation?.queue === "sensitive_case_support";
        if (sensitiveHandoff) {
          failAgentProgress();
          showToast("Sensitive request detected. The automated flow stopped and created a specialist handoff.", true);
        } else {
          finishAgentProgress();
        }
        conversationId = null;
        activeConversation = false;
        profileTicketReference = null;
        bookingInput.value = "";
        addConversationMessage(
          "agent",
          sensitiveHandoff
            ? `Safety error: this request contains sensitive content. Automated processing stopped and a specialist handoff was created. ${event.payload.case.response}`
            : event.payload.case.response,
          sensitiveHandoff ? "chat-message-error" : "",
        );
        if (!sensitiveHandoff) {
          showToast(
            event.payload.case.status === "submitted"
              ? "Action submission confirmed by the mock system."
              : event.payload.case.status === "cancelled"
                ? "Request cancelled; no action was taken."
                : "Case processed.",
          );
        }
      } else if (event.type === "error") {
        throw new Error(event.message);
      }
    });
  } catch (error) {
    failAgentProgress();
    activeConversation = false;
    profileTicketReference = null;
    showToast(error.message, true);
    addConversationMessage("agent", error.message);
  } finally {
    submitButton.disabled = false;
    submitButton.innerHTML = "<span>Send to agent</span><b>→</b>";
    messageInput.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendCustomerMessage(messageInput.value).catch((error) => showToast(error.message, true));
});

document.querySelector("#track-case-form").addEventListener("submit", (event) => {
  event.preventDefault();
  trackCase(document.querySelector("#track-case-reference").value)
    .catch((error) => showToast(error.message, true));
});
document.querySelector("#generate-ticket").addEventListener("click", generateDemoTicket);
document.querySelector("#profile-button").addEventListener("click", showProfileDialog);
document.querySelector("#profile-close").addEventListener("click", () => {
  document.querySelector("#profile-dialog").close();
});
document.querySelector("#profile-skip").addEventListener("click", () => {
  document.querySelector("#profile-dialog").close();
});
document.querySelector("#profile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (activeConversation) {
    showToast("Finish the current conversation before changing your demo profile.", true);
    return;
  }
  const updatedProfile = {
    firstName: document.querySelector("#profile-first-name").value.trim(),
    lastName: document.querySelector("#profile-last-name").value.trim(),
    email: document.querySelector("#profile-email-input").value.trim(),
  };
  if (!updatedProfile.firstName || !updatedProfile.lastName || !updatedProfile.email) {
    showToast("Enter a first name, last name, and email address.", true);
    return;
  }
  demoProfile = updatedProfile;
  try {
    localStorage.setItem(profileStorageKey, JSON.stringify(demoProfile));
  } catch {
    showToast("The profile is active for this page but could not be saved in browser storage.", true);
  }
  generatedTicket = null;
  profileTicketReference = null;
  document.querySelector("#demo-ticket").classList.add("hidden");
  renderProfile();
  if (!demoProfile) showProfileDialog();
  document.querySelector("#profile-dialog").close();
});
document.querySelector("#audit-nav").addEventListener("click", (event) => {
  event.preventDefault();
  loadAudit(lastCaseId);
});
document.querySelector("#help-button").addEventListener("click", () => document.querySelector("#info-dialog").showModal());
document.querySelector("#dialog-close").addEventListener("click", () => document.querySelector("#info-dialog").close());
document.querySelector("#dialog-ok").addEventListener("click", () => document.querySelector("#info-dialog").close());

async function loadModelConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) throw new Error("Could not load model configuration.");
  const { model } = await response.json();
  document.querySelector("#model-badge").textContent = model.label;
  document.querySelector("#model-name").textContent = model.label;
  const setupNote = document.querySelector("#model-setup-note");
  if (model.setup_required) {
    setupNote.textContent = model.configuration_error
      || "Add your IBM ICA key and model details to .env.ica, then restart to enable all five model agents.";
    setupNote.classList.remove("hidden");
  } else {
    setupNote.classList.add("hidden");
  }
  for (const agent of model.agents || []) {
    const row = document.querySelector(`.agent-row[data-agent-id="${agent.id}"]`);
    if (!row) continue;
    const assignment = row.querySelector("[data-agent-model]");
    assignment.textContent = agent.model
      ? `${agent.model} · ${agent.mode}`
      : agent.mode;
  }
}

loadModelConfig().catch((error) => showToast(error.message, true));
renderProfile();
