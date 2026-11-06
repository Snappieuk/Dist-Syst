// The People tab: everyone who has something on loan right now, like the web page's.
// It is worked out from the loans themselves, so nothing is stored. A person disappears
// when their last item is checked in, wherever that happens.

using System;
using System.Collections.Generic;
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

        void BuildPeopleTab()
        {
            TabPage page = new TabPage("People");
            AddButton(peopleButtons, "Check all in and remove", delegate { RemoveSelectedPerson(); });
            peopleList.Columns.Add("Name", 160);
            peopleList.Columns.Add("Items", 60);
            peopleList.Columns.Add("Has on loan", 640);
            page.Controls.Add(peopleList);
            page.Controls.Add(peopleButtons);   // added last so it sits above the table
            tabs.TabPages.Add(page);
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
                    items.Add(J.Text(asset, "asset_id") + " " + J.Text(asset, "description") + " (" + DueText(loan) + ")");
                }
                ListViewItem row = new ListViewItem(new string[] { person.Name, person.Assets.Count.ToString(), string.Join("; ", items.ToArray()) });
                row.Tag = person;
                if (late) row.ForeColor = System.Drawing.Color.Firebrick;
                peopleList.Items.Add(row);
            }
            peopleList.EndUpdate();
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
                list += "\n  " + J.Text(asset, "asset_id") + "  " + J.Text(asset, "description") + ", " + DueText(CurrentLoan(asset));
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
