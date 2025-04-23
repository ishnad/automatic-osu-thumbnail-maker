import requests
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

def create_thumbnail(score_id, client_id, client_secret):
    # 1. Authenticate
    token = get_access_token(client_id, client_secret)
    
    try:
        # 3. Fetch score data with debug logging
        score_data = make_api_request(token, f"scores/{score_id}")
        print("DEBUG - API Response:", json.dumps(score_data, indent=2))
        
        # 4. Extract required information from direct response
        try:
            mods = score_data.get("mods", [])
            mods_str = "+".join(mods) if mods else "NM"
            
            pp = score_data.get("pp", 0)
            accuracy = score_data.get("accuracy", 0) * 100
            accuracy_str = f"{accuracy:.2f}%"

            username = score_data["user"]["username"]
            
            # Get background image
            beatmap_id = score_data["beatmap"]["id"]
                
        except (KeyError, TypeError) as e:
            print(f"Error parsing API response: {str(e)}")
            print("Please check if the score ID is valid and the API response format")
            return
        beatmap_data = make_api_request(token, f"beatmaps/{beatmap_id}")
        beatmapset_id = beatmap_data["beatmapset_id"]
        beatmapset_data = make_api_request(token, f"beatmapsets/{beatmapset_id}")
        
        bg_url = beatmapset_data["covers"]["cover@2x"]
        
        # Download image
        response = requests.get(bg_url)
        bg_image = Image.open(BytesIO(response.content))
        
        # 6. Process image (same as before)
        thumbnail = bg_image.resize((1920, 1080))
        draw = ImageDraw.Draw(thumbnail)
        
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
    score_id = "1784607572"
    create_thumbnail(score_id, client_id, client_secret)