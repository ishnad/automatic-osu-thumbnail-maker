import requests
import zipfile
from osupyparser import OsuFile
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import os
from dotenv import load_dotenv
import base64
import json

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
        # Use requests with Beatconnect API
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
            # Clean up the downloaded .osz file
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
                
                # Make a copy of the image data and close the original
                from io import BytesIO
                img_bytes = BytesIO()
                bg_image.save(img_bytes, format=bg_image.format)
                bg_image.close()
                img_bytes.seek(0)
                bg_image = Image.open(img_bytes)
                
    except Exception as e:
        print(f"Mirror download failed: {e}")
    finally:
        # Clean up extracted folder if it exists
        if os.path.exists(extract_folder):
            import time
            import shutil
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
                        time.sleep(0.5)  # Wait before retrying
    return bg_image

def create_thumbnail(score_id, client_id, client_secret):
    # 1. Authenticate
    token = get_access_token(client_id, client_secret)
    
    try:
        # 3. Fetch score data with debug logging
        score_data = make_api_request(token, f"scores/{score_id}")
        # print("DEBUG - API Response score_data:", json.dumps(score_data, indent=2))
        
        # 4. Extract required information from direct response
        try:
            mods = score_data.get("mods", [])
            mods_str = "+".join(mods) if mods else "NM"
            
            pp = score_data.get("pp", 0)
            accuracy = score_data.get("accuracy", 0) * 100
            accuracy_str = f"{accuracy:.2f}%"

            username = score_data["user"]["username"]
            
            # Get background image - try mirror first, then fallback to API
            beatmap_id = score_data["beatmap"]["id"]
            beatmap_data = make_api_request(token, f"beatmaps/{beatmap_id}")
            beatmapset_id = beatmap_data["beatmapset_id"]
            
            # Try to get background from mirror download first
            bg_image = download_from_mirror(beatmapset_id)
            if bg_image:
                print("Using background from mirror download")
            else:
                print("Falling back to official API cover image")
                
        except (KeyError, TypeError) as e:
            print(f"Error parsing API response: {str(e)}")
            print("Please check if the score ID is valid and the API response format")
            return
        beatmapset_data = make_api_request(token, f"beatmapsets/{beatmapset_id}")
        # print("DEBUG - API Response beatmap_data:", json.dumps(beatmap_data, indent=2))
        #print("DEBUG - API Response beatmapset_data:", json.dumps(beatmapset_data, indent=2))

        # Only fetch from API if we didn't get image from mirror
        if not bg_image:
            # --- Revised Background Image Fetching ---
            covers = None
            if "beatmapset" in beatmap_data and isinstance(beatmap_data.get("beatmapset"), dict) and "covers" in beatmap_data["beatmapset"]:
                covers = beatmap_data["beatmapset"]["covers"]
                print("DEBUG: Found covers object in beatmap_data.beatmapset")
            elif "covers" in beatmapset_data:
                covers = beatmapset_data["covers"]
                print("DEBUG: Found covers object in beatmapset_data")
            else:
                print("Error: Could not find 'covers' object in API responses.")
                return

            bg_url = None
            if covers:
                bg_url = covers.get("raw") or covers.get("cover@2x") or covers.get("cover")
            
            if not bg_url or not isinstance(bg_url, str):
                raise ValueError(f"Failed to retrieve valid background URL from API. Covers: {covers}")

            try:
                print(f"DEBUG: Attempting to download image from: {bg_url}")
                response = requests.get(bg_url, timeout=15)
                response.raise_for_status()
                bg_image = Image.open(BytesIO(response.content))
            except requests.exceptions.RequestException as e:
                print(f"Error downloading background image: {e}")
                return
            except Exception as e:
                print(f"Error processing background image: {e}")
                return
        
        # Create thumbnail with black bars to maintain aspect ratio
        bg_width, bg_height = bg_image.size
        target_width, target_height = 1920, 1080
        
        # Calculate scaling factor
        width_ratio = target_width / bg_width
        height_ratio = target_height / bg_height
        scale = min(width_ratio, height_ratio)
        
        # Resize image proportionally
        new_width = int(bg_width * scale)
        new_height = int(bg_height * scale)
        resized = bg_image.resize((new_width, new_height), Image.LANCZOS)
        
        # Create blank thumbnail with black background
        thumbnail = Image.new("RGB", (target_width, target_height), (0, 0, 0))
        
        # Paste resized image centered
        x_offset = (target_width - new_width) // 2
        y_offset = (target_height - new_height) // 2
        thumbnail.paste(resized, (x_offset, y_offset))
        draw = ImageDraw.Draw(thumbnail)
        
        thumbnail.save("bgBeatmap.jpg") # for debugging

        # Load font (adjust path as needed)
        try:
            font_large = ImageFont.truetype("arialbd.ttf", 72)
            font_medium = ImageFont.truetype("arial.ttf", 48)
        except:
            # Fallback to default font if arial not available
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
        
        # Add song info at top right
        song_title = beatmapset_data["title"]
        difficulty = beatmap_data["version"]
        stars = beatmap_data["difficulty_rating"]
        
        # Add semi-transparent background for text (expanded)
        draw.rectangle([(50, 50), (500, 350)], fill=(0, 0, 0, 128))
        draw.rectangle([(1200, 50), (1870, 150)], fill=(0, 0, 0, 128))
        
        # Add text elements
        draw.text((60, 60), f"Player: {username}", font=font_medium, fill=(255, 255, 255))
        draw.text((60, 120), f"PP: {pp:.0f}", font=font_large, fill=(255, 255, 255))
        draw.text((60, 200), f"Accuracy: {accuracy_str}", font=font_medium, fill=(255, 255, 255))
        draw.text((60, 260), f"Mods: {mods_str}", font=font_medium, fill=(255, 255, 255))
        
        # Add song info
        draw.text((1210, 60), f"{song_title}", font=font_medium, fill=(255, 255, 255))
        draw.text((1210, 110), f"{difficulty} ★{stars:.2f}", font=font_medium, fill=(255, 255, 255))
        
        # Save the result
        thumbnail.save("thumbnail.jpg")
        
    except Exception as e:
        print(f"Error: {str(e)}")

# Load credentials from .env.local
load_dotenv(dotenv_path=".env.local")
client_id = os.getenv("CLIENT_ID")
client_secret = os.getenv("CLIENT_SECRET")

# Example usage
if __name__ == "__main__":
    score_id = "4703632894"
    create_thumbnail(score_id, client_id, client_secret)