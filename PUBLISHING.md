# Publishing it so others can use it

The bridge has to run on a PC that is on the same network as the switcher. Hosting
it in the cloud does not work: it cannot reach the switcher on your local network.
Publishing therefore means: other people download one program and start it on their
own PC.

## What you share

One file: `gostream-tally.exe`. The user does not need to install Python.

## Way 1: GitHub (free, built automatically)

1. Create a new repository on github.com, for example `gostream-tally`.
2. Upload the contents of this folder (including the `.github` folder).
3. Create a version tag: `git tag v1.0.0` and `git push --tags`.
4. GitHub builds the program on a Windows machine by itself.
5. Go to Releases and share the link to the `.exe`.

The workflow lives in `.github/workflows/build-windows.yml` and uses PyInstaller on
`windows-latest`.

## Way 2: build it yourself on this PC

Right-click `build_windows.ps1` and choose *Run with PowerShell*, or:

```
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

The program then appears in `dist\gostream-tally.exe`.

## What the user does

1. Double-click `gostream-tally.exe`.
2. A window appears with an address, for example `http://192.168.1.74:8080`.
3. The browser opens the control page by itself. That address is what they open on
   the phone, tablet or a second PC.

On the first start Windows may warn "Windows protected your PC", because the program
is not signed. Choose *More info* and then *Run anyway*. Signing is possible later
with a code-signing certificate.

## Settings kept next to the program

`labels.json` appears as soon as someone changes the source names, `access.json`
holds the allowed devices, and `switcher.json` remembers the switcher address. Those
files belong next to the program.

## Worth mentioning when publishing

- The app only reads the switcher status; it changes nothing about the broadcast.
- Port 19010 must be reachable on the network.
- Do not use it on a network you do not trust: there is no password, only the
  device approval on the PC.
- English is the main language of the documentation and the instructions, so anyone
  can start; the instructions then let every reader pick their own language.
