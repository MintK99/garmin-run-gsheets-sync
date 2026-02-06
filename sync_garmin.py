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

def build_gear_map(garmin, user_profile_number: int) -> dict:
    """
    Garmin gear 목록을 가져와 gearId -> 이름 매핑을 만든다.
    """
    gear_map = {}

    gears = garmin.get_gear(user_profile_number)

    # 반환 형태 방어
    if isinstance(gears, dict):
        gears = gears.get("gearList") or gears.get("gear") or gears.get("gears") or []

    if isinstance(gears, list):
        for g in gears:
            gid = str(g.get("gearId") or g.get("id") or "")
            name = g.get("customMakeModel") or g.get("displayName") or g.get("name") or ""
            if gid:
                gear_map[gid] = name

    return gear_map

def get_shoes_for_activity(garmin, activity_id: int, gear_map: dict):
    """
    특정 activity에 연결된 gear(신발) 정보를 반환.
    반환: (shoe_names_csv, shoe_ids_csv)
    """
    try:
        ag = garmin.get_activity_gear(activity_id)
    except Exception:
        return "", ""

    # 반환 형태 방어
    # 보통 list 또는 dict(list 포함) 형태
    gear_items = []
    if isinstance(ag, list):
        gear_items = ag
    elif isinstance(ag, dict):
        gear_items = ag.get("gear") or ag.get("gearList") or ag.get("gears") or []

    gear_ids = []
    shoe_names = []
    for g in gear_items:
        gid = str(g.get("gearId") or g.get("id") or "")
        if not gid:
            continue
        gear_ids.append(gid)
        shoe_names.append(gear_map.get(gid, ""))

    # activity에 신발이 1개면 보통 첫 번째만 써도 됨.
    # 여기서는 안전하게 CSV로 반환.
    shoe_names_csv = ", ".join([n for n in shoe_names if n])  # 빈 이름 제거
    shoe_ids_csv = ", ".join(gear_ids)

    return shoe_names_csv, shoe_ids_csv

def get_user_profile_number(garmin) -> int:
    profile = garmin.get_user_profile()

    if not isinstance(profile, dict):
        raise RuntimeError(f"Unexpected profile type: {type(profile)}")

    # 🔎 1회 디버그: 키 확인 (Actions 로그에서 확인 후 지워도 됨)
    print("PROFILE KEYS:", list(profile.keys())[:80])

    # 후보 키들 (버전/언어/엔드포인트에 따라 다름)
    candidates = [
        "userProfileNumber",
        "profileId",
        "userProfileId",
        "userId",
        "displayName",  # (숫자 아님이라 보통 실패, but 방어용)
    ]

    for k in candidates:
        v = profile.get(k)
        # 숫자형이면 바로 리턴
        if isinstance(v, int):
            return v
        # 문자열 숫자면 변환
        if isinstance(v, str) and v.isdigit():
            return int(v)

    # 일부 응답은 중첩 구조일 수 있어 추가 탐색
    for parent_key in ["userProfile", "profile", "data"]:
        sub = profile.get(parent_key)
        if isinstance(sub, dict):
            for k in candidates:
                v = sub.get(k)
                if isinstance(v, int):
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)

    # 못 찾으면 profile 전체를 일부 출력 (민감정보 제외를 위해 keys만)
    raise RuntimeError("Failed to locate user profile number field in profile response")

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
        
        print("Loading gear list...")
        
        # 1) user profile number 획득
        user_profile_number = get_user_profile_number(garmin)
        print("✅ userProfileNumber:", user_profile_number)
        
        # 2) gear map 생성
        gear_map = build_gear_map(garmin, user_profile_number)

        print(f"✅ Loaded {len(gear_map)} gears")

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
            shoe_name, shoe_id = get_shoes_for_activity(garmin, int(activity_id), gear_map)
            
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
