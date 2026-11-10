// C# client. Needs nothing beyond what ships with Windows: it is compiled with the C#
// compiler that comes with the .NET Framework, and uses only .NET Framework libraries.
// Speaks only JSON to the server: a TLS connection, a 4-byte big-endian length header,
// then a UTF-8 JSON object.
//
// Build it with build.bat, in this folder.
//
// Run client.exe with no arguments, or double-click it, to open the window (see
// client_gui.cs). With arguments it works on the command line instead. Those commands
// read config.json and server.crt from beside client.exe, or else from the folder
// above (the project folder):
//   client.exe <username> <password> ping
//   client.exe <username> <password> list
//   client.exe <username> <password> register
//   client.exe <username> <password> register asset_id=A1 description=Laptop category=IT location=HQ "status=In Use"
//   client.exe <username> <password> edit A1 <expected_version> location=Lab
//   client.exe <username> <password> delete A1 <expected_version>
//
// "register" with no fields starts a step-by-step guide. Fields can also be given as one
// JSON object instead of field=value pairs, as with client.js.

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

class Client
{
    const int ProtocolVersion = 1;
    static readonly string[] Statuses = { "In Use", "Not In Use" };

    internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static Dictionary<string, object> config;
    static X509Certificate2 trusted;

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length == 0)
        {
            AssetWindow.Start();
            return 0;
        }
        try
        {
            // Show and read accented and other non-English characters correctly.
            Console.OutputEncoding = new UTF8Encoding(false);
            Console.InputEncoding = new UTF8Encoding(false);
            return Run(args);
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("error: " + e.Message);
            return 1;
        }
    }

    static int Run(string[] args)
    {
        if (args.Length < 3)
        {
            Console.Error.WriteLine("usage: client.exe <username> <password> ping|list|register|edit|delete ...");
            return 2;
        }
        string folder = ConfigFolder();
        config = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(folder, "config.json"), Encoding.UTF8));
        trusted = LoadCertificate(Path.Combine(folder, (string)config["certificate_file"]));

        string username = args[0], password = args[1], command = args[2];
        string[] rest = args.Skip(3).ToArray();

        if (command == "ping")
        {
            Show(Call(new Dictionary<string, object> { { "action", "PING" } }));
            return 0;
        }
        var login = Call(new Dictionary<string, object> { { "action", "LOGIN" }, { "username", username }, { "password", password } });
        if ((string)login["status"] != "OK")
        {
            Show(login);
            return 1;
        }
        string token = (string)login["token"];
        string requestId = Guid.NewGuid().ToString("N");

        var request = new Dictionary<string, object> { { "token", token } };
        switch (command)
        {
            case "list":
                request["action"] = "LIST";
                break;
            case "register":
                var asset = rest.Length > 0 ? ParseFields(rest) : RegisterGuide();
                if (asset == null) return 0;
                request["action"] = "REGISTER";
                request["request_id"] = requestId;
                request["asset"] = asset;
                break;
            case "edit":
                if (rest.Length < 3) throw new ArgumentException("usage: client.exe <username> <password> edit <asset_id> <expected_version> field=value ...");
                request["action"] = "EDIT";
                request["request_id"] = requestId;
                request["asset_id"] = rest[0];
                request["expected_version"] = int.Parse(rest[1]);
                request["changes"] = ParseFields(rest.Skip(2).ToArray());
                break;
            case "delete":
                if (rest.Length < 2) throw new ArgumentException("usage: client.exe <username> <password> delete <asset_id> <expected_version>");
                request["action"] = "DELETE";
                request["request_id"] = requestId;
                request["asset_id"] = rest[0];
                request["expected_version"] = int.Parse(rest[1]);
                break;
            default:
                Console.Error.WriteLine("unknown command: " + command);
                return 2;
        }
        var reply = Call(request);
        Show(reply);
        return (string)reply["status"] == "OK" ? 0 : 1;
    }

    // The folder holding config.json: beside client.exe, or else the folder above it.
    static string ConfigFolder()
    {
        string here = AppDomain.CurrentDomain.BaseDirectory;
        string above = Path.GetDirectoryName(here.TrimEnd(Path.DirectorySeparatorChar));
        if (!File.Exists(Path.Combine(here, "config.json")) && above != null && File.Exists(Path.Combine(above, "config.json")))
            return above;
        return here;
    }

    // Trust only the system's own certificate: the server must present exactly the
    // certificate in server.crt, or the connection is refused before anything is sent.
    static X509Certificate2 LoadCertificate(string path)
    {
        string pem = File.ReadAllText(path);
        string base64 = Regex.Replace(Regex.Replace(pem, "-----[A-Z ]+-----", ""), @"\s", "");
        return new X509Certificate2(Convert.FromBase64String(base64));
    }

    static bool CheckCertificate(object sender, X509Certificate certificate, X509Chain chain, SslPolicyErrors errors)
    {
        return certificate != null && certificate.GetCertHashString() == trusted.Thumbprint;
    }

    // Try the primary, then the backup.
    static Dictionary<string, object> Call(Dictionary<string, object> request)
    {
        var servers = (Dictionary<string, object>)config["servers"];
        var failures = new List<string>();
        foreach (string role in new[] { "primary", "backup" })
        {
            if (!servers.ContainsKey(role)) continue;
            var server = (Dictionary<string, object>)servers[role];
            try
            {
                return SendRequest((string)server["host"], Convert.ToInt32(server["port"]), request);
            }
            catch (Exception e)
            {
                if (!(e is IOException || e is SocketException || e is AuthenticationException || e is TimeoutException || e is AggregateException))
                    throw;
                failures.Add(role + ": " + (e.InnerException ?? e).Message);
            }
        }
        throw new IOException(failures.Count == 0 ? "No servers are configured"
                                                  : "Could not reach a server (" + string.Join("; ", failures) + ")");
    }

    // Send one request to one server over TLS and return its reply.
    static Dictionary<string, object> SendRequest(string host, int port, Dictionary<string, object> request)
    {
        request["protocol_version"] = ProtocolVersion;
        byte[] body = Encoding.UTF8.GetBytes(Json.Serialize(request));
        byte[] header = BigEndian(body.Length);
        int timeout = (int)(Convert.ToDouble(config["client_timeout_seconds"]) * 1000);
        int maxBytes = Convert.ToInt32(config["max_message_bytes"]);

        using (var client = new TcpClient())
        {
            if (!client.ConnectAsync(host, port).Wait(timeout))
                throw new TimeoutException("Timed out connecting to " + host + ":" + port);
            using (var tls = new SslStream(client.GetStream(), false, CheckCertificate))
            {
                tls.ReadTimeout = timeout;
                tls.WriteTimeout = timeout;
                tls.AuthenticateAsClient(host, null, SslProtocols.Tls12, false);
                tls.Write(header.Concat(body).ToArray());
                int length = FromBigEndian(ReadExact(tls, 4));
                if (length < 0 || length > maxBytes) throw new IOException("The reply is too large");
                string reply = Encoding.UTF8.GetString(ReadExact(tls, length));
                return Json.Deserialize<Dictionary<string, object>>(reply);
            }
        }
    }

    internal static byte[] BigEndian(int value)
    {
        return new[] { (byte)(value >> 24), (byte)(value >> 16), (byte)(value >> 8), (byte)value };
    }

    internal static int FromBigEndian(byte[] b)
    {
        return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3];
    }

    internal static byte[] ReadExact(Stream stream, int count)
    {
        var buffer = new byte[count];
        int read = 0;
        while (read < count)
        {
            int n = stream.Read(buffer, read, count - read);
            if (n <= 0) throw new IOException("Connection closed before a full message was received");
            read += n;
        }
        return buffer;
    }

    // Turn field=value pairs, or one JSON object, into the fields of an asset.
    static Dictionary<string, object> ParseFields(string[] words)
    {
        if (words.Length == 1 && words[0].TrimStart().StartsWith("{"))
            return Json.Deserialize<Dictionary<string, object>>(words[0]);
        var fields = new Dictionary<string, object>();
        foreach (string word in words)
        {
            int equals = word.IndexOf('=');
            if (equals < 0) throw new ArgumentException("Expected field=value, got: " + word);
            fields[word.Substring(0, equals).Trim()] = word.Substring(equals + 1).Trim();
        }
        return fields;
    }

    // Step-by-step guide that asks for each field and re-asks until it is valid.
    // Returns null if the user cancels.
    static Dictionary<string, object> RegisterGuide()
    {
        Console.WriteLine("\n--- Asset register guide ---");
        var asset = new Dictionary<string, object>();
        asset["asset_id"] = Ask("Asset ID (e.g. A1): ");
        asset["description"] = Ask("Description: ");
        asset["category"] = Ask("Category: ");
        asset["location"] = Ask("Location: ");
        while (true)
        {
            Console.Write("Status - 1) In Use  2) Not In Use: ");
            string choice = (Console.ReadLine() ?? "").Trim();
            if (choice == "1" || choice == "2")
            {
                asset["status"] = Statuses[int.Parse(choice) - 1];
                break;
            }
            Console.WriteLine("  Please type 1 or 2.");
        }
        Console.WriteLine();
        Show(asset);
        Console.Write("Register this asset? (y/n): ");
        if ((Console.ReadLine() ?? "").Trim().ToLower() != "y")
        {
            Console.WriteLine("Cancelled.");
            return null;
        }
        return asset;
    }

    static string Ask(string label)
    {
        while (true)
        {
            Console.Write(label);
            string line = Console.ReadLine();
            if (line == null) throw new IOException("Input ended");
            string value = line.Trim();
            if (value.Length > 0) return value;
            Console.WriteLine("  This can't be empty.");
        }
    }

    // Print a reply as indented JSON.
    static void Show(object value)
    {
        var text = new StringBuilder();
        Indent(value, 0, text);
        Console.WriteLine(text.ToString());
    }

    static void Indent(object value, int depth, StringBuilder text)
    {
        string pad = new string(' ', (depth + 1) * 2), end = new string(' ', depth * 2);
        var map = value as IDictionary;
        var list = value as IList;
        if (map != null)
        {
            if (map.Count == 0) { text.Append("{}"); return; }
            text.Append("{\n");
            int i = 0;
            foreach (DictionaryEntry entry in map)
            {
                text.Append(pad).Append(Json.Serialize(entry.Key)).Append(": ");
                Indent(entry.Value, depth + 1, text);
                text.Append(++i < map.Count ? ",\n" : "\n");
            }
            text.Append(end).Append("}");
        }
        else if (list != null)
        {
            if (list.Count == 0) { text.Append("[]"); return; }
            text.Append("[\n");
            for (int i = 0; i < list.Count; i++)
            {
                text.Append(pad);
                Indent(list[i], depth + 1, text);
                text.Append(i + 1 < list.Count ? ",\n" : "\n");
            }
            text.Append(end).Append("]");
        }
        else
        {
            text.Append(Json.Serialize(value));
        }
    }
}
