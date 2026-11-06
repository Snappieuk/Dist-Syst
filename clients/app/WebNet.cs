// HTTPS mode: talking to web.py through a reverse proxy. It mirrors "HTTPS mode" in gui.py.
//
// When the address typed starts with https:// (for example https://assets.example.com), the
// program doesn't use the servers' own protocol. It uses the same JSON API as the web page,
// over normal HTTPS, so only port 443 is needed. web.py does the failover between the
// servers and checks every permission itself. The site's certificate is checked the normal
// way (it comes from a public authority such as Let's Encrypt): the certificate check is
// never switched off here, and plain http:// is never used.
//
// HttpsSend turns each request the rest of the program makes into a call to the API, and
// the answer back into the reply a server would have given, so no window needs to know
// which mode is in use.

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Web;

namespace AssetApp
{
    static class WebNet
    {
        // web.py may wait for a backup to take over, so allow longer than for one server.
        const int TimeoutMs = (Net.FailoverWaitSeconds * 1000) + Net.TimeoutMs;

        // The asset fields a typed search looks in, the same as the servers use.
        static readonly string[] SearchFields = { "asset_id", "description", "category", "location", "status" };

        // The cookie web.py gives at login, kept in memory only and sent with each request.
        static CookieContainer cookies = new CookieContainer();

        // The security protocol Windows needs for modern sites. .NET Framework 4.x doesn't
        // always turn on TLS 1.2 by itself, and the enum has no name for it before 4.5.
        // Adding it never removes a protocol and never skips the certificate check.
        public static void Prepare()
        {
            ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072;   // TLS 1.2
        }

        // Start with no cookie, as when a person logs in as someone else.
        public static void NewConnection()
        {
            cookies = new CookieContainer();
        }

        // The typed https://host[:port] address tidied up, or null if it isn't one. Throws
        // FormatException for plain http:// (which is refused) and for anything after the host.
        public static string Address(string text)
        {
            text = (text ?? "").Trim();
            if (text.StartsWith("http://", StringComparison.OrdinalIgnoreCase))
                throw new FormatException("Plain http:// is not supported, because it would send the password " +
                    "unprotected. Use https://, or type just the server's address to use the local network.");
            if (!text.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) return null;

            Uri uri;
            if (!Uri.TryCreate(text, UriKind.Absolute, out uri) || uri.Host.Length == 0 ||
                uri.UserInfo.Length > 0 || uri.Query.Length > 0 || uri.Fragment.Length > 0 || uri.AbsolutePath.Trim('/').Length > 0)
                throw new FormatException("Type just the site's address, such as https://assets.example.com");
            Net.ParseAddress(uri.IdnHost);   // checks it is a valid host name or IP address (throws FormatException)
            return "https://" + uri.Authority.ToLowerInvariant();
        }

        // Make one API call. Returns the HTTP status and the reply's JSON object.
        // Throws IOException if the site can't be reached or its certificate isn't trusted.
        public static Dictionary<string, object> Call(string method, string path, Dictionary<string, object> body, out int status)
        {
            if (Session.WebUrl == null) throw new IOException("Not connected to a site.");
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(Session.WebUrl + path);
            request.Method = method;
            request.Accept = "application/json";
            request.CookieContainer = cookies;
            request.AllowAutoRedirect = false;   // a redirect could lead to plain http://
            request.Timeout = TimeoutMs;
            request.ReadWriteTimeout = TimeoutMs;
            // The certificate is deliberately NOT given a validation callback: the normal check applies.

            byte[] data = body == null ? null : Encoding.UTF8.GetBytes(J.Serializer.Serialize(body));
            if (data != null)
            {
                request.ContentType = "application/json";
                request.ContentLength = data.Length;
            }

            string text;
            try
            {
                // Sending the body is what first connects, so a refused certificate shows up here too.
                if (data != null)
                    using (Stream stream = request.GetRequestStream()) stream.Write(data, 0, data.Length);
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    status = (int)response.StatusCode;
                    text = Read(response);
                }
            }
            catch (WebException error)
            {
                HttpWebResponse response = error.Response as HttpWebResponse;
                if (response == null) throw new IOException(Describe(error), error);
                using (response)   // an error status is still an answer
                {
                    status = (int)response.StatusCode;
                    text = Read(response);
                }
            }

            Dictionary<string, object> reply = null;
            try { reply = J.Serializer.Deserialize<Dictionary<string, object>>(text); }
            catch (Exception) { }
            if (reply != null) return reply;
            if (status >= 300) return new Dictionary<string, object>();   // for example the proxy's own error page
            throw new IOException("The reply is not valid. Is this a web.py site?");
        }

        static string Read(HttpWebResponse response)
        {
            using (Stream stream = response.GetResponseStream())
            {
                MemoryStream all = new MemoryStream();
                byte[] buffer = new byte[8192];
                int n;
                while ((n = stream.Read(buffer, 0, buffer.Length)) > 0)
                {
                    all.Write(buffer, 0, n);
                    if (all.Length > Net.MaxMessageBytes) throw new IOException("The reply is too large");
                }
                return Encoding.UTF8.GetString(all.ToArray());
            }
        }

        static string Describe(WebException error)
        {
            string site = Session.WebUrl;
            // A refused certificate shows up under several statuses, depending on the Windows version.
            bool certificate = error.Status == WebExceptionStatus.TrustFailure ||
                error.Status == WebExceptionStatus.SecureChannelFailure ||
                error.InnerException is System.Security.Authentication.AuthenticationException ||
                error.Message.IndexOf("trust relationship", StringComparison.OrdinalIgnoreCase) >= 0;
            if (certificate)
                return "The certificate of " + site + " could not be verified (" + error.Message + "), so nothing was sent.";
            return "Could not reach " + site + ": " + error.Message;
        }

        // Turn an API answer into the reply a server would have given.
        static Dictionary<string, object> Reply(int status, Dictionary<string, object> data, bool loggingIn)
        {
            if (status >= 200 && status < 300)
            {
                Dictionary<string, object> ok = new Dictionary<string, object>(data);
                ok["status"] = "OK";
                if (data.ContainsKey("asset")) ok["stored"] = data["asset"];   // the servers call the saved asset "stored"
                return ok;
            }
            string detail = J.Text(data, "error");
            // web.py or its proxy can't get an answer from the servers: the same as no server reachable.
            if (status == 502 || status == 503 || status == 504)
                throw new IOException(detail != "" ? detail : "The site is not answering at the moment. Please try again.");
            if (status == 401 && !loggingIn) detail = "Your login has expired. Close this window and log in again.";
            return Failure(CodeFor(status), detail != "" ? detail : "The site returned an error (" + status + ").");
        }

        static Dictionary<string, object> Failure(string code, string detail)
        {
            return new Dictionary<string, object> { { "status", "ERROR" }, { "code", code }, { "detail", detail } };
        }

        // The error code the servers use for each HTTP status web.py answers with.
        static string CodeFor(int status)
        {
            switch (status)
            {
                case 400: return "BAD_REQUEST";
                case 401: return "UNAUTHORIZED";
                case 403: return "FORBIDDEN";
                case 404: return "NOT_FOUND";
                case 409: return "CONFLICT";
                case 429: return "UNAUTHORIZED";   // too many wrong passwords
                default: return "INTERNAL";
            }
        }

        // Check the site answers like web.py does. Asking for the assets needs a login, so
        // the expected answer is a refusal (401); it shows this is a web.py site before any
        // password is sent. Throws IOException if not.
        public static void CheckSite()
        {
            int status;
            Call("GET", "/api/assets", null, out status);
            if (status != 200 && status != 401)
                throw new IOException(Session.WebUrl + " does not look like an asset site (it answered " + status + ").");
        }

        // Do what a request asks, through the web API, and return the reply a server would give.
        public static Dictionary<string, object> Send(Dictionary<string, object> request)
        {
            string action = J.Text(request, "action");
            string asset = "/api/assets/" + Uri.EscapeDataString(J.Text(request, "asset_id"));
            int status;
            Dictionary<string, object> data;

            switch (action)
            {
                case "LOGIN":
                    data = Call("POST", "/api/login", new Dictionary<string, object> {
                        { "username", J.Text(request, "username") }, { "password", J.Text(request, "password") } }, out status);
                    if (status != 200) return Reply(status, data, true);
                    // The login reply doesn't say what the account may do; the asset list does.
                    Dictionary<string, object> listing = Call("GET", "/api/assets", null, out status);
                    if (status != 200) return Reply(status, listing, false);
                    string role = J.Text(listing, "role");
                    return new Dictionary<string, object> {
                        { "status", "OK" }, { "token", "web-session" },
                        { "username", J.Text(data, "username") != "" ? J.Text(data, "username") : J.Text(request, "username") },
                        { "role", role != "" ? role : "user" } };

                case "LIST":
                    data = Call("GET", "/api/assets", null, out status);
                    if (status != 200) return Reply(status, data, false);
                    return Filter(data, request);

                case "REGISTER":
                    data = Call("POST", "/api/assets", new Dictionary<string, object> {
                        { "request_id", request["request_id"] }, { "asset", request["asset"] } }, out status);
                    break;
                case "SET_STATUS":
                    data = Call("PATCH", asset, new Dictionary<string, object> {
                        { "version", request["expected_version"] },
                        { "changes", new Dictionary<string, object> { { "status", request["status"] } } } }, out status);
                    break;
                case "EDIT":
                    data = Call("PATCH", asset, new Dictionary<string, object> {
                        { "version", request["expected_version"] }, { "changes", request["changes"] } }, out status);
                    break;
                case "DELETE":
                    data = Call("DELETE", asset + "?version=" + Uri.EscapeDataString(J.Text(request, "expected_version")), null, out status);
                    break;
                case "APPROVE":
                    data = Call("POST", asset + "/approve", Versioned(request), out status);
                    break;
                case "CHECK_OUT":
                    Dictionary<string, object> outBody = Versioned(request);
                    foreach (string field in new string[] { "borrower", "start", "due" }) outBody[field] = J.Text(request, field);
                    data = Call("POST", asset + "/check-out", outBody, out status);
                    break;
                case "CHECK_IN":
                    Dictionary<string, object> inBody = Versioned(request);
                    if (request.ContainsKey("returned")) inBody["returned"] = request["returned"];
                    data = Call("POST", asset + "/check-in", inBody, out status);
                    break;
                default:
                    throw new InvalidOperationException(action + " is not available over HTTPS");
            }
            return Reply(status, data, false);
        }

        static Dictionary<string, object> Versioned(Dictionary<string, object> request)
        {
            return new Dictionary<string, object> { { "version", request["expected_version"] } };
        }

        // The API lists everything, so the search box and status filter are applied here,
        // the way the servers apply them.
        static Dictionary<string, object> Filter(Dictionary<string, object> data, Dictionary<string, object> request)
        {
            string wanted = J.Text(request, "query").Trim().ToLowerInvariant();
            string wantedStatus = request.ContainsKey("status") ? J.Text(request, "status") : null;
            ArrayList shown = new ArrayList();
            foreach (Dictionary<string, object> item in J.Objects(data, "assets"))
            {
                if (wantedStatus != null && J.Text(item, "status") != wantedStatus) continue;
                bool match = wanted.Length == 0;
                foreach (string field in SearchFields)
                    if (J.Text(item, field).ToLowerInvariant().Contains(wanted)) match = true;
                if (match) shown.Add(item);
            }
            string servedBy = J.Text(data, "served_by");
            return new Dictionary<string, object> {
                { "status", "OK" }, { "assets", shown }, { "total", shown.Count },
                { "served_by", servedBy != "" ? servedBy : "unknown" } };
        }
    }
}
