// A small asset client written in C#, to show the Python servers can be used from another
// language. All it has to match is the servers' format:
//   1. Connect over TLS (the servers use a self-signed certificate).
//   2. Send a 4-byte big-endian length, then that many bytes of UTF-8 JSON.
//   3. Read a 4-byte big-endian length, then that many bytes of UTF-8 JSON back.
//   4. The server then closes the connection, so each request uses a new connection.
// Every request carries "protocol_version": 1. After a LOGIN, the reply's "token" is sent
// with each later request.
//
// Build (no SDK needed, the compiler comes with Windows):
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /out:clients\AssetClient.exe /r:System.Web.Extensions.dll clients\AssetClient.cs
//
// Use:
//   AssetClient list     --user admin
//   AssetClient add      --user admin --description "Dell laptop" --category IT --location "Site A"
//   AssetClient checkout --user admin --id AST-0001 --borrower "Sam Lee"
//   AssetClient checkin  --user admin --id AST-0001
// Also: --server host:port, --password, --fingerprint AB:CD:..., --start and --due (YYYY-MM-DD).

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Web.Script.Serialization;

class AssetClient
{
    const int ProtocolVersion = 1;
    const int MaxMessageBytes = 10 * 1024 * 1024;

    static string server = "127.0.0.1:65432";
    static string fingerprint = null;   // if set, the server's certificate must match it
    static string token = null;         // from LOGIN, sent with every later request
    static bool told = false;
    static readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

    // Read exactly `count` bytes. A stream may give fewer than asked for, so keep reading.
    static byte[] ReadExactly(Stream stream, int count)
    {
        byte[] buffer = new byte[count];
        int got = 0;
        while (got < count)
        {
            int n = stream.Read(buffer, got, count - got);
            if (n <= 0) throw new IOException("The connection closed before the whole reply arrived.");
            got += n;
        }
        return buffer;
    }

    // Send one request to the server and return its reply as a dictionary.
    static Dictionary<string, object> Send(Dictionary<string, object> request)
    {
        request["protocol_version"] = ProtocolVersion;
        if (token != null && !request.ContainsKey("token")) request["token"] = token;

        string[] parts = server.Split(':');
        string host = parts[0];
        int port = parts.Length > 1 ? int.Parse(parts[1]) : 65432;

        using (TcpClient tcp = new TcpClient(host, port))
        {
            // The certificate is self-signed, so the normal check is switched off here and
            // the certificate is compared with --fingerprint below instead.
            using (SslStream tls = new SslStream(tcp.GetStream(), false, (s, c, ch, e) => true))
            {
                tls.AuthenticateAsClient(host, null, SslProtocols.Tls12, false);

                // This check comes BEFORE anything is sent, so a password never goes to the
                // wrong server.
                byte[] hash = SHA256.Create().ComputeHash(new X509Certificate2(tls.RemoteCertificate).RawData);
                string found = BitConverter.ToString(hash).Replace("-", ":");
                if (fingerprint != null && !found.Equals(fingerprint, StringComparison.OrdinalIgnoreCase))
                    throw new Exception("The server's certificate (" + found + ") is not the one you trusted.");
                if (fingerprint == null && !told)
                {
                    Console.WriteLine("Server certificate fingerprint: " + found + " (pass it as --fingerprint to check it next time)");
                    told = true;
                }

                // Send: 4-byte big-endian length, then the JSON.
                byte[] payload = Encoding.UTF8.GetBytes(json.Serialize(request));
                if (payload.Length > MaxMessageBytes) throw new Exception("The request is too large.");
                byte[] header = {
                    (byte)(payload.Length >> 24), (byte)(payload.Length >> 16),
                    (byte)(payload.Length >> 8), (byte)payload.Length };
                tls.Write(header, 0, 4);
                tls.Write(payload, 0, payload.Length);
                tls.Flush();

                // Receive: the same format back.
                byte[] h = ReadExactly(tls, 4);
                int length = (h[0] << 24) | (h[1] << 16) | (h[2] << 8) | h[3];
                if (length < 0 || length > MaxMessageBytes) throw new Exception("The reply is too large.");
                string body = Encoding.UTF8.GetString(ReadExactly(tls, length));
                return json.Deserialize<Dictionary<string, object>>(body);
            }
        }
    }

    // A reply with status OK worked; anything else has a "detail" saying why.
    static Dictionary<string, object> Checked(Dictionary<string, object> reply)
    {
        if ((string)reply["status"] != "OK")
        {
            object detail;
            reply.TryGetValue("detail", out detail);
            throw new Exception("The server said " + reply["status"] + ": " + detail);
        }
        return reply;
    }

    static Dictionary<string, object> Request(string action)
    {
        return new Dictionary<string, object> { { "action", action } };
    }

    // The asset with this ID, from a fresh LIST. Its "version" is needed for changes, so
    // the server can refuse a change made from out-of-date data.
    static Dictionary<string, object> GetAsset(string id)
    {
        foreach (Dictionary<string, object> asset in (ArrayList)Checked(Send(Request("LIST")))["assets"])
            if ((string)asset["asset_id"] == id) return asset;
        throw new Exception("There is no asset " + id + ".");
    }

    // The text after a --name option, or null if it wasn't given.
    static string Option(string[] args, string name)
    {
        for (int i = 1; i < args.Length - 1; i++)
            if (args[i] == "--" + name) return args[i + 1];
        return null;
    }

    static string Required(string[] args, string name)
    {
        string value = Option(args, name);
        if (value == null) throw new Exception("This needs --" + name + ".");
        return value;
    }

    // Read a password without showing it.
    static string AskPassword(string user)
    {
        Console.Write("Password for " + user + ": ");
        StringBuilder text = new StringBuilder();
        ConsoleKeyInfo key;
        while ((key = Console.ReadKey(true)).Key != ConsoleKey.Enter)
        {
            if (key.Key == ConsoleKey.Backspace && text.Length > 0) text.Length--;
            else if (key.KeyChar != '\0' && key.Key != ConsoleKey.Backspace) text.Append(key.KeyChar);
        }
        Console.WriteLine();
        return text.ToString();
    }

    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 0) throw new Exception("Use: AssetClient list|add|checkout|checkin --user NAME [options]. See the top of AssetClient.cs.");
            string command = args[0];
            server = Option(args, "server") ?? server;
            fingerprint = Option(args, "fingerprint");

            string user = Required(args, "user");
            string password = Option(args, "password") ?? AskPassword(user);
            Dictionary<string, object> login = Request("LOGIN");
            login["username"] = user;
            login["password"] = password;
            token = (string)Checked(Send(login))["token"];

            if (command == "list")
            {
                Dictionary<string, object> reply = Checked(Send(Request("LIST")));
                foreach (Dictionary<string, object> a in (ArrayList)reply["assets"])
                {
                    // The last loan is the current one if it hasn't been returned.
                    string note = "";
                    ArrayList loans = a.ContainsKey("loans") ? a["loans"] as ArrayList : null;
                    if (loans != null && loans.Count > 0)
                    {
                        Dictionary<string, object> loan = (Dictionary<string, object>)loans[loans.Count - 1];
                        if (loan["returned"] == null) note = "on loan to " + loan["borrower"] + " until " + loan["due"];
                    }
                    Console.WriteLine("{0,-12} {1,-32} {2,-11} {3}", a["asset_id"], a["description"], a["status"], note);
                }
                Console.WriteLine(reply["total"] + " asset(s)");
            }
            else if (command == "add")
            {
                // The server makes the asset ID itself. The request ID makes a retry safe:
                // sending the same request again returns the same asset, not a second one.
                Dictionary<string, object> request = Request("REGISTER");
                request["request_id"] = Guid.NewGuid().ToString("N");
                request["asset"] = new Dictionary<string, object> {
                    { "description", Required(args, "description") },
                    { "category", Required(args, "category") },
                    { "location", Required(args, "location") },
                    { "status", "In Use" } };
                Dictionary<string, object> stored = (Dictionary<string, object>)Checked(Send(request))["stored"];
                Console.WriteLine("Added " + stored["asset_id"]);
            }
            else if (command == "checkout")
            {
                string id = Required(args, "id");
                Dictionary<string, object> request = Request("CHECK_OUT");
                request["asset_id"] = id;
                request["borrower"] = Required(args, "borrower");
                request["start"] = Option(args, "start") ?? DateTime.Today.ToString("yyyy-MM-dd");
                request["due"] = Option(args, "due") ?? DateTime.Today.AddDays(7).ToString("yyyy-MM-dd");
                request["expected_version"] = GetAsset(id)["version"];
                Checked(Send(request));
                Console.WriteLine("Checked out " + id + " to " + request["borrower"] + " until " + request["due"]);
            }
            else if (command == "checkin")
            {
                string id = Required(args, "id");
                Dictionary<string, object> request = Request("CHECK_IN");
                request["asset_id"] = id;
                request["expected_version"] = GetAsset(id)["version"];
                Checked(Send(request));
                Console.WriteLine("Checked in " + id);
            }
            else
            {
                throw new Exception("Unknown command " + command + ".");
            }
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Error: " + error.Message);
            return 1;
        }
    }
}
