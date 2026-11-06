"""
Fills the system with made-up assets, so the table has plenty of rows to look at.

    python tools/fill.py               adds 500 assets
    python tools/fill.py --count 100   adds 100 assets (or -n 100)

Every asset is marked as sample data, so it gets a "sample data" tag, and about two in
five are given a loan, some returned, some out, some overdue and some still to come, so
the web page's calendar has something to show.

Start the system first, with python start.py. Each asset is registered through the
servers exactly as python cli.py add does it, so the backup gets a copy and the web page and GUI
show the new rows on their next refresh. To remove them all again, use
python setup.py --reset, which deletes every asset.
"""

import argparse  # reads the --count option
import datetime  # works out loan dates around today
import random    # picks the made-up details
import string    # the letters and digits a serial number is made from
import os        # finds the project folder
import sys       # sys.path is where Python looks for modules

# This file is in tools/, but common.py is one folder up, in the project folder, so
# Python is told to look there too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import IN_USE, NOT_IN_USE, new_register_request, send_request

# How many assets to add when --count isn't given.
DEFAULT_COUNT = 500

# Print a progress line after this many assets, so a long run doesn't look stuck.
PROGRESS_EVERY = 50

# The made-up assets are built from these lists. Each category has its own kinds of
# item, so a description always matches its category.
ITEMS_BY_CATEGORY = {
    "IT Equipment": [
        "Dell Latitude 5440 laptop", "Lenovo ThinkPad T14 laptop", "HP EliteBook 840 laptop",
        "Dell OptiPlex 7010 desktop", "Apple MacBook Air M2", "iPad 10th gen",
        "Dell P2422H monitor", "Logitech MX Keys keyboard", "Jabra Evolve2 headset",
    ],
    "Networking": [
        "Cisco Catalyst 9200 switch", "Ubiquiti U6 Pro access point", "Netgear GS308 switch",
        "Fortinet FortiGate 60F firewall", "Patch panel, 24 port",
    ],
    "Printers": [
        "HP LaserJet Pro M404", "Brother HL-L2350DW printer", "Canon imageRUNNER C3226",
        "Zebra ZD421 label printer",
    ],
    "AV": [
        "Epson EB-992F projector", "Samsung 55in display", "Logitech Rally camera",
        "Poly Studio X30 video bar", "Shure MV7 microphone",
    ],
    "Furniture": [
        "Height-adjustable desk", "Office chair, mesh back", "Filing cabinet, 4 drawer",
        "Meeting table, 8 seat", "Storage locker",
    ],
    "Tools": [
        "Makita cordless drill", "Fluke 117 multimeter", "Step ladder, 6 tread",
        "Pallet truck", "Cable tester",
    ],
}

SITES = ["Site A", "Site B", "Site C", "Head Office"]
ROOMS = ["IT Store", "Reception", "Comms room", "Office 1", "Office 2", "Meeting room",
         "Hall", "Workshop", "Warehouse"]

# The share of assets marked In Use. The rest are Not In Use.
IN_USE_SHARE = 0.75

# The server refuses an asset with the same description, category and location as one
# it already has, and the lists above only make about a thousand combinations. So each
# description ends with a random serial number, the way real registers tell identical
# items apart. With 8 characters from 36 there are 2.8 million million serials, so two
# being the same is practically impossible.
SERIAL_LENGTH = 8
SERIAL_CHARACTERS = string.ascii_uppercase + string.digits


def serial_number() -> str:
    return "".join(random.choices(SERIAL_CHARACTERS, k=SERIAL_LENGTH))


# The share of assets given a loan, and the people they are lent to.
LOAN_SHARE = 0.4
BORROWERS = ["Sam Lee", "Priya Shah", "Alex Kim", "Jo Brown", "Chris Evans", "Maria Garcia",
             "Tom Walsh", "Aisha Khan", "Ben Carter", "Lucy Moore"]


def made_up_loan(today: datetime.date) -> dict:
    """Return a loan around today: a start from 30 days ago to 20 days ahead, lasting 1 to
    21 days. Most that have ended were returned (some late), but one in five is overdue."""
    start = today + datetime.timedelta(days=random.randint(-30, 20))
    due = start + datetime.timedelta(days=random.randint(1, 21))
    loan = {"borrower": random.choice(BORROWERS), "start": start.isoformat(), "due": due.isoformat()}
    if due < today and random.random() < 0.8:
        returned = min(today, due + datetime.timedelta(days=random.randint(-2, 3)))
        loan["returned"] = max(returned, start).isoformat()
    return loan


def lend(asset_id: str, loan: dict) -> bool:
    """Check an asset out, and back in if the loan has been returned, returning whether
    the servers accepted it."""
    reply = send_request({"action": "CHECK_OUT", "asset_id": asset_id, **{key: loan[key] for key in
                                                                           ("borrower", "start", "due")}})
    if reply.get("status") == "OK" and "returned" in loan:
        reply = send_request({"action": "CHECK_IN", "asset_id": asset_id, "returned": loan["returned"]})
    return reply.get("status") == "OK"


def made_up_asset() -> dict:
    """Return the details of one made-up asset, ready to register."""
    category = random.choice(list(ITEMS_BY_CATEGORY))
    return {
        "description": f"{random.choice(ITEMS_BY_CATEGORY[category])}, SN {serial_number()}",
        "category": category,
        "location": f"{random.choice(SITES)} - {random.choice(ROOMS)}",
        "status": IN_USE if random.random() < IN_USE_SHARE else NOT_IN_USE,
        "sample": True,  # tagged "sample data", so it is easy to tell from real assets
    }


def positive_number(text: str) -> int:
    """Check the --count value, so a mistake gets a clear message rather than a crash."""
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a whole number")
    if number < 1:
        raise argparse.ArgumentTypeError("it must be at least 1")
    return number


def fill(count: int) -> int:
    """Register `count` made-up assets, lending some of them, and return how many were
    added.

    It stops at the first one the servers refuse, rather than repeating the same
    error hundreds of times.
    """
    added = 0
    today = datetime.date.today()
    for _ in range(count):
        reply = send_request(new_register_request(made_up_asset()))
        if reply.get("status") != "OK":
            print(f"The servers refused an asset, so stopping: {reply.get('detail')}")
            break
        added += 1
        if random.random() < LOAN_SHARE and not lend(reply["stored"]["asset_id"], made_up_loan(today)):
            print(f"The servers refused a loan for {reply['stored']['asset_id']}, so it has none.")
        if added % PROGRESS_EVERY == 0:
            print(f"  Added {added} of {count}...")
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill the system with made-up assets.")
    parser.add_argument("-n", "--count", type=positive_number, default=DEFAULT_COUNT,
                        help=f"how many assets to add (default {DEFAULT_COUNT})")
    count = parser.parse_args().count

    print(f"Adding {count} made-up asset(s)...")
    try:
        added = fill(count)
    except KeyboardInterrupt:
        print("\nStopped. The assets added so far are kept.")
        return
    except (ConnectionError, ValueError) as exc:
        print(f"Could not add the assets: {exc}")
        print("Check that the system is running: python start.py")
        return
    print(f"Done: added {added} asset(s).")


# Only run main() if this file is executed directly (not imported).
if __name__ == "__main__":
    main()
