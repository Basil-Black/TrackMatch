# Running and building TrackMatch from the source code

This guide is for anyone, even if you've never used Python or a terminal before. Follow the section for your computer.

**Just want to use TrackMatch on Windows?** You don't need any of this. Download `TrackMatch-vX.X.X.zip` from the [Releases page](https://github.com/Basil-Black/TrackMatch/releases), extract it and run `TrackMatch.exe` (see [Using the ready-made app](#using-the-ready-made-app-windows) below).

- [Using the ready-made app (Windows)](#using-the-ready-made-app-windows)
- [Windows: run and build from source](#windows)
- [macOS: run and build from source](#macos)
- [Linux: run and build from source](#linux)
- [Updating to a newer version](#updating-to-a-newer-version)
- [Troubleshooting](#troubleshooting)

A few words you'll see:
- **Terminal**: a window where you type commands (PowerShell on Windows, Terminal on Mac and Linux). Type or paste a command, then press **Enter**.
- **Source code**: the `trackmatch.py` file and friends, which Python runs directly.
- **Building**: turning the source code into a normal app you can double-click.

---

## Using the ready-made app (Windows)

1. Go to the [Releases page](https://github.com/Basil-Black/TrackMatch/releases) and download the newest `TrackMatch-vX.X.X.zip`.
2. Right-click the zip > **Extract All...** and pick a folder (for example `Documents\TrackMatch`). Keep all the files together.
3. Double-click `TrackMatch.exe`.
4. The first time, Windows may show a blue **"Windows protected your PC"** box. This appears for any app that isn't signed with a paid certificate. Click **More info**, then **Run anyway**.
5. Install ffmpeg (needed for downloading and converting), see [step 2 of the Windows section](#2-install-ffmpeg).

---

## Windows

### 1. Install Python
1. Go to [python.org/downloads](https://www.python.org/downloads/) and download the latest **Python 3** (3.10 or newer).
2. Run the installer. **On the first screen, tick "Add python.exe to PATH"**, then click **Install Now**.

### 2. Install ffmpeg
1. Open **PowerShell**: click Start, type `powershell`, press Enter.
2. Paste this and press Enter:
   ```powershell
   winget install ffmpeg
   ```
3. Close PowerShell when it's done. (If `winget` isn't found, install **App Installer** from the Microsoft Store and try again.)

### 3. Download the source code
1. On the [TrackMatch GitHub page](https://github.com/Basil-Black/TrackMatch), click the green **Code** button > **Download ZIP**.
2. Extract the zip somewhere easy to find, for example `Documents\TrackMatch`.

### 4. Open a terminal in that folder
Open the extracted folder in File Explorer (the one containing `trackmatch.py`), click in the **address bar** at the top, type `powershell` and press Enter. A PowerShell window opens already in the right folder.

### 5. Set up TrackMatch (only needed once)
Paste these one at a time, pressing Enter after each:
```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```
The first line makes a private Python space for TrackMatch (called `.venv`) so nothing else on your computer is affected. The second downloads the libraries TrackMatch needs. This takes a minute or two.

### 6. Run TrackMatch
```powershell
.venv\Scripts\python trackmatch.py
```
Next time, just repeat steps 4 and 6.

### 7. Build your own TrackMatch.exe (optional)
```powershell
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python -m PyInstaller --onefile --windowed --icon=icon.ico --name TrackMatch trackmatch.py
```
This takes a few minutes. Your app appears in the **`dist`** folder as `TrackMatch.exe`. Copy `icon.ico` and `icon.png` into the same folder as the exe so the window icon shows, then you can move that folder anywhere.

---

## macOS

### 1. Install Python
Download the latest **Python 3** (3.10 or newer) from [python.org/downloads](https://www.python.org/downloads/) and run the installer. Use the python.org version: it includes everything TrackMatch's windows need.

### 2. Install ffmpeg
1. Install [Homebrew](https://brew.sh): open **Terminal** (press Cmd+Space, type `Terminal`, press Enter), paste the command shown on brew.sh, press Enter and follow the instructions.
2. Then:
   ```bash
   brew install ffmpeg
   ```

### 3. Download the source code
On the [TrackMatch GitHub page](https://github.com/Basil-Black/TrackMatch), click **Code** > **Download ZIP**, and double-click the zip to extract it (for example into your Documents folder).

### 4. Open Terminal in that folder
In Terminal, type `cd ` (with a space after it), then **drag the TrackMatch folder from Finder into the Terminal window** and press Enter.

### 5. Set up TrackMatch (only needed once)
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 6. Run TrackMatch
```bash
.venv/bin/python trackmatch.py
```
Next time, just repeat steps 4 and 6.

### 7. Build a Mac app (optional)
```bash
.venv/bin/python -m pip install pyinstaller pillow
.venv/bin/python -m PyInstaller --windowed --icon=icon.png --add-data "icon.png:." --name TrackMatch trackmatch.py
```
The app appears in the **`dist`** folder as `TrackMatch.app` - drag it into your Applications folder. Because it isn't signed by Apple, the first time you open it, **right-click it > Open**, then click **Open** (or allow it in System Settings > Privacy & Security).

The Mac app keeps your settings, saved lookups and logs in `~/Library/Application Support/TrackMatch`, so replacing the app with a newer version keeps them. The Mac build is new, so please report anything that goes wrong.

---

## Linux

These commands are for Ubuntu / Debian / Linux Mint. On other distributions, install the same packages with your package manager.

### 1. Install Python, Tkinter and ffmpeg
Open a **Terminal** and run:
```bash
sudo apt update
sudo apt install python3 python3-venv python3-tk ffmpeg
```
It will ask for your password (nothing shows while you type it; that's normal).

### 2. Download the source code
On the [TrackMatch GitHub page](https://github.com/Basil-Black/TrackMatch), click **Code** > **Download ZIP**, and extract it (for example into your home folder).

### 3. Open a terminal in that folder
Right-click inside the extracted folder in your file manager and choose **Open in Terminal**. (Or type `cd ` in a terminal and drag the folder in.)

### 4. Set up TrackMatch (only needed once)
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 5. Run TrackMatch
```bash
.venv/bin/python trackmatch.py
```

### 6. Build a Linux app (optional)
```bash
.venv/bin/python -m pip install pyinstaller
.venv/bin/python -m PyInstaller --onefile --windowed --add-data "icon.png:." --name TrackMatch trackmatch.py
```
Your app appears in the **`dist`** folder as `TrackMatch`. Run it by double-clicking or with `./TrackMatch`. The Linux build hasn't been tested yet, so please report anything that goes wrong.

---

## Updating to a newer version

- **Ready-made app (Windows):** download the new zip and extract it over the old folder (or into a new one). To keep your settings, copy `trackmatch_settings.json` from the old folder into the new one.
- **Mac app:** just replace `TrackMatch.app` - your settings are kept in `~/Library/Application Support/TrackMatch`.
- **From source:** download the new source zip, extract it, and copy your old `.venv` folder and `trackmatch_settings.json` across (or just repeat the setup step). Then run `pip install -r requirements.txt` again in case anything new is needed:
  - Windows: `.venv\Scripts\python -m pip install -r requirements.txt`
  - Mac / Linux: `.venv/bin/python -m pip install -r requirements.txt`

---

## Troubleshooting

- **`py` or `python3` is "not recognized" / "command not found"**
  Python isn't installed, or (on Windows) "Add python.exe to PATH" wasn't ticked. Re-run the Python installer, choose **Modify** or reinstall, and tick that box. Then close and reopen the terminal.

- **"No module named ..." when starting TrackMatch**
  The setup step didn't finish. Run the `pip install -r requirements.txt` line again, and make sure you start TrackMatch with the `.venv` Python shown above (not plain `python`).

- **Windows: "running scripts is disabled on this system"**
  You don't need to activate anything. Use the commands exactly as shown above, starting with `.venv\Scripts\python`.

- **Linux: "No module named tkinter"**
  Run `sudo apt install python3-tk`, then delete the `.venv` folder and repeat the setup step.

- **Downloading or converting fails straight away**
  ffmpeg isn't installed or TrackMatch can't see it yet. Install it (see your system's section), then close and reopen TrackMatch **and** the terminal.

- **Building fails, or the built app says a module is missing**
  Make sure you ran `pip install -r requirements.txt` **before** building, using the same `.venv` Python you build with.

- **Something else**
  In TrackMatch go to **Settings > Open logs folder**. The newest log file shows what happened. Please attach it when reporting a problem on the [Issues page](https://github.com/Basil-Black/TrackMatch/issues).
