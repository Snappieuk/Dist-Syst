// Minimal Node.js client (no dependencies). Speaks only JSON to the server:
// TLS connection, 4-byte big-endian length header, then a UTF-8 JSON object.
//
// It reads config.json and server.crt from its own folder, or else from the folder
// above (the project folder).
//
// Usage:
//   node client.js <username> <password> ping
//   node client.js <username> <password> list
//   node client.js <username> <password> register '{"asset_id":"A1","description":"Laptop","category":"IT","location":"HQ","status":"In Use"}'
//   node client.js <username> <password> edit <asset_id> <expected_version> '{"location":"Lab"}'
//   node client.js <username> <password> delete <asset_id> <expected_version>
const tls = require("tls");
const net = require("net");
const fs = require("fs");
const crypto = require("crypto");
const path = require("path");

const folder = [__dirname, path.dirname(__dirname)].find((f) => fs.existsSync(path.join(f, "config.json"))) || __dirname;
const config = JSON.parse(fs.readFileSync(path.join(folder, "config.json"), "utf8"));
const ca = fs.readFileSync(path.join(folder, config.certificate_file));

function sendRequest(server, request) {
  return new Promise((resolve, reject) => {
    const body = Buffer.from(JSON.stringify({ ...request, protocol_version: 1 }), "utf8");
    const header = Buffer.alloc(4);
    header.writeUInt32BE(body.length);
    let buf = Buffer.alloc(0);
    const sock = tls.connect(
      {
        host: server.host, port: server.port, ca, minVersion: "TLSv1.2",
        // Node rejects an IP address as the SNI name; the certificate is still checked against the IP.
        ...(net.isIP(server.host) ? {} : { servername: server.host }),
      },
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

// Step-by-step guide that asks for each field and re-asks until it is valid.
async function registerGuide() {
  const rl = require("readline/promises").createInterface({ input: process.stdin, output: process.stdout });
  const ask = async (label) => {
    for (;;) {
      const value = (await rl.question(label)).trim();
      if (value) return value;
      console.log("  This can't be empty.");
    }
  };
  console.log("\n--- Asset register guide ---");
  const asset = {
    asset_id: await ask("Asset ID (e.g. A1): "),
    description: await ask("Description: "),
    category: await ask("Category: "),
    location: await ask("Location: "),
  };
  for (;;) {
    const choice = (await rl.question("Status - 1) In Use  2) Not In Use: ")).trim();
    if (choice === "1" || choice === "2") {
      asset.status = choice === "1" ? "In Use" : "Not In Use";
      break;
    }
    console.log("  Please type 1 or 2.");
  }
  console.log("\n" + JSON.stringify(asset, null, 2));
  const ok = (await rl.question("Register this asset? (y/n): ")).trim().toLowerCase();
  rl.close();
  if (ok !== "y") {
    console.log("Cancelled.");
    process.exit(0);
  }
  return asset;
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
      request = {
        action: "REGISTER", token, request_id: id(),
        asset: args[0] ? JSON.parse(args[0]) : await registerGuide(),
      };
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
