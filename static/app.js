// Fetches the assets from the web program, and shows those matching the filters in the
// chosen order.
//
// Asset details are typed in by people, so they are only ever added to the page as
// plain text (textContent), never as HTML. A description containing a <script> tag is
// therefore displayed as those characters, and cannot run.

const COLUMNS = ["asset_id", "description", "category", "location", "status", "tags", "date_registered"];

// How often to fetch the list again, so that changes and failovers appear by themselves.
const REFRESH_MILLISECONDS = 5000;

const tableBody = document.getElementById("assets");
const message = document.getElementById("message");
const filters = document.getElementById("filters");
const search = document.getElementById("search");
const statusFilter = document.getElementById("status-filter");

const headings = document.querySelectorAll("thead th");

let allAssets = [];
let servedBy = "";
let loadError = "";
let sortColumn = "asset_id";
let sortDirection = 1; // 1 for ascending, -1 for descending

function cellText(asset, column) {
  return column === "tags" ? asset.tags.join(", ") : asset[column];
}

function matchesFilters(asset) {
  if (statusFilter.value && asset.status !== statusFilter.value) {
    return false;
  }
  const wanted = search.value.trim().toLowerCase();
  return COLUMNS.some((column) => cellText(asset, column).toLowerCase().includes(wanted));
}

// numeric makes "AST-10" sort after "AST-9", and sensitivity ignores capitals.
function compareAssets(first, second) {
  const order = cellText(first, sortColumn).localeCompare(cellText(second, sortColumn), undefined, {
    numeric: true,
    sensitivity: "base",
  });
  return order * sortDirection;
}

function showSortOrder() {
  headings.forEach((heading, index) => {
    if (COLUMNS[index] === sortColumn) {
      heading.setAttribute("aria-sort", sortDirection === 1 ? "ascending" : "descending");
    } else {
      heading.removeAttribute("aria-sort");
    }
  });
}

function sortBy(column) {
  sortDirection = column === sortColumn ? -sortDirection : 1;
  sortColumn = column;
  showSortOrder();
  showAssets();
}

// Each heading becomes a button, so that sorting also works from a keyboard.
function makeHeadingsSortable() {
  headings.forEach((heading, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = heading.textContent;
    button.addEventListener("click", () => sortBy(COLUMNS[index]));
    heading.replaceChildren(button);
  });
  showSortOrder();
}

function showMessage(visibleCount) {
  const counts = `Showing ${visibleCount} of ${allAssets.length} asset(s)`;
  message.classList.toggle("problem", loadError !== "");

  if (!loadError) {
    message.textContent = `${counts}, from the ${servedBy} server`;
  } else if (servedBy) {
    message.textContent = `Could not refresh the list: ${loadError}. ${counts}, as last received.`;
  } else {
    message.textContent = `Could not load the assets: ${loadError}`;
  }
}

function showAssets() {
  const visible = allAssets.filter(matchesFilters).sort(compareAssets);
  const rows = visible.map((asset) => {
    const row = document.createElement("tr");
    for (const column of COLUMNS) {
      const cell = document.createElement("td");
      cell.textContent = cellText(asset, column);
      row.append(cell);
    }
    return row;
  });
  tableBody.replaceChildren(...rows);
  showMessage(visible.length);
}

// Status is free text, so the choices come from the assets themselves. The current
// choice is kept even if no asset has that status any more.
function updateStatusChoices() {
  const chosen = statusFilter.value;
  const statuses = new Set(allAssets.map((asset) => asset.status));
  if (chosen) {
    statuses.add(chosen);
  }

  const options = [...statuses].sort().map((status) => {
    const option = document.createElement("option");
    option.value = status;
    option.textContent = status;
    return option;
  });
  statusFilter.replaceChildren(statusFilter.options[0], ...options);
  statusFilter.value = chosen;
}

// A failed refresh keeps the last list on screen, with a warning, because old data is
// more useful than none during an outage.
async function loadAssets() {
  try {
    const response = await fetch("/api/assets");
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error);
    }
    allAssets = body.assets;
    servedBy = body.served_by;
    loadError = "";
    updateStatusChoices();
  } catch (error) {
    loadError = error.message;
  }
  showAssets();
}

// The next refresh is only scheduled once this one has finished, so that a slow server
// cannot build up a queue of requests. A hidden browser tab skips the work.
async function refreshRepeatedly() {
  if (!document.hidden) {
    await loadAssets();
  }
  setTimeout(refreshRepeatedly, REFRESH_MILLISECONDS);
}

search.addEventListener("input", showAssets);
statusFilter.addEventListener("change", showAssets);

// There is nothing to submit, so pressing Enter must not reload the page.
filters.addEventListener("submit", (event) => event.preventDefault());

makeHeadingsSortable();
refreshRepeatedly();
