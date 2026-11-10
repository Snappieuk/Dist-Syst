// The Calendar tab: a month grid showing which assets were out on each day, like the web
// page's. Click a day to list every loan on it, and double-click a loan for its history.

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;

namespace AssetApp
{
    // One loan and the last day it counts for: the day it came back, or the due date, or
    // today if it is overdue and still out (so an overdue loan keeps growing).
    class CalendarLoan
    {
        public Dictionary<string, object> Asset;
        public Dictionary<string, object> Loan;
        public string Start { get { return J.Text(Loan, "start"); } }
        public string End;

        public bool Covers(string date)
        {
            return string.CompareOrdinal(Start, date) <= 0 && string.CompareOrdinal(End, date) >= 0;
        }

        public bool Returned { get { return J.Text(Loan, "returned") != ""; } }

        // "returned 2026-11-03", "overdue" or "on loan".
        public string State(bool overdue)
        {
            return Returned ? "returned " + J.Text(Loan, "returned") : (overdue ? "overdue" : "on loan");
        }
    }

    // The month grid itself. It draws everything, so there are no hundreds of little
    // controls, and it tells its owner which day was clicked.
    class MonthGrid : Control
    {
        const int HeaderHeight = 24;   // the Mon..Sun row
        const int LineHeight = 16;     // one loan in a day's cell
        static readonly string[] DayNames = { "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun" };

        public DateTime Month { get; set; }
        public List<CalendarLoan> Loans = new List<CalendarLoan>();
        public Func<CalendarLoan, bool> IsOverdue;
        public event Action<string> DayClicked;

        // One bold font for today's date, made once and reused on every redraw.
        Font todayFont;
        protected override void OnFontChanged(EventArgs e)
        {
            base.OnFontChanged(e);
            todayFont = new Font(Font, FontStyle.Bold);
        }
        public MonthGrid()
        {
            Month = new DateTime(DateTime.Today.Year, DateTime.Today.Month, 1);
            DoubleBuffered = true;   // no flicker when it redraws
            todayFont = new Font(Font, FontStyle.Bold);
            ResizeRedraw = true;
        }

        // The Monday on or before the 1st, and how many whole weeks the month needs.
        DateTime GridStart() { return Month.AddDays(-(((int)Month.DayOfWeek + 6) % 7)); }

        int Weeks()
        {
            int offset = ((int)Month.DayOfWeek + 6) % 7;
            return (offset + DateTime.DaysInMonth(Month.Year, Month.Month) + 6) / 7;
        }

        public string FirstDate() { return GridStart().ToString("yyyy-MM-dd"); }
        public string LastDate() { return GridStart().AddDays(Weeks() * 7 - 1).ToString("yyyy-MM-dd"); }

        List<CalendarLoan> OnDay(string date)
        {
            List<CalendarLoan> found = new List<CalendarLoan>();
            foreach (CalendarLoan loan in Loans) if (loan.Covers(date)) found.Add(loan);
            return found;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.Clear(SystemColors.Window);
            int weeks = Weeks();
            float cellWidth = Width / 7f;
            float cellHeight = (Height - HeaderHeight) / (float)weeks;
            string today = DateTime.Today.ToString("yyyy-MM-dd");

            for (int i = 0; i < 7; i++)
                g.DrawString(DayNames[i], Font, SystemBrushes.GrayText, i * cellWidth + 6, 4);

            for (int week = 0; week < weeks; week++)
                for (int i = 0; i < 7; i++)
                {
                    DateTime day = GridStart().AddDays(week * 7 + i);
                    string date = day.ToString("yyyy-MM-dd");
                    RectangleF cell = new RectangleF(i * cellWidth, HeaderHeight + week * cellHeight, cellWidth, cellHeight);

                    if (date == today) g.FillRectangle(Brushes.LightYellow, cell);
                    g.DrawRectangle(Pens.Gainsboro, cell.X, cell.Y, cell.Width, cell.Height);
                    g.SetClip(cell);

                    bool inMonth = day.Month == Month.Month;
                    g.DrawString(day.Day.ToString(), date == today ? todayFont : Font,
                                 inMonth ? SystemBrushes.WindowText : SystemBrushes.GrayText, cell.X + 4, cell.Y + 2);

                    // As many loans as fit, then "+N more" (click the day to see them all).
                    List<CalendarLoan> loans = OnDay(date);
                    int room = Math.Max(1, (int)((cell.Height - 22) / LineHeight));
                    for (int n = 0; n < loans.Count && n < room; n++)
                    {
                        bool last = n == room - 1 && loans.Count > room;
                        string text = last ? "+" + (loans.Count - room + 1) + " more" : J.Text(loans[n].Loan, "borrower") + ": " + MainForm.ItemName(loans[n].Asset);
                        using (Brush brush = new SolidBrush(last ? Color.DimGray : ColorOf(loans[n])))
                            g.DrawString(text, Font, brush, cell.X + 4, cell.Y + 20 + n * LineHeight);
                    }
                    g.ResetClip();
                }
        }

        // Grey when returned, red when overdue, blue while out.
        Color ColorOf(CalendarLoan loan)
        {
            if (loan.Returned) return Color.Gray;
            return IsOverdue != null && IsOverdue(loan) ? Color.Firebrick : Color.RoyalBlue;
        }

        protected override void OnMouseClick(MouseEventArgs e)
        {
            base.OnMouseClick(e);
            int weeks = Weeks();
            if (e.Y < HeaderHeight || DayClicked == null) return;
            int column = Math.Min(6, (int)(e.X / (Width / 7f)));
            int week = Math.Min(weeks - 1, (int)((e.Y - HeaderHeight) / ((Height - HeaderHeight) / (float)weeks)));
            DayClicked(GridStart().AddDays(week * 7 + column).ToString("yyyy-MM-dd"));
        }
    }

    partial class MainForm
    {
        readonly MonthGrid calendar = new MonthGrid { Dock = DockStyle.Fill };
        readonly Label calendarMonth = new Label { AutoSize = true, Font = new Font(SystemFonts.DefaultFont.FontFamily, 12, FontStyle.Bold), Margin = new Padding(16, 6, 0, 0) };
        readonly FlowLayoutPanel calendarButtons = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(0, 6, 0, 6) };

        void BuildCalendarTab()
        {
            TabPage page = new TabPage("Calendar");
            AddButton(calendarButtons, "< Previous", delegate { calendar.Month = calendar.Month.AddMonths(-1); FillCalendar(); });
            AddButton(calendarButtons, "Today", delegate { calendar.Month = new DateTime(DateTime.Today.Year, DateTime.Today.Month, 1); FillCalendar(); });
            AddButton(calendarButtons, "Next >", delegate { calendar.Month = calendar.Month.AddMonths(1); FillCalendar(); });
            calendarButtons.Controls.Add(calendarMonth);

            calendar.IsOverdue = delegate(CalendarLoan loan) { return !loan.Returned && IsOverdue(loan.Loan); };
            calendar.DayClicked += ShowDay;
            page.Controls.Add(calendar);
            page.Controls.Add(calendarButtons);   // added last so it sits above the grid
            tabs.TabPages.Add(page);
        }

        // Work out which loans touch the month shown, then redraw.
        void FillCalendar()
        {
            calendarMonth.Text = calendar.Month.ToString("MMMM yyyy");
            string first = calendar.FirstDate(), last = calendar.LastDate();
            List<CalendarLoan> loans = new List<CalendarLoan>();
            foreach (Dictionary<string, object> asset in assets)
                foreach (Dictionary<string, object> loan in J.Objects(asset, "loans"))
                {
                    string returned = J.Text(loan, "returned");
                    string end = returned != "" ? returned : (IsOverdue(loan) ? Today() : J.Text(loan, "due"));
                    if (string.CompareOrdinal(J.Text(loan, "start"), last) <= 0 && string.CompareOrdinal(end, first) >= 0)
                        loans.Add(new CalendarLoan { Asset = asset, Loan = loan, End = end });
                }
            loans.Sort(delegate(CalendarLoan a, CalendarLoan b) { return string.CompareOrdinal(a.Start, b.Start); });
            calendar.Loans = loans;
            calendar.Invalidate();
        }

        // Every loan on one day, in a window. Double-click one to see that asset's history.
        void ShowDay(string date)
        {
            List<CalendarLoan> covering = calendar.Loans.FindAll(delegate(CalendarLoan loan) { return loan.Covers(date); });
            if (covering.Count == 0) return;

            using (Form window = new Form { Text = DateTime.Parse(date).ToString("dddd d MMMM yyyy"), Size = new Size(820, 340), StartPosition = FormStartPosition.CenterParent })
            {
                Label note = new Label { Text = covering.Count + " loan(s) on this day. Double-click one to see its history.", Dock = DockStyle.Top, Height = 28, Padding = new Padding(6, 6, 0, 0) };
                ListView list = NewList();
                list.Columns.Add("Item", 220);
                list.Columns.Add("Asset ID", 100);
                list.Columns.Add("Borrower", 130);
                list.Columns.Add("Start", 90);
                list.Columns.Add("Due", 90);
                list.Columns.Add("State", 140);
                foreach (CalendarLoan loan in covering)
                {
                    ListViewItem row = new ListViewItem(new string[] { ItemName(loan.Asset), J.Text(loan.Asset, "asset_id"),
                        J.Text(loan.Loan, "borrower"), loan.Start, J.Text(loan.Loan, "due"), loan.State(IsOverdue(loan.Loan)) });
                    row.Tag = loan.Asset;
                    if (!loan.Returned && IsOverdue(loan.Loan)) row.ForeColor = Color.Firebrick;
                    list.Items.Add(row);
                }
                list.DoubleClick += delegate
                {
                    if (list.SelectedItems.Count > 0) ShowHistoryOf((Dictionary<string, object>)list.SelectedItems[0].Tag);
                };
                window.Controls.Add(list);
                window.Controls.Add(note);
                window.ShowDialog(this);
            }
        }
    }
}
