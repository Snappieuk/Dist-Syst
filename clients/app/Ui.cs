// Small helpers shared by the windows: running work off the window's thread, and a
// base class for the little forms (add, edit, check out, history).

using System;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;

namespace AssetApp
{
    static class Background
    {
        // Run work() on another thread, so the window stays responsive while the network is
        // used, then call done(result, error) back on the window's own thread. Exactly one of
        // result and error is meaningful.
        public static void Run<T>(Control owner, Func<T> work, Action<T, Exception> done)
        {
            ThreadPool.QueueUserWorkItem(delegate
            {
                T result = default(T);
                Exception error = null;
                try { result = work(); }
                catch (Exception caught) { error = caught; }

                try
                {
                    if (!owner.IsDisposed)
                        owner.BeginInvoke((Action)delegate { done(result, error); });
                }
                catch (InvalidOperationException) { }   // the window closed while this was running
            });
        }
    }

    // A small window with rows of "label: control" and OK / Cancel buttons. A form that
    // inherits from it adds its rows in the constructor and may override Problem() to
    // refuse OK, by returning a message saying what is wrong.
    class Dialog : Form
    {
        readonly TableLayoutPanel grid = new TableLayoutPanel();
        protected readonly Button Ok = new Button();

        public Dialog(string title, string okText)
        {
            Text = title;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterParent;
            MaximizeBox = false;
            MinimizeBox = false;
            ShowInTaskbar = false;
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Padding = new Padding(12);

            grid.ColumnCount = 2;
            grid.AutoSize = true;
            grid.Dock = DockStyle.Fill;
            Controls.Add(grid);

            Button cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, AutoSize = true };
            Ok.Text = okText;
            Ok.AutoSize = true;
            Ok.Click += delegate
            {
                string problem = Problem();
                if (problem != null) MessageBox.Show(this, problem, "Check the form", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                else DialogResult = DialogResult.OK;
            };
            FlowLayoutPanel buttons = new FlowLayoutPanel { AutoSize = true, Anchor = AnchorStyles.Right, Margin = new Padding(0, 12, 0, 0) };
            buttons.Controls.Add(Ok);
            buttons.Controls.Add(cancel);
            // The button row sits under the last row; Row() moves it down as rows are added.
            ButtonRow = buttons;
            grid.Controls.Add(ButtonRow, 1, 0);
            AcceptButton = Ok;
            CancelButton = cancel;
        }

        FlowLayoutPanel ButtonRow;
        int rows;

        // Add a row and return its control, so it can be kept in a field.
        protected T Row<T>(string label, T control) where T : Control
        {
            grid.Controls.Remove(ButtonRow);
            grid.Controls.Add(new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(0, 6, 12, 6) }, 0, rows);
            control.Anchor = AnchorStyles.Left | AnchorStyles.Right;
            control.Margin = new Padding(0, 3, 0, 3);
            grid.Controls.Add(control, 1, rows);
            rows++;
            grid.Controls.Add(ButtonRow, 1, rows);
            return control;
        }

        protected virtual string Problem() { return null; }
    }
}
