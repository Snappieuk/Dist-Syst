// The window of the C# client: the same screens and behaviour as gui.py, built with
// Windows Forms, which is part of the .NET Framework. It opens when client.exe is run
// with no arguments, or double-clicked. Build it together with client.cs (see there).
//
// On the login screen, enter the server's IP address or host name (add :port if it
// isn't the usual one), or press "Find servers" to look for it on the local network.
// "Check" tests an address before you log in. Only one address is needed: the server
// tells the window where both the primary and the backup are, and if the primary
// fails, the window moves to the backup by itself.
//
// Certificates: the first time you connect to a server, the window shows the
// fingerprint of its certificate and asks whether to trust it (trust on first use).
// After that it only accepts that exact certificate, so a different server at the
// same address is refused. Server addresses and trusted certificates are remembered
// in .asset_management_csharp.json in your home folder.
//
// What you can do depends on your account; the server checks every request.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;

// A server's address: host name or IP address, and port.
class ServerAddress
{
    public readonly string Host;
    public readonly int Port;

    public ServerAddress(string host, int port) { Host = host; Port = port; }

    public override string ToString() { return Host + ":" + Port; }
    public override bool Equals(object other) { var a = other as ServerAddress; return a != null && a.Host == Host && a.Port == Port; }
    public override int GetHashCode() { return ToString().GetHashCode(); }
}

// The server's certificate is not one this window trusts.
class UntrustedServerException : Exception
{
    public UntrustedServerException(string message) : base(message) { }
}

// What a PING found at an address.
class ProbeResult
{
    public ServerAddress Address;
    public string Role;
    public string Fingerprint;
    public List<ServerAddress> Servers;   // primary first, as the server reports them
}

// ---- Talking to a server ------------------------------------------------------------
static class Network
{
    // Must match the server (common.py and server.py).
    public const int ProtocolVersion = 1;
    public const int MaxMessageBytes = 1048576;
    public const int PrimaryPort = 65432;
    public const int BackupPort = 65433;
    public const string ServiceName = "asset-management";
    static readonly byte[] DiscoveryRequest = Encoding.ASCII.GetBytes("ASSET_MANAGEMENT_DISCOVER");

    const int TimeoutMs = 5000;
    const int DiscoveryMs = 1500;
    static readonly Regex HostName = new Regex(
        @"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$");

    // Turn "host" or "host:port" into an address, or throw ArgumentException saying what's wrong.
    public static ServerAddress ParseAddress(string text, int defaultPort)
    {
        text = (text ?? "").Trim();
        if (text.Length == 0) throw new ArgumentException("Enter the server's IP address or host name.");
        bool hasPort = text.Count(c => c == ':') == 1;
        string host = text;
        int port = defaultPort;
        if (hasPort)
        {
            string[] parts = text.Split(':');
            host = parts[0];
            if (!int.TryParse(parts[1], NumberStyles.None, CultureInfo.InvariantCulture, out port) || port < 1 || port > 65535)
                throw new ArgumentException("'" + parts[1] + "' is not a port. Use a number from 1 to 65535.");
        }
        IPAddress ip;
        bool isIp = host.Contains(":") ? IPAddress.TryParse(host, out ip)
                                       : Regex.IsMatch(host, @"^\d{1,3}(\.\d{1,3}){3}$") && IPAddress.TryParse(host, out ip);
        if (!isIp && (Regex.IsMatch(host, @"^[\d.]+$") || !HostName.IsMatch(host)))
            throw new ArgumentException("'" + host + "' is not a valid IP address or host name.");
        return new ServerAddress(host, port);
    }

    // The SHA-256 fingerprint of a certificate, as people compare it: AB:CD:...
    public static string Fingerprint(byte[] certificate)
    {
        using (var sha = SHA256.Create())
            return string.Join(":", sha.ComputeHash(certificate).Select(b => b.ToString("X2")));
    }

    // An open, encrypted connection to one server, and the fingerprint of its certificate.
    public class Connection : IDisposable
    {
        public TcpClient Client;
        public SslStream Tls;
        public string Fingerprint;

        public void Dispose()
        {
            if (Tls != null) Tls.Dispose();
            Client.Close();
        }
    }

    // Open an encrypted connection. The usual certificate check is switched off because
    // these servers use their own certificate; the caller checks the fingerprint
    // against the trusted ones instead, before anything is sent.
    public static Connection Connect(ServerAddress address)
    {
        var connection = new Connection { Client = new TcpClient() };
        try
        {
            try
            {
                if (!connection.Client.ConnectAsync(address.Host, address.Port).Wait(TimeoutMs))
                    throw new TimeoutException("Timed out connecting to " + address);
            }
            catch (AggregateException e)
            {
                throw e.InnerException;
            }
            connection.Tls = new SslStream(connection.Client.GetStream(), false, (s, c, ch, e) => true);
            connection.Tls.ReadTimeout = TimeoutMs;
            connection.Tls.WriteTimeout = TimeoutMs;
            try
            {
                connection.Tls.AuthenticateAsClient(address.Host, null, SslProtocols.Tls12, false);
            }
            catch (IOException e)
            {
                throw new AuthenticationException("The server did not set up TLS", e);
            }
            connection.Fingerprint = Fingerprint(connection.Tls.RemoteCertificate.GetRawCertData());
            return connection;
        }
        catch
        {
            connection.Dispose();
            throw;
        }
    }

    // Send one request as length-prefixed JSON and return the reply.
    public static Dictionary<string, object> Exchange(Connection connection, Dictionary<string, object> request)
    {
        var message = new Dictionary<string, object>(request);
        message["protocol_version"] = ProtocolVersion;
        byte[] body = Encoding.UTF8.GetBytes(Client.Json.Serialize(message));
        if (body.Length > MaxMessageBytes) throw new InvalidDataException("The request is too large");
        connection.Tls.Write(Client.BigEndian(body.Length).Concat(body).ToArray());
        int length = Client.FromBigEndian(Client.ReadExact(connection.Tls, 4));
        if (length < 0 || length > MaxMessageBytes) throw new InvalidDataException("The reply is too large");
        var reply = Client.Json.DeserializeObject(Encoding.UTF8.GetString(Client.ReadExact(connection.Tls, length)))
            as Dictionary<string, object>;
        if (reply == null) throw new InvalidDataException("The reply is not valid");
        return reply;
    }

    // Check that an address is an Asset Management server. Only PING is sent, which
    // needs no login, so nothing secret goes to an untrusted server.
    public static ProbeResult Probe(ServerAddress address)
    {
        Dictionary<string, object> reply;
        string fingerprint;
        using (var connection = Connect(address))
        {
            fingerprint = connection.Fingerprint;
            reply = Exchange(connection, new Dictionary<string, object> { { "action", "PING" } });
        }
        if (Text(reply, "service") != ServiceName)
            throw new InvalidDataException(address + " answered, but it isn't an Asset Management server.");
        return new ProbeResult
        {
            Address = address,
            Role = Text(reply, "role") ?? "server",
            Fingerprint = fingerprint,
            Servers = ServerAddresses(reply.ContainsKey("servers") ? reply["servers"] : null, address),
        };
    }

    // The primary and backup addresses from a PING reply, primary first. A server set
    // up as 127.0.0.1 (or 0.0.0.0) runs on the same machine as the one that replied,
    // so the address used to reach that one is used instead; that way it still works
    // from other computers. Falls back to just the address used.
    static List<ServerAddress> ServerAddresses(object servers, ServerAddress reached)
    {
        var addresses = new List<ServerAddress>();
        var map = servers as Dictionary<string, object>;
        if (map != null)
        {
            foreach (string role in new[] { "primary", "backup" })
            {
                object value;
                var server = map.TryGetValue(role, out value) ? value as Dictionary<string, object> : null;
                if (server == null || !server.ContainsKey("port") || !(server["port"] is int)) continue;
                string host = Text(server, "host") ?? "";
                if (host == "" || host == "localhost" || host == "0.0.0.0" || host == "::1" || host.StartsWith("127."))
                    host = reached.Host;
                addresses.Add(new ServerAddress(host, (int)server["port"]));
            }
        }
        if (addresses.Count == 0) addresses.Add(reached);
        return addresses;
    }

    static string Text(Dictionary<string, object> map, string key)
    {
        object value;
        return map.TryGetValue(key, out value) ? value as string : null;
    }

    // Whether an exception means a server could not be reached (so try the next one).
    public static bool IsConnectionFailure(Exception e)
    {
        return e is IOException || e is SocketException || e is AuthenticationException || e is TimeoutException;
    }

    // A short, plain reason a connection failed.
    public static string Explain(Exception e)
    {
        var socket = e as SocketException;
        if (socket != null)
        {
            switch (socket.SocketErrorCode)
            {
                case SocketError.HostNotFound:
                case SocketError.NoData:
                case SocketError.TryAgain:
                    return "no such host name";
                case SocketError.ConnectionRefused:
                case SocketError.TimedOut:
                    return "nothing answered there";
            }
        }
        if (e is TimeoutException) return "nothing answered there";
        if (e is AuthenticationException) return "not an Asset Management server (no TLS)";
        return e.Message;
    }

    // Send a request to the primary, or the backup if the primary can't be reached.
    // The certificate is checked before anything is sent. A server not checked at login
    // is accepted only if it has the same certificate as one already trusted (the
    // primary and backup share one).
    public static Tuple<Dictionary<string, object>, ServerAddress> SendToServers(
        List<ServerAddress> addresses, Dictionary<string, string> trusted, Dictionary<string, object> request)
    {
        Exception lastError = new IOException("No server address");
        foreach (var address in addresses)
        {
            Connection connection;
            try
            {
                connection = Connect(address);
            }
            catch (Exception e)
            {
                if (!IsConnectionFailure(e)) throw;
                lastError = e;
                continue;
            }
            using (connection)
            {
                string key = address.ToString(), pinned;
                if (trusted.TryGetValue(key, out pinned) && pinned != connection.Fingerprint
                    || !trusted.ContainsValue(connection.Fingerprint))
                    throw new UntrustedServerException("The server at " + key + " is not trusted. Log in again to check it.");
                return Tuple.Create(Exchange(connection, request), address);
            }
        }
        throw lastError;
    }

    // What "Check" reports for an address.
    public static string CheckAddress(ServerAddress address, Dictionary<string, string> trusted)
    {
        ProbeResult found;
        try
        {
            found = Probe(address);
        }
        catch (Exception e)
        {
            if (!IsConnectionFailure(e) && !(e is InvalidDataException)) throw;
            return address + ": " + Explain(e);
        }
        string pinned;
        bool known = trusted.TryGetValue(address.ToString(), out pinned) && pinned == found.Fingerprint;
        var others = found.Servers.Where(a => !a.Equals(address)).Select(a => a.ToString()).ToList();
        return address + ": " + found.Role + " server found"
            + (known ? "" : " (certificate not yet trusted)")
            + (others.Count > 0 ? "; it reports the other server at " + string.Join(", ", others) : "");
    }

    // Probe each address in turn and return what the first that answers reports. The
    // remembered addresses come after the typed one, so the window can still log in to
    // the backup when the primary is already down.
    public static ProbeResult FirstAnswer(List<ServerAddress> addresses)
    {
        Exception lastError = new IOException("no server answered at that address");
        foreach (var address in addresses)
        {
            try
            {
                return Probe(address);
            }
            catch (Exception e)
            {
                if (!IsConnectionFailure(e) && !(e is InvalidDataException)) throw;
                lastError = e;
            }
        }
        throw lastError;
    }

    // Broadcast on the local network and return the servers that answer, primary first.
    public static List<ProbeResult> Discover(int[] ports)
    {
        var found = new Dictionary<string, ProbeResult>();
        using (var udp = new UdpClient(0))
        {
            udp.EnableBroadcast = true;
            udp.Client.ReceiveTimeout = 200;
            foreach (int port in ports)
            {
                foreach (var target in new[] { IPAddress.Broadcast, IPAddress.Loopback })   // this machine too
                {
                    try { udp.Send(DiscoveryRequest, DiscoveryRequest.Length, new IPEndPoint(target, port)); }
                    catch (SocketException) { }
                }
            }
            var deadline = DateTime.UtcNow.AddMilliseconds(DiscoveryMs);
            while (DateTime.UtcNow < deadline)
            {
                Dictionary<string, object> info;
                IPEndPoint sender = new IPEndPoint(IPAddress.Any, 0);
                try
                {
                    byte[] data = udp.Receive(ref sender);
                    info = Client.Json.DeserializeObject(Encoding.UTF8.GetString(data)) as Dictionary<string, object>;
                }
                catch (SocketException e)
                {
                    // Windows reports "port unreachable" from a port with no server as an
                    // error on the next receive; it only means nothing is there.
                    if (e.SocketErrorCode == SocketError.TimedOut || e.SocketErrorCode == SocketError.ConnectionReset) continue;
                    break;
                }
                catch (ArgumentException)
                {
                    continue;   // not JSON
                }
                if (info == null || Text(info, "service") != ServiceName || !(info.ContainsKey("port") && info["port"] is int)) continue;
                string role = Text(info, "role") ?? "server";
                string host = sender.Address.ToString();
                string key = role + "/" + info["port"];
                // The same server can answer on 127.0.0.1 and its network address; keep
                // the network one, which works from other machines too.
                if (!found.ContainsKey(key) || found[key].Address.Host.StartsWith("127."))
                    found[key] = new ProbeResult { Role = role, Address = new ServerAddress(host, (int)info["port"]) };
            }
        }
        return found.Values.OrderBy(server => server.Role != "primary").ToList();
    }
}

// ---- Remembered settings ----------------------------------------------------------
class Settings
{
    public string Server = "";
    public List<string> Servers = new List<string>();
    public Dictionary<string, string> Trusted = new Dictionary<string, string>();

    public static readonly string DefaultFile =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".asset_management_csharp.json");

    public static Settings Load(string path)
    {
        try
        {
            var loaded = Client.Json.Deserialize<Settings>(File.ReadAllText(path, Encoding.UTF8));
            if (loaded != null && loaded.Trusted != null)
            {
                if (loaded.Servers == null) loaded.Servers = new List<string>();
                if (loaded.Server == null) loaded.Server = "";
                return loaded;
            }
        }
        catch (Exception e)
        {
            if (!(e is IOException || e is UnauthorizedAccessException || e is ArgumentException || e is InvalidOperationException)) throw;
        }
        return new Settings();
    }

    public void Save(string path)
    {
        try
        {
            File.WriteAllText(path, Client.Json.Serialize(this), new UTF8Encoding(false));
        }
        catch (Exception e)
        {
            // Not being able to remember is no reason to stop working.
            if (!(e is IOException || e is UnauthorizedAccessException)) throw;
        }
    }
}

// ---- The window: a login screen, then the asset screen ------------------------------
class AssetWindow : Form
{
    const int PageSize = 20;
    const string AnyField = "Any field";
    static readonly string[] AssetFields = { "asset_id", "description", "category", "location", "status" };
    static readonly string[] EditableFields = { "description", "category", "location", "status" };
    static readonly string[] Statuses = { "In Use", "Not In Use" };
    static readonly Dictionary<string, string> Labels = new Dictionary<string, string>
    {
        { "asset_id", "Asset ID" }, { "description", "Description" }, { "category", "Category" },
        { "location", "Location" }, { "status", "Status" }, { "updated_at", "Last changed" },
    };
    static readonly string[] Columns = { "asset_id", "description", "category", "location", "status", "updated_at" };

    readonly string settingsFile;
    readonly Settings settings;
    List<ServerAddress> addresses = new List<ServerAddress>();   // [primary, backup], as the server reported them
    string token;
    string role;
    int page = 1;
    Dictionary<string, object> search = new Dictionary<string, object>();   // the LIST parameters of the search shown
    Dictionary<string, Dictionary<string, object>> shown = new Dictionary<string, Dictionary<string, object>>();
    string notice;   // a message to show once the table has refreshed

    // Login screen
    Panel loginScreen;
    TextBox serverBox, usernameBox, passwordBox;
    Button loginButton;

    // Asset screen
    TableLayoutPanel mainScreen;
    ComboBox searchField;
    TextBox searchBox;
    Button searchButton;
    Label userLabel, pageLabel, serverLabel;
    ListView table;
    Button previousButton, nextButton, deleteButton;
    Dictionary<string, Control> fields = new Dictionary<string, Control>();
    ToolStripStatusLabel statusText;

    [DllImport("kernel32.dll")] static extern uint GetConsoleProcessList(uint[] processes, uint count);
    [DllImport("kernel32.dll")] static extern bool FreeConsole();

    public static void Start()
    {
        // Double-clicking client.exe opens a console window for it as well; close that
        // one. A console that was already open (a terminal) is shared, so it is left alone.
        if (GetConsoleProcessList(new uint[2], 2) == 1) FreeConsole();
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        // Report anything unexpected instead of closing the window.
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (s, e) =>
            MessageBox.Show(e.Exception.Message, "Unexpected error", MessageBoxButtons.OK, MessageBoxIcon.Error);
        Application.Run(new AssetWindow(Settings.DefaultFile));
    }

    public AssetWindow(string settingsFile)
    {
        this.settingsFile = settingsFile;
        settings = Settings.Load(settingsFile);

        Text = "Asset Management System";
        Font = new Font("Segoe UI", 9F);
        AutoScaleMode = AutoScaleMode.Font;
        MinimumSize = new Size(940, 560);
        Size = new Size(980, 620);
        StartPosition = FormStartPosition.CenterScreen;

        var statusBar = new StatusStrip { SizingGrip = false };
        statusText = new ToolStripStatusLabel("Enter a server, or press Find servers, then log in.")
            { Spring = true, TextAlign = ContentAlignment.MiddleLeft };
        statusBar.Items.Add(statusText);

        BuildLogin();
        BuildMain();
        Controls.Add(mainScreen);
        Controls.Add(loginScreen);
        Controls.Add(statusBar);
        ShowLogin();
    }

    // ---- Layout -------------------------------------------------------------------

    static Button MakeButton(string text, EventHandler onClick)
    {
        var button = new Button { Text = text, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Padding = new Padding(6, 0, 6, 0) };
        button.Click += onClick;
        return button;
    }

    static Label MakeLabel(string text)
    {
        return new Label { Text = text, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 6, 3, 3) };
    }

    void BuildLogin()
    {
        loginScreen = new Panel { Dock = DockStyle.Fill, Padding = new Padding(20) };
        var box = new GroupBox { Text = "Connect and log in", AutoSize = true, Location = new Point(20, 20), Padding = new Padding(12) };
        var grid = new TableLayoutPanel { ColumnCount = 4, RowCount = 4, AutoSize = true, Dock = DockStyle.Fill };

        serverBox = new TextBox { Width = 240, Text = settings.Server };
        usernameBox = new TextBox { Width = 240 };
        passwordBox = new TextBox { Width = 240, UseSystemPasswordChar = true };
        loginButton = MakeButton("Log in", (s, e) => LogIn());

        grid.Controls.Add(MakeLabel("Server"), 0, 0);
        grid.Controls.Add(serverBox, 1, 0);
        grid.Controls.Add(MakeButton("Check", (s, e) => CheckServer()), 2, 0);
        grid.Controls.Add(MakeButton("Find servers", (s, e) => FindServers()), 3, 0);
        var userCaption = MakeLabel("Username");
        userCaption.Margin = new Padding(3, 18, 3, 3);
        usernameBox.Margin = new Padding(3, 15, 3, 3);
        grid.Controls.Add(userCaption, 0, 1);
        grid.Controls.Add(usernameBox, 1, 1);
        grid.Controls.Add(MakeLabel("Password"), 0, 2);
        grid.Controls.Add(passwordBox, 1, 2);
        loginButton.Anchor = AnchorStyles.Right;
        grid.Controls.Add(loginButton, 1, 3);

        box.Controls.Add(grid);
        loginScreen.Controls.Add(box);
    }

    void BuildMain()
    {
        mainScreen = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5, Padding = new Padding(8) };
        mainScreen.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        mainScreen.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        mainScreen.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        mainScreen.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        mainScreen.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        // Search bar
        var searchBar = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 6, RowCount = 1 };
        for (int i = 0; i < 5; i++) searchBar.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        searchBar.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        searchField = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 120 };
        searchField.Items.Add(AnyField);
        foreach (string field in AssetFields) searchField.Items.Add(Labels[field]);
        searchField.SelectedIndex = 0;
        searchBox = new TextBox { Width = 240 };
        searchButton = MakeButton("Search", (s, e) => RunSearch());
        userLabel = new Label { AutoSize = true, Anchor = AnchorStyles.Right, Margin = new Padding(3, 6, 3, 3) };
        searchBar.Controls.Add(MakeLabel("Search"), 0, 0);
        searchBar.Controls.Add(searchField, 1, 0);
        searchBar.Controls.Add(searchBox, 2, 0);
        searchBar.Controls.Add(searchButton, 3, 0);
        searchBar.Controls.Add(MakeButton("Show all", (s, e) => ClearSearch()), 4, 0);
        searchBar.Controls.Add(userLabel, 5, 0);

        // Asset table
        table = new ListView
        {
            Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true, MultiSelect = false,
            HideSelection = false, GridLines = true,
        };
        foreach (string column in Columns) table.Columns.Add(Labels[column], column == "description" ? 220 : 130);
        table.SelectedIndexChanged += (s, e) => LoadSelected();

        // Paging
        var paging = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 4, RowCount = 1 };
        for (int i = 0; i < 3; i++) paging.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        paging.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        previousButton = MakeButton("< Previous", (s, e) => ChangePage(-1));
        nextButton = MakeButton("Next >", (s, e) => ChangePage(1));
        pageLabel = MakeLabel("");
        serverLabel = new Label { AutoSize = true, Anchor = AnchorStyles.Right, Margin = new Padding(3, 6, 3, 3) };
        paging.Controls.Add(previousButton, 0, 0);
        paging.Controls.Add(pageLabel, 1, 0);
        paging.Controls.Add(nextButton, 2, 0);
        paging.Controls.Add(serverLabel, 3, 0);

        // Form
        var form = new GroupBox { Text = "Asset details (every field is required)", Dock = DockStyle.Fill, AutoSize = true, Padding = new Padding(8) };
        var formGrid = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = AssetFields.Length, RowCount = 2 };
        for (int column = 0; column < AssetFields.Length; column++)
        {
            formGrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F / AssetFields.Length));
            string field = AssetFields[column];
            Control entry;
            if (field == "status")
            {
                var combo = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };
                combo.Items.AddRange(Statuses);
                combo.SelectedIndex = 0;
                entry = combo;
            }
            else
            {
                entry = new TextBox();
            }
            entry.Dock = DockStyle.Fill;
            formGrid.Controls.Add(MakeLabel(Labels[field]), column, 0);
            formGrid.Controls.Add(entry, column, 1);
            fields[field] = entry;
        }
        form.Controls.Add(formGrid);

        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
        deleteButton = MakeButton("Delete selected", (s, e) => DeleteSelected());
        buttons.Controls.Add(MakeButton("Register new asset", (s, e) => Register()));
        buttons.Controls.Add(MakeButton("Save changes", (s, e) => SaveChanges()));
        buttons.Controls.Add(deleteButton);
        buttons.Controls.Add(MakeButton("Clear form", (s, e) => ClearForm()));

        mainScreen.Controls.Add(searchBar, 0, 0);
        mainScreen.Controls.Add(table, 0, 1);
        mainScreen.Controls.Add(paging, 0, 2);
        mainScreen.Controls.Add(form, 0, 3);
        mainScreen.Controls.Add(buttons, 0, 4);
    }

    // ---- Background work ----------------------------------------------------------

    // Run `work` on a background thread so the window never freezes. Awaiting it comes
    // back to the window's own thread, the only one allowed to touch the window.
    Task<T> InBackground<T>(Func<T> work)
    {
        statusText.Text = "Working...";
        return Task.Run(work);
    }

    // ---- Finding and checking servers -----------------------------------------------

    ServerAddress ReadAddress()
    {
        return Network.ParseAddress(serverBox.Text, Network.PrimaryPort);
    }

    async void FindServers()
    {
        List<ProbeResult> result;
        try
        {
            result = await InBackground(() => Network.Discover(new[] { Network.PrimaryPort, Network.BackupPort }));
        }
        catch (SocketException)
        {
            result = new List<ProbeResult>();
        }
        if (result.Count == 0)
        {
            statusText.Text = "No servers found on this network. Enter the address by hand.";
            return;
        }
        serverBox.Text = result[0].Address.ToString();   // the primary if it answered, otherwise the backup
        statusText.Text = "Found " + string.Join(", ", result.Select(s => s.Role + " at " + s.Address)) + ".";
    }

    async void CheckServer()
    {
        ServerAddress address;
        try
        {
            address = ReadAddress();
        }
        catch (ArgumentException e)
        {
            statusText.Text = e.Message;
            return;
        }
        var trusted = new Dictionary<string, string>(settings.Trusted);
        statusText.Text = await InBackground(() => Network.CheckAddress(address, trusted));
    }

    // Decide whether to trust a server's certificate (trust on first use). A certificate
    // already trusted, for this address or another, is accepted. A different one at a
    // trusted address is refused outright: it may be someone pretending to be the
    // server. Otherwise the user is asked, and shown the fingerprint to compare with the
    // one the administrator gave out.
    bool Trust(ServerAddress address, string fingerprint)
    {
        string key = address.ToString(), pinned;
        bool known = settings.Trusted.TryGetValue(key, out pinned);
        if (known && pinned == fingerprint) return true;
        if (!known && settings.Trusted.ContainsValue(fingerprint))
        {
            // Same certificate as a server already trusted: the primary and backup share one.
            settings.Trusted[key] = fingerprint;
            settings.Save(settingsFile);
            return true;
        }
        if (known)
        {
            MessageBox.Show(this,
                "The server at " + key + " has a different certificate from last time, so it was not trusted. "
                + "If your administrator replaced it, delete " + settingsFile + " and connect again.",
                "Certificate changed", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return false;
        }
        var answer = MessageBox.Show(this,
            "First connection to " + key + ".\n\nCertificate fingerprint:\n" + fingerprint
            + "\n\nOnly trust it if this matches the fingerprint your administrator gave you.",
            "Trust this server?", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
        if (answer != DialogResult.Yes) return false;
        settings.Trusted[key] = fingerprint;
        settings.Save(settingsFile);
        return true;
    }

    // ---- Requests -------------------------------------------------------------------

    // Send a request in the background and return the server's reply. Returns null if
    // the session has expired, after going back to the login screen.
    async Task<Dictionary<string, object>> Send(Dictionary<string, object> request)
    {
        if (token != null) request["token"] = token;
        var targets = new List<ServerAddress>(addresses);
        var trusted = new Dictionary<string, string>(settings.Trusted);
        Tuple<Dictionary<string, object>, ServerAddress> result;
        try
        {
            result = await InBackground(() => Network.SendToServers(targets, trusted, request));
        }
        catch (Exception e)
        {
            if (!(e is UntrustedServerException) && !Network.IsConnectionFailure(e) && !(e is InvalidDataException)) throw;
            string message = e is UntrustedServerException ? e.Message : "Could not reach the server: " + Network.Explain(e);
            return new Dictionary<string, object> { { "status", "ERROR" }, { "code", "NO_SERVER" }, { "message", message } };
        }
        var reply = result.Item1;
        var address = result.Item2;
        bool backup = addresses.Count > 1 && address.Equals(addresses[1]);
        serverLabel.Text = "Server: " + address + (backup ? " (backup)" : "");
        if (Get(reply, "code") == "UNAUTHENTICATED" && token != null)
        {
            ShowLogin();
            statusText.Text = Get(reply, "message");
            return null;
        }
        return reply;
    }

    static string Get(Dictionary<string, object> map, string key)
    {
        object value;
        return map.TryGetValue(key, out value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : null;
    }

    static bool IsOk(Dictionary<string, object> reply)
    {
        return Get(reply, "status") == "OK";
    }

    void ReportError(Dictionary<string, object> reply)
    {
        statusText.Text = Get(reply, "code") + ": " + Get(reply, "message");
    }

    // ---- Logging in -----------------------------------------------------------------

    // Check the server and its certificate, learn where both servers are, then log in.
    async void LogIn()
    {
        ServerAddress typed;
        try
        {
            typed = ReadAddress();
        }
        catch (ArgumentException e)
        {
            statusText.Text = e.Message;
            return;
        }
        token = null;
        var candidates = new List<ServerAddress> { typed };
        foreach (string text in settings.Servers)
        {
            try
            {
                var remembered = Network.ParseAddress(text, Network.PrimaryPort);
                if (!candidates.Contains(remembered)) candidates.Add(remembered);
            }
            catch (ArgumentException) { }
        }

        ProbeResult found;
        try
        {
            found = await InBackground(() => Network.FirstAnswer(candidates));
        }
        catch (Exception e)
        {
            if (!Network.IsConnectionFailure(e) && !(e is InvalidDataException)) throw;
            statusText.Text = "Could not connect: " + Network.Explain(e);
            return;
        }
        if (!Trust(found.Address, found.Fingerprint))
        {
            statusText.Text = "Not connected: " + found.Address + " was not trusted.";
            return;
        }
        // From now on every request goes to the primary, or the backup if the primary
        // can't be reached, whichever address was typed.
        addresses = found.Servers;
        settings.Server = serverBox.Text.Trim();
        settings.Servers = addresses.Select(a => a.ToString()).ToList();
        settings.Save(settingsFile);

        var reply = await Send(new Dictionary<string, object>
        {
            { "action", "LOGIN" }, { "username", usernameBox.Text.Trim() }, { "password", passwordBox.Text },
        });
        if (reply == null) return;
        if (!IsOk(reply))
        {
            ReportError(reply);
            return;
        }
        token = Get(reply, "token");
        role = Get(reply, "role");
        passwordBox.Clear();
        userLabel.Text = "Logged in as " + Get(reply, "username") + " (" + role + ")";
        deleteButton.Enabled = role == "admin";
        loginScreen.Visible = false;
        mainScreen.Visible = true;
        AcceptButton = searchButton;
        Refresh_();
    }

    void ShowLogin()
    {
        token = null;
        mainScreen.Visible = false;
        loginScreen.Visible = true;
        AcceptButton = loginButton;
    }

    // ---- Viewing and searching ------------------------------------------------------

    async void Refresh_()
    {
        var request = new Dictionary<string, object> { { "action", "LIST" }, { "page", page }, { "page_size", PageSize } };
        foreach (var entry in search) request[entry.Key] = entry.Value;
        var reply = await Send(request);
        if (reply != null) ShowAssets(reply);
    }

    void ShowAssets(Dictionary<string, object> reply)
    {
        if (!IsOk(reply))
        {
            ReportError(reply);
            return;
        }
        var assets = ((IEnumerable)reply["assets"]).Cast<Dictionary<string, object>>().ToList();
        int replyPage = Convert.ToInt32(reply["page"]), pages = Convert.ToInt32(reply["pages"]), total = Convert.ToInt32(reply["total"]);
        if (assets.Count == 0 && page > 1)   // the last page emptied: step back
        {
            page = Math.Max(1, pages);
            Refresh_();
            return;
        }
        table.BeginUpdate();
        table.Items.Clear();
        shown.Clear();
        foreach (var asset in assets)
        {
            string id = Get(asset, "asset_id");
            shown[id] = asset;
            var item = new ListViewItem(Columns.Select(c => Get(asset, c) ?? "").ToArray()) { Name = id };
            table.Items.Add(item);
        }
        table.EndUpdate();
        pageLabel.Text = "Page " + replyPage + " of " + pages + " (" + total + " assets)";
        previousButton.Enabled = replyPage > 1;
        nextButton.Enabled = replyPage < pages;
        if (notice != null)
        {
            statusText.Text = notice;
            notice = null;
        }
        else if (total == 0)
        {
            statusText.Text = search.Count > 0 ? "No assets match this search." : "No assets registered yet.";
        }
        else
        {
            statusText.Text = "Showing " + assets.Count + " of " + total + " assets.";
        }
    }

    void RunSearch()
    {
        string text = searchBox.Text.Trim();
        string label = (string)searchField.SelectedItem;
        search = new Dictionary<string, object>();
        if (text.Length > 0 && label == AnyField)
        {
            search["query"] = text;
        }
        else if (text.Length > 0)
        {
            string field = Labels.First(entry => entry.Value == label).Key;
            search["filters"] = new Dictionary<string, object> { { field, text } };
        }
        page = 1;
        Refresh_();
    }

    void ClearSearch()
    {
        searchBox.Clear();
        searchField.SelectedIndex = 0;
        RunSearch();
    }

    void ChangePage(int step)
    {
        page = Math.Max(1, page + step);
        Refresh_();
    }

    // ---- The form -------------------------------------------------------------------

    Dictionary<string, object> FormValues()
    {
        var values = new Dictionary<string, object>();
        foreach (var entry in fields)
            values[entry.Key] = entry.Value is ComboBox ? (string)((ComboBox)entry.Value).SelectedItem : entry.Value.Text.Trim();
        return values;
    }

    Dictionary<string, object> Selected()
    {
        if (table.SelectedItems.Count == 0) return null;
        Dictionary<string, object> asset;
        return shown.TryGetValue(table.SelectedItems[0].Name, out asset) ? asset : null;
    }

    void LoadSelected()
    {
        var asset = Selected();
        if (asset == null) return;
        foreach (var entry in fields)
        {
            var combo = entry.Value as ComboBox;
            if (combo != null) combo.SelectedItem = Get(asset, entry.Key);
            else entry.Value.Text = Get(asset, entry.Key);
        }
        ((TextBox)fields["asset_id"]).ReadOnly = true;   // IDs can't be edited
    }

    void ClearForm()
    {
        table.SelectedItems.Clear();
        foreach (var entry in fields)
        {
            var combo = entry.Value as ComboBox;
            if (combo != null) combo.SelectedIndex = 0;
            else entry.Value.Text = "";
        }
        ((TextBox)fields["asset_id"]).ReadOnly = false;
    }

    // ---- Changes --------------------------------------------------------------------

    async void SendChange(Dictionary<string, object> request, string success)
    {
        var reply = await Send(request);
        if (reply == null) return;
        if (!IsOk(reply))
        {
            ReportError(reply);
            if (Get(reply, "code") == "CONFLICT")
            {
                // Show the latest data, keeping the explanation on screen.
                notice = Get(reply, "message") + " The list now shows the latest data.";
                Refresh_();
            }
            return;
        }
        ClearForm();
        notice = success;
        Refresh_();
    }

    static string NewRequestId()
    {
        return Guid.NewGuid().ToString("N");
    }

    void Register()
    {
        var values = FormValues();
        SendChange(new Dictionary<string, object>
        {
            { "action", "REGISTER" }, { "request_id", NewRequestId() }, { "asset", values },
        }, "Registered " + values["asset_id"] + ".");
    }

    void SaveChanges()
    {
        var asset = Selected();
        if (asset == null)
        {
            statusText.Text = "Select an asset to edit first.";
            return;
        }
        var values = FormValues();
        var changes = new Dictionary<string, object>();
        foreach (string field in EditableFields)
            if ((string)values[field] != Get(asset, field)) changes[field] = values[field];
        if (changes.Count == 0)
        {
            statusText.Text = "Nothing has changed.";
            return;
        }
        SendChange(new Dictionary<string, object>
        {
            { "action", "EDIT" }, { "request_id", NewRequestId() }, { "asset_id", Get(asset, "asset_id") },
            { "expected_version", asset["version"] }, { "changes", changes },
        }, "Saved changes to " + Get(asset, "asset_id") + ".");
    }

    void DeleteSelected()
    {
        var asset = Selected();
        if (asset == null)
        {
            statusText.Text = "Select an asset to delete first.";
            return;
        }
        string id = Get(asset, "asset_id");
        if (MessageBox.Show(this, "Delete " + id + "?", "Delete asset", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
            return;
        SendChange(new Dictionary<string, object>
        {
            { "action", "DELETE" }, { "request_id", NewRequestId() }, { "asset_id", id },
            { "expected_version", asset["version"] },
        }, "Deleted " + id + ".");
    }
}
