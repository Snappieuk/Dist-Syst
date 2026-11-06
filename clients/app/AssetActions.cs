// Buttons and dialogs on the Assets tab: add, edit, change status and delete.
// Changes send the asset's version as "expected_version". If someone else changed the
// asset meanwhile, the server refuses, and nothing they did is overwritten.

using System;
using System.Collections.Generic;
using System.Windows.Forms;

namespace AssetApp
{
    // The form for adding an asset, or editing one when `asset` is given.
    class AssetDialog : Dialog
    {
        readonly TextBox description = new TextBox { Width = 300 };
        readonly TextBox category = new TextBox();
        readonly TextBox location = new TextBox();
        readonly ComboBox status = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList };

        public AssetDialog(string title, string okText, Dictionary<string, object> asset) : base(title, okText)
        {
            Row("Description", description);
            Row("Category", category);
            Row("Location", location);
            status.Items.AddRange(new object[] { "In Use", "Not In Use" });
            Row("Status", status);

            description.Text = asset == null ? "" : J.Text(asset, "description");
            category.Text = asset == null ? "" : J.Text(asset, "category");
            location.Text = asset == null ? "" : J.Text(asset, "location");
            status.SelectedItem = asset == null ? "In Use" : J.Text(asset, "status");
            if (status.SelectedIndex < 0) status.SelectedIndex = 0;
        }

        // What was typed, as the server's field names.
        public Dictionary<string, object> Values()
        {
            return new Dictionary<string, object> {
                { "description", description.Text.Trim() }, { "category", category.Text.Trim() },
                { "location", location.Text.Trim() }, { "status", status.Text } };
        }

        protected override string Problem()
        {
            List<string> missing = new List<string>();
            if (description.Text.Trim().Length == 0) missing.Add("Description");
            if (category.Text.Trim().Length == 0) missing.Add("Category");
            if (location.Text.Trim().Length == 0) missing.Add("Location");
            return missing.Count == 0 ? null : "Please fill in: " + string.Join(", ", missing.ToArray());
        }
    }

    partial class MainForm
    {
        // The last registration whose result we never saw (the connection may have dropped
        // after the server saved it). Pressing Add again with the same details re-sends the
        // SAME request ID, so the server recognises it and doesn't create a second asset.
        Dictionary<string, object> unconfirmedRegistration;

        // Make a button, add it to a row of buttons, and run `click` when pressed.
        Button AddButton(FlowLayoutPanel row, string text, EventHandler click)
        {
            Button button = new Button { Text = text, AutoSize = true };
            button.Click += click;
            row.Controls.Add(button);
            return button;
        }

        void BuildAssetButtons()
        {
            AddButton(assetButtons, "Add...", delegate { AddAsset(); });
            AddButton(assetButtons, "Edit...", delegate { EditSelected(); });
            AddButton(assetButtons, "In Use", delegate { SetStatus("In Use"); });
            AddButton(assetButtons, "Not In Use", delegate { SetStatus("Not In Use"); });
            AddButton(assetButtons, "Delete", delegate { DeleteSelected(); });
            BuildMoreAssetButtons();
        }

        // The selected asset, or null after telling the user to pick one.
        Dictionary<string, object> RequireSelected()
        {
            Dictionary<string, object> asset = Selected();
            if (asset == null)
                MessageBox.Show(this, "Please select an asset in the table.", "No asset selected", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return asset;
        }

        void AddAsset()
        {
            using (AssetDialog dialog = new AssetDialog("Add asset", "Add", null))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                Dictionary<string, object> asset = dialog.Values();

                // Reuse the last request if it is for the same details.
                if (unconfirmedRegistration == null ||
                    J.Serializer.Serialize(unconfirmedRegistration["asset"]) != J.Serializer.Serialize(asset))
                {
                    unconfirmedRegistration = Net.Request("REGISTER");
                    unconfirmedRegistration["request_id"] = Guid.NewGuid().ToString("N");
                    unconfirmedRegistration["asset"] = asset;
                }
                Perform(unconfirmedRegistration, "Asset registered", delegate { unconfirmedRegistration = null; });
            }
        }

        void EditSelected()
        {
            Dictionary<string, object> asset = RequireSelected();
            if (asset == null) return;
            using (AssetDialog dialog = new AssetDialog("Edit " + J.Text(asset, "asset_id"), "Save changes", asset))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;

                // Send only what changed.
                Dictionary<string, object> changes = new Dictionary<string, object>();
                foreach (KeyValuePair<string, object> value in dialog.Values())
                    if ((string)value.Value != J.Text(asset, value.Key)) changes[value.Key] = value.Value;
                if (changes.Count == 0) { statusText.Text = "Nothing was changed"; return; }

                Dictionary<string, object> request = Net.Request("EDIT");
                request["asset_id"] = J.Text(asset, "asset_id");
                request["changes"] = changes;
                request["expected_version"] = J.Number(asset, "version");
                Perform(request, "Asset updated", null);
            }
        }

        void SetStatus(string status)
        {
            Dictionary<string, object> asset = RequireSelected();
            if (asset == null) return;
            Dictionary<string, object> request = Net.Request("SET_STATUS");
            request["asset_id"] = J.Text(asset, "asset_id");
            request["status"] = status;
            request["expected_version"] = J.Number(asset, "version");
            Perform(request, "Status changed", null);
        }

        void DeleteSelected()
        {
            Dictionary<string, object> asset = RequireSelected();
            if (asset == null) return;
            string id = J.Text(asset, "asset_id");
            if (MessageBox.Show(this, "Delete " + id + " for good? This cannot be undone.", "Delete asset",
                                MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
            Dictionary<string, object> request = Net.Request("DELETE");
            request["asset_id"] = id;
            request["expected_version"] = J.Number(asset, "version");
            Perform(request, "Asset deleted", null);
        }
    }
}
