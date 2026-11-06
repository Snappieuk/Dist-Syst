// Entry point: read the settings a downloaded copy carries, log in, then show the main window.

using System;
using System.Windows.Forms;

namespace AssetApp
{
    static class Program
    {
        [STAThread]
        static void Main()
        {
            Application.EnableVisualStyles();
            SiteSettings.Load();
            using (LoginForm login = new LoginForm())
                if (login.ShowDialog() != DialogResult.OK) return;   // closed without logging in
            Application.Run(new MainForm());
        }
    }
}
