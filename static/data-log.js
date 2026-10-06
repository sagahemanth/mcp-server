const kinds = {
  flights: {
    title: "Generated flights",
    file: "flights",
    columns: [
      ["logged_at", "Saved at"],
      ["record_ref", "Record"],
      ["flight", "Flight"],
      ["origin", "Origin"],
      ["destination", "Destination"],
      ["travel_date", "Travel date"],
      ["flight_status", "Status"],
      ["fare_type", "Fare"],
      ["boarding_pass_status", "Boarding pass"],
    ],
  },
  cases: {
    title: "Case status updates",
    file: "cases",
    columns: [
      ["logged_at", "Saved at"],
      ["record_ref", "Record"],
      ["intent", "Request type"],
      ["status", "Case status"],
      ["channel", "Channel"],
      ["transaction_status", "Transaction"],
      ["policy", "Policy"],
      ["escalation_queue", "Escalation queue"],
    ],
  },
  actions: {
    title: "Workflow and audit events",
    file: "actions",
    columns: [
      ["logged_at", "Saved at"],
      ["record_ref", "Record"],
      ["agent", "Agent"],
      ["action", "Action"],
      ["outcome", "Outcome"],
    ],
  },
};

let history = null;
let activeKind = "flights";

const escapeHtml = (value = "") =>
  String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);

function renderActiveTable() {
  if (!history) return;
  const configuration = kinds[activeKind];
  const records = history[activeKind] || [];
  document.querySelector("#log-heading").textContent = configuration.title;
  document.querySelector("#download-csv").href = `/api/data-log/${configuration.file}.csv`;
  const headers = configuration.columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
  const rows = records.map((record) => `
    <tr>${configuration.columns.map(([key]) => {
      const value = record[key] || "—";
      return `<td>${escapeHtml(value.replaceAll("_", " "))}</td>`;
    }).join("")}</tr>`).join("");
  const empty = `<tr><td class="data-empty" colspan="${configuration.columns.length}">No ${escapeHtml(
    configuration.title.toLowerCase(),
  )} have been recorded yet.</td></tr>`;
  document.querySelector("#data-table-wrap").innerHTML = `
    <table class="data-table">
      <thead><tr>${headers}</tr></thead>
      <tbody>${rows || empty}</tbody>
    </table>`;
}

async function loadHistory() {
  const response = await fetch("/api/data-log");
  if (!response.ok) throw new Error("Could not load saved demo data.");
  history = await response.json();
  document.querySelector("#flight-total").textContent = history.counts.flights;
  document.querySelector("#case-total").textContent = history.counts.cases;
  document.querySelector("#action-total").textContent = history.counts.actions;
  document.querySelector("#data-privacy").textContent = history.privacy;
  renderActiveTable();
}

document.querySelectorAll(".data-tab").forEach((button) => {
  button.addEventListener("click", () => {
    activeKind = button.dataset.kind;
    document.querySelectorAll(".data-tab").forEach((tab) => {
      const selected = tab === button;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-selected", String(selected));
    });
    renderActiveTable();
  });
});

document.querySelector("#refresh-data").addEventListener("click", () => {
  loadHistory().catch((error) => {
    document.querySelector("#data-privacy").textContent = error.message;
  });
});

loadHistory().catch((error) => {
  document.querySelector("#data-privacy").textContent = error.message;
});
