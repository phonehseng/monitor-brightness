# Monitor Brightness

Change external monitor brightness over DDC/CI (via [monitorcontrol](https://pypi.org/project/monitorcontrol/))
from the system tray, the command line, or a Stream Deck+ dial.

```
brightness.py          tray app + command line
install_plugin.py      installs the Stream Deck plugin
streamdeck/            the Stream Deck+ plugin (no npm needed)
```

## Requirements

- Windows 10 or 11 (the command line also works on Linux; the tray app is Windows-only)
- Python 3.9+
- For the dial: a Stream Deck+ and Stream Deck 6.5 or newer

## Setup

```
git clone https://github.com/phonehseng/monitor-brightness.git
cd monitor-brightness
py -m pip install -r requirements.txt
```

Your monitors need **DDC/CI** turned on in their on-screen menu (usually on by default).
Laptop built-in screens don't support DDC/CI.

## Tray app

```
pythonw brightness.py
```

- **Left-click** the sun icon: a flyout with a slider per monitor, plus "All displays".
  Drag, click or use the mouse wheel. Click elsewhere or press Esc to close.
- **Right-click**: Refresh monitors · Start with Windows · Quit.
- Windows 11 puts new tray icons in the **^** overflow. Drag the sun icon out onto the
  taskbar to keep it next to the clock.

It idles at about 30 MB of RAM and 0% CPU: nothing polls. A background thread per monitor
sleeps until you change something, and only the latest value goes to the monitor.
Errors are logged to `%LOCALAPPDATA%\MonitorBrightness\brightness.log`.

## Command line

```
py brightness.py list             # monitors, current brightness and IDs
py brightness.py get              # brightness of every monitor
py brightness.py set 60           # all monitors to 60%
py brightness.py set +10 lg       # monitor whose name contains "lg", up 10
py brightness.py set -10 2        # monitor #2 (from `list`), down 10
```

## Stream Deck+ dial

1. Start the tray app (the plugin talks to it).
2. `py install_plugin.py`: copies the plugin into Stream Deck and restarts the app.
3. In Stream Deck, drag **Monitor Brightness → Monitor Brightness** onto a dial.

| On the dial      | Does                                                                  |
|------------------|-----------------------------------------------------------------------|
| Turn             | Brightness up/down by the step (default 5%)                           |
| Hold + turn      | 1% steps                                                              |
| Press            | Jump to the preset (default 20%); press again to restore each monitor |
| Tap touch strip  | Re-read the monitors (e.g. after using their own buttons)             |

In the dial's settings you can pick one monitor or all of them, plus the step and the
preset. Put two dials side by side to control each monitor separately. The touch strip
updates right away when you change brightness from the tray flyout.

After editing anything in `streamdeck/`, run `py install_plugin.py` again.

## How it fits together

The tray app owns the monitors and serves a small JSON API on `127.0.0.1:47823`
(`GET /monitors`, `POST /brightness`). It only accepts local requests that carry an
`X-Monitor-Brightness: 1` header, which web pages can't send. The Stream Deck plugin
(`bin/plugin.js`) runs on Stream Deck's bundled Node.js and calls that API. It
long-polls `/monitors?since=<version>` so the touch strip stays in sync without polling
on a timer.

## License

[MIT](LICENSE)
