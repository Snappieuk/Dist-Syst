// The main window: tabs for the assets, the loans and the flagged-for-review list.
// Every action follows one pattern: send a request on a background thread, say how it
// went in the status bar, and fetch the list again so the tables show the change.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

namespace AssetApp
{
    partial class MainForm : Form
    {
        // The assets from the last LIST, which every tab shows a part of.
        List<Dictionary<string, object>> assets = new List<Dictionary<string, object>>();

        readonly TabControl tabs = new TabControl { Dock = DockStyle.Fill };
        readonly StatusStrip statusBar = new StatusStrip();
        readonly ToolStripStatusLabel statusText = new ToolStripStatusLabel { Spring = true, TextAlign = ContentAlignment.MiddleLeft };

        // Assets tab
        readonly TextBox search = new TextBox { Width = 200 };
        readonly ComboBox statusFilter = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 110 };
        readonly ListView assetList = NewList();
        readonly FlowLayoutPanel assetButtons = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(0, 6, 0, 6) };

        // The window is unusable while a request is running, so two can't overlap.
        bool busy;

        public MainForm()
        {
            Text = "Asset Management - " + Session.Username + " (" + Session.Role + ")";
            Size = new Size(1000, 640);
            StartPosition = FormStartPosition.CenterScreen;

            statusBar.Items.Add(statusText);
            Controls.Add(tabs);
            Controls.Add(statusBar);
            tabs.BringToFront();   // so the status bar keeps its place at the bottom

            BuildAssetsTab();
            BuildLoansTab();
            BuildReviewTab();

            Shown += delegate { Refresh_(); };
        }

        // A table with the look used on every tab: full-row selection, column headers, no grid.
        static ListView NewList()
        {
            return new ListView { Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true, HideSelection = false, MultiSelect = false };
        }

        // ---- Assets tab ----

        void BuildAssetsTab()
        {
            TabPage page = new TabPage("Assets");

            FlowLayoutPanel top = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(0, 6, 0, 6) };
            statusFilter.Items.AddRange(new object[] { "All statuses", "In Use", "Not In Use" });
            statusFilter.SelectedIndex = 0;
            Button go = new Button { Text = "Search", AutoSize = true };
            Button clear = new Button { Text = "Clear", AutoSize = true };
            go.Click += delegate { Refresh_(); };
            clear.Click += delegate { search.Clear(); statusFilter.SelectedIndex = 0; Refresh_(); };
            statusFilter.SelectedIndexChanged += delegate { if (!busy) Refresh_(); };
            search.KeyDown += delegate(object s, KeyEventArgs e) { if (e.KeyCode == Keys.Enter) { e.SuppressKeyPress = true; Refresh_(); } };
            top.Controls.AddRange(new Control[] { new Label { Text = "Search", AutoSize = true, Margin = new Padding(0, 6, 4, 0) }, search, statusFilter, go, clear });

            assetList.Columns.Add("ID", 100);
            assetList.Columns.Add("Description", 280);
            assetList.Columns.Add("Category", 110);
            assetList.Columns.Add("Location", 130);
            assetList.Columns.Add("Status", 80);
            assetList.Columns.Add("Registered", 90);
            assetList.Columns.Add("Loan", 150);

            // Added last so it fills what the rows above leave. Docked controls are laid out
            // from the last one added to the first.
            page.Controls.Add(assetList);
            page.Controls.Add(assetButtons);
            page.Controls.Add(top);
            tabs.TabPages.Add(page);
        }

        // The asset selected in the Assets tab, or null.
        Dictionary<string, object> Selected()
        {
            return assetList.SelectedItems.Count == 0 ? null : (Dictionary<string, object>)assetList.SelectedItems[0].Tag;
        }

        // The loan an asset is out on now, or null. The last loan is the current one, if not returned.
        static Dictionary<string, object> CurrentLoan(Dictionary<string, object> asset)
        {
            List<Dictionary<string, object>> loans = J.Objects(asset, "loans");
            if (loans.Count == 0) return null;
            Dictionary<string, object> last = loans[loans.Count - 1];
            return J.Text(last, "returned") == "" ? last : null;
        }

        // yyyy-MM-dd text compares correctly as plain text, so "overdue" is a text comparison.
        static string Today() { return DateTime.Today.ToString("yyyy-MM-dd"); }

        static bool IsOverdue(Dictionary<string, object> loan)
        {
            return loan != null && string.CompareOrdinal(J.Text(loan, "due"), Today()) < 0;
        }

        void FillAssets()
        {
            assetList.BeginUpdate();
            assetList.Items.Clear();
            foreach (Dictionary<string, object> asset in assets)
            {
                Dictionary<string, object> loan = CurrentLoan(asset);
                string loanText = loan == null ? "" : (IsOverdue(loan) ? "Overdue: " : "Out: ") + J.Text(loan, "borrower");
                ListViewItem row = new ListViewItem(new string[] {
                    J.Text(asset, "asset_id"), J.Text(asset, "description"), J.Text(asset, "category"),
                    J.Text(asset, "location"), J.Text(asset, "status"), J.Text(asset, "date_registered"), loanText });
                row.Tag = asset;
                if (J.Flag(asset, "flagged")) row.ForeColor = Color.DarkOrange;
                else if (IsOverdue(loan)) row.ForeColor = Color.Firebrick;
                assetList.Items.Add(row);
            }
            assetList.EndUpdate();
        }

        // ---- Talking to the servers ----

        // The LIST request for what the search box and filter ask for.
        Dictionary<string, object> ListRequest()
        {
            Dictionary<string, object> request = Net.Request("LIST");
            if (search.Text.Trim().Length > 0) request["query"] = search.Text.Trim();
            if (statusFilter.SelectedIndex > 0) request["status"] = statusFilter.Text;
            return request;
        }

        void SetBusy(bool value, string text)
        {
            busy = value;
            tabs.Enabled = !value;
            statusText.Text = text;
            UseWaitCursor = value;
        }

        // Fetch the list again and redraw every tab.
        // (Named with a trailing underscore because Form already has a Refresh method.)
        void Refresh_()
        {
            Perform(null, null);
        }

        // Send `request` (if any), then fetch the list. `success` is shown in the status bar
        // when the request worked. A failed request shows a message box and still refreshes,
        // because "someone else changed it" is the usual reason and the list is then stale.
        void Perform(Dictionary<string, object> request, string success)
        {
            if (busy) return;
            SetBusy(true, "Connecting to the asset server...");
            Dictionary<string, object> listRequest = ListRequest();   // read on this thread: it reads widgets

            Background.Run<object[]>(this, delegate
            {
                Dictionary<string, object> reply = request == null ? null : Net.Send(request);
                Dictionary<string, object> list = Net.Send(listRequest);
                return new object[] { reply, list };
            }, delegate(object[] results, Exception error)
            {
                SetBusy(false, "");
                if (error != null)
                {
                    statusText.Text = "Not connected.";
                    MessageBox.Show(this, error.Message, "Connection problem", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }
                Dictionary<string, object> reply = (Dictionary<string, object>)results[0];
                Dictionary<string, object> list = (Dictionary<string, object>)results[1];

                if (J.Text(list, "status") == "OK")
                {
                    assets = J.Objects(list, "assets");
                    FillAssets();
                    FillLoans();
                    FillReview();
                    statusText.Text = assets.Count + " assets, served by " + J.Text(list, "served_by");
                }
                else statusText.Text = J.Text(list, "detail");

                if (reply != null)
                {
                    if (J.Text(reply, "status") == "OK") statusText.Text = success + "  (" + statusText.Text + ")";
                    else MessageBox.Show(this, J.Text(reply, "detail"), "Not done", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            });
        }
    }
}
