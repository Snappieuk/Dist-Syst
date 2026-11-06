// The network side of the desktop app: how it talks to the Python servers. Nothing here
// draws a window. It mirrors what gui.py does, in C#:
//   - every message is JSON in UTF-8, with a 4-byte big-endian length in front of it
//   - every connection is TLS, and carries exactly one request and one reply
//   - the server's certificate is pinned by its SHA-256 fingerprint
//   - if one server is down, the next is tried (failover)
//   - UDP broadcasts find servers on the local network (discovery)
//
// The compiler that ships with Windows is C# 5, so this avoids newer syntax such as
// string interpolation and "?.".

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

namespace AssetApp
{
    // A server's address.
    class Addr
    {
        public string Host;
        public int Port;

        public Addr(string host, int port) { Host = host; Port = port; }
        public override string ToString() { return Host + ":" + Port; }
        public override bool Equals(object other)
        {
            Addr a = other as Addr;
            return a != null && a.Host == Host && a.Port == Port;
        }
        public override int GetHashCode() { return Host.GetHashCode() ^ Port; }
    }

    // A certificate that is not the one the user trusted.
    class UntrustedServerException : IOException
    {
        public UntrustedServerException(string message) : base(message) { }
    }

    // Small helpers for reading the dictionaries and lists that JSON turns into.
    static class J
    {
        public static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

        public static string Text(Dictionary<string, object> d, string key)
        {
            object value;
            return d != null && d.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : "";
        }

        public static int Number(Dictionary<string, object> d, string key)
        {
            object value;
            return d != null && d.TryGetValue(key, out value) && value != null ? Convert.ToInt32(value) : 0;
        }

        public static bool Flag(Dictionary<string, object> d, string key)
        {
            object value;
            return d != null && d.TryGetValue(key, out value) && value is bool && (bool)value;
        }

        // The items of a JSON list that are objects. A missing or wrong-typed value is an empty list.
        public static List<Dictionary<string, object>> Objects(Dictionary<string, object> d, string key)
        {
            List<Dictionary<string, object>> items = new List<Dictionary<string, object>>();
            object value;
            if (d != null && d.TryGetValue(key, out value) && value is ArrayList)
                foreach (object item in (ArrayList)value)
                {
                    Dictionary<string, object> entry = item as Dictionary<string, object>;
                    if (entry != null) items.Add(entry);
                }
            return items;
        }

        // The items of a JSON list of text, such as an asset's tags.
        public static List<string> Strings(Dictionary<string, object> d, string key)
        {
            List<string> items = new List<string>();
            object value;
            if (d != null && d.TryGetValue(key, out value) && value is ArrayList)
                foreach (object item in (ArrayList)value) items.Add(Convert.ToString(item));
            return items;
        }
    }

    // Who is logged in and which servers to talk to. Nothing is saved to disk, so
    // everything here lasts until the program is closed.
    static class Session
    {
        public static List<Addr> Servers = new List<Addr>();   // the primary first
        public static string Trusted;                          // the certificate fingerprint the program accepted
        public static string Token;                            // proves the login to the servers
        public static string Username;
        public static string Role;
        public static string WebUrl;                           // https://host when using the web API, else null (see WebNet.cs)
    }

    // Settings added to the end of the .exe by the web page's download button: where the
    // servers are, and which certificate is the real one. Neither is secret. A copy of the
    // program that was not downloaded from the web page has none, and works anywhere by
    // trusting the first certificate it sees.
    static class SiteSettings
    {
        public static List<Addr> Servers = new List<Addr>();
        public static string Fingerprint;
        public static string Url;   // https://host, for a copy downloaded through an HTTPS proxy is built in two pieces, so the finished program never contains it whole
        // by accident and can't mistake its own code for the settings.
        static readonly string Marker = "--ASSET-" + "SITE--";

        public static void Load()
        {
            try
            {
                string path = System.Reflection.Assembly.GetExecutingAssembly().Location;
                using (FileStream file = File.OpenRead(path))
                {
                    int size = (int)Math.Min(file.Length, 16384);
                    byte[] tail = new byte[size];
                    file.Seek(-size, SeekOrigin.End);
                    file.Read(tail, 0, size);
                    string text = Encoding.UTF8.GetString(tail);
                    int at = text.LastIndexOf(Marker, StringComparison.Ordinal);
                    if (at < 0) return;

                    Dictionary<string, object> settings =
                        J.Serializer.Deserialize<Dictionary<string, object>>(text.Substring(at + Marker.Length));
                    Fingerprint = J.Text(settings, "fingerprint");
                    if (Fingerprint == "") Fingerprint = null;
                    Url = WebNet.Address(J.Text(settings, "url"));   // null if absent or not a valid https address
                    foreach (string entry in J.Strings(settings, "servers"))
                    {
                        Addr address = Net.ParseAddress(entry);
                        if (!Servers.Contains(address)) Servers.Add(address);
                    }
                }
            }
            catch (Exception)
            {
                Servers.Clear();      // settings that can't be read are ignored: the program
                Fingerprint = null;   // then works like one that was not downloaded
                Url = null;
            }
        }
    }

    static class Net
    {
        public const int ProtocolVersion = 1;
        public const int MaxMessageBytes = 10 * 1024 * 1024;
        public const int TimeoutMs = 5000;
        public const int FailoverWaitSeconds = 15;
        public static readonly int[] DefaultPorts = { 65432, 65433 };
        static readonly string[] LoopbackNames = { "localhost", "127.0.0.1", "::1", "0.0.0.0" };

        // ---- Addresses ----

        // "host" or "host:port" to an address. Throws FormatException saying what is wrong.
        public static Addr ParseAddress(string text)
        {
            text = (text ?? "").Trim();
            if (text.Length == 0) throw new FormatException("Enter the server's IP address or host name.");
            string host = text;
            int port = DefaultPorts[0];
            int colon = text.LastIndexOf(':');
            if (colon >= 0)
            {
                host = text.Substring(0, colon);
                string portText = text.Substring(colon + 1);
                if (!Regex.IsMatch(portText, "^[0-9]{1,5}$") || int.Parse(portText) < 1 || int.Parse(portText) > 65535)
                    throw new FormatException("'" + portText + "' is not a port. Use a number from 1 to 65535.");
                port = int.Parse(portText);
            }
            IPAddress ignored;
            // Something like 999.1.1 looks like an IP address but isn't one, and isn't a name either.
            if (!IPAddress.TryParse(host, out ignored) &&
                (Regex.IsMatch(host, "^[0-9.]+$") || !Regex.IsMatch(host, "^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")))
                throw new FormatException("'" + host + "' is not a valid IP address or host name.");
            return new Addr(host, port);
        }

        // The servers' own list from a PING reply, with loopback names replaced by the host
        // that was typed, because "127.0.0.1" means nothing to a program on another computer.
        // The order is the servers' own (primary first), and the typed address is always there.
        public static List<Addr> ServersFromPing(Addr typed, Dictionary<string, object> reply)
        {
            List<Addr> servers = new List<Addr>();
            object list;
            if (reply.TryGetValue("servers", out list) && list is ArrayList)
                foreach (object entry in (ArrayList)list)
                {
                    ArrayList pair = entry as ArrayList;
                    if (pair == null || pair.Count < 2) continue;
                    string host = Convert.ToString(pair[0]);
                    int port;
                    if (!int.TryParse(Convert.ToString(pair[1]), out port)) continue;
                    Addr address = new Addr(Array.IndexOf(LoopbackNames, host) >= 0 ? typed.Host : host, port);
                    if (!servers.Contains(address)) servers.Add(address);
                }
            if (!servers.Contains(typed)) servers.Insert(0, typed);
            return servers;
        }

        // ---- Talking to a server ----

        // The SHA-256 fingerprint of a certificate as people compare it: AB:CD:...
        public static string Fingerprint(byte[] certificate)
        {
            return BitConverter.ToString(SHA256.Create().ComputeHash(certificate)).Replace("-", ":");
        }

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

        // Send one request to one server and return its reply. If `trusted` is given, the
        // server's certificate must match it, and this is checked BEFORE anything is sent, so
        // a password is never sent to a server that wasn't accepted. With no `trusted`, the
        // request is sent anyway, so it must hold nothing private: only PING does that.
        public static Dictionary<string, object> Ask(Addr address, Dictionary<string, object> request,
                                                     string trusted, out string found)
        {
            using (TcpClient tcp = new TcpClient())
            {
                IAsyncResult connecting = tcp.BeginConnect(address.Host, address.Port, null, null);
                if (!connecting.AsyncWaitHandle.WaitOne(TimeoutMs))
                    throw new IOException("Timed out connecting to " + address);
                tcp.EndConnect(connecting);
                tcp.ReceiveTimeout = TimeoutMs;
                tcp.SendTimeout = TimeoutMs;

                // The certificate is self-signed, so the normal check is switched off. The
                // fingerprint is compared instead.
                using (SslStream tls = new SslStream(tcp.GetStream(), false, (s, c, chain, errors) => true))
                {
                    tls.AuthenticateAsClient(address.Host, null, SslProtocols.Tls12, false);
                    found = Fingerprint(new X509Certificate2(tls.RemoteCertificate).RawData);
                    if (trusted != null && !found.Equals(trusted, StringComparison.OrdinalIgnoreCase))
                        throw new UntrustedServerException("The server at " + address +
                            " has a different certificate from the one you trusted, so it was not used.");

                    request["protocol_version"] = ProtocolVersion;
                    byte[] payload = Encoding.UTF8.GetBytes(J.Serializer.Serialize(request));
                    if (payload.Length > MaxMessageBytes) throw new IOException("The request is too large.");
                    tls.Write(new byte[] {
                        (byte)(payload.Length >> 24), (byte)(payload.Length >> 16),
                        (byte)(payload.Length >> 8), (byte)payload.Length }, 0, 4);
                    tls.Write(payload, 0, payload.Length);
                    tls.Flush();

                    byte[] h = ReadExactly(tls, 4);
                    int length = (h[0] << 24) | (h[1] << 16) | (h[2] << 8) | h[3];
                    if (length < 0 || length > MaxMessageBytes) throw new IOException("The reply is too large.");
                    string body = Encoding.UTF8.GetString(ReadExactly(tls, length));
                    return J.Serializer.Deserialize<Dictionary<string, object>>(body);
                }
            }
        }

        public static Dictionary<string, object> Request(string action)
        {
            return new Dictionary<string, object> { { "action", action } };
        }

        // PING a server we know nothing about, returning its certificate fingerprint and reply.
        // A PING needs no login and carries nothing private.
        public static Dictionary<string, object> Probe(Addr address, out string found)
        {
            return Ask(address, Request("PING"), null, out found);
        }

        // Send a request to the first server that answers, adding the login token.
        //
        // This is failover: if the primary is down, the next server is tried without the
        // user doing anything. Retrying on another server is safe, because a repeated
        // registration is recognised by its request ID. The next server is also tried after
        // an INTERNAL or NOT_FOUND reply (this one may not have the asset yet), and if every
        // server says NOT_PRIMARY a backup is taking over, so it waits for that.
        //
        // Throws IOException if no server can be reached.
        public static Dictionary<string, object> Send(Dictionary<string, object> request)
        {
            if (Session.WebUrl != null) return WebNet.Send(request);   // HTTPS mode: web.py does the failover
            if (Session.Token != null) request["token"] = Session.Token;
            DateTime deadline = DateTime.UtcNow.AddSeconds(FailoverWaitSeconds);

            while (true)
            {
                Exception lastError = null;
                Dictionary<string, object> retryable = null, notPrimary = null;
                foreach (Addr address in Session.Servers)
                {
                    Dictionary<string, object> reply;
                    try
                    {
                        string ignored;
                        reply = Ask(address, request, Session.Trusted, out ignored);
                    }
                    catch (Exception error)   // refused, timed out, dropped, or an untrusted certificate
                    {
                        lastError = error;
                        continue;
                    }
                    string code = J.Text(reply, "code");
                    if (code == "INTERNAL" || code == "NOT_FOUND") retryable = reply;
                    else if (code == "NOT_PRIMARY") notPrimary = reply;
                    else return reply;
                }

                if (retryable != null) return retryable;
                if (notPrimary == null)
                    throw new IOException("Could not reach any asset server (" +
                        (lastError == null ? "none are known" : lastError.Message) +
                        ").\nCheck that the server is running, then try again.");
                if (DateTime.UtcNow >= deadline) return notPrimary;
                Thread.Sleep(1000);
            }
        }

        // ---- Discovery ----

        // Look for servers on the local network. A broadcast is sent to each usual port, and
        // every server answers, naming the port it really uses. This machine is asked
        // directly too, as a broadcast doesn't always reach a server on the same computer.
        public static List<Addr> Discover()
        {
            List<Addr> found = new List<Addr>();
            using (Socket sock = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
            {
                sock.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.Broadcast, true);
                sock.Bind(new IPEndPoint(IPAddress.Any, 0));
                byte[] hello = Encoding.ASCII.GetBytes("ASSET_MANAGEMENT_DISCOVER");
                foreach (string target in new[] { "255.255.255.255", "127.0.0.1" })
                    foreach (int port in DefaultPorts)
                        try { sock.SendTo(hello, new IPEndPoint(IPAddress.Parse(target), port)); }
                        catch (SocketException) { }   // no network to broadcast on; the other target may still work

                DateTime end = DateTime.UtcNow.AddSeconds(1.5);
                byte[] buffer = new byte[1024];
                while (DateTime.UtcNow < end)
                {
                    sock.ReceiveTimeout = 300;
                    EndPoint from = new IPEndPoint(IPAddress.Any, 0);
                    try
                    {
                        int n = sock.ReceiveFrom(buffer, ref from);
                        Dictionary<string, object> info =
                            J.Serializer.Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(buffer, 0, n));
                        if (J.Text(info, "service") != "asset-management") continue;
                        Addr address = new Addr(((IPEndPoint)from).Address.ToString(), J.Number(info, "port"));
                        if (!found.Contains(address)) found.Add(address);
                    }
                    catch (SocketException) { }     // a timeout, or an unreachable sender
                    catch (ArgumentException) { }   // not an answer from one of our servers
                    catch (InvalidOperationException) { }
                }
            }
            return found;
        }
    }
}
