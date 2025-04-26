import requests
import zipfile
from PIL import Image, ImageDraw, ImageFont, ImageFilter # Added ImageFilter
from io import BytesIO
import os
from dotenv import load_dotenv
import base64
import json
import re
import winreg
import ctypes
import time
import shutil
import sys
from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QLineEdit,
                            QPushButton, QVBoxLayout, QWidget, QMessageBox,
                            QTextEdit)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QThread
import logging
from enum import IntFlag

# --- Setup logging early ---
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_file_handler = logging.FileHandler('thumbnail_generator.log')
log_file_handler.setFormatter(log_formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG) # Set logger level
logger.addHandler(log_file_handler)

# --- Custom Log Handler for PyQt GUI ---
class QtLogHandler(logging.Handler, QObject):
    log_signal = pyqtSignal(str)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setFormatter(log_formatter) # Use the same formatter

    def emit(self, record):
        log_entry = self.format(record)
        # Emit the signal - this is thread-safe
        self.log_signal.emit(log_entry)

# --- Worker Object for Threading ---
class Worker(QObject):
    finished = pyqtSignal(bool, str) # Signal: success(bool), message(str)

    def __init__(self, ordr_url, client_id, client_secret):
        super().__init__()
        self.ordr_url = ordr_url
        self.client_id = client_id
        self.client_secret = client_secret

    def run(self):
        """Runs the thumbnail creation task."""
        try:
            logger.info("Worker thread started for thumbnail generation.")
            create_thumbnail(self.ordr_url, self.client_id, self.client_secret)
            # logger.info("Worker thread finished successfully.")
            # Emit success message only if create_thumbnail doesn't raise an error
            self.finished.emit(True, "Thumbnail created successfully as thumbnail.jpg!")
        except (ValueError, RuntimeError) as user_error:
            # Catch specific errors meant for the user
            logger.error(f"Worker thread failed (User Error): {user_error}")
            self.finished.emit(False, f"{str(user_error)}")
        except Exception as e:
            # Catch unexpected errors
            logger.exception("Worker thread failed (Unexpected Error):") # Log full traceback
            self.finished.emit(False, f"An unexpected error occurred:\n{str(e)}\n\nCheck logs for details.")
        finally:
            logger.info("Worker run method finished execution.")


# --- Mods Enum ---
# Based on osu!api specification: https://osu.ppy.sh/docs/index.html#mods
class Mods(IntFlag):
    NoMod = 0
    NoFail = 1
    Easy = 2
    TouchDevice = 4 # Deprecated, use 4?
    Hidden = 8
    HardRock = 16
    SuddenDeath = 32
    DoubleTime = 64
    Relax = 128
    HalfTime = 256
    Nightcore = 512 # Always used with DoubleTime, replace DT with NC
    Flashlight = 1024
    Autoplay = 2048
    SpunOut = 4096
    Relax2 = 8192 # Autopilot
    Perfect = 16384 # Only included with SuddenDeath
    Key4 = 32768
    Key5 = 65536
    Key6 = 131072
    Key7 = 262144
    Key8 = 524288
    FadeIn = 1048576
    Random = 2097152
    Cinema = 4194304
    Target = 8388608
    Key9 = 16777216
    KeyCoop = 33554432
    Key1 = 67108864
    Key3 = 134217728
    Key2 = 268435456
    ScoreV2 = 536870912
    Mirror = 1073741824

    # Convenience sets
    KeyMod = Key1 | Key2 | Key3 | Key4 | Key5 | Key6 | Key7 | Key8 | Key9 | KeyCoop
    FreeModAllowed = NoFail | Easy | Hidden | HardRock | SuddenDeath | Flashlight | FadeIn | Relax | Relax2 | SpunOut | KeyMod
    ScoreIncreaseMods = Hidden | HardRock | DoubleTime | Flashlight | FadeIn

# --- Mod Conversion Helpers ---
# Map mod strings (from API score) back to enum values
MOD_STRING_TO_ENUM = {
    "NF": Mods.NoFail,
    "EZ": Mods.Easy,
    "TD": Mods.TouchDevice,
    "HD": Mods.Hidden,
    "HR": Mods.HardRock,
    "SD": Mods.SuddenDeath,
    "DT": Mods.DoubleTime,
    "RX": Mods.Relax,
    "HT": Mods.HalfTime,
    "NC": Mods.Nightcore,
    "FL": Mods.Flashlight,
    "AU": Mods.Autoplay, # Note: API might use "AT" sometimes? Check if needed.
    "SO": Mods.SpunOut,
    "AP": Mods.Relax2,
    "PF": Mods.Perfect,
    "K4": Mods.Key4,
    "K5": Mods.Key5,
    "K6": Mods.Key6,
    "K7": Mods.Key7,
    "K8": Mods.Key8,
    "FI": Mods.FadeIn,
    "RD": Mods.Random,
    "CN": Mods.Cinema,
    "TP": Mods.Target,
    "K9": Mods.Key9,
    "KC": Mods.KeyCoop,
    "K1": Mods.Key1,
    "K3": Mods.Key3,
    "K2": Mods.Key2,
    "V2": Mods.ScoreV2,
    "MR": Mods.Mirror,
}

def get_mods_string(mods_enum: int) -> str:
    """Converts an osu! mods enum integer into a standard string representation."""
    if mods_enum == Mods.NoMod:
        return "NM"

    try: # Wrap in try-except in case mods_enum is not a valid int/flag
        mods_enum_flag = Mods(mods_enum) # Convert int to Mods IntFlag instance
    except ValueError:
        logger.warning(f"Invalid integer value for mods enum: {mods_enum}. Defaulting to NM.")
        return "NM"

    mod_strings = []

    # Order matters for display consistency (e.g., EZHDNF)
    if Mods.Easy in mods_enum_flag: mod_strings.append("EZ")
    if Mods.NoFail in mods_enum_flag: mod_strings.append("NF")
    if Mods.HalfTime in mods_enum_flag: mod_strings.append("HT")

    # Handle NC/DT conflict (NC includes DT)
    if Mods.Nightcore in mods_enum_flag:
        mod_strings.append("NC")
    elif Mods.DoubleTime in mods_enum_flag:
        mod_strings.append("DT")

    if Mods.Hidden in mods_enum_flag: mod_strings.append("HD")
    if Mods.HardRock in mods_enum_flag: mod_strings.append("HR")

    # Handle SD/PF conflict (PF includes SD)
    if Mods.Perfect in mods_enum_flag:
        mod_strings.append("PF")
    elif Mods.SuddenDeath in mods_enum_flag:
        mod_strings.append("SD")

    if Mods.Flashlight in mods_enum_flag: mod_strings.append("FL")
    if Mods.Relax in mods_enum_flag: mod_strings.append("RX")
    if Mods.Relax2 in mods_enum_flag: mod_strings.append("AP")
    if Mods.SpunOut in mods_enum_flag: mod_strings.append("SO")
    if Mods.TouchDevice in mods_enum_flag: mod_strings.append("TD") # Check if TD is still used
    if Mods.Cinema in mods_enum_flag: mod_strings.append("CN")
    if Mods.ScoreV2 in mods_enum_flag: mod_strings.append("V2")
    if Mods.Mirror in mods_enum_flag: mod_strings.append("MR") # Mania only usually

    # Key mods (less common in std thumbnails but good to have)
    if Mods.Key1 in mods_enum_flag: mod_strings.append("K1")
    if Mods.Key2 in mods_enum_flag: mod_strings.append("K2")
    if Mods.Key3 in mods_enum_flag: mod_strings.append("K3")
    if Mods.Key4 in mods_enum_flag: mod_strings.append("K4")
    if Mods.Key5 in mods_enum_flag: mod_strings.append("K5")
    if Mods.Key6 in mods_enum_flag: mod_strings.append("K6")
    if Mods.Key7 in mods_enum_flag: mod_strings.append("K7")
    if Mods.Key8 in mods_enum_flag: mod_strings.append("K8")
    if Mods.Key9 in mods_enum_flag: mod_strings.append("K9")
    if Mods.KeyCoop in mods_enum_flag: mod_strings.append("KC")
    if Mods.Random in mods_enum_flag: mod_strings.append("RD")
    if Mods.FadeIn in mods_enum_flag: mod_strings.append("FI")
    if Mods.Target in mods_enum_flag: mod_strings.append("TP") # Check if TP is still used

    return "".join(mod_strings) if mod_strings else "NM" # Return NM if list is empty

def get_mods_enum_from_list(mod_list: list[str]) -> int:
    """Converts a list of osu! mod strings (like ['HD', 'DT']) into a mods enum integer."""
    final_enum = Mods.NoMod
    has_nc = "NC" in mod_list
    has_dt = "DT" in mod_list
    has_pf = "PF" in mod_list
    has_sd = "SD" in mod_list

    for mod_str in mod_list:
        # Handle combined mods correctly
        if mod_str == "NC" and has_dt: # NC implies DT
            final_enum |= Mods.Nightcore
            continue # Don't add DT separately if NC is present
        if mod_str == "DT" and has_nc:
            continue # Already handled by NC

        if mod_str == "PF" and has_sd: # PF implies SD
            final_enum |= Mods.Perfect
            continue # Don't add SD separately if PF is present
        if mod_str == "SD" and has_pf:
            continue # Already handled by PF

        enum_val = MOD_STRING_TO_ENUM.get(mod_str)
        if enum_val is not None:
            final_enum |= enum_val
        else:
            logger.warning(f"Unknown mod string encountered: {mod_str}. Skipping.")

    # Ensure NC includes DT and PF includes SD even if only one was listed somehow
    if Mods.Nightcore in final_enum:
        final_enum |= Mods.DoubleTime
    if Mods.Perfect in final_enum:
        final_enum |= Mods.SuddenDeath

    return int(final_enum)


# OAuth2 authentication
def get_access_token(client_id, client_secret):
    auth_string = f"{client_id}:{client_secret}"
    auth_bytes = auth_string.encode("ascii")
    auth_base64 = base64.b64encode(auth_bytes).decode("ascii")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Authorization": f"Basic {auth_base64}"
    }

    data = {
        "grant_type": "client_credentials",
        "scope": "public"
    }

    logger.info("Requesting OAuth token")
    response = requests.post(
        "https://osu.ppy.sh/oauth/token",
        headers=headers,
        data=data,
        timeout=10 # Add timeout for token request
    )

    if response.status_code == 200:
        logger.info("OAuth token obtained successfully")
        return response.json().get("access_token")
    else:
        logger.error(f"Authentication failed: {response.status_code} - {response.text}")
        raise Exception(f"Authentication failed: {response.text}")

# API request helper
def make_api_request(token, endpoint, method='GET', payload=None):
    """Makes an API request to the osu! API v2."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json", # Needed for POST
        "Authorization": f"Bearer {token}"
    }
    url = f"https://osu.ppy.sh/api/v2/{endpoint}"
    logger.info(f"Making API request: {method} {url}")

    try:
        if method.upper() == 'POST':
            response = requests.post(url, headers=headers, json=payload, timeout=15)
        else: # Default to GET
            response = requests.get(url, headers=headers, timeout=15)

        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        logger.info(f"API request successful for {method} {endpoint}")
        return response.json()

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed ({method} {url}): {e}")
        # More specific error message if possible
        error_message = f"API request failed for {method} {endpoint}: {e}"
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_details = e.response.json() # Try to get JSON error details
                error_message += f" - Details: {error_details}"
            except json.JSONDecodeError:
                error_message += f" - Status: {e.response.status_code}, Body: {e.response.text[:200]}..." # Show beginning of non-JSON error
        raise Exception(error_message)
    except Exception as e:
        logger.error(f"Unexpected error during API request ({method} {url}): {e}")
        raise Exception(f"Unexpected error during API request for {method} {endpoint}: {e}")


def download_from_mirror(beatmapset_id):
    """Attempt to download beatmap from mirror API and extract background image"""
    extract_folder = f"./temp_beatmap_{beatmapset_id}"
    bg_image = None
    try:
        logger.info(f"Attempting to download beatmapset {beatmapset_id} from mirror...")
        # Add timeout to mirror request
        response = requests.get(f"https://beatconnect.io/b/{beatmapset_id}/", stream=True, timeout=30)
        response.raise_for_status()

        osz_path = f"{beatmapset_id}.osz"
        with open(osz_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"Downloaded {osz_path}")

        # Extract .osz
        try:
            with zipfile.ZipFile(osz_path, 'r') as zip_ref:
                zip_ref.extractall(extract_folder)
            logger.info(f"Extracted to {extract_folder}")
        finally:
            if os.path.exists(osz_path):
                os.remove(osz_path)
                logger.info(f"Removed temporary file: {osz_path}")

        # Find and parse .osu file
        bg_filename = None
        for filename in os.listdir(extract_folder):
            if filename.endswith(".osu"):
                osu_file_path = os.path.join(extract_folder, filename)
                logger.info(f"Parsing .osu file: {osu_file_path}")
                try:
                    # Simplified parsing: just look for the background line
                    with open(osu_file_path, 'r', encoding='utf-8') as f:
                        in_events_section = False
                        for line in f:
                            line = line.strip()
                            if not in_events_section:
                                if line == "[Events]":
                                    in_events_section = True
                                continue
                            else: # Inside [Events]
                                if line.startswith("//") or not line:
                                    continue
                                # Stop if we hit the next section
                                if line.startswith("["):
                                    break
                                # Look for background definition: 0,0,"bg.jpg",0,0 or Background,,0,"bg.jpg"
                                parts = line.split(',')
                                if len(parts) >= 3 and (parts[0] == '0' or parts[0].lower() == 'background') and parts[1] == '0':
                                    bg_filename = parts[2].strip('"')
                                    logger.info(f"Found background filename in .osu: {bg_filename}")
                                    break # Found the background
                                elif len(parts) >= 4 and parts[0].lower() == 'background' and parts[2] == '0': # Alternative format
                                    bg_filename = parts[3].strip('"')
                                    logger.info(f"Found background filename (alt format) in .osu: {bg_filename}")
                                    break # Found the background
                        if bg_filename: # Stop searching other .osu files if found
                            break
                except Exception as e:
                    logger.error(f"Error parsing .osu file {osu_file_path}: {e}")
                    continue # Try next .osu file

        # Load the background image if found
        if bg_filename:
            # Sometimes the filename in .osu might have incorrect casing or path separators
            # Try to find the actual file case-insensitively
            actual_bg_filename = None
            for item in os.listdir(extract_folder):
                # Handle potential subdirectories like 'BG/' often used
                item_path = os.path.join(extract_folder, item)
                if os.path.isfile(item_path) and item.lower() == bg_filename.lower():
                    actual_bg_filename = item
                    break
                elif os.path.isdir(item_path):
                    # Check one level deeper for common BG folders
                    for sub_item in os.listdir(item_path):
                         sub_item_path = os.path.join(item_path, sub_item) # Full path for isfile check
                         if os.path.isfile(sub_item_path) and sub_item.lower() == bg_filename.lower():
                             actual_bg_filename = os.path.join(item, sub_item) # Keep relative path
                             break
                    if actual_bg_filename: break


            if actual_bg_filename:
                image_path = os.path.join(extract_folder, actual_bg_filename)
                if os.path.exists(image_path):
                    logger.info(f"Loading background image from: {image_path}")
                    try:
                        bg_image_temp = Image.open(image_path)
                        # Re-save to BytesIO to handle potential format issues or locks
                        img_bytes = BytesIO()
                        # Ensure format is specified, default to PNG if unknown
                        img_format = Image.registered_extensions().get(os.path.splitext(image_path)[1].lower(), 'PNG')
                        bg_image_temp.save(img_bytes, format=img_format)
                        bg_image_temp.close() # Close the file handle
                        img_bytes.seek(0)
                        bg_image = Image.open(img_bytes).convert('RGB') # Ensure RGB after loading
                        logger.info("Background image loaded successfully from mirror download.")
                    except Exception as e:
                        logger.error(f"Failed to load image file {image_path}: {e}")
                else:
                    logger.warning(f"Background image file specified in .osu not found: {image_path}")
            else:
                 logger.warning(f"Background filename '{bg_filename}' found in .osu, but no matching file found in archive (checked subdirs).")

    except requests.exceptions.RequestException as e:
        logger.warning(f"Mirror download failed for beatmapset {beatmapset_id}: {e}")
    except zipfile.BadZipFile:
        logger.error(f"Downloaded file for {beatmapset_id} is not a valid zip file.")
    except Exception as e:
        logger.error(f"An unexpected error occurred during mirror download/extraction for {beatmapset_id}: {e}")
    finally:
        if os.path.exists(extract_folder):
            max_retries = 3
            for i in range(max_retries):
                try:
                    shutil.rmtree(extract_folder)
                    logger.info(f"Cleaned up temporary folder: {extract_folder}")
                    break
                except PermissionError as e:
                     logger.warning(f"PermissionError cleaning up {extract_folder} (attempt {i+1}/{max_retries}): {e}. Retrying...")
                     time.sleep(0.5)
                except Exception as e:
                    logger.error(f"Failed to clean up temporary folder {extract_folder} (attempt {i+1}/{max_retries}): {e}")
                    if i < max_retries - 1:
                        time.sleep(0.5) # Wait before retrying
                    else:
                        logger.error(f"Giving up on cleaning temporary folder {extract_folder} after {max_retries} attempts.")
                        break # Stop retrying after max attempts
    return bg_image


def extract_ordr_code(ordr_url: str) -> str:
    """Pulls the code from ORDR URLs like https://link.issou.best/<code> or https://ordr.issou.best/watch/<code>"""
    # Updated regex to handle both link formats and potential trailing slashes/params
    # Matches link.issou.best/CODE or ordr.issou.best/renders?/CODE or ordr.issou.best/watch/CODE
    m = re.search(r"(?:link\.issou\.best/|ordr\.issou\.best/(?:renders?|watch)/)(\w+)", ordr_url)
    if not m:
        logger.error(f"Could not extract ORDR code from URL: {ordr_url}")
        raise ValueError(f"Invalid or unrecognized ORDR URL format: {ordr_url}")
    code = m.group(1)
    logger.info(f"Extracted ORDR code: {code}")
    return code

def fetch_ordr_metadata(link_code: str) -> dict:
    """GET https://apis.issou.best/ordr/renders?link=<code>"""
    url = "https://apis.issou.best/ordr/renders"
    params = {"link": link_code}
    logger.info(f"Fetching ORDR metadata for code: {link_code} from {url}")
    try:
        resp = requests.get(url, params=params, timeout=15) # Added timeout
        resp.raise_for_status() # Raises HTTPError for bad responses (4xx or 5xx)
        data = resp.json()
        if not data.get("renders"):
            logger.error(f"No render data found for ORDR code: {link_code}")
            raise RuntimeError("No render data found for code " + link_code)
        logger.info(f"Successfully fetched ORDR metadata for code: {link_code}")
        # Assuming we only care about the first render if multiple exist
        return data["renders"][0]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch ORDR metadata for code {link_code}: {e}")
        raise RuntimeError(f"Failed to connect to ORDR API: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response from ORDR API for code {link_code}: {e}")
        raise RuntimeError(f"Invalid response from ORDR API: {e}")

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller/Nuitka """
    try:
        # PyInstaller/Nuitka with --onefile creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
        logger.debug(f"Running from bundle (_MEIPASS), base path: {base_path}")
    except AttributeError:
        # If _MEIPASS is not set, check if running frozen (Nuitka standalone/onefile)
        if getattr(sys, 'frozen', False):
             # Use the directory containing the executable
             base_path = os.path.dirname(sys.executable)
             logger.debug(f"Running frozen (no _MEIPASS), using executable dir: {base_path}")
        else:
             # Not frozen, assume running as script
             base_path = os.path.abspath(".")
             logger.debug(f"Running from script, base path: {base_path}")

    path = os.path.join(base_path, relative_path)
    logger.debug(f"Resource path check: Resolved '{relative_path}' to '{path}'")
    return path

# --- Text Dimension Helper Functions ---
def get_text_dimensions(font, text):
    """Gets width and height of text using the most reliable method."""
    try:
        # Prefer getbbox for TrueType fonts (more accurate)
        bbox = font.getbbox(text)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        # Note: bbox height includes ascender+descender, might not be visual height
        return width, height
    except AttributeError:
        try:
            # Fallback for older PIL/Pillow or non-TrueType fonts
            width, height = font.getsize(text)
            return width, height
        except Exception as e:
             logger.warning(f"Could not get dimensions for text '{text}' with font {font}: {e}. Using default 10x10.")
             return 10, 10

# --- Text Drawing Helper Functions ---
def draw_text_with_outline(draw_surface, pos, text, font, fill, outline_fill, outline_width):
    """Draws text with a multi-pass outline."""
    x, y = pos
    # Draw outline by drawing text multiple times with offsets
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            # Skip the center position for the outline pass
            if dx == 0 and dy == 0:
                continue
            # Basic check to avoid drawing too far out (optional optimization)
            # Use Manhattan distance for simplicity and performance
            if abs(dx) + abs(dy) > outline_width:
                 continue
            draw_surface.text((x + dx, y + dy), text, font=font, fill=outline_fill)
    # Draw the main text on top
    draw_surface.text(pos, text, font=font, fill=fill)

def draw_centered_text_with_outline(draw_surface, center_x, y_pos, text, font, fill, outline_fill, outline_width):
    """Draws text horizontally centered at center_x with an outline."""
    text_width, _ = get_text_dimensions(font, text)
    x_pos = center_x - (text_width / 2)
    draw_text_with_outline(draw_surface, (x_pos, y_pos), text, font, fill, outline_fill, outline_width)

def draw_right_aligned_text_with_outline(draw_surface, right_x, y_pos, text, font, fill, outline_fill, outline_width):
    """Draws text right-aligned ending at right_x with an outline."""
    text_width, _ = get_text_dimensions(font, text)
    x_pos = right_x - text_width
    draw_text_with_outline(draw_surface, (x_pos, y_pos), text, font, fill, outline_fill, outline_width)

# --- Truncation Helper Function ---
def truncate_text(text, font, max_width):
    """Truncates text with '...' if it exceeds max_width."""
    try:
        # Prefer getlength if available (more accurate for TrueType)
        text_width = font.getlength(text)
    except AttributeError:
        try:
            # Fallback to getbbox
            bbox = font.getbbox(text)
            text_width = bbox[2] - bbox[0]
        except AttributeError:
            try:
                # Fallback for older PIL/Pillow or non-TrueType fonts
                text_width = font.getsize(text)[0]
            except Exception:
                logger.warning(f"Could not get width for text '{text}' with font {font}. Truncation might be inaccurate.")
                # Basic estimation if all else fails
                avg_char_width = font.getsize("A")[0] if hasattr(font, 'getsize') else 10
                text_width = len(text) * avg_char_width


    if text_width <= max_width:
        return text
    else:
        # Estimate ellipsis width using the same method hierarchy
        try: ellipsis_width = font.getlength("...")
        except AttributeError:
            try: ellipsis_width = font.getbbox("...")[2] - font.getbbox("...")[0]
            except AttributeError:
                try: ellipsis_width = font.getsize("...")[0]
                except Exception: ellipsis_width = 30

        truncated = ""
        # Iterate backwards to find suitable truncation point
        for i in range(len(text) -1, 0, -1):
            test_text = text[:i]
            # Get width of truncated text using the same method hierarchy
            try: test_width = font.getlength(test_text)
            except AttributeError:
                try: test_width = font.getbbox(test_text)[2] - font.getbbox(test_text)[0]
                except AttributeError:
                    try: test_width = font.getsize(test_text)[0]
                    except Exception: test_width = i * 10 # Estimate

            if test_width + ellipsis_width <= max_width:
                truncated = test_text + "..."
                break
        # Return ellipsis if text is too short to truncate or truncation failed
        return truncated if truncated else "..."


def create_thumbnail(ordr_url, client_id=None, client_secret=None):
    """Create osu! thumbnail from ORDR render link.
       NOTE: This function now assumes it might be called from a worker thread.
       It should raise exceptions on failure for the worker to catch.
    """
    # No try/except block here - let the caller (Worker.run) handle exceptions
    if not ordr_url:
        logger.error("ORDR URL is required but was not provided.")
        raise ValueError("ORDR URL is required")

    logger.info(f"Generating thumbnail from ORDR URL: {ordr_url}")
    code = extract_ordr_code(ordr_url)
    meta = fetch_ordr_metadata(code)

    # Extract fields from metadata
    username = meta.get("replayUsername", "Unknown Player")
    song_title = meta.get("mapTitle", "Unknown Song")
    difficulty = meta.get("replayDifficulty", "Unknown Difficulty")
    beatmapset_id = meta.get("mapID") # This is actually beatmapset ID from ORDR
    ordr_mods_enum = meta.get("modsEnum", 0) # Get mods enum directly from ORDR

    if not beatmapset_id:
         logger.error("mapID not found in ORDR metadata.")
         raise ValueError("Could not find beatmapset ID in ORDR metadata.")

    # Get mods string initially from ORDR enum as a fallback
    mods_str = get_mods_string(ordr_mods_enum)
    final_mods_enum = ordr_mods_enum # Store the enum value we'll use for star calc
    logger.info(f"ORDR Metadata - User: {username}, Title: {song_title}, Diff: {difficulty}, SetID: {beatmapset_id}, Mods (from ORDR enum): {mods_str} (Enum: {ordr_mods_enum})")

    # Parse accuracy & stars from description (still useful as fallback/primary source)
    desc = meta.get("description", "")
    acc_match = re.search(r"Accuracy:\s*([\d.]+)%", desc)
    accuracy = float(acc_match.group(1)) if acc_match else 0.0
    accuracy_str = f"{accuracy:.2f}%"

    star_match = re.search(r"\(([\d.]+) ⭐\)", desc) # Match stars like (5.67 ⭐)
    # Use description stars as initial fallback only
    base_stars_from_desc = float(star_match.group(1)) if star_match else 0.0
    stars = base_stars_from_desc # Initialize stars with this value
    stars_str = f"{stars:.2f}" # Keep precision for display
    stars_source = "ORDR Description" # Track where the star value came from

    logger.info(f"Parsed from description - Accuracy: {accuracy_str}, Stars: {stars_str} (Source: {stars_source})")


    token = None
    beatmapset_data = None
    user_data = None
    pp = 0.0 # Default PP
    beatmap_id = None # Specific difficulty ID
    mods_source = "ORDR Enum" # Track where the final mods came from
    rank = 'D' # Default rank
    is_fc = False # Default FC status
    api_mods_list = None # Store mods list from API score if available

    # Fetch API data if credentials provided
    if client_id and client_secret:
        try:
            token = get_access_token(client_id, client_secret)
            # Fetch beatmapset details (needed for difficulty ID and potentially background)
            logger.info(f"Fetching beatmapset data for set ID: {beatmapset_id}")
            beatmapset_data = make_api_request(token, f"beatmapsets/{beatmapset_id}")
            # Fetch user details (needed for user ID and avatar)
            logger.info(f"Fetching user data for username: {username}")
            # URL encode username just in case it has special characters
            encoded_username = requests.utils.quote(username)
            user_data = make_api_request(token, f"users/{encoded_username}/osu")
        except Exception as e:
            logger.warning(f"Failed to fetch initial API data: {e}. Proceeding with limited info.")
            # Continue without API data if possible, features like PP/Avatar/API Mods/Modded Stars might fail

    # Get background image: Try mirror first, then API
    logger.info(f"Attempting to get background for beatmapset ID: {beatmapset_id}")
    bg_image = download_from_mirror(beatmapset_id)

    if not bg_image and beatmapset_data: # Use fetched beatmapset_data if available
        covers = beatmapset_data.get("covers", {})
        # Prefer cover@2x for higher resolution, fallback to cover
        bg_url = covers.get("cover@2x") or covers.get("cover")
        if bg_url:
            logger.info("Using background from osu! API")
            try:
                response = requests.get(bg_url, timeout=15)
                response.raise_for_status()
                bg_image = Image.open(BytesIO(response.content)).convert('RGB') # Ensure RGB
                logger.info("Successfully loaded background from osu! API.")
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to download background from osu! API ({bg_url}): {e}")
            except Exception as e:
                 logger.error(f"Failed to process background from osu! API ({bg_url}): {e}")

    if not bg_image:
        logger.error("Could not obtain background image from mirror or API.")
        raise ValueError("Could not obtain background image")

    # Create thumbnail base
    bg_width, bg_height = bg_image.size
    target_width, target_height = 1920, 1080
    logger.info(f"Background size: {bg_width}x{bg_height}. Target size: {target_width}x{target_height}")

    # Calculate scaling to fill the target aspect ratio, then crop
    bg_aspect = bg_width / bg_height
    target_aspect = target_width / target_height

    if bg_aspect > target_aspect: # Background is wider than target
        scale = target_height / bg_height
        new_width = int(bg_width * scale)
        new_height = target_height
        resized = bg_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        # Crop horizontally
        x_crop = (new_width - target_width) // 2
        y_crop = 0
        cropped = resized.crop((x_crop, y_crop, x_crop + target_width, y_crop + target_height))
    else: # Background is taller than target (or same aspect)
        scale = target_width / bg_width
        new_width = target_width
        new_height = int(bg_height * scale)
        resized = bg_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        # Crop vertically
        x_crop = 0
        y_crop = (new_height - target_height) // 2
        cropped = resized.crop((x_crop, y_crop, x_crop + target_width, y_crop + target_height))

    # Ensure the final image is RGB before drawing
    thumbnail_base = cropped.convert("RGB")
    logger.info("Created base thumbnail canvas and processed background.")

    # Apply blur to the base thumbnail
    blur_radius = 5 # Adjust this value for more/less blur
    thumbnail = thumbnail_base.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    logger.info(f"Applied Gaussian blur with radius {blur_radius} to background.")

    # Re-initialize Draw object on the potentially blurred image
    draw = ImageDraw.Draw(thumbnail)

    # Add asset1.png overlay FIRST (after blur, before other elements)
    try:
        asset_path = resource_path(os.path.join("assets", "asset1.png"))
        # Add logging before loading asset1.png
        logger.info(f"Attempting to load asset1.png from: {asset_path}")
        logger.info(f"Does asset1.png path exist? {os.path.exists(asset_path)}")
        if os.path.exists(asset_path):
            asset_img = Image.open(asset_path).convert("RGBA") # Load with alpha
            # Ensure asset is the correct size (optional, but good practice)
            if asset_img.size != (target_width, target_height):
                logger.warning(f"asset1.png size {asset_img.size} does not match target {target_width}x{target_height}. Resizing.")
                asset_img = asset_img.resize((target_width, target_height), Image.Resampling.LANCZOS)
            # Paste asset covering the whole thumbnail at (0,0) using its alpha
            thumbnail.paste(asset_img, (0, 0), asset_img)
            logger.info(f"Overlayed asset1.png at (0, 0)")
        else:
            logger.warning(f"Asset file not found at {asset_path}. Skipping overlay.")
    except FileNotFoundError:
         logger.warning(f"Asset file 'assets/asset1.png' not found. Skipping overlay.")
    except Exception as asset_e:
        logger.error(f"Could not load or place asset1.png: {asset_e}")


    # Add player avatar using fetched user_data
    avatar_url = None
    user_id = None # Keep track of user ID for PP fetch
    avatar_size = 450 # Define default size (increased from 150)
    avatar_pos_x = (target_width - avatar_size) // 2
    avatar_pos_y = (target_height - avatar_size) // 2
    avatar_pos = (avatar_pos_x, avatar_pos_y)
    # This is halfway between the top and the center of the avatar
    side_text_vertical_center_y = avatar_pos_y + (avatar_size / 4)

    try:
        if user_data: # Use data from API if available
             avatar_url = user_data.get("avatar_url")
             user_id = user_data.get("id") # Get user ID here
             logger.info(f"Found avatar URL from API: {avatar_url}")

        if not avatar_url: # Fallback if no API data or URL missing
             # Fallback using a.ppy.sh - less reliable, especially for numeric names or restricted users
             logger.warning(f"API data for user '{username}' not available or missing avatar_url. Using fallback a.ppy.sh URL.")
             # Try fetching user ID via API again just for the avatar if user_data failed initially but token exists
             if token and not user_id:
                 try:
                     logger.info(f"Attempting secondary fetch for user ID: {username}")
                     encoded_username = requests.utils.quote(username)
                     temp_user_data = make_api_request(token, f"users/{encoded_username}/osu")
                     user_id = temp_user_data.get("id")
                     if user_id:
                         avatar_url = f"https://a.ppy.sh/{user_id}"
                         logger.info(f"Using fallback avatar URL with fetched user ID: {avatar_url}")
                     else:
                         logger.warning("Could not fetch user ID for fallback avatar.")
                         avatar_url = None # Indicate failure
                 except Exception as e:
                     logger.warning(f"Secondary user fetch failed: {e}")
                     avatar_url = None
             else:
                 # If no token or secondary fetch failed, cannot use ID-based fallback
                 avatar_url = None # Indicate failure

        if avatar_url:
            response = requests.get(avatar_url, timeout=10)
            response.raise_for_status()
            avatar_img = Image.open(BytesIO(response.content)).convert("RGBA") # Keep alpha for masking
            avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)

            # Create circular mask
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.ellipse((0, 0, avatar_size, avatar_size), fill=255)

            # Paste avatar at the calculated position
            thumbnail.paste(avatar_img, avatar_pos, mask) # Use mask for transparency
            logger.info(f"Added player avatar at {avatar_pos} (center)")

        else:
            logger.warning("Could not determine avatar URL. Skipping avatar.")

    except Exception as e:
        logger.error(f"Couldn't load or place player avatar: {e}")


    # --- Font Setup ---
    # Prioritize Symbola for special characters, fallback for others
    font_paths = [
        resource_path(os.path.join("symbola", "Symbola.ttf")),
        "C:/Windows/Fonts/arialuni.ttf", # Arial Unicode MS (common fallback)
        "C:/Windows/Fonts/seguiemj.ttf", # Segoe UI Emoji (Windows)
        "C:/Windows/Fonts/arialbd.ttf", # Arial Bold
        "C:/Windows/Fonts/arial.ttf" # Arial Regular
    ]

    def find_font(paths, size):
        # Check provided paths (already resolved by resource_path or absolute)
        for path in paths:
            try:
                # Add logging before loading font
                logger.info(f"Attempting to load font from: {path}")
                logger.info(f"Does font path exist? {os.path.exists(path)}")
                if os.path.exists(path):
                    logger.info(f"Loading font: {path} with size {size}")
                    return ImageFont.truetype(path, size)
                else:
                    logger.debug(f"Font path not found: {path}")
            except IOError as e:
                logger.warning(f"Could not load font {path} with size {size}: {e}")

        logger.error(f"Could not find any suitable font in paths: {paths} for size {size}. Using default.")
        return ImageFont.load_default(size) # Pass size to default loader

    # Define font sizes here for easy adjustment
    # Modify these values to change the text sizes on the thumbnail
    size_map_title = 160      # Size for the map title (top center)
    size_difficulty = 70     # Size for the difficulty name (under title)
    size_username = 120       # Size for the username (bottom center)
    size_pp_stars = 115       # Size for PP (right of avatar) and Stars number (under PP)
    size_acc_mods = 115       # Size for Accuracy (left of avatar) and Mods (under Accuracy)
    size_star_emoji = 80      # Size for the star emoji (smaller than number)
    size_fc_text = 300         # Size for the "FC" text

    # Load fonts using the defined sizes
    font_map_title = find_font(font_paths, size_map_title)
    font_difficulty = find_font(font_paths, size_difficulty)
    font_username = find_font(font_paths, size_username)
    font_pp_stars = find_font(font_paths, size_pp_stars)
    font_acc_mods = find_font(font_paths, size_acc_mods)
    font_star_emoji = find_font(font_paths, size_star_emoji)
    font_fc = find_font(font_paths, size_fc_text)

    # Check if default font was loaded and log path if successful
    if hasattr(font_map_title, 'path'): logger.info(f"Using font: {font_map_title.path} (map title)")
    else: logger.warning("Using default PIL font (map title).")
    if hasattr(font_difficulty, 'path'): logger.info(f"Using font: {font_difficulty.path} (difficulty)")
    else: logger.warning("Using default PIL font (difficulty).")
    if hasattr(font_username, 'path'): logger.info(f"Using font: {font_username.path} (username)")
    else: logger.warning("Using default PIL font (username).")
    if hasattr(font_pp_stars, 'path'): logger.info(f"Using font: {font_pp_stars.path} (pp/stars)")
    else: logger.warning("Using default PIL font (pp/stars).")
    if hasattr(font_acc_mods, 'path'): logger.info(f"Using font: {font_acc_mods.path} (acc/mods)")
    else: logger.warning("Using default PIL font (acc/mods).")
    if hasattr(font_star_emoji, 'path'): logger.info(f"Using font: {font_star_emoji.path} (star emoji)")
    else: logger.warning("Using default PIL font (star emoji).")
    if hasattr(font_fc, 'path'): logger.info(f"Using font: {font_fc.path} (FC text)")
    else: logger.warning("Using default PIL font (FC text).")


    # --- Find Beatmap ID and Fetch Score/PP/Mods/Rank ---
    # Note: mods_str and final_mods_enum are initialized using ORDR data as a fallback.
    if token and user_id and beatmapset_data: # Check if we have API token, user ID, and beatmapset data
        try:
            target_difficulty_name = meta.get('replayDifficulty', '')
            logger.info(f"Attempting to find beatmap ID for difficulty: '{target_difficulty_name}' in set {beatmapset_id}")

            # Find the beatmap ID for the specific difficulty within the set
            found_map = False
            base_stars_from_api = 0.0 # Store base star rating from API beatmap data
            for beatmap in beatmapset_data.get('beatmaps', []):
                # Compare difficulty names (case-insensitive and strip whitespace)
                if beatmap.get('version', '').strip().lower() == target_difficulty_name.strip().lower():
                    beatmap_id = beatmap.get('id')
                    # Get base star rating from API beatmap data
                    api_stars = beatmap.get('difficulty_rating')
                    if api_stars:
                        base_stars_from_api = float(api_stars)
                        # Update stars only if API base stars are available and ORDR desc stars were 0
                        if stars == 0.0:
                            stars = base_stars_from_api
                            stars_str = f"{stars:.2f}"
                            stars_source = "osu! API Beatmap (Base)"
                            logger.info(f"Using base stars from API beatmap data: {stars_str}")
                        else:
                            logger.info(f"API base stars available ({api_stars:.2f}), but keeping initial stars from ORDR description ({stars:.2f}) for now.")
                    logger.info(f"Found matching beatmap ID: {beatmap_id} for difficulty '{target_difficulty_name}'. API Base Stars: {base_stars_from_api:.2f}")
                    found_map = True
                    break

            if not found_map:
                 # Fallback: If exact match fails, try finding the closest star rating if ORDR provided one
                 if base_stars_from_desc > 0:
                     logger.warning(f"Exact difficulty name match failed. Trying fallback using star rating from ORDR description: {base_stars_from_desc}")
                     closest_map = None
                     min_diff = float('inf')
                     for beatmap in beatmapset_data.get('beatmaps', []):
                         api_stars = beatmap.get('difficulty_rating')
                         if api_stars:
                             diff = abs(float(api_stars) - base_stars_from_desc)
                             if diff < min_diff:
                                 min_diff = diff
                                 closest_map = beatmap
                     # Allow a small tolerance for star rating match
                     if closest_map and min_diff < 0.1:
                         beatmap_id = closest_map.get('id')
                         api_stars = closest_map.get('difficulty_rating')
                         actual_diff_name = closest_map.get('version', 'Unknown Difficulty')
                         if api_stars:
                             base_stars_from_api = float(api_stars)
                             # Update stars only if API base stars are available and ORDR desc stars were 0
                             if stars == 0.0:
                                 stars = base_stars_from_api
                                 stars_str = f"{stars:.2f}"
                                 stars_source = "osu! API Beatmap (Base - Fallback Match)"
                                 logger.info(f"Using base stars from API beatmap data (fallback match): {stars_str}")
                             else:
                                 logger.info(f"API base stars available ({api_stars:.2f}) from fallback match, but keeping initial stars from ORDR description ({stars:.2f}) for now.")
                         logger.info(f"Found closest beatmap by stars: ID {beatmap_id}, Diff '{actual_diff_name}', Base Stars {base_stars_from_api:.2f} (Difference: {min_diff:.3f})")
                         difficulty = actual_diff_name # Update difficulty name to the matched one
                         found_map = True
                     else:
                         logger.warning(f"Could not find a close match by star rating either (min diff: {min_diff:.3f}).")

            if found_map and beatmap_id:
                # --- Fetch User's Best Score on the Map ---
                logger.info(f"Fetching score details for user {user_id} on beatmap {beatmap_id} (for PP, Mods, Rank override)")
                # Note: The /scores endpoint might not return the *exact* score from the replay if multiple scores exist.
                # It usually returns the user's best score on that map.
                score_data = make_api_request(token, f"beatmaps/{beatmap_id}/scores/users/{user_id}")
                score_info = score_data.get("score")
                if score_info:
                    fetched_pp = score_info.get("pp")
                    if fetched_pp is not None: # PP can be 0.0, so check for None
                        pp = float(fetched_pp)
                        logger.info(f"Fetched PP from user's best score: {pp:.2f}")
                    else:
                        pp = 0.0 # Handle null PP (e.g., loved maps)
                        logger.info("API returned null PP for the score, setting to 0.")

                    # Get Rank
                    fetched_rank = score_info.get("rank")
                    if fetched_rank:
                        rank = fetched_rank # e.g., "A", "S", "SH", "X", "XH"
                        logger.info(f"Fetched Rank from user's best score: {rank}")
                    else:
                        rank = 'D' # Default if rank is missing
                        logger.warning("Rank not found in API score data, defaulting to D.")

                    # Get FC status
                    is_fc = score_info.get("perfect", False)
                    logger.info(f"Fetched FC status from user's best score: {is_fc}")

                    # Mod Override Logic
                    api_mods_list = score_info.get("mods", []) # Store this list
                    if api_mods_list:
                        api_mods_str = "".join(api_mods_list)
                        logger.info(f"Found mods from API score: {api_mods_str}. Overriding mods from ORDR enum.")
                        mods_str = api_mods_str
                        mods_source = "osu! API Score"
                        # Convert API mod list back to enum for star calculation
                        final_mods_enum = get_mods_enum_from_list(api_mods_list)
                        logger.info(f"Converted API mods list {api_mods_list} to enum: {final_mods_enum}")
                    elif not api_mods_list and mods_str != "NM":
                        logger.info("API score has no mods (NM), but ORDR enum provided mods. Keeping ORDR mods.")
                        # Keep mods_str and final_mods_enum from ORDR
                    else:
                        logger.info("API score has no mods (NM), and ORDR enum was also NM. Setting mods to NM.")
                        mods_str = "NM"
                        mods_source = "osu! API Score (NM)"
                        final_mods_enum = Mods.NoMod # Ensure enum is also NM

                else: # This else corresponds to 'if score_info:'
                    logger.warning(f"Score not found via API for user {user_id} on beatmap {beatmap_id}. PP set to 0. Rank set to D. FC status unknown. Using mods from ORDR enum ('{mods_str}', enum {final_mods_enum}).")
                    pp = 0.0
                    rank = 'D'
                    is_fc = False # Assume not FC if score not found
                    # Keep mods_str and final_mods_enum from ORDR

                # --- Fetch Modded Star Rating ---
                # Requires beatmap_id and the final_mods_enum determined above
                try:
                    logger.info(f"Fetching difficulty attributes for beatmap {beatmap_id} with mods enum: {final_mods_enum}")
                    attributes_payload = {"mods": final_mods_enum}
                    attributes_data = make_api_request(token, f"beatmaps/{beatmap_id}/attributes", method='POST', payload=attributes_payload)
                    modded_stars_value = attributes_data.get("attributes", {}).get("star_rating")
                    if modded_stars_value is not None:
                        stars = float(modded_stars_value)
                        stars_str = f"{stars:.2f}"
                        stars_source = "osu! API Attributes (Modded)"
                        logger.info(f"Successfully fetched modded star rating: {stars_str}")
                    else:
                        logger.warning(f"Modded star rating not found in attributes response. Falling back to previous value ({stars_str}, Source: {stars_source}).")
                except Exception as attr_e:
                    logger.error(f"Failed to fetch modded difficulty attributes: {attr_e}. Falling back to previous star rating ({stars_str}, Source: {stars_source}).")

            else: # This else corresponds to 'if found_map and beatmap_id:'
                logger.warning(f"Could not find matching beatmap ID for difficulty '{target_difficulty_name}' in beatmapset {beatmapset_id}. Cannot fetch PP/Rank/FC or override mods/stars. Using mods from ORDR enum ('{mods_str}') and stars from '{stars_source}' ({stars_str}).")
                rank = 'D' # Default rank if map not found
                is_fc = False # Assume not FC if map not found
                # Keep mods_str, final_mods_enum, stars, stars_str from earlier steps

        except Exception as e:
            logger.error(f"Could not fetch PP/Score/Rank/FC/Stars details: {e}", exc_info=True)
            logger.warning(f"Proceeding with default PP (0.0), Rank (D), FC (False), mods from ORDR enum ('{mods_str}'), and stars from '{stars_source}' ({stars_str}).")
            rank = 'D' # Default rank on error
            is_fc = False # Default FC on error
            # Keep mods_str, final_mods_enum, stars, stars_str from earlier steps

    else: # No API credentials provided or initial API fetch failed
         logger.info(f"No API credentials or data available. Using default PP (0.0), Rank (D), FC (False), mods from ORDR enum ('{mods_str}'), and stars from '{stars_source}' ({stars_str}).")
         rank = 'D' # Default rank
         is_fc = False # Default FC
         # Keep mods_str, final_mods_enum, stars, stars_str from earlier steps

    logger.info(f"Final values - PP: {pp:.2f}, Mods: {mods_str} (Source: {mods_source}, Enum: {final_mods_enum}), Rank: {rank}, FC: {is_fc}, Stars: {stars_str} (Source: {stars_source})")

    # --- Text Drawing ---
    text_color = (255, 255, 255)
    outline_color = (0, 0, 0)
    star_color = (255, 215, 0) # Gold color
    outline_width = 3
    center_x = target_width / 2
    top_margin = 30
    bottom_margin = 30
    side_spacing = 30
    vertical_spacing = 60
    horizontal_spacing_rank_mods = 40 # Spacing between rank image and mods text
    horizontal_spacing_star_num = 5 # Small gap between number and star emoji
    star_emoji_vertical_offset = 5 # Pixels to push emoji down relative to number's top

    # 1. Map Title (Middle Top)
    max_title_width = target_width * 0.9
    truncated_title = truncate_text(song_title, font_map_title, max_title_width)
    _, title_height = get_text_dimensions(font_map_title, truncated_title)
    title_y = top_margin
    draw_centered_text_with_outline(draw, center_x, title_y, truncated_title, font_map_title, text_color, outline_color, outline_width)
    logger.info(f"Drew map title at ({center_x}, {title_y})")

    # 2. Difficulty Name (Under Map Title)
    max_diff_width = target_width * 0.8
    truncated_diff = truncate_text("[" + difficulty + "]", font_difficulty, max_diff_width)
    _, diff_height = get_text_dimensions(font_difficulty, truncated_diff)
    diff_y = title_y + title_height # Place directly under title
    draw_centered_text_with_outline(draw, center_x, diff_y, truncated_diff, font_difficulty, text_color, outline_color, outline_width)
    logger.info(f"Drew difficulty at ({center_x}, {diff_y})")

    # 3. Username (Middle Bottom)
    max_username_width = target_width * 0.9
    truncated_username = truncate_text(username, font_username, max_username_width)
    _, username_height = get_text_dimensions(font_username, truncated_username)
    username_y = target_height - bottom_margin - username_height
    draw_centered_text_with_outline(draw, center_x, username_y, truncated_username, font_username, text_color, outline_color, outline_width)
    logger.info(f"Drew username at ({center_x}, {username_y})")

    # --- Calculate positions for left block (Acc, Mods) ---
    acc_text = accuracy_str
    acc_width, acc_height = get_text_dimensions(font_acc_mods, acc_text)
    mods_display_text = f"+{mods_str}" if mods_str != "NM" else "NM"
    mods_width, mods_height = get_text_dimensions(font_acc_mods, mods_display_text)

    # Calculate total height of the Acc/Mods block
    total_left_block_height = acc_height + vertical_spacing + mods_height

    # Calculate base Y for the Accuracy/Mods block, centered on the reference point
    left_block_base_y = side_text_vertical_center_y - (total_left_block_height / 2)

    # Calculate X end position (right alignment for text)
    left_x_end = avatar_pos_x - side_spacing

    # 4. Accuracy (Top of left block)
    acc_y = left_block_base_y
    draw_right_aligned_text_with_outline(draw, left_x_end, acc_y, acc_text, font_acc_mods, text_color, outline_color, outline_width)
    logger.info(f"Drew accuracy ending at ({left_x_end}, {acc_y})")

    # 5. Mods (Under Accuracy)
    mods_y = acc_y + acc_height + vertical_spacing
    draw_right_aligned_text_with_outline(draw, left_x_end, mods_y, mods_display_text, font_acc_mods, text_color, outline_color, outline_width)
    logger.info(f"Drew mods ending at ({left_x_end}, {mods_y})")

    # --- Calculate and draw Rank Image ---
    # Load Rank Image
    rank_img = None
    rank_image_width = 0
    rank_image_height = 0
    try:
        rank_image_filename = f"ranking-{rank}@2x.png"
        rank_image_path = resource_path(os.path.join("assets", "ranking", rank_image_filename))
        logger.info(f"Attempting to load rank image from: {rank_image_path}")
        logger.info(f"Does rank image path exist? {os.path.exists(rank_image_path)}")
        if os.path.exists(rank_image_path):
            rank_img_loaded = Image.open(rank_image_path).convert("RGBA")
            desired_rank_height = int(size_acc_mods * 1.2) # Match height roughly with mods text
            aspect_ratio = rank_img_loaded.width / rank_img_loaded.height
            rank_image_width = int(desired_rank_height * aspect_ratio)
            rank_image_height = desired_rank_height
            rank_img = rank_img_loaded.resize((rank_image_width, rank_image_height), Image.Resampling.LANCZOS)
            logger.info(f"Loaded and resized rank image: {rank_image_path}")
        else:
            logger.warning(f"Rank image not found: {rank_image_path}")
    except FileNotFoundError:
        logger.warning(f"Rank image file not found for rank '{rank}'.")
    except Exception as e:
        logger.error(f"Could not load or process rank image for rank '{rank}': {e}")

    # Position and Paste Rank Image (if loaded)
    if rank_img:
        # Calculate Y position to vertically align center with Mods text center
        mods_center_y = mods_y + mods_height / 2
        rank_y = mods_center_y - (rank_image_height / 2)

        # Calculate X position to be left of Mods text
        mods_x_start = left_x_end - mods_width
        rank_x = mods_x_start - horizontal_spacing_rank_mods - rank_image_width

        thumbnail.paste(rank_img, (int(rank_x), int(rank_y)), rank_img)
        logger.info(f"Drew rank image at ({int(rank_x)}, {int(rank_y)})")


    # --- Calculate positions for right block (PP, Stars) ---
    pp_text = f"{pp:.0f}PP"
    pp_width, pp_height = get_text_dimensions(font_pp_stars, pp_text)
    star_number_text = stars_str # Use the potentially modded star rating string
    star_emoji_text = "⭐"
    star_number_width, star_number_height = get_text_dimensions(font_pp_stars, star_number_text)
    star_emoji_width, star_emoji_height = get_text_dimensions(font_star_emoji, star_emoji_text)

    total_pp_stars_height = pp_height + vertical_spacing + max(star_number_height, star_emoji_height) # Use max height for spacing
    pp_stars_base_y = side_text_vertical_center_y - (total_pp_stars_height / 2)
    pp_y = pp_stars_base_y
    pp_x = avatar_pos_x + avatar_size + side_spacing

    # 6. PP (Right of Avatar)
    draw_text_with_outline(draw, (pp_x, pp_y), pp_text, font_pp_stars, text_color, outline_color, outline_width)
    logger.info(f"Drew PP at ({pp_x}, {pp_y})")

    # 7. Star Rating (Under PP)
    stars_y = pp_y + pp_height + vertical_spacing
    stars_x = avatar_pos_x + avatar_size + side_spacing # Align with PP

    # Draw star number
    draw_text_with_outline(draw, (stars_x, stars_y), star_number_text, font_pp_stars, text_color, outline_color, outline_width)

    # Calculate position for star emoji
    star_emoji_x = stars_x + star_number_width + horizontal_spacing_star_num
    # --- CHANGE: Align emoji top relative to number top + offset ---
    # star_number_center_y = stars_y + star_number_height / 2
    # star_emoji_y = star_number_center_y - (star_emoji_height / 2) # Old center alignment
    star_emoji_y = stars_y + star_emoji_vertical_offset # Align top + offset
    # --- END CHANGE ---

    # Draw star emoji
    draw_text_with_outline(draw, (star_emoji_x, int(star_emoji_y)), star_emoji_text, font_star_emoji, star_color, outline_color, outline_width)
    logger.info(f"Drew stars number at ({stars_x}, {stars_y}) and emoji at ({star_emoji_x}, {int(star_emoji_y)}) (using {star_emoji_vertical_offset}px vertical offset)")

    # --- Draw FC Text if applicable ---
    if is_fc:
        fc_text = "FC"
        fc_pos_x = 50 # Left margin
        fc_pos_y = 30 # Top margin
        # Use star_color for the fill color
        draw_text_with_outline(draw, (fc_pos_x, fc_pos_y), fc_text, font_fc, star_color, outline_color, outline_width)
        logger.info(f"Drew FC text at ({fc_pos_x}, {fc_pos_y})")

    # Save result
    output_filename = "thumbnail.jpg"
    thumbnail.save(output_filename, quality=95)
    logger.info(f"Thumbnail saved successfully as {output_filename}")


def verify_credentials(client_id, client_secret):
    """Verify if credentials are valid by attempting to get an access token"""
    if not client_id or not client_secret:
        logger.warning("Verification attempt with empty client ID or secret.")
        return False
    try:
        logger.info("Verifying credentials by requesting token.")
        token = get_access_token(client_id, client_secret)
        is_valid = token is not None
        if is_valid:
            logger.info("Credentials verified successfully.")
        else:
            logger.warning("Credential verification failed (token was None).")
        return is_valid
    except Exception as e:
        logger.error(f"Credential verification failed: {e}")
        return False

def save_credentials_to_env(client_id, client_secret):
    """Save credentials to Windows user environment variables"""
    try:
        logger.info("Attempting to save credentials to Windows environment variables.")
        # Open the environment key in registry HKEY_CURRENT_USER\Environment
        key_path = "Environment"
        # Ensure the key exists, create if not (though it usually does)
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)

        # Set the values
        winreg.SetValueEx(key, "OSU_CLIENT_ID_THUMBNAIL", 0, winreg.REG_SZ, client_id)
        logger.debug("Saved OSU_CLIENT_ID_THUMBNAIL.")
        winreg.SetValueEx(key, "OSU_CLIENT_SECRET_THUMBNAIL", 0, winreg.REG_SZ, client_secret)
        logger.debug("Saved OSU_CLIENT_SECRET_THUMBNAIL.")

        # Close the key
        winreg.CloseKey(key)

        # Update current process environment immediately
        os.environ["OSU_CLIENT_ID_THUMBNAIL"] = client_id
        os.environ["OSU_CLIENT_SECRET_THUMBNAIL"] = client_secret
        logger.info("Updated environment variables for current process.")

        # Broadcast WM_SETTINGCHANGE to notify other processes (like Explorer)
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002

        result = ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0, # wParam (not used)
            "Environment", # lParam (string indicating change)
            SMTO_ABORTIFHUNG,
            5000, # timeout in ms
            None # lpdwResult (not used)
        )
        if result == 0:
             last_error = ctypes.get_last_error()
             if last_error != 0:
                 logger.warning(f"SendMessageTimeout failed to broadcast environment change. Error code: {last_error}")
             else:
                 logger.warning("SendMessageTimeout timed out while broadcasting environment change.")
        else:
            logger.info("Successfully broadcast environment variable change notification.")

        return True
    except OSError as e:
        logger.error(f"Failed to save credentials to environment (OS Error): {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Failed to save credentials to environment (Unexpected Error): {e}", exc_info=True)
        return False

class ThumbnailGeneratorGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("osu! Thumbnail Generator")
        self.setMinimumSize(500, 500)
        self.resize(600, 550)

        self.logger = logger
        self.logger.info("Initializing GUI.")

        self.thread = None
        self.worker = None

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # --- Credential Input Section ---
        self.credential_widget = QWidget()
        credential_layout = QVBoxLayout(self.credential_widget)
        credential_layout.setContentsMargins(0, 0, 0, 0)

        self.client_id_input = QLineEdit()
        self.client_id_input.setPlaceholderText("osu! API Client ID")
        self.client_secret_input = QLineEdit()
        self.client_secret_input.setPlaceholderText("osu! API Client Secret")
        self.client_secret_input.setEchoMode(QLineEdit.Password)

        # Load saved credentials from environment variables
        client_id = os.getenv("OSU_CLIENT_ID_THUMBNAIL")
        client_secret = os.getenv("OSU_CLIENT_SECRET_THUMBNAIL")
        if client_id:
            self.client_id_input.setText(client_id)
            self.logger.info("Loaded Client ID from environment.")
        if client_secret:
            # Don't log the secret itself
            self.client_secret_input.setText(client_secret)
            self.logger.info("Loaded Client Secret from environment.")

        credential_layout.addWidget(QLabel("Client ID:"))
        credential_layout.addWidget(self.client_id_input)
        credential_layout.addWidget(QLabel("Client Secret:"))
        credential_layout.addWidget(self.client_secret_input)

        # --- ORDR Input Section ---
        self.ordr_widget = QWidget()
        ordr_layout = QVBoxLayout(self.ordr_widget)
        ordr_layout.setContentsMargins(0, 0, 0, 0)

        self.ordr_url_input = QLineEdit()
        self.ordr_url_input.setPlaceholderText("ORDR URL (e.g. https://link.issou.best/xyz123 or https://ordr.issou.best/watch/abc456)")

        ordr_layout.addWidget(QLabel("ORDR URL:"))
        ordr_layout.addWidget(self.ordr_url_input)

        # --- Action Button (Common to both phases) ---
        self.action_button = QPushButton("Verify Credentials")
        self.action_button.clicked.connect(self.handle_action_button)

        # --- Log Display Area ---
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setLineWrapMode(QTextEdit.NoWrap)

        # --- Add widgets to main layout ---
        layout.addWidget(self.credential_widget)
        layout.addWidget(self.ordr_widget)
        layout.addWidget(self.action_button)
        layout.addWidget(QLabel("Log Output:"))
        layout.addWidget(self.log_display)

        # --- Setup GUI Log Handler ---
        self.log_handler = QtLogHandler()
        self.log_handler.log_signal.connect(self.update_log_display)
        self.logger.addHandler(self.log_handler)
        self.logger.info("GUI Log Handler configured.")

        # --- Initial State ---
        self.ordr_widget.hide()
        self.logger.info("GUI Initialized. Showing credential input.")

        # --- Attempt Auto-Verification ---
        if client_id and client_secret:
            self.logger.info("Found credentials in environment. Attempting auto-verification...")
            if verify_credentials(client_id, client_secret):
                self.logger.info("Auto-verification successful. Switching to ORDR view.")
                self._switch_to_ordr_view()
            else:
                self.logger.warning("Auto-verification failed. Please check saved credentials.")
                # Optionally, show a message box, but logging might be sufficient
                # QMessageBox.warning(self, "Auto-Verification Failed", "Could not verify saved credentials. Please check them.")

    def _switch_to_ordr_view(self):
        """Hides credential input and shows ORDR input."""
        self.credential_widget.hide()
        self.ordr_widget.show()
        self.action_button.setText("Generate Thumbnail")
        self.logger.info("Switched view to ORDR input.")

    def update_log_display(self, log_entry):
        """Appends a log message to the QTextEdit. This runs in the main GUI thread."""
        self.log_display.append(log_entry)

    def handle_action_button(self):
        """Handles clicks for the main button, switching between verify and generate."""
        try:
            if self.ordr_widget.isHidden():
                # --- Verification Phase (Runs synchronously, usually fast) ---
                self.logger.info("Verify button clicked.")
                client_id = self.client_id_input.text().strip()
                client_secret = self.client_secret_input.text().strip()

                if not client_id or not client_secret:
                    self.logger.warning("Verification failed: Client ID or Secret missing.")
                    QMessageBox.warning(self, "Input Required", "Both Client ID and Client Secret are required.")
                    return

                self.action_button.setEnabled(False)
                self.action_button.setText("Verifying...")
                QApplication.processEvents()

                if verify_credentials(client_id, client_secret):
                    self.logger.info("Credentials verified successfully.")
                    if save_credentials_to_env(client_id, client_secret):
                         self.logger.info("Credentials saved to environment.")
                         # QMessageBox.information(self, "Success", "Credentials verified and saved successfully!")
                    else:
                         self.logger.error("Failed to save credentials after verification.")
                         QMessageBox.warning(self, "Warning", "Credentials verified, but failed to save them to environment variables. Check log for details (may require admin rights).")

                    self._switch_to_ordr_view()
                else:
                    self.logger.warning("Verification failed: Invalid credentials.")
                    QMessageBox.warning(self, "Verification Failed", "Invalid API credentials. Please check and try again.")
                    self.action_button.setText("Verify Credentials")

                # Re-enable button
                self.action_button.setEnabled(True)

            else:
                # --- Generation Phase (Run in worker thread) ---
                self.logger.info("Generate Thumbnail button clicked.")
                ordr_url = self.ordr_url_input.text().strip()

                if not ordr_url:
                    self.logger.warning("Generation failed: ORDR URL missing.")
                    QMessageBox.warning(self, "Input Required", "ORDR URL is required.")
                    return

                self.action_button.setEnabled(False)
                self.action_button.setText("Generating...")
                QApplication.processEvents()

                client_id = self.client_id_input.text().strip()
                client_secret = self.client_secret_input.text().strip()

                # --- Setup and start worker thread ---
                self.thread = QThread()
                self.worker = Worker(ordr_url, client_id, client_secret)
                self.worker.moveToThread(self.thread)

                self.worker.finished.connect(self.on_generation_complete)
                self.thread.started.connect(self.worker.run)
                self.worker.finished.connect(self.thread.quit)
                self.worker.finished.connect(self.worker.deleteLater)
                self.thread.finished.connect(self.thread.deleteLater)

                self.logger.info("Starting worker thread...")
                self.thread.start()

        except Exception as e:
            # Catch-all for unexpected errors in the handler logic itself
            self.logger.exception("Unexpected error in GUI action handler:")
            QMessageBox.critical(self, "GUI Error", f"An unexpected error occurred in the application:\n{str(e)}\n\nCheck the log window and thumbnail_generator.log for details.")

            self.action_button.setEnabled(True)
            if self.ordr_widget.isHidden():
                self.action_button.setText("Verify Credentials")
            else:
                self.action_button.setText("Generate Thumbnail")

    def on_generation_complete(self, success, message):
        """Slot executed in the main GUI thread when the worker finishes."""
        self.logger.info(f"Generation complete signal received. Success: {success}")
        self.action_button.setEnabled(True)
        self.action_button.setText("Generate Thumbnail")

        if success:
            QMessageBox.information(self, "Success", message)
        else:
            QMessageBox.critical(self, "Generation Error", message)

        self.thread = None
        self.worker = None
        self.logger.info("GUI state updated after generation completion.")

    def closeEvent(self, event):
        """Ensure thread is stopped if GUI is closed while running."""
        if self.thread is not None and self.thread.isRunning():
            logger.warning("Attempting to stop worker thread on close...")
            self.thread.quit()
            if not self.thread.wait(2000):
                 logger.warning("Worker thread did not stop gracefully, terminating.")
                 self.thread.terminate() # Force terminate if needed
                 self.thread.wait() # Wait for termination
            logger.info("Worker thread stopped.")
        event.accept()


def main():
    # Load .env file if it exists (optional, for local dev)
    load_dotenv()

    # Check if running as admin (needed for registry writing)
    is_admin = False
    try:
        # Check only if platform is Windows
        if os.name == 'nt':
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        else:
            # On non-Windows, assume not admin or check using effective UID if needed
            is_admin = False
            logger.info("Non-Windows platform detected. Skipping admin check.")
    except AttributeError:
        logger.warning("Could not determine admin status (ctypes/shell32 unavailable or not Windows). Assuming not admin.")
        is_admin = False
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        is_admin = False

    # Log admin status *after* basic logging is set up
    logger.info(f"Running as administrator: {is_admin}")
    if not is_admin and os.name == 'nt':
         logger.warning("Application not running as administrator on Windows. Saving credentials to environment variables might fail.")

    # Setup PyQt Application
    app = QApplication(sys.argv)
    # app.setStyle('Fusion')
    window = ThumbnailGeneratorGUI()
    window.show()
    logger.info("Application started.")
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()