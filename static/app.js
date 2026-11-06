// Runs the web page: logging in and out, showing the assets that match the filters in the
// chosen order, adding, editing and deleting assets, and creating accounts.
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

// The last heading is for the Edit and Delete buttons, and isn't sortable.
// Only the main table's headings: the review table has its own, which don't sort.
const headings = [...tableBody.closest("table").querySelectorAll("thead th")].slice(0, COLUMNS.length);
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
const editPanel = document.getElementById("edit-panel");
const addDialog = document.getElementById("add-dialog");
const editHeading = document.getElementById("edit-heading");
const editForm = document.getElementById("edit-form");
const editButton = document.getElementById("edit-button");
const editCancel = document.getElementById("edit-cancel");

const currentRole = document.getElementById("current-role");
const accountToggle = document.getElementById("account-toggle");
const accountPopover = document.getElementById("account-popover");
const manageAccountsButton = document.getElementById("manage-accounts");
const accountsDialog = document.getElementById("accounts-dialog");
const accountsClose = document.getElementById("accounts-close");
const accountsMessage = document.getElementById("accounts-message");
const accountList = document.getElementById("account-list");
const checkoutDialog = document.getElementById("checkout-dialog");
const checkoutForm = document.getElementById("checkout-form");
const historyDialog = document.getElementById("history-dialog");
const reviewTable = document.getElementById("review-table");
const reviewSelectAll = document.getElementById("review-select-all");

const ROLE_DESCRIPTIONS = {
  admin: "Admin: can also delete assets and manage accounts",
  user: "User: can view, add and edit assets",
};

const EDITABLE_FIELDS = ["description", "category", "location", "status"];

// What the logged-in account may do. The server checks this on every request, so hiding
// buttons here only keeps the page tidy. It isn't what stops a user deleting.
let role = "";
let editingAsset = null; // the asset the edit form is showing, as it was when opened

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
  const borrower = currentLoan(asset) ? currentLoan(asset).borrower : "";
  return [...COLUMNS.map((column) => cellText(asset, column)), borrower]
    .some((text) => text.toLowerCase().includes(wanted));
}

// ---------- Dates ----------

// Dates are handled as YYYY-MM-DD text, the form the servers use. Text in that form sorts
// in date order, so dates can be compared with < and >.
function isoDate(date) {
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function addDays(iso, days) {
  const date = new Date(`${iso}T00:00:00`);
  date.setDate(date.getDate() + days);
  return isoDate(date);
}

function today() {
  return isoDate(new Date());
}

// ---------- Loans ----------

// An asset keeps every loan it has had. Only the last one can still be out.
function currentLoan(asset) {
  const last = asset.loans[asset.loans.length - 1];
  return last && !last.returned ? last : null;
}

function isOverdue(loan) {
  return !loan.returned && loan.due < today();
}

// Sends one change about one asset (approve, check-out or check-in), with the version the
// page was showing, and returns the updated asset, or throws with the server's reason.
async function assetAction(asset, name, details = {}) {
  const response = await fetch(`/api/assets/${encodeURIComponent(asset.asset_id)}/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version: asset.version, ...details }),
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("Please log in again");
  }
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error);
  }
  return body.asset;
}

let checkoutAsset = null; // the asset the check-out form is for

function startCheckOut(asset) {
  checkoutAsset = asset;
  document.getElementById("checkout-heading").textContent = `Check out ${asset.asset_id}: ${asset.description}`;
  document.getElementById("checkout-message").textContent = "";
  checkoutForm.reset();
  checkoutForm.elements.start.value = today();
  checkoutForm.elements.due.value = addDays(today(), 7); // a week, unless changed
  checkoutDialog.showModal();
  checkoutForm.elements.borrower.focus();
}

async function saveCheckOut(event) {
  event.preventDefault();
  const details = Object.fromEntries(new FormData(checkoutForm));
  const button = document.getElementById("checkout-button");
  button.disabled = true;
  try {
    await assetAction(checkoutAsset, "check-out", details);
    checkoutDialog.close();
    showActionResult(`Checked out ${checkoutAsset.asset_id} to ${details.borrower} until ${details.due}`);
    await loadAssets();
  } catch (error) {
    document.getElementById("checkout-message").textContent = `Could not check it out: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function checkIn(asset) {
  const loan = currentLoan(asset);
  if (!confirm(`Check ${asset.asset_id} back in from ${loan.borrower}?`)) {
    return;
  }
  try {
    await assetAction(asset, "check-in");
    showActionResult(`Checked ${asset.asset_id} back in`);
  } catch (error) {
    showActionResult(`Could not check ${asset.asset_id} in: ${error.message}`, true);
  }
  await loadAssets();
}

// Under the status, who has the asset and until when, in red once it is overdue.
function loanNote(asset) {
  const loan = currentLoan(asset);
  if (!loan) {
    return null;
  }
  const note = document.createElement("span");
  note.className = "loan-note";
  note.classList.toggle("overdue", isOverdue(loan));
  note.textContent = isOverdue(loan)
    ? `Overdue: ${loan.borrower}, due ${loan.due}`
    : `On loan to ${loan.borrower} until ${loan.due}`;
  return note;
}

// ---------- History ----------

function showHistory(asset) {
  document.getElementById("history-heading").textContent = `History of ${asset.asset_id}`;
  document.getElementById("history-summary").textContent =
    `${asset.description}, ${asset.category}, at ${asset.location}. ` +
    `${asset.loans.length} loan(s) so far. Newest first.`;
  const entries = [...asset.history].reverse().map((entry) => {
    const item = document.createElement("li");
    const when = document.createElement("time");
    when.dateTime = entry.at;
    when.textContent = new Date(entry.at).toLocaleString();
    const what = document.createElement("span");
    what.className = "history-detail";
    what.textContent = entry.detail;
    const who = document.createElement("span");
    who.className = "history-who";
    who.textContent = `${entry.user ? `by ${entry.user}, ` : ""}on the ${entry.server} server`;
    item.append(when, what, who);
    return item;
  });
  // Assets saved before history was added have none.
  if (!entries.length) {
    const item = document.createElement("li");
    item.textContent = "Nothing has been recorded for this asset yet.";
    entries.push(item);
  }
  document.getElementById("history-list").replaceChildren(...entries);
  historyDialog.showModal();
}

// ---------- Flagged for review ----------

// The flagged assets that are ticked, by ID. Kept between refreshes, so the list redrawing
// every few seconds doesn't untick anything.
let reviewChecked = new Set();

function flaggedAssets() {
  return allAssets.filter((asset) => asset.flagged).sort((a, b) => a.asset_id.localeCompare(b.asset_id));
}

function reviewRow(asset) {
  const row = document.createElement("tr");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = reviewChecked.has(asset.asset_id);
  box.disabled = role !== "admin";
  box.setAttribute("aria-label", `Select ${asset.asset_id}`);
  box.addEventListener("change", () => {
    if (box.checked) {
      reviewChecked.add(asset.asset_id);
    } else {
      reviewChecked.delete(asset.asset_id);
    }
    showReview();
  });
  const cells = [box, asset.asset_id, asset.description, asset.location,
    new Date(asset.created_at).toLocaleString(), asset.history[0] ? `the ${asset.history[0].server} server` : ""];
  for (const content of cells) {
    const cell = document.createElement("td");
    cell.append(content);
    row.append(cell);
  }
  row.cells[0].className = "check-column";
  const actions = document.createElement("td");
  actions.className = "actions";
  if (role === "admin") {
    actions.append(
      actionButton("Approve", "secondary", `Approve ${asset.asset_id}`, () => review([asset], "approve")),
      actionButton("Deny", "delete", `Deny ${asset.asset_id}`, () => review([asset], "deny")),
    );
  }
  row.append(actions);
  return row;
}

function showReview() {
  const flagged = flaggedAssets();
  // The Review page is always there. With nothing flagged it says so instead of vanishing.
  reviewTable.hidden = flagged.length === 0;
  document.getElementById("review-count").textContent = flagged.length;
  document.getElementById("review-count").classList.toggle("waiting", flagged.length > 0);
  // Forget ticks on assets that are no longer flagged.
  reviewChecked = new Set(flagged.map((asset) => asset.asset_id).filter((id) => reviewChecked.has(id)));

  document.getElementById("review-explanation").textContent = flagged.length === 0
    ? "Nothing is waiting for review. An asset appears here when it is added while the primary server is offline."
    : flagged.length === 1
      ? "This asset was added while the primary server was offline. Check it, then approve or deny it."
      : `These ${flagged.length} assets were added while the primary server was offline. Check them, then approve or deny them.`;
  document.getElementById("review-assets").replaceChildren(...flagged.map(reviewRow));

  const isAdmin = role === "admin";
  document.getElementById("review-buttons").hidden = !isAdmin || flagged.length === 0;
  document.getElementById("review-note").hidden = isAdmin || flagged.length === 0;
  reviewSelectAll.disabled = !isAdmin;
  reviewSelectAll.checked = flagged.length > 0 && reviewChecked.size === flagged.length;
  reviewSelectAll.indeterminate = reviewChecked.size > 0 && reviewChecked.size < flagged.length;
  for (const id of ["approve-selected", "deny-selected"]) {
    document.getElementById(id).disabled = reviewChecked.size === 0;
  }
  document.getElementById("approve-selected").textContent = `Approve selected (${reviewChecked.size})`;
  document.getElementById("deny-selected").textContent = `Deny selected (${reviewChecked.size})`;
}

// Approves or denies each asset in turn. Denying deletes, so it asks first. Each one is a
// separate request, so one that fails (say, someone else changed it) doesn't stop the rest.
async function review(assets, decision) {
  if (!assets.length) {
    return;
  }
  if (decision === "deny" &&
      !confirm(`Deny ${assets.length} asset(s)? Denied assets are deleted for good. This can't be undone.`)) {
    return;
  }
  const problems = [];
  for (const asset of assets) {
    try {
      if (decision === "approve") {
        await assetAction(asset, "approve");
      } else {
        await deleteRequest(asset);
      }
      reviewChecked.delete(asset.asset_id);
    } catch (error) {
      problems.push(`${asset.asset_id}: ${error.message}`);
    }
  }
  const done = assets.length - problems.length;
  const verb = decision === "approve" ? "Approved" : "Denied and deleted";
  showActionResult(problems.length
    ? `${verb} ${done} of ${assets.length}. Could not do ${problems.join("; ")}`
    : `${verb} ${done} asset(s)`, problems.length > 0);
  await loadAssets();
}

function selectedFlagged() {
  return flaggedAssets().filter((asset) => reviewChecked.has(asset.asset_id));
}

// ---------- The Loans page ----------

// What state a loan is in, matching the choices in the Loans page's filter.
function loanState(loan) {
  return loan.returned ? "returned" : isOverdue(loan) ? "overdue" : "out";
}

// Every loan of every asset, newest first (then by asset ID, so the order is steady).
function showLoans() {
  const wanted = document.getElementById("loan-filter").value;
  const all = allAssets.flatMap((asset) => asset.loans.map((loan) => ({ asset, loan })));
  const shown = all
    .filter(({ loan }) => !wanted || loanState(loan) === wanted)
    .sort((a, b) => b.loan.start.localeCompare(a.loan.start) || a.asset.asset_id.localeCompare(b.asset.asset_id));

  const rows = shown.map(({ asset, loan }) => {
    const row = document.createElement("tr");
    const state = loanState(loan);
    const last = state === "returned" ? loan.returned : state === "overdue" ? "Overdue" : "Not yet";
    for (const text of [asset.asset_id, asset.description, loan.borrower, loan.start, loan.due, last]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    row.cells[5].className = `loan-${state}`;
    row.className = "clickable";
    row.tabIndex = 0;
    row.addEventListener("click", () => showHistory(asset));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        showHistory(asset);
      }
    });
    return row;
  });
  document.getElementById("loans").replaceChildren(...rows);
  const overdue = all.filter(({ loan }) => loanState(loan) === "overdue").length;
  document.getElementById("loans-message").textContent =
    `Showing ${shown.length} of ${all.length} loan(s), ${overdue} overdue`;
}

// ---------- Loans calendar ----------

// The first day of the month the calendar shows.
let calendarMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);

// Each loan that overlaps the month, with the asset it belongs to. A loan still out ends
// on its due date, or today if it is overdue. A returned one ends when it came back.
function loansInMonth(assets, first, last) {
  const found = [];
  for (const asset of assets) {
    for (const loan of asset.loans) {
      const end = loan.returned || (isOverdue(loan) ? today() : loan.due);
      if (loan.start <= last && end >= first) {
        found.push({ asset, loan, end });
      }
    }
  }
  return found.sort((a, b) => a.loan.start.localeCompare(b.loan.start));
}

function showCalendar(assets) {
  const year = calendarMonth.getFullYear();
  const month = calendarMonth.getMonth();
  const days = new Date(year, month + 1, 0).getDate();
  const first = isoDate(new Date(year, month, 1));
  const last = isoDate(new Date(year, month, days));
  document.getElementById("calendar-month").textContent =
    calendarMonth.toLocaleDateString(undefined, { month: "long", year: "numeric" });

  // One column for the names, then one for each day of the month.
  const calendar = document.getElementById("calendar");
  calendar.style.setProperty("--days", days);

  const header = document.createElement("div");
  header.className = "calendar-row calendar-days";
  header.append(document.createElement("span"));
  for (let day = 1; day <= days; day += 1) {
    const date = new Date(year, month, day);
    const cell = document.createElement("span");
    cell.textContent = day;
    cell.classList.toggle("weekend", date.getDay() === 0 || date.getDay() === 6);
    cell.classList.toggle("today", isoDate(date) === today());
    header.append(cell);
  }

  const rows = loansInMonth(assets, first, last).map(({ asset, loan, end }) => {
    const row = document.createElement("div");
    row.className = "calendar-row";
    const label = document.createElement("span");
    label.className = "calendar-label";
    label.textContent = `${asset.asset_id} · ${loan.borrower}`;
    label.title = asset.description;

    // The bar runs from the first to the last of its days that fall in this month.
    const startDay = loan.start < first ? 1 : Number(loan.start.slice(8));
    const endDay = end > last ? days : Number(end.slice(8));
    const bar = document.createElement("button");
    bar.type = "button";
    bar.className = "calendar-bar";
    bar.classList.add(loan.returned ? "returned" : isOverdue(loan) ? "overdue" : "on-loan");
    bar.style.gridColumn = `${startDay + 1} / ${endDay + 2}`;
    bar.textContent = asset.description;
    bar.title = `${asset.asset_id} ${asset.description}: ${loan.borrower}, ${loan.start} to ${loan.due}` +
      (loan.returned ? `, returned ${loan.returned}` : isOverdue(loan) ? ", overdue" : "");
    bar.addEventListener("click", () => showHistory(asset));
    row.append(label, bar);
    return row;
  });
  calendar.replaceChildren(header, ...rows);
  document.getElementById("calendar-empty").hidden = rows.length > 0;
}

function moveCalendar(months) {
  calendarMonth = months === 0
    ? new Date(new Date().getFullYear(), new Date().getMonth(), 1)
    : new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + months, 1);
  showAssets();
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

// ---- The account menu, at the top right ----

function initialOf(name) {
  return (name || "?").charAt(0).toUpperCase();
}

function showAccount(username, accountRole) {
  role = accountRole;
  currentUser.textContent = username;
  currentRole.textContent = accountRole;
  currentRole.classList.toggle("admin", accountRole === "admin");
  document.getElementById("account-avatar").textContent = initialOf(username);
  document.getElementById("popover-avatar").textContent = initialOf(username);
  document.getElementById("popover-user").textContent = username;
  document.getElementById("popover-role").textContent = ROLE_DESCRIPTIONS[accountRole] || accountRole;
  manageAccountsButton.hidden = accountRole !== "admin";
}

function setMenuOpen(open) {
  accountPopover.hidden = !open;
  accountToggle.setAttribute("aria-expanded", String(open));
}

// A click anywhere else, or Escape, closes the menu, as menus usually behave.
document.addEventListener("click", (event) => {
  if (!accountPopover.hidden && !event.target.closest(".account-menu")) {
    setMenuOpen(false);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !accountPopover.hidden) {
    setMenuOpen(false);
    accountToggle.focus();
  }
});

// ---- Managing accounts, in a window of its own ----

function showAccountsMessage(text, isProblem = false) {
  accountsMessage.textContent = text;
  accountsMessage.classList.toggle("problem", isProblem);
}

// Sends one change to the accounts, returning the reply's body, or throwing with the
// server's explanation. A lost login goes back to the login form.
async function accountsRequest(path, method, body) {
  const options = { method };
  if (body) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 401) {
    showLogin();
    throw new Error("Please log in again");
  }
  const reply = await response.json();
  if (!response.ok) {
    throw new Error(reply.error);
  }
  return reply;
}

function accountRow(account) {
  const isMe = account.username === currentUser.textContent;
  const row = document.createElement("li");
  row.className = "account-row";

  const avatar = document.createElement("span");
  avatar.className = "avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = initialOf(account.username);

  const name = document.createElement("span");
  name.className = "account-row-name";
  name.textContent = account.username;
  if (isMe) {
    const you = document.createElement("span");
    you.className = "you";
    you.textContent = "you";
    name.append(" ", you);
  }

  const choice = document.createElement("select");
  choice.setAttribute("aria-label", `Role of ${account.username}`);
  for (const value of ["user", "admin"]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value === "admin" ? "Admin" : "User";
    choice.append(option);
  }
  choice.value = account.role;
  choice.addEventListener("change", () => changeRole(account.username, choice.value));

  row.append(avatar, name, choice);
  if (!isMe) {
    row.append(actionButton("Delete", "delete", `Delete the account ${account.username}`,
      () => deleteAccount(account.username)));
  }
  return row;
}

async function loadAccounts() {
  try {
    const body = await accountsRequest("/api/users", "GET");
    accountList.replaceChildren(...body.accounts.map(accountRow));
  } catch (error) {
    showAccountsMessage(`Could not load the accounts: ${error.message}`, true);
  }
}

async function openAccounts() {
  setMenuOpen(false);
  showAccountsMessage("");
  accountForm.reset();
  accountsDialog.showModal();
  await loadAccounts();
}

// Changing your own role to user takes away the right to manage accounts, so the window
// closes and the page is redrawn for a user.
async function changeRole(username, newRole) {
  try {
    await accountsRequest(`/api/users/${encodeURIComponent(username)}`, "PATCH", { role: newRole });
    showAccountsMessage(`${username} is now ${newRole === "admin" ? "an admin" : "a user"}`);
    if (username === currentUser.textContent && newRole !== "admin") {
      accountsDialog.close();
      await loadAssets();
      return;
    }
  } catch (error) {
    showAccountsMessage(`Could not change ${username}'s role: ${error.message}`, true);
  }
  await loadAccounts();
}

async function deleteAccount(username) {
  if (!confirm(`Delete the account ${username}? They will be logged out straight away.`)) {
    return;
  }
  try {
    await accountsRequest(`/api/users/${encodeURIComponent(username)}`, "DELETE");
    showAccountsMessage(`Deleted the account ${username}`);
  } catch (error) {
    showAccountsMessage(`Could not delete ${username}: ${error.message}`, true);
  }
  await loadAccounts();
}

async function createAccount(event) {
  event.preventDefault();
  const details = Object.fromEntries(new FormData(accountForm));
  if (details.password !== details.confirm) {
    showAccountsMessage("Could not create the account: the two passwords don't match", true);
    return;
  }

  accountButton.disabled = true;
  try {
    const body = await accountsRequest("/api/users", "POST",
      { username: details.username, password: details.password, role: details.role });
    accountForm.reset();
    showAccountsMessage(`Created the account ${body.username}, as ${body.role === "admin" ? "an admin" : "a user"}`);
    await loadAccounts();
  } catch (error) {
    showAccountsMessage(`Could not create the account: ${error.message}`, true);
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

function actionButton(text, className, label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", action);
  return button;
}

function actionsCell(asset) {
  const cell = document.createElement("td");
  cell.className = "actions";
  if (currentLoan(asset)) {
    cell.append(actionButton("Check in", "secondary", `Check in ${asset.asset_id}`, () => checkIn(asset)));
  } else {
    cell.append(actionButton("Check out", "secondary", `Check out ${asset.asset_id}`, () => startCheckOut(asset)));
  }
  cell.append(actionButton("Edit", "edit secondary", `Edit ${asset.asset_id}`, () => startEdit(asset)));
  if (role === "admin") {
    cell.append(actionButton("Delete", "delete", `Delete ${asset.asset_id}`, () => deleteAsset(asset)));
  }
  return cell;
}

// The form starts with the asset as this page last showed it. Saving sends only the fields
// that were changed, with that version, so the server refuses if someone else has changed
// the asset since.
function startEdit(asset) {
  editingAsset = asset;
  editHeading.textContent = `Edit ${asset.asset_id}`;
  for (const field of EDITABLE_FIELDS) {
    editForm.elements[field].value = asset[field];
  }
  editPanel.showModal();
  editForm.elements.description.focus();
}

// Closing the window by any route, including Escape, forgets the asset being edited.
function stopEdit() {
  if (editPanel.open) {
    editPanel.close();
  }
}

editPanel.addEventListener("close", () => {
  editingAsset = null;
  editForm.reset();
  document.getElementById("edit-message").textContent = "";
});

async function saveEdit(event) {
  event.preventDefault();
  const asset = editingAsset;
  const values = Object.fromEntries(new FormData(editForm));
  const changes = {};
  for (const field of EDITABLE_FIELDS) {
    // An older asset may have a status that isn't in the list. The list then shows
    // nothing, and the status is left as it is unless one is chosen.
    const value = (values[field] ?? "").trim();
    if (value !== asset[field] && !(field === "status" && !value)) {
      changes[field] = value;
    }
  }
  if (Object.keys(changes).length === 0) {
    stopEdit();
    showActionResult("Nothing was changed");
    return;
  }

  editButton.disabled = true;
  try {
    const response = await fetch(`/api/assets/${encodeURIComponent(asset.asset_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: asset.version, changes }),
    });
    if (response.status === 401) {
      showLogin();
      return;
    }
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error);
    }
    stopEdit();
    showActionResult(`Saved the changes to ${asset.asset_id}`);
  } catch (error) {
    document.getElementById("edit-message").textContent =
      `Could not save the changes to ${asset.asset_id}: ${error.message}`;
  } finally {
    editButton.disabled = false;
  }
  await loadAssets();
}

// The version is the one this page was showing, so if someone else has changed the asset
// since, the server refuses, and the refresh below shows what is there now.
// Deletes one asset, with the version the page was showing, or throws with the reason.
async function deleteRequest(asset) {
  const response = await fetch(`/api/assets/${encodeURIComponent(asset.asset_id)}?version=${asset.version}`, {
    method: "DELETE",
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("Please log in again");
  }
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error);
  }
}

async function deleteAsset(asset) {
  if (!confirm(`Delete ${asset.asset_id} (${asset.description}) for good? This can't be undone.`)) {
    return;
  }

  try {
    await deleteRequest(asset);
    showActionResult(`Deleted ${asset.asset_id}`);
  } catch (error) {
    showActionResult(`Could not delete ${asset.asset_id}: ${error.message}`, true);
  }
  await loadAssets();
}

// The totals count the assets the search and status filter match, so searching "switch"
// shows how many switches there are. During an outage the list on screen is an old one,
// so nothing is named as having answered.
function showStats(visible) {
  const withStatus = (status) => visible.filter((asset) => asset.status === status).length;
  const filtering = search.value.trim() !== "" || statusFilter.value !== "";
  document.getElementById("stat-total-label").textContent = filtering ? "Matching assets" : "Total assets";
  document.getElementById("stat-total").textContent = visible.length;
  document.getElementById("stat-in-use").textContent = withStatus("In Use");
  document.getElementById("stat-not-in-use").textContent = withStatus("Not In Use");
  document.getElementById("stat-on-loan").textContent = visible.filter(currentLoan).length;

  const server = document.getElementById("stat-server");
  server.textContent = loadError || !servedBy ? "No server" : servedBy;
  server.classList.toggle("server-primary", !loadError && servedBy === "primary");
  // Any backup answering (backup, backup2 and so on) means the primary is down.
  server.classList.toggle("server-backup", !loadError && servedBy.startsWith("backup"));
  server.classList.toggle("server-none", loadError !== "" || !servedBy);
}

function showMessage(visible) {
  showStats(visible);
  const counts = `Showing ${visible.length} of ${allAssets.length} asset(s)`;
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
        const note = loanNote(asset);
        if (note) {
          cell.append(note);
        }
      } else if (column === "asset_id" && asset.flagged) {
        const marker = document.createElement("span");
        marker.className = "review-marker";
        marker.textContent = "Needs review";
        cell.append(asset.asset_id, marker);
      } else if (column === "tags") {
        cell.className = "tags";
        cell.append(...asset.tags.map((tag) => {
          const chip = document.createElement("span");
          chip.className = "tag";
          chip.textContent = tag;
          return chip;
        }));
      } else {
        cell.textContent = cellText(asset, column);
      }
      row.append(cell);
    }
    row.append(actionsCell(asset));

    // Clicking a row (but not one of its buttons) or pressing Enter on it shows the history.
    row.className = "clickable";
    row.tabIndex = 0;
    row.addEventListener("click", (event) => {
      if (!event.target.closest("button")) {
        showHistory(asset);
      }
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target === row) {
        showHistory(asset);
      }
    });
    return row;
  });
  tableBody.replaceChildren(...rows);
  showMessage(visible);
  showReview();
  showLoans();
  showCalendar(allAssets);
}

// ---------- Pages ----------

// The page is chosen by the address after the #, so a page can be bookmarked and the
// browser's back button moves between pages. Anything unknown means the first page.
const PAGES = ["assets", "loans", "calendar", "review"];

function showPage() {
  const wanted = location.hash.slice(1);
  const page = PAGES.includes(wanted) ? wanted : PAGES[0];
  for (const section of document.querySelectorAll("[data-page]")) {
    section.hidden = section.dataset.page !== page;
  }
  for (const tab of document.querySelectorAll("[data-tab]")) {
    if (tab.dataset.tab === page) {
      tab.setAttribute("aria-current", "page");
    } else {
      tab.removeAttribute("aria-current");
    }
  }
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
  stopEdit();
  showActionResult("");
  tableBody.replaceChildren();
  appSection.hidden = true;
  account.hidden = true;
  setMenuOpen(false);
  for (const dialog of document.querySelectorAll("dialog[open]")) {
    dialog.close();
  }
  showAccount("", "");
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

// Results appear as a small notice in the corner, which goes by itself after a while.
let actionTimer = null;
function showActionResult(text, isProblem = false) {
  clearTimeout(actionTimer);
  actionMessage.textContent = text;
  actionMessage.classList.toggle("problem", isProblem);
  if (text) {
    actionTimer = setTimeout(() => showActionResult(""), isProblem ? 9000 : 5000);
  }
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
    addDialog.close();
    showActionResult(`Added ${body.asset.asset_id}`);
    await loadAssets();
  } catch (error) {
    document.getElementById("add-message").textContent = `Could not add the asset: ${error.message}`;
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
    showAccount(body.username, body.role);
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
document.getElementById("loan-filter").addEventListener("change", showLoans);

// There is nothing to submit, so pressing Enter must not reload the page.
filters.addEventListener("submit", (event) => event.preventDefault());

loginForm.addEventListener("submit", logIn);
logoutButton.addEventListener("click", logOut);
addForm.addEventListener("submit", addAsset);
accountForm.addEventListener("submit", createAccount);
accountToggle.addEventListener("click", () => setMenuOpen(accountPopover.hidden));
manageAccountsButton.addEventListener("click", openAccounts);
accountsClose.addEventListener("click", () => accountsDialog.close());
document.getElementById("open-add").addEventListener("click", () => {
  document.getElementById("add-message").textContent = "";
  addDialog.showModal();
  addForm.elements.description.focus();
});

// Every Cancel and close button in a window closes the window it is in.
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}
editForm.addEventListener("submit", saveEdit);
checkoutForm.addEventListener("submit", saveCheckOut);
document.getElementById("calendar-prev").addEventListener("click", () => moveCalendar(-1));
document.getElementById("calendar-next").addEventListener("click", () => moveCalendar(1));
document.getElementById("calendar-today").addEventListener("click", () => moveCalendar(0));
reviewSelectAll.addEventListener("change", () => {
  reviewChecked = new Set(reviewSelectAll.checked ? flaggedAssets().map((asset) => asset.asset_id) : []);
  showReview();
});
document.getElementById("approve-selected").addEventListener("click", () => review(selectedFlagged(), "approve"));
document.getElementById("approve-all").addEventListener("click", () => review(flaggedAssets(), "approve"));
document.getElementById("deny-selected").addEventListener("click", () => review(selectedFlagged(), "deny"));
document.getElementById("deny-all").addEventListener("click", () => review(flaggedAssets(), "deny"));
editCancel.addEventListener("click", stopEdit);

makeHeadingsSortable();
window.addEventListener("hashchange", showPage);
showPage();
refreshRepeatedly();
