import requests
import zipfile
from osupyparser import OsuFile
from PIL import Image, ImageDraw, ImageFont
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
    
    response = requests.post(
        "https://osu.ppy.sh/oauth/token",
        headers=headers,
        data=data
    )
    
    if response.status_code == 200:
        return response.json().get("access_token")
    else:
        raise Exception(f"Authentication failed: {response.text}")

# API request helper
def make_api_request(token, endpoint):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}"
    }
    response = requests.get(f"https://osu.ppy.sh/api/v2/{endpoint}", headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"API request failed: {response.text}")

def download_from_mirror(beatmapset_id):
    """Attempt to download beatmap from mirror API and extract background image"""
    extract_folder = f"./temp_beatmap_{beatmapset_id}"
    try:
        print(f"Attempting to download beatmapset {beatmapset_id} from mirror...")
        response = requests.get(f"https://beatconnect.io/b/{beatmapset_id}/", stream=True)
        response.raise_for_status()
        
        osz_path = f"{beatmapset_id}.osz"
        with open(osz_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded {osz_path}")

        # Extract .osz
        try:
            with zipfile.ZipFile(osz_path, 'r') as zip_ref:
                zip_ref.extractall(extract_folder)
            print(f"Extracted to {extract_folder}")
        finally:
            if os.path.exists(osz_path):
                os.remove(osz_path)

        # Find and parse .osu file
        bg_filename = None
        for filename in os.listdir(extract_folder):
            if filename.endswith(".osu"):
                try:
                    with open(os.path.join(extract_folder, filename), 'r', encoding='utf-8') as f:
                        in_events_section = False
                        for line in f:
                            line = line.strip()
                            if line == "[Events]":
                                in_events_section = True
                                continue
                            if in_events_section:
                                if line.startswith("//") or not line:
                                    continue
                                if line.startswith("["):
                                    break
                                parts = line.split(',')
                                if len(parts) >= 3 and parts[0] == '0' and parts[1] == '0':
                                    bg_filename = parts[2].strip('"')
                                    print(f"Found background filename: {bg_filename}")
                                    break
                except Exception as e:
                    print(f"Error parsing .osu file: {e}")
                    continue

        # Load the background image if found
        bg_image = None
        if bg_filename:
            image_path = os.path.join(extract_folder, bg_filename)
            if os.path.exists(image_path):
                bg_image = Image.open(image_path)
                img_bytes = BytesIO()
                bg_image.save(img_bytes, format=bg_image.format)
                bg_image.close()
                img_bytes.seek(0)
                bg_image = Image.open(img_bytes)
                
    except Exception as e:
        print(f"Mirror download failed: {e}")
    finally:
        if os.path.exists(extract_folder):
            max_retries = 3
            for i in range(max_retries):
                try:
                    shutil.rmtree(extract_folder)
                    print(f"Cleaned up temporary folder: {extract_folder}")
                    break
                except Exception as e:
                    if i == max_retries - 1:
                        print(f"Failed to clean up temporary folder after {max_retries} attempts: {e}")
                    else:
                        time.sleep(0.5)
    return bg_image

def extract_ordr_code(ordr_url: str) -> str:
    """Pulls the 6- or 7-character code from URLs like https://link.issou.best/Q6w6UU"""
    m = re.search(r"(?:best/)(\w+)", ordr_url)
    if not m:
        raise ValueError(f"Invalid ORDR URL: {ordr_url}")
    return m.group(1)

def fetch_ordr_metadata(link_code: str) -> dict:
    """GET https://apis.issou.best/ordr/renders?link=<code>"""
    resp = requests.get(
        "https://apis.issou.best/ordr/renders",
        params={"link": link_code}
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("renders"):
        raise RuntimeError("No render data found for code " + link_code)
    return data["renders"][0]

def create_thumbnail(ordr_url, client_id=None, client_secret=None):
    """Create osu! thumbnail from ORDR render link."""
    try:
        if not ordr_url:
            raise ValueError("ORDR URL is required")
        
        print("Generating thumbnail from ORDR URL")
        code = extract_ordr_code(ordr_url)
        meta = fetch_ordr_metadata(code)

        # Extract fields from metadata
        username = meta["replayUsername"]
        song_title = meta["mapTitle"] 
        difficulty = meta["replayDifficulty"]

        # Parse accuracy & stars
        desc = meta["description"]
        acc_match = re.search(r"Accuracy:\s*([\d.]+)%", desc)
        accuracy = float(acc_match.group(1)) if acc_match else 0.0
        accuracy_str = f"{accuracy:.2f}%"
        
        star_match = re.search(r"\(([\d.]+) ⭐\)", desc)
        stars = float(star_match.group(1)) if star_match else 0.0

        beatmapset_id = meta["mapID"]
        token = None
        beatmapset_data = None
        user_data = None

        # Fetch API data if credentials provided
        if client_id and client_secret:
            try:
                token = get_access_token(client_id, client_secret)
                # Fetch beatmapset details (needed for difficulty ID and potentially background)
                beatmapset_data = make_api_request(token, f"beatmapsets/{beatmapset_id}")
                # Fetch user details (needed for user ID and avatar)
                user_data = make_api_request(token, f"users/{username}/osu")
            except Exception as e:
                print(f"Failed to fetch initial API data: {e}")
                # Continue without API data if possible, features like PP/Avatar might fail

        # Get background image: Try mirror first, then API
        bg_image = download_from_mirror(beatmapset_id)
        
        if not bg_image and beatmapset_data: # Use fetched beatmapset_data if available
            covers = beatmapset_data.get("covers", {})
            bg_url = covers.get("cover@2x") or covers.get("cover")
            if bg_url:
                print("Using background from osu! API")
                response = requests.get(bg_url)
                response.raise_for_status()
                bg_image = Image.open(BytesIO(response.content))

        if not bg_image:
            raise ValueError("Could not obtain background image")

        # Create thumbnail
        bg_width, bg_height = bg_image.size
        target_width, target_height = 1920, 1080
        
        # Calculate scaling
        scale = min(target_width/bg_width, target_height/bg_height)
        new_width = int(bg_width * scale)
        new_height = int(bg_height * scale)
        resized = bg_image.resize((new_width, new_height), Image.LANCZOS)
        
        # Create blank thumbnail
        thumbnail = Image.new("RGB", (target_width, target_height), (0, 0, 0))
        x_offset = (target_width - new_width) // 2
        y_offset = (target_height - new_height) // 2
        thumbnail.paste(resized, (x_offset, y_offset))
        draw = ImageDraw.Draw(thumbnail)

        # Add player avatar using fetched user_data
        try:
            avatar_url = None
            if user_data: # Use data from API if available
                 avatar_url = user_data.get("avatar_url")
            
            if not avatar_url: # Fallback if no API data or URL missing
                 # Fallback if no API credentials or user fetch failed
                 avatar_url = f"https://a.ppy.sh/{username}" # Might not work for numeric usernames
                 print("Using fallback avatar URL")

            response = requests.get(avatar_url, timeout=10)
            response.raise_for_status()
            avatar_img = Image.open(BytesIO(response.content)).convert("RGB")
            avatar_img = avatar_img.resize((150, 150))
            mask = Image.new("L", (150, 150), 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.ellipse((0, 0, 150, 150), fill=255)
            thumbnail.paste(avatar_img, (50, 930), mask)
        except Exception as e:
            print(f"Couldn't load player avatar: {e}")

        # Add text elements
        font_paths = [
            "symbola/Symbola.ttf",
            "arialuni.ttf",
            "seguiemj.ttf", 
            "arialbd.ttf",
            "arial.ttf"
        ]
        
        font_large = None
        font_medium = None
        
        for path in font_paths:
            try:
                if not font_large:
                    font_large = ImageFont.truetype(path, 72)
                if not font_medium:
                    font_medium = ImageFont.truetype(path, 48)
            except:
                continue

        # Default values for fields not provided by ORDR or API
        mods_str = "NM" # ORDR doesn't provide mods directly yet
        pp = 0.0

        # Fetch PP if possible using user_id and correct beatmap_id
        if token and user_data and beatmapset_data: # Check if we have API token and necessary data
            try:
                user_id = user_data['id']
                target_difficulty_name = meta['replayDifficulty']
                beatmap_id = None

                # Find the beatmap ID for the specific difficulty
                for beatmap in beatmapset_data.get('beatmaps', []):
                    if beatmap.get('version') == target_difficulty_name:
                        beatmap_id = beatmap.get('id')
                        print(f"Found matching beatmap ID: {beatmap_id} for difficulty '{target_difficulty_name}'")
                        break
                
                if beatmap_id:
                    # Make API call with the correct beatmap difficulty ID
                    score_data = make_api_request(token, f"beatmaps/{beatmap_id}/scores/users/{user_id}")
                    score_info = score_data.get("score")
                    if score_info:
                        pp = score_info.get("pp", 0.0)
                        # Extract mods from the actual score if available (inside if score_info)
                        mods_list = score_info.get("mods", [])
                        if mods_list:
                             mods_str = "".join(mods_list)
                        else:
                             mods_str = "NM" # Keep NM if mods list is empty
                    else: # This else corresponds to 'if score_info:'
                        # Handle case where the specific score wasn't found for this difficulty
                        print(f"Score not found for user {user_id} on beatmap {beatmap_id}. PP set to 0.")
                        pp = 0.0
                        # mods_str remains the default "NM"
                    
                    if pp is None: # API might return null PP for some scores (e.g. loved maps)
                        pp = 0.0
                    print(f"Fetched PP: {pp}, Mods: {mods_str}")
                
                else: # This else corresponds to 'if beatmap_id:'
                    print(f"Could not find beatmap ID for difficulty '{target_difficulty_name}' in beatmapset {beatmapset_id}.")

            except Exception as e:
                # Print exception details, including potential API errors
                print(f"Could not fetch PP/Score details: {e}")
                # Keep default pp = 0.0

        # Add text
        draw.text((60, 60), f"Player: {username}", font=font_medium, fill=(255, 255, 255))
        draw.text((60, 120), f"PP: {pp:.0f}", font=font_large, fill=(255, 255, 255))
        draw.text((60, 200), f"Accuracy: {accuracy_str}", font=font_medium, fill=(255, 255, 255))
        draw.text((60, 260), f"Mods: {mods_str}", font=font_medium, fill=(255, 255, 255)) # Using default NM for now
        # Adjust text position slightly if needed, ensure it fits
        # Example: Truncate long titles/difficulties if necessary
        max_text_width = target_width - 1210 - 60 # Max width for right-side text
        
        # Simple truncation example (can be improved with text wrapping or shrinking)
        available_width = target_width - 1210 - 60 
        
        def truncate_text(text, font, max_width):
            if font.getlength(text) <= max_width:
                return text
            else:
                truncated = ""
                for char in text:
                    if font.getlength(truncated + char + "...") <= max_width:
                        truncated += char
                    else:
                        break
                return truncated + "..."

        truncated_title = truncate_text(song_title, font_medium, available_width)
        truncated_diff = truncate_text(f"{difficulty} ★{stars:.2f}", font_medium, available_width)

        draw.text((1210, 60), truncated_title, font=font_medium, fill=(255, 255, 255))
        draw.text((1210, 110), truncated_diff, font=font_medium, fill=(255, 255, 255))
        
        # Save result
        thumbnail.save("thumbnail.jpg")
        
    except Exception as e:
        print(f"Thumbnail generation failed: {e}")
        raise

def verify_credentials(client_id, client_secret):
    """Verify if credentials are valid by attempting to get an access token"""
    try:
        token = get_access_token(client_id, client_secret)
        return True
    except Exception as e:
        print(f"Credential verification failed: {e}")
        return False

def save_credentials_to_env(client_id, client_secret):
    """Save credentials to Windows user environment variables"""
    try:
        # Open the environment key in registry
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE
        )
        
        # Set the values
        winreg.SetValueEx(key, "OSU_CLIENT_ID_THUMBNAIL", 0, winreg.REG_SZ, client_id)
        winreg.SetValueEx(key, "OSU_CLIENT_SECRET_THUMBNAIL", 0, winreg.REG_SZ, client_secret)
        
        # Close the key
        winreg.CloseKey(key)
        
        # Update current process environment
        os.environ["OSU_CLIENT_ID_THUMBNAIL"] = client_id
        os.environ["OSU_CLIENT_SECRET_THUMBNAIL"] = client_secret
        
        # Broadcast WM_SETTINGCHANGE to notify other processes
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x1A,    # WM_SETTINGCHANGE
            0,
            "Environment",
            0x02,    # SMTO_ABORTIFHUNG
            5000,    # timeout in ms
            None
        )
        return True
    except Exception as e:
        print(f"Failed to save credentials to environment: {e}")
        return False

def get_input(prompt):
    """Cross-platform input function that works with compiled executables"""
    try:
        return input(prompt).strip()
    except EOFError:
        # For compiled executables that don't properly handle input()
        print(prompt, end='', flush=True)
        return sys.stdin.readline().strip()

def main():
    # Try to load existing credentials
    client_id = os.getenv("OSU_CLIENT_ID_THUMBNAIL")
    client_secret = os.getenv("OSU_CLIENT_SECRET_THUMBNAIL")
    
    # If not found, prompt user
    if not client_id or not client_secret:
        print("Please enter your osu! API credentials:")
        client_id = get_input("Client ID: ")
        client_secret = get_input("Client Secret: ")
        
        if not client_id or not client_secret:
            print("Error: Both Client ID and Secret are required")
            get_input("Press Enter to exit...")
            return
            
        # Verify credentials
        if verify_credentials(client_id, client_secret):
            if save_credentials_to_env(client_id, client_secret):
                print("Credentials verified and saved to system environment!")
            else:
                print("Credentials verified but failed to save to environment.")
                print("The application will continue but you'll need to enter credentials next time.")
        else:
            print("Invalid credentials. Please try again.")
            get_input("Press Enter to exit...")
            return
    
    # Get ORDR URL
    ordr_url = get_input("Enter ORDR URL: ")
    if not ordr_url:
        print("ORDR URL is required")
        get_input("Press Enter to exit...")
        return
    
    try:
        create_thumbnail(
            ordr_url=ordr_url,
            client_id=client_id,
            client_secret=client_secret
        )
        print("Thumbnail created successfully!")
    except Exception as e:
        print(f"Error creating thumbnail: {e}")
    finally:
        get_input("Press Enter to exit...")

if __name__ == "__main__":
    main()