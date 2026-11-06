// The login window. It finds the server, decides which certificate to trust, and logs in.
// It follows the same steps as the login window in gui.py:
//   1. PING the typed server (no password sent yet) to see its certificate and learn the
//      full list of servers.
//   2. Decide whether to trust that certificate:
//        - a program downloaded from the web page knows the right fingerprint, and only
//          accepts a server showing exactly that one;
//        - any other copy trusts the first certificate it sees and pins it from then on.
//   3. LOGIN, sending the password only to a server with the trusted certificate.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

namespace AssetApp
{
    class LoginForm : Dialog
    {
        readonly TextBox server = new TextBox { Width = 240 };
        readonly TextBox user = new TextBox();
        readonly TextBox password = new TextBox { UseSystemPasswordChar = true };
        readonly Label message = new Label { AutoSize = true, MaximumSize = new Size(320, 0), ForeColor = Color.Firebrick };
        readonly Button find = new Button { Text = "Find servers", AutoSize = true };

        public LoginForm() : base("Asset Management - Log in", "Log in")
        {
            Row("Server", server);
            Row("", find);
            Row("Username", user);
            Row("Password", password);
            Row("", message);

            // A downloaded copy starts with its own server filled in.
            if (SiteSettings.Servers.Count > 0) server.Text = SiteSettings.Servers[0].ToString();
            else server.Text = "localhost";

            find.Click += delegate { FindServers(); };
        }

        // Validate the fields, then start the login in the background. The form stays open
        // (Problem returns a message or "" to stay open without a popup) until it succeeds.
        protected override string Problem()
        {
            Addr typed;
            try { typed = Net.ParseAddress(server.Text); }
            catch (FormatException error) { return error.Message; }
            if (user.Text.Trim().Length == 0) return "Enter your username.";
            if (password.Text.Length == 0) return "Enter your password.";

            string name = user.Text.Trim(), pass = password.Text;
            SetBusy(true, "Connecting...");
            Background.Run<Dictionary<string, object>>(this, delegate { return Connect(typed, name, pass); }, LoggedIn);
            return "";
        }

        void SetBusy(bool busy, string text)
        {
            Ok.Enabled = !busy;
            find.Enabled = !busy;
            message.ForeColor = Color.DimGray;
            message.Text = text;
        }

        // Runs on a background thread. Returns the LOGIN reply, or throws.
        static Dictionary<string, object> Connect(Addr typed, string name, string pass)
        {
            // Try the typed server first, then (for a downloaded copy) the others it knows.
            List<Addr> candidates = new List<Addr> { typed };
            foreach (Addr other in SiteSettings.Servers) if (!candidates.Contains(other)) candidates.Add(other);

            Dictionary<string, object> pong = null;
            string found = null;
            Addr reached = null;
            Exception last = null;
            foreach (Addr candidate in candidates)
            {
                try { pong = Net.Probe(candidate, out found); reached = candidate; break; }
                catch (Exception error) { last = error; }
            }
            if (pong == null) throw new System.IO.IOException("Could not connect to " + typed + ": " + last.Message);

            if (SiteSettings.Fingerprint != null && !found.Equals(SiteSettings.Fingerprint, StringComparison.OrdinalIgnoreCase))
                throw new UntrustedServerException("The server at " + reached +
                    " does not have the certificate this program was downloaded with, so it was not used.");

            Session.Trusted = SiteSettings.Fingerprint ?? found;
            Session.Servers = Net.ServersFromPing(reached, pong);
            Session.Token = null;

            Dictionary<string, object> request = Net.Request("LOGIN");
            request["username"] = name;
            request["password"] = pass;
            return Net.Send(request);
        }

        void LoggedIn(Dictionary<string, object> reply, Exception error)
        {
            password.Clear();   // don't leave the password in the window
            if (error != null) { Fail(error.Message); return; }
            if (J.Text(reply, "status") != "OK")
            {
                Fail(J.Text(reply, "detail") != "" ? J.Text(reply, "detail") : "The login failed.");
                return;
            }
            Session.Token = J.Text(reply, "token");
            Session.Username = J.Text(reply, "username");
            Session.Role = J.Text(reply, "role");
            DialogResult = DialogResult.OK;
        }

        void Fail(string text)
        {
            SetBusy(false, text);
            message.ForeColor = Color.Firebrick;
        }

        // Broadcast on the network and put the first server found in the box.
        void FindServers()
        {
            SetBusy(true, "Looking for servers...");
            Background.Run<List<Addr>>(this, delegate { return Net.Discover(); }, delegate(List<Addr> found, Exception error)
            {
                SetBusy(false, "");
                if (error != null || found.Count == 0)
                {
                    Fail("No servers answered. Type the address of the computer running the server.");
                    return;
                }
                server.Text = found[0].ToString();
                message.ForeColor = Color.DimGray;
                message.Text = "Found " + found.Count + " server(s). Using " + found[0] + ".";
            });
        }
    }
}
