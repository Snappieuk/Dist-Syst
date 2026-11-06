// History, check out / check in, and the Loans tab.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

namespace AssetApp
{
    // The check-out form. Dates are picked from a calendar, so they are always valid
    // YYYY-MM-DD. Start defaults to today and due to a week from today.
    class CheckOutDialog : Dialog
    {
        readonly TextBox borrower = new TextBox { Width = 200 };
        readonly DateTimePicker start = new DateTimePicker { Format = DateTimePickerFormat.Custom, CustomFormat = "yyyy-MM-dd", Value = DateTime.Today };
        readonly DateTimePicker due = new DateTimePicker { Format = DateTimePickerFormat.Custom, CustomFormat = "yyyy-MM-dd", Value = DateTime.Today.AddDays(7) };

        public CheckOutDialog(string assetId) : base("Check out " + assetId, "Check out")
        {
            Row("Borrower", borrower);
            Row("Start", start);
            Row("Due", due);
        }

        public string Borrower { get { return borrower.Text.Trim(); } }
        public string Start { get { return start.Value.ToString("yyyy-MM-dd"); } }
        public string Due { get { return due.Value.ToString("yyyy-MM-dd"); } }

        protected override string Problem()
        {
            if (Borrower.Length == 0) return "Please enter who is borrowing it.";
            if (due.Value.Date < start.Value.Date) return "The due date can't be before the start date.";
            return null;
        }
    }

    partial class MainForm
    {
        void BuildMoreAssetButtons()
        {
            AddButton(assetButtons, "History", delegate { ShowHistory(); });
            AddButton(assetButtons, "Check Out / In", delegate { CheckOutOrIn(); });
        }

        // Show the asset's history, newest last, in a read-only window.
        void ShowHistory()
        {
            Dictionary<string, object> asset = RequireSelected();
            if (asset == null) return;
            using (Form window = new Form { Text = "History of " + J.Text(asset, "asset_id"), Size = new Size(760, 380), StartPosition = FormStartPosition.CenterParent })
            {
                ListView list = NewList();
                list.Columns.Add("When", 150);
                list.Columns.Add("Who", 100);
                list.Columns.Add("Server", 70);
                list.Columns.Add("What happened", 400);
                foreach (Dictionary<string, object> entry in J.Objects(asset, "history"))
                    list.Items.Add(new ListViewItem(new string[] { J.Text(entry, "at"), J.Text(entry, "user"), J.Text(entry, "server"), J.Text(entry, "detail") }));
                window.Controls.Add(list);
                window.ShowDialog(this);
            }
        }

        // Out on loan: confirm, then check it in. Not on loan: ask who has it, then check it out.
        void CheckOutOrIn()
        {
            Dictionary<string, object> asset = RequireSelected();
            if (asset == null) return;
            string id = J.Text(asset, "asset_id");
            Dictionary<string, object> loan = CurrentLoan(asset);

            if (loan != null)
            {
                if (MessageBox.Show(this, id + " is out on loan to " + J.Text(loan, "borrower") + ".\nCheck it back in?",
                                    "Check in", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
                Dictionary<string, object> back = Net.Request("CHECK_IN");
                back["asset_id"] = id;
                back["expected_version"] = J.Number(asset, "version");
                Perform(back, "Asset checked in", null);
                return;
            }

            using (CheckOutDialog dialog = new CheckOutDialog(id))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                Dictionary<string, object> request = Net.Request("CHECK_OUT");
                request["asset_id"] = id;
                request["borrower"] = dialog.Borrower;
                request["start"] = dialog.Start;
                request["due"] = dialog.Due;
                request["expected_version"] = J.Number(asset, "version");
                Perform(request, "Asset checked out", null);
            }
        }

        // ---- Loans tab ----

        readonly ListView loanList = NewList();
        readonly FlowLayoutPanel loanButtons = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(0, 6, 0, 6) };

        void BuildLoansTab()
        {
            TabPage page = new TabPage("Loans");
            AddButton(loanButtons, "Check in", delegate { CheckInSelectedLoan(); });
            loanList.Columns.Add("Asset ID", 100);
            loanList.Columns.Add("Description", 240);
            loanList.Columns.Add("Borrower", 140);
            loanList.Columns.Add("Start", 90);
            loanList.Columns.Add("Due", 90);
            loanList.Columns.Add("Returned", 100);
            page.Controls.Add(loanList);
            page.Controls.Add(loanButtons);   // added last so it sits above the table
            tabs.TabPages.Add(page);
        }

        // Check in the asset of the selected loan. Only a loan still out can be checked in.
        void CheckInSelectedLoan()
        {
            if (loanList.SelectedItems.Count == 0 || loanList.SelectedItems[0].Tag == null)
            {
                MessageBox.Show(this, "Select a loan that is still out (one with no return date) first.", "Check in", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            Dictionary<string, object> asset = (Dictionary<string, object>)loanList.SelectedItems[0].Tag;
            Dictionary<string, object> loan = CurrentLoan(asset);
            if (loan == null) return;
            if (MessageBox.Show(this, J.Text(asset, "asset_id") + " is out on loan to " + J.Text(loan, "borrower") + ".\nCheck it back in?",
                                "Check in", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            Dictionary<string, object> back = Net.Request("CHECK_IN");
            back["asset_id"] = J.Text(asset, "asset_id");
            back["expected_version"] = J.Number(asset, "version");
            Perform(back, "Asset checked in", null);
        }

        // Every loan of the assets shown, oldest start date first. Dates are yyyy-MM-dd,
        // so sorting them as text sorts them by date. A loan still out keeps its asset in
        // the row's Tag, which is what Check in uses.
        void FillLoans()
        {
            List<string[]> rows = new List<string[]>();
            List<Dictionary<string, object>> owners = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> asset in assets)
                foreach (Dictionary<string, object> loan in J.Objects(asset, "loans"))
                {
                    bool out_ = J.Text(loan, "returned") == "";
                    string returned = !out_ ? J.Text(loan, "returned") : (IsOverdue(loan) ? "Overdue" : "");
                    rows.Add(new string[] { J.Text(asset, "asset_id"), J.Text(asset, "description"), J.Text(loan, "borrower"),
                                            J.Text(loan, "start"), J.Text(loan, "due"), returned });
                    owners.Add(out_ ? asset : null);
                }

            // Sort the rows and their owners together, by start date.
            int[] order = new int[rows.Count];
            for (int i = 0; i < order.Length; i++) order[i] = i;
            Array.Sort(order, delegate(int a, int b)
            {
                int byStart = string.CompareOrdinal(rows[a][3], rows[b][3]);
                return byStart != 0 ? byStart : a.CompareTo(b);
            });

            loanList.BeginUpdate();
            loanList.Items.Clear();
            foreach (int i in order)
            {
                ListViewItem item = new ListViewItem(rows[i]);
                item.Tag = owners[i];
                if (rows[i][5] == "Overdue") item.ForeColor = Color.Firebrick;
                loanList.Items.Add(item);
            }
            loanList.EndUpdate();
        }
    }
}
