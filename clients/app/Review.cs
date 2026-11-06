// The Review tab: assets flagged for review. Approving keeps one; denying deletes it.

using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

namespace AssetApp
{
    partial class MainForm
    {
        readonly ListView reviewList = new ListView { Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true, CheckBoxes = true };
        readonly FlowLayoutPanel reviewButtons = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(0, 6, 0, 6) };
        TabPage reviewPage;

        void BuildReviewTab()
        {
            reviewPage = new TabPage("Review");
            AddButton(reviewButtons, "Approve ticked", delegate { Review(false, true); });
            AddButton(reviewButtons, "Deny ticked", delegate { Review(true, true); });
            AddButton(reviewButtons, "Approve all", delegate { Review(false, false); });
            AddButton(reviewButtons, "Deny all", delegate { Review(true, false); });

            reviewList.Columns.Add("ID", 100);
            reviewList.Columns.Add("Description", 300);
            reviewList.Columns.Add("Category", 120);
            reviewList.Columns.Add("Location", 140);
            reviewPage.Controls.Add(reviewList);
            reviewPage.Controls.Add(reviewButtons);
            tabs.TabPages.Add(reviewPage);
        }

        // Show the flagged assets, and put their number in the tab's title.
        void FillReview()
        {
            reviewList.BeginUpdate();
            reviewList.Items.Clear();
            foreach (Dictionary<string, object> asset in assets)
            {
                if (!J.Flag(asset, "flagged")) continue;
                ListViewItem row = new ListViewItem(new string[] {
                    J.Text(asset, "asset_id"), J.Text(asset, "description"), J.Text(asset, "category"), J.Text(asset, "location") });
                row.Tag = asset;
                reviewList.Items.Add(row);
            }
            reviewList.EndUpdate();
            reviewPage.Text = reviewList.Items.Count == 0 ? "Review" : "Review (" + reviewList.Items.Count + ")";
        }

        // Approve or deny the ticked assets, or all of them. Denying sends DELETE, because
        // a denied asset is removed.
        void Review(bool deny, bool tickedOnly)
        {
            List<Dictionary<string, object>> requests = new List<Dictionary<string, object>>();
            foreach (ListViewItem row in reviewList.Items)
            {
                if (tickedOnly && !row.Checked) continue;
                Dictionary<string, object> asset = (Dictionary<string, object>)row.Tag;
                Dictionary<string, object> request = Net.Request(deny ? "DELETE" : "APPROVE");
                request["asset_id"] = J.Text(asset, "asset_id");
                request["expected_version"] = J.Number(asset, "version");
                requests.Add(request);
            }
            if (requests.Count == 0)
            {
                MessageBox.Show(this, tickedOnly ? "Tick the assets first." : "Nothing is waiting for review.", "Review", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            if (deny && MessageBox.Show(this, "Delete " + requests.Count + " asset(s) for good?", "Deny",
                                        MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
            PerformAll(requests, deny ? "Denied and deleted" : "Approved", null);
        }
    }
}
