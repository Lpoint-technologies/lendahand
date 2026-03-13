from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory
import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import requests
import json
from datetime import datetime, timedelta
import uuid
import threading
import time
import random
app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

otp_storage = {}
farmer_otp_storage = {}
# ================= SMS Sending Function ==================
def send_sms(phone, message):
    """Send SMS using Fast2SMS API"""
    api_key = "CELR3Zg21VMUIiWy4rzqnS6fYBaxNdsHlOhpJ7DQ0GFKAbTPtkNKUbiwAG0YaTfsIBxmyV4nlqJugeCR"
    url = "https://www.fast2sms.com/dev/bulkV2"

    # Clean phone number - remove any non-digit characters
    phone_clean = ''.join(filter(str.isdigit, str(phone)))
    
    payload = f"sender_id=LPOINT&message={message}&language=english&route=q&numbers={phone_clean}"
    headers = {
        'authorization': api_key,
        'Content-Type': "application/x-www-form-urlencoded",
        'Cache-Control': "no-cache",
    }

    try:
        response = requests.post(url, data=payload, headers=headers)
        print("📱 SMS Response:", response.text)  # Debug output
        
        # Parse response to check if successful
        response_data = response.json()
        
        # Fast2SMS success response usually has return=True
        if response_data.get('return', False):
            return {'success': True, 'message_id': response_data.get('request_id')}
        else:
            error_msg = response_data.get('message', 'Unknown error')
            print(f"❌ SMS failed: {error_msg}")
            return {'success': False, 'error': error_msg}
            
    except Exception as e:
        print(f"❌ SMS Error: {str(e)}")
        return {'success': False, 'error': str(e)}

def check_and_send_automatic_reminders():
    """Check for due returns and send automatic reminders 2 days before end date"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        today = datetime.now().date()
        
        # Find requests ending in 2 days (2 days from today = end_date)
        two_days_from_now = today + timedelta(days=2)
        
        print(f"🔔 Checking reminders: Today={today}, Looking for end_date={two_days_from_now}")
        
        cursor.execute("""
            SELECT rr.id, rr.user_name, rr.user_phone, rr.equipment_name, rr.end_date
            FROM rent_requests rr
            WHERE rr.status = 'approved' 
            AND rr.end_date = ?
            AND (rr.last_reminder_sent IS NULL OR rr.last_reminder_sent < ?)
        """, (two_days_from_now.strftime('%Y-%m-%d'), today))
        
        due_requests = cursor.fetchall()
        
        print(f"📱 Found {len(due_requests)} requests needing 2-day reminders")
        
        for request in due_requests:
            request_id, user_name, user_phone, equipment_name, end_date = request
            
            # Clean phone number
            user_phone_clean = ''.join(filter(str.isdigit, str(user_phone)))
            
            # Send 2-day reminder (2 days BEFORE end date)
            reminder_message = f"REMINDER: Your rental for {equipment_name} is due in 2 days (on {end_date}). Please prepare for return. - Lend A Hand"
            
            sms_result = send_sms(user_phone_clean, reminder_message)
            
            if sms_result.get('success'):
                print(f"✅ Auto-reminder sent for request #{request_id} to {user_name}")
                cursor.execute("""
                    UPDATE rent_requests 
                    SET last_reminder_sent = ?, reminder_type = 'auto_2day'
                    WHERE id = ?
                """, (datetime.now(), request_id))
            else:
                print(f"❌ Failed to send reminder for request #{request_id}: {sms_result.get('error')}")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error in automatic reminder system: {str(e)}")

# ================= AUTOMATIC REMINDER SYSTEM ==================
def check_and_complete_expired_rentals():
    """Check for expired rentals and mark them as completed + restock equipment"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        today = datetime.now().date()
        
        print(f"🔄 Checking for expired rentals: Today={today}")
        
        # Find approved rent requests that ended yesterday or earlier
        cursor.execute("""
            SELECT rr.id, rr.equipment_id, rr.equipment_name, rr.user_name, rr.user_phone, 
                   e.min_stock_threshold
            FROM rent_requests rr
            JOIN equipment e ON rr.equipment_id = e.id
            WHERE rr.status = 'approved' 
            AND rr.end_date < ?
        """, (today.strftime('%Y-%m-%d'),))
        
        expired_rentals = cursor.fetchall()
        
        print(f"📦 Found {len(expired_rentals)} expired rentals to complete")
        
        for rental in expired_rentals:
            request_id, equipment_id, equipment_name, user_name, user_phone, min_stock_threshold = rental
            
            print(f"✅ Completing expired rental #{request_id} for {equipment_name}")
            
            # Mark rent request as completed
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'completed', processed_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (request_id,))
            
            # ✅ RESTOCK THE EQUIPMENT (increase stock by 1) with proper status update
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 <= ? THEN 'low_stock'
                        WHEN stock_quantity + 1 > 0 THEN 'available'
                        ELSE status
                    END
                WHERE id = ?
            """, (min_stock_threshold or 5, equipment_id))
            
            # Send completion notification to user
            completion_message = f"Your rental period for {equipment_name} has been automatically completed. Equipment has been restocked. Thank you for using Lend A Hand!"
            send_sms(user_phone, completion_message)
            
            print(f"✅ Rental #{request_id} completed and equipment restocked")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error in automatic completion system: {str(e)}")

def start_reminder_scheduler():
    """Start the background scheduler for automatic reminders AND returns"""
    def run_scheduler():
        while True:
            try:
                check_and_send_automatic_reminders()  # Existing function
                check_and_complete_expired_rentals()   # NEW: Auto-return pending
            except Exception as e:
                print(f"❌ Scheduler error: {str(e)}")
            time.sleep(86400)  # Run every 24 hours
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print("✅ Automatic reminder AND return scheduler started")

def add_reminder_columns():
    """Add reminder tracking columns to rent_requests table"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA table_info(rent_requests)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'last_reminder_sent' not in columns:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN last_reminder_sent TIMESTAMP")
        
        if 'reminder_type' not in columns:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN reminder_type TEXT")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error adding reminder columns: {str(e)}")

def init_db():
    # Vendors database
    conn_vendors = sqlite3.connect('vendors.db')
    c_vendors = conn_vendors.cursor()
    c_vendors.execute('''CREATE TABLE IF NOT EXISTS vendors
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 business_name TEXT NOT NULL,
                 contact_name TEXT NOT NULL,
                 email TEXT UNIQUE NOT NULL,
                 phone TEXT NOT NULL,
                 service_type TEXT NOT NULL,
                 password TEXT NOT NULL,
                 description TEXT,
                 business_document TEXT,  
                 document_verified TEXT DEFAULT 'pending',  
                 registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 status TEXT DEFAULT 'pending')''')
    
    
    c_vendors.execute('''
        CREATE TABLE IF NOT EXISTS equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vendor_email TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        price REAL DEFAULT 0,
        price_unit TEXT DEFAULT 'day',
        rental_price REAL DEFAULT 0,
        rental_price_unit TEXT DEFAULT 'day',
        purchase_price REAL DEFAULT 0,
        purchase_unit TEXT DEFAULT 'unit',
        equipment_type TEXT DEFAULT 'both',
        location TEXT NOT NULL,
        image_url TEXT,
        status TEXT DEFAULT 'available',
        stock_quantity INTEGER DEFAULT 1,
        min_stock_threshold INTEGER DEFAULT 5,
        created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')
    
     # Rent requests table - SIMPLIFIED without vendor_id
    c_vendors.execute('''
        CREATE TABLE IF NOT EXISTS rent_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            user_phone TEXT NOT NULL,
            user_email TEXT,
            equipment_id INTEGER NOT NULL,
            equipment_name TEXT NOT NULL,
            vendor_email TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            duration INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            notes TEXT,
            daily_rate REAL NOT NULL,
            base_amount REAL NOT NULL,
            service_fee REAL NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            submitted_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_date TIMESTAMP
            last_reminder_sent TIMESTAMP,  -- ✅ ADDED THIS
            reminder_type TEXT             -- ✅ ADDED THIS
        )
    ''')
    
    c_vendors.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            user_email TEXT,
            user_phone TEXT,
            equipment_id INTEGER NOT NULL,
            equipment_name TEXT NOT NULL,
            vendor_email TEXT NOT NULL,
            vendor_name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            duration INTEGER NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_date TIMESTAMP
        )
    ''')
    # Add this to your init_db() function in vendors.db
    c_vendors.execute('''
    CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    equipment_id INTEGER NOT NULL,
    equipment_name TEXT NOT NULL,
    vendor_email TEXT NOT NULL,
    vendor_name TEXT NOT NULL,
    order_type TEXT NOT NULL, -- 'booking' or 'rent'
    order_id INTEGER NOT NULL, -- booking_id or rent_request_id
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    title TEXT NOT NULL,
    comment TEXT NOT NULL,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active'
)
''')
    
    c_vendors.execute('''
        CREATE TABLE IF NOT EXISTS cancellation_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            
            -- Order Identification
            order_id INTEGER NOT NULL,
            order_type TEXT NOT NULL, -- 'booking' or 'rent'
            
            -- User Information (Complete Details)
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            user_email TEXT NOT NULL,
            user_phone TEXT NOT NULL,
            user_location TEXT,
            
            -- Vendor Information (Complete Details)
            vendor_email TEXT NOT NULL,
            vendor_name TEXT NOT NULL,
            vendor_business_name TEXT,
            vendor_contact_phone TEXT,
            
            -- Equipment Information (Complete Details)
            equipment_id INTEGER NOT NULL,
            equipment_name TEXT NOT NULL,
            equipment_category TEXT,
            equipment_description TEXT,
            equipment_price REAL,
            equipment_price_unit TEXT,
            equipment_location TEXT,
            equipment_image_url TEXT,
            
            -- Order Information (Complete Details)
            total_amount REAL NOT NULL,
            start_date TEXT,
            end_date TEXT,
            duration INTEGER,
            order_notes TEXT,
            purpose TEXT, -- For rent requests
            order_status_before_cancel TEXT NOT NULL,
            order_created_date TEXT,
            
            -- Cancellation Details
            cancellation_reason TEXT NOT NULL,
            status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
            requested_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_date TIMESTAMP,
            processed_by TEXT, -- 'vendor' or 'system'
            vendor_response_notes TEXT,
            
            -- Additional Metadata
            days_until_start INTEGER,
            is_urgent BOOLEAN DEFAULT 0
        )
    ''')
    conn_vendors.commit()
    conn_vendors.close()

    conn_agri = sqlite3.connect('agriculture.db')
    c_agri = conn_agri.cursor()

    # Farmers table
    c_agri.execute('''
        CREATE TABLE IF NOT EXISTS farmers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT,
            phone TEXT NOT NULL,
            farm_location TEXT NOT NULL,
            farm_size REAL,
            crop_types TEXT NOT NULL,
            password TEXT NOT NULL,
            additional_info TEXT,
            rtc_document TEXT,
            registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending'
        )
    ''')

    # ✅ FIXED: Rent requests table with all required columns
    
    
    conn_agri.commit()
    conn_agri.close()
    add_missing_columns()
# ================= File Upload Config ==================
UPLOAD_FOLDER = 'static/uploads/equipment'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Create upload directory if it doesn't exist
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_uploaded_image(file):
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4().hex}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(filepath)
        return f"/static/uploads/equipment/{unique_filename}"  # This should be the return value
    return None
def create_cancellation_requests_table():
    """Create the cancellation_requests table with all required columns"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Drop the table if it exists to recreate it with proper schema
        cursor.execute("DROP TABLE IF EXISTS cancellation_requests")
        
        # Create the table with complete schema
        cursor.execute('''
            CREATE TABLE cancellation_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                
                -- Order Identification
                order_id INTEGER NOT NULL,
                order_type TEXT NOT NULL, -- 'booking' or 'rent'
                
                -- User Information (Complete Details)
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                user_email TEXT NOT NULL,
                user_phone TEXT NOT NULL,
                user_location TEXT,
                
                -- Vendor Information (Complete Details)
                vendor_email TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                vendor_business_name TEXT,
                vendor_contact_phone TEXT,
                
                -- Equipment Information (Complete Details)
                equipment_id INTEGER NOT NULL,
                equipment_name TEXT NOT NULL,
                equipment_category TEXT,
                equipment_description TEXT,
                equipment_price REAL,
                equipment_price_unit TEXT,
                equipment_location TEXT,
                equipment_image_url TEXT,
                
                -- Order Information (Complete Details)
                total_amount REAL NOT NULL,
                start_date TEXT,
                end_date TEXT,
                duration INTEGER,
                order_notes TEXT,
                purpose TEXT, -- For rent requests
                order_status_before_cancel TEXT NOT NULL,
                order_created_date TEXT,
                
                -- Cancellation Details
                cancellation_reason TEXT NOT NULL,
                status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
                requested_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_date TIMESTAMP,
                processed_by TEXT, -- 'vendor' or 'system'
                vendor_response_notes TEXT,
                
                -- Additional Metadata
                days_until_start INTEGER,
                is_urgent BOOLEAN DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
        print("✅ cancellation_requests table created successfully with all columns")
        
    except Exception as e:
        print(f"❌ Error creating cancellation_requests table: {str(e)}")

def generate_farmer_otp():
    """Generate a 6-digit OTP"""
    return str(random.randint(100000, 999999))

def save_farmer_otp(phone, otp):
    """Save farmer OTP with expiration time (10 minutes)"""
    farmer_otp_storage[phone] = {
        'otp': otp,
        'expires_at': time.time() + 600  # 10 minutes
    }
    print(f"Farmer OTP saved for {phone}: {otp}")

def verify_farmer_otp(phone, otp):
    """Verify farmer OTP"""
    print(f"Verifying farmer OTP for {phone}: {otp}")
    if phone not in farmer_otp_storage:
        print(f"No OTP found for farmer {phone}")
        return False
    
    stored_data = farmer_otp_storage[phone]
    if time.time() > stored_data['expires_at']:
        # OTP expired
        del farmer_otp_storage[phone]
        print(f"Farmer OTP expired for {phone}")
        return False
    
    if stored_data['otp'] == otp:
        # OTP verified, clean up
        del farmer_otp_storage[phone]
        print(f"Farmer OTP verified for {phone}")
        return True
    
    print(f"Farmer OTP mismatch for {phone}")
    return False

def update_farmer_password(phone, new_password):
    """Update farmer password in database"""
    try:
        conn = sqlite3.connect('agriculture.db')
        cursor = conn.cursor()
        
        # Hash the new password
        hashed_password = generate_password_hash(new_password)
        
        # Update password
        cursor.execute("UPDATE farmers SET password = ? WHERE phone = ?", 
                      (hashed_password, phone))
        
        if cursor.rowcount > 0:
            conn.commit()
            conn.close()
            print(f"Password updated for farmer with phone: {phone}")
            return True
        else:
            print(f"No farmer found with phone: {phone}")
            conn.close()
            return False
        
    except Exception as e:
        print(f"Error updating farmer password: {str(e)}")
        return False
def migrate_equipment_table():
    """Migrate existing equipment table to add new pricing columns"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check if new columns exist
        cursor.execute("PRAGMA table_info(equipment)")
        columns = [column[1] for column in cursor.fetchall()]
        
        print("🔍 Current equipment columns:", columns)
        
        # Add missing columns
        new_columns = [
            ('rental_price', 'REAL DEFAULT 0'),
            ('rental_price_unit', 'TEXT DEFAULT "day"'),
            ('purchase_price', 'REAL DEFAULT 0'),
            ('purchase_unit', 'TEXT DEFAULT "unit"'),
            ('equipment_type', 'TEXT DEFAULT "both"')
        ]
        
        for col_name, col_type in new_columns:
            if col_name not in columns:
                cursor.execute(f"ALTER TABLE equipment ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added {col_name} column")
        
        conn.commit()
        conn.close()
        print("✅ Equipment table migration completed successfully")
        
    except Exception as e:
        print(f"❌ Error migrating equipment table: {str(e)}")
@app.route("/farmer/forgot_password_modal", methods=["POST"])
def farmer_forgot_password_modal():
    """Handle farmer forgot password from modal - Step 1: Send OTP"""
    phone = request.form.get("phone")
    
    if not phone:
        return jsonify({"success": False, "message": "Phone number is required"})
    
    # Clean phone number
    phone_clean = ''.join(filter(str.isdigit, phone))
    
    # Check if phone exists in farmer database
    conn = sqlite3.connect('agriculture.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM farmers WHERE phone = ?", (phone_clean,))
    farmer = cursor.fetchone()
    conn.close()
    
    if not farmer:
        print(f"No farmer found with phone: {phone_clean}")
        return jsonify({"success": False, "message": "Phone number not registered!"})
    
    # Generate and send OTP
    otp = generate_farmer_otp()
    save_farmer_otp(phone_clean, otp)
    
    # Send OTP via SMS
    message = f"Your Lend A Hand farmer password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone_clean, message)
    
    if sms_result['success']:
        # Store phone in session for verification
        session['farmer_reset_phone'] = phone_clean
        print(f"OTP sent successfully to farmer {phone_clean}")
        return jsonify({
            "success": True, 
            "message": f"OTP sent to {phone_clean}", 
            "phone": phone_clean
        })
    else:
        print(f"Failed to send OTP to farmer {phone_clean}: {sms_result.get('error')}")
        return jsonify({
            "success": False, 
            "message": "Failed to send OTP. Please try again."
        })

@app.route("/farmer/verify_otp_modal", methods=["POST"])
def farmer_verify_otp_modal():
    """Handle farmer OTP verification from modal - Step 2: Verify OTP"""
    phone = session.get('farmer_reset_phone')
    otp = request.form.get("otp")
    
    if not phone or not otp:
        return jsonify({"success": False, "message": "Invalid request"})
    
    if verify_farmer_otp(phone, otp):
        session['farmer_otp_verified'] = True
        return jsonify({"success": True, "message": "OTP verified successfully!"})
    else:
        return jsonify({"success": False, "message": "Invalid or expired OTP!"})

@app.route("/farmer/reset_password_modal", methods=["POST"])
def farmer_reset_password_modal():
    """Handle farmer password reset from modal - Step 3: Reset password"""
    phone = session.get('farmer_reset_phone')
    new_password = request.form.get("new_password")
    confirm_password = request.form.get("confirm_password")
    
    # Check if OTP was verified
    if not session.get('farmer_otp_verified'):
        return jsonify({"success": False, "message": "Please verify OTP first"})
    
    if not phone or not new_password:
        return jsonify({"success": False, "message": "Invalid request"})
    
    if new_password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match!"})
    
    # Validate password strength
    import re
    if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', new_password):
        return jsonify({
            "success": False, 
            "message": "Password must have 8+ chars, uppercase, lowercase, number, special char."
        })
    
    # Update password
    if update_farmer_password(phone, new_password):
        # Clear session data
        session.pop('farmer_reset_phone', None)
        session.pop('farmer_otp_verified', None)
        
        # Send confirmation SMS
        success_message = "Your password has been reset successfully! You can now login with your new password."
        send_sms(phone, success_message)
        
        return jsonify({
            "success": True, 
            "message": "Password reset successfully! Please login."
        })
    else:
        return jsonify({"success": False, "message": "Failed to reset password"})

@app.route("/farmer/resend_otp_modal", methods=["POST"])
def farmer_resend_otp_modal():
    """Resend OTP for farmer from modal"""
    phone = session.get('farmer_reset_phone')
    
    if not phone:
        return jsonify({"success": False, "message": "Phone number not found in session"})
    
    # Generate and send new OTP
    otp = generate_farmer_otp()
    save_farmer_otp(phone, otp)
    
    # Send OTP via SMS
    message = f"Your Lend A Hand farmer password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone, message)
    
    if sms_result['success']:
        return jsonify({"success": True, "message": f"New OTP sent to {phone}"})
    else:
        return jsonify({"success": False, "message": "Failed to resend OTP"})
def generate_otp():
    """Generate a 6-digit OTP"""
    return str(random.randint(100000, 999999))

def save_otp(phone, otp):
    """Save OTP with expiration time (10 minutes)"""
    otp_storage[phone] = {
        'otp': otp,
        'expires_at': time.time() + 600  # 10 minutes
    }

def verify_otp(phone, otp):
    """Verify OTP"""
    if phone not in otp_storage:
        return False
    
    stored_data = otp_storage[phone]
    if time.time() > stored_data['expires_at']:
        # OTP expired
        del otp_storage[phone]
        return False
    
    if stored_data['otp'] == otp:
        # OTP verified, clean up
        del otp_storage[phone]
        return True
    
    return False

def update_password(phone, new_password):
    """Update vendor password in database"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Hash the new password
        hashed_password = generate_password_hash(new_password)
        
        # Update password
        cursor.execute("UPDATE vendors SET password = ? WHERE phone = ?", 
                      (hashed_password, phone))
        
        if cursor.rowcount > 0:
            conn.commit()
            conn.close()
            return True
        
        conn.close()
        return False
        
    except Exception as e:
        print(f"Error updating password: {str(e)}")
        return False

@app.route("/vendor/forgot_password_modal", methods=["POST"])
def vendor_forgot_password_modal():
    """Handle vendor forgot password from modal - Step 1: Send OTP"""
    phone = request.form.get("phone")
    
    if not phone:
        return jsonify({"success": False, "message": "Phone number is required"})
    
    # Clean phone number
    phone_clean = ''.join(filter(str.isdigit, phone))
    
    # Check if phone exists in vendor database
    conn = sqlite3.connect('vendors.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vendors WHERE phone = ?", (phone_clean,))
    vendor = cursor.fetchone()
    conn.close()
    
    if not vendor:
        return jsonify({"success": False, "message": "Phone number not registered!"})
    
    # Generate and send OTP
    otp = generate_otp()
    save_otp(phone_clean, otp)
    
    # Send OTP via SMS
    message = f"Your Lend A Hand vendor password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone_clean, message)
    
    if sms_result['success']:
        # Store phone in session for verification
        session['vendor_reset_phone'] = phone_clean
        return jsonify({
            "success": True, 
            "message": f"OTP sent to {phone_clean}", 
            "phone": phone_clean
        })
    else:
        return jsonify({
            "success": False, 
            "message": "Failed to send OTP. Please try again."
        })

@app.route("/vendor/verify_otp_modal", methods=["POST"])
def vendor_verify_otp_modal():
    """Handle vendor OTP verification from modal - Step 2: Verify OTP"""
    phone = session.get('vendor_reset_phone')
    otp = request.form.get("otp")
    
    if not phone or not otp:
        return jsonify({"success": False, "message": "Invalid request"})
    
    if verify_otp(phone, otp):
        session['vendor_otp_verified'] = True
        return jsonify({"success": True, "message": "OTP verified successfully!"})
    else:
        return jsonify({"success": False, "message": "Invalid or expired OTP!"})

@app.route("/vendor/reset_password_modal", methods=["POST"])
def vendor_reset_password_modal():
    """Handle vendor password reset from modal - Step 3: Reset password"""
    phone = session.get('vendor_reset_phone')
    new_password = request.form.get("new_password")
    confirm_password = request.form.get("confirm_password")
    
    # Check if OTP was verified
    if not session.get('vendor_otp_verified'):
        return jsonify({"success": False, "message": "Please verify OTP first"})
    
    if not phone or not new_password:
        return jsonify({"success": False, "message": "Invalid request"})
    
    if new_password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match!"})
    
    # Validate password strength
    import re
    if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', new_password):
        return jsonify({
            "success": False, 
            "message": "Password must have 8+ chars, uppercase, lowercase, number, special char."
        })
    
    # Update password
    if update_password(phone, new_password):
        # Clear session data
        session.pop('vendor_reset_phone', None)
        session.pop('vendor_otp_verified', None)
        
        # Send confirmation SMS
        success_message = "Your password has been reset successfully! You can now login with your new password."
        send_sms(phone, success_message)
        
        return jsonify({
            "success": True, 
            "message": "Password reset successfully! Please login."
        })
    else:
        return jsonify({"success": False, "message": "Failed to reset password"})

@app.route("/vendor/resend_otp_modal", methods=["POST"])
def vendor_resend_otp_modal():
    """Resend OTP for vendor from modal"""
    phone = session.get('vendor_reset_phone')
    
    if not phone:
        return jsonify({"success": False, "message": "Phone number not found in session"})
    
    # Generate and send new OTP
    otp = generate_otp()
    save_otp(phone, otp)
    
    # Send OTP via SMS
    message = f"Your Lend A Hand vendor password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone, message)
    
    if sms_result['success']:
        return jsonify({"success": True, "message": f"New OTP sent to {phone}"})
    else:
        return jsonify({"success": False, "message": "Failed to resend OTP"})

@app.route('/recreate-cancellation-table')
def recreate_cancellation_table():
    """Recreate the cancellation_requests table with proper schema"""
    try:
        create_cancellation_requests_table()
        return "✅ cancellation_requests table recreated successfully!"
    except Exception as e:
        return f"❌ Error: {str(e)}"
@app.route('/static/uploads/equipment/<filename>')
def serve_equipment_image(filename):
    return send_from_directory('static/uploads/equipment', filename)
@app.route('/uploads/equipment/<filename>')
def serve_equipment_image_to_users(filename):
    """Serve equipment images for user dashboard"""
    uploads_path = os.path.join(app.root_path, 'static', 'uploads', 'equipment')
    return send_from_directory(uploads_path, filename)
# ================= Routes ==================
@app.route('/')
def index():
    return redirect(url_for('dashboard'))
@app.route('/fix-vendor-table')
def fix_vendor_table():
    """Add missing columns to vendors table"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check existing columns
        cursor.execute("PRAGMA table_info(vendors)")
        columns = [column[1] for column in cursor.fetchall()]
        
        result = "<h3>Current columns:</h3><ul>"
        for col in columns:
            result += f"<li>{col}</li>"
        result += "</ul>"
        
        # Try to add missing columns
        try:
            cursor.execute("ALTER TABLE vendors ADD COLUMN business_document TEXT")
            result += "<p>✅ Added business_document column</p>"
        except:
            result += "<p>⚠️ business_document column already exists</p>"
            
        try:
            cursor.execute("ALTER TABLE vendors ADD COLUMN document_verified TEXT DEFAULT 'pending'")
            result += "<p>✅ Added document_verified column</p>"
        except:
            result += "<p>⚠️ document_verified column already exists</p>"
        
        conn.commit()
        conn.close()
        
        result += "<p><a href='/vendorreg'>Go to vendor registration</a></p>"
        return result
        
    except Exception as e:
        return f"❌ Error: {str(e)}"
@app.route('/dashboard')
def dashboard():
    lang = request.args.get('lang', 'en')
    if lang == 'kn':
        # Set session for Kannada
        session['language'] = 'kn'
    return render_template('dashboard.html')
@app.context_processor
def inject_lang():
    return {'current_lang': session.get('language', 'en')}
@app.route('/index.html')
def index_page():
    return render_template('index.html')
import sqlite3

def add_missing_columns():
    """Add missing cancellation columns to existing database"""
    
    conn = sqlite3.connect('vendors.db')
    cursor = conn.cursor()
    
    print("🔄 Adding missing columns to bookings table...")
    
    # List of columns to add to bookings table
    booking_columns = [
        ('cancellation_requested_date', 'TIMESTAMP'),
        ('cancellation_reason', 'TEXT'),
        ('status_before_cancel', 'TEXT'),
        ('cancelled_date', 'TIMESTAMP')
    ]
    
    for col_name, col_type in booking_columns:
        try:
            cursor.execute(f"ALTER TABLE bookings ADD COLUMN {col_name} {col_type}")
            print(f"✅ Added {col_name} to bookings table")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print(f"⚠️ Column {col_name} already exists in bookings")
            else:
                print(f"❌ Error adding {col_name}: {e}")
    
    print("\n🔄 Adding missing columns to rent_requests table...")
    
    # List of columns to add to rent_requests table
    rent_columns = [
        ('cancellation_requested_date', 'TIMESTAMP'),
        ('cancellation_reason', 'TEXT'),
        ('status_before_cancel', 'TEXT'),
        ('cancelled_date', 'TIMESTAMP'),
        ('last_reminder_sent', 'TIMESTAMP'),
        ('reminder_type', 'TEXT')
    ]
    
    for col_name, col_type in rent_columns:
        try:
            cursor.execute(f"ALTER TABLE rent_requests ADD COLUMN {col_name} {col_type}")
            print(f"✅ Added {col_name} to rent_requests table")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print(f"⚠️ Column {col_name} already exists in rent_requests")
            else:
                print(f"❌ Error adding {col_name}: {e}")
    
    conn.commit()
    conn.close()
    
    print("\n✅ Database columns updated successfully!")

if __name__ == '__main__':
    add_missing_columns()
@app.route('/api/user/orders')
def get_user_orders():
    """Get all orders (bookings and rent requests) for the logged-in user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        user_id = session['user_id']
        print(f"🔄 Fetching orders for user ID: {user_id}")
        
        # Connect to vendors database
        conn_vendors = sqlite3.connect('vendors.db')
        conn_vendors.row_factory = sqlite3.Row
        cursor_vendors = conn_vendors.cursor()
        
        # SAFE BOOKINGS QUERY (uses COALESCE to handle missing columns gracefully)
        try:
            cursor_vendors.execute("""
                SELECT 
                    b.id, 
                    'booking' as order_type,
                    b.equipment_name,
                    v.contact_name as vendor_name,
                    b.vendor_email,
                    b.start_date,
                    b.end_date,
                    b.duration,
                    b.total_amount,
                    b.status,
                    b.created_date,
                    e.image_url as equipment_image,
                    b.equipment_id,
                    b.user_name,
                    b.user_email,
                    b.user_phone,
                    v.business_name,
                    v.contact_name as vendor_contact,
                    -- Use COALESCE to handle potential NULL values
                    b.cancellation_requested_date,
                    b.cancellation_reason,
                    b.status_before_cancel,
                    b.cancelled_date
                FROM bookings b
                LEFT JOIN equipment e ON b.equipment_id = e.id
                LEFT JOIN vendors v ON b.vendor_email = v.email
                WHERE b.user_id = ?
                ORDER BY b.created_date DESC
            """, (user_id,))
        except sqlite3.OperationalError as e:
            # If columns are missing, use fallback query
            print(f"⚠️ Using fallback query (missing columns): {e}")
            cursor_vendors.execute("""
                SELECT 
                    b.id, 
                    'booking' as order_type,
                    b.equipment_name,
                    v.contact_name as vendor_name,
                    b.vendor_email,
                    b.start_date,
                    b.end_date,
                    b.duration,
                    b.total_amount,
                    b.status,
                    b.created_date,
                    e.image_url as equipment_image,
                    b.equipment_id,
                    b.user_name,
                    b.user_email,
                    b.user_phone,
                    v.business_name,
                    v.contact_name as vendor_contact
                FROM bookings b
                LEFT JOIN equipment e ON b.equipment_id = e.id
                LEFT JOIN vendors v ON b.vendor_email = v.email
                WHERE b.user_id = ?
                ORDER BY b.created_date DESC
            """, (user_id,))
        
        bookings = cursor_vendors.fetchall()
        print(f"✅ Found {len(bookings)} bookings")
        
        # SAFE RENT REQUESTS QUERY
        try:
            cursor_vendors.execute("""
                SELECT 
                    rr.id,
                    'rent' as order_type,
                    rr.equipment_name,
                    v.contact_name as vendor_name,
                    rr.vendor_email,
                    rr.start_date,
                    rr.end_date,
                    rr.duration,
                    rr.total_amount,
                    rr.status,
                    rr.submitted_date as created_date,
                    e.image_url as equipment_image,
                    rr.equipment_id,
                    rr.user_name,
                    rr.user_email,
                    rr.user_phone,
                    v.business_name,
                    v.contact_name as vendor_contact,
                    -- Use COALESCE to handle potential NULL values
                    rr.cancellation_requested_date,
                    rr.cancellation_reason,
                    rr.status_before_cancel,
                    rr.cancelled_date
                FROM rent_requests rr
                JOIN vendors v ON rr.vendor_email = v.email
                LEFT JOIN equipment e ON rr.equipment_id = e.id
                WHERE rr.user_id = ?
                ORDER BY rr.submitted_date DESC
            """, (user_id,))
        except sqlite3.OperationalError as e:
            # If columns are missing, use fallback query
            print(f"⚠️ Using fallback query (missing columns): {e}")
            cursor_vendors.execute("""
                SELECT 
                    rr.id,
                    'rent' as order_type,
                    rr.equipment_name,
                    v.contact_name as vendor_name,
                    rr.vendor_email,
                    rr.start_date,
                    rr.end_date,
                    rr.duration,
                    rr.total_amount,
                    rr.status,
                    rr.submitted_date as created_date,
                    e.image_url as equipment_image,
                    rr.equipment_id,
                    rr.user_name,
                    rr.user_email,
                    rr.user_phone,
                    v.business_name,
                    v.contact_name as vendor_contact
                FROM rent_requests rr
                JOIN vendors v ON rr.vendor_email = v.email
                LEFT JOIN equipment e ON rr.equipment_id = e.id
                WHERE rr.user_id = ?
                ORDER BY rr.submitted_date DESC
            """, (user_id,))
        
        rent_requests = cursor_vendors.fetchall()
        print(f"✅ Found {len(rent_requests)} rent requests")
        
        conn_vendors.close()
        
        # Combine and format orders
        orders_list = []
        
        # Process bookings
        for booking in bookings:
            vendor_name = booking['vendor_name'] or booking['vendor_contact'] or 'Vendor'
            
            # Get cancellation fields (may be None if columns don't exist)
            cancellation_requested_date = booking['cancellation_requested_date'] if 'cancellation_requested_date' in booking.keys() else None
            cancellation_reason = booking['cancellation_reason'] if 'cancellation_reason' in booking.keys() else None
            status_before_cancel = booking['status_before_cancel'] if 'status_before_cancel' in booking.keys() else None
            cancelled_date = booking['cancelled_date'] if 'cancelled_date' in booking.keys() else None
            
            orders_list.append({
                'id': booking['id'],
                'order_type': 'booking',
                'equipment_name': booking['equipment_name'],
                'vendor_name': vendor_name,
                'vendor_email': booking['vendor_email'],
                'vendor_contact': booking['vendor_contact'],
                'business_name': booking['business_name'],
                'start_date': booking['start_date'],
                'end_date': booking['end_date'],
                'duration': booking['duration'],
                'total_amount': float(booking['total_amount']),
                'status': booking['status'],
                'created_date': booking['created_date'],
                'cancellation_requested_date': cancellation_requested_date,
                'cancellation_reason': cancellation_reason,
                'status_before_cancel': status_before_cancel,
                'cancelled_date': cancelled_date,
                'equipment_image': booking['equipment_image'],
                'equipment_id': booking['equipment_id'],
                'user_name': booking['user_name'],
                'user_email': booking['user_email'],
                'user_phone': booking['user_phone'],
                'can_cancel': booking['status'] in ['pending', 'confirmed'] and booking['status'] != 'cancellation_requested',
                'is_cancellation_requested': booking['status'] == 'cancellation_requested'
            })
        
        # Process rent requests
        for rent in rent_requests:
            vendor_name = rent['vendor_name'] or rent['vendor_contact'] or 'Vendor'
            
            # Get cancellation fields (may be None if columns don't exist)
            cancellation_requested_date = rent['cancellation_requested_date'] if 'cancellation_requested_date' in rent.keys() else None
            cancellation_reason = rent['cancellation_reason'] if 'cancellation_reason' in rent.keys() else None
            status_before_cancel = rent['status_before_cancel'] if 'status_before_cancel' in rent.keys() else None
            cancelled_date = rent['cancelled_date'] if 'cancelled_date' in rent.keys() else None
            
            orders_list.append({
                'id': rent['id'],
                'order_type': 'rent',
                'equipment_name': rent['equipment_name'],
                'vendor_name': vendor_name,
                'vendor_email': rent['vendor_email'],
                'vendor_contact': rent['vendor_contact'],
                'business_name': rent['business_name'],
                'start_date': rent['start_date'],
                'end_date': rent['end_date'],
                'duration': rent['duration'],
                'total_amount': float(rent['total_amount']),
                'status': rent['status'],
                'created_date': rent['created_date'],
                'cancellation_requested_date': cancellation_requested_date,
                'cancellation_reason': cancellation_reason,
                'status_before_cancel': status_before_cancel,
                'cancelled_date': cancelled_date,
                'equipment_image': rent['equipment_image'],
                'equipment_id': rent['equipment_id'],
                'user_name': rent['user_name'],
                'user_email': rent['user_email'],
                'user_phone': rent['user_phone'],
                'can_cancel': rent['status'] in ['pending', 'approved'] and rent['status'] != 'cancellation_requested',
                'is_cancellation_requested': rent['status'] == 'cancellation_requested'
            })
        
        # Sort by creation date (newest first)
        orders_list.sort(key=lambda x: x['created_date'], reverse=True)
        
        print(f"📦 Total orders to return: {len(orders_list)}")
        return jsonify(orders_list)
        
    except Exception as e:
        print(f"❌ Error fetching user orders: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
@app.route('/api/admin/broadcast', methods=['POST'])
def api_admin_send_broadcast():
    """Send broadcast message to all farmers"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        title = data.get('title', '').strip()
        content = data.get('content', '').strip()
        message_type = data.get('type', 'announcement')
        
        if not title or not content:
            return jsonify({'error': 'Title and content are required'}), 400
        
        # Get all approved farmers' phone numbers
        conn = sqlite3.connect('agriculture.db')
        cursor = conn.cursor()
        cursor.execute("SELECT full_name, phone FROM farmers WHERE status = 'approved'")
        farmers = cursor.fetchall()
        conn.close()
        
        if not farmers:
            return jsonify({'error': 'No approved farmers found'}), 400
        
        success_count = 0
        failed_count = 0
        
        # Format the message - KEEP IT SIMPLE, NO EMOJIS
        full_message = f"{title}\n\n{content}\n\n- Lend A Hand"
        
        print(f"📢 Starting broadcast to {len(farmers)} farmers")
        
        # Send SMS to each farmer
        for farmer in farmers:
            farmer_name, farmer_phone = farmer
            
            try:
                # Get phone number as string and clean it
                phone = str(farmer_phone).strip()
                
                # Remove all non-digit characters (spaces, dashes, etc.)
                phone = ''.join(filter(str.isdigit, phone))
                
                print(f"📱 Sending to {farmer_name}: {phone}")
                
                # Send SMS using the cleaned number
                sms_result = send_sms(phone, full_message)
                
                if sms_result.get('success'):
                    success_count += 1
                    print(f"✅ Sent to {farmer_name}")
                else:
                    failed_count += 1
                    print(f"❌ Failed for {farmer_name}: {sms_result.get('error')}")
                    
            except Exception as e:
                failed_count += 1
                print(f"❌ Error for {farmer_name}: {str(e)}")
        
        # Create response - ALWAYS return success for the API call itself
        response_data = {
            'success': True,  # Always true, the API call itself succeeded
            'message': f'Broadcast completed. Sent to {success_count} of {len(farmers)} farmers',
            'stats': {
                'total': len(farmers),
                'success': success_count,
                'failed': failed_count
            }
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        print(f"Error sending broadcast: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/admin/farmers-count')
def api_admin_farmers_count():
    """Get count of approved farmers for broadcast"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('agriculture.db')
        cursor = conn.cursor()
        
        # Get count of approved farmers
        cursor.execute("SELECT COUNT(*) FROM farmers WHERE status = 'approved'")
        count = cursor.fetchone()[0]
        
        conn.close()
        
        return jsonify({
            'total_farmers': count,
            'success': True
        })
        
    except Exception as e:
        print(f"Error fetching farmers count: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/user/order/request-cancel', methods=['POST'])
def request_order_cancellation():
    """Request cancellation for an order - STORES ONLY ESSENTIAL DATA"""
    print("📥 Received cancellation request")
    
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        print("📦 Request data:", data)
        
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400
            
        order_id = data.get('order_id')
        order_type = data.get('order_type')  # 'booking' or 'rent'
        cancellation_reason = data.get('cancellation_reason', '')
        
        print(f"🔍 Processing: {order_type} #{order_id}, reason: {cancellation_reason}")
        
        if not order_id or not order_type:
            return jsonify({'error': 'Order ID and type are required'}), 400
        
        user_id = session['user_id']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Get ONLY the essential data that shows in order details
        if order_type == 'booking':
            cursor.execute("""
                SELECT 
                    b.equipment_id,
                    b.equipment_name,
                    b.status,
                    b.total_amount,
                    b.created_date,
                    b.start_date,
                    b.end_date,
                    b.duration,
                    b.vendor_email,
                    b.user_name,
                    b.user_email,
                    b.user_phone,
                    v.contact_name as vendor_name
                FROM bookings b
                JOIN vendors v ON b.vendor_email = v.email
                WHERE b.id = ? AND b.user_id = ?
            """, (order_id, user_id))
        else:  # rent
            cursor.execute("""
                SELECT 
                    rr.equipment_id,
                    rr.equipment_name,
                    rr.status,
                    rr.total_amount,
                    rr.submitted_date as created_date,
                    rr.start_date,
                    rr.end_date,
                    rr.duration,
                    rr.vendor_email,
                    rr.user_name,
                    rr.user_email,
                    rr.user_phone,
                    v.contact_name as vendor_name
                FROM rent_requests rr
                JOIN vendors v ON rr.vendor_email = v.email
                WHERE rr.id = ? AND rr.user_id = ?
            """, (order_id, user_id))
        
        order = cursor.fetchone()
        
        if not order:
            conn.close()
            return jsonify({'error': 'Order not found or access denied'}), 404
        
        # Extract ONLY essential data
        equipment_id, equipment_name, status, total_amount, created_date, start_date, end_date, duration, vendor_email, user_name, user_email, user_phone, vendor_name = order
        
        print("📋 Extracted essential order details:")
        print(f"   - Equipment: {equipment_name}")
        print(f"   - Vendor: {vendor_name} ({vendor_email})")
        print(f"   - User: {user_name} ({user_email}, {user_phone})")
        print(f"   - Amount: ₹{total_amount}")
        print(f"   - Dates: {start_date} to {end_date}")
        print(f"   - Duration: {duration} days")
        
        # Update order status to cancellation_requested
        if order_type == 'booking':
            cursor.execute("""
                UPDATE bookings 
                SET status = 'cancellation_requested',
                    cancellation_requested_date = CURRENT_TIMESTAMP,
                    cancellation_reason = ?,
                    status_before_cancel = ?
                WHERE id = ? AND user_id = ?
            """, (cancellation_reason, status, order_id, user_id))
        else:  # rent
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'cancellation_requested',
                    cancellation_requested_date = CURRENT_TIMESTAMP,
                    cancellation_reason = ?,
                    status_before_cancel = ?
                WHERE id = ? AND user_id = ?
            """, (cancellation_reason, status, order_id, user_id))
        
        # Store ONLY essential data - remove order_notes, purpose, user_location
        cursor.execute("""
            INSERT INTO cancellation_requests 
            (order_id, order_type, user_id, user_name, user_email, user_phone,
             vendor_email, vendor_name, equipment_id, equipment_name, total_amount, 
             start_date, end_date, duration, order_status_before_cancel, 
             order_created_date, cancellation_reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            order_id, 
            order_type, 
            user_id, 
            user_name, 
            user_email, 
            user_phone,
            vendor_email, 
            vendor_name,
            equipment_id,
            equipment_name,
            total_amount, 
            start_date,
            end_date, 
            duration,
            status, 
            created_date,
            cancellation_reason
        ))
        
        cancellation_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        print(f"✅ Cancellation request #{cancellation_id} stored successfully")
        
        return jsonify({
            'success': True,
            'message': 'Cancellation request submitted successfully! Waiting for vendor approval.',
            'cancellation_id': cancellation_id
        })
        
    except Exception as e:
        print(f"❌ Error in cancellation request: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/cleanup-cancellation-table')
def cleanup_cancellation_table():
    """Remove unnecessary columns from cancellation_requests table"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Create a new table without unnecessary columns
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cancellation_requests_clean (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                user_email TEXT NOT NULL,
                user_phone TEXT NOT NULL,
                vendor_email TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                equipment_id INTEGER NOT NULL,
                equipment_name TEXT NOT NULL,
                total_amount REAL NOT NULL,
                start_date TEXT,
                end_date TEXT,
                duration INTEGER,
                order_status_before_cancel TEXT NOT NULL,
                order_created_date TEXT,
                cancellation_reason TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                requested_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_date TIMESTAMP,
                processed_by TEXT
            )
        """)
        
        # Copy data from old table to new table
        cursor.execute("""
            INSERT INTO cancellation_requests_clean 
            (order_id, order_type, user_id, user_name, user_email, user_phone,
             vendor_email, vendor_name, equipment_id, equipment_name, total_amount,
             start_date, end_date, duration, order_status_before_cancel,
             order_created_date, cancellation_reason, status, requested_date,
             processed_date, processed_by)
            SELECT 
                order_id, order_type, user_id, user_name, user_email, user_phone,
                vendor_email, vendor_name, equipment_id, equipment_name, total_amount,
                start_date, end_date, duration, order_status_before_cancel,
                order_created_date, cancellation_reason, status, requested_date,
                processed_date, processed_by
            FROM cancellation_requests
        """)
        
        # Drop old table and rename new one
        cursor.execute("DROP TABLE cancellation_requests")
        cursor.execute("ALTER TABLE cancellation_requests_clean RENAME TO cancellation_requests")
        
        conn.commit()
        conn.close()
        
        return "✅ Cancellation table cleaned up successfully!"
        
    except Exception as e:
        return f"❌ Error: {str(e)}"
@app.route('/debug-vendor-cancellations')
def debug_vendor_cancellations():
    """Debug vendor cancellation requests"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Get all cancellation requests for this vendor
        cursor.execute("""
            SELECT id, order_id, order_type, equipment_name, user_name, 
                   cancellation_reason, status, requested_date
            FROM cancellation_requests 
            WHERE vendor_email = ? 
            ORDER BY requested_date DESC
        """, (vendor_email,))
        
        cancellations = cursor.fetchall()
        conn.close()
        
        cancellations_list = []
        for cancel in cancellations:
            cancellations_list.append({
                'cancellation_id': cancel[0],
                'order_id': cancel[1],
                'order_type': cancel[2],
                'equipment_name': cancel[3],
                'user_name': cancel[4],
                'cancellation_reason': cancel[5],
                'status': cancel[6],
                'requested_date': cancel[7]
            })
        
        return jsonify({
            'vendor_email': vendor_email,
            'cancellation_requests': cancellations_list,
            'total_requests': len(cancellations_list)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)})
@app.route('/check-cancellation-storage')
def check_cancellation_storage():
    """Check what's actually stored in cancellation_requests"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Get the latest cancellation request
        cursor.execute("""
            SELECT 
                equipment_name, vendor_name, vendor_email,
                user_name, user_email, user_phone,
                total_amount, start_date, end_date, duration,
                cancellation_reason, status
            FROM cancellation_requests 
            ORDER BY id DESC LIMIT 1
        """)
        latest = cursor.fetchone()
        
        conn.close()
        
        if latest:
            return jsonify({
                'stored_data': {
                    'equipment': latest[0],
                    'vendor_name': latest[1],
                    'vendor_email': latest[2],
                    'user_name': latest[3],
                    'user_email': latest[4],
                    'user_phone': latest[5],
                    'total_amount': latest[6],
                    'start_date': latest[7],
                    'end_date': latest[8],
                    'duration': latest[9],
                    'cancellation_reason': latest[10],
                    'status': latest[11]
                }
            })
        else:
            return jsonify({'message': 'No cancellation requests found'})
            
    except Exception as e:
        return jsonify({'error': str(e)})
@app.route('/api/vendor/cancellation-requests/details')
def get_vendor_cancellation_requests_details():
    """Get complete cancellation request details for vendor dashboard"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        status_filter = request.args.get('status', 'pending')
        
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = """
            SELECT * FROM cancellation_requests 
            WHERE vendor_email = ?
        """
        
        params = [vendor_email]
        
        if status_filter != 'all':
            query += " AND status = ?"
            params.append(status_filter)
        
        query += " ORDER BY requested_date DESC"
        
        cursor.execute(query, params)
        cancellation_requests = cursor.fetchall()
        conn.close()
        
        # Format complete response
        requests_list = []
        for req in cancellation_requests:
            requests_list.append({
                # Cancellation Info
                'cancellation_id': req['id'],
                'order_type': req['order_type'],
                'order_id': req['order_id'],
                'requested_date': req['requested_date'],
                'cancellation_reason': req['cancellation_reason'],
                'status': req['status'],
                
                # ✅ User Information (Complete)
                'user_name': req['user_name'],
                'user_email': req['user_email'],
                'user_phone': req['user_phone'],
                'user_location': req['user_location'],
                'user_id': req['user_id'],
                
                # ✅ Vendor Information
                'vendor_name': req['vendor_name'],
                'vendor_business_name': req['vendor_business_name'],
                'vendor_contact_phone': req['vendor_contact_phone'],
                
                # ✅ Equipment Information (Complete)
                'equipment_name': req['equipment_name'],
                'equipment_category': req['equipment_category'],
                'equipment_description': req['equipment_description'],
                'equipment_price': req['equipment_price'],
                'equipment_price_unit': req['equipment_price_unit'],
                'equipment_location': req['equipment_location'],
                'equipment_image_url': req['equipment_image_url'],
                
                # ✅ Order Information (Complete)
                'total_amount': req['total_amount'],
                'start_date': req['start_date'],
                'end_date': req['end_date'],
                'duration': req['duration'],
                'order_notes': req['order_notes'],
                'purpose': req['purpose'],
                'order_status_before_cancel': req['order_status_before_cancel'],
                'order_created_date': req['order_created_date'],
                
                # Additional Info
                'days_until_start': req['days_until_start'],
                'is_urgent': bool(req['is_urgent']),
                'processed_date': req['processed_date'],
                'vendor_response_notes': req['vendor_response_notes']
            })
        
        print(f"📊 Returning {len(requests_list)} cancellation requests with complete details")
        return jsonify(requests_list)
        
    except Exception as e:
        print(f"❌ Error fetching cancellation requests: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/vendor/cancellation-requests')
def get_vendor_cancellation_requests():
    """Get pending cancellation requests for vendor - WITH COMPLETE DETAILS"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get ALL cancellation requests (not just pending)
        cursor.execute("""
            SELECT 
                cr.id as cancellation_id,
                cr.order_type,
                cr.order_id,
                cr.user_name,
                cr.user_phone,
                cr.user_email,
                cr.equipment_name,
                cr.total_amount,
                cr.start_date,
                cr.end_date,
                cr.cancellation_reason,
                cr.requested_date,
                cr.order_status_before_cancel as previous_status,
                cr.status,
                cr.order_created_date,
                cr.processed_date,
                v.business_name,
                v.contact_name as vendor_contact_name,
                cr.equipment_id
            FROM cancellation_requests cr
            LEFT JOIN vendors v ON cr.vendor_email = v.email
            WHERE cr.vendor_email = ?
            ORDER BY cr.requested_date DESC
        """, (vendor_email,))
        
        cancellation_requests = cursor.fetchall()
        
        conn.close()
        
        # Format response with ALL details
        requests_list = []
        for request in cancellation_requests:
            requests_list.append({
                'cancellation_id': request['cancellation_id'],
                'order_type': request['order_type'],
                'order_id': request['order_id'],
                'user_name': request['user_name'],
                'user_phone': request['user_phone'],
                'user_email': request['user_email'],
                'equipment_name': request['equipment_name'],
                'total_amount': request['total_amount'],
                'start_date': request['start_date'],
                'end_date': request['end_date'],
                'cancellation_reason': request['cancellation_reason'],
                'requested_date': request['requested_date'],
                'previous_status': request['previous_status'],
                'status': request['status'],
                'order_created_date': request['order_created_date'],
                'processed_date': request['processed_date'],
                'vendor_business_name': request['business_name'],
                'vendor_contact_name': request['vendor_contact_name'],
                'vendor_email': vendor_email,
                'equipment_id': request['equipment_id']
            })
        
        print(f"📊 Returning {len(requests_list)} cancellation requests with complete details")
        return jsonify(requests_list)
        
    except Exception as e:
        print(f"❌ Error fetching vendor cancellation requests: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/user/order/<int:order_id>')
def get_order_details(order_id):
    try:
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': 'Not authenticated'}), 401
        
        order_type = request.args.get('type', 'booking')
        
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        
        if order_type == 'booking':
            order = conn.execute("""
                SELECT b.*, e.image_url as equipment_image, 
                       v.contact_name as vendor_name,  -- CHANGED: Use contact_name
                       v.email as vendor_email, 
                       v.business_name,
                       b.user_name, b.user_email, b.user_phone
                FROM bookings b
                LEFT JOIN equipment e ON b.equipment_id = e.id
                LEFT JOIN vendors v ON b.vendor_email = v.email
                WHERE b.id = ? AND b.user_id = ?
            """, (order_id, user_id)).fetchone()
        else:  # rent
            order = conn.execute("""
                SELECT rr.*, e.image_url as equipment_image, 
                       v.contact_name as vendor_name,  -- CHANGED: Use contact_name
                       v.email as vendor_email,
                       v.business_name,
                       rr.user_name, rr.user_email, rr.user_phone
                FROM rent_requests rr
                LEFT JOIN equipment e ON rr.equipment_id = e.id
                LEFT JOIN vendors v ON rr.vendor_email = v.email
                WHERE rr.id = ? AND rr.user_id = ?
            """, (order_id, user_id)).fetchone()
        
        conn.close()
        
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        # Format order details with contact name as vendor_name
        if order_type == 'booking':
            order_details = {
                'id': order['id'],
                'order_type': 'booking',
                'equipment_name': order['equipment_name'],
                'vendor_name': order['vendor_name'],  # This is now contact_name
                'vendor_email': order['vendor_email'],
                'business_name': order['business_name'],
                'start_date': order['start_date'],
                'end_date': order['end_date'],
                'duration': order['duration'],
                'total_amount': order['total_amount'],
                'status': order['status'],
                'notes': order['notes'],
                'created_date': order['created_date'],
                'cancellation_requested_date': order['cancellation_requested_date'],
                'cancellation_reason': order['cancellation_reason'],
                'equipment_image': order['equipment_image'],
                'user_name': order['user_name'],
                'user_email': order['user_email'],
                'user_phone': order['user_phone']
            }
        else:  # rent
            order_details = {
                'id': order['id'],
                'order_type': 'rent',
                'equipment_name': order['equipment_name'],
                'vendor_name': order['vendor_name'],  # This is now contact_name
                'vendor_email': order['vendor_email'],
                'business_name': order['business_name'],
                'start_date': order['start_date'],
                'end_date': order['end_date'],
                'duration': order['duration'],
                'total_amount': order['total_amount'],
                'status': order['status'],
                'purpose': order['purpose'],
                'notes': order['notes'],
                'created_date': order['submitted_date'],
                'cancellation_requested_date': order['cancellation_requested_date'],
                'cancellation_reason': order['cancellation_reason'],
                'equipment_image': order['equipment_image'],
                'user_name': order['user_name'],
                'user_email': order['user_email'],
                'user_phone': order['user_phone']
            }
        
        return jsonify(order_details)
        
    except Exception as e:
        print(f"Error fetching order details: {e}")
        return jsonify({'error': 'Failed to fetch order details'}), 500

@app.route('/api/vendor/cancellation-request/approve', methods=['POST'])
def approve_cancellation_request():
    """Vendor approves a cancellation request - USING DEDICATED TABLE"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        cancellation_id = data.get('cancellation_id')
        
        if not cancellation_id:
            return jsonify({'error': 'Missing cancellation ID'}), 400
        
        vendor_email = session['vendor_email']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Get cancellation request details
        cursor.execute("""
            SELECT order_id, order_type, equipment_id, user_phone, user_name, equipment_name
            FROM cancellation_requests 
            WHERE id = ? AND vendor_email = ? AND status = 'pending'
        """, (cancellation_id, vendor_email))
        
        cancellation_request = cursor.fetchone()
        
        if not cancellation_request:
            conn.close()
            return jsonify({'error': 'Cancellation request not found or access denied'}), 404
        
        order_id, order_type, equipment_id, user_phone, user_name, equipment_name = cancellation_request
        
        if order_type == 'booking':
            # Update booking status to cancelled
            cursor.execute("""
                UPDATE bookings 
                SET status = 'cancelled'
                WHERE id = ? AND vendor_email = ?
            """, (order_id, vendor_email))
            
        elif order_type == 'rent':
            # Update rent request status to cancelled
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'cancelled'
                WHERE id = ? AND vendor_email = ?
            """, (order_id, vendor_email))
        
        # Restock equipment for both booking and rent
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = stock_quantity + 1,
                status = CASE 
                    WHEN stock_quantity + 1 > 0 THEN 'available' 
                    ELSE status 
                END
            WHERE id = ?
        """, (equipment_id,))
        
        # Update cancellation request status - with error handling for missing columns
        try:
            cursor.execute("""
                UPDATE cancellation_requests 
                SET status = 'approved', 
                    processed_date = CURRENT_TIMESTAMP,
                    processed_by = 'vendor'
                WHERE id = ?
            """, (cancellation_id,))
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                # Fallback if columns don't exist yet
                cursor.execute("""
                    UPDATE cancellation_requests 
                    SET status = 'approved'
                    WHERE id = ?
                """, (cancellation_id,))
                print("⚠️ Using fallback update (missing columns)")
            else:
                raise e
        
        conn.commit()
        conn.close()
        
        # Send notification to user
        sms_message = f"Dear {user_name}, your cancellation request for {equipment_name} has been approved by the vendor. Equipment has been restocked. - Lend A Hand"
        send_sms(user_phone, sms_message)
        
        return jsonify({
            'success': True,
            'message': 'Cancellation approved and equipment restocked successfully'
        })
        
    except Exception as e:
        print(f"❌ Error approving cancellation: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/vendor/cancellation-request/reject', methods=['POST'])
def reject_cancellation_request():
    """Vendor rejects a cancellation request - USING DEDICATED TABLE"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        cancellation_id = data.get('cancellation_id')
        
        if not cancellation_id:
            return jsonify({'error': 'Missing cancellation ID'}), 400
        
        vendor_email = session['vendor_email']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Get cancellation request details
        cursor.execute("""
            SELECT order_id, order_type, user_phone, user_name, equipment_name, order_status_before_cancel
            FROM cancellation_requests 
            WHERE id = ? AND vendor_email = ? AND status = 'pending'
        """, (cancellation_id, vendor_email))
        
        cancellation_request = cursor.fetchone()
        
        if not cancellation_request:
            conn.close()
            return jsonify({'error': 'Cancellation request not found'}), 404
        
        order_id, order_type, user_phone, user_name, equipment_name, previous_status = cancellation_request
        
        if order_type == 'booking':
            # Restore booking to previous status
            cursor.execute("""
                UPDATE bookings 
                SET status = ? 
                WHERE id = ? AND vendor_email = ?
            """, (previous_status, order_id, vendor_email))
            
        elif order_type == 'rent':
            # Restore rent request to previous status
            cursor.execute("""
                UPDATE rent_requests 
                SET status = ? 
                WHERE id = ? AND vendor_email = ?
            """, (previous_status, order_id, vendor_email))
        
        # Update cancellation request status - with error handling for missing columns
        try:
            cursor.execute("""
                UPDATE cancellation_requests 
                SET status = 'rejected', 
                    processed_date = CURRENT_TIMESTAMP,
                    processed_by = 'vendor'
                WHERE id = ?
            """, (cancellation_id,))
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                # Fallback if columns don't exist yet
                cursor.execute("""
                    UPDATE cancellation_requests 
                    SET status = 'rejected'
                    WHERE id = ?
                """, (cancellation_id,))
                print("⚠️ Using fallback update (missing columns)")
            else:
                raise e
        
        conn.commit()
        conn.close()
        
        # Send notification to user
        sms_message = f"Your cancellation request for {equipment_name} has been rejected. Order remains active. - Lend A Hand"
        send_sms(user_phone, sms_message)
        
        return jsonify({
            'success': True,
            'message': 'Cancellation rejected'
        })
        
    except Exception as e:
        print(f"❌ Error rejecting cancellation: {str(e)}")
        return jsonify({'error': str(e)}), 500



@app.route('/api/user/order/cancel', methods=['POST'])
def cancel_user_order():
    """Cancel a user order (booking or rent request) and restock equipment"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        order_type = data.get('order_type')  # 'booking' or 'rent'
        order_id = data.get('order_id')
        cancellation_reason = data.get('cancellation_reason', '')
        
        if not order_type or not order_id:
            return jsonify({'error': 'Missing order type or ID'}), 400
        
        user_id = session['user_id']
        
        conn_vendors = sqlite3.connect('vendors.db')
        cursor_vendors = conn_vendors.cursor()
        
        if order_type == 'booking':
            # Check if booking exists and belongs to user
            cursor_vendors.execute("""
                SELECT equipment_id, status FROM bookings 
                WHERE id = ? AND user_id = ?
            """, (order_id, user_id))
            
            booking = cursor_vendors.fetchone()
            
            if not booking:
                conn_vendors.close()
                return jsonify({'error': 'Booking not found'}), 404
            
            equipment_id, current_status = booking
            
            # Only allow cancellation for pending or confirmed bookings
            if current_status not in ['pending', 'confirmed']:
                conn_vendors.close()
                return jsonify({'error': 'Cannot cancel booking with current status'}), 400
            
            # Update booking status to cancelled
            cursor_vendors.execute("""
                UPDATE bookings 
                SET status = 'cancelled', 
                    cancelled_date = CURRENT_TIMESTAMP,
                    cancellation_reason = ?
                WHERE id = ? AND user_id = ?
            """, (cancellation_reason, order_id, user_id))
            
            # Restock equipment
            cursor_vendors.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 > 0 THEN 'available' 
                        ELSE status 
                    END
                WHERE id = ?
            """, (equipment_id,))
            
        elif order_type == 'rent':
            # Check if rent request exists and belongs to user
            cursor_vendors.execute("""
                SELECT equipment_id, status FROM rent_requests 
                WHERE id = ? AND user_id = ?
            """, (order_id, user_id))
            
            rent_request = cursor_vendors.fetchone()
            
            if not rent_request:
                conn_vendors.close()
                return jsonify({'error': 'Rent request not found'}), 404
            
            equipment_id, current_status = rent_request
            
            # Only allow cancellation for pending or approved rent requests
            if current_status not in ['pending', 'approved']:
                conn_vendors.close()
                return jsonify({'error': 'Cannot cancel rent request with current status'}), 400
            
            # Update rent request status to cancelled
            cursor_vendors.execute("""
                UPDATE rent_requests 
                SET status = 'cancelled', 
                    cancelled_date = CURRENT_TIMESTAMP,
                    cancellation_reason = ?
                WHERE id = ? AND user_id = ?
            """, (cancellation_reason, order_id, user_id))
            
            # Restock equipment
            cursor_vendors.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 > 0 THEN 'available' 
                        ELSE status 
                    END
                WHERE id = ?
            """, (equipment_id,))
        
        else:
            conn_vendors.close()
            return jsonify({'error': 'Invalid order type'}), 400
        
        conn_vendors.commit()
        conn_vendors.close()
        
        return jsonify({
            'success': True,
            'message': f'{order_type.capitalize()} cancelled successfully'
        })
        
    except Exception as e:
        print(f"Error cancelling order: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/fix-cancellation-columns')
def fix_cancellation_columns():
    """Fix missing cancellation columns in database"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check and add columns to bookings table
        cursor.execute("PRAGMA table_info(bookings)")
        booking_columns = [column[1] for column in cursor.fetchall()]
        
        cancellation_columns = [
            ('cancellation_requested_date', 'TIMESTAMP'),
            ('cancellation_reason', 'TEXT'),
            ('status_before_cancel', 'TEXT'),
            ('cancelled_date', 'TIMESTAMP')
        ]
        
        for col_name, col_type in cancellation_columns:
            if col_name not in booking_columns:
                cursor.execute(f"ALTER TABLE bookings ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added {col_name} to bookings")
        
        # Check and add columns to rent_requests table
        cursor.execute("PRAGMA table_info(rent_requests)")
        rent_columns = [column[1] for column in cursor.fetchall()]
        
        for col_name, col_type in cancellation_columns:
            if col_name not in rent_columns:
                cursor.execute(f"ALTER TABLE rent_requests ADD COLUMN {col_name} {col_type}")
                print(f"✅ Added {col_name} to rent_requests")
        
        conn.commit()
        conn.close()
        return "✅ Cancellation columns fixed successfully!"
        
    except Exception as e:
        return f"❌ Error: {str(e)}"
# Add this route to your Flask app to fix the database
@app.route('/api/user/booking/<int:booking_id>/request-cancel', methods=['POST'])
def request_booking_cancellation(booking_id):
    """Request cancellation for a specific booking"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        cancellation_reason = data.get('cancellation_reason', 'No reason provided')
        
        user_id = session['user_id']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check if booking exists and belongs to user
        cursor.execute("""
            SELECT status FROM bookings 
            WHERE id = ? AND user_id = ?
        """, (booking_id, user_id))
        
        booking = cursor.fetchone()
        
        if not booking:
            conn.close()
            return jsonify({'error': 'Booking not found'}), 404
        
        current_status = booking[0]
        
        # Only allow cancellation for pending or confirmed bookings
        if current_status not in ['pending', 'confirmed']:
            conn.close()
            return jsonify({'error': 'Cannot cancel booking with current status'}), 400
        
        # Update booking status to cancellation_requested
        cursor.execute("""
            UPDATE bookings 
            SET status = 'cancellation_requested', 
                cancellation_requested_date = CURRENT_TIMESTAMP,
                cancellation_reason = ?,
                status_before_cancel = ?
            WHERE id = ? AND user_id = ?
        """, (cancellation_reason, current_status, booking_id, user_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Cancellation request submitted successfully! Waiting for vendor approval.'
        })
        
    except Exception as e:
        print(f"Error requesting booking cancellation: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/rent-request/<int:request_id>/request-cancel', methods=['POST'])
def request_rent_cancellation(request_id):
    """Request cancellation for a specific rent request"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        cancellation_reason = data.get('cancellation_reason', 'No reason provided')
        
        user_id = session['user_id']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check if rent request exists and belongs to user
        cursor.execute("""
            SELECT status FROM rent_requests 
            WHERE id = ? AND user_id = ?
        """, (request_id, user_id))
        
        rent_request = cursor.fetchone()
        
        if not rent_request:
            conn.close()
            return jsonify({'error': 'Rent request not found'}), 404
        
        current_status = rent_request[0]
        
        # Only allow cancellation for pending or approved rent requests
        if current_status not in ['pending', 'approved']:
            conn.close()
            return jsonify({'error': 'Cannot cancel rent request with current status'}), 400
        
        # Update rent request status to cancellation_requested
        cursor.execute("""
            UPDATE rent_requests 
            SET status = 'cancellation_requested', 
                cancellation_requested_date = CURRENT_TIMESTAMP,
                cancellation_reason = ?,
                status_before_cancel = ?
            WHERE id = ? AND user_id = ?
        """, (cancellation_reason, current_status, request_id, user_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Cancellation request submitted successfully! Waiting for vendor approval.'
        })
        
    except Exception as e:
        print(f"Error requesting rent cancellation: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/fix-cancellation-db')
def fix_cancellation_db():
    """Add cancellation columns to existing tables using ALTER TABLE"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        print("🔄 Adding cancellation columns to bookings table...")
        
        # Add cancellation columns to bookings table
        try:
            cursor.execute("ALTER TABLE bookings ADD COLUMN cancellation_requested_date TIMESTAMP")
            print("✅ Added cancellation_requested_date to bookings")
        except sqlite3.OperationalError:
            print("⚠️ cancellation_requested_date already exists in bookings")
        
        try:
            cursor.execute("ALTER TABLE bookings ADD COLUMN cancellation_reason TEXT")
            print("✅ Added cancellation_reason to bookings")
        except sqlite3.OperationalError:
            print("⚠️ cancellation_reason already exists in bookings")
        
        try:
            cursor.execute("ALTER TABLE bookings ADD COLUMN status_before_cancel TEXT")
            print("✅ Added status_before_cancel to bookings")
        except sqlite3.OperationalError:
            print("⚠️ status_before_cancel already exists in bookings")
        
        try:
            cursor.execute("ALTER TABLE bookings ADD COLUMN cancelled_date TIMESTAMP")
            print("✅ Added cancelled_date to bookings")
        except sqlite3.OperationalError:
            print("⚠️ cancelled_date already exists in bookings")
        
        print("🔄 Adding cancellation columns to rent_requests table...")
        
        # Add cancellation columns to rent_requests table
        try:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN cancellation_requested_date TIMESTAMP")
            print("✅ Added cancellation_requested_date to rent_requests")
        except sqlite3.OperationalError:
            print("⚠️ cancellation_requested_date already exists in rent_requests")
        
        try:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN cancellation_reason TEXT")
            print("✅ Added cancellation_reason to rent_requests")
        except sqlite3.OperationalError:
            print("⚠️ cancellation_reason already exists in rent_requests")
        
        try:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN status_before_cancel TEXT")
            print("✅ Added status_before_cancel to rent_requests")
        except sqlite3.OperationalError:
            print("⚠️ status_before_cancel already exists in rent_requests")
        
        try:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN cancelled_date TIMESTAMP")
            print("✅ Added cancelled_date to rent_requests")
        except sqlite3.OperationalError:
            print("⚠️ cancelled_date already exists in rent_requests")
        
        conn.commit()
        conn.close()
        return "✅ Database tables updated with cancellation columns"
        
    except Exception as e:
        return f"❌ Error: {str(e)}"



@app.route('/userreg', methods=['GET', 'POST'])
def userreg():
    if request.method == 'POST':                                                                    
        full_name = request.form.get('full_name')
        last_name = request.form.get('last_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        farm_location = request.form.get('farm_location')
        farm_size = request.form.get('farm_size')
        crop_types = ','.join(request.form.getlist('crop_types'))
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        additional_info = request.form.get('additional_info')

        # File upload handling
        rtc_document = request.files.get('rtc_document')
        rtc_filename = None
        
        if rtc_document and rtc_document.filename:
            if allowed_file(rtc_document.filename):
                filename = secure_filename(rtc_document.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                rtc_document.save(filepath)
                rtc_filename = unique_filename
            else:
                flash('Invalid file type for RTC document. Please upload PDF, JPG, or PNG files.', 'error')
                return render_template('userreg.html')

        # Password check
        if password != confirm_password:
            flash('Passwords do not match!', 'error')
            return render_template('userreg.html')

        import re
        if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', password):
            flash('Password must have 8+ chars, uppercase, lowercase, number, special char.', 'error')
            return render_template('userreg.html')

        hashed_password = generate_password_hash(password)

        # Save to DB
        try:
            conn = sqlite3.connect('agriculture.db')
            c = conn.cursor()

            if email:
                c.execute("SELECT id FROM farmers WHERE email = ?", (email,))
                if c.fetchone():
                    flash('Email already registered!', 'error')
                    conn.close()
                    return render_template('userreg.html')

            c.execute('''INSERT INTO farmers 
                        (full_name, last_name, email, phone, farm_location, farm_size, 
                         crop_types, password, additional_info, rtc_document)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (full_name, last_name, email, phone, farm_location, farm_size,
                       crop_types, hashed_password, additional_info, rtc_filename))
            conn.commit()
            conn.close()

            # Send SMS after registration
            sms_message = "Thank you for registering with us! Your form is under process."
            send_sms(phone, sms_message)

            flash('Your farmer application has been submitted successfully! Please login.', 'success')
            return redirect(url_for('farmer_login'))

        except sqlite3.Error as e:
            flash(f'Error: {str(e)}', 'error')
            return render_template('userreg.html')

    return render_template('userreg.html')

@app.route('/vendorreg', methods=['GET', 'POST'])
def vendor_registration():
    if request.method == 'POST':
        business_name = request.form.get('business_name')
        contact_name = request.form.get('contact_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        service_type = request.form.get('service_type')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        description = request.form.get('description')
        
        # === ADD THIS SECTION FOR DOCUMENT UPLOAD ===
        business_document = request.files.get('business_document')
        document_filename = None
        
        if business_document and business_document.filename:
            allowed_extensions = {'pdf', 'jpg', 'jpeg', 'png'}
            if '.' in business_document.filename and \
               business_document.filename.rsplit('.', 1)[1].lower() in allowed_extensions:
                
                filename = secure_filename(business_document.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                
                # Create folder if it doesn't exist
                upload_folder = 'static/uploads/vendor_documents'
                if not os.path.exists(upload_folder):
                    os.makedirs(upload_folder)
                
                filepath = os.path.join(upload_folder, unique_filename)
                business_document.save(filepath)
                document_filename = unique_filename
            else:
                flash('Invalid file type. Please upload PDF, JPG, or PNG files.', 'error')
                return render_template('vendorreg.html')
        # === END OF DOCUMENT UPLOAD SECTION ===
        
        # Existing password validation...
        if password != confirm_password:
            flash('Passwords do not match!', 'error')
            return render_template('vendorreg.html')
        
        import re
        if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', password):
            flash('Password must be at least 8 characters with uppercase, lowercase, number, and special character', 'error')
            return render_template('vendorreg.html')
        
        hashed_password = generate_password_hash(password)
        
        try:
            conn = sqlite3.connect('vendors.db')
            c = conn.cursor()
            
            c.execute("SELECT id FROM vendors WHERE email = ?", (email,))
            if c.fetchone():
                flash('Email address already registered!', 'error')
                return render_template('vendorreg.html')
            
            # === UPDATE THIS INSERT STATEMENT ===
            try:
                # Try with document columns
                c.execute('''INSERT INTO vendors 
                             (business_name, contact_name, email, phone, service_type, 
                              password, description, business_document, document_verified)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                             (business_name, contact_name, email, phone, service_type, 
                              hashed_password, description, document_filename, 'pending'))
            except sqlite3.OperationalError as e:
                if "no such column" in str(e):
                    # Fallback to old columns
                    c.execute('''INSERT INTO vendors 
                                 (business_name, contact_name, email, phone, service_type, 
                                  password, description)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                 (business_name, contact_name, email, phone, service_type, 
                                  hashed_password, description))
                else:
                    raise e
            # === END OF UPDATE ===
            
            conn.commit()
            conn.close()
            
            flash('Your vendor application has been submitted successfully! Our team will review it shortly.', 'success')
            return redirect(url_for('vendor_login'))
            
        except sqlite3.Error as e:
            flash(f'An error occurred: {str(e)}', 'error')
            return render_template('vendorreg.html')
    
    return render_template('vendorreg.html')
def add_missing_columns():
    """Add missing columns to existing tables"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check and add columns to vendors table
        cursor.execute("PRAGMA table_info(vendors)")
        columns = [column[1] for column in cursor.fetchall()]
        
        print("🔍 Current vendor columns:", columns)
        
        # Add business_document if missing
        if 'business_document' not in columns:
            cursor.execute("ALTER TABLE vendors ADD COLUMN business_document TEXT")
            print("✅ Added business_document to vendors")
        
        # Add document_verified if missing
        if 'document_verified' not in columns:
            cursor.execute("ALTER TABLE vendors ADD COLUMN document_verified TEXT DEFAULT 'pending'")
            print("✅ Added document_verified to vendors")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error adding columns: {str(e)}")
@app.route('/uploads/vendor_documents/<filename>')
def serve_vendor_document(filename):
    """Serve vendor uploaded documents"""
    try:
        return send_from_directory('static/uploads/vendor_documents', filename)
    except:
        return "Document not found", 404
@app.route('/api/user/booking/<int:booking_id>')
def get_user_booking_detail(booking_id):
    """Get booking details for review"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT b.*, e.id as equipment_id, e.name as equipment_name, e.vendor_email
            FROM bookings b
            JOIN equipment e ON b.equipment_id = e.id
            WHERE b.id = ? AND b.user_id = ?
        """, (booking_id, session['user_id']))
        
        booking = cursor.fetchone()
        conn.close()
        
        if not booking:
            return jsonify({'error': 'Booking not found'}), 404
        
        booking_data = {
            'id': booking['id'],
            'equipment_id': booking['equipment_id'],
            'equipment_name': booking['equipment_name'],
            'vendor_email': booking['vendor_email'],
            'status': booking['status'],
            'created_date': booking['created_date']
        }
        
        return jsonify(booking_data)
        
    except Exception as e:
        print(f"Error fetching booking details: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/complete-expired-rentals')
def complete_expired_rentals():
    """Manually trigger completion check for testing"""
    if 'vendor_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        check_and_complete_expired_rentals()
        return jsonify({'success': True, 'message': 'Expired rentals completion check completed'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def check_and_complete_expired_rentals():
    """Check for expired rentals and mark them as completed + restock equipment"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        today = datetime.now().date()
        
        print(f"🔄 Checking for expired rentals: Today={today}")
        
        # Find approved rent requests that ended yesterday or earlier
        cursor.execute("""
            SELECT rr.id, rr.equipment_id, rr.equipment_name, rr.user_name, rr.user_phone
            FROM rent_requests rr
            WHERE rr.status = 'approved' 
            AND rr.end_date < ?
        """, (today.strftime('%Y-%m-%d'),))
        
        expired_rentals = cursor.fetchall()
        
        print(f"📦 Found {len(expired_rentals)} expired rentals to complete")
        
        for rental in expired_rentals:
            request_id, equipment_id, equipment_name, user_name, user_phone = rental
            
            print(f"✅ Completing rental #{request_id} for {equipment_name}")
            
            # Mark rent request as completed
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'completed', processed_date = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (request_id,))
            
            # ✅ RESTOCK THE EQUIPMENT (increase stock by 1)
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 > 0 THEN 'available' 
                        ELSE status 
                    END
                WHERE id = ?
            """, (equipment_id,))
            
            # Send completion notification to user
            completion_message = f"Your rental period for {equipment_name} has been completed. Thank you for using Lend A Hand!"
            send_sms(user_phone, completion_message)
            
            print(f"✅ Rental #{request_id} completed and equipment restocked")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error in automatic completion system: {str(e)}")

@app.route('/api/user/rent-requests')
def get_user_rent_requests():
    """Get rent requests for the logged-in user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT rr.*, v.business_name as vendor_name
            FROM rent_requests rr
            JOIN vendors v ON rr.vendor_email = v.email
            WHERE rr.user_id = ?
            ORDER BY rr.submitted_date DESC
        """, (session['user_id'],))
        
        requests = cursor.fetchall()
        conn.close()
        
        requests_list = []
        for row in requests:
            requests_list.append({
                'id': row['id'],
                'equipment_name': row['equipment_name'],
                'vendor_name': row['vendor_name'],
                'start_date': row['start_date'],
                'end_date': row['end_date'],
                'total_amount': row['total_amount'],
                'status': row['status'],
                'submitted_date': row['submitted_date']
            })
        
        return jsonify(requests_list)
        
    except Exception as e:
        print(f"Error fetching user rent requests: {str(e)}")
        return jsonify({'error': str(e)}), 500
# Add this to your init_db() function or run as a separate migration
def add_cancellation_columns():
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Add cancellation_requested_date to bookings
        cursor.execute("PRAGMA table_info(bookings)")
        booking_columns = [column[1] for column in cursor.fetchall()]
        
        if 'cancellation_requested_date' not in booking_columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN cancellation_requested_date TIMESTAMP")
        
        # Add cancellation_requested_date to rent_requests
        cursor.execute("PRAGMA table_info(rent_requests)")
        rent_columns = [column[1] for column in cursor.fetchall()]
        
        if 'cancellation_requested_date' not in rent_columns:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN cancellation_requested_date TIMESTAMP")
        
        conn.commit()
        conn.close()
        print("✅ Cancellation columns added successfully")
        
    except Exception as e:
        print(f"❌ Error adding cancellation columns: {str(e)}")

# Call this function after init_db()
add_cancellation_columns()
@app.route("/farmerlogin", methods=["GET", "POST"])
def farmer_login():
    if request.method == "POST":
        email = request.form.get('email')
        password = request.form.get('password')

        conn = sqlite3.connect('agriculture.db')
        c = conn.cursor()
        c.execute("SELECT id, full_name, email, phone, password, status FROM farmers WHERE email = ?", (email,))
        farmer = c.fetchone()
        conn.close()

        if farmer:
            if check_password_hash(farmer[4], password):
                if farmer[5] == 'approved':
                    session['user_id'] = farmer[0]
                    session['user_name'] = farmer[1]
                    session['user_email'] = farmer[2]
                    session['user_phone'] = farmer[3]  # Store phone for rent requests
                    session['user_type'] = 'farmer'
                    session.permanent = True
                    
                    flash('Login successful!', 'success')
                    return redirect(url_for("userdashboard"))
                else:
                    flash('Your account is pending approval by administrator', 'error')
            else:
                flash('Invalid email or password', 'error')
        else:
            flash('Invalid email or password', 'error')

    return render_template("farmer_login.html")

@app.route("/vendor_login", methods=["GET", "POST"])
def vendor_login():
    if request.method == "POST":
        email = request.form.get('email')
        password = request.form.get('password')

        conn = sqlite3.connect('vendors.db')
        c = conn.cursor()
        c.execute("SELECT id, contact_name, email, password, status, business_name FROM vendors WHERE email = ?", (email,))
        vendor = c.fetchone()
        conn.close()

        if vendor and check_password_hash(vendor[3], password):
            if vendor[4] == 'approved':
                # Set session variables
                session['vendor_id'] = vendor[0]
                session['contact_name'] = vendor[1]
                session['vendor_email'] = vendor[2]
                session['business_name'] = vendor[5]
                session['user_type'] = 'vendor'
                
                print(f"✅ Vendor logged in: {vendor[1]}")  # Debug
                
                flash('Login successful!', 'success')
                # IMPORTANT: Redirect to vendordashboard route, not index.html
                return redirect(url_for("vendordashboard"))
            else:
                flash('Your vendor account is pending approval', 'error')
        else:
            flash('Invalid email or password', 'error')

    return render_template("vendor_login.html")

@app.route("/userdashboard")
def userdashboard():
    if 'user_id' not in session or session.get('user_type') != 'farmer':
        flash('Please log in first', 'error')
        return redirect(url_for('farmer_login'))
    
    return render_template("userdashboard.html", 
                         user_name=session.get('user_name', 'User'),
                         user_id=session.get('user_id'))

    
 
@app.route("/vendordashboard")
def vendordashboard():
    if 'vendor_id' not in session or session.get('user_type') != 'vendor':
        flash('Please log in first', 'error')
        return redirect(url_for('vendor_login'))

    try:
        # Get vendor data from session
        contact_name = session.get('contact_name')
        vendor_email = session.get('vendor_email')
        business_name = session.get('business_name')
        
        # If session data is missing, get from database
        if not contact_name:
            conn = sqlite3.connect('vendors.db')
            c = conn.cursor()
            c.execute("SELECT contact_name, email, business_name FROM vendors WHERE id = ?", (session['vendor_id'],))
            vendor = c.fetchone()
            conn.close()
            
            if vendor:
                contact_name, vendor_email, business_name = vendor
                # Update session
                session['contact_name'] = contact_name
                session['vendor_email'] = vendor_email
                session['business_name'] = business_name
        
        print(f"✅ Rendering dashboard for: {contact_name}")  # Debug
        
        # Render index.html with vendor data
        return render_template("index.html", 
                             contact_name=contact_name, 
                             vendor_email=vendor_email,
                             business_name=business_name)
            
    except Exception as e:
        print(f"❌ Error in vendordashboard: {str(e)}")
        flash('Error loading dashboard', 'error')
        return redirect(url_for('vendor_login'))



@app.route('/debug_session')
def debug_session():
    return f"""
    <h3>Session Debug Info</h3>
    <p>Vendor ID: {session.get('vendor_id')}</p>
    <p>Contact Name: {session.get('contact_name')}</p>
    <p>Vendor Email: {session.get('vendor_email')}</p>
    <p>User Type: {session.get('user_type')}</p>
    <hr>
    <p><a href="/vendordashboard">Go to Dashboard</a></p>
    """

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))

# Admin credentials
ADMIN_EMAIL = "admin@lendahand.com"
ADMIN_PASSWORD = "admin123"

@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            session['admin_id'] = 1
            session['admin_name'] = 'Administrator'
            session['admin_email'] = ADMIN_EMAIL
            session['user_type'] = 'admin'
            flash('Login successful!', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid email or password', 'error')
            
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        flash('Please log in as administrator first', 'error')
        return redirect(url_for('admin_login'))
    
    return render_template("admin_dashboard.html", admin_name=session.get('admin_name', 'Admin'))
# Add these routes to your Flask app
# Add this route to handle equipment returns
@app.route('/api/vendor/rent-request/<int:request_id>/return', methods=['POST'])
def mark_equipment_returned(request_id):
    """Mark equipment as returned by farmer and await vendor approval"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Update rent request status to 'returned' (awaiting vendor approval)
        cursor.execute("""
            UPDATE rent_requests 
            SET status = 'returned', processed_date = CURRENT_TIMESTAMP 
            WHERE id = ? AND vendor_email = ?
        """, (request_id, session['vendor_email']))
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': 'Rent request not found or access denied'}), 404
        
        # Get request details for notification
        cursor.execute("""
            SELECT user_phone, user_name, equipment_name 
            FROM rent_requests WHERE id = ?
        """, (request_id,))
        
        request_data = cursor.fetchone()
        conn.commit()
        conn.close()
        
        # Send notification to farmer
        if request_data:
            user_phone, user_name, equipment_name = request_data
            message = f"Dear {user_name}, your return request for {equipment_name} has been submitted. Waiting for vendor approval."
            send_sms(user_phone, message)
        
        return jsonify({
            'success': True, 
            'message': 'Equipment return submitted for vendor approval'
        })
        
    except Exception as e:
        print(f"Error marking equipment returned: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/vendor/reviews')
def get_vendor_reviews():
    """Get all reviews for the logged-in vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get reviews for this vendor
        cursor.execute("""
            SELECT 
                r.*,
                e.image_url as equipment_image,
                e.category as equipment_category
            FROM reviews r
            LEFT JOIN equipment e ON r.equipment_id = e.id
            WHERE r.vendor_email = ?
            ORDER BY r.created_date DESC
        """, (vendor_email,))
        
        reviews = cursor.fetchall()
        conn.close()
        
        reviews_list = []
        for review in reviews:
            reviews_list.append({
                'id': review['id'],
                'user_name': review['user_name'],
                'equipment_name': review['equipment_name'],
                'equipment_category': review['equipment_category'],
                'equipment_image': review['equipment_image'],
                'rating': review['rating'],
                'title': review['title'],
                'comment': review['comment'],
                'created_date': review['created_date'],
                'order_type': review['order_type']
            })
        
        print(f"📊 Found {len(reviews_list)} reviews for vendor {vendor_email}")
        return jsonify(reviews_list)
        
    except Exception as e:
        print(f"❌ Error fetching vendor reviews: {str(e)}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/vendor/rent-request/<int:request_id>/complete', methods=['POST'])
def complete_rent_request(request_id):
    """Vendor approves the return and marks request as completed"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Get equipment details before updating
        cursor.execute("""
            SELECT equipment_id, user_phone, user_name, equipment_name 
            FROM rent_requests WHERE id = ? AND vendor_email = ?
        """, (request_id, session['vendor_email']))
        
        request_data = cursor.fetchone()
        
        if not request_data:
            conn.close()
            return jsonify({'error': 'Rent request not found or access denied'}), 404
        
        equipment_id, user_phone, user_name, equipment_name = request_data
        
        # Update rent request status to 'completed'
        cursor.execute("""
            UPDATE rent_requests 
            SET status = 'completed', processed_date = CURRENT_TIMESTAMP 
            WHERE id = ? AND vendor_email = ?
        """, (request_id, session['vendor_email']))
        
        # ✅ RESTOCK THE EQUIPMENT (increase stock by 1)
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = stock_quantity + 1,
                status = CASE 
                    WHEN stock_quantity + 1 > 0 THEN 'available' 
                    ELSE status 
                END
            WHERE id = ?
        """, (equipment_id,))
        
        conn.commit()
        conn.close()
        
        # Send completion notification to farmer
        completion_message = f"Thank you {user_name}! Your equipment {equipment_name} has been successfully returned and approved. We appreciate your business! - Lend A Hand"
        send_sms(user_phone, completion_message)
        
        return jsonify({
            'success': True, 
            'message': 'Rent request completed successfully'
        })
        
    except Exception as e:
        print(f"Error completing rent request: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/admin/equipment')
def api_admin_equipment():
    """Get all equipment for admin dashboard"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all equipment with vendor details
        cursor.execute("""
            SELECT e.*, v.business_name, v.contact_name, v.phone as vendor_phone
            FROM equipment e
            JOIN vendors v ON e.vendor_email = v.email
            ORDER BY e.created_date DESC
        """)
        
        equipment = cursor.fetchall()
        conn.close()
        
        equipment_list = []
        for item in equipment:
            equipment_list.append({
                'id': item['id'],
                'name': item['name'],
                'category': item['category'],
                'description': item['description'],
                'price': item['price'],
                'price_unit': item['price_unit'],
                'location': item['location'],
                'image_url': item['image_url'],
                'status': item['status'],
                'stock_quantity': item['stock_quantity'],
                'min_stock_threshold': item['min_stock_threshold'],
                'vendor_name': item['business_name'],
                'vendor_contact': item['contact_name'],
                'vendor_phone': item['vendor_phone'],
                'created_date': item['created_date']
            })
        
        return jsonify(equipment_list)
        
    except Exception as e:
        print(f"Error fetching equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/admin/bookings')
def api_admin_bookings():
    """Get all bookings for admin dashboard - FIXED DATABASE ISSUE"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        status_filter = request.args.get('status', 'all')
        search_term = request.args.get('search', '')
        
        # Connect to vendors.db for bookings and equipment data
        conn_vendors = sqlite3.connect('vendors.db')
        conn_vendors.row_factory = sqlite3.Row
        cursor_vendors = conn_vendors.cursor()
        
        # Connect to agriculture.db for farmers data
        conn_agri = sqlite3.connect('agriculture.db')
        conn_agri.row_factory = sqlite3.Row
        cursor_agri = conn_agri.cursor()
        
        # First get all bookings from vendors.db
        query = "SELECT * FROM bookings"
        params = []
        conditions = []
        
        if status_filter != 'all':
            conditions.append("status = ?")
            params.append(status_filter)
        
        if search_term:
            conditions.append("(user_name LIKE ? OR equipment_name LIKE ? OR vendor_name LIKE ?)")
            search_pattern = f"%{search_term}%"
            params.extend([search_pattern, search_pattern, search_pattern])
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY created_date DESC"
        
        cursor_vendors.execute(query, params)
        bookings = cursor_vendors.fetchall()
        
        bookings_list = []
        for booking in bookings:
            # Get farmer details from agriculture.db
            farmer_name = booking['user_name']
            farmer_phone = booking['user_phone']
            farmer_location = "Unknown"
            
            # Try to get additional farmer details from agriculture.db
            cursor_agri.execute("SELECT farm_location FROM farmers WHERE id = ?", (booking['user_id'],))
            farmer_data = cursor_agri.fetchone()
            if farmer_data:
                farmer_location = farmer_data['farm_location']
            
            # Get vendor details from vendors.db
            vendor_name = booking['vendor_name']
            vendor_phone = "Unknown"
            
            cursor_vendors.execute("SELECT phone FROM vendors WHERE email = ?", (booking['vendor_email'],))
            vendor_data = cursor_vendors.fetchone()
            if vendor_data:
                vendor_phone = vendor_data['phone']
            
            # Get equipment details
            equipment_name = booking['equipment_name']
            equipment_price = 0
            
            cursor_vendors.execute("SELECT price FROM equipment WHERE id = ?", (booking['equipment_id'],))
            equipment_data = cursor_vendors.fetchone()
            if equipment_data:
                equipment_price = equipment_data['price']
            
            bookings_list.append({
                'id': booking['id'],
                'farmer_name': farmer_name,
                'farmer_phone': farmer_phone,
                'farmer_location': farmer_location,
                'vendor_name': vendor_name,
                'vendor_phone': vendor_phone,
                'equipment_name': equipment_name,
                'equipment_price': equipment_price,
                'start_date': booking['start_date'],
                'end_date': booking['end_date'],
                'total_days': booking['duration'],
                'total_amount': booking['total_amount'],
                'status': booking['status'],
                'notes': booking['notes'],
                'booking_date': booking['created_date']
            })
        
        conn_vendors.close()
        conn_agri.close()
        
        return jsonify(bookings_list)
        
    except Exception as e:
        print(f"Error fetching bookings: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/admin/booking/<int:booking_id>')
def api_admin_booking_detail(booking_id):
    """Get detailed booking information - FIXED DATABASE ISSUE"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        # Connect to vendors.db for bookings data
        conn_vendors = sqlite3.connect('vendors.db')
        conn_vendors.row_factory = sqlite3.Row
        cursor_vendors = conn_vendors.cursor()
        
        # Connect to agriculture.db for farmers data
        conn_agri = sqlite3.connect('agriculture.db')
        conn_agri.row_factory = sqlite3.Row
        cursor_agri = conn_agri.cursor()
        
        # Get booking from vendors.db
        cursor_vendors.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        booking = cursor_vendors.fetchone()
        
        if not booking:
            conn_vendors.close()
            conn_agri.close()
            return jsonify({'error': 'Booking not found'}), 404
        
        # Get farmer details from agriculture.db
        farmer_name = booking['user_name']
        farmer_phone = booking['user_phone']
        farmer_email = "Unknown"
        farmer_location = "Unknown"
        
        cursor_agri.execute("SELECT email, farm_location FROM farmers WHERE id = ?", (booking['user_id'],))
        farmer_data = cursor_agri.fetchone()
        if farmer_data:
            farmer_email = farmer_data['email']
            farmer_location = farmer_data['farm_location']
        
        # Get vendor details from vendors.db
        vendor_name = booking['vendor_name']
        vendor_contact = "Unknown"
        vendor_phone = "Unknown"
        vendor_email = booking['vendor_email']
        
        cursor_vendors.execute("SELECT contact_name, phone FROM vendors WHERE email = ?", (vendor_email,))
        vendor_data = cursor_vendors.fetchone()
        if vendor_data:
            vendor_contact = vendor_data['contact_name']
            vendor_phone = vendor_data['phone']
        
        # Get equipment details from vendors.db
        equipment_name = booking['equipment_name']
        equipment_category = "Unknown"
        equipment_price = 0
        
        cursor_vendors.execute("SELECT category, price FROM equipment WHERE id = ?", (booking['equipment_id'],))
        equipment_data = cursor_vendors.fetchone()
        if equipment_data:
            equipment_category = equipment_data['category']
            equipment_price = equipment_data['price']
        
        booking_data = {
            'id': booking['id'],
            'farmer_name': farmer_name,
            'farmer_phone': farmer_phone,
            'farmer_email': farmer_email,
            'farmer_location': farmer_location,
            'vendor_name': vendor_name,
            'vendor_contact': vendor_contact,
            'vendor_phone': vendor_phone,
            'vendor_email': vendor_email,
            'equipment_name': equipment_name,
            'equipment_category': equipment_category,
            'equipment_price': equipment_price,
            'start_date': booking['start_date'],
            'end_date': booking['end_date'],
            'total_days': booking['duration'],
            'total_amount': booking['total_amount'],
            'status': booking['status'],
            'notes': booking['notes'],
            'booking_date': booking['created_date'],
            'payment_status': 'paid'  # You can add payment tracking later
        }
        
        conn_vendors.close()
        conn_agri.close()
        
        return jsonify(booking_data)
        
    except Exception as e:
        print(f"Error fetching booking details: {str(e)}")
        return jsonify({'error': str(e)}), 500
# ================= REVIEW SYSTEM ==================
@app.route('/api/user/completed-orders')
def get_user_completed_orders():
    """Get completed bookings AND rent requests for review writing - FIXED VERSION"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        user_id = session['user_id']
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        print(f"🔍 Fetching completed orders for user {user_id}")
        
        # Get completed bookings that haven't been reviewed yet
        cursor.execute("""
            SELECT 
                b.id as order_id,
                'booking' as order_type,
                b.equipment_id,
                b.equipment_name,
                b.vendor_email,
                b.vendor_name,
                b.created_date,
                b.total_amount,
                e.image_url as equipment_image
            FROM bookings b
            LEFT JOIN equipment e ON b.equipment_id = e.id
            WHERE b.user_id = ? 
            AND b.status IN ('completed', 'confirmed')  -- Allow both completed and confirmed
            AND NOT EXISTS (
                SELECT 1 FROM reviews r 
                WHERE r.order_id = b.id 
                AND r.order_type = 'booking' 
                AND r.user_id = ?
            )
            
            UNION ALL
            
            SELECT 
                rr.id as order_id,
                'rent' as order_type,
                rr.equipment_id,
                rr.equipment_name,
                rr.vendor_email,
                v.contact_name as vendor_name,
                rr.submitted_date as created_date,
                rr.total_amount,
                e.image_url as equipment_image
            FROM rent_requests rr
            LEFT JOIN equipment e ON rr.equipment_id = e.id
            LEFT JOIN vendors v ON rr.vendor_email = v.email
            WHERE rr.user_id = ? 
            AND rr.status IN ('completed', 'approved')  -- Allow both completed and approved
            AND NOT EXISTS (
                SELECT 1 FROM reviews r 
                WHERE r.order_id = rr.id 
                AND r.order_type = 'rent' 
                AND r.user_id = ?
            )
            
            ORDER BY created_date DESC
        """, (user_id, user_id, user_id, user_id))
        
        orders = cursor.fetchall()
        conn.close()
        
        orders_list = []
        for order in orders:
            orders_list.append({
                'order_id': order['order_id'],
                'order_type': order['order_type'],
                'equipment_id': order['equipment_id'],
                'equipment_name': order['equipment_name'],
                'equipment_image': order['equipment_image'],
                'vendor_email': order['vendor_email'],
                'vendor_name': order['vendor_name'] or 'Vendor',
                'created_date': order['created_date'],
                'total_amount': order['total_amount']
            })
        
        print(f"✅ Found {len(orders_list)} completed orders: {len([o for o in orders_list if o['order_type'] == 'booking'])} bookings, {len([o for o in orders_list if o['order_type'] == 'rent'])} rentals")
        
        return jsonify(orders_list)
        
    except Exception as e:
        print(f"❌ Error fetching completed orders: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
@app.route('/add-avg-rating-column')
def add_avg_rating_column():
    """Add avg_rating column to equipment table"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check if column already exists
        cursor.execute("PRAGMA table_info(equipment)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'avg_rating' not in columns:
            cursor.execute("ALTER TABLE equipment ADD COLUMN avg_rating REAL DEFAULT 0")
            print("✅ Added avg_rating column to equipment table")
        else:
            print("⚠️ avg_rating column already exists")
        
        conn.commit()
        conn.close()
        return "✅ avg_rating column added/verified successfully"
        
    except Exception as e:
        return f"❌ Error: {str(e)}"
@app.route('/api/user/reviews')
def get_user_reviews():
    """Get reviews written by the user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        user_id = session['user_id']
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT r.*, e.image_url as equipment_image
            FROM reviews r
            LEFT JOIN equipment e ON r.equipment_id = e.id
            WHERE r.user_id = ?
            ORDER BY r.created_date DESC
        """, (user_id,))
        
        reviews = cursor.fetchall()
        conn.close()
        
        reviews_list = []
        for review in reviews:
            reviews_list.append({
                'id': review['id'],
                'equipment_name': review['equipment_name'],
                'vendor_name': review['vendor_name'],
                'order_type': review['order_type'],
                'rating': review['rating'],
                'title': review['title'],
                'comment': review['comment'],
                'created_date': review['created_date'],
                'equipment_image': review['equipment_image']
            })
        
        return jsonify(reviews_list)
        
    except Exception as e:
        print(f"Error fetching user reviews: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/reviews/submit', methods=['POST'])
def submit_review():
    """Submit a new review"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        
        required_fields = ['order_id', 'order_type', 'equipment_id', 'equipment_name', 
                          'vendor_email', 'vendor_name', 'rating', 'title', 'comment']
        
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({'error': f'Missing fields: {", ".join(missing_fields)}'}), 400
        
        user_id = session['user_id']
        user_name = session.get('user_name', 'User')
        
        # Check if review already exists for this order
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id FROM reviews 
            WHERE user_id = ? AND order_id = ? AND order_type = ?
        """, (user_id, data['order_id'], data['order_type']))
        
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'You have already reviewed this order'}), 400
        
        # Insert the review
        cursor.execute("""
            INSERT INTO reviews 
            (user_id, user_name, equipment_id, equipment_name, vendor_email, 
             vendor_name, order_type, order_id, rating, title, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            user_name,
            data['equipment_id'],
            data['equipment_name'],
            data['vendor_email'],
            data['vendor_name'],
            data['order_type'],
            data['order_id'],
            data['rating'],
            data['title'],
            data['comment']
        ))
        
        review_id = cursor.lastrowid
        
        try:
            # Try to update equipment average rating (if column exists)
            cursor.execute("""
                UPDATE equipment 
                SET avg_rating = (
                    SELECT COALESCE(AVG(rating), 0) FROM reviews 
                    WHERE equipment_id = ?
                )
                WHERE id = ?
            """, (data['equipment_id'], data['equipment_id']))
            print(f"✅ Updated avg_rating for equipment #{data['equipment_id']}")
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                print(f"⚠️ avg_rating column doesn't exist, skipping rating update")
                # Column will be added when you run /add-avg-rating-column
            else:
                raise e
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Review submitted successfully!',
            'review_id': review_id
        })
        
    except Exception as e:
        print(f"❌ Error submitting review: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
@app.route('/api/user/reviews/<int:review_id>/delete', methods=['POST'])
def delete_review(review_id):
    """Delete a review"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        user_id = session['user_id']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check if review belongs to user and get equipment_id
        cursor.execute("""
            SELECT equipment_id FROM reviews 
            WHERE id = ? AND user_id = ?
        """, (review_id, user_id))
        
        review = cursor.fetchone()
        
        if not review:
            conn.close()
            return jsonify({'error': 'Review not found or access denied'}), 404
        
        equipment_id = review[0]
        
        # Delete the review
        cursor.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        
        try:
            # Try to update equipment average rating (if column exists)
            cursor.execute("""
                UPDATE equipment 
                SET avg_rating = (
                    SELECT COALESCE(AVG(rating), 0) FROM reviews 
                    WHERE equipment_id = ?
                )
                WHERE id = ?
            """, (equipment_id, equipment_id))
        except sqlite3.OperationalError as e:
            if "no such column" in str(e):
                print(f"⚠️ avg_rating column doesn't exist, skipping rating update")
            else:
                raise e
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Review deleted successfully'
        })
        
    except Exception as e:
        print(f"❌ Error deleting review: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/equipment/<int:equipment_id>/reviews')
def get_equipment_reviews(equipment_id):
    """Get reviews for a specific equipment"""
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT r.*, u.full_name as user_name 
            FROM reviews r
            LEFT JOIN agriculture.farmers u ON r.user_id = u.id
            WHERE r.equipment_id = ? AND r.status = 'active'
            ORDER BY r.created_date DESC
        """, (equipment_id,))
        
        reviews = cursor.fetchall()
        conn.close()
        
        reviews_list = []
        for review in reviews:
            reviews_list.append({
                'id': review['id'],
                'user_name': review['user_name'],
                'rating': review['rating'],
                'title': review['title'],
                'comment': review['comment'],
                'created_date': review['created_date'],
                'order_type': review['order_type']
            })
        
        return jsonify(reviews_list)
        
    except Exception as e:
        print(f"Error fetching equipment reviews: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/user/completed-bookings')
def get_user_completed_bookings():
    """Get completed bookings for review writing"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT b.*, e.name as equipment_name, v.business_name as vendor_name
            FROM bookings b
            JOIN equipment e ON b.equipment_id = e.id
            JOIN vendors v ON b.vendor_email = v.email
            WHERE b.user_id = ? AND b.status = 'completed'
            AND NOT EXISTS (
                SELECT 1 FROM reviews r 
                WHERE r.booking_id = b.id AND r.user_id = ?
            )
            ORDER BY b.created_date DESC
        """, (session['user_id'], session['user_id']))
        
        bookings = cursor.fetchall()
        conn.close()
        
        bookings_list = []
        for booking in bookings:
            bookings_list.append({
                'id': booking['id'],
                'equipment_name': booking['equipment_name'],
                'vendor_name': booking['vendor_name'],
                'booking_date': booking['created_date'],
                'total_amount': booking['total_amount']
            })
        
        return jsonify(bookings_list)
        
    except Exception as e:
        print(f"Error fetching completed bookings: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/farmer/<int:farmer_id>')
def api_admin_farmer_detail(farmer_id):
    """Get detailed farmer information for admin dashboard"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('agriculture.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM farmers 
            WHERE id = ?
        """, (farmer_id,))
        
        farmer = cursor.fetchone()
        conn.close()
        
        if not farmer:
            return jsonify({'error': 'Farmer not found'}), 404
        
        # Format farmer details
        farmer_data = {
            'id': farmer['id'],
            'full_name': farmer['full_name'],
            'last_name': farmer['last_name'],
            'email': farmer['email'] or 'N/A',
            'phone': farmer['phone'],
            'farm_location': farmer['farm_location'],
            'farm_size': farmer['farm_size'] or 'N/A',
            'crop_types': farmer['crop_types'] or 'N/A',
            'additional_info': farmer['additional_info'] or 'N/A',
            'rtc_document': farmer['rtc_document'],
            'document_url': url_for('serve_equipment_image', filename=farmer['rtc_document']) if farmer['rtc_document'] else None,
            'registration_date': farmer['registration_date'],
            'status': farmer['status']
        }
        
        return jsonify(farmer_data)
        
    except Exception as e:
        print(f"Error fetching farmer details: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/vendor/<int:vendor_id>')
def api_admin_vendor_detail(vendor_id):
    """Get detailed vendor information for admin dashboard"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM vendors 
            WHERE id = ?
        """, (vendor_id,))
        
        vendor = cursor.fetchone()
        
        if not vendor:
            conn.close()
            return jsonify({'error': 'Vendor not found'}), 404
        
        # Get additional stats for this vendor
        cursor.execute("SELECT COUNT(*) FROM equipment WHERE vendor_email = ?", (vendor['email'],))
        equipment_count = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT COUNT(*) FROM bookings WHERE vendor_email = ?", (vendor['email'],))
        booking_count = cursor.fetchone()[0] or 0
        
        conn.close()
        
        # Format vendor details
        vendor_data = {
            'id': vendor['id'],
            'business_name': vendor['business_name'],
            'contact_name': vendor['contact_name'],
            'email': vendor['email'],
            'phone': vendor['phone'],
            'service_type': vendor['service_type'],
            'description': vendor['description'] or 'N/A',
            'business_document': vendor['business_document'],
            'document_verified': vendor['document_verified'] or 'pending',
            'document_url': url_for('serve_vendor_document', filename=vendor['business_document']) if vendor['business_document'] else None,
            'equipment_count': equipment_count,
            'booking_count': booking_count,
            'registration_date': vendor['registration_date'],
            'status': vendor['status']
        }
        
        return jsonify(vendor_data)
        
    except Exception as e:
        print(f"Error fetching vendor details: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/booking/delete/<int:booking_id>', methods=['POST'])
def api_admin_delete_booking(booking_id):
    """Delete a booking (admin only)"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Booking deleted successfully'})
        
    except Exception as e:
        print(f"Error deleting booking: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    session.pop('admin_name', None)
    session.pop('user_type', None)
    flash('Admin logged out successfully', 'success')
    return redirect(url_for('admin_login'))

# API endpoint to get farmers data
@app.route('/api/admin/farmers')
def api_admin_farmers():
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = sqlite3.connect('agriculture.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    status_filter = request.args.get('status', 'all')
    search_term = request.args.get('search', '')
    
    query = "SELECT * FROM farmers"
    params = []
    
    if status_filter != 'all' or search_term:
        query += " WHERE "
        conditions = []
        
        if status_filter != 'all':
            conditions.append("status = ?")
            params.append(status_filter)
            
        if search_term:
            conditions.append("(full_name LIKE ? OR last_name LIKE ? OR email LIKE ? OR phone LIKE ? OR farm_location LIKE ?)")
            search_pattern = f"%{search_term}%"
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern, search_pattern])
            
        query += " AND ".join(conditions)
    
    query += " ORDER BY registration_date DESC"
    
    c.execute(query, params)
    farmers = c.fetchall()
    
    farmers_list = []
    for farmer in farmers:
        farmers_list.append({
            'id': farmer['id'],
            'full_name': farmer['full_name'],
            'last_name': farmer['last_name'],
            'email': farmer['email'],
            'phone': farmer['phone'],
            'farm_location': farmer['farm_location'],
            'farm_size': farmer['farm_size'],
            'crop_types': farmer['crop_types'],
            'additional_info': farmer['additional_info'],
            'rtc_document': farmer['rtc_document'],
            'registration_date': farmer['registration_date'],
            'status': farmer['status']
        })
    
    conn.close()
    return jsonify(farmers_list)

@app.route('/api/admin/vendors')
def api_admin_vendors():
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = sqlite3.connect('vendors.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    status_filter = request.args.get('status', 'all')
    search_term = request.args.get('search', '')
    
    query = "SELECT * FROM vendors"
    params = []
    
    if status_filter != 'all' or search_term:
        query += " WHERE "
        conditions = []
        
        if status_filter != 'all':
            conditions.append("status = ?")
            params.append(status_filter)
            
        if search_term:
            conditions.append("(business_name LIKE ? OR contact_name LIKE ? OR email LIKE ? OR phone LIKE ? OR service_type LIKE ?)")
            search_pattern = f"%{search_term}%"
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern, search_pattern])
            
        query += " AND ".join(conditions)
    
    query += " ORDER BY registration_date DESC"
    
    c.execute(query, params)
    vendors = c.fetchall()
    
    vendors_list = []
    for vendor in vendors:
        vendor_data = {
            'id': vendor['id'],
            'business_name': vendor['business_name'],
            'contact_name': vendor['contact_name'],
            'email': vendor['email'],
            'phone': vendor['phone'],
            'service_type': vendor['service_type'],
            'description': vendor['description'],
            'business_document': vendor['business_document'],  # Document filename
            'document_verified': vendor['document_verified'],  # Verification status
            'document_url': url_for('serve_vendor_document', filename=vendor['business_document']) if vendor['business_document'] else None,  # Document URL
            'registration_date': vendor['registration_date'],
            'status': vendor['status']
        }
        
        # Get counts for this vendor
        c.execute("SELECT COUNT(*) FROM equipment WHERE vendor_email = ?", (vendor['email'],))
        equipment_count = c.fetchone()[0]
        vendor_data['equipment_count'] = equipment_count
        
        c.execute("SELECT COUNT(*) FROM bookings WHERE vendor_email = ?", (vendor['email'],))
        booking_count = c.fetchone()[0]
        vendor_data['booking_count'] = booking_count
        
        vendors_list.append(vendor_data)
    
    conn.close()
    return jsonify(vendors_list)
@app.route('/api/admin/vendor/document/verify', methods=['POST'])
def verify_vendor_document():
    """Update vendor document verification status - FIXED VERSION"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        print("📨 Received verification data:", data)
        
        vendor_id = data.get('vendor_id')
        status = data.get('status')  # 'verified', 'rejected', 'pending'
        
        if not vendor_id or not status:
            return jsonify({'error': 'Missing vendor_id or status'}), 400
        
        if status not in ['verified', 'rejected', 'pending']:
            return jsonify({'error': 'Invalid status. Use verified, rejected, or pending'}), 400
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # First, check if vendor exists
        cursor.execute("SELECT id, email, business_name FROM vendors WHERE id = ?", (vendor_id,))
        vendor = cursor.fetchone()
        
        if not vendor:
            conn.close()
            return jsonify({'error': 'Vendor not found'}), 404
        
        # Get vendor's phone for SMS notification
        cursor.execute("SELECT phone FROM vendors WHERE id = ?", (vendor_id,))
        vendor_phone_result = cursor.fetchone()
        vendor_phone = vendor_phone_result[0] if vendor_phone_result else None
        
        # Update document verification status
        cursor.execute("""
            UPDATE vendors 
            SET document_verified = ? 
            WHERE id = ?
        """, (status, vendor_id))
        
        affected_rows = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        print(f"✅ Updated vendor {vendor_id} document status to {status}. Affected rows: {affected_rows}")
        
        # Send SMS notification if phone exists
        if vendor_phone:
            if status == 'verified':
                sms_message = "🎉 Your business document has been verified! Your vendor account is now fully active."
            elif status == 'rejected':
                sms_message = "⚠️ Your business document verification was rejected. Please upload a valid document or contact support."
            elif status == 'pending':
                sms_message = "📄 Your document verification status has been reset to pending."
            
            sms_result = send_sms(vendor_phone, sms_message)
            print(f"📱 Sent {status} notification to vendor {vendor_phone}: {sms_result}")
        
        return jsonify({
            'success': True,
            'message': f'Document status updated to {status}',
            'vendor_id': vendor_id,
            'status': status,
            'affected_rows': affected_rows
        })
        
    except Exception as e:
        print(f"❌ Error verifying document: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
@app.route('/api/admin/stats')
def api_admin_stats():
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        stats = {}
        
        # Connect to agriculture database
        conn_agri = sqlite3.connect('agriculture.db')
        c_agri = conn_agri.cursor()
        
        # Get ALL farmers count (total registered) ✅ THIS IS CORRECT
        c_agri.execute("SELECT COUNT(*) FROM farmers")
        total_farmers_result = c_agri.fetchone()
        stats['total_farmers'] = total_farmers_result[0] if total_farmers_result else 0
        
        # Get PENDING farmers count
        c_agri.execute("SELECT COUNT(*) FROM farmers WHERE status = 'pending'")
        pending_farmers_result = c_agri.fetchone()
        stats['pending_farmers'] = pending_farmers_result[0] if pending_farmers_result else 0
        
        conn_agri.close()
        
        # ================ ADD VENDOR STATS ================
        conn_vendors = sqlite3.connect('vendors.db')
        c_vendors = conn_vendors.cursor()
        
        # Get ALL vendors count
        c_vendors.execute("SELECT COUNT(*) FROM vendors")
        total_vendors_result = c_vendors.fetchone()
        stats['total_vendors'] = total_vendors_result[0] if total_vendors_result else 0
        
        # Get PENDING vendors count
        c_vendors.execute("SELECT COUNT(*) FROM vendors WHERE status = 'pending'")
        pending_vendors_result = c_vendors.fetchone()
        stats['pending_vendors'] = pending_vendors_result[0] if pending_vendors_result else 0
        
        # Get EQUIPMENT count
        c_vendors.execute("SELECT COUNT(*) FROM equipment")
        total_equipment_result = c_vendors.fetchone()
        stats['total_equipment'] = total_equipment_result[0] if total_equipment_result else 0
        
        # Get BOOKINGS count
        c_vendors.execute("SELECT COUNT(*) FROM bookings")
        total_bookings_result = c_vendors.fetchone()
        stats['total_bookings'] = total_bookings_result[0] if total_bookings_result else 0
        
        conn_vendors.close()
        
        print(f"✅ Stats generated: Farmers={stats.get('total_farmers', 0)}, Vendors={stats.get('total_vendors', 0)}, Equipment={stats.get('total_equipment', 0)}, Bookings={stats.get('total_bookings', 0)}")
        
        return jsonify(stats)
        
    except Exception as e:
        print(f"❌ Error generating stats: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/user/stats')
def get_user_stats():
    """Get statistics for user dashboard"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        user_id = session['user_id']
        
        conn_vendors = sqlite3.connect('vendors.db')
        cursor_vendors = conn_vendors.cursor()
        
        # Get active bookings count (pending + confirmed)
        cursor_vendors.execute("""
            SELECT COUNT(*) FROM bookings 
            WHERE user_id = ? AND status IN ('pending', 'confirmed', 'active')
        """, (user_id,))
        active_bookings = cursor_vendors.fetchone()[0] or 0
        
        # Get past bookings count (completed + cancelled)
        cursor_vendors.execute("""
            SELECT COUNT(*) FROM bookings 
            WHERE user_id = ? AND status IN ('completed', 'cancelled')
        """, (user_id,))
        past_bookings = cursor_vendors.fetchone()[0] or 0
        
        # Get rent requests counts
        cursor_vendors.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status IN ('pending', 'approved') THEN 1 ELSE 0 END) as active_rents,
                SUM(CASE WHEN status IN ('completed', 'cancelled', 'rejected') THEN 1 ELSE 0 END) as past_rents
            FROM rent_requests 
            WHERE user_id = ?
        """, (user_id,))
        rent_counts = cursor_vendors.fetchone()
        
        active_rents = rent_counts[1] if rent_counts else 0
        past_rents = rent_counts[2] if rent_counts else 0
        
        # Get wishlist count (from localStorage is fine, but we can fetch from DB if stored)
        cursor_vendors.execute("""
            SELECT COUNT(*) FROM reviews 
            WHERE user_id = ?
        """, (user_id,))
        reviews_written = cursor_vendors.fetchone()[0] or 0
        
        conn_vendors.close()
        
        # For wishlist, we'll still use localStorage since it's client-side
        # But we can get wishlist count from database if you store it
        
        return jsonify({
            'active_bookings': active_bookings,
            'past_bookings': past_bookings,
            'active_rents': active_rents,
            'past_rents': past_rents,
            'reviews_written': reviews_written,
            'total_orders': active_bookings + past_bookings + active_rents + past_rents
        })
        
    except Exception as e:
        print(f"❌ Error fetching user stats: {str(e)}")
        return jsonify({'error': str(e)}), 500
# API endpoint to approve a farmer
@app.route('/api/admin/farmer/approve/<int:farmer_id>', methods=['POST'])
def api_approve_farmer(farmer_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('agriculture.db')
        c = conn.cursor()
        
        c.execute("UPDATE farmers SET status = 'approved' WHERE id = ?", (farmer_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== REAL REPORTS API ====================
@app.route('/api/admin/reports/real-data')
def api_admin_real_reports():
    """Get ONLY real data from database - NO DUMMY VALUES"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        print("📊 Loading REAL reports data from database...")
        
        # Connect to databases
        conn_vendors = sqlite3.connect('vendors.db')
        conn_agri = sqlite3.connect('agriculture.db')
        
        cursor_vendors = conn_vendors.cursor()
        cursor_agri = conn_agri.cursor()
        
        # ==================== BASIC COUNTS FROM DATABASE ====================
        
        # Total approved farmers (REAL)
        cursor_agri.execute("SELECT COUNT(*) FROM farmers WHERE status = 'approved'")
        total_farmers = cursor_agri.fetchone()[0] or 0
        
        # Total approved vendors (REAL)
        cursor_vendors.execute("SELECT COUNT(*) FROM vendors WHERE status = 'approved'")
        total_vendors = cursor_vendors.fetchone()[0] or 0
        
        # Total equipment (REAL)
        cursor_vendors.execute("SELECT COUNT(*) FROM equipment")
        total_equipment = cursor_vendors.fetchone()[0] or 0
        
        # Available equipment (REAL)
        cursor_vendors.execute("SELECT COUNT(*) FROM equipment WHERE status = 'available'")
        available_equipment = cursor_vendors.fetchone()[0] or 0
        
        # Total bookings (REAL)
        cursor_vendors.execute("SELECT COUNT(*) FROM bookings")
        total_bookings = cursor_vendors.fetchone()[0] or 0
        
        # Total rent requests (REAL)
        cursor_vendors.execute("SELECT COUNT(*) FROM rent_requests")
        total_rents = cursor_vendors.fetchone()[0] or 0
        
        # ==================== REGISTRATION TIMELINE (REAL) ====================
        registration_data = []
        for i in range(5, -1, -1):  # Last 6 months
            date = datetime.now().date() - timedelta(days=30 * i)
            start_date = date.replace(day=1)
            end_date = (date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            
            # Farmers this month
            cursor_agri.execute("""
                SELECT COUNT(*) FROM farmers 
                WHERE DATE(registration_date) BETWEEN ? AND ?
            """, (start_date, end_date))
            farmers_count = cursor_agri.fetchone()[0] or 0
            
            # Vendors this month
            cursor_vendors.execute("""
                SELECT COUNT(*) FROM vendors 
                WHERE DATE(registration_date) BETWEEN ? AND ?
            """, (start_date, end_date))
            vendors_count = cursor_vendors.fetchone()[0] or 0
            
            registration_data.append({
                'date': start_date.strftime('%b %Y'),
                'farmers': farmers_count,
                'vendors': vendors_count,
                'total': farmers_count + vendors_count
            })
        
        # ==================== EQUIPMENT CATEGORIES (REAL) ====================
        cursor_vendors.execute("""
            SELECT category, COUNT(*) as count 
            FROM equipment 
            GROUP BY category 
            ORDER BY count DESC
        """)
        categories_result = cursor_vendors.fetchall()
        
        category_distribution = []
        for category, count in categories_result:
            if category:  # Only include if category is not null/empty
                category_distribution.append({
                    'category': category,
                    'count': count
                })
        
        # ==================== BOOKING STATUS (REAL) ====================
        cursor_vendors.execute("""
            SELECT status, COUNT(*) as count 
            FROM bookings 
            GROUP BY status
        """)
        booking_statuses = cursor_vendors.fetchall()
        
        booking_status_distribution = []
        for status, count in booking_statuses:
            if status:  # Only include if status is not null
                booking_status_distribution.append({
                    'status': status.replace('_', ' ').title(),
                    'count': count
                })
        
        # ==================== RENT REQUEST STATUS (REAL) ====================
        cursor_vendors.execute("""
            SELECT status, COUNT(*) as count 
            FROM rent_requests 
            GROUP BY status
        """)
        rent_statuses = cursor_vendors.fetchall()
        
        rent_status_distribution = []
        for status, count in rent_statuses:
            if status:  # Only include if status is not null
                rent_status_distribution.append({
                    'status': status.replace('_', ' ').title(),
                    'count': count
                })
        
        # ==================== RECENT ACTIVITIES (REAL) ====================
        
        # Recent bookings (last 10)
        cursor_vendors.execute("""
            SELECT id, equipment_name, user_name, total_amount, status, created_date
            FROM bookings 
            ORDER BY created_date DESC 
            LIMIT 10
        """)
        recent_bookings = []
        for row in cursor_vendors.fetchall():
            recent_bookings.append({
                'id': row[0],
                'equipment_name': row[1],
                'user_name': row[2],
                'total_amount': float(row[3]) if row[3] else 0,
                'status': row[4],
                'date': row[5]
            })
        
        # Recent rent requests (last 10)
        cursor_vendors.execute("""
            SELECT id, equipment_name, user_name, total_amount, status, submitted_date
            FROM rent_requests 
            ORDER BY submitted_date DESC 
            LIMIT 10
        """)
        recent_rents = []
        for row in cursor_vendors.fetchall():
            recent_rents.append({
                'id': row[0],
                'equipment_name': row[1],
                'user_name': row[2],
                'total_amount': float(row[3]) if row[3] else 0,
                'status': row[4],
                'date': row[5]
            })
        
        # Recent registrations (last 10)
        cursor_agri.execute("""
            SELECT id, full_name, last_name, 'farmer' as type, registration_date
            FROM farmers 
            ORDER BY registration_date DESC 
            LIMIT 5
        """)
        recent_farmers = cursor_agri.fetchall()
        
        cursor_vendors.execute("""
            SELECT id, business_name, contact_name, 'vendor' as type, registration_date
            FROM vendors 
            ORDER BY registration_date DESC 
            LIMIT 5
        """)
        recent_vendors = cursor_vendors.fetchall()
        
        recent_registrations = []
        for farmer in recent_farmers:
            recent_registrations.append({
                'id': farmer[0],
                'name': f"{farmer[1]} {farmer[2]}",
                'type': farmer[3],
                'date': farmer[4]
            })
        
        for vendor in recent_vendors:
            recent_registrations.append({
                'id': vendor[0],
                'name': f"{vendor[1]} ({vendor[2]})",
                'type': vendor[3],
                'date': vendor[4]
            })
        
        # Sort by date
        recent_registrations.sort(key=lambda x: x['date'], reverse=True)
        
        conn_vendors.close()
        conn_agri.close()
        
        # ==================== PREPARE RESPONSE ====================
        reports_data = {
            'summary': {
                'totalFarmers': total_farmers,
                'totalVendors': total_vendors,
                'totalEquipment': total_equipment,
                'availableEquipment': available_equipment,
                'totalBookings': total_bookings,
                'totalRentRequests': total_rents
            },
            'registrationTimeline': registration_data,
            'equipmentCategories': category_distribution,
            'bookingStatus': booking_status_distribution,
            'rentStatus': rent_status_distribution,
            'recentActivities': {
                'bookings': recent_bookings,
                'rentRequests': recent_rents,
                'registrations': recent_registrations[:10]  # Top 10
            }
        }
        
        print(f"✅ REAL Reports data loaded: {total_farmers} farmers, {total_vendors} vendors, {total_equipment} equipment")
        
        return jsonify(reports_data)
        
    except Exception as e:
        print(f"❌ Error in REAL reports API: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'message': 'Failed to load real reports data'}), 500
# API endpoint to reject a farmer
@app.route('/api/admin/farmer/reject/<int:farmer_id>', methods=['POST'])
def api_reject_farmer(farmer_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('agriculture.db')
        c = conn.cursor()
        
        c.execute("SELECT phone FROM farmers WHERE id = ?", (farmer_id,))
        farmer_phone = c.fetchone()
        
        if farmer_phone:
            sms_message = "Your farmer registration has been rejected. Please contact support for more information."
            send_sms(farmer_phone[0], sms_message)
        
        c.execute("UPDATE farmers SET status = 'rejected' WHERE id = ?", (farmer_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# API endpoint to approve a vendor
@app.route('/api/admin/vendor/approve/<int:vendor_id>', methods=['POST'])
def api_approve_vendor(vendor_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        c = conn.cursor()
        
        c.execute("SELECT phone FROM vendors WHERE id = ?", (vendor_id,))
        vendor_phone = c.fetchone()
        
        if vendor_phone:
            sms_message = "Your vendor registration has been approved! You can now access all features of the platform."
            send_sms(vendor_phone[0], sms_message)
        
        c.execute("UPDATE vendors SET status = 'approved' WHERE id = ?", (vendor_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# API endpoint to reject a vendor
@app.route('/api/admin/vendor/reject/<int:vendor_id>', methods=['POST'])
def api_reject_vendor(vendor_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        c = conn.cursor()
        
        c.execute("SELECT phone FROM vendors WHERE id = ?", (vendor_id,))
        vendor_phone = c.fetchone()
        
        if vendor_phone:
            sms_message = "Your vendor registration has been rejected. Please contact support for more information."
            send_sms(vendor_phone[0], sms_message)
        
        c.execute("UPDATE vendors SET status = 'rejected' WHERE id = ?", (vendor_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/translate")
def translate():
    response = requests.get("https://example.com")
    return response.text
@app.route('/api/bookings/submit', methods=['POST'])
def submit_booking():
    """Submit a new booking using purchase price"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        print("📩 Received booking data:", data)
        
        required_fields = ['equipment_id', 'total_amount']
        missing_fields = [field for field in required_fields if field not in data]
        
        if missing_fields:
            return jsonify({
                'success': False, 
                'error': f'Missing required fields: {", ".join(missing_fields)}'
            }), 400
        
        # Get equipment details WITH VENDOR INFO
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                e.id, e.vendor_email, e.name, e.category, e.description,
                e.purchase_price, e.purchase_unit, e.rental_price, e.rental_price_unit, e.equipment_type,
                e.location, e.status, e.stock_quantity, e.min_stock_threshold, e.image_url,
                v.contact_name, v.business_name
            FROM equipment e
            JOIN vendors v ON e.vendor_email = v.email
            WHERE e.id = ?
        """, (data['equipment_id'],))
        
        equipment = cursor.fetchone()
        if not equipment:
            conn.close()
            return jsonify({'error': 'Equipment not found'}), 404
        
        # Extract data with proper indices
        (
            equipment_id, vendor_email, equipment_name, category, description,
            purchase_price, purchase_unit, rental_price, rental_price_unit, equipment_type,
            location, status, stock_quantity, min_stock_threshold, image_url,
            vendor_name, business_name
        ) = equipment
        
        # Check if equipment is available for purchase
        if equipment_type == 'rental_only':
            conn.close()
            return jsonify({'error': 'This equipment is only available for rental, not purchase'}), 400
        
        # ✅ FIX: Convert stock_quantity to integer
        try:
            if stock_quantity is None:
                stock_quantity_int = 1
            elif isinstance(stock_quantity, str):
                stock_quantity_int = int(stock_quantity) if stock_quantity.strip() != '' else 1
            else:
                stock_quantity_int = int(stock_quantity)
        except (ValueError, TypeError):
            stock_quantity_int = 1  # Default to 1 if conversion fails
        
        print(f"📦 Stock quantity check: raw={stock_quantity}, converted={stock_quantity_int}")
        
        # ✅ FIX: Check if equipment is actually available (stock > 0)
        if stock_quantity_int <= 0:
            conn.close()
            return jsonify({'error': 'Equipment out of stock'}), 400
        
        print(f"📝 Storing booking with: Equipment={equipment_name}, Vendor={vendor_name}")
        print(f"📦 Current stock: {stock_quantity_int}")
        
        # For instant booking, set duration to 1 day and use current date
        duration = 1
        start_date = datetime.now().strftime('%Y-%m-%d')
        end_date = datetime.now().strftime('%Y-%m-%d')
        
        # Insert booking into database with COMPLETE DATA
        cursor.execute("""
            INSERT INTO bookings 
            (user_id, user_name, user_email, user_phone,
             equipment_id, equipment_name, vendor_email, vendor_name,
             start_date, end_date, duration, total_amount, status, notes,
             created_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            session['user_id'],
            session['user_name'],
            session.get('user_email', ''),
            session.get('user_phone', ''),
            data['equipment_id'],
            equipment_name,
            vendor_email,
            vendor_name,
            start_date,
            end_date,
            duration,
            data['total_amount'],
            'pending',
            data.get('notes', '')
        ))
        
        booking_id = cursor.lastrowid
        
        # ✅ FIX: Update equipment stock (decrease by 1) - ONLY mark as unavailable if stock reaches 0
        new_stock = stock_quantity_int - 1
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = ?,
                status = CASE 
                    WHEN ? <= 0 THEN 'unavailable' 
                    WHEN ? <= min_stock_threshold THEN 'low_stock'  
                    ELSE 'available' 
                END
            WHERE id = ?
        """, (new_stock, new_stock, new_stock, data['equipment_id']))
        
        conn.commit()
        conn.close()
        
        print(f"✅ Booking #{booking_id} stored COMPLETELY in database")
        print(f"   Equipment: {equipment_name}")
        print(f"   Vendor: {vendor_name} ({vendor_email})")
        print(f"   User: {session['user_name']}")
        print(f"   Amount: ₹{data['total_amount']}")
        print(f"   Stock updated: {stock_quantity_int} → {new_stock}")
        print(f"   New status: {'unavailable' if new_stock <= 0 else 'low_stock' if new_stock <= (min_stock_threshold or 5) else 'available'}")
        
        # Send SMS notification to user
        user_message = f"Your booking for {equipment_name} has been submitted successfully! Booking ID: #{booking_id}"
        send_sms(session.get('user_phone', ''), user_message)
        
        return jsonify({
            'success': True,
            'message': 'Booking submitted successfully!',
            'booking_id': booking_id,
            'equipment_name': equipment_name,
            'vendor_name': vendor_name
        })
        
    except Exception as e:
        print(f"❌ Error submitting booking: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/bookings')
def get_user_bookings():
    """Get all bookings for the logged-in user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM bookings 
            WHERE user_id = ? 
            ORDER BY created_date DESC
        """, (session['user_id'],))
        
        bookings = cursor.fetchall()
        conn.close()
        
        bookings_list = []
        for booking in bookings:
            bookings_list.append({
                'id': booking['id'],
                'equipment_name': booking['equipment_name'],
                'vendor_name': booking['vendor_name'],
                'start_date': booking['start_date'],
                'end_date': booking['end_date'],
                'duration': booking['duration'],
                'total_amount': booking['total_amount'],
                'status': booking['status'],
                'notes': booking['notes'],
                'created_date': booking['created_date']
            })
        
        return jsonify(bookings_list)
        
    except Exception as e:
        print(f"Error fetching user bookings: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vendor/bookings')
def get_vendor_bookings():
    """Get all bookings for the logged-in vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        status_filter = request.args.get('status', 'all')
        vendor_email = session['vendor_email']
        
        print(f"🔄 Fetching bookings for vendor: {vendor_email}, filter: {status_filter}")
        
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        if status_filter == 'all':
            cursor.execute("""
                SELECT * FROM bookings 
                WHERE vendor_email = ?
                ORDER BY created_date DESC
            """, (vendor_email,))
        else:
            cursor.execute("""
                SELECT * FROM bookings 
                WHERE vendor_email = ? AND status = ?
                ORDER BY created_date DESC
            """, (vendor_email, status_filter))
        
        bookings = cursor.fetchall()
        conn.close()
        
        bookings_list = []
        for booking in bookings:
            bookings_list.append({
                'id': booking['id'],
                'user_name': booking['user_name'],
                'user_email': booking['user_email'],
                'user_phone': booking['user_phone'],
                'equipment_name': booking['equipment_name'],
                'equipment_id': booking['equipment_id'],
                'start_date': booking['start_date'],
                'end_date': booking['end_date'],
                'duration': booking['duration'],
                'total_amount': booking['total_amount'],
                'status': booking['status'],
                'notes': booking['notes'],
                'created_date': booking['created_date']
            })
        
        print(f"✅ Found {len(bookings_list)} bookings for vendor")
        return jsonify(bookings_list)
        
    except Exception as e:
        print(f"❌ Error fetching vendor bookings: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/equipment/update/<int:equipment_id>', methods=['POST'])
def update_equipment(equipment_id):
    """Update equipment details with separate pricing"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        print(f"🔄 Updating equipment ID: {equipment_id}")
        
        # Get form data
        name = request.form.get('name')
        category = request.form.get('category')
        description = request.form.get('description', '')
        
        # NEW: Rental pricing fields
        rental_price = request.form.get('rental_price')
        rental_price_unit = request.form.get('rental_price_unit', 'day')
        
        # NEW: Purchase pricing fields
        purchase_price = request.form.get('purchase_price')
        purchase_unit = request.form.get('purchase_unit', 'unit')
        equipment_type = request.form.get('equipment_type', 'both')
        
        location = request.form.get('location')
        status = request.form.get('status', 'available')
        stock_quantity = request.form.get('stock_quantity')
        min_stock_threshold = request.form.get('min_stock_threshold')
        
        print(f"📦 Received update data - equipment_type: {equipment_type}, rental_price: {rental_price}, purchase_price: {purchase_price}")
        
        # Validate required fields based on equipment type
        if not all([name, category, location, stock_quantity, min_stock_threshold, equipment_type]):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Validate pricing based on equipment type
        if equipment_type in ['both', 'rental_only'] and not rental_price:
            return jsonify({'error': 'Rental price is required for rental equipment'}), 400
        
        if equipment_type in ['both', 'purchase_only'] and not purchase_price:
            return jsonify({'error': 'Purchase price is required for purchase equipment'}), 400
        
        try:
            rental_price = float(rental_price) if rental_price and equipment_type in ['both', 'rental_only'] else 0
            purchase_price = float(purchase_price) if purchase_price and equipment_type in ['both', 'purchase_only'] else 0
            stock_quantity = int(stock_quantity)
            min_stock_threshold = int(min_stock_threshold)
        except ValueError as e:
            print(f"❌ Number conversion error: {e}")
            return jsonify({'error': 'Invalid numeric format'}), 400
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # First, get current equipment data to preserve existing image if no new one is uploaded
        cursor.execute("SELECT image_url FROM equipment WHERE id = ? AND vendor_email = ?", 
                      (equipment_id, session['vendor_email']))
        current_equipment = cursor.fetchone()
        
        if not current_equipment:
            conn.close()
            return jsonify({'error': 'Equipment not found or access denied'}), 404
        
        current_image_url = current_equipment[0]
        
        # Handle image upload - only update if new image is provided
        image_url = current_image_url  # Keep current image by default
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename:  # Only process if a file was actually selected
                new_image_url = save_uploaded_image(image_file)
                if new_image_url:
                    image_url = new_image_url
                    print(f"🖼️ New image saved: {image_url}")
        
        # Update equipment in database
        cursor.execute("""
            UPDATE equipment 
            SET name = ?, category = ?, description = ?, 
                rental_price = ?, rental_price_unit = ?, 
                purchase_price = ?, purchase_unit = ?, equipment_type = ?,
                location = ?, status = ?, image_url = ?,
                stock_quantity = ?, min_stock_threshold = ?
            WHERE id = ? AND vendor_email = ?
        """, (
            name, category, description,
            rental_price, rental_price_unit,
            purchase_price, purchase_unit, equipment_type,
            location, status, image_url,
            stock_quantity, min_stock_threshold,
            equipment_id, session['vendor_email']
        ))
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': 'Equipment not found or access denied'}), 404
        
        conn.commit()
        conn.close()
        
        print(f"✅ Equipment updated successfully: {equipment_id}")
        print(f"💰 Pricing updated: Rental=₹{rental_price}/{rental_price_unit}, Purchase=₹{purchase_price}/{purchase_unit}")
        print(f"📦 Type: {equipment_type}")
        
        return jsonify({
            'success': True, 
            'message': 'Equipment updated successfully',
            'image_url': image_url
        })
        
    except Exception as e:
        print(f"❌ Error updating equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/vendor/booking/<int:booking_id>/update', methods=['POST'])
def update_booking_status(booking_id):
    """Update booking status"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        if new_status not in ['confirmed', 'rejected', 'completed']:
            return jsonify({'error': 'Invalid status'}), 400
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Update booking status
        cursor.execute("""
            UPDATE bookings 
            SET status = ?, processed_date = CURRENT_TIMESTAMP 
            WHERE id = ? AND vendor_email = ?
        """, (new_status, booking_id, session['vendor_email']))
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': 'Booking not found or access denied'}), 404
        
        # If rejected or completed, mark equipment as available again
        if new_status in ['rejected', 'completed']:
            cursor.execute("""
                UPDATE equipment 
                SET status = 'available' 
                WHERE id = (
                    SELECT equipment_id FROM bookings WHERE id = ?
                )
            """, (booking_id,))
        
        conn.commit()
        
        # Get booking details for notification
        cursor.execute("""
            SELECT user_phone, user_name, equipment_name, total_amount 
            FROM bookings WHERE id = ?
        """, (booking_id,))
        
        booking_data = cursor.fetchone()
        conn.close()
        
        # Send SMS notification
        if booking_data:
            user_phone, user_name, equipment_name, total_amount = booking_data
            if new_status == 'confirmed':
                message = f"Dear {user_name}, your booking for {equipment_name} has been confirmed! Total amount: ₹{total_amount}."
            elif new_status == 'rejected':
                message = f"Dear {user_name}, your booking for {equipment_name} has been rejected."
            elif new_status == 'completed':
                message = f"Dear {user_name}, your booking period for {equipment_name} has been completed."
            
            send_sms(user_phone, message)
        
        return jsonify({
            'success': True, 
            'message': f'Booking {new_status} successfully'
        })
        
    except Exception as e:
        print(f"Error updating booking: {str(e)}")
        return jsonify({'error': str(e)}), 500

def send_booking_notification(booking_id, action):
    """Send notification when booking is created"""
    try:
        print(f"Notification: Booking #{booking_id} {action}")
        # You can add email/SMS notifications to vendor here
    except Exception as e:
        print(f"Error sending notification: {str(e)}")
@app.route('/api/equipment')
def get_equipment_for_users():
    """Get all available equipment for users (frontend)"""
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # ✅ FIX: Include 'low_stock' equipment too (but not 'unavailable')
        cursor.execute("""
            SELECT 
                e.*,
                v.contact_name as vendor_name,
                v.business_name,
                v.phone as vendor_phone,
                v.email as vendor_email
            FROM equipment e
            JOIN vendors v ON e.vendor_email = v.email
            WHERE e.status IN ('available', 'low_stock') AND e.stock_quantity > 0
            ORDER BY e.created_date DESC
        """)
        
        equipment = cursor.fetchall()
        conn.close()
        
        equipment_list = []
        for item in equipment:
            # Calculate stock status
            stock_quantity = item['stock_quantity'] or 1
            min_stock_threshold = item['min_stock_threshold'] or 5
            
            # Determine stock status for display
            stock_status = 'available'
            if stock_quantity <= 0:
                stock_status = 'out_of_stock'
            elif stock_quantity <= min_stock_threshold:
                stock_status = 'low_stock'
            
            # Handle image URL properly
            image_url = item['image_url']
            if image_url and image_url.startswith('/static/'):
                # Make sure the URL is correct
                image_url = image_url
            elif image_url and not image_url.startswith('http'):
                # Prepend /static/uploads/equipment/ if needed
                if not image_url.startswith('/static/uploads/equipment/'):
                    image_url = f"/static/uploads/equipment/{image_url}"
            
            equipment_data = {
                'id': item['id'],
                'name': item['name'],
                'category': item['category'],
                'description': item['description'],
                'price': item['price'],
                'price_unit': item['price_unit'],
                'rental_price': item['rental_price'] or item['price'],
                'rental_price_unit': item['rental_price_unit'] or 'day',
                'purchase_price': item['purchase_price'] or item['price'],
                'purchase_unit': item['purchase_unit'] or 'unit',
                'equipment_type': item['equipment_type'] or 'both',
                'location': item['location'],
                'image_url': image_url,
                'status': stock_status,  # ✅ Use calculated stock_status instead of raw status
                'stock_quantity': stock_quantity,
                'min_stock_threshold': min_stock_threshold,
                'vendor_name': item['vendor_name'],
                'business_name': item['business_name'],
                'vendor_phone': item['vendor_phone'],
                'vendor_email': item['vendor_email'],
                'created_date': item['created_date']
            }
            
            equipment_list.append(equipment_data)
        
        print(f"✅ Found {len(equipment_list)} equipment items for user")
        return jsonify(equipment_list)
        
    except Exception as e:
        print(f"❌ Error fetching equipment for users: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
@app.route('/api/equipment/add', methods=['POST'])
def add_equipment():
    """Add new equipment with separate rental and purchase pricing"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        print("📦 Processing equipment addition...")
        
        # Handle form data (including file upload)
        name = request.form.get('name')
        category = request.form.get('category')
        description = request.form.get('description', '')
        
        # NEW: Rental pricing fields
        rental_price = request.form.get('rental_price')
        rental_price_unit = request.form.get('rental_price_unit', 'day')
        
        # NEW: Purchase pricing fields
        purchase_price = request.form.get('purchase_price')
        purchase_unit = request.form.get('purchase_unit', 'unit')
        equipment_type = request.form.get('equipment_type', 'both')
        
        location = request.form.get('location')
        status = request.form.get('status', 'available')
        stock_quantity = request.form.get('stock_quantity')
        min_stock_threshold = request.form.get('min_stock_threshold')
        
        print(f"📦 Received form data - rental_price: {rental_price}, purchase_price: {purchase_price}, equipment_type: {equipment_type}")
        
        # Validate required fields based on equipment type
        if not all([name, category, location, stock_quantity, min_stock_threshold, equipment_type]):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Validate pricing based on equipment type
        if equipment_type in ['both', 'rental_only'] and not rental_price:
            return jsonify({'error': 'Rental price is required for rental equipment'}), 400
        
        if equipment_type in ['both', 'purchase_only'] and not purchase_price:
            return jsonify({'error': 'Purchase price is required for purchase equipment'}), 400
        
        try:
            rental_price = float(rental_price) if rental_price and equipment_type in ['both', 'rental_only'] else 0
            purchase_price = float(purchase_price) if purchase_price and equipment_type in ['both', 'purchase_only'] else 0
            stock_quantity = int(stock_quantity)
            min_stock_threshold = int(min_stock_threshold)
        except ValueError as e:
            print(f"❌ Number conversion error: {e}")
            return jsonify({'error': 'Invalid numeric format'}), 400
        
        # Handle image upload
        image_url = None
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename != '':
                image_url = save_uploaded_image(image_file)
                print(f"🖼️ Image saved: {image_url}")
        
        # Save to database
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # For backward compatibility, set price and price_unit based on equipment_type
        price = 0
        price_unit = ''
        if equipment_type in ['both', 'rental_only']:
            price = rental_price
            price_unit = rental_price_unit
        elif equipment_type == 'purchase_only':
            price = purchase_price
            price_unit = purchase_unit
        
        cursor.execute("""
            INSERT INTO equipment 
            (vendor_email, name, category, description, price, price_unit,
             rental_price, rental_price_unit, purchase_price, purchase_unit, equipment_type,
             location, status, image_url, stock_quantity, min_stock_threshold)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session['vendor_email'],
            name,
            category,
            description,
            price,
            price_unit,
            rental_price,
            rental_price_unit,
            purchase_price,
            purchase_unit,
            equipment_type,
            location,
            status,
            image_url,
            stock_quantity,
            min_stock_threshold
        ))
        
        conn.commit()
        equipment_id = cursor.lastrowid
        conn.close()
        
        print(f"✅ Equipment added successfully. ID: {equipment_id}")
        print(f"💰 Pricing: Rental=₹{rental_price}/{rental_price_unit}, Purchase=₹{purchase_price}/{purchase_unit}")
        print(f"📦 Type: {equipment_type}")
        
        return jsonify({
            'success': True,
            'message': 'Equipment added successfully',
            'equipment_id': equipment_id
        })
        
    except Exception as e:
        print(f"❌ Error adding equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/debug_database_tables')
def debug_database_tables():
    """Check if equipment table exists and has data"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check if equipment table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='equipment'")
        table_exists = cursor.fetchone()
        
        result = f"<h3>Database Debug</h3>"
        
        if table_exists:
            # Count equipment records
            cursor.execute("SELECT COUNT(*) FROM equipment")
            count = cursor.fetchone()[0]
            result += f"<p>Equipment table exists: YES</p>"
            result += f"<p>Total equipment records: {count}</p>"
            
            # Show all equipment
            cursor.execute("SELECT * FROM equipment")
            equipment = cursor.fetchall()
            result += f"<h4>Equipment Data:</h4>"
            for item in equipment:
                result += f"<p>ID: {item[0]}, Name: {item[2]}, Vendor: {item[1]}</p>"
        else:
            result += f"<p>Equipment table exists: NO</p>"
            
        conn.close()
        return result
        
    except Exception as e:
        return f"Error: {str(e)}"
@app.route('/api/vendor/equipment')
def get_vendor_equipment():
    """Get all equipment for the logged-in vendor - WITH NEW PRICING FIELDS"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM equipment 
            WHERE vendor_email = ? 
            ORDER BY created_date DESC
        """, (session['vendor_email'],))
        
        equipment = cursor.fetchall()
        conn.close()
        
        equipment_list = []
        for item in equipment:
            # ✅ FIX: Calculate stock status properly
            stock_quantity = item['stock_quantity'] or 0
            min_stock_threshold = item['min_stock_threshold'] or 5
            current_status = item['status']
            
            # Determine stock status for display
            stock_status = 'available'
            if stock_quantity <= 0:
                stock_status = 'out_of_stock'
            elif stock_quantity <= min_stock_threshold:
                stock_status = 'low_stock'
            
            # For backward compatibility, set price and price_unit based on equipment_type
            price = 0
            price_unit = ''
            if item['equipment_type'] in ['both', 'rental_only']:
                price = item['rental_price']
                price_unit = item['rental_price_unit']
            elif item['equipment_type'] == 'purchase_only':
                price = item['purchase_price']
                price_unit = item['purchase_unit']
            
            equipment_data = {
                'id': item['id'],
                'name': item['name'],
                'category': item['category'],
                'description': item['description'],
                'price': price,  # For backward compatibility
                'price_unit': price_unit,  # For backward compatibility
                'rental_price': item['rental_price'],
                'rental_price_unit': item['rental_price_unit'],
                'purchase_price': item['purchase_price'],
                'purchase_unit': item['purchase_unit'],
                'equipment_type': item['equipment_type'],
                'location': item['location'],
                'image_url': item['image_url'],
                'status': stock_status,  # ✅ Use calculated stock_status
                'raw_status': current_status,  # Keep original status for reference
                'stock_quantity': stock_quantity,
                'min_stock_threshold': item['min_stock_threshold'],
                'created_date': item['created_date']
            }
            equipment_list.append(equipment_data)
        
        return jsonify(equipment_list)
        
    except Exception as e:
        print(f"❌ Error fetching vendor equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500
def fix_equipment_statuses():
    """Fix equipment statuses based on stock quantity"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Update all equipment statuses based on stock quantity
        cursor.execute("""
            UPDATE equipment 
            SET status = CASE 
                WHEN stock_quantity <= 0 THEN 'unavailable'
                WHEN stock_quantity <= COALESCE(min_stock_threshold, 5) THEN 'low_stock'
                ELSE 'available'
            END
        """)
        
        affected_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        print(f"✅ Fixed equipment statuses for {affected_rows} items")
        
    except Exception as e:
        print(f"❌ Error fixing equipment statuses: {str(e)}")

# Call this after init_db()
init_db()
fix_equipment_statuses()
# ================= RENT SYSTEM ROUTES ==================
@app.route('/api/equipment/available')
def get_available_equipment():
    """Get all available equipment for rent with stock information"""
    try:
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT e.*, v.contact_name as vendor_name, v.business_name, v.phone as vendor_phone
            FROM equipment e, vendors v
            WHERE e.vendor_email = v.email AND e.status = 'available' AND e.stock_quantity > 0
            ORDER BY e.stock_quantity ASC, e.created_date DESC
        """)
        
        equipment = cursor.fetchall()
        conn.close()
        
        equipment_list = []
        for item in equipment:
            # Determine stock status
            stock_quantity = item['stock_quantity'] or 1
            min_stock_threshold = item['min_stock_threshold'] or 5
            
            stock_status = 'available'
            if stock_quantity <= 0:
                stock_status = 'out_of_stock'
            elif stock_quantity <= min_stock_threshold:
                stock_status = 'low_stock'
            
            # Handle image URL
            image_url = item['image_url']
            if image_url and image_url.startswith('/static/'):
                image_url = image_url.replace('/static/', '/')
            
            equipment_data = {
                'id': item['id'],
                'name': item['name'],
                'category': item['category'],
                'description': item['description'],
                'price': item['price'],
                'price_unit': item['price_unit'],
                'location': item['location'],
                'image_url': image_url,
                'status': item['status'],
                'stock_quantity': stock_quantity,
                'min_stock_threshold': min_stock_threshold,
                'stock_status': stock_status,
                'vendor_name': item['vendor_name'],
                'business_name': item['business_name'],
                'vendor_phone': item['vendor_phone']
            }
            equipment_list.append(equipment_data)
        
        return jsonify(equipment_list)
        
    except Exception as e:
        print(f"Error fetching equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500
# In the submit_rent_request function, update to use rental_price:
@app.route('/api/rent/submit-request', methods=['POST'])
def submit_rent_request():
    """Submit a rent request using rental price"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        print("📩 Received rent request:", data)
        
        # Validate required fields
        required_fields = ['equipment_id', 'start_date', 'end_date', 'purpose', 'total_amount']
        missing_fields = [field for field in required_fields if field not in data]
        
        if missing_fields:
            return jsonify({
                'success': False, 
                'error': f'Missing required fields: {", ".join(missing_fields)}'
            }), 400
        
        # Calculate rental details
        start_date = datetime.strptime(data['start_date'], '%Y-%m-%d')
        end_date = datetime.strptime(data['end_date'], '%Y-%m-%d')
        duration = (end_date - start_date).days + 1
        
        if duration <= 0:
            return jsonify({'error': 'End date must be after start date'}), 400
        
        # Get equipment details with vendor info
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                e.id, e.vendor_email, e.name, e.category, e.description,
                e.rental_price, e.rental_price_unit, e.purchase_price, e.purchase_unit, e.equipment_type,
                e.location, e.status, e.stock_quantity, e.min_stock_threshold, e.image_url,
                v.contact_name, v.business_name, v.phone
            FROM equipment e
            JOIN vendors v ON e.vendor_email = v.email
            WHERE e.id = ?
        """, (data['equipment_id'],))
        
        equipment = cursor.fetchone()
        if not equipment:
            conn.close()
            return jsonify({'error': 'Equipment not found'}), 404
        
        # Debug: Print equipment structure
        print(f"🔍 Equipment structure: {len(equipment)} columns")
        print(f"🔍 Equipment data: {equipment}")
        
        # Extract data with proper indices
        (
            equipment_id, vendor_email, equipment_name, category, description,
            rental_price, rental_price_unit, purchase_price, purchase_unit, equipment_type,
            location, status, stock_quantity, min_stock_threshold, image_url,
            vendor_name, business_name, vendor_phone
        ) = equipment
        
        # Check if equipment is available for rental
        if equipment_type == 'purchase_only':
            conn.close()
            return jsonify({'error': 'This equipment is only available for purchase, not rental'}), 400
        
        # ✅ FIX: Convert stock_quantity to integer
        try:
            if stock_quantity is None:
                stock_quantity_int = 1
            elif isinstance(stock_quantity, str):
                stock_quantity_int = int(stock_quantity) if stock_quantity.strip() != '' else 1
            else:
                stock_quantity_int = int(stock_quantity)
        except (ValueError, TypeError):
            stock_quantity_int = 1  # Default to 1 if conversion fails
        
        print(f"📦 Stock quantity check: raw={stock_quantity}, converted={stock_quantity_int}")
        
        if stock_quantity_int <= 0:
            conn.close()
            return jsonify({'error': 'Equipment out of stock'}), 400
        
        # ✅ FIX: Ensure rental_price is a float
        try:
            daily_rate = float(rental_price) if rental_price is not None else 0
        except (ValueError, TypeError):
            daily_rate = 0
        
        print(f"💰 Daily rate: {daily_rate} (rental_price={rental_price}, type={type(rental_price)})")
        
        # Calculate amounts
        base_amount = daily_rate * duration
        service_fee = base_amount * 0.1
        total_amount = data['total_amount']  # Use the total_amount from frontend
        
        print(f"📝 Storing rent request: Equipment={equipment_name}, Vendor={vendor_name}")
        print(f"💰 Price details: Daily Rate={daily_rate}, Duration={duration} days")
        print(f"💰 Amounts: Base={base_amount}, Service Fee={service_fee}, Total={total_amount}")
        
        # Insert rent request into database with COMPLETE DATA
        cursor.execute("""
            INSERT INTO rent_requests 
            (user_id, user_name, user_phone, user_email,
             equipment_id, equipment_name, vendor_email, vendor_name,
             start_date, end_date, duration, purpose, notes,
             daily_rate, base_amount, service_fee, total_amount, status,
             submitted_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            session['user_id'],
            session['user_name'],
            session.get('user_phone', ''),
            session.get('user_email', ''),
            data['equipment_id'],
            equipment_name,
            vendor_email,
            vendor_name,
            data['start_date'],
            data['end_date'],
            duration,
            data['purpose'],
            data.get('notes', ''),
            daily_rate,
            base_amount,
            service_fee,
            total_amount,
            'pending'
        ))
        
        request_id = cursor.lastrowid
        
        # In submit_rent_request function, find the stock update section:
        # ✅ Update equipment stock (decrease by 1) - ONLY mark as unavailable if stock reaches 0
        new_stock = stock_quantity_int - 1
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = ?,
                status = CASE 
                    WHEN ? <= 0 THEN 'unavailable' 
                    WHEN ? <= ? THEN 'low_stock'
                    ELSE 'available' 
                END
            WHERE id = ?
        """, (new_stock, new_stock, new_stock, min_stock_threshold or 5, data['equipment_id']))
        conn.commit()
        conn.close()
        
        print(f"✅ Rent request #{request_id} stored COMPLETELY in database")
        print(f"   Equipment: {equipment_name}")
        print(f"   Vendor: {vendor_name} ({vendor_email})")
        print(f"   User: {session['user_name']}")
        print(f"   Amount: ₹{total_amount}")
        print(f"   Dates: {data['start_date']} to {data['end_date']}")
        print(f"   Duration: {duration} days")
        print(f"   Stock updated: {stock_quantity_int} → {new_stock}")
        
        # Send notifications
        user_message = f"Your rent request for {equipment_name} has been submitted! Request ID: #{request_id}"
        send_sms(session.get('user_phone', ''), user_message)
        
        return jsonify({
            'success': True,
            'message': 'Rent request submitted successfully!',
            'request_id': request_id,
            'equipment_name': equipment_name,
            'vendor_name': vendor_name
        })
        
    except Exception as e:
        print(f"❌ Error submitting rent request: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
def fix_stock_quantity_column():
    """Ensure stock_quantity column is INTEGER type"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check column type
        cursor.execute("PRAGMA table_info(equipment)")
        columns = cursor.fetchall()
        
        for column in columns:
            if column[1] == 'stock_quantity' and column[2].upper() != 'INTEGER':
                print(f"⚠️ stock_quantity column is {column[2]}, converting to INTEGER...")
                
                # Create temporary table with correct schema
                cursor.execute("""
                    CREATE TABLE equipment_temp AS 
                    SELECT 
                        id, vendor_email, name, category, description,
                        price, price_unit, rental_price, rental_price_unit,
                        purchase_price, purchase_unit, equipment_type,
                        location, image_url, status,
                        CAST(stock_quantity AS INTEGER) as stock_quantity,
                        CAST(min_stock_threshold AS INTEGER) as min_stock_threshold,
                        created_date
                    FROM equipment
                """)
                
                # Drop old table and rename new one
                cursor.execute("DROP TABLE equipment")
                cursor.execute("ALTER TABLE equipment_temp RENAME TO equipment")
                
                # Recreate indexes and constraints if needed
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vendor_email ON equipment(vendor_email)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_equipment_status ON equipment(status)
                """)
                
                print("✅ stock_quantity column converted to INTEGER")
                break
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error fixing stock_quantity column: {str(e)}")

# Call this function after init_db()
init_db()
fix_stock_quantity_column()
def add_vendor_name_column():
    """Add vendor_name column to rent_requests table"""
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Check if column exists
        cursor.execute("PRAGMA table_info(rent_requests)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'vendor_name' not in columns:
            cursor.execute("ALTER TABLE rent_requests ADD COLUMN vendor_name TEXT")
            print("✅ Added vendor_name column to rent_requests table")
        else:
            print("⚠️ vendor_name column already exists")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error adding vendor_name column: {str(e)}")

# Call this function after init_db()
add_vendor_name_column()
@app.route('/api/vendor/rent-requests')
def get_vendor_rent_requests():
    """Get rent requests for vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        status_filter = request.args.get('status', 'all')
        vendor_email = session['vendor_email']
        
        print(f"🔄 Fetching rent requests for vendor: {vendor_email}, filter: {status_filter}")
        
        conn = sqlite3.connect('vendors.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        if status_filter == 'all':
            cursor.execute("""
                SELECT rr.*, e.name as equipment_name
                FROM rent_requests rr
                JOIN equipment e ON rr.equipment_id = e.id
                WHERE rr.vendor_email = ?
                ORDER BY rr.submitted_date DESC
            """, (vendor_email,))
        else:
            cursor.execute("""
                SELECT rr.*, e.name as equipment_name
                FROM rent_requests rr
                JOIN equipment e ON rr.equipment_id = e.id
                WHERE rr.vendor_email = ? AND rr.status = ?
                ORDER BY rr.submitted_date DESC
            """, (vendor_email, status_filter))
        
        rent_requests = cursor.fetchall()
        conn.close()
        
        requests_list = []
        for row in rent_requests:
            requests_list.append({
                'id': row['id'],
                'user_name': row['user_name'],
                'user_phone': row['user_phone'],
                'user_email': row['user_email'],
                'equipment_name': row['equipment_name'],
                'equipment_id': row['equipment_id'],
                'start_date': row['start_date'],
                'end_date': row['end_date'],
                'duration': row['duration'],
                'purpose': row['purpose'],
                'notes': row['notes'],
                'daily_rate': row['daily_rate'],
                'base_amount': row['base_amount'],
                'service_fee': row['service_fee'],
                'total_amount': row['total_amount'],
                'status': row['status'],
                'submitted_date': row['submitted_date'],
                'processed_date': row['processed_date']
            })
        
        print(f"✅ Found {len(requests_list)} rent requests for vendor")
        return jsonify(requests_list)
        
    except Exception as e:
        print(f"❌ Error fetching rent requests: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/equipment/delete/<int:equipment_id>', methods=['POST'])
def delete_equipment(equipment_id):
    """Delete equipment"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM equipment 
            WHERE id = ? AND vendor_email = ?
        """, (equipment_id, session['vendor_email']))
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': 'Equipment not found or access denied'}), 404
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Equipment deleted successfully'})
        
    except Exception as e:
        print(f"❌ Error deleting equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/api/vendor/rent-request/<int:request_id>/update', methods=['POST'])
def update_rent_request_status(request_id):
    """Update rent request status with proper SMS notifications"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        if new_status not in ['approved', 'rejected', 'completed']:
            return jsonify({'error': 'Invalid status'}), 400
        
        vendor_email = session['vendor_email']
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        print(f"🔄 Updating rent request #{request_id} to status: {new_status}")
        
        # Get current rent request details including equipment stock
        cursor.execute("""
            SELECT 
                rr.user_phone, rr.user_name, rr.equipment_name, rr.total_amount, rr.duration,
                rr.equipment_id, e.stock_quantity, e.min_stock_threshold,
                v.contact_name as vendor_name, v.phone as vendor_phone
            FROM rent_requests rr
            JOIN equipment e ON rr.equipment_id = e.id
            JOIN vendors v ON rr.vendor_email = v.email
            WHERE rr.id = ? AND rr.vendor_email = ?
        """, (request_id, vendor_email))
        
        request_data = cursor.fetchone()
        
        if not request_data:
            conn.close()
            return jsonify({'error': 'Rent request not found or access denied'}), 404
        
        (user_phone, user_name, equipment_name, total_amount, duration,
         equipment_id, current_stock, min_stock_threshold,
         vendor_name, vendor_phone) = request_data
        
        print(f"📱 Notification details: User={user_name}, Phone={user_phone}, Equipment={equipment_name}")
        print(f"📦 Current stock: {current_stock}")
        
        # Update rent request status
        cursor.execute("""
            UPDATE rent_requests 
            SET status = ?, processed_date = CURRENT_TIMESTAMP 
            WHERE id = ? AND vendor_email = ?
        """, (new_status, request_id, vendor_email))
        
        # Handle equipment stock based on status
        if new_status == 'approved':
            # ✅ STOCK ALREADY DECREASED WHEN USER SUBMITTED
            # Just update status if needed
            cursor.execute("""
                UPDATE equipment 
                SET status = CASE 
                    WHEN stock_quantity <= 0 THEN 'unavailable'
                    WHEN stock_quantity <= ? THEN 'low_stock'
                    ELSE 'available'
                END
                WHERE id = ?
            """, (min_stock_threshold or 5, equipment_id))
            
            print(f"✅ Approved rent: Stock NOT decreased (already decreased on submission)")
            
        elif new_status == 'rejected':
            # ✅ RESTOCK SINCE REJECTING THE REQUEST
            # Stock was decreased when user submitted, so we need to add it back
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 <= ? THEN 'low_stock'
                        WHEN stock_quantity + 1 > 0 THEN 'available'
                        ELSE status
                    END
                WHERE id = ?
            """, (min_stock_threshold or 5, equipment_id))
            print(f"✅ Rejected rent: Restocked equipment (+1)")
            
        elif new_status == 'completed':
            # ✅ RESTOCK equipment when rent is completed
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 <= ? THEN 'low_stock'
                        WHEN stock_quantity + 1 > 0 THEN 'available'
                        ELSE status
                    END
                WHERE id = ?
            """, (min_stock_threshold or 5, equipment_id))
            print(f"✅ Completed rent: Restocked equipment (+1)")
        
        conn.commit()
        conn.close()
        
        # ✅ SEND SMS NOTIFICATION TO USER
        if new_status == 'approved':
            message = f"🎉 Dear {user_name}, your rent request for {equipment_name} has been APPROVED by {vendor_name}! Total amount: ₹{total_amount} for {duration} days. Please contact vendor at {vendor_phone} for pickup details. - Lend A Hand"
        elif new_status == 'rejected':
            message = f"❌ Dear {user_name}, your rent request for {equipment_name} has been REJECTED by {vendor_name}. Please contact support if you have questions. - Lend A Hand"
        elif new_status == 'completed':
            message = f"✅ Dear {user_name}, your rental period for {equipment_name} has been COMPLETED. Equipment has been returned. Thank you for using Lend A Hand! - Lend A Hand"
        
        print(f"📤 Sending SMS to {user_phone}: {message}")
        
        sms_result = send_sms(user_phone, message)
        
        if sms_result.get('success'):
            print(f"✅ SMS sent successfully to user {user_name}")
        else:
            print(f"⚠️ SMS sending failed: {sms_result.get('error', 'Unknown error')}")
        
        # ✅ ALSO SEND SMS TO VENDOR (optional)
        if vendor_phone and new_status == 'approved':
            vendor_message = f"✅ You approved rent request #{request_id} for {equipment_name}. User: {user_name}, Amount: ₹{total_amount}, Duration: {duration} days. - Lend A Hand"
            vendor_sms_result = send_sms(vendor_phone, vendor_message)
            
            if vendor_sms_result.get('success'):
                print(f"✅ Vendor notification sent to {vendor_name}")
            else:
                print(f"⚠️ Vendor SMS failed: {vendor_sms_result.get('error', 'Unknown error')}")
        
        return jsonify({
            'success': True, 
            'message': f'Rent request {new_status} successfully',
            'sms_sent': sms_result.get('success', False)
        })
        
    except Exception as e:
        print(f"❌ Error updating rent request: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ADD THIS ROUTE TO YOUR FLASK APP.PY FILE
@app.route('/api/equipment/update-stock/<int:equipment_id>', methods=['POST'])
def update_equipment_stock(equipment_id):
    """Update equipment stock quantity"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        quantity_change = data.get('quantity_change', 0)
        
        conn = sqlite3.connect('vendors.db')
        cursor = conn.cursor()
        
        # Get current stock
        cursor.execute("SELECT stock_quantity FROM equipment WHERE id = ? AND vendor_email = ?", 
                      (equipment_id, session['vendor_email']))
        current_stock = cursor.fetchone()
        
        if not current_stock:
            conn.close()
            return jsonify({'error': 'Equipment not found or access denied'}), 404
        
        # Calculate new stock
        new_stock = current_stock[0] + quantity_change
        if new_stock < 0:
            new_stock = 0
        
        # Update stock
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = ?, 
                status = CASE WHEN ? <= 0 THEN 'unavailable' ELSE 'available' END
            WHERE id = ? AND vendor_email = ?
        """, (new_stock, new_stock, equipment_id, session['vendor_email']))
        
        conn.commit()
        conn.close()
        
        print(f"📦 Stock updated for equipment {equipment_id}: {current_stock[0]} → {new_stock}")
        
        return jsonify({
            'success': True, 
            'message': 'Stock updated successfully',
            'new_stock': new_stock
        })
        
    except Exception as e:
        print(f"❌ Error updating stock: {str(e)}")
        return jsonify({'error': str(e)}), 500
@app.route('/debug_database')
def debug_database():
    if 'vendor_id' not in session:
        return "Not logged in"
    
    vendor_id = session['vendor_id']
    
    conn = sqlite3.connect('vendors.db')
    c = conn.cursor()
    
    # Check what's in the vendors table
    c.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,))
    vendor_data = c.fetchone()
    
    conn.close()
    
    if vendor_data:
        return f"""
        <h3>Database Debug Info</h3>
        <p>Vendor ID: {vendor_data[0]}</p>
        <p>Business Name: {vendor_data[1]}</p>
        <p>Contact Name: {vendor_data[2]}</p>
        <p>Email: {vendor_data[3]}</p>
        <p>Phone: {vendor_data[4]}</p>
        <p>Service Type: {vendor_data[5]}</p>
        <hr>
        <p><a href="/vendordashboard">Go to Dashboard</a></p>
        """
    else:
        return "No vendor found in database"

if __name__ == '__main__':
    # Reinitialize database to ensure all columns are created
     # Start the automatic reminder scheduler
   
    init_db()
    add_reminder_columns()  # ✅ ADD THIS LINE
    start_reminder_scheduler()

    app.run(debug=True)


