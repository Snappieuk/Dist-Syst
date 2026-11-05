// Fetches the assets from the web program and shows them in the table.
//
// Asset details are typed in by people, so they are only ever added to the page as
// plain text (textContent), never as HTML. A description containing a <script> tag is
// therefore displayed as those characters, and cannot run.

const COLUMNS = ["asset_id", "description", "category", "location", "status", "tags", "date_registered"];

const tableBody = document.getElementById("assets");
const message = document.getElementById("message");

function cellText(asset, column) {
  return column === "tags" ? asset.tags.join(", ") : asset[column];
}

function showAssets(assets) {
  const rows = assets.map((asset) => {
    const row = document.createElement("tr");
    for (const column of COLUMNS) {
      const cell = document.createElement("td");
      cell.textContent = cellText(asset, column);
      row.append(cell);
    }
    return row;
  });
  tableBody.replaceChildren(...rows);
  message.textContent = `${assets.length} asset(s)`;
}

async function loadAssets() {
  try {
    const response = await fetch("/api/assets");
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error);
    }
    showAssets(body.assets);
  } catch (error) {
    message.textContent = `Could not load the assets: ${error.message}`;
  }
}

loadAssets();
