"""Install (or update) the Monitor Brightness Stream Deck plugin, then restart the Stream Deck app.

    py install_plugin.py
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

NAME = "com.local.monitorbrightness.sdPlugin"
SOURCE = Path(__file__).resolve().parent / "streamdeck" / NAME
TARGET = Path(os.environ["APPDATA"]) / "Elgato" / "StreamDeck" / "Plugins" / NAME
STREAM_DECK = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Elgato" / "StreamDeck" / "StreamDeck.exe"


def stream_deck_running():
    tasks = subprocess.run(["tasklist", "/FI", "IMAGENAME eq StreamDeck.exe", "/NH"], capture_output=True, text=True)
    return "StreamDeck.exe" in tasks.stdout


# Stream Deck only picks up new plugins when it starts, and keeps plugin files in use while running.
if stream_deck_running():
    print("Closing Stream Deck...")
    subprocess.run(["taskkill", "/IM", "StreamDeck.exe", "/F"], capture_output=True)
    for _ in range(50):
        if not stream_deck_running():
            break
        time.sleep(0.2)
    time.sleep(1)  # give it a moment to stop the plugin processes

if TARGET.exists():
    shutil.rmtree(TARGET)
shutil.copytree(SOURCE, TARGET)
print(f"Installed plugin to {TARGET}")

if STREAM_DECK.exists():
    subprocess.Popen([str(STREAM_DECK)], creationflags=subprocess.DETACHED_PROCESS)
    print("Started Stream Deck. Drag 'Monitor Brightness' from the action list onto a dial.")
else:
    print("Start Stream Deck to load the plugin.")
