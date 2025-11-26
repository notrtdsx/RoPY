import logging
from datetime import datetime
from typing import Dict, Any, Optional
from json import JSONDecodeError
from urllib.parse import urlparse
import requests
import time
import sys
import random

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Developer mode flag ---
# For developers only: Set this to True to enable developer mode
DEVELOPER_MODE = False  # Set to True for extra technical details

# Constants
API_URL = "https://users.roblox.com/v1/users/"
MAX_RETRIES = 3
BASE_RETRY_DELAY = 1  # Base delay for exponential backoff (seconds)
MAX_RETRY_DELAY = 30  # Maximum delay between retries (seconds)
REQUEST_TIMEOUT = 10  # Request timeout in seconds

# Roblox user ID constraints
MAX_ID_LENGTH = 12  # Maximum reasonable length for Roblox user ID
MAX_USER_ID = 10_000_000_000  # Max ~10 billion (current Roblox IDs are around 7-8 billion)

# Allowed URL schemes for avatar URLs
ALLOWED_URL_SCHEMES = ('https', 'http')
ALLOWED_AVATAR_DOMAINS = ('roblox.com', 'rbxcdn.com', 'tr.rbxcdn.com', 't0.rbxcdn.com', 't1.rbxcdn.com', 't2.rbxcdn.com', 't3.rbxcdn.com', 't4.rbxcdn.com', 't5.rbxcdn.com', 't6.rbxcdn.com', 't7.rbxcdn.com')

def calculate_retry_delay(attempt: int) -> float:
    """
    Calculate retry delay using exponential backoff with jitter.
    
    Parameters:
    attempt (int): Current attempt number (0-indexed).
    
    Returns:
    float: Delay in seconds before the next retry.
    """
    # Exponential backoff: base_delay * 2^attempt
    delay = BASE_RETRY_DELAY * (2 ** attempt)
    # Cap at max delay
    delay = min(delay, MAX_RETRY_DELAY)
    # Add jitter (random value between 0 and 50% of delay) to prevent thundering herd
    jitter = random.uniform(0, delay * 0.5)
    return delay + jitter


def validate_avatar_url(url: str) -> str:
    """
    Validate and sanitize avatar URL.
    
    Parameters:
    url (str): The URL to validate.
    
    Returns:
    str: The validated URL or 'Invalid URL' if validation fails.
    """
    if not url or url == "Unknown":
        return "Not available"
    
    if not isinstance(url, str):
        return "Invalid URL"
    
    try:
        parsed = urlparse(url)
        
        # Check scheme
        if parsed.scheme not in ALLOWED_URL_SCHEMES:
            logger.warning(f"Invalid URL scheme: {parsed.scheme}")
            return "Invalid URL"
        
        # Check if it has a valid netloc (domain)
        if not parsed.netloc:
            logger.warning("URL missing domain")
            return "Invalid URL"
        
        # Validate domain is from Roblox
        domain = parsed.netloc.lower()
        is_valid_domain = any(
            domain == allowed or domain.endswith('.' + allowed)
            for allowed in ALLOWED_AVATAR_DOMAINS
        )
        
        if not is_valid_domain:
            logger.warning(f"URL domain not allowed: {domain}")
            return "Invalid URL"
        
        return url
    except Exception as e:
        logger.warning(f"Error validating URL: {e}")
        return "Invalid URL"


def validate_json_response(response: requests.Response) -> Optional[Dict[str, Any]]:
    """
    Validate that the response contains valid JSON.
    
    Parameters:
    response (requests.Response): The HTTP response object.
    
    Returns:
    Optional[Dict[str, Any]]: Parsed JSON data or None if invalid.
    """
    # Check Content-Type header
    content_type = response.headers.get('Content-Type', '')
    if not content_type:
        logger.warning("Response missing Content-Type header")
    elif 'application/json' not in content_type.lower():
        logger.warning(f"Unexpected Content-Type: {content_type}")
    
    # Check for empty response body
    if not response.text or response.text.strip() == '':
        logger.error("Empty response body received")
        return None
    
    try:
        data = response.json()
        
        # Validate that response is a dictionary
        if not isinstance(data, dict):
            logger.error(f"Expected dict response, got {type(data).__name__}")
            return None
        
        return data
    except JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        return None


def safe_get_count(data: Dict[str, Any], field: str) -> str:
    """
    Safely get a count field from API response with validation.
    
    Parameters:
    data (Dict[str, Any]): The API response data.
    field (str): The field name to retrieve.
    
    Returns:
    str: The count as a string, or 'Not available' if missing/invalid.
    """
    value = data.get(field)
    
    if value is None:
        return "Not available"
    
    # Validate it's a number
    if isinstance(value, (int, float)):
        # Check for reasonable bounds
        if value < 0:
            return "Not available"
        return str(int(value))
    
    return "Not available"


def fetch_user_information(user_id: str, session: Optional[requests.Session] = None) -> None:
    """
    Fetch data from the Roblox API and print user information.
    
    Parameters:
    user_id (str): The ID of the Roblox user.
    session (Optional[requests.Session]): Optional session for connection pooling.
    """
    url = f"{API_URL}{user_id}"
    
    # Use provided session or create a new one
    should_close_session = False
    if session is None:
        session = requests.Session()
        should_close_session = True
    
    try:
        for attempt in range(MAX_RETRIES):
            try:
                start_time = time.time()
                response = session.get(url, timeout=REQUEST_TIMEOUT)
                end_time = time.time()
                latency = end_time - start_time

                # Check for rate limiting headers and display info
                rate_limit_remaining = response.headers.get('X-RateLimit-Remaining')
                rate_limit_reset = response.headers.get('X-RateLimit-Reset')
                
                if rate_limit_remaining is not None and DEVELOPER_MODE:
                    logger.info(f"Rate limit remaining: {rate_limit_remaining}")
                
                # Check specific status codes before raise_for_status
                status_code = response.status_code
                
                if status_code == 404:
                    print(f"Error: User with ID {user_id} not found.")
                    logger.warning(f"User not found: {user_id}")
                    return
                
                if status_code == 429:  # Rate limited
                    if attempt < MAX_RETRIES - 1:
                        delay = calculate_retry_delay(attempt)
                        
                        # Try to use Retry-After header if available
                        retry_after = response.headers.get('Retry-After')
                        if retry_after:
                            try:
                                delay = min(float(retry_after), MAX_RETRY_DELAY)
                            except (ValueError, TypeError):
                                pass
                        
                        logger.warning(f"Rate limit hit. Waiting {delay:.2f} seconds before retry...")
                        print(f"Rate limited. Retrying in {delay:.1f} seconds...")
                        time.sleep(delay)
                        continue
                    else:
                        print("Error: Rate limit exceeded. Please try again later.")
                        logger.error("Rate limit exceeded after max retries")
                        return

                response.raise_for_status()

                # Validate JSON response
                data = validate_json_response(response)
                if data is None:
                    print("Error: Received invalid response from server.")
                    return

                # Validate required fields exist
                if 'name' not in data:
                    logger.warning("Response missing 'name' field")
                
                # Safely extract and validate avatar URL
                raw_avatar_url = data.get("avatarUrl", "")
                validated_avatar_url = validate_avatar_url(raw_avatar_url)
                
                user_info: Dict[str, Any] = {
                    "username": str(data.get("name", "Unknown")) if data.get("name") else "Unknown",
                    "display_name": str(data.get("displayName", "Unknown")) if data.get("displayName") else "Unknown",
                    "created_date": parse_date(data.get("created")),
                    "avatar_url": validated_avatar_url,
                    "followers_count": safe_get_count(data, "followersCount"),
                    "friends_count": safe_get_count(data, "friendsCount"),
                    "latency": f"{latency:.2f} seconds"
                }

                logger.info(f"Successfully fetched data for user ID {user_id}")
                if DEVELOPER_MODE:
                    logger.debug(f"Raw data: {data}")

                display_user_info(**user_info)
                return

            except requests.exceptions.HTTPError as http_err:
                if hasattr(http_err, 'response') and http_err.response is not None:
                    status_code = http_err.response.status_code
                    
                    # Handle specific HTTP errors
                    if status_code == 400:
                        print("Error: Invalid request. Please check the user ID.")
                        logger.error(f"Bad request for user ID: {user_id}")
                        return
                    elif status_code == 401:
                        print("Error: Authentication required.")
                        logger.error("Authentication error")
                        return
                    elif status_code == 403:
                        print("Error: Access forbidden.")
                        logger.error("Forbidden access")
                        return
                    elif status_code >= 500:
                        # Server error - retry with backoff
                        if attempt < MAX_RETRIES - 1:
                            delay = calculate_retry_delay(attempt)
                            logger.warning(f"Server error {status_code}. Retrying in {delay:.2f} seconds...")
                            time.sleep(delay)
                            continue
                        print("Error: Server is temporarily unavailable. Please try again later.")
                        logger.error(f"Server error after max retries: {status_code}")
                        return
                    
                    logger.error(f"HTTP error occurred: {http_err}")
                    print(f"Error: {status_code} - Unable to fetch user information.")
                else:
                    logger.error(f"HTTP error occurred: {http_err}")
                    print("Error: Unable to fetch user information.")
                return
                
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as conn_err:
                logger.error(f"Connection error occurred: {conn_err}")
                if attempt < MAX_RETRIES - 1:
                    delay = calculate_retry_delay(attempt)
                    logger.info(f"Retrying in {delay:.2f} seconds... Attempt {attempt + 2} of {MAX_RETRIES}")
                    print(f"Connection issue. Retrying in {delay:.1f} seconds...")
                    time.sleep(delay)
                    continue
                print("A network error occurred. Please check your internet connection.")
                return
                
            except JSONDecodeError as json_err:
                logger.error(f"JSON decode error: {json_err}")
                print("Error: Received malformed response from server.")
                return
                
            except Exception as err:
                logger.error(f"An unexpected error occurred: {err}")
                print("An unexpected error occurred. Please try again later.")
                return
    finally:
        if should_close_session:
            session.close()

def parse_date(date_str: Optional[str]) -> str:
    """
    Parse and format the date string.
    
    Parameters:
    date_str (Optional[str]): The date string to parse.
    
    Returns:
    str: The formatted date string.
    """
    if date_str is None or not date_str or date_str == "Unknown":
        return "Unknown"
    
    if not isinstance(date_str, str):
        return "Invalid date format"

    date_formats = ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"]
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    
    logger.warning(f"Could not parse date string: {date_str}")
    return "Invalid date format"

def display_user_info(username: str, display_name: str, created_date: str, avatar_url: str, followers_count: str, friends_count: str, latency: str) -> None:
    """
    Displays information about a user.
    
    Parameters:
    username (str): The username of the user.
    display_name (str): The display name of the user.
    created_date (str): The cleaned creation date of the user.
    avatar_url (str): The avatar URL of the user.
    followers_count (str): The follower count of the user.
    friends_count (str): The friend count of the user.
    latency (str): The latency of the request.
    """
    print("\nUser Information:")
    print(f"Username: {username}")
    print(f"Display Name: {display_name}")
    print(f"Created: {created_date}")
    print(f"Avatar URL: {avatar_url}")
    print(f"Followers: {followers_count}")
    print(f"Friends: {friends_count}")
    print(f"Latency: {latency}")

def validate_user_id(user_id: str) -> bool:
    """
    Validate the user ID input.
    
    Parameters:
    user_id (str): The user ID to validate.
    
    Returns:
    bool: True if valid, False otherwise.
    """
    # Handle whitespace - strip and check
    stripped_id = user_id.strip()
    
    if not stripped_id:
        error_msg = "Error: ID cannot be empty."
        print(error_msg)
        logger.warning("User provided empty ID")
        return False
    
    # Check for special characters or non-digit characters
    if not stripped_id.isdigit():
        error_msg = "Error: ID must contain only numbers."
        print(error_msg)
        logger.warning(f"User provided invalid ID format: {user_id}")
        return False
    
    # Check for leading zeros (invalid for Roblox IDs)
    if len(stripped_id) > 1 and stripped_id[0] == '0':
        error_msg = "Error: ID cannot have leading zeros."
        print(error_msg)
        logger.warning(f"User provided ID with leading zeros: {user_id}")
        return False
    
    # Check length constraint
    if len(stripped_id) > MAX_ID_LENGTH:
        error_msg = f"Error: ID is too long (maximum {MAX_ID_LENGTH} digits)."
        print(error_msg)
        logger.warning(f"User provided ID too long: {len(stripped_id)} digits")
        return False
    
    # Convert to integer safely to check bounds
    try:
        user_id_int = int(stripped_id)
    except (ValueError, OverflowError):
        error_msg = "Error: ID is not a valid number."
        print(error_msg)
        logger.warning(f"Could not parse ID as integer: {user_id}")
        return False
    
    # Check for non-positive ID
    if user_id_int <= 0:
        error_msg = "Error: ID must be a positive number."
        print(error_msg)
        logger.warning(f"User provided non-positive ID: {user_id}")
        return False
    
    # Check for integer overflow / unreasonably large IDs
    if user_id_int > MAX_USER_ID:
        error_msg = f"Error: ID exceeds maximum allowed value ({MAX_USER_ID:,})."
        print(error_msg)
        logger.warning(f"User provided ID exceeds maximum: {user_id}")
        return False
        
    return True

def main() -> None:
    """
    Main function to run the script.
    Prompts the user to enter the ID of a Roblox user and displays their information.
    """
    print("Welcome to RoPY - Roblox User Information Fetcher")
    print("Enter 'q' or 'quit' to exit the program")
    
    # Create a session for connection pooling
    session = requests.Session()
    
    try:
        while True:
            try:
                user_input = input("\nEnter Roblox user ID: ").strip()
            except EOFError:
                print("\n\nInput stream ended. Exiting program. Goodbye!")
                sys.exit(0)
            
            # Check for quit commands (case-insensitive)
            if user_input.lower() in ('q', 'quit'):
                print("\nExiting program. Goodbye!")
                sys.exit(0)
                
            if validate_user_id(user_input):
                fetch_user_information(user_input.strip(), session)
                
                while True:
                    try:
                        continue_choice = input("\nWould you like to look up another user? (y/n): ").strip().lower()
                        if continue_choice in ('y', 'yes'):
                            break
                        elif continue_choice in ('n', 'no'):
                            print("\nExiting program. Goodbye!")
                            sys.exit(0)
                        else:
                            print("Please enter 'y' or 'n'")
                    except EOFError:
                        print("\n\nInput stream ended. Exiting program. Goodbye!")
                        sys.exit(0)
    finally:
        session.close()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nProgram interrupted by user. Goodbye!")
        sys.exit(0)
