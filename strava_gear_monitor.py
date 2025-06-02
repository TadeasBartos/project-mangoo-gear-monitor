"""
Strava Gear Wear Monitor

This module provides functionality to monitor gear usage and wear from Strava activities.
It fetches activities, analyzes gear usage, and provides detailed gear information.
"""

import requests
import time
import urllib3
from urllib.parse import urlencode
import webbrowser
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import json
from pathlib import Path
from enum import Enum, auto

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class MaintenanceType(Enum):
    """Types of maintenance that can be performed."""
    WASH = auto()
    TIRE_PRESSURE = auto()
    LUBE = auto()
    # Add more maintenance types here as needed
    
    @classmethod
    def get_all_types(cls):
        """Get all maintenance types with their descriptions."""
        return {
            cls.WASH: "Washed bike",
            cls.TIRE_PRESSURE: "Pressured tires",
            cls.LUBE: "Lubed chain/components",
            # Add more maintenance types here
        }
    
    @classmethod
    def get_type_by_number(cls, number: int) -> Optional['MaintenanceType']:
        """Get maintenance type by its number in the list."""
        types = list(cls.get_all_types().keys())
        if 1 <= number <= len(types):
            return types[number - 1]
        return None

@dataclass
class MaintenanceRecord:
    """Data class to store gear maintenance records."""
    gear_id: str
    maintenance_type: MaintenanceType
    date: datetime
    notes: Optional[str] = None
    distance_at_maintenance: float = 0.0  # in kilometers
    activities_since_last_maintenance: List[Dict] = None  # List of activities since last maintenance

    def __post_init__(self):
        if self.activities_since_last_maintenance is None:
            self.activities_since_last_maintenance = []

    def calculate_distance(self) -> float:
        """Calculate actual distance ridden since last maintenance."""
        return sum(activity.get('distance', 0) for activity in self.activities_since_last_maintenance) / 1000  # Convert to km

@dataclass
class GearUsage:
    """Data class to store gear usage statistics."""
    gear_id: str
    sport_types: Set[str]
    total_distance_m: float
    total_distance_km: float
    activities_count: int
    first_activity_date: Optional[datetime] = None
    last_activity_date: Optional[datetime] = None
    maintenance_history: List[MaintenanceRecord] = None

    def __post_init__(self):
        if self.maintenance_history is None:
            self.maintenance_history = []

@dataclass
class ServiceInterval:
    """Data class to store service interval requirements."""
    gear_id: str
    item: str
    interval_type: str  # 'time' or 'distance'
    interval_value: float  # weeks for time, kilometers for distance
    action: str  # what to do (e.g., 'replace', 'service', 'check')
    last_service_date: Optional[datetime] = None
    last_service_distance: Optional[float] = None  # in kilometers

    def __post_init__(self):
        if self.interval_type not in ['time', 'distance']:
            raise ValueError("interval_type must be 'time' or 'distance'")

@dataclass
class Component:
    """Data class to store component information."""
    id: str  # Unique identifier for the component
    name: str  # Component name (e.g., "Chain", "Tires")
    brand: str
    model: str
    installation_date: datetime
    gear_id: str  # ID of the bike it's installed on
    status: str = "active"  # active, in_inventory, retired
    notes: Optional[str] = None
    purchase_date: Optional[datetime] = None
    purchase_price: Optional[float] = None
    mileage_at_installation: float = 0.0  # in kilometers
    current_mileage: float = 0.0  # in kilometers

    def __post_init__(self):
        if self.status not in ["active", "in_inventory", "retired"]:
            raise ValueError("status must be 'active', 'in_inventory', or 'retired'")

@dataclass
class ComponentSwap:
    """Data class to store component swap information."""
    date: datetime
    gear_id: str
    component_id: str
    old_component_id: Optional[str]  # None if it's a new component
    action: str  # 'install', 'remove', 'retire'
    mileage: float  # in kilometers
    notes: Optional[str] = None

@dataclass
class SyncState:
    """Data class to store sync state information."""
    last_sync_time: datetime
    latest_activity_id: Optional[str] = None
    latest_activity_date: Optional[datetime] = None

class StravaGearMonitor:
    """Main class for monitoring Strava gear usage and wear."""
    
    def __init__(self, client_id: str, client_secret: str, refresh_token: Optional[str] = None):
        """
        Initialize the Strava Gear Monitor.
        
        Args:
            client_id: Strava API client ID
            client_secret: Strava API client secret
            refresh_token: Optional refresh token for existing authentication
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = None
        self.token_expires_at = None
        self.base_url = "https://www.strava.com/api/v3"
        self.redirect_uri = "http://localhost/exchange_token"
        self.headers = None
        
        # Initialize user_id as None - will be set after authentication
        self.user_id = None
        
        # Add active bike tracking
        self.active_bike: Optional[Dict] = None  # Will store the currently selected bike
        
        # Initialize file paths as None - will be set after user_id is obtained
        self.maintenance_file = None
        self.intervals_file = None
        self.components_file = None
        self.component_swaps_file = None
        self.sync_state_file = None
        
        # Initialize data structures
        self.maintenance_records: Dict[str, List[MaintenanceRecord]] = {}
        self.service_intervals: Dict[str, List[ServiceInterval]] = {}
        self.components: Dict[str, Component] = {}
        self.component_swaps: List[ComponentSwap] = []
        self.sync_state: Optional[SyncState] = None
        self.activities_cache: List[Dict] = []  # Cache for activities

    def is_token_expired(self) -> bool:
        """
        Check if the current access token is expired or will expire soon.
        
        Returns:
            bool: True if token is expired or will expire within 5 minutes
        """
        if not self.token_expires_at:
            return True
            
        # Check if token expires within 5 minutes (300 seconds)
        return time.time() >= (self.token_expires_at - 300)

    def ensure_valid_token(self) -> bool:
        """
        Ensure we have a valid access token, refreshing if necessary.
        
        Returns:
            bool: True if we have a valid token, False otherwise
        """
        if not self.access_token or self.is_token_expired():
            return self.refresh_access_token()
        return True

    def make_authenticated_request(self, endpoint: str, params: Optional[Dict] = None, method: str = 'GET', data: Optional[Dict] = None) -> Optional[requests.Response]:
        """
        Make an authenticated request to the Strava API.
        
        Args:
            endpoint: API endpoint (e.g., '/athlete/activities')
            params: Optional query parameters
            method: HTTP method ('GET' or 'POST')
            data: Optional data for POST requests
            
        Returns:
            Response object or None if failed
        """
        if not self.ensure_valid_token():
            logger.error("Failed to obtain valid access token")
            return None
            
        url = f"{self.base_url}{endpoint}"
        
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=self.headers, params=params)
            else:  # POST
                response = requests.post(url, headers=self.headers, params=params, data=data)
            
            # If we get 401, try refreshing token once more
            if response.status_code == 401:
                logger.info("Received 401, attempting to refresh token...")
                if self.refresh_access_token():
                    if method.upper() == 'GET':
                        response = requests.get(url, headers=self.headers, params=params)
                    else:  # POST
                        response = requests.post(url, headers=self.headers, params=params, data=data)
                else:
                    logger.error("Token refresh failed")
                    return None
            
            response.raise_for_status()
            return response
            
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response text: {e.response.text}")
            return None

    def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token."""
        if not self.refresh_token:
            logger.error("No refresh token available")
            return False

        auth_url = "https://www.strava.com/oauth/token"
        payload = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token,
            'grant_type': 'refresh_token'
        }
        
        try:
            response = requests.post(auth_url, data=payload)
            response.raise_for_status()
            token_data = response.json()
            
            self.access_token = token_data['access_token']
            self.refresh_token = token_data['refresh_token']
            self.token_expires_at = token_data['expires_at']
            self._update_headers()
            
            # Save the refresh token to api_keys.py
            try:
                with open('api_keys.py', 'r') as f:
                    lines = f.readlines()
                
                # Find the line with EXISTING_REFRESH_TOKEN and update it
                for i, line in enumerate(lines):
                    if line.startswith('EXISTING_REFRESH_TOKEN'):
                        lines[i] = f'EXISTING_REFRESH_TOKEN = "{self.refresh_token}"\n'
                        break
                
                with open('api_keys.py', 'w') as f:
                    f.writelines(lines)
                logger.info("Saved refresh token to api_keys.py")
            except Exception as e:
                logger.error(f"Error saving refresh token to api_keys.py: {e}")
            
            logger.info(f"Token refreshed successfully. Expires at: {time.ctime(self.token_expires_at)}")
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error refreshing token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response text: {e.response.text}")
            return False

    def exchange_code_for_tokens(self, authorization_code: str) -> Optional[Dict]:
        """Exchange authorization code for access and refresh tokens."""
        auth_url = "https://www.strava.com/oauth/token"
        payload = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': authorization_code,
            'grant_type': 'authorization_code',
            'redirect_uri': self.redirect_uri
        }
        
        try:
            response = requests.post(auth_url, data=payload)
            response.raise_for_status()
            token_data = response.json()
            
            self.access_token = token_data['access_token']
            self.refresh_token = token_data['refresh_token']
            self.token_expires_at = token_data['expires_at']
            self._update_headers()

            # Save the refresh token to api_keys.py
            try:
                with open('api_keys.py', 'r') as f:
                    lines = f.readlines()
                
                # Find the line with EXISTING_REFRESH_TOKEN and update it
                for i, line in enumerate(lines):
                    if line.startswith('EXISTING_REFRESH_TOKEN'):
                        lines[i] = f'EXISTING_REFRESH_TOKEN = "{self.refresh_token}"\n'
                        break
                
                with open('api_keys.py', 'w') as f:
                    f.writelines(lines)
                logger.info("Saved refresh token to api_keys.py")
            except Exception as e:
                logger.error(f"Error saving refresh token to api_keys.py: {e}")

            return token_data
        except requests.exceptions.RequestException as e:
            logger.error(f"Error exchanging code: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response text: {e.response.text}")
            return None

    def _update_headers(self):
        """Update the headers with the current access token."""
        self.headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }

    def get_all_activities(self, per_page: int = 200) -> List[Dict]:
        """
        Fetch all activities from Strava API.
        
        Args:
            per_page: Number of activities per page (max 200)
            
        Returns:
            List of activity dictionaries
        """
        all_activities = []
        page = 1
        
        while True:
            try:
                params = {'per_page': per_page, 'page': page}
                logger.info(f"Fetching activities page {page}...")
                
                response = self.make_authenticated_request('/athlete/activities', params)
                if not response:
                    break
                
                activities = response.json()
                if not activities:  # Empty list means no more activities
                    break
                    
                all_activities.extend(activities)
                logger.info(f"Retrieved {len(activities)} activities from page {page}")
                page += 1
                
                # Respect API rate limits
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Error fetching activities: {e}")
                break
                
        logger.info(f"Total activities retrieved: {len(all_activities)}")
        return all_activities

    def filter_activities_by_sport_type(self, activities: List[Dict], sport_type: str) -> List[Dict]:
        """
        Filter activities by sport type.
        
        Args:
            activities: List of activity dictionaries
            sport_type: Sport type to filter by (e.g., 'GravelRide', 'Ride')
            
        Returns:
            Filtered list of activities
        """
        return [activity for activity in activities if activity.get('sport_type') == sport_type]

    def analyze_gear_usage(self, activities: List[Dict]) -> Dict[str, GearUsage]:
        """
        Analyze gear usage from activities.
        
        Args:
            activities: List of activity dictionaries
            
        Returns:
            Dictionary mapping gear IDs to GearUsage objects
        """
        gear_usage = {}
        
        for activity in activities:
            gear_id = activity.get('gear_id')
            if not gear_id:
                continue
                
            if gear_id not in gear_usage:
                gear_usage[gear_id] = GearUsage(
                    gear_id=gear_id,
                    sport_types=set(),
                    total_distance_m=0,
                    total_distance_km=0,
                    activities_count=0,
                    maintenance_history=self.maintenance_records.get(gear_id, [])
                )
            
            usage = gear_usage[gear_id]
            
            # Update sport types
            if activity.get('sport_type'):
                usage.sport_types.add(activity['sport_type'])
            
            # Update distances
            distance = activity.get('distance', 0)
            usage.total_distance_m += distance
            usage.total_distance_km = usage.total_distance_m / 1000
            
            # Update activity count
            usage.activities_count += 1
            
            # Update dates
            activity_date = datetime.fromisoformat(activity['start_date'].replace('Z', '+00:00'))
            if not usage.first_activity_date or activity_date < usage.first_activity_date:
                usage.first_activity_date = activity_date
            if not usage.last_activity_date or activity_date > usage.last_activity_date:
                usage.last_activity_date = activity_date
        
        return gear_usage

    def get_gear_details(self, gear_id: str) -> Optional[Dict]:
        """
        Fetch detailed gear information from Strava API.
        
        Args:
            gear_id: Strava gear ID
            
        Returns:
            Dictionary containing gear details or None if error occurs
        """
        try:
            url = f"{self.base_url}/gear/{gear_id}"
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching gear details for {gear_id}: {e}")
            return None

    def get_all_gear_details(self, gear_usage: Dict[str, GearUsage]) -> Dict[str, Dict]:
        """
        Fetch details for all gear IDs.
        
        Args:
            gear_usage: Dictionary of gear usage statistics
            
        Returns:
            Dictionary mapping gear IDs to their details
        """
        gear_details = {}
        
        for gear_id in gear_usage.keys():
            logger.info(f"Fetching details for gear: {gear_id}")
            details = self.get_gear_details(gear_id)
            if details:
                gear_details[gear_id] = details
            time.sleep(0.1)  # Respect API rate limits
            
        return gear_details

    def _get_activities_between_dates(self, activities: List[Dict], start_date: Optional[datetime], end_date: datetime) -> List[Dict]:
        """
        Get activities between two dates.
        
        Args:
            activities: List of all activities
            start_date: Start date (inclusive) or None for no start date
            end_date: End date (inclusive)
            
        Returns:
            List of activities between the dates
        """
        filtered_activities = []
        
        # Debug logging
        logger.info(f"Filtering activities between {start_date} and {end_date}")
        
        for activity in activities:
            try:
                # Parse activity date and ensure it's timezone-aware
                activity_date = datetime.fromisoformat(activity['start_date'].replace('Z', '+00:00'))
                
                # Debug logging for first few activities
                if len(filtered_activities) < 3:
                    logger.info(f"Activity date: {activity_date}, Distance: {activity.get('distance', 0)/1000:.2f} km")
                
                # Check if activity is within date range
                if start_date is None or activity_date >= start_date:
                    if activity_date <= end_date:
                        filtered_activities.append(activity)
                        if len(filtered_activities) <= 3:
                            logger.info(f"Included activity from {activity_date}")
            except Exception as e:
                logger.error(f"Error processing activity date: {e}")
                continue
        
        # Log summary
        total_distance = sum(activity.get('distance', 0) for activity in filtered_activities) / 1000
        logger.info(f"Found {len(filtered_activities)} activities between {start_date} and {end_date}")
        logger.info(f"Total distance: {total_distance:.2f} km")
        
        return filtered_activities

    def record_maintenance(self, gear_id: str, maintenance_type: MaintenanceType, notes: Optional[str] = None) -> bool:
        """
        Record a maintenance event.
        
        Args:
            gear_id: Strava gear ID
            maintenance_type: Type of maintenance performed
            notes: Optional notes about the maintenance
            
        Returns:
            bool: True if record was added successfully
        """
        try:
            current_date = datetime.now().astimezone()
            
            # Get all activities
            activities = self.get_all_activities()
            
            # Get previous maintenance record of the same type
            previous_maintenance = None
            if gear_id in self.maintenance_records:
                type_records = [r for r in self.maintenance_records[gear_id] 
                              if r.maintenance_type == maintenance_type]
                if type_records:
                    previous_maintenance = max(type_records, key=lambda x: x.date)
            
            # Get activities since last maintenance
            activities_since_maintenance = self._get_activities_between_dates(
                activities,
                previous_maintenance.date if previous_maintenance else None,
                current_date
            )
            
            # Filter activities for this gear
            gear_activities = [a for a in activities_since_maintenance if a.get('gear_id') == gear_id]
            
            # Create maintenance record
            record = MaintenanceRecord(
                gear_id=gear_id,
                maintenance_type=maintenance_type,
                date=current_date,
                notes=notes,
                activities_since_last_maintenance=gear_activities
            )
            
            # Add to records
            if gear_id not in self.maintenance_records:
                self.maintenance_records[gear_id] = []
            self.maintenance_records[gear_id].append(record)
            
            # Save to file
            self._save_maintenance_records()
            
            # Calculate and log the distance
            actual_distance = record.calculate_distance()
            logger.info(f"Recorded {maintenance_type.name} maintenance for gear {gear_id}")
            logger.info(f"Distance ridden since last {maintenance_type.name}: {actual_distance:.2f} km")
            
            return True
            
        except Exception as e:
            logger.error(f"Error recording maintenance: {e}")
            return False

    def delete_maintenance_record(self, gear_id: str, record_index: int) -> bool:
        """
        Delete a maintenance record.
        
        Args:
            gear_id: Strava gear ID
            record_index: Index of the record to delete (1-based)
            
        Returns:
            bool: True if record was deleted successfully
        """
        try:
            if gear_id not in self.maintenance_records:
                logger.error(f"No maintenance records found for gear {gear_id}")
                return False
                
            records = self.maintenance_records[gear_id]
            if not 1 <= record_index <= len(records):
                logger.error(f"Invalid record index: {record_index}")
                return False
                
            # Remove the record (convert to 0-based index)
            del records[record_index - 1]
            
            # Save changes
            self._save_maintenance_records()
            
            logger.info(f"Deleted maintenance record {record_index} for gear {gear_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting maintenance record: {e}")
            return False

    def get_maintenance_history(self, gear_id: str, item: Optional[str] = None) -> List[MaintenanceRecord]:
        """
        Get maintenance history for a gear item.
        
        Args:
            gear_id: Strava gear ID
            item: Optional item to filter by (e.g., 'chain', 'tires')
            
        Returns:
            List of maintenance records
        """
        records = self.maintenance_records.get(gear_id, [])
        if item:
            records = [r for r in records if r.maintenance_type == item]
        return sorted(records, key=lambda x: x.date)

    def print_maintenance_report(self, gear_id: str, item: Optional[str] = None):
        """
        Print a maintenance history report for a gear item.
        
        Args:
            gear_id: Strava gear ID
            item: Optional item to filter by
        """
        records = self.get_maintenance_history(gear_id, item)
        
        if not records:
            print(f"\nNo maintenance records found for gear {gear_id}" + 
                  (f" and item {item}" if item else ""))
            return
        
        print(f"\nMaintenance History Report")
        print("=" * 80)
        
        # Get gear details
        gear_details = self.get_gear_details(gear_id)
        if gear_details:
            print(f"Gear: {gear_details.get('name', 'Unknown')}")
            print(f"Brand: {gear_details.get('brand_name', 'Unknown')}")
            print(f"Model: {gear_details.get('model_name', 'Unknown')}")
        
        print("\nMaintenance Records:")
        print("-" * 80)
        
        for i, record in enumerate(records, 1):
            print(f"\nRecord {i}:")
            print(f"Type: {MaintenanceType.get_all_types()[record.maintenance_type]}")
            print(f"Date: {record.date.strftime('%Y-%m-%d')}")
            print(f"Distance ridden: {record.calculate_distance():.2f} km")
            
            # Show date range
            if record.activities_since_last_maintenance:
                first_activity = min(record.activities_since_last_maintenance, 
                                   key=lambda x: x['start_date'])
                last_activity = max(record.activities_since_last_maintenance, 
                                  key=lambda x: x['start_date'])
                print(f"Date range: {first_activity['start_date'][:10]} to {last_activity['start_date'][:10]}")
        
        print("=" * 80)

    def print_gear_report(self, gear_usage: Dict[str, GearUsage], gear_details: Dict[str, Dict]):
        """
        Print a comprehensive gear usage report.
        
        Args:
            gear_usage: Dictionary of gear usage statistics
            gear_details: Dictionary of gear details
        """
        print("\nStrava Gear Wear Monitor Report")
        print("=" * 80)
        
        for gear_id, usage in gear_usage.items():
            details = gear_details.get(gear_id, {})
            
            print(f"\nGear ID: {gear_id}")
            print("-" * 40)
            
            # Basic gear information
            print(f"Name: {details.get('name', 'Unknown')}")
            print(f"Brand: {details.get('brand_name', 'Unknown')}")
            print(f"Model: {details.get('model_name', 'Unknown')}")
            
            # Usage statistics
            print(f"\nUsage Statistics:")
            print(f"Sport Types: {', '.join(sorted(usage.sport_types))}")
            print(f"Total Distance: {usage.total_distance_km:.2f} km")
            print(f"Number of Activities: {usage.activities_count}")
            
            if usage.first_activity_date and usage.last_activity_date:
                # Ensure timezone-aware dates for display
                first_date = usage.first_activity_date.replace(tzinfo=datetime.now().astimezone().tzinfo)
                last_date = usage.last_activity_date.replace(tzinfo=datetime.now().astimezone().tzinfo)
                print(f"First Used: {first_date.strftime('%Y-%m-%d')}")
                print(f"Last Used: {last_date.strftime('%Y-%m-%d')}")
            
            # Maintenance history
            if usage.maintenance_history:
                print("\nMaintenance History:")
                # Ensure all dates are timezone-aware before sorting
                sorted_records = sorted(
                    usage.maintenance_history,
                    key=lambda x: x.date.replace(tzinfo=datetime.now().astimezone().tzinfo)
                )
                for record in sorted_records:
                    actual_distance = record.calculate_distance()
                    print(f"- {record.date.strftime('%Y-%m-%d')}: {MaintenanceType.get_all_types()[record.maintenance_type]} "
                          f"(ridden {actual_distance:.2f} km)")
            
            # Strava's recorded distance
            strava_distance = details.get('distance', 0) / 1000  # Convert to km
            print(f"\nTotal Distance (Strava): {strava_distance:.2f} km")
            
            # Additional gear information
            if details.get('description'):
                print(f"\nDescription: {details['description']}")
            if details.get('frame_type'):
                print(f"Frame Type: {details['frame_type']}")
            if details.get('primary'):
                print("Status: Primary gear")
            
            print("-" * 80)

    def get_available_bikes(self) -> Dict[str, Dict]:
        """
        Get a list of available bikes with their details.
        
        Returns:
            Dictionary mapping bike numbers to bike details including gear_id, name, and distance
        """
        activities = self.get_all_activities()
        gear_usage = self.analyze_gear_usage(activities)
        gear_details = self.get_all_gear_details(gear_usage)
        
        bikes = {}
        for i, (gear_id, usage) in enumerate(gear_usage.items(), 1):
            details = gear_details.get(gear_id, {})
            bikes[str(i)] = {
                'gear_id': gear_id,
                'name': details.get('name', 'Unknown Bike'),
                'distance': usage.total_distance_km,
                'brand': details.get('brand_name', 'Unknown'),
                'model': details.get('model_name', 'Unknown')
            }
        return bikes

    def display_available_bikes(self) -> Dict[str, Dict]:
        """
        Display available bikes and return the bikes dictionary.
        
        Returns:
            Dictionary of available bikes
        """
        bikes = self.get_available_bikes()
        
        print("\nAvailable Bikes:")
        print("-" * 80)
        for num, bike in bikes.items():
            print(f"{num}. {bike['name']} ({bike['brand']} {bike['model']}) - {bike['distance']:.2f} km")
        print("-" * 80)
        
        return bikes

    def get_bike_selection(self, bikes: Dict[str, Dict]) -> Optional[str]:
        """
        Get bike selection from user.
        
        Args:
            bikes: Dictionary of available bikes
            
        Returns:
            Selected gear_id or None if invalid selection
        """
        while True:
            choice = input("\nSelect a bike (number) or 'q' to quit: ").strip()
            if choice.lower() == 'q':
                return None
            if choice in bikes:
                return bikes[choice]['gear_id']
            print("Invalid selection. Please try again.")

    def get_maintained_items(self, gear_id: str) -> Dict[str, List[MaintenanceRecord]]:
        """
        Get a dictionary of maintained items and their records for a gear.
        
        Args:
            gear_id: Strava gear ID
            
        Returns:
            Dictionary mapping item names to their maintenance records
        """
        records = self.maintenance_records.get(gear_id, [])
        items = {}
        for record in records:
            if record.maintenance_type not in items:
                items[record.maintenance_type] = []
            items[record.maintenance_type].append(record)
        return items

    def display_maintained_items(self, gear_id: str):
        """
        Display existing maintenance history for a gear.
        
        Args:
            gear_id: Strava gear ID
        """
        items = self.get_maintained_items(gear_id)
        if not items:
            print("\nNo maintenance records found for this bike.")
            return
            
        print("\nExisting Maintenance History:")
        print("-" * 80)
        for item, records in sorted(items.items()):
            print(f"\n{item.name}:")
            for record in sorted(records, key=lambda x: x.date):
                actual_distance = record.calculate_distance()
                print(f"  - {record.date.strftime('%Y-%m-%d')}: {MaintenanceType.get_all_types()[item]} "
                      f"(ridden {actual_distance:.2f} km)")
        print("-" * 80)

    def display_service_intervals(self, gear_id: str, item: Optional[str] = None):
        """
        Display existing service intervals for a gear.
        
        Args:
            gear_id: Strava gear ID
            item: Optional item to filter by
        """
        intervals = self.get_service_intervals(gear_id, item)
        if not intervals:
            print("\nNo service intervals found for this bike.")
            return
            
        print("\nExisting Service Intervals:")
        print("-" * 80)
        for interval in intervals:
            print(f"\nItem: {interval.item}")
            print(f"Action: {interval.action}")
            print(f"Interval: Every {interval.interval_value} " + 
                  ("weeks" if interval.interval_type == 'time' else "kilometers"))
            
            if interval.last_service_date:
                print(f"Last Service: {interval.last_service_date.strftime('%Y-%m-%d')}")
                if interval.last_service_distance is not None:
                    print(f"Distance at Last Service: {interval.last_service_distance:.2f} km")
                
                # Calculate next service
                if interval.interval_type == 'time':
                    next_service = interval.last_service_date + timedelta(weeks=interval.interval_value)
                    print(f"Next Service Due: {next_service.strftime('%Y-%m-%d')}")
                else:  # distance
                    next_service_distance = interval.last_service_distance + interval.interval_value
                    print(f"Next Service Due: At {next_service_distance:.2f} km")
        print("-" * 80)

    def add_service_interval(self, gear_id: str, item: str, interval_type: str, 
                           interval_value: float, action: str) -> bool:
        """
        Add a service interval for a gear item.
        
        Args:
            gear_id: Strava gear ID
            item: Item to service (e.g., 'chain', 'tires')
            interval_type: 'time' (weeks) or 'distance' (kilometers)
            interval_value: Number of weeks or kilometers
            action: What to do (e.g., 'replace', 'service', 'check')
            
        Returns:
            bool: True if interval was added successfully
        """
        try:
            # Check if the item has any maintenance records
            if not self.get_maintenance_history(gear_id, item):
                logger.error(f"Cannot add service interval: No maintenance records found for {item}")
                print(f"\nError: No maintenance records found for {item}. Please record at least one maintenance event before setting up service intervals.")
                return False

            # Validate interval type
            if interval_type not in ['time', 'distance']:
                logger.error("Invalid interval type. Must be 'time' or 'distance'")
                return False

            # Validate interval value
            if interval_value <= 0:
                logger.error("Interval value must be positive")
                return False

            # Get the latest maintenance record to set as last service
            maintenance_records = self.get_maintenance_history(gear_id, item)
            latest_record = max(maintenance_records, key=lambda x: x.date)
            
            # Create service interval
            interval = ServiceInterval(
                gear_id=gear_id,
                item=item.lower(),
                interval_type=interval_type,
                interval_value=interval_value,
                action=action.lower(),
                last_service_date=latest_record.date,
                last_service_distance=latest_record.calculate_distance()
            )

            # Add to intervals
            if gear_id not in self.service_intervals:
                self.service_intervals[gear_id] = []
            self.service_intervals[gear_id].append(interval)

            # Save to file
            self._save_service_intervals()

            logger.info(f"Added service interval for {item} on gear {gear_id}")
            return True

        except Exception as e:
            logger.error(f"Error adding service interval: {e}")
            return False

    def get_service_intervals(self, gear_id: str, item: Optional[str] = None) -> List[ServiceInterval]:
        """
        Get service intervals for a gear item.
        
        Args:
            gear_id: Strava gear ID
            item: Optional item to filter by
            
        Returns:
            List of service intervals
        """
        intervals = self.service_intervals.get(gear_id, [])
        if item:
            intervals = [i for i in intervals if i.item == item.lower()]
        return sorted(intervals, key=lambda x: x.item)

    def print_service_intervals(self, gear_id: str, item: Optional[str] = None):
        """
        Print service intervals for a gear item.
        
        Args:
            gear_id: Strava gear ID
            item: Optional item to filter by
        """
        intervals = self.get_service_intervals(gear_id, item)
        
        if not intervals:
            print(f"\nNo service intervals found for gear {gear_id}" + 
                  (f" and item {item}" if item else ""))
            return

        # Get gear details
        gear_details = self.get_gear_details(gear_id)
        
        print("\nService Intervals Report")
        print("=" * 80)
        if gear_details:
            print(f"Gear: {gear_details.get('name', 'Unknown')}")
            print(f"Brand: {gear_details.get('brand_name', 'Unknown')}")
            print(f"Model: {gear_details.get('model_name', 'Unknown')}")
        
        print("\nService Intervals:")
        print("-" * 80)
        
        for interval in intervals:
            print(f"\nItem: {interval.item}")
            print(f"Action: {interval.action}")
            print(f"Interval: Every {interval.interval_value} " + 
                  ("weeks" if interval.interval_type == 'time' else "kilometers"))
            
            if interval.last_service_date:
                print(f"Last Service: {interval.last_service_date.strftime('%Y-%m-%d')}")
                if interval.last_service_distance is not None:
                    print(f"Distance at Last Service: {interval.last_service_distance:.2f} km")
                
                # Calculate next service
                if interval.interval_type == 'time':
                    next_service = interval.last_service_date + timedelta(weeks=interval.interval_value)
                    print(f"Next Service Due: {next_service.strftime('%Y-%m-%d')}")
                else:  # distance
                    next_service_distance = interval.last_service_distance + interval.interval_value
                    print(f"Next Service Due: At {next_service_distance:.2f} km")
        
        print("=" * 80)

    def _load_components(self):
        """Load components from file."""
        if self.components_file.exists():
            try:
                with open(self.components_file, 'r') as f:
                    data = json.load(f)
                    self.components = {
                        comp_id: Component(
                            id=comp_data['id'],
                            name=comp_data['name'],
                            brand=comp_data['brand'],
                            model=comp_data['model'],
                            installation_date=datetime.fromisoformat(comp_data['installation_date']).replace(tzinfo=datetime.now().astimezone().tzinfo),
                            gear_id=comp_data['gear_id'],
                            status=comp_data['status'],
                            notes=comp_data.get('notes'),
                            purchase_date=datetime.fromisoformat(comp_data['purchase_date']).replace(tzinfo=datetime.now().astimezone().tzinfo) if comp_data.get('purchase_date') else None,
                            purchase_price=comp_data.get('purchase_price'),
                            mileage_at_installation=comp_data['mileage_at_installation'],
                            current_mileage=comp_data['current_mileage']
                        )
                        for comp_id, comp_data in data.items()
                    }
            except Exception as e:
                logger.error(f"Error loading components: {e}")
                self.components = {}

    def _save_components(self):
        """Save components to file."""
        try:
            data = {
                comp_id: {
                    'id': comp.id,
                    'name': comp.name,
                    'brand': comp.brand,
                    'model': comp.model,
                    'installation_date': comp.installation_date.isoformat(),
                    'gear_id': comp.gear_id,
                    'status': comp.status,
                    'notes': comp.notes,
                    'purchase_date': comp.purchase_date.isoformat() if comp.purchase_date else None,
                    'purchase_price': comp.purchase_price,
                    'mileage_at_installation': comp.mileage_at_installation,
                    'current_mileage': comp.current_mileage
                }
                for comp_id, comp in self.components.items()
            }
            with open(self.components_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving components: {e}")

    def _load_component_swaps(self):
        """Load component swaps from file."""
        if self.component_swaps_file.exists():
            try:
                with open(self.component_swaps_file, 'r') as f:
                    data = json.load(f)
                    self.component_swaps = [
                        ComponentSwap(
                            date=datetime.fromisoformat(swap['date']).replace(tzinfo=datetime.now().astimezone().tzinfo),
                            gear_id=swap['gear_id'],
                            component_id=swap['component_id'],
                            old_component_id=swap.get('old_component_id'),
                            action=swap['action'],
                            mileage=swap['mileage'],
                            notes=swap.get('notes')
                        )
                        for swap in data
                    ]
            except Exception as e:
                logger.error(f"Error loading component swaps: {e}")
                self.component_swaps = []

    def _save_component_swaps(self):
        """Save component swaps to file."""
        try:
            data = [
                {
                    'date': swap.date.isoformat(),
                    'gear_id': swap.gear_id,
                    'component_id': swap.component_id,
                    'old_component_id': swap.old_component_id,
                    'action': swap.action,
                    'mileage': swap.mileage,
                    'notes': swap.notes
                }
                for swap in self.component_swaps
            ]
            with open(self.component_swaps_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving component swaps: {e}")

    def add_component(self, name: str, brand: str, model: str, gear_id: str,
                     purchase_date: Optional[datetime] = None,
                     purchase_price: Optional[float] = None,
                     notes: Optional[str] = None) -> Optional[str]:
        """
        Add a new component to the system.
        
        Args:
            name: Component name
            brand: Brand name
            model: Model name
            gear_id: ID of the bike it's installed on
            purchase_date: Optional purchase date
            purchase_price: Optional purchase price
            notes: Optional notes
            
        Returns:
            Component ID if successful, None otherwise
        """
        try:
            # Generate unique component ID
            component_id = f"{brand.lower()}_{model.lower()}_{int(time.time())}"
            
            # Get current mileage for the bike
            gear_usage = self.analyze_gear_usage(self.get_all_activities())
            current_mileage = gear_usage.get(gear_id, GearUsage(gear_id, set(), 0, 0, 0)).total_distance_km
            
            component = Component(
                id=component_id,
                name=name,
                brand=brand,
                model=model,
                installation_date=datetime.now().astimezone(),
                gear_id=gear_id,
                status="active",
                notes=notes,
                purchase_date=purchase_date,
                purchase_price=purchase_price,
                mileage_at_installation=current_mileage,
                current_mileage=current_mileage
            )
            
            self.components[component_id] = component
            
            # Record the component swap
            swap = ComponentSwap(
                date=component.installation_date,
                gear_id=gear_id,
                component_id=component_id,
                old_component_id=None,
                action="install",
                mileage=current_mileage,
                notes="Initial installation"
            )
            self.component_swaps.append(swap)
            
            # Save changes
            self._save_components()
            self._save_component_swaps()
            
            return component_id
            
        except Exception as e:
            logger.error(f"Error adding component: {e}")
            return None

    def swap_component(self, gear_id: str, old_component_id: str, new_component_id: Optional[str],
                      action: str = "remove", notes: Optional[str] = None) -> bool:
        """
        Swap a component on a bike.
        
        Args:
            gear_id: ID of the bike
            old_component_id: ID of the component being removed
            new_component_id: ID of the new component (None if not installing a new one)
            action: What to do with the old component ('remove' or 'retire')
            notes: Optional notes about the swap
            
        Returns:
            bool: True if successful
        """
        try:
            if old_component_id not in self.components:
                logger.error(f"Component {old_component_id} not found")
                return False
                
            old_component = self.components[old_component_id]
            if old_component.gear_id != gear_id:
                logger.error(f"Component {old_component_id} is not installed on bike {gear_id}")
                return False
                
            # Get current mileage
            gear_usage = self.analyze_gear_usage(self.get_all_activities())
            current_mileage = gear_usage.get(gear_id, GearUsage(gear_id, set(), 0, 0, 0)).total_distance_km
            
            # Update old component
            old_component.status = "retired" if action == "retire" else "in_inventory"
            old_component.current_mileage = current_mileage
            
            # Record the swap
            swap = ComponentSwap(
                date=datetime.now().astimezone(),
                gear_id=gear_id,
                component_id=old_component_id,
                old_component_id=None,
                action=action,
                mileage=current_mileage,
                notes=notes
            )
            self.component_swaps.append(swap)
            
            # If installing a new component
            if new_component_id:
                if new_component_id not in self.components:
                    logger.error(f"New component {new_component_id} not found")
                    return False
                    
                new_component = self.components[new_component_id]
                if new_component.status != "in_inventory":
                    logger.error(f"New component {new_component_id} is not in inventory")
                    return False
                    
                # Update new component
                new_component.status = "active"
                new_component.gear_id = gear_id
                new_component.installation_date = datetime.now().astimezone()
                new_component.mileage_at_installation = current_mileage
                new_component.current_mileage = current_mileage
                
                # Record the installation
                swap = ComponentSwap(
                    date=new_component.installation_date,
                    gear_id=gear_id,
                    component_id=new_component_id,
                    old_component_id=old_component_id,
                    action="install",
                    mileage=current_mileage,
                    notes=notes
                )
                self.component_swaps.append(swap)
            
            # Save changes
            self._save_components()
            self._save_component_swaps()
            
            return True
            
        except Exception as e:
            logger.error(f"Error swapping component: {e}")
            return False

    def get_bike_components(self, gear_id: str, status: Optional[str] = None) -> List[Component]:
        """
        Get components for a bike.
        
        Args:
            gear_id: ID of the bike
            status: Optional status filter ('active', 'in_inventory', 'retired')
            
        Returns:
            List of components
        """
        components = [comp for comp in self.components.values() if comp.gear_id == gear_id]
        if status:
            components = [comp for comp in components if comp.status == status]
        return sorted(components, key=lambda x: x.installation_date, reverse=True)

    def get_inventory_components(self) -> List[Component]:
        """Get all components in inventory."""
        return [comp for comp in self.components.values() if comp.status == "in_inventory"]

    def get_retired_components(self) -> List[Component]:
        """Get all retired components."""
        return [comp for comp in self.components.values() if comp.status == "retired"]

    def select_active_bike(self) -> bool:
        """
        Select an active bike from available bikes.
        
        Returns:
            bool: True if a bike was selected, False if user chose to exit
        """
        bikes = self.get_available_bikes()
        if not bikes:
            print("\nNo bikes found in your Strava activities.")
            return False
            
        print("\nAvailable Bikes:")
        print("-" * 80)
        for num, bike in bikes.items():
            print(f"{num}. {bike['name']} ({bike['brand']} {bike['model']}) - {bike['distance']:.2f} km")
        print("-" * 80)
        
        while True:
            choice = input("\nSelect a bike (number) or 'q' to quit: ").strip()
            if choice.lower() == 'q':
                return False
            if choice in bikes:
                self.active_bike = bikes[choice]
                print(f"\nActive bike set to: {self.active_bike['name']}")
                return True
            print("Invalid selection. Please try again.")

    def clear_all_data(self) -> bool:
        """
        Clear all user data (components, swaps, maintenance, service intervals).
        
        Returns:
            bool: True if all data was cleared successfully
        """
        try:
            # Clear in-memory data
            self.components.clear()
            self.component_swaps.clear()
            self.maintenance_records.clear()
            self.service_intervals.clear()
            
            # Delete files
            files_to_delete = [
                self.components_file,
                self.component_swaps_file,
                self.maintenance_file,
                self.intervals_file
            ]
            
            for file in files_to_delete:
                if file.exists():
                    file.unlink()
                    logger.info(f"Deleted file: {file}")
            
            logger.info(f"Cleared all data for user: {self.user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error clearing data: {e}")
            return False

    def clear_components(self) -> bool:
        """
        Clear only component-related data (components and swaps).
        
        Returns:
            bool: True if component data was cleared successfully
        """
        try:
            # Clear in-memory data
            self.components.clear()
            self.component_swaps.clear()
            
            # Delete files
            if self.components_file.exists():
                self.components_file.unlink()
            if self.component_swaps_file.exists():
                self.component_swaps_file.unlink()
            
            logger.info(f"Cleared component data for user: {self.user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error clearing component data: {e}")
            return False

    def clear_maintenance(self) -> bool:
        """
        Clear only maintenance records.
        
        Returns:
            bool: True if maintenance data was cleared successfully
        """
        try:
            # Clear in-memory data
            self.maintenance_records.clear()
            
            # Delete file
            if self.maintenance_file.exists():
                self.maintenance_file.unlink()
            
            logger.info(f"Cleared maintenance data for user: {self.user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error clearing maintenance data: {e}")
            return False

    def clear_service_intervals(self) -> bool:
        """
        Clear only service intervals.
        
        Returns:
            bool: True if service interval data was cleared successfully
        """
        try:
            # Clear in-memory data
            self.service_intervals.clear()
            
            # Delete file
            if self.intervals_file.exists():
                self.intervals_file.unlink()
            
            logger.info(f"Cleared service interval data for user: {self.user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error clearing service interval data: {e}")
            return False

    def _load_sync_state(self):
        """Load sync state from file."""
        if self.sync_state_file.exists():
            try:
                with open(self.sync_state_file, 'r') as f:
                    data = json.load(f)
                    self.sync_state = SyncState(
                        last_sync_time=datetime.fromisoformat(data['last_sync_time']).replace(tzinfo=datetime.now().astimezone().tzinfo),
                        latest_activity_id=data.get('latest_activity_id'),
                        latest_activity_date=datetime.fromisoformat(data['latest_activity_date']).replace(tzinfo=datetime.now().astimezone().tzinfo) if data.get('latest_activity_date') else None
                    )
            except Exception as e:
                logger.error(f"Error loading sync state: {e}")
                self.sync_state = None
        else:
            self.sync_state = None

    def _save_sync_state(self):
        """Save sync state to file."""
        try:
            if self.sync_state:
                data = {
                    'last_sync_time': self.sync_state.last_sync_time.isoformat(),
                    'latest_activity_id': self.sync_state.latest_activity_id,
                    'latest_activity_date': self.sync_state.latest_activity_date.isoformat() if self.sync_state.latest_activity_date else None
                }
                with open(self.sync_state_file, 'w') as f:
                    json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving sync state: {e}")

    def get_latest_activity(self) -> Optional[Dict]:
        """
        Get the latest activity from Strava.
        
        Returns:
            Optional[Dict]: Latest activity data or None if error occurs
        """
        try:
            url = f"{self.base_url}/athlete/activities"
            params = {'per_page': 1}  # We only need the latest activity
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            activities = response.json()
            return activities[0] if activities else None
        except Exception as e:
            logger.error(f"Error getting latest activity: {e}")
            return None

    def needs_sync(self) -> bool:
        """
        Check if activities need to be synced.
        
        Returns:
            bool: True if sync is needed, False otherwise
        """
        if not self.sync_state:
            return True
            
        # Check if we've synced today
        now = datetime.now().astimezone()
        last_sync = self.sync_state.last_sync_time
        if now.date() > last_sync.date():
            # Only sync if it's night time (between 8 PM and 6 AM)
            if 20 <= now.hour or now.hour < 6:
                # Get latest activity from Strava
                latest_activity = self.get_latest_activity()
                if not latest_activity:
                    return False
                    
                # Check if we have new activities
                return latest_activity['id'] != self.sync_state.latest_activity_id
                
        return False

    def sync_activities(self) -> bool:
        """
        Sync activities from Strava if needed.
        Only updates if it's night time and there are new activities.
        
        Returns:
            bool: True if sync was successful or not needed
        """
        if not self.needs_sync():
            logger.info("No sync needed at this time.")
            return True
            
        try:
            logger.info("Starting activity sync...")
            
            # Get latest activity from Strava
            latest_activity = self.get_latest_activity()
            if not latest_activity:
                logger.error("Failed to get latest activity")
                return False
                
            # If we have a previous sync state, only get new activities
            if self.sync_state and self.sync_state.latest_activity_id:
                # Find the latest activity we know about
                known_activity = None
                for activity in self.activities_cache:
                    if activity['id'] == self.sync_state.latest_activity_id:
                        known_activity = activity
                        break
                
                if known_activity:
                    # Get activities after the known activity
                    new_activities = self._get_activities_after_date(
                        datetime.fromisoformat(known_activity['start_date'].replace('Z', '+00:00'))
                    )
                    if new_activities:
                        self.activities_cache.extend(new_activities)
                        logger.info(f"Added {len(new_activities)} new activities")
                else:
                    # If we can't find the known activity, get all activities
                    self.activities_cache = self.get_all_activities()
            else:
                # First sync, get all activities
                self.activities_cache = self.get_all_activities()
            
            # Update sync state
            self.sync_state = SyncState(
                last_sync_time=datetime.now().astimezone(),
                latest_activity_id=str(latest_activity['id']),
                latest_activity_date=datetime.fromisoformat(latest_activity['start_date'].replace('Z', '+00:00'))
            )
            self._save_sync_state()
            
            # Update gear usage
            self._update_gear_usage()
            
            logger.info("Activity sync completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error syncing activities: {e}")
            return False

    def _get_activities_after_date(self, date: datetime) -> List[Dict]:
        """
        Get activities after a specific date.
        
        Args:
            date: The date to get activities after
            
        Returns:
            List[Dict]: List of activities after the date
        """
        try:
            url = f"{self.base_url}/athlete/activities"
            all_activities = []
            page = 1
            per_page = 200
            
            while True:
                params = {
                    'per_page': per_page,
                    'page': page,
                    'after': int(date.timestamp())
                }
                
                response = requests.get(url, headers=self.headers, params=params)
                response.raise_for_status()
                
                activities = response.json()
                if not activities:
                    break
                    
                all_activities.extend(activities)
                page += 1
                
                # Respect API rate limits
                time.sleep(0.1)
            
            return all_activities
            
        except Exception as e:
            logger.error(f"Error getting activities after date: {e}")
            return []

    def _update_gear_usage(self):
        """Update gear usage statistics based on cached activities."""
        try:
            # Update gear usage
            gear_usage = self.analyze_gear_usage(self.activities_cache)
            
            # Update component mileage
            for component in self.components.values():
                if component.status == "active":
                    usage = gear_usage.get(component.gear_id)
                    if usage:
                        component.current_mileage = usage.total_distance_km
            
            # Save updated components
            self._save_components()
            
            logger.info("Gear usage updated successfully")
            
        except Exception as e:
            logger.error(f"Error updating gear usage: {e}")

    def authenticate(self) -> bool:
        """
        Authenticate with Strava API and initialize user data.
        If refresh token exists, use it. Otherwise, start OAuth flow.
        
        Returns:
            bool: True if authentication successful, False otherwise
        """
        if self.refresh_token:
            logger.info("Using existing refresh token...")
            if self.refresh_access_token():
                logger.info("Successfully refreshed access token!")
                return self.initialize_user_data()
            logger.error("Failed to refresh token. Starting new authentication...")
        
        # Start OAuth flow
        logger.info("Starting new authentication...")
        auth_url = self.get_authorization_url()
        print("\nPlease visit this URL to authorize the application:")
        print(auth_url)
        print("\nAfter authorizing, you'll be redirected to a URL like:")
        print("http://localhost/exchange_token?state=&code=AUTHORIZATION_CODE&scope=read")
        print()
        
        try:
            webbrowser.open(auth_url)
            print("Authorization URL opened in your browser.")
        except:
            print("Could not open browser automatically. Please copy the URL above.")
        
        authorization_code = input("Enter the 'code' parameter from the redirect URL: ").strip()
        
        if self.exchange_code_for_tokens(authorization_code):
            logger.info("Successfully obtained tokens!")
            print(f"Save this refresh token for future use: {self.refresh_token}")
            return self.initialize_user_data()
        
        logger.error("Failed to obtain tokens.")
        return False

    def get_authorization_url(self) -> str:
        """Generate the authorization URL for user to visit."""
        base_url = "https://www.strava.com/oauth/authorize"
        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'redirect_uri': self.redirect_uri,
            'approval_prompt': 'force',
            'scope': 'read,activity:read_all'  # Added activity:read_all scope
        }
        return f"{base_url}?{urlencode(params)}"

    def initialize_user_data(self) -> bool:
        """
        Initialize user data after authentication.
        Sets up user_id and file paths, then loads data.
        
        Returns:
            bool: True if initialization was successful
        """
        try:
            # Get athlete info using the new make_authenticated_request method
            response = self.make_authenticated_request('/athlete')
            if not response:
                logger.error("Failed to get athlete info from Strava")
                return False
                
            athlete_data = response.json()
            athlete_id = str(athlete_data.get('id'))
            if not athlete_id:
                logger.error("Failed to get athlete ID from Strava")
                return False
                
            # Set user ID and file paths
            self.user_id = f"strava_{athlete_id}"
            self.maintenance_file = Path(f"{self.user_id}_gear_maintenance.json")
            self.intervals_file = Path(f"{self.user_id}_service_intervals.json")
            self.components_file = Path(f"{self.user_id}_components.json")
            self.component_swaps_file = Path(f"{self.user_id}_component_swaps.json")
            self.sync_state_file = Path(f"{self.user_id}_sync_state.json")
            
            # Load data
            self._load_maintenance_records()
            self._load_service_intervals()
            self._load_components()
            self._load_component_swaps()
            self._load_sync_state()
            
            logger.info(f"Initialized data for user: {self.user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error initializing user data: {e}")
            return False

    def _load_maintenance_records(self):
        """Load maintenance records from file."""
        if self.maintenance_file.exists():
            try:
                with open(self.maintenance_file, 'r') as f:
                    data = json.load(f)
                    self.maintenance_records = {
                        gear_id: [
                            MaintenanceRecord(
                                gear_id=record['gear_id'],
                                maintenance_type=MaintenanceType[record['maintenance_type']],
                                date=datetime.fromisoformat(record['date']).replace(tzinfo=datetime.now().astimezone().tzinfo),
                                notes=record.get('notes'),
                                distance_at_maintenance=record['distance_at_maintenance'],
                                activities_since_last_maintenance=[
                                    {
                                        'id': activity['id'],
                                        'start_date': activity['start_date'],
                                        'distance': activity['distance']
                                    }
                                    for activity in record.get('activities_since_last_maintenance', [])
                                ]
                            )
                            for record in records
                        ]
                        for gear_id, records in data.items()
                    }
            except Exception as e:
                logger.error(f"Error loading maintenance records: {e}")
                self.maintenance_records = {}

    def _save_maintenance_records(self):
        """Save maintenance records to file."""
        try:
            data = {
                gear_id: [
                    {
                        'gear_id': record.gear_id,
                        'maintenance_type': record.maintenance_type.name,
                        'date': record.date.isoformat(),
                        'notes': record.notes,
                        'distance_at_maintenance': record.calculate_distance(),
                        'activities_since_last_maintenance': [
                            {
                                'id': activity['id'],
                                'start_date': activity['start_date'],
                                'distance': activity['distance']
                            }
                            for activity in record.activities_since_last_maintenance
                        ]
                    }
                    for record in records
                ]
                for gear_id, records in self.maintenance_records.items()
            }
            with open(self.maintenance_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving maintenance records: {e}")

    def _load_service_intervals(self):
        """Load service intervals from file."""
        if self.intervals_file.exists():
            try:
                with open(self.intervals_file, 'r') as f:
                    data = json.load(f)
                    self.service_intervals = {
                        gear_id: [
                            ServiceInterval(
                                gear_id=interval['gear_id'],
                                item=interval['item'],
                                interval_type=interval['interval_type'],
                                interval_value=interval['interval_value'],
                                action=interval['action'],
                                last_service_date=datetime.fromisoformat(interval['last_service_date']).replace(tzinfo=datetime.now().astimezone().tzinfo) if interval.get('last_service_date') else None,
                                last_service_distance=interval.get('last_service_distance')
                            )
                            for interval in intervals
                        ]
                        for gear_id, intervals in data.items()
                    }
            except Exception as e:
                logger.error(f"Error loading service intervals: {e}")
                self.service_intervals = {}

    def _save_service_intervals(self):
        """Save service intervals to file."""
        try:
            data = {
                gear_id: [
                    {
                        'gear_id': interval.gear_id,
                        'item': interval.item,
                        'interval_type': interval.interval_type,
                        'interval_value': interval.interval_value,
                        'action': interval.action,
                        'last_service_date': interval.last_service_date.isoformat() if interval.last_service_date else None,
                        'last_service_distance': interval.last_service_distance
                    }
                    for interval in intervals
                ]
                for gear_id, intervals in self.service_intervals.items()
            }
            with open(self.intervals_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving service intervals: {e}")

    def _load_components(self):
        """Load components from file."""
        if self.components_file.exists():
            try:
                with open(self.components_file, 'r') as f:
                    data = json.load(f)
                    self.components = {
                        comp_id: Component(
                            id=comp_data['id'],
                            name=comp_data['name'],
                            brand=comp_data['brand'],
                            model=comp_data['model'],
                            installation_date=datetime.fromisoformat(comp_data['installation_date']).replace(tzinfo=datetime.now().astimezone().tzinfo),
                            gear_id=comp_data['gear_id'],
                            status=comp_data['status'],
                            notes=comp_data.get('notes'),
                            purchase_date=datetime.fromisoformat(comp_data['purchase_date']).replace(tzinfo=datetime.now().astimezone().tzinfo) if comp_data.get('purchase_date') else None,
                            purchase_price=comp_data.get('purchase_price'),
                            mileage_at_installation=comp_data['mileage_at_installation'],
                            current_mileage=comp_data['current_mileage']
                        )
                        for comp_id, comp_data in data.items()
                    }
            except Exception as e:
                logger.error(f"Error loading components: {e}")
                self.components = {}

    def _save_components(self):
        """Save components to file."""
        try:
            data = {
                comp_id: {
                    'id': comp.id,
                    'name': comp.name,
                    'brand': comp.brand,
                    'model': comp.model,
                    'installation_date': comp.installation_date.isoformat(),
                    'gear_id': comp.gear_id,
                    'status': comp.status,
                    'notes': comp.notes,
                    'purchase_date': comp.purchase_date.isoformat() if comp.purchase_date else None,
                    'purchase_price': comp.purchase_price,
                    'mileage_at_installation': comp.mileage_at_installation,
                    'current_mileage': comp.current_mileage
                }
                for comp_id, comp in self.components.items()
            }
            with open(self.components_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving components: {e}")

    def _load_component_swaps(self):
        """Load component swaps from file."""
        if self.component_swaps_file.exists():
            try:
                with open(self.component_swaps_file, 'r') as f:
                    data = json.load(f)
                    self.component_swaps = [
                        ComponentSwap(
                            date=datetime.fromisoformat(swap['date']).replace(tzinfo=datetime.now().astimezone().tzinfo),
                            gear_id=swap['gear_id'],
                            component_id=swap['component_id'],
                            old_component_id=swap.get('old_component_id'),
                            action=swap['action'],
                            mileage=swap['mileage'],
                            notes=swap.get('notes')
                        )
                        for swap in data
                    ]
            except Exception as e:
                logger.error(f"Error loading component swaps: {e}")
                self.component_swaps = []

    def _save_component_swaps(self):
        """Save component swaps to file."""
        try:
            data = [
                {
                    'date': swap.date.isoformat(),
                    'gear_id': swap.gear_id,
                    'component_id': swap.component_id,
                    'old_component_id': swap.old_component_id,
                    'action': swap.action,
                    'mileage': swap.mileage,
                    'notes': swap.notes
                }
                for swap in self.component_swaps
            ]
            with open(self.component_swaps_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving component swaps: {e}")

    def _load_sync_state(self):
        """Load sync state from file."""
        if self.sync_state_file.exists():
            try:
                with open(self.sync_state_file, 'r') as f:
                    data = json.load(f)
                    self.sync_state = SyncState(
                        last_sync_time=datetime.fromisoformat(data['last_sync_time']).replace(tzinfo=datetime.now().astimezone().tzinfo),
                        latest_activity_id=data.get('latest_activity_id'),
                        latest_activity_date=datetime.fromisoformat(data['latest_activity_date']).replace(tzinfo=datetime.now().astimezone().tzinfo) if data.get('latest_activity_date') else None
                    )
            except Exception as e:
                logger.error(f"Error loading sync state: {e}")
                self.sync_state = None
        else:
            self.sync_state = None

    def _save_sync_state(self):
        """Save sync state to file."""
        try:
            if self.sync_state:
                data = {
                    'last_sync_time': self.sync_state.last_sync_time.isoformat(),
                    'latest_activity_id': self.sync_state.latest_activity_id,
                    'latest_activity_date': self.sync_state.latest_activity_date.isoformat() if self.sync_state.latest_activity_date else None
                }
                with open(self.sync_state_file, 'w') as f:
                    json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving sync state: {e}")

def main():
    """Example usage of the Strava Gear Monitor."""
    # Import your API credentials
    from api_keys import CLIENT_ID, CLIENT_SECRET, EXISTING_REFRESH_TOKEN
    
    # Initialize the monitor with your credentials
    monitor = StravaGearMonitor(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=EXISTING_REFRESH_TOKEN
    )
    
    # Authenticate and initialize user data
    if not monitor.authenticate():
        logger.error("Authentication failed. Exiting...")
        return
    
    print(f"\nWelcome to Strava Gear Monitor!")
    print(f"Your data will be stored under user ID: {monitor.user_id}")
    
    # Sync activities if needed
    if monitor.sync_activities():
        print("Activities are up to date.")
    else:
        print("Warning: Failed to sync activities. Some data may be outdated.")
    
    # Initial bike selection
    if not monitor.select_active_bike():
        print("No bike selected. Exiting...")
        return

    while True:
        print(f"\nStrava Gear Wear Monitor - Active Bike: {monitor.active_bike['name']}")
        print("1. Bike List")
        print("2. Maintenance")
        print("3. Service")
        print("4. Data Management")
        print("5. Exit")
        
        choice = input("\nEnter your choice (1-5): ").strip()
        
        if choice == "4":
            # Data Management submenu
            while True:
                print("\nData Management")
                print("4.1 Clear all data")
                print("4.2 Clear components only")
                print("4.3 Clear maintenance only")
                print("4.4 Clear service intervals only")
                print("4.5 Back to main menu")
                
                subchoice = input("\nEnter your choice (4.1-4.5): ").strip()
                
                if subchoice == "4.1":
                    confirm = input("\nWARNING: This will delete ALL your data. Are you sure? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if monitor.clear_all_data():
                            print("\nAll data cleared successfully.")
                            print("Please restart the application to continue.")
                            return
                        else:
                            print("\nFailed to clear data.")
                    else:
                        print("\nOperation cancelled.")
                        
                elif subchoice == "4.2":
                    confirm = input("\nWARNING: This will delete all component data. Are you sure? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if monitor.clear_components():
                            print("\nComponent data cleared successfully.")
                        else:
                            print("\nFailed to clear component data.")
                    else:
                        print("\nOperation cancelled.")
                        
                elif subchoice == "4.3":
                    confirm = input("\nWARNING: This will delete all maintenance records. Are you sure? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if monitor.clear_maintenance():
                            print("\nMaintenance data cleared successfully.")
                        else:
                            print("\nFailed to clear maintenance data.")
                    else:
                        print("\nOperation cancelled.")
                        
                elif subchoice == "4.4":
                    confirm = input("\nWARNING: This will delete all service intervals. Are you sure? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if monitor.clear_service_intervals():
                            print("\nService interval data cleared successfully.")
                        else:
                            print("\nFailed to clear service interval data.")
                    else:
                        print("\nOperation cancelled.")
                        
                elif subchoice == "4.5":
                    break
                    
                else:
                    print("Invalid choice. Please try again.")
                    
        elif choice == "5":
            print("Goodbye!")
            break
            
        elif choice == "1":
            # Bike List submenu
            while True:
                print(f"\nBike List - {monitor.active_bike['name']}")
                print("1.1 See my bikes")
                print("1.2 Change active bike")
                print("1.3 Inventory (stored components)")
                print("1.4 Retired components")
                print("1.5 Back to main menu")
                
                subchoice = input("\nEnter your choice (1.1-1.5): ").strip()
                
                if subchoice == "1.1":
                    # Show all bikes
                    bikes = monitor.get_available_bikes()
                    print("\nYour Bikes:")
                    print("-" * 80)
                    for num, bike in bikes.items():
                        print(f"{num}. {bike['name']} ({bike['brand']} {bike['model']}) - {bike['distance']:.2f} km")
                    print("-" * 80)
                    
                elif subchoice == "1.2":
                    # Change active bike
                    if monitor.select_active_bike():
                        print(f"\nActive bike changed to: {monitor.active_bike['name']}")
                    else:
                        print("\nNo bike selected. Exiting...")
                        return
                        
                elif subchoice == "1.3":
                    # Show inventory
                    components = monitor.get_inventory_components()
                    if not components:
                        print("\nNo components in inventory.")
                        continue
                        
                    print("\nInventory Components:")
                    print("-" * 80)
                    for comp in components:
                        print(f"\n{comp.name} ({comp.brand} {comp.model})")
                        print(f"Last Used: {comp.installation_date.strftime('%Y-%m-%d')}")
                        print(f"Last Mileage: {comp.current_mileage:.2f} km")
                        if comp.notes:
                            print(f"Notes: {comp.notes}")
                    print("-" * 80)
                    
                elif subchoice == "1.4":
                    # Show retired components
                    components = monitor.get_retired_components()
                    if not components:
                        print("\nNo retired components.")
                        continue
                        
                    print("\nRetired Components:")
                    print("-" * 80)
                    for comp in components:
                        print(f"\n{comp.name} ({comp.brand} {comp.model})")
                        print(f"Last Used: {comp.installation_date.strftime('%Y-%m-%d')}")
                        print(f"Final Mileage: {comp.current_mileage:.2f} km")
                        if comp.notes:
                            print(f"Notes: {comp.notes}")
                    print("-" * 80)
                    
                elif subchoice == "1.5":
                    break
                    
                else:
                    print("Invalid choice. Please try again.")
                    
        elif choice == "2":
            # Maintenance submenu
            while True:
                print(f"\nMaintenance - {monitor.active_bike['name']}")
                print("2.1 Record Maintenance")
                print("2.2 View Maintenance")
                print("2.3 Back to main menu")
                
                subchoice = input("\nEnter your choice (2.1-2.3): ").strip()
                
                if subchoice == "2.1":
                    # Show maintenance types
                    print("\nSelect maintenance type:")
                    maintenance_types = MaintenanceType.get_all_types()
                    for i, (mtype, desc) in enumerate(maintenance_types.items(), 1):
                        print(f"{i}. {desc}")
                    
                    try:
                        type_choice = int(input("\nEnter maintenance type (number): ").strip())
                        maintenance_type = MaintenanceType.get_type_by_number(type_choice)
                        if not maintenance_type:
                            print("Invalid maintenance type.")
                            continue
                            
                        notes = input("\nEnter maintenance notes (optional): ").strip() or None
                        
                        if monitor.record_maintenance(monitor.active_bike['gear_id'], maintenance_type, notes):
                            print("\nMaintenance recorded successfully!")
                        else:
                            print("\nFailed to record maintenance.")
                            
                    except ValueError:
                        print("Invalid input. Please enter a number.")
                        continue
                    
                elif subchoice == "2.2":
                    # View maintenance history
                    while True:
                        print("\nView Maintenance")
                        print("2.2.1 View all maintenance")
                        print("2.2.2 Delete a record")
                        print("2.2.3 Back")
                        
                        view_choice = input("\nEnter your choice (2.2.1-2.2.3): ").strip()
                        
                        if view_choice == "2.2.1":
                            # Show all maintenance records
                            records = monitor.maintenance_records.get(monitor.active_bike['gear_id'], [])
                            if not records:
                                print("\nNo maintenance records found.")
                                continue
                                
                            print("\nMaintenance History:")
                            print("-" * 80)
                            for i, record in enumerate(sorted(records, key=lambda x: x.date, reverse=True), 1):
                                print(f"\nRecord {i}:")
                                print(f"Type: {MaintenanceType.get_all_types()[record.maintenance_type]}")
                                print(f"Date: {record.date.strftime('%Y-%m-%d %H:%M')}")
                                if record.notes:
                                    print(f"Notes: {record.notes}")
                                print(f"Distance since last: {record.calculate_distance():.2f} km")
                            print("-" * 80)
                            
                        elif view_choice == "2.2.2":
                            # Delete a record
                            records = monitor.maintenance_records.get(monitor.active_bike['gear_id'], [])
                            if not records:
                                print("\nNo maintenance records found.")
                                continue
                                
                            print("\nSelect record to delete:")
                            for i, record in enumerate(sorted(records, key=lambda x: x.date, reverse=True), 1):
                                print(f"{i}. {record.date.strftime('%Y-%m-%d %H:%M')} - "
                                      f"{MaintenanceType.get_all_types()[record.maintenance_type]}")
                            
                            try:
                                record_choice = int(input("\nEnter record number to delete: ").strip())
                                if monitor.delete_maintenance_record(monitor.active_bike['gear_id'], record_choice):
                                    print("\nRecord deleted successfully!")
                                else:
                                    print("\nFailed to delete record.")
                            except ValueError:
                                print("Invalid input. Please enter a number.")
                                continue
                                
                        elif view_choice == "2.2.3":
                            break
                            
                        else:
                            print("Invalid choice. Please try again.")
                    
                elif subchoice == "2.3":
                    break
                    
                else:
                    print("Invalid choice. Please try again.")
                    
        elif choice == "3":
            # Service submenu
            while True:
                print(f"\nService - {monitor.active_bike['name']}")
                print("3.1 Record a service")
                print("3.2 View service history")
                print("3.3 Back to main menu")
                
                subchoice = input("\nEnter your choice (3.1-3.3): ").strip()
                
                if subchoice == "3.1":
                    # Record service (component swap)
                    while True:
                        # Show current components
                        components = monitor.get_bike_components(monitor.active_bike['gear_id'], status="active")
                        if not components:
                            print("\nNo active components found for this bike.")
                            print("Would you like to add a new component?")
                            print("1. Yes, add new component")
                            print("2. No, go back")
                            add_choice = input("Enter choice (1-2): ").strip()
                            
                            if add_choice == "1":
                                # Add new component
                                print("\nEnter new component details:")
                                name = input("Component name (e.g., Chain, Tires): ").strip()
                                brand = input("Brand: ").strip()
                                model = input("Model: ").strip()
                                notes = input("Notes (optional): ").strip() or None
                                
                                try:
                                    purchase_date = input("Purchase date (YYYY-MM-DD, optional): ").strip()
                                    purchase_date = datetime.strptime(purchase_date, '%Y-%m-%d').replace(tzinfo=datetime.now().astimezone().tzinfo) if purchase_date else None
                                    
                                    purchase_price = input("Purchase price (optional): ").strip()
                                    purchase_price = float(purchase_price) if purchase_price else None
                                except ValueError:
                                    print("Invalid date or price format.")
                                    continue
                                
                                component_id = monitor.add_component(
                                    name=name,
                                    brand=brand,
                                    model=model,
                                    gear_id=monitor.active_bike['gear_id'],
                                    purchase_date=purchase_date,
                                    purchase_price=purchase_price,
                                    notes=notes
                                )
                                
                                if component_id:
                                    print("\nComponent added successfully!")
                                    # Refresh components list
                                    components = monitor.get_bike_components(monitor.active_bike['gear_id'], status="active")
                                else:
                                    print("\nFailed to add component.")
                                    continue
                            else:
                                break
                        
                        # Now proceed with service recording
                        print("\nCurrent Components:")
                        print("0. Done recording services")
                        for i, comp in enumerate(components, 1):
                            print(f"{i}. {comp.name} ({comp.brand} {comp.model})")
                        
                        try:
                            comp_choice = input("\nSelect component to service (number) or 0 to finish: ").strip()
                            if comp_choice == "0":
                                break
                                
                            comp_idx = int(comp_choice) - 1
                            if comp_idx < 0 or comp_idx >= len(components):
                                print("Invalid selection.")
                                continue
                            old_component = components[comp_idx]
                        except ValueError:
                            print("Invalid input.")
                            continue
                        
                        # Ask what to do with the old component
                        print("\nWhat would you like to do with the old component?")
                        print("1. Remove and store in inventory")
                        print("2. Retire (no longer usable)")
                        action_choice = input("Enter choice (1-2): ").strip()
                        action = "remove" if action_choice == "1" else "retire"
                        
                        # Ask if installing a new component
                        print("\nWould you like to install a new component?")
                        print("1. Yes, from inventory")
                        print("2. Yes, new component")
                        print("3. No, just remove/retire the old one")
                        install_choice = input("Enter choice (1-3): ").strip()
                        
                        new_component_id = None
                        if install_choice in ["1", "2"]:
                            if install_choice == "1":
                                # Show inventory
                                inventory = monitor.get_inventory_components()
                                if not inventory:
                                    print("\nNo components in inventory.")
                                    continue
                                    
                                print("\nAvailable in Inventory:")
                                for i, comp in enumerate(inventory, 1):
                                    print(f"{i}. {comp.name} ({comp.brand} {comp.model})")
                                
                                try:
                                    inv_idx = int(input("\nSelect component from inventory (number): ").strip()) - 1
                                    if inv_idx < 0 or inv_idx >= len(inventory):
                                        print("Invalid selection.")
                                        continue
                                    new_component_id = inventory[inv_idx].id
                                except ValueError:
                                    print("Invalid input.")
                                    continue
                                    
                            else:  # install_choice == "2"
                                # Add new component
                                print("\nEnter new component details:")
                                name = input("Component name (e.g., Chain, Tires): ").strip()
                                brand = input("Brand: ").strip()
                                model = input("Model: ").strip()
                                notes = input("Notes (optional): ").strip() or None
                                
                                try:
                                    purchase_date = input("Purchase date (YYYY-MM-DD, optional): ").strip()
                                    purchase_date = datetime.strptime(purchase_date, '%Y-%m-%d').replace(tzinfo=datetime.now().astimezone().tzinfo) if purchase_date else None
                                    
                                    purchase_price = input("Purchase price (optional): ").strip()
                                    purchase_price = float(purchase_price) if purchase_price else None
                                except ValueError:
                                    print("Invalid date or price format.")
                                    continue
                                
                                new_component_id = monitor.add_component(
                                    name=name,
                                    brand=brand,
                                    model=model,
                                    gear_id=monitor.active_bike['gear_id'],
                                    purchase_date=purchase_date,
                                    purchase_price=purchase_price,
                                    notes=notes
                                )
                                
                                if not new_component_id:
                                    print("Failed to add new component.")
                                    continue
                        
                        # Get service notes
                        notes = input("\nEnter service notes: ").strip() or None
                        
                        # Perform the swap
                        if monitor.swap_component(monitor.active_bike['gear_id'], old_component.id, new_component_id, action, notes):
                            print("\nService recorded successfully!")
                        else:
                            print("\nFailed to record service.")
                            
                        # Ask if user wants to record another service
                        print("\nWould you like to record another service?")
                        print("1. Yes")
                        print("2. No, go back to service menu")
                        another_choice = input("Enter choice (1-2): ").strip()
                        if another_choice != "1":
                            break
                    
                elif subchoice == "3.2":
                    # View service history
                    while True:
                        print("\nView Service History")
                        print("3.2.1 View all services")
                        print("3.2.2 Delete a record")
                        print("3.2.3 Back")
                        
                        view_choice = input("\nEnter your choice (3.2.1-3.2.3): ").strip()
                        
                        if view_choice == "3.2.1":
                            # Show all component swaps
                            swaps = [swap for swap in monitor.component_swaps 
                                   if swap.gear_id == monitor.active_bike['gear_id']]
                            if not swaps:
                                print("\nNo service records found.")
                                continue
                                
                            print("\nService History:")
                            print("-" * 80)
                            for i, swap in enumerate(sorted(swaps, key=lambda x: x.date, reverse=True), 1):
                                print(f"\nRecord {i}:")
                                print(f"Date: {swap.date.strftime('%Y-%m-%d %H:%M')}")
                                component = monitor.components.get(swap.component_id)
                                if component:
                                    print(f"Component: {component.name} ({component.brand} {component.model})")
                                print(f"Action: {swap.action}")
                                if swap.notes:
                                    print(f"Notes: {swap.notes}")
                            print("-" * 80)
                            
                        elif view_choice == "3.2.2":
                            # TODO: Implement service record deletion
                            print("\nService record deletion not implemented yet.")
                            
                        elif view_choice == "3.2.3":
                            break
                            
                        else:
                            print("Invalid choice. Please try again.")
                    
                elif subchoice == "3.3":
                    break
                    
                else:
                    print("Invalid choice. Please try again.")
                    
        elif choice == "5":
            print("Goodbye!")
            break
            
        else:
            print("Invalid choice. Please try again.")

if __name__ == "__main__":
    main() 