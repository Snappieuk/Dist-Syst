// The People tab: everyone who has something on loan right now, like the web page's.
// It is worked out from the loans themselves, so nothing is stored. A person disappears
// when their last item is checked in, wherever that happens.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Text.RegularExpressions;
using System.Windows.Forms;

namespace AssetApp
{
    // One person and the assets they have out.
    class Person
    {
        public string Name;
        public List<Dictionary<string, object>> Assets = new List<Dictionary<string, object>>();
    }

    partial class MainForm
    {
        readonly ListView peopleList = NewList();
        readonly FlowLayoutPanel peopleButtons = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(0, 6, 0, 6) };

        // The detail panel under the list: what the selected person has out, one row each.
        readonly Panel personPanel = new Panel { Dock = DockStyle.Bottom, Height = 230 };
        readonly Label personHeading = new Label { Dock = DockStyle.Top, Height = 30, Font = new Font(SystemFonts.DefaultFont, FontStyle.Bold), Padding = new Padding(6, 8, 0, 0) };
        readonly ListView personItems = NewList();
        readonly FlowLayoutPanel personButtons = new FlowLayoutPanel { Dock = DockStyle.Bottom, AutoSize = true, Padding = new Padding(0, 4, 0, 4) };

        void BuildPeopleTab()
        {
            TabPage page = new TabPage("People");
            AddButton(peopleButtons, "Check all in and remove", delegate { RemoveSelectedPerson(); });
            peopleList.Columns.Add("Name", 160);
            peopleList.Columns.Add("Items", 60);
            peopleList.Columns.Add("Has on loan", 640);
            peopleList.SelectedIndexChanged += delegate { ShowPerson(); };

            personItems.Columns.Add("Item", 220);
            personItems.Columns.Add("Asset ID", 100);
            personItems.Columns.Add("Category", 110);
            personItems.Columns.Add("Location", 120);
            personItems.Columns.Add("Out since", 90);
            personItems.Columns.Add("Due", 90);
            personItems.Columns.Add("State", 150);
            AddButton(personButtons, "Check in selected item", delegate { CheckInPersonItem(); });
            AddButton(personButtons, "History", delegate
            {
                if (personItems.SelectedItems.Count > 0) ShowHistoryOf((Dictionary<string, object>)personItems.SelectedItems[0].Tag);
            });
            personPanel.Controls.Add(personItems);
            personPanel.Controls.Add(personButtons);
            personPanel.Controls.Add(personHeading);

            page.Controls.Add(peopleList);
            page.Controls.Add(personPanel);     // docked to the bottom
            page.Controls.Add(peopleButtons);   // added last so it sits above the table
            tabs.TabPages.Add(page);
        }

        // Fill the panel under the list for the person selected, or clear it.
        void ShowPerson()
        {
            personItems.Items.Clear();
            if (peopleList.SelectedItems.Count == 0)
            {
                personHeading.Text = "Select a person to see what they have out.";
                return;
            }
            Person person = (Person)peopleList.SelectedItems[0].Tag;
            personHeading.Text = person.Name + " has " + person.Assets.Count + (person.Assets.Count == 1 ? " item" : " items") + " out";
            foreach (Dictionary<string, object> asset in person.Assets)
            {
                Dictionary<string, object> loan = CurrentLoan(asset);
                int days = (int)(DateTime.Parse(J.Text(loan, "due")) - DateTime.Today).TotalDays;
                string state = days < 0 ? "Overdue by " + (-days) + (days == -1 ? " day" : " days")
                             : days == 0 ? "Due today" : "Due in " + days + (days == 1 ? " day" : " days");
                ListViewItem row = new ListViewItem(new string[] { ItemName(asset), J.Text(asset, "asset_id"), J.Text(asset, "category"),
                    J.Text(asset, "location"), J.Text(loan, "start"), J.Text(loan, "due"), state });
                row.Tag = asset;
                if (days < 0) row.ForeColor = System.Drawing.Color.Firebrick;
                personItems.Items.Add(row);
            }
        }

        // Check in just the item selected in the panel.
        void CheckInPersonItem()
        {
            if (personItems.SelectedItems.Count == 0)
            {
                MessageBox.Show(this, "Select one of their items first.", "Check in", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            Dictionary<string, object> asset = (Dictionary<string, object>)personItems.SelectedItems[0].Tag;
            if (MessageBox.Show(this, "Check " + ItemName(asset) + " back in?", "Check in", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            Dictionary<string, object> back = Net.Request("CHECK_IN");
            back["asset_id"] = J.Text(asset, "asset_id");
            back["expected_version"] = J.Number(asset, "version");
            Perform(back, "Asset checked in", null);
        }

        // Names are compared ignoring capitals and extra spaces, so "sam lee" and "Sam  Lee"
        // are one person, and a second item lent to them joins the same row.
        static string CleanName(string name)
        {
            return Regex.Replace(name.Trim(), @"\s+", " ");
        }

        static string DueText(Dictionary<string, object> loan)
        {
            return (IsOverdue(loan) ? "overdue, was due " : "due ") + J.Text(loan, "due");
        }

        // Group the assets that are out by who has them, in name order.
        List<Person> PeopleWithLoans()
        {
            Dictionary<string, Person> byName = new Dictionary<string, Person>();
            foreach (Dictionary<string, object> asset in assets)
            {
                Dictionary<string, object> loan = CurrentLoan(asset);
                if (loan == null) continue;
                string name = CleanName(J.Text(loan, "borrower"));
                string key = name.ToLowerInvariant();
                if (!byName.ContainsKey(key)) byName[key] = new Person { Name = name };
                byName[key].Assets.Add(asset);
            }
            List<Person> people = new List<Person>(byName.Values);
            people.Sort(delegate(Person a, Person b) { return string.Compare(a.Name, b.Name, StringComparison.CurrentCultureIgnoreCase); });
            return people;
        }

        void FillPeople()
        {
            // Redrawing clears the selection, so reselect the same person afterwards.
            string selected = peopleList.SelectedItems.Count == 0 ? null : ((Person)peopleList.SelectedItems[0].Tag).Name;
            peopleList.BeginUpdate();
            peopleList.Items.Clear();
            foreach (Person person in PeopleWithLoans())
            {
                List<string> items = new List<string>();
                bool late = false;
                foreach (Dictionary<string, object> asset in person.Assets)
                {
                    Dictionary<string, object> loan = CurrentLoan(asset);
                    late = late || IsOverdue(loan);
                    items.Add(ItemName(asset) + " (" + DueText(loan) + ")");
                }
                ListViewItem row = new ListViewItem(new string[] { person.Name, person.Assets.Count.ToString(), string.Join("; ", items.ToArray()) });
                row.Tag = person;
                if (late) row.ForeColor = System.Drawing.Color.Firebrick;
                peopleList.Items.Add(row);
                if (person.Name == selected) row.Selected = true;
            }
            peopleList.EndUpdate();
            ShowPerson();   // shows the person again with fresh data, or clears the panel
        }

        // Removing someone means checking all of their items back in. It lists what they
        // still have, then asks a second time, because it changes the assets.
        void RemoveSelectedPerson()
        {
            if (peopleList.SelectedItems.Count == 0)
            {
                MessageBox.Show(this, "Select a person first.", "Remove person", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            Person person = (Person)peopleList.SelectedItems[0].Tag;

            string list = "";
            foreach (Dictionary<string, object> asset in person.Assets)
                list += "\n  " + ItemName(asset) + ", " + DueText(CurrentLoan(asset));
            if (MessageBox.Show(this, person.Name + " still has " + person.Assets.Count + " item(s) on loan. Removing them means checking every one of these back in:\n" + list,
                                "Remove " + person.Name, MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
            if (MessageBox.Show(this, "Really check in " + person.Assets.Count + " item(s) from " + person.Name + "?",
                                "Are you sure?", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;

            List<Dictionary<string, object>> requests = new List<Dictionary<string, object>>();
            foreach (Dictionary<string, object> asset in person.Assets)
            {
                Dictionary<string, object> back = Net.Request("CHECK_IN");
                back["asset_id"] = J.Text(asset, "asset_id");
                back["expected_version"] = J.Number(asset, "version");
                requests.Add(back);
            }
            PerformAll(requests, "Checked in " + person.Assets.Count + " item(s) from " + person.Name, null);
        }
    }
}
