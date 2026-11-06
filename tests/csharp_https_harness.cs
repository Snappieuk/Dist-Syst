// A tiny console program for tests/test_csharp_https.py. It is built together with the
// app's Net.cs and WebNet.cs (and nothing else), so those two files can be tested without
// opening a window.
//   harness address <text>        prints the tidied https address, "none" or "error: <why>"
//   harness login <https-url>     tries a login and prints "error: <why>" or "reply: <status>"
using System;
using System.Collections.Generic;

namespace AssetApp
{
    static class Harness
    {
        static int Main(string[] args)
        {
            try
            {
                if (args[0] == "address")
                {
                    Console.WriteLine(WebNet.Address(args[1]) ?? "none");
                    return 0;
                }
                WebNet.Prepare();
                WebNet.NewConnection();
                Session.WebUrl = WebNet.Address(args[1]);
                Dictionary<string, object> login = Net.Request("LOGIN");
                login["username"] = "tester";
                login["password"] = "test password";
                Console.WriteLine("reply: " + J.Text(Net.Send(login), "status"));
                return 0;
            }
            catch (Exception error)
            {
                Console.WriteLine("error: " + error.Message);
                return 0;
            }
        }
    }
}
