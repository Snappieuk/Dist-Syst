// Minimal Node.js client (no dependencies). Speaks only JSON to the server:
// TLS connection, 4-byte big-endian length header, then a UTF-8 JSON object.
//
// Usage:
//   node client.js <username> <password> ping
//   node client.js <username> <password> list
//   node client.js <username> <password> register '{"asset_id":"A1","description":"Laptop","category":"IT","location":"HQ","status":"In Use"}'
//   node client.js <username> <password> edit <asset_id> <expected_version> '{"location":"Lab"}'
//   node client.js <username> <password> delete <asset_id> <expected_version>
const tls = require("tls");
const fs = require("fs");
const crypto = require("crypto");
const path = require("path");

const config = JSON.parse(fs.readFileSync(path.join(__dirname, "config.json"), "utf8"));
const ca = fs.readFileSync(path.join(__dirname, config.certificate_file));

function sendRequest(server, request) {
  return new Promise((resolve, reject) => {
    const body = Buffer.from(JSON.stringify({ ...request, protocol_version: 1 }), "utf8");
    const header = Buffer.alloc(4);
    header.writeUInt32BE(body.length);
    let buf = Buffer.alloc(0);
    const sock = tls.connect(
      { host: server.host, port: server.port, ca, servername: server.host, minVersion: "TLSv1.2" },
      () => sock.write(Buffer.concat([header, body]))
    );
    sock.setTimeout(config.client_timeout_seconds * 1000, () => sock.destroy(new Error("timeout")));
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (buf.length >= 4 && buf.length >= 4 + buf.readUInt32BE(0)) {
        const len = buf.readUInt32BE(0);
        sock.end();
        try {
          resolve(JSON.parse(buf.subarray(4, 4 + len).toString("utf8")));
        } catch (e) {
          reject(e);
        }
      }
    });
    sock.on("error", reject);
  });
}

// Try the primary, then the backup.
async function call(request) {
  let lastError;
  for (const role of ["primary", "backup"]) {
    const server = config.servers[role];
    if (!server) continue;
    try {
      return await sendRequest(server, request);
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError;
}

async function main() {
  const [username, password, command, ...args] = process.argv.slice(2);
  if (!command) {
    console.error("usage: node client.js <username> <password> ping|list|register|edit|delete ...");
    process.exit(2);
  }
  if (command === "ping") {
    console.log(JSON.stringify(await call({ action: "PING" }), null, 2));
    return;
  }
  const login = await call({ action: "LOGIN", username, password });
  if (login.status !== "OK") {
    console.log(JSON.stringify(login, null, 2));
    process.exit(1);
  }
  const token = login.token;
  const id = () => crypto.randomBytes(16).toString("hex");
  let request;
  switch (command) {
    case "list":
      request = { action: "LIST", token };
      break;
    case "register":
      request = { action: "REGISTER", token, request_id: id(), asset: JSON.parse(args[0]) };
      break;
    case "edit":
      request = {
        action: "EDIT", token, request_id: id(),
        asset_id: args[0], expected_version: Number(args[1]), changes: JSON.parse(args[2]),
      };
      break;
    case "delete":
      request = {
        action: "DELETE", token, request_id: id(),
        asset_id: args[0], expected_version: Number(args[1]),
      };
      break;
    default:
      console.error(`unknown command: ${command}`);
      process.exit(2);
  }
  console.log(JSON.stringify(await call(request), null, 2));
}

main().catch((e) => {
  console.error("error:", e.message);
  process.exit(1);
});
