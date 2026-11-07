# GoStream Tally — preview and on air on your phone

What you see on the phone: eight tiles, one per input.

```
+--------------------------------------+
| * connected           192.168.1.232  |
+------------------+-------------------+
|   Cam 1          |   Cam 2           |
|                  |   ON AIR          |
|                  |   (red)           |
+------------------+-------------------+
|   Cam 3          |   Cam 4           |
|   PREVIEW        |                   |
|   (green)        |   (dark)          |
+------------------+-------------------+
|   Cam 5          |   Cam 6           |
+------------------+-------------------+
|   Cam 7          |   Cam 8           |
+------------------+-------------------+
| PGM red, PVW green    [Flash on]     |
+--------------------------------------+
```

Red = on air, green = preview, dark = free. The screen flashes briefly when a
camera goes on air, so the operator does not have to watch it all the time.

## What you need

1. The GoStream Duet 8 ISO on your network, for example **192.168.1.232**.
2. A Windows PC on **the same network** as the switcher, with Python 3.
   A phone cannot open a direct connection to the switcher itself. The PC does
   that, and the phone reads along through the browser.
3. The phone on that same network (same wifi).

## Starting on Windows 11

**Once: install Python.** Go to https://www.python.org/downloads/ and install
Python 3. Tick **"Add python.exe to PATH"** during setup.

**After that, every time:**

1. Unzip the package, for example to `C:\Users\<you>\Documents\gostream_tally`.
2. Open the extracted folder `gostream_tally`.
3. Right-click `start_windows.ps1` -> **Run with PowerShell**.

If that does not work (execution policy), open PowerShell in that folder
(right-click in the folder -> **Open in Terminal**) and type:

```
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
```

Or directly, without a script:

```
python gostream_tally.py
```

The window shows your own address, for example `http://192.168.1.55:8080`. The
control page opens in your browser by itself; keep that window open. If you would
rather not, start with `--no-open`.

## The switcher address

The bridge looks for the switcher on the network by itself. When it finds it,
everything just works. When it does not, open the control page, enter the address
at the bottom (for example `192.168.1.232`) and press **Save and connect**.

The address is remembered in `switcher.json`, so you only do this once. The bridge
reconnects immediately, without a restart.

## Opening it on the phone

Type that address into the phone's browser. Then add the page to the home screen
(Share -> Add to Home Screen) so it opens full screen like an app.

## Windows firewall

The first time, Windows may ask whether Python may accept connections. Choose
**Allow** for private networks.

If that never happened and the page stays empty on the phone, run this once as
administrator:

```
New-NetFirewallRule -DisplayName "GoStream Tally" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

## Together with the GoStream PC software

The Duet 8 ISO is controlled over **Ethernet**, not over USB. The USB port is for
video (webcam/UVC) or for the supplied USB-C to Ethernet adapter. A PC that is not
physically attached to the switcher can still control it, as long as it is on the
same network and you enter the right address. That holds for the GoStream software
and for this tally page alike.

One thing I could not confirm for certain: whether the switcher **accepts several
connections at once**. That is why the page has a **Release** button. It closes the
connection cleanly so the GoStream software has the switcher to itself. **Connect**
opens it again. The button turns orange with "switcher free" while the connection
is released.

Testing it takes a minute:

1. Start the bridge and open the page on the phone.
2. Open the GoStream software on the PC at the same time and see whether it
   connects and shows its status normally.
3. If the software works fine, they can run together. If it cannot connect, press
   **Release** and try again.

## When port 8080 is taken

On a PC that already runs other software, port 8080 can be taken or reserved. The
script then says so and picks the next free port automatically. The window shows
the right address straight away, for example:

```
  Open on your phone: http://192.168.1.55:8093
```

You do not have to change anything, just use the address shown. To pick a port
yourself:

```
python gostream_tally.py --web-port 9000
```

## Who gets in

The bridge is closed to unknown devices. A phone that opens the page only sees
"Access needed" and asks for permission. On the PC you open:

    http://127.0.0.1:8080/host

Every device asking for access is listed there, with name, address and time.
*Connect* lets it in, *Deny* refuses it, and *Disconnect* puts it out again.

Once allowed, the device receives a six-character code. The same code appears on
the PC. If they do not match, it is a different device than you thought:
disconnect it.

That list can only be opened on the PC itself. From any other address on the
network the bridge gives no answer, so nobody can let themselves in. The status,
the names and the buttons only work with the key the allowed device received.
Without a key every request gets "no access".

Settings live in `access.json` next to the script. Delete that file to make every
device ask again.

## The address for the phone

The host page shows the address a phone has to open, right at the top, with a
Copy button. You no longer have to read it from the black window.

## Assigning an input to a device

Each device can be tied to one input, so an operator only has to watch the edge
of the screen instead of the whole grid.

1. Open `http://127.0.0.1:<port>/host` on the PC.
2. Next to the device, choose an input from the list.

That phone now shows a red edge while its input is on air, a green edge while it
is in preview, and no edge when it is not selected. The choice is remembered,
also after a disconnect and a new approval. Choose the dash to clear it.

You can change it at any time, also halfway through a broadcast. The phone
follows within a second, without reloading the page.

## Changing the language

Top right there is a small button with the language code, for example `EN`. Tap it
and pick a language. The choice is stored on that device and applies immediately,
including in the names panel.

Available: Papiamentu, Nederlands, English, Espanol, Francais, Deutsch, Portugues,
Italiano, Chinese, Japanese, Korean, Russian, Arabic, Hindi and Indonesian. Arabic
switches the whole page to right-to-left.

The Papiamentu texts follow the Buki di Oro (FPI 2009). *ON AIR* and *PREVIEW* stay
in every language: that is control-room vocabulary and everyone reads it that way.

## Instructions and languages

`INSTRUCTIONS.html` is a separate file you can open without the bridge. It explains
in short how everything works.

Before you read anything it asks **which language you want**, with all fifteen
languages listed by their own name. English comes first, so anyone can start, and
everyone else picks their own. That choice is remembered, and the button at the top
takes you back to the list at any time.

The same explanation is also at `http://<pc>:<port>/instructions`, which is handy
from the phone.

## Signal on a switch

There is *no sound*, because the app sits next to a recording or a broadcast. When
the program source changes, the screen flashes white briefly. On Android the device
also vibrates; an iPhone cannot vibrate from a web page.

The button in the bottom bar controls this signal and is on by default.

## Source names

The switcher only sends source numbers over the protocol, no names. The names
therefore live in the app itself and can be changed:

1. Open the page on the phone or the PC.
2. Tap **Names**.
3. Enter a name per source number and tap **Save**.

The names are stored in `labels.json` next to the program, so they apply to every
device that opens the page. Without your own names the app uses tidy defaults, for
example `MultiSource` for source 5001 and `Multiview` for 9001.

Note: changing a name does not change anything on the switcher itself.

## Changing the camera names

By default input 1 to 8 are called "Cam 1" to "Cam 8". To pass your own names:

```
python gostream_tally.py --labels "Main camera,PTZ left,PTZ right,Document camera,Phone 1,Phone 2,Spare,Spare"
```

The order is the order of the inputs on the switcher.

## When it does not connect

Work through this, in this order:

1. Is the PC on the same network as the switcher? Test in PowerShell:
   `ping 192.168.1.232`
2. Make sure network control on the switcher is switched on. The OSEE Companion
   module also only works over the network, not over USB.
3. Firmware: the protocol works from Duet 8 ISO firmware **2.1.0**.
4. Is the phone on the same wifi and not on a guest network? Guest networks often
   keep devices apart.
5. Did the router hand out a different IP? Start with
   `python gostream_tally.py --host <other-ip>`, or enter the address on the
   control page.

The page shows "no connection" at the top as long as the bridge cannot reach the
switcher. The bridge keeps trying every 2 seconds, so nothing needs restarting when
the switcher comes on later.

## Without Python: build a single program

To run it on a PC that has no Python, or to hand it over as one file, build a
`.exe` once. Do that on a PC that does have Python.

1. Open the extracted folder `gostream_tally`.
2. Right-click `build_windows.ps1` and choose **Run with PowerShell**. If that does
   not work, open PowerShell in the folder and type:

       powershell -ExecutionPolicy Bypass -File .\build_windows.ps1

3. The script fetches PyInstaller and builds. After about a minute you have
   `dist\gostream-tally.exe`.
4. Copy that file into its own folder and start it with a double-click.

The window with the address is still required: that is the bridge. Close it and the
page on the phone stops too. `labels.json`, `access.json` and `switcher.json` end
up next to the `.exe`.

The firewall asks again, because this is a different program than `python.exe`.
Some scanners report an unknown file; that comes from how such a single file is
built, not from a bug in the program. If you do not trust the report, just run it
with Python.

## Testing without a switcher

```
python gostream_tally.py --demo
```

The script then simulates a switcher that changes camera every 3 seconds. Handy to
look at the page on the phone before you go on location.

## How it works (for when something breaks later)

The switcher uses the GoStream Protocol over **TCP port 19010**:

```
packet = 0xEB 0xA6 | protoid 0x00 | U16LE(len+2) | JSON | U16LE(CRC16-modbus)
```

Every message is JSON, for example `{"id":"pgmIndex","type":"get"}`. The switcher
answers with the value and sends `type: "pus"` on its own whenever something
changes. This app listens to that, so the status is always current without polling.

The parameters used:

```
pgmIndex        which input is on air
pvwIndex        which input is in preview
keyOnAir        upstream key on/off
dskOnAir        downstream key on/off
ftb             fade to black
autoTransition  a transition is running
```

The protocol comes from the open-source Companion module by OSEE
(bitfocus/companion-module-osee-gostream-series). The same route Companion and the
GoStream PC software use, so no guesswork.

## Files

```
gostream_tally.py        the bridge and the web page, one file, no extra packages
start_windows.ps1        start on Windows 11 (shows your own address)
start_mac.command        start on a Mac if needed
build_windows.ps1        build a single .exe that runs without Python
INSTRUCTIONS.html        the instructions, in fifteen languages
MANUAL.txt               this manual as plain text for Notepad
MANUAL.nl.txt            the same manual in Dutch
README.md                this file
README.nl.md             the same in Dutch
PUBLISHING.md            how to publish it so others can download it
test_fake_switcher.py    fake switcher to test the bridge without hardware
```

Every file has an English name, so the package reads the same on any machine.

Only Python's standard library is needed, so nothing to install with `pip`.

## What has been tested and what has not

Tested live with a real Duet 8 ISO: the connection over TCP 19010, reading the
start status, and the program/preview values following the switcher (Cam 2 in
preview, source 5001 on air). Also tested with a simulated switcher: the packet
layout and CRC, the access system (allow, deny, disconnect, request again, and the
expired key returning 401), the language switch in all fifteen languages, and the
saved switcher address after a restart. Not tested: building the `.exe` on Windows
itself, because this was written on Linux.
