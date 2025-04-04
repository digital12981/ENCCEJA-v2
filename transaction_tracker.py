from datetime import datetime, timedelta
from typing import Dict, Any, Tuple
from flask import request, current_app
import hashlib
import json

# Store transaction attempts by IP and data hash
# Format: {ip: {data_hash: [(timestamp, transaction_id), ...]}}
transaction_attempts = {}

# Configuration
MAX_TRANSACTIONS = 5  # Maximum number of transactions with the same data per IP
TRACKING_WINDOW = timedelta(hours=24)  # Time window to track transactions
BAN_DURATION = timedelta(hours=24)  # Duration of ban

# IP banning system specifically for transaction attempts
TRANSACTION_BANNED_IPS = {}  # Format: {ip: expiry_timestamp}

def get_client_ip() -> str:
    """Get client IP address from request"""
    if request.headers.get('X-Forwarded-For'):
        # If behind a proxy, get the real IP
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'

def hash_transaction_data(data: Dict[str, Any]) -> str:
    """Create a hash from transaction data to identify unique transactions"""
    # Extract only the fields we want to track for uniqueness
    track_fields = {}
    
    # Add relevant fields for tracking
    for field in ['name', 'cpf', 'amount']:
        if field in data and data[field]:
            track_fields[field] = str(data[field]).strip()
    
    # Create a deterministic JSON string (sorted keys)
    json_str = json.dumps(track_fields, sort_keys=True)
    
    # Create hash
    return hashlib.md5(json_str.encode()).hexdigest()

def is_transaction_ip_banned(ip: str) -> bool:
    """Check if an IP is banned for transaction attempts"""
    if ip in TRANSACTION_BANNED_IPS:
        expiry = TRANSACTION_BANNED_IPS[ip]
        if datetime.now() < expiry:
            current_app.logger.warning(f"Blocking transaction from banned IP: {ip}")
            return True
        else:
            # Ban expired, remove from list
            del TRANSACTION_BANNED_IPS[ip]
    return False

def track_transaction_attempt(ip: str, data: Dict[str, Any], transaction_id: str = None) -> Tuple[bool, str]:
    """
    Track a transaction attempt
    
    Returns:
        Tuple[bool, str]: (is_allowed, message)
    """
    current_time = datetime.now()
    
    # Check if IP is banned for transactions
    if is_transaction_ip_banned(ip):
        return False, "IP bloqueado por excesso de tentativas de transação"
    
    # Get data hash
    data_hash = hash_transaction_data(data)
    
    # Initialize tracking for this IP if needed
    if ip not in transaction_attempts:
        transaction_attempts[ip] = {}
    
    # Initialize tracking for this data hash if needed
    if data_hash not in transaction_attempts[ip]:
        transaction_attempts[ip][data_hash] = []
    
    # Clean up old attempts outside tracking window
    cutoff_time = current_time - TRACKING_WINDOW
    transaction_attempts[ip][data_hash] = [
        attempt for attempt in transaction_attempts[ip][data_hash]
        if attempt[0] >= cutoff_time
    ]
    
    # Count recent attempts for this data
    recent_attempts_count = len(transaction_attempts[ip][data_hash])
    
    # If too many attempts, ban the IP
    if recent_attempts_count >= MAX_TRANSACTIONS:
        TRANSACTION_BANNED_IPS[ip] = current_time + BAN_DURATION
        current_app.logger.warning(
            f"IP {ip} banned for excessive transaction attempts with same data. "
            f"Attempts: {recent_attempts_count}, Data hash: {data_hash}"
        )
        return False, "Excesso de tentativas de transação. Tente novamente em 24 horas."
    
    # Add this attempt to the tracking
    transaction_attempts[ip][data_hash].append((current_time, transaction_id))
    current_app.logger.info(
        f"Transaction attempt tracked: IP={ip}, Hash={data_hash}, "
        f"Attempt #{recent_attempts_count + 1}"
    )
    
    return True, "Transaction allowed"

def cleanup_transaction_tracking():
    """Clean up old transaction tracking data"""
    current_time = datetime.now()
    cutoff_time = current_time - TRACKING_WINDOW
    
    # Clean up transaction attempts
    for ip in list(transaction_attempts.keys()):
        for data_hash in list(transaction_attempts[ip].keys()):
            transaction_attempts[ip][data_hash] = [
                attempt for attempt in transaction_attempts[ip][data_hash]
                if attempt[0] >= cutoff_time
            ]
            
            # Remove empty hash entries
            if not transaction_attempts[ip][data_hash]:
                del transaction_attempts[ip][data_hash]
        
        # Remove empty IP entries
        if not transaction_attempts[ip]:
            del transaction_attempts[ip]
    
    # Clean up expired bans
    for ip in list(TRANSACTION_BANNED_IPS.keys()):
        if current_time > TRANSACTION_BANNED_IPS[ip]:
            del TRANSACTION_BANNED_IPS[ip]