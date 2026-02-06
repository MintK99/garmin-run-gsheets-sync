import os
import json
from garminconnect import Garmin
from google.oauth2.service_account import Credentials
import gspread
from datetime import datetime, timedelta

# Load environment variables from .env file if it exists (for local testing)
if os.path.exists('.env'):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("Warning: python-dotenv not installed. Install with: pip install python-dotenv")
        pass

def format_duration(seconds):
    """Convert seconds to minutes (rounded to 2 decimals)"""
    return round(seconds / 60, 2) if seconds else 0

def format_pace(distance_meters, duration_seconds):
    """Calculate pace in min/km"""
    if not distance_meters or not duration_seconds:
        return 0
    distance_km = distance_meters / 1000
    pace_seconds = duration_seconds / distance_km
    return round(pace_seconds / 60, 2)  # Convert to min/km

def get_activity_detail_for_gear(garmin, activity_id: int) -> dict:
    """
    gear(신발) 정보가 들어있을 가능성이 높은 '활동 요약/상세' 응답을 가져온다.
    라이브러리 버전에 따라 메서드명이 달라서 순차적으로 시도한다.
    """
    candidates = [
        "get_activity_summary",        # 가장 그럴듯한 이름
        "get_activity",                # 어떤 버전에서는 activityId로 요약을 주기도 함
        "get_activity_details",        # 지금은 metrics를 주고 있지만, 버전에 따라 요약일 수도 있어 fallback
        "get_activity_detail",
    ]

    last_err = None
    for m in candidates:
        if hasattr(garmin, m):
            try:
                return getattr(garmin, m)(activity_id) or {}
            except Exception as e:
                last_err = e

    # 아무것도 못 가져오면 빈 dict
    if last_err:
        print(f"Warning: could not fetch activity detail for gear. activityId={activity_id} err={last_err}")
    return {}

def extract_shoe_from_detail(detail: dict):
    """
    활동 요약/상세 응답에서 신발(gear) 추출.
    케이스가 여러 개라 후보 키를 폭넓게 봄.
    """
    if not isinstance(detail, dict):
        return "", ""

    # 대표 케이스: gear 리스트
    gear_list = detail.get("gear")
    if isinstance(gear_list, list) and gear_list:
        g = gear_list[0]
        name = g.get("customMakeModel") or g.get("displayName") or g.get("name") or ""
        gid = str(g.get("gearId") or g.get("id") or "")
        return name, gid

    # 다른 케이스들
    for key in ["activityGearDTOs", "activityGear", "gears", "activityGearList"]:
        v = detail.get(key)
        if isinstance(v, list) and v:
            g = v[0]
            name = g.get("customMakeModel") or g.get("displayName") or g.get("name") or ""
            gid = str(g.get("gearId") or g.get("id") or "")
            return name, gid

    # 요약 필드로만 오는 케이스
    name = detail.get("gearName") or ""
    gid = str(detail.get("gearId") or "")
    return name, gid


def main():
    print("Starting Garmin running activities sync...")
    
    # Get credentials from environment variables
    garmin_email = os.environ.get('GARMIN_EMAIL')
    garmin_password = os.environ.get('GARMIN_PASSWORD')
    google_creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    sheet_id = os.environ.get('SHEET_ID')  # Add sheet ID from environment
    
    # For local testing: try to load from credentials.json file
    if not google_creds_json and os.path.exists('credentials.json'):
        print("Loading Google credentials from credentials.json...")
        with open('credentials.json', 'r') as f:
            google_creds_json = f.read()
    
    if not all([garmin_email, garmin_password, google_creds_json, sheet_id]):
        print("❌ Missing required environment variables")
        print(f"   GARMIN_EMAIL: {'✓' if garmin_email else '✗'}")
        print(f"   GARMIN_PASSWORD: {'✓' if garmin_password else '✗'}")
        print(f"   GOOGLE_CREDENTIALS: {'✓' if google_creds_json else '✗'}")
        print(f"   SHEET_ID: {'✓' if sheet_id else '✗'}")
        return
    
    # Connect to Garmin
    print("Connecting to Garmin...")
    try:
        garmin = Garmin(garmin_email, garmin_password)
        garmin.login()
        print("✅ Connected to Garmin")
    except Exception as e:
        print(f"❌ Failed to connect to Garmin: {e}")
        return
    
    # Get recent activities (last 7 days)
    print("Fetching recent activities...")
    try:
        activities = garmin.get_activities(0, 20)  # Get last 20 activities
        print(f"Found {len(activities)} total activities")
    except Exception as e:
        print(f"❌ Failed to fetch activities: {e}")
        return
    
    # Filter for running activities only
    running_activities = [
        activity for activity in activities 
        if activity.get('activityType', {}).get('typeKey', '').lower() in ['running', 'track_running', 'treadmill_running', 'trail_running']
    ]
    
    print(f"Found {len(running_activities)} running activities")
    
    if not running_activities:
        print("No running activities found in recent data")
        return
    
    # Connect to Google Sheets
    print("Connecting to Google Sheets...")
    try:
        creds_dict = json.loads(google_creds_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]
        )
        client = gspread.authorize(creds)
        sheet = client.open("Garmin Data").sheet1
        print("✅ Connected to Google Sheets")
    except Exception as e:
        print(f"❌ Failed to connect to Google Sheets: {e}")
        return
    
    # Get existing dates to avoid duplicates
    try:
        existing_data = sheet.get_all_values()
        existing_activity_ids = set()
        if len(existing_data) > 1:
            for row in existing_data[1:]:
                if row and row[0]:
                    existing_activity_ids.add(row[0])
        print(f"Found {len(existing_activity_ids)} existing entries")
    except Exception as e:
        print(f"Warning: Could not check existing data: {e}")
        existing_activity_ids = set()
    
    # Process each running activity
    new_entries = 0
    for activity in running_activities:
        try:
            activity_id = str(activity.get('activityId', ''))
            if not activity_id:
                print("Skipping activity - missing activityId")
                continue
    
            # Skip if already in sheet (by activityId)
            if activity_id in existing_activity_ids:
                print(f"Skipping activityId {activity_id} - already exists")
                continue
    
            activity_date = activity.get('startTimeLocal', '')[:10]  # YYYY-MM-DD
    
            # Extract metrics
            activity_name = activity.get('activityName', 'Run')
            distance_meters = activity.get('distance', 0)
            distance_km = round(distance_meters / 1000, 2) if distance_meters else 0
            duration_seconds = activity.get('duration', 0)
            duration_min = format_duration(duration_seconds)
            avg_pace = format_pace(distance_meters, duration_seconds)
            avg_hr = activity.get('averageHR', 0) or 0
            max_hr = activity.get('maxHR', 0) or 0
            calories = activity.get('calories', 0) or 0
            avg_cadence = activity.get('averageRunningCadenceInStepsPerMinute', 0) or 0
            elevation_gain = round(activity.get('elevationGain', 0), 1) if activity.get('elevationGain') else 0
            activity_type = activity.get('activityType', {}).get('typeKey', 'running')

            activity_id = activity.get("activityId")
            shoe_name, shoe_id = "", ""
            
            detail = get_activity_detail_for_gear(garmin, activity_id)
            
            # 🔎 디버그: gear가 있는지 확인 (처음엔 꼭 찍어보세요)
            print(f"activityId={activity_id} DETAIL_FOR_GEAR_KEYS:", list(detail.keys())[:80])
            
            shoe_name, shoe_id = extract_shoe_from_detail(detail)
            print(f"activityId={activity_id} shoe_name={shoe_name} shoe_id={shoe_id}")
    
            # Prepare row (activity_id added)
            row = [
                activity_id,
                activity_date,
                activity_name,
                distance_km,
                duration_min,
                avg_pace,
                avg_hr,
                max_hr,
                calories,
                avg_cadence,
                elevation_gain,
                activity_type,
                shoe_name,   # NEW
                shoe_id,     # NEW
            ]
    
            sheet.append_row(row)
            print(f"✅ Added: {activity_date} - {activity_name} ({distance_km} km) [id={activity_id}]")
            new_entries += 1
            existing_activity_ids.add(activity_id)  # avoid duplicates within same run
    
        except Exception as e:
            print(f"❌ Error processing activity: {e}")
            continue

    
    if new_entries > 0:
        print(f"\n🎉 Successfully added {new_entries} new running activities!")
    else:
        print("\n✓ No new activities to add")

if __name__ == "__main__":
    main()
