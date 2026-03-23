# YouTube Toolkit

A small Flask app for downloading YouTube videos, MP3 audio, thumbnails, and transcripts through a local web interface.

## Features

- Download videos as `.mp4`
- Download audio as `.mp3`
- Download thumbnails
- Export transcripts as `.txt`
- Show thumbnail, title, transcript, and chapter preview before downloading
- Choose a custom download folder from the UI

## Requirements

- Python 3.10 or newer
- `ffmpeg` available in `PATH` if you want MP3 downloads

## Quick Start on Windows

```bat
start_youtube_downloader.bat
```

The script automatically creates a local virtual environment in `.venv`, installs the dependencies, and starts the app.

## Quick Start on macOS / Linux

```sh
chmod +x start_youtube_downloader.sh
./start_youtube_downloader.sh
```

## Manual Setup

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Windows `cmd`:

```bat
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python app.py
```

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The app is available by default at `http://127.0.0.1:5000`.

## Configuration

You can override the server host and port with environment variables:

```sh
HOST=0.0.0.0 PORT=5000 python app.py
```

Example on Windows:

```bat
set PORT=8080
start_youtube_downloader.bat
```

## Notes

- Video and thumbnail downloads work without `ffmpeg`.
- MP3 downloads require `ffmpeg`.
- The default download folder is `~/Downloads`, but it can be changed in the web interface.

## Legal Disclaimer

This project is provided for educational and personal-use purposes only.

You are solely responsible for how you use this software. Before downloading any video, audio, thumbnail, subtitle, or transcript, make sure you have the legal right to do so and that your use complies with:

- the platform's Terms of Service
- applicable copyright laws
- local laws and regulations in your country

Do not use this project to infringe copyright, violate license terms, bypass access restrictions, or download content you do not have permission to use.

The author and contributors do not endorse or accept liability for unlawful or unauthorized use of this software.
