import requests
import zipfile
from PIL import Image, ImageDraw, ImageFont, ImageFilter
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
from PyQt5.QtGui import QTextCursor
import logging
from enum import IntFlag
import math # Import math for glow effect calculation
from typing import Union, Tuple, Optional # Import Union, Tuple, Optional

try:
    import rosu_pp_py
    ROSU_PP_AVAILABLE = True
except ImportError:
    ROSU_PP_AVAILABLE = False
    # Log this issue later once logger is fully configured

# --- Setup logging early ---
LOG_FILENAME = 'thumbnail_generator.log' # Define log filename constant
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_file_handler = logging.FileHandler(LOG_FILENAME) # Use constant
log_file_handler.setFormatter(log_formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG) # Set logger level
logger.addHandler(log_file_handler)

# Log rosu-pp status after logger setup
if not ROSU_PP_AVAILABLE:
    logger.warning("rosu-pp-py library not found. PP calculation for FC check will be skipped. Please install it using 'pip install rosu-pp-py'")


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
        logger.info("Worker thread run method started.")
        try:
            logger.info("Calling create_thumbnail function...")
            output_filename = create_thumbnail(self.ordr_url, self.client_id, self.client_secret)
            logger.info(f"create_thumbnail function finished successfully. Result: {output_filename}")
            logger.info("Emitting finished signal (success=True)...")
            self.finished.emit(True, f"Thumbnail created successfully as {output_filename}!")
            logger.info("Finished signal emitted.")
        except (ValueError, RuntimeError) as user_error:
            logger.error(f"Worker thread failed (User Error): {user_error}")
            logger.info("Emitting finished signal (success=False, user error)...")
            self.finished.emit(False, f"{str(user_error)}")
            logger.info("Finished signal emitted.")
        except Exception as e:
            logger.exception("Worker thread failed (Unexpected Error):") # Log full traceback
            logger.info("Emitting finished signal (success=False, unexpected error)...")
            self.finished.emit(False, f"An unexpected error occurred:\n{str(e)}\n\nCheck logs for details.")
            logger.info("Finished signal emitted.")
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

# --- REMOVED map_mods_to_rosu function ---

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


# download_from_mirror
def download_from_mirror(beatmapset_id: int, target_difficulty_name: str) -> Tuple[Optional[Image.Image], Optional[str]]:
    """
    Attempt to download beatmap from mirror API, extract background image,
    and find the content of the .osu file matching target_difficulty_name.
    Returns a tuple: (PIL.Image or None, osu_file_content_string or None)
    """
    extract_folder = f"./temp_beatmap_{beatmapset_id}"
    bg_image = None
    osu_file_content = None
    osu_file_path_found = None # Store the path to the target .osu file

    try:
        logger.info(f"Attempting to download beatmapset {beatmapset_id} from mirror (beatconnect)...")
        # Add timeout to mirror request
        response = requests.get(f"https://beatconnect.io/b/{beatmapset_id}/", stream=True, timeout=30)
        logger.debug(f"Mirror request status code: {response.status_code}")
        response.raise_for_status()
        logger.info(f"Mirror download request successful for {beatmapset_id}.")

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

        # --- Find target .osu file and background filename ---
        bg_filename = None
        target_osu_found = False
        normalized_target_diff = target_difficulty_name.strip().lower() if target_difficulty_name else None

        # Iterate through extracted files to find the correct .osu file first
        for filename in os.listdir(extract_folder):
            if filename.endswith(".osu"):
                osu_file_path = os.path.join(extract_folder, filename)
                logger.debug(f"Checking .osu file: {osu_file_path}")
                try:
                    with open(osu_file_path, 'r', encoding='utf-8') as f:
                        file_content_lines = f.readlines() # Read all lines for parsing

                    # Parse Version (difficulty name)
                    current_diff_name = None
                    for line in file_content_lines:
                        line = line.strip()
                        if line.startswith("Version:"):
                            current_diff_name = line.split(":", 1)[1].strip()
                            break # Found version

                    if current_diff_name:
                        normalized_current_diff = current_diff_name.strip().lower()
                        logger.debug(f"Found difficulty '{current_diff_name}' (Normalized: '{normalized_current_diff}') in {filename}")

                        # Check if this is the target difficulty
                        if not target_osu_found and normalized_target_diff and normalized_current_diff == normalized_target_diff:
                            logger.info(f"Found matching .osu file for difficulty '{target_difficulty_name}': {osu_file_path}")
                            osu_file_path_found = osu_file_path
                            osu_file_content = "".join(file_content_lines) # Store the content
                            target_osu_found = True
                            # Continue parsing this file for background info

                        # Parse background filename from the *current* file (might be the target or another diff)
                        # Only update bg_filename if we haven't found one yet or if this is the target file
                        if bg_filename is None or osu_file_path == osu_file_path_found:
                            in_events_section = False
                            temp_bg_filename = None
                            for line in file_content_lines:
                                line = line.strip()
                                if not in_events_section:
                                    if line == "[Events]":
                                        in_events_section = True
                                    continue
                                else: # Inside [Events]
                                    if line.startswith("//") or not line:
                                        continue
                                    if line.startswith("["): # Stop if we hit the next section
                                        break
                                    # Look for background definition
                                    parts = line.split(',')
                                    if len(parts) >= 3 and (parts[0] == '0' or parts[0].lower() == 'background') and parts[1] == '0':
                                        temp_bg_filename = parts[2].strip('"')
                                        break
                                    elif len(parts) >= 4 and parts[0].lower() == 'background' and parts[2] == '0': # Alt format
                                        temp_bg_filename = parts[3].strip('"')
                                        break
                            if temp_bg_filename:
                                if osu_file_path == osu_file_path_found:
                                    logger.info(f"Found background filename in target .osu: {temp_bg_filename}")
                                    bg_filename = temp_bg_filename # Prioritize BG from target diff
                                elif bg_filename is None:
                                    logger.info(f"Found background filename in non-target .osu ({filename}): {temp_bg_filename} (using as fallback)")
                                    bg_filename = temp_bg_filename

                    else:
                        logger.warning(f"Could not parse 'Version:' from {filename}")

                except Exception as e:
                    logger.error(f"Error parsing .osu file {osu_file_path}: {e}")
                    continue # Try next .osu file

        if not target_osu_found:
            logger.warning(f"Could not find .osu file matching target difficulty '{target_difficulty_name}' in the archive.")
            # osu_file_content remains None

        # --- Load the background image if filename was found ---
        if bg_filename:
            actual_bg_filename = None
            for item in os.listdir(extract_folder):
                item_path = os.path.join(extract_folder, item)
                if os.path.isfile(item_path) and item.lower() == bg_filename.lower():
                    actual_bg_filename = item
                    break
                elif os.path.isdir(item_path):
                    for sub_item in os.listdir(item_path):
                         sub_item_path = os.path.join(item_path, sub_item)
                         if os.path.isfile(sub_item_path) and sub_item.lower() == bg_filename.lower():
                             actual_bg_filename = os.path.join(item, sub_item)
                             break
                    if actual_bg_filename: break

            if actual_bg_filename:
                image_path = os.path.join(extract_folder, actual_bg_filename)
                if os.path.exists(image_path):
                    logger.info(f"Loading background image from: {image_path}")
                    try:
                        bg_image_temp = Image.open(image_path)
                        img_bytes = BytesIO()
                        img_format = Image.registered_extensions().get(os.path.splitext(image_path)[1].lower(), 'PNG')
                        bg_image_temp.save(img_bytes, format=img_format)
                        bg_image_temp.close()
                        img_bytes.seek(0)
                        bg_image = Image.open(img_bytes).convert('RGB')
                        logger.info("Background image loaded successfully from mirror download.")
                    except Exception as e:
                        logger.error(f"Failed to load image file {image_path}: {e}")
                else:
                    logger.warning(f"Background image file specified in .osu not found: {image_path}")
            else:
                 logger.warning(f"Background filename '{bg_filename}' found in .osu, but no matching file found in archive (checked subdirs).")
        else:
            logger.warning("No background filename found in any parsed .osu files.")

    except requests.exceptions.RequestException as e:
        logger.warning(f"Mirror download failed for beatmapset {beatmapset_id}: {e}")
    except zipfile.BadZipFile:
        logger.error(f"Downloaded file for {beatmapset_id} is not a valid zip file.")
    except Exception as e:
        logger.error(f"An unexpected error occurred during mirror download/extraction for {beatmapset_id}: {e}")
    finally:
        time.sleep(0.01) # Yield time before cleanup
        # Clean up temp folder regardless of success/failure
        if os.path.exists(extract_folder):
            logger.info(f"Attempting to clean up temporary folder: {extract_folder}")
            max_retries = 3
            for i in range(max_retries):
                try:
                    shutil.rmtree(extract_folder)
                    logger.info(f"Successfully cleaned up temporary folder: {extract_folder}")
                    break
                except PermissionError as e:
                     logger.warning(f"PermissionError cleaning up {extract_folder} (attempt {i+1}/{max_retries}): {e}. Retrying...")
                     time.sleep(0.5)
                except Exception as e:
                    logger.error(f"Failed to clean up temporary folder {extract_folder} (attempt {i+1}/{max_retries}): {e}")
                    if i < max_retries - 1:
                        time.sleep(0.5)
                    else:
                        logger.error(f"Giving up on cleaning temporary folder {extract_folder} after {max_retries} attempts.")
                        break
    # Return both bg_image (PIL Image or None) and osu_file_content (string or None)
    return bg_image, osu_file_content

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
        resp = requests.get(url, params=params, timeout=15)
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

# --- Font Finding Helper ---
def find_font(paths, size):
    """Finds and loads a font from a list of paths at a given size."""
    # Check provided paths (already resolved by resource_path or absolute)
    for path in paths:
        try:
            # Add logging before loading font
            # logger.debug(f"Attempting to load font from: {path} at size {size}") # Make debug
            # logger.debug(f"Does font path exist? {os.path.exists(path)}")
            if os.path.exists(path):
                # logger.debug(f"Loading font: {path} with size {size}")
                return ImageFont.truetype(path, size)
            else:
                logger.debug(f"Font path not found: {path}")
        except IOError as e:
            logger.warning(f"Could not load font {path} with size {size}: {e}")

    logger.error(f"Could not find any suitable font in paths: {paths} for size {size}. Using default.")
    return ImageFont.load_default(size) # Pass size to default loader

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
def draw_text_with_effect(draw_surface, pos, text, font, fill,
                          effect_type='outline', effect_color=(0,0,0), effect_radius=2):
    """Draws text with an outline or glow effect."""
    x, y = pos

    if effect_type == 'outline':
        # Draw outline by drawing text multiple times with offsets
        for dx in range(-effect_radius, effect_radius + 1):
            for dy in range(-effect_radius, effect_radius + 1):
                if dx == 0 and dy == 0: continue
                # Use Manhattan distance for simple outline
                if abs(dx) + abs(dy) > effect_radius: continue
                draw_surface.text((x + dx, y + dy), text, font=font, fill=effect_color)
    elif effect_type == 'glow':
        # --- Neon Glow Effect ---
        # Draw multiple layers for a brighter, more spread-out glow
        # Outer, slightly thicker layer
        outer_radius = effect_radius + 1 # Slightly larger radius for spread
        num_steps_outer = max(12, outer_radius * 3) # More steps for smoother outer glow
        for i in range(num_steps_outer):
            angle = 2 * math.pi * i / num_steps_outer
            dx = int(round(outer_radius * math.cos(angle)))
            dy = int(round(outer_radius * math.sin(angle)))
            draw_surface.text((x + dx, y + dy), text, font=font, fill=effect_color)

        # Inner, denser layers (draw multiple times)
        num_steps_inner = max(8, effect_radius * 2)
        for r in range(effect_radius, 0, -1): # Draw from radius down to 1
            for i in range(num_steps_inner):
                angle = 2 * math.pi * i / num_steps_inner
                dx = int(round(r * math.cos(angle)))
                dy = int(round(r * math.sin(angle)))
                # Draw the same point multiple times for density (especially for radius 1)
                draw_surface.text((x + dx, y + dy), text, font=font, fill=effect_color)
                if r == 1: # Draw center point extra times for brightness core
                     draw_surface.text((x + dx, y + dy), text, font=font, fill=effect_color)


    # Draw the main text on top
    draw_surface.text(pos, text, font=font, fill=fill)

def draw_centered_text_with_effect(draw_surface, center_x, y_pos, text, font, fill,
                                   effect_type='outline', effect_color=(0,0,0), effect_radius=2):
    """Draws text horizontally centered at center_x with an effect."""
    text_width, _ = get_text_dimensions(font, text)
    x_pos = center_x - (text_width / 2)
    draw_text_with_effect(draw_surface, (x_pos, y_pos), text, font, fill,
                          effect_type, effect_color, effect_radius)

def draw_right_aligned_text_with_effect(draw_surface, right_x, y_pos, text, font, fill,
                                        effect_type='outline', effect_color=(0,0,0), effect_radius=2):
    """Draws text right-aligned ending at right_x with an effect."""
    text_width, _ = get_text_dimensions(font, text)
    x_pos = right_x - text_width
    draw_text_with_effect(draw_surface, (x_pos, y_pos), text, font, fill,
                          effect_type, effect_color, effect_radius)

# --- Font Size Adjustment Helper ---
def adjust_font_size(text, initial_size, font_paths, max_width, min_size=20, step=2):
    """
    Adjusts font size dynamically to fit text within max_width.
    Starts at initial_size and decreases by step until it fits or hits min_size.
    Returns the adjusted font object and its final dimensions (width, height).
    """
    current_size = initial_size
    font = None
    width = float('inf')
    height = 0

    logger.debug(f"Adjusting font for text '{text[:30]}...' Initial: {initial_size}, MaxWidth: {max_width}, Min: {min_size}")

    while current_size >= min_size:
        font = find_font(font_paths, current_size)
        width, height = get_text_dimensions(font, text)
        logger.debug(f"Trying size {current_size}: width={width}")

        if width <= max_width:
            logger.debug(f"Fit found at size {current_size} (Width: {width})")
            return font, width, height # Found a size that fits

        current_size -= step # Decrease size and try again

    # If loop finishes, it means min_size was reached but text still didn't fit
    # Use the font and dimensions from the last iteration (min_size)
    if font is None: # Should only happen if initial_size < min_size
        font = find_font(font_paths, min_size)
        width, height = get_text_dimensions(font, text)

    logger.warning(f"Text '{text[:30]}...' exceeded max width {max_width} even at min font size {min_size}. Using min size. Final width: {width}")
    return font, width, height

# --- CORRECTED: PP Calculation Helper using rosu-pp-py ---
def get_theoretical_pp(osu_file_content: Optional[str], mods_enum: int, accuracy: Optional[float] = None,
                       count300: Optional[int] = None, count100: Optional[int] = None, count50: Optional[int] = None,
                       miss_count: int = 0) -> Optional[float]:
    """
    Calculates theoretical PP for a map with given mods and accuracy/counts using rosu-pp-py.
    Requires the content of the .osu file.
    Provide EITHER accuracy (percentage float, e.g., 99.5) OR hit counts (count300, count100, count50).
    Counts take precedence if provided. miss_count defaults to 0 for FC calculation.
    Returns PP value as float or None if calculation fails or rosu-pp-py is unavailable.
    """
    if not ROSU_PP_AVAILABLE:
        logger.warning("Cannot calculate theoretical PP: rosu-pp-py library not available.")
        return None

    if not osu_file_content:
        logger.warning("Cannot calculate theoretical PP: .osu file content not provided.")
        return None

    calc_method = "Unknown" # For logging
    calculator = None # Initialize calculator variable

    try:
        # Parse the beatmap content
        logger.debug("Parsing .osu file content with rosu-pp-py...")
        beatmap = rosu_pp_py.Beatmap(bytes=osu_file_content.encode('utf-8'))
        logger.debug("Parsing successful.")

        # Removed diagnostic logging

        # Prepare parameters for the Performance constructor
        calc_params = {
            "mods": mods_enum,
            "misses": miss_count # Corrected keyword from n_misses to misses
        }
        calc_method = "Unknown" # For logging

        if count300 is not None and count100 is not None and count50 is not None:
            calc_params["n300"] = count300
            calc_params["n100"] = count100
            calc_params["n50"] = count50
            calc_method = f"Counts (300:{count300}, 100:{count100}, 50:{count50}, Miss:{miss_count})"
        elif accuracy is not None:
            # Clamp accuracy between 0 and 100
            accuracy = max(0.0, min(100.0, accuracy))
            calc_params["acc"] = accuracy
            calc_method = f"Accuracy ({accuracy:.2f}%, Miss:{miss_count})"
        else:
            logger.warning("Cannot calculate theoretical PP: Neither accuracy nor hit counts provided.")
            return None

        logger.info(f"Requesting theoretical PP using rosu-pp-py Performance with {calc_method} and mods integer: {mods_enum}")

        # Create a Performance instance with the parameters
        performance = rosu_pp_py.Performance(**calc_params)

        # Perform the calculation by passing the beatmap to the performance instance
        pp_result = performance.calculate(beatmap)
        pp_value = pp_result.pp

        logger.info(f"Theoretical PP calculated via rosu-pp-py: {pp_value:.2f}")
        return float(pp_value)

    # Removed misplaced except UnicodeDecodeError block that caused F821

    except UnicodeDecodeError as e:
        logger.error(f"Failed to decode .osu file content (likely invalid encoding): {e}")
        return None
    except AttributeError as e:
        # Catch specific AttributeError if Calculate or other methods are missing
        logger.error(f"Error during rosu-pp-py usage (AttributeError): {e}", exc_info=True)
        logger.error("This might indicate an incompatible version of rosu-pp-py or incorrect usage.")
        return None
    except Exception as e:
        # Catch other potential errors during parsing or calculation within rosu-pp-py
        logger.error(f"Error during rosu-pp-py calculation: {e}", exc_info=True)
        return None
# --- END CORRECTED PP Calculation Helper ---


def create_thumbnail(ordr_url, client_id=None, client_secret=None):
    """Create osu! thumbnail from ORDR render link.
       Returns the filename of the created thumbnail.
    """
    logger.info("--- Starting create_thumbnail function ---")
    # No try/except block here - let the caller (Worker.run) handle exceptions
    if not ordr_url:
        logger.error("ORDR URL is required but was not provided.")
        raise ValueError("ORDR URL is required")

    logger.info(f"Generating thumbnail from ORDR URL: {ordr_url}")
    logger.info("Extracting ORDR code...")
    code = extract_ordr_code(ordr_url) # Capture code for filename
    logger.info("Fetching ORDR metadata...")
    meta = fetch_ordr_metadata(code)
    logger.info("ORDR metadata fetched.")
    time.sleep(0.01) # Yield time

    # Extract fields from metadata
    logger.info("Extracting data from ORDR metadata...")
    username = meta.get("replayUsername", "Unknown Player")
    song_title = meta.get("mapTitle", "Unknown Song")
    difficulty = meta.get("replayDifficulty", "Unknown Difficulty") # Keep original case for display
    target_difficulty_name_ordr = difficulty # Store for mirror download lookup
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
    accuracy = float(acc_match.group(1)) if acc_match else 0.0 # Store as float for PP calc
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
    fetched_pp = 0.0 # PP from the actual score
    beatmap_id = None # Specific difficulty ID
    beatmap_max_combo = None # Store max combo for the specific beatmap difficulty
    mods_source = "ORDR Enum" # Track where the final mods came from
    rank = 'D' # Default rank
    is_true_fc = False # Default "True FC" status (0 misses AND 0 slider breaks, verified by PP check or combo check)
    api_mods_list = None # Store mods list from API score if available
    osu_file_content = None # Store content of the .osu file

    # Fetch API data if credentials provided
    if client_id and client_secret:
        logger.info("API credentials provided. Attempting to fetch API data.")
        try:
            logger.info("Getting API access token...")
            token = get_access_token(client_id, client_secret)
            logger.info("Access token obtained.")
            # Fetch beatmapset details (needed for difficulty ID and potentially background)
            logger.info(f"Fetching beatmapset data for set ID: {beatmapset_id}")
            beatmapset_data = make_api_request(token, f"beatmapsets/{beatmapset_id}")
            logger.info("Beatmapset data fetched.")
            # Fetch user details (needed for user ID and avatar)
            logger.info(f"Fetching user data for username: {username}")
            # URL encode username just in case it has special characters
            encoded_username = requests.utils.quote(username)
            user_data = make_api_request(token, f"users/{encoded_username}/osu")
            logger.info("User data fetched.")
            time.sleep(0.01) # Yield time
        except Exception as e:
            logger.warning(f"Failed to fetch initial API data: {e}. Proceeding with limited info.")
            # Continue without API data if possible, features like PP/Avatar/API Mods/Modded Stars might fail
            time.sleep(0.01) # Yield time even on failure
    else:
        logger.info("API credentials not provided. Skipping API data fetch.")

    # Get background image AND .osu file content: Try mirror first, then API for background only
    logger.info(f"Attempting to get background and .osu content via mirror for beatmapset ID: {beatmapset_id}, Difficulty: '{target_difficulty_name_ordr}'")
    bg_image, osu_file_content = download_from_mirror(beatmapset_id, target_difficulty_name_ordr)
    logger.info("Mirror download attempt finished.")
    time.sleep(0.01) # Yield time

    if osu_file_content:
        logger.info(f"Successfully obtained .osu file content for '{target_difficulty_name_ordr}' from mirror.")
    else:
        logger.warning(f"Could not obtain .osu file content for '{target_difficulty_name_ordr}' from mirror. Theoretical PP calculation for FC check will be skipped.")
        # Note: We might still get the beatmap_id later via API to fetch score/rank etc.

    if not bg_image and beatmapset_data: # Use fetched beatmapset_data if available for background fallback
        covers = beatmapset_data.get("covers", {})
        # Prefer cover@2x for higher resolution, fallback to cover
        bg_url = covers.get("cover@2x") or covers.get("cover")
        if bg_url:
            logger.info(f"Using background from osu! API (fallback): {bg_url}")
            try:
                logger.debug("Downloading API background...")
                response = requests.get(bg_url, timeout=15)
                response.raise_for_status()
                logger.debug("API background downloaded. Processing image...")
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
    logger.info("Processing background image for thumbnail base...")
    bg_width, bg_height = bg_image.size
    target_width, target_height = 1920, 1080
    logger.info(f"Background size: {bg_width}x{bg_height}. Target size: {target_width}x{target_height}")

    # Calculate scaling to fill the target aspect ratio, then crop
    logger.debug("Calculating background scaling and cropping...")
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
    logger.info("Applying blur...")
    blur_radius = 5 # Adjust this value for more/less blur
    thumbnail_blurred = thumbnail_base.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    logger.info(f"Applied Gaussian blur with radius {blur_radius} to background.")

    # Apply dimming
    logger.info("Applying dimming...")
    thumbnail_blurred = thumbnail_blurred.convert("RGBA") # Ensure RGBA for compositing
    dim_alpha = 100 # Adjust 0-255 for less/more dimming (e.g., 100 is moderate)
    dim_layer = Image.new('RGBA', thumbnail_blurred.size, (0, 0, 0, 0)) # Transparent layer
    draw_dim = ImageDraw.Draw(dim_layer)
    draw_dim.rectangle([(0,0), thumbnail_blurred.size], fill=(0, 0, 0, dim_alpha)) # Draw semi-transparent black
    thumbnail = Image.alpha_composite(thumbnail_blurred, dim_layer) # Composite dim layer onto blurred image
    logger.info(f"Applied dimming overlay with alpha {dim_alpha}.")

    # Re-initialize Draw object on the dimmed and blurred image
    # Draw needs RGBA for alpha compositing of glow/rank
    # thumbnail is already RGBA after alpha_composite
    logger.debug("Initializing ImageDraw object...")
    draw = ImageDraw.Draw(thumbnail)

    # Add overlay.png overlay FIRST (after blur/dim, before other elements)
    logger.info("Attempting to add overlay.png...")
    try:
        asset_path = resource_path(os.path.join("assets", "overlay.png"))
        # Add logging before loading overlay.png
        logger.info(f"Attempting to load overlay.png from: {asset_path}")
        logger.info(f"Does overlay.png path exist? {os.path.exists(asset_path)}")
        if os.path.exists(asset_path):
            asset_img = Image.open(asset_path).convert("RGBA") # Load with alpha
            # Ensure asset is the correct size (optional, but good practice)
            if asset_img.size != (target_width, target_height):
                logger.warning(f"overlay.png size {asset_img.size} does not match target {target_width}x{target_height}. Resizing.")
                asset_img = asset_img.resize((target_width, target_height), Image.Resampling.LANCZOS)
            # Paste asset covering the whole thumbnail at (0,0) using its alpha
            logger.debug("Pasting overlay image...")
            thumbnail.paste(asset_img, (0, 0), asset_img)
            logger.info(f"Overlayed overlay.png at (0, 0)")
        else:
            logger.warning(f"Asset file not found at {asset_path}. Skipping overlay.")
    except FileNotFoundError:
         logger.warning(f"Asset file 'assets/overlay.png' not found. Skipping overlay.")
    except Exception as asset_e:
        logger.error(f"Could not load or place overlay.png: {asset_e}")
    logger.info("Overlay step finished.")
    time.sleep(0.01) # Yield time


    # Add player avatar using fetched user_data
    logger.info("Attempting to add player avatar...")
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
            logger.info(f"Downloading avatar from: {avatar_url}")
            response = requests.get(avatar_url, timeout=10)
            response.raise_for_status()
            avatar_img = Image.open(BytesIO(response.content)).convert("RGBA") # Keep alpha for masking
            avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)

            # Create circular mask
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.ellipse((0, 0, avatar_size, avatar_size), fill=255)

            # Paste avatar at the calculated position
            logger.debug("Pasting avatar image with mask...")
            thumbnail.paste(avatar_img, avatar_pos, mask) # Use mask for transparency
            logger.info(f"Added player avatar at {avatar_pos} (center)")

        else:
            logger.warning("Could not determine avatar URL. Skipping avatar.")

    except Exception as e:
        logger.error(f"Couldn't load or place player avatar: {e}")
    logger.info("Avatar step finished.")
    time.sleep(0.01) # Yield time


    # --- Font Setup ---
    logger.info("Setting up fonts...")
    # Prioritize Symbola/Segoe UI Symbol for special characters, fallback for others
    font_paths = [
        "C:/Windows/Fonts/seguiemj.ttf", # Segoe UI Emoji (Windows)
    ]
    bold_font_paths = [
        "C:/Windows/Fonts/arialbd.ttf", # Arial Bold first
    ]

    # Define INITIAL font sizes and MINIMUM sizes
    initial_size_map_title = 140
    min_size_map_title = 40
    initial_size_difficulty = 70
    min_size_difficulty = 30
    size_username = 120
    size_pp_stars = 115
    size_acc_mods = 115
    size_star_emoji = 140
    size_fc_text = 300
    size_rank_text = 180

    # Define specific font paths for the star emoji (now U+2605), prioritizing Segoe UI Symbol and Symbola
    star_emoji_font_paths = [
        "C:/Windows/Fonts/seguisym.ttf", # Segoe UI Symbol (often has solid U+2605)
    ]

    # Load fixed-size fonts
    font_username = find_font(font_paths, size_username)
    font_pp_stars = find_font(font_paths, size_pp_stars)
    font_acc_mods = find_font(font_paths, size_acc_mods)
    # Use the dedicated path list for the star emoji font
    font_star_emoji = find_font(star_emoji_font_paths, size_star_emoji)
    font_fc = find_font(font_paths, size_fc_text)
    font_rank = find_font(bold_font_paths, size_rank_text) # Try bold first

    # Log fixed-size font paths
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
    if hasattr(font_rank, 'path'): logger.info(f"Using font: {font_rank.path} (rank)")
    else: logger.warning("Using default PIL font (rank).")


    logger.info("Font setup finished.")

    # --- Find Beatmap ID and Fetch Score/PP/Mods/Rank ---
    logger.info("Attempting to find beatmap ID and fetch score/rank/mods/stars...")
    # Note: mods_str and final_mods_enum are initialized using ORDR data as a fallback.
    if token and user_id and beatmapset_data: # Check if we have API token, user ID, and beatmapset data
        logger.info("API token, user ID, and beatmapset data available. Proceeding with API lookups.")
        try:
            # Use the difficulty name from ORDR metadata for matching
            target_difficulty_name_api = meta.get('replayDifficulty', '')
            logger.info(f"Attempting to find beatmap ID for difficulty: '{target_difficulty_name_api}' in set {beatmapset_id}")

            # Find the beatmap ID for the specific difficulty within the set
            logger.debug("Iterating through beatmapset difficulties...")
            found_map = False
            base_stars_from_api = 0.0 # Store base star rating from API beatmap data
            for beatmap in beatmapset_data.get('beatmaps', []):
                # Compare difficulty names (case-insensitive and strip whitespace)
                if beatmap.get('version', '').strip().lower() == target_difficulty_name_api.strip().lower():
                    beatmap_id = beatmap.get('id')
                    beatmap_max_combo = beatmap.get('max_combo') # Store beatmap max combo
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
                    logger.info(f"Found matching beatmap ID: {beatmap_id} for difficulty '{target_difficulty_name_api}'. API Base Stars: {base_stars_from_api:.2f}, Map Max Combo: {beatmap_max_combo}")
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
                         beatmap_max_combo = closest_map.get('max_combo') # Store beatmap max combo
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
                         logger.info(f"Found closest beatmap by stars: ID {beatmap_id}, Diff '{actual_diff_name}', Base Stars {base_stars_from_api:.2f}, Map Max Combo: {beatmap_max_combo} (Difference: {min_diff:.3f})")
                         difficulty = actual_diff_name # Update difficulty name for display to the matched one
                         found_map = True
                     else:
                         logger.warning(f"Could not find a close match by star rating either (min diff: {min_diff:.3f}).")

            if found_map and beatmap_id:
                # --- Fetch User's Best Score on the Map ---
                logger.info(f"Beatmap ID found ({beatmap_id}). Fetching score details for user {user_id}...")
                score_data = make_api_request(token, f"beatmaps/{beatmap_id}/scores/users/{user_id}")
                score_info = score_data.get("score")
                if score_info:
                    logger.info("Score data found via API.")
                    # Get actual PP achieved in the score
                    logger.debug("Extracting PP, Rank, Stats, Mods from score data...")
                    fetched_pp_val = score_info.get("pp")
                    if fetched_pp_val is not None:
                        fetched_pp = float(fetched_pp_val)
                        logger.info(f"Fetched PP from user's best score: {fetched_pp:.2f}")
                    else:
                        fetched_pp = 0.0 # Handle null PP (e.g., loved maps)
                        logger.info("API returned null PP for the score, setting to 0.")

                    # Get Rank
                    fetched_rank = score_info.get("rank")
                    if fetched_rank:
                        rank = fetched_rank
                        logger.info(f"Fetched Rank from user's best score: {rank}")
                    else:
                        rank = 'D'
                        logger.warning("Rank not found in API score data, defaulting to D.")

                    # Get miss count, hit counts, and score max combo
                    stats = score_info.get("statistics", {})
                    miss_count = stats.get("count_miss", -1) # Keep -1 if not found? Or default 0? Let's use -1 to indicate unknown.
                    # --- FIX START: Complete the following lines ---
                    count_300 = stats.get("count_300", 0)
                    count_100 = stats.get("count_100", 0)
                    count_50 = stats.get("count_50", 0)
                    score_max_combo = score_info.get("max_combo", 0)
                    # --- FIX END ---
                    logger.info(f"Score Stats - Miss: {miss_count}, 300: {count_300}, 100: {count_100}, 50: {count_50}, Combo: {score_max_combo}")

                    # Mod Override Logic (BEFORE True FC check, as mods affect PP calc)
                    api_mods_list = score_info.get("mods", []) # Store this list
                    if api_mods_list:
                        api_mods_str = "".join(api_mods_list)
                        logger.info(f"Found mods from API score: {api_mods_str}. Overriding mods from ORDR enum.")
                        mods_str = api_mods_str
                        mods_source = "osu! API Score"
                        # Convert API mod list back to enum for star calculation AND PP calculation
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

                    # --- Determine "True FC" status ---
                    # Requires miss_count, hit counts, final_mods_enum, osu_file_content, score_max_combo, beatmap_max_combo
                    if miss_count == 0:
                        logger.info("Miss count is 0. Prioritizing combo check for FC status.")
                        # 1. Check combo first
                        if beatmap_max_combo is not None and score_max_combo >= beatmap_max_combo:
                            is_true_fc = True
                            logger.info(f"Determined True FC: Yes (Combo Check: Score Combo={score_max_combo}, Map Combo={beatmap_max_combo})")
                        else:
                            # Combo check failed or not possible, proceed to PP check
                            reason = []
                            if beatmap_max_combo is None: reason.append("Map Max Combo unknown")
                            elif score_max_combo < beatmap_max_combo: reason.append(f"Score Combo={score_max_combo} < Map Combo={beatmap_max_combo}")
                            logger.info(f"Combo check inconclusive ({', '.join(reason)}). Attempting PP comparison for FC check using rosu-pp-py.")

                            # 2. Try calculating theoretical PP for an FC with this accuracy/mods using exact hit counts
                            logger.info("Calling get_theoretical_pp for FC check...")
                            theoretical_pp = get_theoretical_pp(
                                osu_file_content=osu_file_content, # Use the fetched content
                                mods_enum=final_mods_enum, # Use the final mods
                                count300=count_300,
                                count100=count_100,
                                count50=count_50,
                                miss_count=0 # Explicitly set 0 misses for FC calc
                            )

                            if theoretical_pp is not None:
                                # Compare fetched PP with theoretical PP (allow 15% relative difference)
                                pp_relative_tolerance = 0.05 # 5%
                                difference = abs(fetched_pp - theoretical_pp)
                                # Handle theoretical_pp being zero or very small
                                if theoretical_pp > 1e-6: # Avoid division by zero/large relative error for tiny PP values
                                    is_match = (difference / theoretical_pp) <= pp_relative_tolerance
                                else:
                                    # If theoretical PP is essentially zero, fetched PP must also be essentially zero
                                    is_match = difference <= 1e-6 # Use a very small absolute tolerance for zero case

                                if is_match:
                                    is_true_fc = True
                                    logger.info(f"Determined True FC: Yes (PP Match: Fetched={fetched_pp:.2f}, Theoretical={theoretical_pp:.2f}, Relative Difference <= {pp_relative_tolerance*100:.0f}%)")
                                else:
                                    is_true_fc = False
                                    relative_diff_str = f"{(difference / theoretical_pp * 100):.1f}%" if theoretical_pp > 1e-6 else "N/A (Theoretical PP is zero)"
                                    logger.info(f"Determined True FC: No (PP Mismatch: Fetched={fetched_pp:.2f}, Theoretical={theoretical_pp:.2f}, Relative Difference={relative_diff_str} > {pp_relative_tolerance*100:.0f}%) - Likely slider break.")
                            else:
                                # PP calculation failed or skipped, and combo check already failed/inconclusive
                                is_true_fc = False
                                logger.warning("Theoretical PP calculation failed or skipped, and combo check was inconclusive. Determined True FC: No.")

                    elif miss_count > 0:
                        # Not an FC if misses > 0
                        is_true_fc = False
                        logger.info(f"Determined True FC: No (Miss Count={miss_count})")
                    else: # miss_count is -1 (unknown)
                        is_true_fc = False
                        logger.info(f"Determined True FC: No (Miss Count unknown from API)")


                    # Log the API 'perfect' flag for comparison/debugging
                    api_perfect_flag = score_info.get("perfect", False)
                    logger.info(f"API 'perfect' flag value (for info only): {api_perfect_flag}")

                else: # This else corresponds to 'if score_info:'
                    logger.warning(f"Score not found via API for user {user_id} on beatmap {beatmap_id}. PP set to 0. Rank set to D. True FC status unknown. Using mods from ORDR enum ('{mods_str}', enum {final_mods_enum}).")
                    fetched_pp = 0.0 # Reset PP if score not found
                    rank = 'D'
                    is_true_fc = False # Assume not True FC if score not found
                    # Keep mods_str and final_mods_enum from ORDR

                # --- Fetch Modded Star Rating ---
                # Requires beatmap_id and the final_mods_enum determined above
                logger.info("Attempting to fetch modded star rating...")
                try:
                    logger.info(f"Fetching difficulty attributes for beatmap {beatmap_id} with mods enum: {final_mods_enum}")
                    attributes_payload = {"mods": final_mods_enum}
                    attributes_data = make_api_request(token, f"beatmaps/{beatmap_id}/attributes", method='POST', payload=attributes_payload)
                    logger.debug("Difficulty attributes response received.")
                    modded_stars_value = attributes_data.get("attributes", {}).get("star_rating")
                    if modded_stars_value is not None:
                        logger.debug(f"Modded stars found in response: {modded_stars_value}")
                        stars = float(modded_stars_value)
                        stars_str = f"{stars:.2f}"
                        stars_source = "osu! API Attributes (Modded)"
                        logger.info(f"Successfully fetched modded star rating: {stars_str}")
                    else:
                        logger.warning(f"Modded star rating not found in attributes response. Falling back to previous value ({stars_str}, Source: {stars_source}).")
                except Exception as attr_e:
                    logger.error(f"Failed to fetch modded difficulty attributes: {attr_e}. Falling back to previous star rating ({stars_str}, Source: {stars_source}).")

            else: # This else corresponds to 'if found_map and beatmap_id:'
                logger.warning(f"Could not find matching beatmap ID for difficulty '{target_difficulty_name_api}' in beatmapset {beatmapset_id}. Cannot fetch PP/Rank/FC or override mods/stars. Using mods from ORDR enum ('{mods_str}') and stars from '{stars_source}' ({stars_str}).")
                rank = 'D' # Default rank if map not found
                is_true_fc = False # Assume not True FC if map not found
                # Keep mods_str, final_mods_enum, stars, stars_str from earlier steps

        except Exception as e:
            logger.error(f"Could not fetch PP/Score/Rank/FC/Stars details: {e}", exc_info=True)
            logger.warning(f"Proceeding with default PP (0.0), Rank (D), True FC (False), mods from ORDR enum ('{mods_str}'), and stars from '{stars_source}' ({stars_str}).")
            fetched_pp = 0.0 # Reset PP on error
            rank = 'D' # Default rank on error
            is_true_fc = False # Default True FC on error
            # Keep mods_str, final_mods_enum, stars, stars_str from earlier steps

    else: # No API credentials provided or initial API fetch failed
         logger.info(f"No API credentials or data available. Using default PP (0.0), Rank (D), True FC (False), mods from ORDR enum ('{mods_str}'), and stars from '{stars_source}' ({stars_str}).")
         fetched_pp = 0.0 # Default PP
         rank = 'D' # Default rank
         is_true_fc = False # Default True FC
         # Keep mods_str, final_mods_enum, stars, stars_str from earlier steps
    logger.info("Finished fetching/determining score/rank/mods/stars.")
    time.sleep(0.01) # Yield time

    logger.info(f"Final values - PP: {fetched_pp:.2f}, Mods: {mods_str} (Source: {mods_source}, Enum: {final_mods_enum}), Rank: {rank}, True FC (0 Miss + PP/Combo Check): {is_true_fc}, Stars: {stars_str} (Source: {stars_source})")

    # --- Text Drawing ---
    logger.info("Starting text drawing phase...")
    text_color = (255, 255, 255)
    outline_color = (0, 0, 0) # Used for non-rank text outline
    star_color = (255, 215, 0) # Gold color for star emoji
    outline_width = 3
    center_x = target_width / 2
    top_margin = 30
    bottom_margin = 30
    side_spacing = 30
    vertical_spacing = 60 # Spacing between Acc/Mods and PP/Stars
    vertical_spacing_title_diff = 10 # Smaller spacing between title and difficulty
    horizontal_spacing_rank_mods = 40 # Spacing between rank text and mods text
    horizontal_spacing_star_num = 5 # Small gap between number and star emoji
    rank_glow_radius = 6 # Increased glow radius for Rank neon effect
    fc_glow_radius = 7 # Increased glow radius for FC neon effect

    # Using RGBA for potential transparency in glow
    RANK_BASE_COLORS = {
        'D': (255, 0, 0, 255),      # Red
        'C': (180, 0, 255, 255),    # Purple
        'B': (0, 120, 255, 255),   # Blue
        'A': (0, 255, 0, 255),      # Green
        'S': (255, 215, 0, 255),    # Gold
        'X': (255, 215, 0, 255),    # Gold
    }
    RANK_GLOW_COLORS = {
        'D': (255, 100, 100, 180),  # Light Red
        'C': (220, 100, 255, 180),  # Light Purple
        'B': (100, 180, 255, 180),  # Light Blue
        'A': (100, 255, 100, 180),  # Light Green
        'S': (255, 235, 100, 180),  # Light Gold
        'X': (255, 235, 100, 180),  # Light Gold
    }
    SILVER_BASE_COLOR = (192, 192, 192, 255) # Silver
    SILVER_GLOW_COLOR = (230, 230, 230, 180) # Light Silver
    FC_GLOW_COLOR = (255, 255, 150, 180) # Bright Yellow Glow for FC

    # Define maximum widths for dynamic text areas
    max_title_width = target_width / 2
    max_diff_width = target_width / 2
    max_username_width = target_width * 0.90 # Keep username width large for now

    # 1. Map Title (Middle Top) - Adjust Font Size
    logger.info("Drawing map title...")
    font_map_title, title_width, title_height = adjust_font_size(
        song_title,
        initial_size_map_title,
        font_paths,
        max_title_width, # Use updated max width
        min_size=min_size_map_title
    )
    title_y = top_margin
    # Use standard outline for title/difficulty/username
    draw_centered_text_with_effect(draw, center_x, title_y, song_title, font_map_title, text_color,
                                   effect_type='outline', effect_color=outline_color, effect_radius=outline_width)
    logger.info(f"Drew map title at ({center_x}, {title_y}) with adjusted font size {font_map_title.size} (Height: {title_height}, MaxWidth: {max_title_width})")

    # 2. Difficulty Name (Under Map Title) - Adjust Font Size
    logger.info("Drawing difficulty name...")
    # Use the 'difficulty' variable which might have been updated by the star rating fallback match
    difficulty_text = "[" + difficulty + "]"
    font_difficulty, diff_width, diff_height = adjust_font_size(
        difficulty_text,
        initial_size_difficulty,
        font_paths,
        max_diff_width, # Use updated max width
        min_size=min_size_difficulty
    )
    # Position difficulty based on the *actual* height of the adjusted title font
    diff_y = title_y + title_height + vertical_spacing_title_diff
    draw_centered_text_with_effect(draw, center_x, diff_y, difficulty_text, font_difficulty, text_color,
                                   effect_type='outline', effect_color=outline_color, effect_radius=outline_width)
    logger.info(f"Drew difficulty at ({center_x}, {diff_y}) with adjusted font size {font_difficulty.size} (Height: {diff_height}, MaxWidth: {max_diff_width})")

    # 3. Username (Middle Bottom) - Dynamic Size
    logger.info("Drawing username...")
    font_username, username_width, username_height = adjust_font_size(
        username,
        size_username, # Use the original fixed size as the initial/max size
        font_paths,
        max_username_width,
        min_size=40 # Set a minimum size for username
    )
    username_y = target_height - bottom_margin - username_height
    draw_centered_text_with_effect(draw, center_x, username_y, username, font_username, text_color,
                                   effect_type='outline', effect_color=outline_color, effect_radius=outline_width)
    logger.info(f"Drew username at ({center_x}, {username_y}) with adjusted font size {font_username.size} (Height: {username_height})")


    # --- Calculate positions for left block (Acc, Mods, Rank) ---
    acc_text = accuracy_str
    acc_width, acc_height = get_text_dimensions(font_acc_mods, acc_text)
    mods_display_text = f"+{mods_str}" if mods_str != "NM" else "NM"
    mods_width, mods_height = get_text_dimensions(font_acc_mods, mods_display_text)

    rank_text = rank[0] # Get the base letter (D, C, B, A, S, X)
    is_hidden_rank = rank.endswith('H') # Check if it's SH or XH
    # Determine if silver rank should be used based on API rank OR if rank is S/X and HD mod is present
    use_silver = is_hidden_rank or (rank_text in ['S', 'X'] and Mods.Hidden in final_mods_enum)

    if use_silver:
        rank_base_color = SILVER_BASE_COLOR
        rank_glow_color = SILVER_GLOW_COLOR
        logger.info(f"Using Silver color scheme for rank '{rank}' (Hidden mod detected or SH/XH rank).")
    else:
        rank_base_color = RANK_BASE_COLORS.get(rank_text, (255, 255, 255, 255)) # Default white
        rank_glow_color = RANK_GLOW_COLORS.get(rank_text, (200, 200, 200, 180)) # Default light gray glow
        logger.info(f"Using {rank_text} color scheme for rank '{rank}'.")

    rank_width, rank_height = get_text_dimensions(font_rank, rank_text)

    # Calculate total height of the Acc/Mods block (Rank is drawn separately)
    total_left_block_height = acc_height + vertical_spacing + mods_height

    # Calculate base Y for the Accuracy/Mods block, centered on the reference point
    left_block_base_y = side_text_vertical_center_y - (total_left_block_height / 2)

    # Calculate X end position (right alignment for text)
    left_x_end = avatar_pos_x - side_spacing

    # 4. Accuracy (Top of left block)
    logger.info("Drawing accuracy...")
    acc_y = left_block_base_y
    draw_right_aligned_text_with_effect(draw, left_x_end, acc_y, acc_text, font_acc_mods, text_color,
                                        effect_type='outline', effect_color=outline_color, effect_radius=outline_width)
    logger.info(f"Drew accuracy ending at ({left_x_end}, {acc_y})")

    # 5. Mods (Under Accuracy)
    logger.info("Drawing mods...")
    mods_y = acc_y + acc_height + vertical_spacing
    draw_right_aligned_text_with_effect(draw, left_x_end, mods_y, mods_display_text, font_acc_mods, text_color,
                                        effect_type='outline', effect_color=outline_color, effect_radius=outline_width)
    logger.info(f"Drew mods ending at ({left_x_end}, {mods_y})")

    # 6. Rank (Left of Mods)
    logger.info("Drawing rank...")
    # Calculate Y position to align the bottom of rank text with the bottom of mods text
    rank_y = mods_y + mods_height - rank_height # Align bottoms

    # Calculate X position to be left of Mods text
    mods_x_start = left_x_end - mods_width
    rank_x = mods_x_start - horizontal_spacing_rank_mods - rank_width

    # Draw the rank text with neon glow
    draw_text_with_effect(draw, (int(rank_x), int(rank_y)), rank_text, font_rank,
                          fill=rank_base_color,
                          effect_type='glow',
                          effect_color=rank_glow_color,
                          effect_radius=rank_glow_radius) # Uses updated rank_glow_radius
    logger.info(f"Drew rank text '{rank_text}' with neon glow at ({int(rank_x)}, {int(rank_y)}) (Bottom aligned with mods)")

    # --- Calculate positions for right block (PP, Stars) ---
    pp_text = f"{fetched_pp:.0f}PP" # Display the fetched PP
    pp_width, pp_height = get_text_dimensions(font_pp_stars, pp_text)
    star_number_text = stars_str # Use the potentially modded star rating string
    star_emoji_text = "★" # Use BLACK STAR (U+2605) instead of emoji star
    star_number_width, star_number_height = get_text_dimensions(font_pp_stars, star_number_text)
    star_emoji_width, star_emoji_height = get_text_dimensions(font_star_emoji, star_emoji_text)

    total_pp_stars_height = pp_height + vertical_spacing + max(star_number_height, star_emoji_height) # Use max height for spacing
    pp_stars_base_y = side_text_vertical_center_y - (total_pp_stars_height / 2)
    pp_y = pp_stars_base_y
    pp_x = avatar_pos_x + avatar_size + side_spacing

    # 7. PP (Right of Avatar)
    logger.info("Drawing PP...")
    draw_text_with_effect(draw, (pp_x, pp_y), pp_text, font_pp_stars, text_color,
                          effect_type='outline', effect_color=outline_color, effect_radius=outline_width)
    logger.info(f"Drew PP at ({pp_x}, {pp_y})")

    # 8. Star Rating (Under PP)
    logger.info("Drawing star rating...")
    stars_y = pp_y + pp_height + vertical_spacing
    stars_x = avatar_pos_x + avatar_size + side_spacing # Align with PP

    # Draw star number
    draw_text_with_effect(draw, (stars_x, stars_y), star_number_text, font_pp_stars, text_color,
                          effect_type='outline', effect_color=outline_color, effect_radius=outline_width)

    # Calculate position for star emoji
    star_emoji_x = stars_x + star_number_width + horizontal_spacing_star_num
    # Align top of emoji with top of number, then adjust upwards slightly
    star_emoji_vertical_adjust = -int(star_number_height * 0.8)
    star_emoji_y = stars_y + star_emoji_vertical_adjust

    # Draw star emoji (using outline for consistency with other text)
    draw_text_with_effect(draw, (star_emoji_x, int(star_emoji_y)), star_emoji_text, font_star_emoji, star_color,
                          effect_type='outline', effect_color=outline_color, effect_radius=outline_width)
    logger.info(f"Drew stars number at ({stars_x}, {stars_y}) and emoji at ({star_emoji_x}, {int(star_emoji_y)}) (aligned top + {star_emoji_vertical_adjust}px adjustment)")

    # --- Draw FC Text if applicable (based on 0 misses AND PP/combo check) ---
    logger.info("Checking if FC text should be drawn...")
    if is_true_fc: # Use the result of the PP comparison or combo fallback
        logger.info("Drawing FC text...")
        fc_text = "FC"
        fc_pos_x = 50 # Left margin
        fc_pos_y = 30 # Top margin
        # Use star_color for fill, FC_GLOW_COLOR for neon effect
        draw_text_with_effect(draw, (fc_pos_x, fc_pos_y), fc_text, font_fc,
                              fill=star_color, # Base color (Gold)
                              effect_type='glow',
                              effect_color=FC_GLOW_COLOR, # Glow color (Bright Yellow)
                              effect_radius=fc_glow_radius) # Use defined FC glow radius
        logger.info(f"Drew FC text with neon glow at ({fc_pos_x}, {fc_pos_y}) because True FC conditions were met.")
    else:
        logger.info("Skipping FC text because True FC conditions were not met (misses > 0 or PP/combo check failed).")


    logger.info("Text drawing phase finished.")
    time.sleep(0.01) # Yield time

    # --- Save result ---
    logger.info("Saving final thumbnail image...")
    output_dir = "thumbnails"
    # Create the directory if it doesn't exist
    try:
        logger.debug(f"Ensuring output directory exists: {output_dir}")
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Output directory exists: {output_dir}")
    except OSError as e:
        logger.error(f"Could not create output directory '{output_dir}': {e}")
        raise RuntimeError(f"Failed to create thumbnail directory: {e}") # Re-raise as runtime error

    # Construct the full output path
    base_filename = f"thumbnail_{code}.jpg"
    output_filepath = os.path.join(output_dir, base_filename)
    logger.debug(f"Output file path: {output_filepath}")

    # Convert back to RGB before saving as JPG
    logger.debug("Converting image to RGB for saving...")
    thumbnail = thumbnail.convert("RGB")
    logger.debug("Saving image...")
    thumbnail.save(output_filepath, quality=95)
    logger.info(f"Thumbnail saved successfully as {output_filepath}")

    logger.info("--- Finished create_thumbnail function ---")
    return output_filepath # Return the full path


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
        self.log_display.setLineWrapMode(QTextEdit.WidgetWidth) # Wrap lines at widget width

        # --- Add widgets to main layout ---
        layout.addWidget(self.credential_widget)
        layout.addWidget(self.ordr_widget)
        layout.addWidget(self.action_button)
        layout.addWidget(QLabel("Log Output:"))
        layout.addWidget(self.log_display)

        # --- Setup GUI Log Handler ---
        self.log_handler = QtLogHandler()
        self.log_handler.setLevel(logging.INFO) # Only show INFO and above in GUI
        self.log_handler.log_signal.connect(self.update_log_display)
        self.logger.addHandler(self.log_handler)
        self.logger.info("GUI Log Handler configured (Level: INFO).")

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
        """Appends a log message to the QTextEdit and ensures it's visible. This runs in the main GUI thread."""
        self.log_display.append(log_entry)
        # Ensure the latest log entry is visible by moving the cursor and scrollbar
        cursor = self.log_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_display.setTextCursor(cursor)
        # Optional: Force scrollbar to bottom if needed, append usually handles this
        # self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())


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
                QApplication.processEvents() # Update button text

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
                    self.action_button.setText("Verify Credentials") # Reset button text on failure

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

                # Disable button and update text *before* starting thread
                self.action_button.setEnabled(False)
                self.action_button.setText("Generating...")
                QApplication.processEvents() # Ensure button updates before potential blocking

                client_id = self.client_id_input.text().strip()
                client_secret = self.client_secret_input.text().strip()

                # --- Setup and start worker thread ---
                # Ensure previous thread is cleaned up if user clicks rapidly (shouldn't happen with button disabled)
                if self.thread is not None and self.thread.isRunning():
                    logger.warning("Previous worker thread still running? Attempting to wait...")
                    self.thread.quit()
                    self.thread.wait(1000) # Wait a bit

                self.thread = QThread()
                self.worker = Worker(ordr_url, client_id, client_secret)
                self.worker.moveToThread(self.thread)

                # Connections
                self.worker.finished.connect(self.on_generation_complete)
                self.thread.started.connect(self.worker.run)
                # Cleanup connections
                self.worker.finished.connect(self.thread.quit)
                self.worker.finished.connect(self.worker.deleteLater)
                self.thread.finished.connect(self.thread.deleteLater)
                # Ensure thread object is cleared after it finishes
                self.thread.finished.connect(self._clear_thread_ref)

                self.logger.info("Starting worker thread...")
                self.thread.start()
                self.logger.info("Worker thread start command issued.")

        except Exception as e:
            # Catch-all for unexpected errors in the handler logic itself
            self.logger.exception("Unexpected error in GUI action handler:")
            QMessageBox.critical(self, "GUI Error", f"An unexpected error occurred in the application:\n{str(e)}\n\nCheck the log window and thumbnail_generator.log for details.")

            # Reset button state on error
            self.action_button.setEnabled(True)
            if self.ordr_widget.isHidden():
                self.action_button.setText("Verify Credentials")
            else:
                self.action_button.setText("Generate Thumbnail")
            # Clear thread refs if an error occurred during setup
            self.thread = None
            self.worker = None

    def _clear_thread_ref(self):
        """Clear thread and worker references after thread finishes."""
        logger.debug("Clearing thread and worker references.")
        self.thread = None
        self.worker = None

    def on_generation_complete(self, success, message):
        """Slot executed in the main GUI thread when the worker finishes."""
        self.logger.info("--- on_generation_complete slot entered ---")
        self.logger.info(f"Generation complete signal received. Success: {success}, Message: {message}")
        self.action_button.setEnabled(True) # Re-enable button
        self.action_button.setText("Generate Thumbnail") # Reset button text

        if success:
            QMessageBox.information(self, "Success", message)
        else:
            QMessageBox.critical(self, "Generation Error", message)

        # Note: Thread/worker cleanup is handled by connections to finished signals
        self.logger.info("GUI state updated after generation completion.")

    def closeEvent(self, event):
        """Ensure thread is stopped and log file is deleted if GUI is closed."""
        logger.info("--- closeEvent triggered ---")
        # Stop worker thread first
        if self.thread is not None and self.thread.isRunning():
            logger.warning("Worker thread is running. Attempting to stop worker thread on close...")
            # Disconnect signals to avoid issues during shutdown? Maybe not necessary.
            logger.debug("Disconnecting worker/thread signals...")
            try:
                self.worker.finished.disconnect(self.on_generation_complete)
                self.thread.started.disconnect(self.worker.run)
                self.worker.finished.disconnect(self.thread.quit)
                self.worker.finished.disconnect(self.worker.deleteLater)
                self.thread.finished.disconnect(self.thread.deleteLater)
                self.thread.finished.disconnect(self._clear_thread_ref)
            except TypeError: # Signals might already be disconnected
                pass
            except Exception as e:
                logger.error(f"Error disconnecting signals during close: {e}")
            logger.debug("Signals disconnected.")

            logger.info("Requesting thread quit...")
            self.thread.quit()
            logger.info("Waiting for thread to finish (max 2 seconds)...")
            if not self.thread.wait(2000): # Wait up to 2 seconds
                 logger.warning("Worker thread did not stop gracefully after quit(). Terminating.")
                 self.thread.terminate() # Force terminate if needed
                 logger.info("Waiting for thread termination...")
                 self.thread.wait() # Wait for termination
                 logger.info("Thread terminated.")
            else:
                logger.info("Worker thread stopped gracefully.")
        # Note: The 'else' for the 'if self.thread...' check was missing in the provided file,
        # so this 'try' block directly follows the 'if' block's scope.
        # If the thread wasn't running, this 'try' block is reached immediately after the 'if' condition check.

        logger.info("Attempting to close log handler and delete log file...")
        try:
            # Remove the handler to release the file lock
            logger.info(f"Removing log handler for {LOG_FILENAME}")
            logger.removeHandler(log_file_handler)
            log_file_handler.close() # Explicitly close the handler
            logger.info(f"Attempting to delete log file: {LOG_FILENAME}")
            if os.path.exists(LOG_FILENAME):
                os.remove(LOG_FILENAME)
                # Cannot log success here as handler is removed
                print(f"Successfully deleted log file: {LOG_FILENAME}") # Use print as fallback
            else:
                # Cannot log warning here
                print(f"Log file not found, skipping deletion: {LOG_FILENAME}")
        except PermissionError as e:
             # Cannot log error here
             print(f"Permission error deleting log file {LOG_FILENAME}: {e}")
        except Exception as e:
            # Cannot log error here
            print(f"Error deleting log file {LOG_FILENAME}: {e}")

        event.accept() # Accept the close event


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
    # app.setStyle('Fusion') # Optional: Set style
    window = ThumbnailGeneratorGUI()
    window.show()
    logger.info("Application started.")
    exit_code = app.exec_()
    # Ensure logging is shut down cleanly *before* sys.exit
    logging.shutdown()
    sys.exit(exit_code)

if __name__ == "__main__":
    main()