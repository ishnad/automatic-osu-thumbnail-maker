# osu! Thumbnail Generator

This script, `create_thumbnail.py`, provides a graphical user interface (GUI) to generate thumbnails for osu! gameplay videos, primarily using data from [o!rdr](https://ordr.issou.best/) replay links.

## Features

- Fetches replay metadata from o!rdr.
- Fetches additional data from the osu! API v2 (player avatar, beatmap details, PP, rank, modded star rating).
- Downloads beatmap background images from mirrors (beatconnect.io) or the osu! API.
- Creates a 1920x1080 thumbnail with:
    - Blurred and dimmed background.
    - Custom overlay image (`assets/overlay.png`).
    - Circular player avatar.
    - Map title and difficulty.
    - Player username.
    - Accuracy, mods, and rank (with color-coded glow).
    - PP and star rating.
    - Optional "FC" text.
    - Optional "#1" global rank indicator.
- Saves osu! API credentials to Windows environment variables for persistence.
- Logs operations to `thumbnail_generator.log` (deleted on GUI close) and a GUI log window.

## Requirements

- Python 3.x
- PyQt5
- Pillow (PIL)
- requests
- python-dotenv
- (Windows only for credential saving) `pywin32` might be implicitly needed for `winreg` and `ctypes` interaction with Windows API, though not directly imported by that name.

## Setup

1.  **Clone the repository or download the script.**
2.  **Install dependencies:**
    ```bash
    pip install PyQt5 Pillow requests python-dotenv
    ```
3.  **osu! API Credentials (Recommended):**
    To get full features like player avatars, PP, rank, and modded star ratings, you'll need osu! API v2 credentials (Client ID and Client Secret).
    - Go to your osu! account settings: [https://osu.ppy.sh/home/account/edit#oauth](https://osu.ppy.sh/home/account/edit#oauth)
    - Click "New OAuth Application".
    - Application Name: Anything (e.g., "ThumbnailGenerator")
    - Application Callback URL: `http://localhost/` (or any valid URL, it's not actively used by this script for callback)
    - Click "Register application".
    - You will get a Client ID and Client Secret.

## Usage

1.  **Run the script:**
    ```bash
    python create_thumbnail.py
    ```
2.  **Enter Credentials:**
    - The first time you run the GUI, it will ask for your osu! API Client ID and Client Secret.
    - Enter them and click "Verify Credentials". If successful, they will be saved to your Windows user environment variables (`OSU_CLIENT_ID_THUMBNAIL` and `OSU_CLIENT_SECRET_THUMBNAIL`) for future use.
3.  **Generate Thumbnail:**
    - Once credentials are verified, the GUI will prompt for an o!rdr URL.
    - Enter the o!rdr link (e.g., `https://link.issou.best/xyz123` or `https://ordr.issou.best/watch/abc456`).
    - Click "Generate Thumbnail (Score)" to generate a standard thumbnail.
    - Click "Generate Thumbnail (FC)" to generate a thumbnail with an "FC" overlay.
4.  **Output:**
    - The generated thumbnail will be saved in a `thumbnails` subfolder (e.g., `thumbnails/thumbnail_xyz123.jpg`).
    - A log file `thumbnail_generator.log` is created in the script's directory and deleted when the GUI is closed.

## Notes

- The `assets/overlay.png` file is used as a base overlay. You can customize this image.
- The script attempts to find common system fonts. If specific fonts are missing, it will fall back to default fonts, which might affect the visual appearance.
- On Windows, saving credentials requires writing to the registry. If the script is not run with sufficient permissions (though usually user-level is fine for `HKEY_CURRENT_USER`), saving might fail. The script checks for admin rights and logs a warning if not run as admin, but saving to user environment variables typically doesn't require full admin rights.
- The script is designed to be run from its directory so it can find the `assets` folder.