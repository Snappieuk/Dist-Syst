// Runs the web page: logging in and out, showing the assets that match the filters in the
// chosen order, adding and deleting assets, and creating accounts.
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

// The last heading is for the Delete buttons, and isn't sortable.
const headings = [...document.querySelectorAll("thead th")].slice(0, COLUMNS.length);
const loginSection = document.getElementById("login-section");
const appSection = document.getElementById("app-section");
const loginForm = document.getElementById("login-form");
const username = document.getElementById("username");
const password = document.getElementById("password");
const loginError = document.getElementById("login-error");
const logoutButton = document.getElementById("logout");
const account = document.getElementById("account");
const currentUser = document.getElementById("current-user");
const addForm = document.getElementById("add-form");
const addButton = document.getElementById("add-button");
const actionMessage = document.getElementById("action-message");
const accountForm = document.getElementById("account-form");
const accountButton = document.getElementById("account-button");

let allAssets = [];
let servedBy = "";
let loadError = "";
let unconfirmedAdd = null; // see addAsset()
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

async function createAccount(event) {
  event.preventDefault();
  const details = Object.fromEntries(new FormData(accountForm));
  if (details.password !== details.confirm) {
    showActionResult("Could not create the account: the two passwords don't match", true);
    return;
  }

  accountButton.disabled = true;
  try {
    const response = await fetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: details.username, password: details.password }),
    });
    if (response.status === 401) {
      showLogin();
      return;
    }
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error);
    }
    accountForm.reset();
    showActionResult(`Created the account ${body.username}`);
  } catch (error) {
    showActionResult(`Could not create the account: ${error.message}`, true);
  } finally {
    accountButton.disabled = false;
  }
}

// Status is free text, so only the two standard values get their own colour.
function statusBadge(status) {
  const badge = document.createElement("span");
  badge.className = "status-badge";
  badge.classList.toggle("in-use", status === "In Use");
  badge.classList.toggle("not-in-use", status === "Not In Use");
  badge.textContent = status;
  return badge;
}

function deleteCell(asset) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "delete";
  button.textContent = "Delete";
  button.setAttribute("aria-label", `Delete ${asset.asset_id}`);
  button.addEventListener("click", () => deleteAsset(asset));

  const cell = document.createElement("td");
  cell.append(button);
  return cell;
}

// The version is the one this page was showing, so if someone else has changed the asset
// since, the server refuses, and the refresh below shows what is there now.
async function deleteAsset(asset) {
  if (!confirm(`Delete ${asset.asset_id} (${asset.description}) for good? This can't be undone.`)) {
    return;
  }

  try {
    const response = await fetch(`/api/assets/${encodeURIComponent(asset.asset_id)}?version=${asset.version}`, {
      method: "DELETE",
    });
    if (response.status === 401) {
      showLogin();
      return;
    }
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error);
    }
    showActionResult(`Deleted ${asset.asset_id}`);
  } catch (error) {
    showActionResult(`Could not delete ${asset.asset_id}: ${error.message}`, true);
  }
  await loadAssets();
}

// The totals cover every asset, whatever the filters are showing. During an outage the
// list on screen is an old one, so nothing is named as having answered.
function showStats() {
  const withStatus = (status) => allAssets.filter((asset) => asset.status === status).length;
  document.getElementById("stat-total").textContent = allAssets.length;
  document.getElementById("stat-in-use").textContent = withStatus("In Use");
  document.getElementById("stat-not-in-use").textContent = withStatus("Not In Use");

  const server = document.getElementById("stat-server");
  server.textContent = loadError || !servedBy ? "No server" : servedBy;
  server.classList.toggle("server-backup", !loadError && servedBy === "backup");
  server.classList.toggle("problem", loadError !== "");
}

function showMessage(visibleCount) {
  showStats();
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
      if (column === "status") {
        cell.append(statusBadge(asset.status));
      } else {
        cell.textContent = cellText(asset, column);
      }
      row.append(cell);
    }
    row.append(deleteCell(asset));
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

// Clears the assets from the page as well as hiding them, so that nothing is left behind
// after logging out or when a login has expired.
function showLogin() {
  allAssets = [];
  servedBy = "";
  loadError = "";
  unconfirmedAdd = null;
  addForm.reset();
  accountForm.reset();
  showActionResult("");
  tableBody.replaceChildren();
  appSection.hidden = true;
  account.hidden = true;
  currentUser.textContent = "";
  loginSection.hidden = false;
  username.focus();
}

function showApp() {
  loginSection.hidden = true;
  appSection.hidden = false;
  account.hidden = false;
}

async function logIn(event) {
  event.preventDefault();
  loginError.textContent = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error);
    }
    password.value = "";
    await loadAssets();
  } catch (error) {
    loginError.textContent = error.message;
  }
}

async function logOut() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } finally {
    showLogin();
  }
}

function showActionResult(text, isProblem = false) {
  actionMessage.textContent = text;
  actionMessage.classList.toggle("problem", isProblem);
}

function newRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

// If the last attempt failed, the server may still have saved it, with only the reply
// being lost. Trying again with the same details sends the same request ID, which cannot
// create a second copy of the asset.
async function addAsset(event) {
  event.preventDefault();
  const asset = Object.fromEntries(new FormData(addForm));
  if (!unconfirmedAdd || JSON.stringify(unconfirmedAdd.asset) !== JSON.stringify(asset)) {
    unconfirmedAdd = { asset, request_id: newRequestId() };
  }

  addButton.disabled = true;
  try {
    const response = await fetch("/api/assets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(unconfirmedAdd),
    });
    if (response.status === 401) {
      showLogin();
      return;
    }
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error);
    }
    unconfirmedAdd = null;
    addForm.reset();
    showActionResult(`Added ${body.asset.asset_id}`);
    await loadAssets();
  } catch (error) {
    showActionResult(`Could not add the asset: ${error.message}`, true);
  } finally {
    addButton.disabled = false;
  }
}

// A failed refresh keeps the last list on screen, with a warning, because old data is
// more useful than none during an outage.
async function loadAssets() {
  try {
    const response = await fetch("/api/assets");
    if (response.status === 401) {
      showLogin();
      return;
    }
    showApp();
    const body = await response.json();
    currentUser.textContent = body.username;
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
// cannot build up a queue of requests. A hidden browser tab skips the work, and so does
// the login form, which has nothing to show until someone logs in.
async function refreshRepeatedly() {
  if (!document.hidden && loginSection.hidden) {
    await loadAssets();
  }
  setTimeout(refreshRepeatedly, REFRESH_MILLISECONDS);
}

search.addEventListener("input", showAssets);
statusFilter.addEventListener("change", showAssets);

// There is nothing to submit, so pressing Enter must not reload the page.
filters.addEventListener("submit", (event) => event.preventDefault());

loginForm.addEventListener("submit", logIn);
logoutButton.addEventListener("click", logOut);
addForm.addEventListener("submit", addAsset);
accountForm.addEventListener("submit", createAccount);

makeHeadingsSortable();
refreshRepeatedly();
