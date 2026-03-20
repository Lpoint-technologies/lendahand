from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory
import psycopg2
import os
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import requests
import json
from datetime import datetime, timedelta, date
import uuid
import threading
import time
import random

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# ================= DATABASE CONNECTION FUNCTIONS ==================

def get_vendors_db():
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not DATABASE_URL:
        # Fallback for local development
        DATABASE_URL = "postgresql://postgres:Hruthik@2004@localhost:5432/vendors"
        print("⚠️ Using local database connection")

    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    return psycopg2.connect(
        DATABASE_URL,
        sslmode='require',
        cursor_factory=RealDictCursor
    )

# OTP storage (in-memory)
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
        print("📱 SMS Response:", response.text)
        
        response_data = response.json()
        
        if response_data.get('return', False):
            return {'success': True, 'message_id': response_data.get('request_id')}
        else:
            error_msg = response_data.get('message', 'Unknown error')
            print(f"❌ SMS failed: {error_msg}")
            return {'success': False, 'error': error_msg}
            
    except Exception as e:
        print(f"❌ SMS Error: {str(e)}")
        return {'success': False, 'error': str(e)}

# ================= AUTOMATIC REMINDER FUNCTIONS ==================
def check_and_send_automatic_reminders():
    """Check for due returns and send automatic reminders 2 days before end date"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        today = datetime.now().date()
        two_days_from_now = today + timedelta(days=2)
        
        print(f"🔔 Checking reminders: Today={today}, Looking for end_date={two_days_from_now}")
        
        cursor.execute("""
            SELECT rr.id, rr.user_name, rr.user_phone, rr.equipment_name, rr.end_date
            FROM rent_requests rr
            WHERE rr.status = 'approved' 
            AND rr.end_date = %s
            AND (rr.last_reminder_sent IS NULL OR rr.last_reminder_sent < %s)
        """, (two_days_from_now.strftime('%Y-%m-%d'), today))
        
        due_requests = cursor.fetchall()
        
        print(f"📱 Found {len(due_requests)} requests needing 2-day reminders")
        
        for request in due_requests:
            request_id = request['id']
            user_name = request['user_name']
            user_phone = request['user_phone']
            equipment_name = request['equipment_name']
            end_date = request['end_date']
            
            user_phone_clean = ''.join(filter(str.isdigit, str(user_phone)))
            
            reminder_message = f"REMINDER: Your rental for {equipment_name} is due in 2 days (on {end_date}). Please prepare for return. - Lend A Hand"
            
            sms_result = send_sms(user_phone_clean, reminder_message)
            
            if sms_result.get('success'):
                print(f"✅ Auto-reminder sent for request #{request_id} to {user_name}")
                cursor.execute("""
                    UPDATE rent_requests 
                    SET last_reminder_sent = %s, reminder_type = 'auto_2day'
                    WHERE id = %s
                """, (datetime.now(), request_id))
            else:
                print(f"❌ Failed to send reminder for request #{request_id}: {sms_result.get('error')}")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error in automatic reminder system: {str(e)}")

def check_and_complete_expired_rentals():
    """Check for expired rentals and mark them as completed + restock equipment"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        today = datetime.now().date()
        
        print(f"🔄 Checking for expired rentals: Today={today}")
        
        cursor.execute("""
            SELECT rr.id, rr.equipment_id, rr.equipment_name, rr.user_name, rr.user_phone, 
                   e.min_stock_threshold
            FROM rent_requests rr
            JOIN equipment e ON rr.equipment_id = e.id
            WHERE rr.status = 'approved' 
            AND rr.end_date < %s
        """, (today.strftime('%Y-%m-%d'),))
        
        expired_rentals = cursor.fetchall()
        
        print(f"📦 Found {len(expired_rentals)} expired rentals to complete")
        
        for rental in expired_rentals:
            request_id = rental['id']
            equipment_id = rental['equipment_id']
            equipment_name = rental['equipment_name']
            user_name = rental['user_name']
            user_phone = rental['user_phone']
            min_stock_threshold = rental['min_stock_threshold'] or 5
            
            print(f"✅ Completing expired rental #{request_id} for {equipment_name}")
            
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'completed', processed_date = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (request_id,))
            
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 <= %s THEN 'low_stock'
                        WHEN stock_quantity + 1 > 0 THEN 'available'
                        ELSE status
                    END
                WHERE id = %s
            """, (min_stock_threshold, equipment_id))
            
            completion_message = f"Your rental period for {equipment_name} has been automatically completed. Equipment has been restocked. Thank you for using Lend A Hand!"
            send_sms(user_phone, completion_message)
            
            print(f"✅ Rental #{request_id} completed and equipment restocked")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"❌ Error in automatic completion system: {str(e)}")

def check_emi_due_dates():
    """Check for EMI due dates and update loan status (overdue after 1 month, default after 3)"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        today = datetime.now().date()
        
        # Find loans where EMI is due (next_due_date <= today)
        cursor.execute("""
            SELECT * FROM loan_purchases 
            WHERE status IN ('active', 'overdue')
            AND next_due_date <= %s
        """, (today,))
        
        due_loans = cursor.fetchall()
        
        for loan in due_loans:
            # Check if payment was made for this due date
            cursor.execute("""
                SELECT COUNT(*) as paid 
                FROM loan_payments 
                WHERE loan_id = %s 
                AND DATE(payment_date) >= %s
                AND payment_month = %s
            """, (loan['id'], loan['next_due_date'], loan['emi_paid'] + 1))
            
            payment_made = cursor.fetchone()['paid'] > 0
            
            if not payment_made:
                # Calculate days since due
                days_since_due = (today - loan['next_due_date']).days
                
                # Update missed payments count
                new_emi_missed = (loan['emi_missed'] or 0) + 1
                
                # Determine new status
                new_status = loan['status']
                if days_since_due >= 90:  # 3 months overdue
                    new_status = 'defaulted'
                    default_amount = loan['emi_amount'] * 3
                    # Send default notification
                    send_sms(loan['user_phone'], 
                        f"⚠️ URGENT: Your loan for {loan['equipment_name']} has been marked as DEFAULTED due to 3 missed payments. Total overdue: ₹{default_amount}. Please contact support immediately. - Lend A Hand")
                elif days_since_due >= 30:  # 1 month overdue
                    new_status = 'overdue'
                    # Send overdue reminder
                    if new_emi_missed == 1:
                        send_sms(loan['user_phone'], 
                            f"REMINDER: Your EMI for {loan['equipment_name']} of ₹{loan['emi_amount']} is now OVERDUE by {days_since_due} days. Please pay immediately to avoid default. - Lend A Hand")
                else:
                    new_status = 'active'
                    # Send regular reminder for first few days
                    if days_since_due <= 7:
                        send_sms(loan['user_phone'], 
                            f"REMINDER: Your EMI for {loan['equipment_name']} of ₹{loan['emi_amount']} was due on {loan['next_due_date']}. Please pay to avoid late fees. - Lend A Hand")
                
                # Update loan record
                cursor.execute("""
                    UPDATE loan_purchases 
                    SET emi_missed = %s,
                        default_days = %s,
                        default_amount = emi_amount * %s,
                        status = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (new_emi_missed, days_since_due, new_emi_missed, new_status, loan['id']))
                
                print(f"📊 Loan #{loan['id']}: {days_since_due} days overdue, status: {new_status}")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"Error checking EMI due dates: {str(e)}")

def start_reminder_scheduler():
    """Start the background scheduler for all automatic tasks"""
    def run_scheduler():
        while True:
            try:
                check_and_send_automatic_reminders()
                check_and_complete_expired_rentals()
                check_emi_due_dates()
            except Exception as e:
                print(f"❌ Scheduler error: {str(e)}")
            time.sleep(86400)  # Run every 24 hours
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print("✅ Automatic reminder, return, and EMI scheduler started")

# ================= DATABASE INITIALIZATION ==================

def init_vendors_db():
    """Initialize vendors database with all tables"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor()
       
        
        # Farmers table (moved from agriculture DB)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS farmers (
                id SERIAL PRIMARY KEY,
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
        
        # Rest of your existing tables...
        # vendors, equipment, rent_requests, etc.
        # Vendors table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vendors (
                id SERIAL PRIMARY KEY,
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
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        # Equipment table with separate pricing
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS equipment (
                id SERIAL PRIMARY KEY,
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
                avg_rating REAL DEFAULT 0,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (vendor_email) REFERENCES vendors(email)
            )
        ''')
        
        # Rent requests table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rent_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                user_phone TEXT NOT NULL,
                user_email TEXT,
                equipment_id INTEGER NOT NULL,
                equipment_name TEXT NOT NULL,
                vendor_email TEXT NOT NULL,
                vendor_name TEXT,
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
                processed_date TIMESTAMP,
                last_reminder_sent TIMESTAMP,
                reminder_type TEXT,
                cancellation_requested_date TIMESTAMP,
                cancellation_reason TEXT,
                status_before_cancel TEXT,
                cancelled_date TIMESTAMP,
                FOREIGN KEY (equipment_id) REFERENCES equipment(id),
                FOREIGN KEY (vendor_email) REFERENCES vendors(email)
            )
        ''')
        
        # Bookings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
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
                processed_date TIMESTAMP,
                cancellation_requested_date TIMESTAMP,
                cancellation_reason TEXT,
                status_before_cancel TEXT,
                cancelled_date TIMESTAMP,
                FOREIGN KEY (equipment_id) REFERENCES equipment(id),
                FOREIGN KEY (vendor_email) REFERENCES vendors(email)
            )
        ''')
        
        # Reviews table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reviews (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                equipment_id INTEGER NOT NULL,
                equipment_name TEXT NOT NULL,
                vendor_email TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                order_type TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
                title TEXT NOT NULL,
                comment TEXT NOT NULL,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active',
                FOREIGN KEY (equipment_id) REFERENCES equipment(id),
                FOREIGN KEY (vendor_email) REFERENCES vendors(email)
            )
        ''')
        
        # Cancellation requests table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cancellation_requests (
                id SERIAL PRIMARY KEY,
                order_id INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                user_email TEXT NOT NULL,
                user_phone TEXT NOT NULL,
                user_location TEXT,
                vendor_email TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                vendor_business_name TEXT,
                vendor_contact_phone TEXT,
                equipment_id INTEGER NOT NULL,
                equipment_name TEXT NOT NULL,
                equipment_category TEXT,
                equipment_description TEXT,
                equipment_price REAL,
                equipment_price_unit TEXT,
                equipment_location TEXT,
                equipment_image_url TEXT,
                total_amount REAL NOT NULL,
                start_date TEXT,
                end_date TEXT,
                duration INTEGER,
                order_notes TEXT,
                purpose TEXT,
                order_status_before_cancel TEXT NOT NULL,
                order_created_date TEXT,
                cancellation_reason TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                requested_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_date TIMESTAMP,
                processed_by TEXT,
                vendor_response_notes TEXT,
                days_until_start INTEGER,
                is_urgent BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (equipment_id) REFERENCES equipment(id),
                FOREIGN KEY (vendor_email) REFERENCES vendors(email)
            )
        ''')
        
        # Broadcast history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_history (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                type TEXT DEFAULT 'announcement',
                recipients_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                sent_by TEXT,
                sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'sent'
            )
        ''')
        
        # NEW: Loan purchases table for equipment bought on loan
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS loan_purchases (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                user_phone TEXT NOT NULL,
                user_email TEXT,
                equipment_id INTEGER NOT NULL,
                equipment_name TEXT NOT NULL,
                vendor_email TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                purchase_amount DECIMAL(10,2) NOT NULL,
                down_payment DECIMAL(10,2) DEFAULT 0,
                loan_amount DECIMAL(10,2) NOT NULL,
                interest_rate DECIMAL(5,2) NOT NULL,
                loan_term_years INTEGER NOT NULL,
                loan_term_months INTEGER NOT NULL,
                emi_amount DECIMAL(10,2) NOT NULL,
                total_payable DECIMAL(10,2) NOT NULL,
                total_interest DECIMAL(10,2) NOT NULL,
                purchase_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                first_emi_date DATE,
                last_emi_date DATE,
                payment_mode TEXT DEFAULT 'loan',
                status TEXT DEFAULT 'active',
                emi_paid INTEGER DEFAULT 0,
                emi_missed INTEGER DEFAULT 0,
                default_amount DECIMAL(10,2) DEFAULT 0,
                default_days INTEGER DEFAULT 0,
                last_payment_date TIMESTAMP,
                next_due_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes TEXT,
                FOREIGN KEY (equipment_id) REFERENCES equipment(id),
                FOREIGN KEY (vendor_email) REFERENCES vendors(email)
            )
        ''')

        # NEW: Loan payments table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS loan_payments (
                id SERIAL PRIMARY KEY,
                loan_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                due_date DATE NOT NULL,
                amount_paid DECIMAL(10,2) NOT NULL,
                principal_paid DECIMAL(10,2) NOT NULL,
                interest_paid DECIMAL(10,2) NOT NULL,
                penalty_amount DECIMAL(10,2) DEFAULT 0,
                payment_method TEXT DEFAULT 'cash',
                transaction_id TEXT,
                status TEXT DEFAULT 'completed',
                payment_month INTEGER NOT NULL,
                remarks TEXT,
                FOREIGN KEY (loan_id) REFERENCES loan_purchases(id)
            )
        ''')
        
        conn.commit()
        conn.close()
        print("✅ Vendors database initialized with loan tables")
        
    except Exception as e:
        print(f"❌ Error initializing vendors database: {str(e)}")

def init_databases():
    init_vendors_db()  # This now creates farmers table too
    print("✅ Vendors database initialized successfully")

# ================= OTP FUNCTIONS ==================

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
        del otp_storage[phone]
        return False
    
    if stored_data['otp'] == otp:
        del otp_storage[phone]
        return True
    
    return False

def update_password(phone, new_password):
    """Update vendor password in database"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        hashed_password = generate_password_hash(new_password)
        
        cursor.execute("UPDATE vendors SET password = %s WHERE phone = %s", 
                      (hashed_password, phone))
        
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected > 0
        
    except Exception as e:
        print(f"Error updating password: {str(e)}")
        return False

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
        del farmer_otp_storage[phone]
        print(f"Farmer OTP expired for {phone}")
        return False
    
    if stored_data['otp'] == otp:
        del farmer_otp_storage[phone]
        print(f"Farmer OTP verified for {phone}")
        return True
    
    print(f"Farmer OTP mismatch for {phone}")
    return False

def update_farmer_password(phone, new_password):
    """Update farmer password in database"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        hashed_password = generate_password_hash(new_password)
        
        cursor.execute("UPDATE farmers SET password = %s WHERE phone = %s", 
                      (hashed_password, phone))
        
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        print(f"Password updated for farmer with phone: {phone}")
        return affected > 0
        
    except Exception as e:
        print(f"Error updating farmer password: {str(e)}")
        return False

# ================= FILE UPLOAD CONFIG ==================

UPLOAD_FOLDER = 'static/uploads/equipment'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

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
        return f"/static/uploads/equipment/{unique_filename}"
    return None

# ================= STATIC FILE SERVING ==================

@app.route('/static/uploads/equipment/<filename>')
def serve_equipment_image(filename):
    return send_from_directory('static/uploads/equipment', filename)

@app.route('/uploads/equipment/<filename>')
def serve_equipment_image_to_users(filename):
    uploads_path = os.path.join(app.root_path, 'static', 'uploads', 'equipment')
    return send_from_directory(uploads_path, filename)

@app.route('/uploads/vendor_documents/<filename>')
def serve_vendor_document(filename):
    try:
        return send_from_directory('static/uploads/vendor_documents', filename)
    except:
        return "Document not found", 404

# ================= BASIC ROUTES ==================

@app.route('/')
def index():
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    lang = request.args.get('lang', 'en')
    if lang == 'kn':
        session['language'] = 'kn'
    return render_template('dashboard.html')

@app.context_processor
def inject_lang():
    return {'current_lang': session.get('language', 'en')}

@app.route('/index.html')
def index_page():
    return render_template('index.html')

# ================= FORGOT PASSWORD ROUTES ==================

@app.route("/farmer/forgot_password_modal", methods=["POST"])
def farmer_forgot_password_modal():
    """Handle farmer forgot password from modal - Step 1: Send OTP"""
    phone = request.form.get("phone")
    
    if not phone:
        return jsonify({"success": False, "message": "Phone number is required"})
    
    phone_clean = ''.join(filter(str.isdigit, phone))
    
    conn = get_vendors_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM farmers WHERE phone = %s", (phone_clean,))
    farmer = cursor.fetchone()
    conn.close()
    
    if not farmer:
        print(f"No farmer found with phone: {phone_clean}")
        return jsonify({"success": False, "message": "Phone number not registered!"})
    
    otp = generate_farmer_otp()
    save_farmer_otp(phone_clean, otp)
    
    message = f"Your Lend A Hand farmer password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone_clean, message)
    
    if sms_result['success']:
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
    
    if not session.get('farmer_otp_verified'):
        return jsonify({"success": False, "message": "Please verify OTP first"})
    
    if not phone or not new_password:
        return jsonify({"success": False, "message": "Invalid request"})
    
    if new_password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match!"})
    
    import re
    if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', new_password):
        return jsonify({
            "success": False, 
            "message": "Password must have 8+ chars, uppercase, lowercase, number, special char."
        })
    
    if update_farmer_password(phone, new_password):
        session.pop('farmer_reset_phone', None)
        session.pop('farmer_otp_verified', None)
        
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
    
    otp = generate_farmer_otp()
    save_farmer_otp(phone, otp)
    
    message = f"Your Lend A Hand farmer password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone, message)
    
    if sms_result['success']:
        return jsonify({"success": True, "message": f"New OTP sent to {phone}"})
    else:
        return jsonify({"success": False, "message": "Failed to resend OTP"})

@app.route("/vendor/forgot_password_modal", methods=["POST"])
def vendor_forgot_password_modal():
    """Handle vendor forgot password from modal - Step 1: Send OTP"""
    phone = request.form.get("phone")
    
    if not phone:
        return jsonify({"success": False, "message": "Phone number is required"})
    
    phone_clean = ''.join(filter(str.isdigit, phone))
    
    conn = get_vendors_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM vendors WHERE phone = %s", (phone_clean,))
    vendor = cursor.fetchone()
    conn.close()
    
    if not vendor:
        return jsonify({"success": False, "message": "Phone number not registered!"})
    
    otp = generate_otp()
    save_otp(phone_clean, otp)
    
    message = f"Your Lend A Hand vendor password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone_clean, message)
    
    if sms_result['success']:
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
    
    if not session.get('vendor_otp_verified'):
        return jsonify({"success": False, "message": "Please verify OTP first"})
    
    if not phone or not new_password:
        return jsonify({"success": False, "message": "Invalid request"})
    
    if new_password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match!"})
    
    import re
    if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', new_password):
        return jsonify({
            "success": False, 
            "message": "Password must have 8+ chars, uppercase, lowercase, number, special char."
        })
    
    if update_password(phone, new_password):
        session.pop('vendor_reset_phone', None)
        session.pop('vendor_otp_verified', None)
        
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
    
    otp = generate_otp()
    save_otp(phone, otp)
    
    message = f"Your Lend A Hand vendor password reset OTP is: {otp}. Valid for 10 minutes."
    sms_result = send_sms(phone, message)
    
    if sms_result['success']:
        return jsonify({"success": True, "message": f"New OTP sent to {phone}"})
    else:
        return jsonify({"success": False, "message": "Failed to resend OTP"})

# ================= USER REGISTRATION & LOGIN ==================

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
                flash('Invalid file type for RTC document. Please upload JPG or PNG files.', 'error')
                return render_template('userreg.html')

        if password != confirm_password:
            flash('Passwords do not match!', 'error')
            return render_template('userreg.html')

        import re
        if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', password):
            flash('Password must have 8+ chars, uppercase, lowercase, number, special char.', 'error')
            return render_template('userreg.html')

        hashed_password = generate_password_hash(password)

        try:
            conn = get_vendors_db()
            cursor = conn.cursor(cursor_factory=RealDictCursor)

            if email:
                cursor.execute("SELECT id FROM farmers WHERE email = %s", (email,))
                if cursor.fetchone():
                    flash('Email already registered!', 'error')
                    conn.close()
                    return render_template('userreg.html')

            cursor.execute('''INSERT INTO farmers 
                        (full_name, last_name, email, phone, farm_location, farm_size, 
                         crop_types, password, additional_info, rtc_document)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                      (full_name, last_name, email, phone, farm_location, farm_size,
                       crop_types, hashed_password, additional_info, rtc_filename))
            conn.commit()
            conn.close()

            sms_message = "Thank you for registering with us! Your form is under process."
            send_sms(phone, sms_message)

            flash('Your farmer application has been submitted successfully! Please login.', 'success')
            return redirect(url_for('farmer_login'))

        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
            return render_template('userreg.html')

    return render_template('userreg.html')

@app.route('/api/user/loans')
def get_user_loans():
    """Get all loans for the logged-in farmer"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        user_id = session['user_id']
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT 
                lp.*,
                e.image_url as equipment_image,
                v.contact_name as vendor_contact,
                v.business_name
            FROM loan_purchases lp
            LEFT JOIN equipment e ON lp.equipment_id = e.id
            LEFT JOIN vendors v ON lp.vendor_email = v.email
            WHERE lp.user_id = %s
            ORDER BY lp.created_at DESC
        """, (user_id,))
        
        loans = cursor.fetchall()
        
        # Calculate days overdue for each loan
        today = datetime.now().date()
        for loan in loans:
            if loan['next_due_date'] and loan['status'] in ['active', 'overdue']:
                due_date = loan['next_due_date']
                if isinstance(due_date, str):
                    due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
                days_overdue = (today - due_date).days if today > due_date else 0
                loan['days_overdue'] = days_overdue
        
        conn.close()
        return jsonify(loans)
        
    except Exception as e:
        print(f"Error fetching user loans: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/loan/pay-emi', methods=['POST'])
def pay_emi():
    """Farmer pays an EMI"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        loan_id = data.get('loan_id')
        payment_method = data.get('payment_method')
        transaction_id = data.get('transaction_id')
        remarks = data.get('remarks')
        
        if not loan_id:
            return jsonify({'error': 'Loan ID is required'}), 400
            
        user_id = session['user_id']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # First try to get from loan_purchases
        cursor.execute("""
            SELECT * FROM loan_purchases 
            WHERE id = %s AND user_id = %s
        """, (loan_id, user_id))
        
        loan = cursor.fetchone()
        table_name = 'loan_purchases'
        
        # If not found, try loan_history
        if not loan:
            cursor.execute("""
                SELECT * FROM loan_history 
                WHERE id = %s AND user_id = %s
            """, (loan_id, user_id))
            loan = cursor.fetchone()
            table_name = 'loan_history'
        
        if not loan:
            conn.close()
            return jsonify({'error': 'Loan not found'}), 404
        
        if loan['status'] == 'defaulted':
            return jsonify({'error': 'Cannot pay EMI on defaulted loan. Contact support.'}), 400
        
        if loan['status'] == 'completed':
            return jsonify({'error': 'Loan is already completed'}), 400
        
        # Convert Decimal to float for calculations
        loan_amount = float(loan['loan_amount'])
        emi_amount = float(loan['emi_amount'])
        interest_rate = float(loan['interest_rate'])
        emi_paid = int(loan['emi_paid'] or 0)
        loan_term_months = int(loan['loan_term_months'])
        
        # Calculate payment details
        monthly_rate = interest_rate / 100 / 12
        
        # Calculate outstanding balance
        outstanding = loan_amount - (emi_paid * emi_amount * 0.7)
        if outstanding < 0:
            outstanding = 0
            
        interest_paid = outstanding * monthly_rate
        principal_paid = emi_amount - interest_paid
        
        if principal_paid < 0:
            principal_paid = 0
            interest_paid = emi_amount
        
        payment_month = emi_paid + 1
        
        # Determine which table to use for the foreign key
                # FIXED: Determine which table to use for the foreign key - ALWAYS use loan_purchases ID
        if table_name == 'loan_purchases':
            payment_loan_id = loan_id
        else:
            # If we're in loan_history, find or create the corresponding loan_purchases record
            cursor.execute("""
                SELECT id FROM loan_purchases 
                WHERE user_id = %s AND equipment_id = %s AND loan_amount = %s
                ORDER BY id DESC LIMIT 1
            """, (user_id, loan['equipment_id'], loan['loan_amount']))
            
            existing_purchase = cursor.fetchone()
            
            if existing_purchase:
                payment_loan_id = existing_purchase['id']
            else:
                # Create a loan_purchases record from the loan_history data
                cursor.execute("""
                    INSERT INTO loan_purchases 
                    (user_id, user_name, user_phone, user_email, equipment_id, equipment_name, 
                     vendor_email, vendor_name, purchase_amount, down_payment, loan_amount, 
                     interest_rate, loan_term_years, loan_term_months, emi_amount, 
                     total_payable, total_interest, first_emi_date, last_emi_date, 
                     payment_mode, status, emi_paid, next_due_date, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    loan['user_id'], loan['user_name'], loan['user_phone'], loan['user_email'],
                    loan['equipment_id'], loan['equipment_name'],
                    loan['vendor_email'], loan['vendor_name'],
                    loan['loan_amount'], loan['down_payment'] if 'down_payment' in loan else 0, 
                    loan['loan_amount'],
                    loan['interest_rate'],
                    loan['loan_term_years'] if 'loan_term_years' in loan else loan['loan_term_months'] // 12,
                    loan['loan_term_months'],
                    loan['emi_amount'],
                    loan['total_payable'] if 'total_payable' in loan else loan['loan_amount'],
                    loan['total_interest'] if 'total_interest' in loan else 0,
                    loan['first_emi_date'] if 'first_emi_date' in loan else None,
                    loan['last_emi_date'] if 'last_emi_date' in loan else None,
                    loan['payment_mode'] if 'payment_mode' in loan else 'loan',
                    'active',
                    loan['emi_paid'],
                    loan['next_due_date'],
                    datetime.now()
                ))
                new_purchase = cursor.fetchone()
                payment_loan_id = new_purchase['id']
        
        # Record payment in loan_payments
        cursor.execute("""
            INSERT INTO loan_payments 
            (loan_id, user_id, due_date, amount_paid, principal_paid, 
             interest_paid, payment_method, transaction_id, payment_month, remarks)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            payment_loan_id,
            user_id,
            loan['next_due_date'],
            emi_amount,
            principal_paid,
            interest_paid,
            payment_method,
            transaction_id,
            payment_month,
            remarks
        ))
        
        # Update the original loan record
        new_emi_paid = emi_paid + 1
        
        # Calculate next due date
        next_due_date = None
        if loan['next_due_date']:
            if isinstance(loan['next_due_date'], str):
                next_due = datetime.strptime(loan['next_due_date'], '%Y-%m-%d').date()
            else:
                next_due = loan['next_due_date']
            
            if next_due.month == 12:
                next_due_date = date(next_due.year + 1, 1, next_due.day)
            else:
                next_due_date = date(next_due.year, next_due.month + 1, next_due.day)
        
        # Determine new status
        new_status = loan['status']
        if new_emi_paid >= loan_term_months:
            new_status = 'completed'
        elif loan['status'] == 'overdue':
            if next_due_date and next_due_date < datetime.now().date():
                new_status = 'overdue'
            else:
                new_status = 'active'
        else:
            new_status = 'active'
        
        # Update the original table only - NO LOAN_HISTORY UPDATE
        if table_name == 'loan_purchases':
            cursor.execute("""
                UPDATE loan_purchases 
                SET emi_paid = %s,
                    emi_missed = 0,
                    default_days = 0,
                    last_payment_date = CURRENT_TIMESTAMP,
                    next_due_date = %s,
                    status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (new_emi_paid, next_due_date, new_status, loan_id))
        else:
            cursor.execute("""
                UPDATE loan_history 
                SET emi_paid = %s,
                    emi_missed = 0,
                    default_days = 0,
                    last_payment_date = CURRENT_TIMESTAMP,
                    next_due_date = %s,
                    status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (new_emi_paid, next_due_date, new_status, loan_id))
        
        conn.commit()
        conn.close()
        
        # Send confirmation SMS
        try:
            next_due_str = next_due_date.strftime('%d-%b-%Y') if next_due_date else 'N/A'
            send_sms(loan['user_phone'], 
                f"✅ EMI payment of ₹{emi_amount} for {loan['equipment_name']} received. EMI {new_emi_paid}/{loan_term_months} paid. Next due: {next_due_str} - Lend A Hand")
        except:
            pass
        
        return jsonify({
            'success': True,
            'message': 'EMI paid successfully'
        })
        
    except Exception as e:
        print(f"Error paying EMI: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/loan/<int:loan_id>/schedule')
def get_loan_schedule(loan_id):
    """Get payment schedule for a loan"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        user_id = session['user_id']
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get loan details
        cursor.execute("""
            SELECT * FROM loan_purchases 
            WHERE id = %s AND user_id = %s
        """, (loan_id, user_id))
        
        loan = cursor.fetchone()
        
        if not loan:
            conn.close()
            return jsonify({'error': 'Loan not found'}), 404
        
        # Convert date objects to strings
        if loan.get('next_due_date') and not isinstance(loan['next_due_date'], str):
            loan['next_due_date'] = loan['next_due_date'].strftime('%Y-%m-%d')
        if loan.get('first_emi_date') and not isinstance(loan['first_emi_date'], str):
            loan['first_emi_date'] = loan['first_emi_date'].strftime('%Y-%m-%d')
        if loan.get('last_emi_date') and not isinstance(loan['last_emi_date'], str):
            loan['last_emi_date'] = loan['last_emi_date'].strftime('%Y-%m-%d')
        
        # Get payment history
        cursor.execute("""
            SELECT * FROM loan_payments 
            WHERE loan_id = %s 
            ORDER BY payment_month
        """, (loan_id,))
        
        payments = cursor.fetchall()
        
        # Convert payment dates to strings
        for payment in payments:
            if payment.get('payment_date') and not isinstance(payment['payment_date'], str):
                payment['payment_date'] = payment['payment_date'].strftime('%Y-%m-%d %H:%M:%S')
            if payment.get('due_date') and not isinstance(payment['due_date'], str):
                payment['due_date'] = payment['due_date'].strftime('%Y-%m-%d')
        
        # Generate future schedule
        future_schedule = []
        if loan['status'] != 'completed':
            monthly_rate = loan['interest_rate'] / 100 / 12
            balance = loan['loan_amount']
            
            # Calculate current outstanding balance
            for paid_month in range(loan['emi_paid']):
                interest = balance * monthly_rate
                principal = loan['emi_amount'] - interest
                balance -= principal
            
            for month in range(loan['emi_paid'] + 1, loan['loan_term_months'] + 1):
                interest = balance * monthly_rate
                principal = loan['emi_amount'] - interest
                
                # Calculate due date
                if month == loan['emi_paid'] + 1 and loan.get('next_due_date'):
                    if isinstance(loan['next_due_date'], str):
                        due_date = datetime.strptime(loan['next_due_date'], '%Y-%m-%d').date()
                    else:
                        due_date = loan['next_due_date']
                else:
                    # Calculate based on previous due date
                    prev_due = None
                    if month == loan['emi_paid'] + 1:
                        if isinstance(loan['next_due_date'], str):
                            prev_due = datetime.strptime(loan['next_due_date'], '%Y-%m-%d').date()
                        else:
                            prev_due = loan['next_due_date']
                    else:
                        prev_due = datetime.strptime(future_schedule[-1]['due_date'], '%Y-%m-%d').date()
                    
                    months_to_add = month - (loan['emi_paid'] + 1)
                    new_year = prev_due.year + (prev_due.month + months_to_add - 1) // 12
                    new_month = (prev_due.month + months_to_add - 1) % 12 + 1
                    new_day = min(prev_due.day, [31,29 if new_year % 4 == 0 else 28,31,30,31,30,31,31,30,31,30,31][new_month-1])
                    due_date = date(new_year, new_month, new_day)
                
                future_schedule.append({
                    'month': month,
                    'due_date': due_date.strftime('%Y-%m-%d'),
                    'emi_amount': loan['emi_amount'],
                    'principal': round(principal, 2),
                    'interest': round(interest, 2),
                    'outstanding': round(balance - principal, 2)
                })
                
                balance -= principal
        
        conn.close()
        
        return jsonify({
            'loan': loan,
            'paid_payments': payments,
            'future_schedule': future_schedule
        })
        
    except Exception as e:
        print(f"Error fetching loan schedule: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

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
        
        business_document = request.files.get('business_document')
        document_filename = None
        
        if business_document and business_document.filename:
            allowed_extensions = {'pdf', 'jpg', 'jpeg', 'png'}
            if '.' in business_document.filename and \
               business_document.filename.rsplit('.', 1)[1].lower() in allowed_extensions:
                
                filename = secure_filename(business_document.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                
                upload_folder = 'static/uploads/vendor_documents'
                if not os.path.exists(upload_folder):
                    os.makedirs(upload_folder)
                
                filepath = os.path.join(upload_folder, unique_filename)
                business_document.save(filepath)
                document_filename = unique_filename
            else:
                flash('Invalid file type. Please upload PDF, JPG, or PNG files.', 'error')
                return render_template('vendorreg.html')
        
        if password != confirm_password:
            flash('Passwords do not match!', 'error')
            return render_template('vendorreg.html')
        
        import re
        if not re.match(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*])(?=.{8,})', password):
            flash('Password must be at least 8 characters with uppercase, lowercase, number, and special character', 'error')
            return render_template('vendorreg.html')
        
        hashed_password = generate_password_hash(password)
        
        try:
            conn = get_vendors_db()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            cursor.execute("SELECT id FROM vendors WHERE email = %s", (email,))
            if cursor.fetchone():
                flash('Email address already registered!', 'error')
                return render_template('vendorreg.html')
            
            cursor.execute('''INSERT INTO vendors 
                         (business_name, contact_name, email, phone, service_type, 
                          password, description, business_document, document_verified)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                         (business_name, contact_name, email, phone, service_type, 
                          hashed_password, description, document_filename, 'pending'))
            
            conn.commit()
            conn.close()
            
            flash('Your vendor application has been submitted successfully! Our team will review it shortly.', 'success')
            return redirect(url_for('vendor_login'))
            
        except Exception as e:
            flash(f'An error occurred: {str(e)}', 'error')
            return render_template('vendorreg.html')
    
    return render_template('vendorreg.html')

@app.route("/farmerlogin", methods=["GET", "POST"])
def farmer_login():
    if request.method == "POST":
        email = request.form.get('email')
        password = request.form.get('password')

        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT id, full_name, email, phone, password, status FROM farmers WHERE email = %s", (email,))
        farmer = cursor.fetchone()
        conn.close()

        if farmer:
            if check_password_hash(farmer['password'], password):
                if farmer['status'] == 'approved':
                    session['user_id'] = farmer['id']
                    session['user_name'] = farmer['full_name']
                    session['user_email'] = farmer['email']
                    session['user_phone'] = farmer['phone']
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

        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT id, contact_name, email, password, status, business_name FROM vendors WHERE email = %s", (email,))
        vendor = cursor.fetchone()
        conn.close()

        if vendor and check_password_hash(vendor['password'], password):
            if vendor['status'] == 'approved':
                session['vendor_id'] = vendor['id']
                session['vendor_name'] = vendor['contact_name']
                session['vendor_email'] = vendor['email']
                session['business_name'] = vendor['business_name']
                session['user_type'] = 'vendor'
                
                print(f"✅ Vendor logged in: {vendor['contact_name']}")
                
                flash('Login successful!', 'success')
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
        contact_name = session.get('vendor_name')
        vendor_email = session.get('vendor_email')
        business_name = session.get('business_name')
        
        if not contact_name:
            conn = get_vendors_db()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT contact_name, email, business_name FROM vendors WHERE id = %s", (session['vendor_id'],))
            vendor = cursor.fetchone()
            conn.close()
            
            if vendor:
                contact_name = vendor['contact_name']
                vendor_email = vendor['email']
                business_name = vendor['business_name']
                session['vendor_name'] = contact_name
                session['vendor_email'] = vendor_email
                session['business_name'] = business_name
        
        print(f"✅ Rendering dashboard for: {contact_name}")
        
        return render_template("index.html", 
                             contact_name=contact_name, 
                             vendor_email=vendor_email,
                             business_name=business_name)
            
    except Exception as e:
        print(f"❌ Error in vendordashboard: {str(e)}")
        flash('Error loading dashboard', 'error')
        return redirect(url_for('vendor_login'))

# ================= ADMIN ROUTES ==================

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

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    session.pop('admin_name', None)
    session.pop('user_type', None)
    flash('Admin logged out successfully', 'success')
    return redirect(url_for('admin_login'))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))

# ================= DEBUG ROUTES ==================

@app.route('/debug_session')
def debug_session():
    return f"""
    <h3>Session Debug Info</h3>
    <p>Vendor ID: {session.get('vendor_id')}</p>
    <p>Contact Name: {session.get('vendor_name')}</p>
    <p>Vendor Email: {session.get('vendor_email')}</p>
    <p>User Type: {session.get('user_type')}</p>
    <hr>
    <p><a href="/vendordashboard">Go to Dashboard</a></p>
    """

@app.route('/debug_database')
def debug_database():
    if 'vendor_id' not in session:
        return "Not logged in"
    
    vendor_id = session['vendor_id']
    
    conn = get_vendors_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute("SELECT * FROM vendors WHERE id = %s", (vendor_id,))
    vendor_data = cursor.fetchone()
    
    conn.close()
    
    if vendor_data:
        return f"""
        <h3>Database Debug Info</h3>
        <p>Vendor ID: {vendor_data['id']}</p>
        <p>Business Name: {vendor_data['business_name']}</p>
        <p>Contact Name: {vendor_data['contact_name']}</p>
        <p>Email: {vendor_data['email']}</p>
        <p>Phone: {vendor_data['phone']}</p>
        <p>Service Type: {vendor_data['service_type']}</p>
        <hr>
        <p><a href="/vendordashboard">Go to Dashboard</a></p>
        """
    else:
        return "No vendor found in database"

@app.route('/debug_database_tables')
def debug_database_tables():
    """Check if equipment table exists and has data"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'equipment'
            )
        """)
        table_exists = cursor.fetchone()[0]
        
        result = f"<h3>Database Debug</h3>"
        
        if table_exists:
            cursor.execute("SELECT COUNT(*) FROM equipment")
            count = cursor.fetchone()[0]
            result += f"<p>Equipment table exists: YES</p>"
            result += f"<p>Total equipment records: {count}</p>"
            
            cursor.execute("SELECT id, name, vendor_email FROM equipment LIMIT 5")
            equipment = cursor.fetchall()
            result += f"<h4>Equipment Data (first 5):</h4>"
            for item in equipment:
                result += f"<p>ID: {item[0]}, Name: {item[1]}, Vendor: {item[2]}</p>"
        else:
            result += f"<p>Equipment table exists: NO</p>"
            
        conn.close()
        return result
        
    except Exception as e:
        return f"Error: {str(e)}"

@app.route('/debug-vendor-cancellations')
def debug_vendor_cancellations():
    """Debug vendor cancellation requests"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT id, order_id, order_type, equipment_name, user_name, 
                   cancellation_reason, status, requested_date
            FROM cancellation_requests 
            WHERE vendor_email = %s 
            ORDER BY requested_date DESC
        """, (vendor_email,))
        
        cancellations = cursor.fetchall()
        conn.close()
        
        cancellations_list = []
        for cancel in cancellations:
            cancellations_list.append({
                'cancellation_id': cancel['id'],
                'order_id': cancel['order_id'],
                'order_type': cancel['order_type'],
                'equipment_name': cancel['equipment_name'],
                'user_name': cancel['user_name'],
                'cancellation_reason': cancel['cancellation_reason'],
                'status': cancel['status'],
                'requested_date': cancel['requested_date']
            })
        
        return jsonify({
            'vendor_email': vendor_email,
            'cancellation_requests': cancellations_list,
            'total_requests': len(cancellations_list)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/check-cancellation-storage')
def check_cancellation_storage():
    """Check what's actually stored in cancellation_requests"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
                    'equipment': latest['equipment_name'],
                    'vendor_name': latest['vendor_name'],
                    'vendor_email': latest['vendor_email'],
                    'user_name': latest['user_name'],
                    'user_email': latest['user_email'],
                    'user_phone': latest['user_phone'],
                    'total_amount': latest['total_amount'],
                    'start_date': latest['start_date'],
                    'end_date': latest['end_date'],
                    'duration': latest['duration'],
                    'cancellation_reason': latest['cancellation_reason'],
                    'status': latest['status']
                }
            })
        else:
            return jsonify({'message': 'No cancellation requests found'})
            
    except Exception as e:
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

@app.route('/add-avg-rating-column')
def add_avg_rating_column():
    """Add avg_rating column to equipment table"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='equipment' AND column_name='avg_rating'
        """)
        
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE equipment ADD COLUMN avg_rating REAL DEFAULT 0")
            print("✅ Added avg_rating column to equipment table")
        
        conn.commit()
        conn.close()
        return "✅ avg_rating column added/verified successfully"
        
    except Exception as e:
        return f"❌ Error: {str(e)}"

# ================= API ENDPOINTS - USER ==================

@app.route('/api/user/orders')
def get_user_orders():
    """Get all orders (bookings and rent requests) for the logged-in user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        user_id = session['user_id']
        print(f"🔄 Fetching orders for user ID: {user_id}")
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get bookings
        cursor.execute("""
            SELECT 
                b.id, 
                'booking' as order_type,
                b.equipment_name,
                COALESCE(v.contact_name, b.vendor_name) as vendor_name,
                b.vendor_email,
                b.start_date,
                b.end_date,
                b.duration,
                b.total_amount,
                b.status,
                b.created_date,
                b.cancellation_requested_date,
                b.cancellation_reason,
                b.status_before_cancel,
                b.cancelled_date,
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
            WHERE b.user_id = %s
            ORDER BY b.created_date DESC
        """, (user_id,))
        
        bookings = cursor.fetchall()
        print(f"✅ Found {len(bookings)} bookings")
        
        # Get rent requests
        cursor.execute("""
            SELECT 
                rr.id,
                'rent' as order_type,
                rr.equipment_name,
                COALESCE(v.contact_name, rr.vendor_name) as vendor_name,
                rr.vendor_email,
                rr.start_date,
                rr.end_date,
                rr.duration,
                rr.total_amount,
                rr.status,
                rr.submitted_date as created_date,
                rr.cancellation_requested_date,
                rr.cancellation_reason,
                rr.status_before_cancel,
                rr.cancelled_date,
                e.image_url as equipment_image,
                rr.equipment_id,
                rr.user_name,
                rr.user_email,
                rr.user_phone,
                v.business_name,
                v.contact_name as vendor_contact
            FROM rent_requests rr
            LEFT JOIN vendors v ON rr.vendor_email = v.email
            LEFT JOIN equipment e ON rr.equipment_id = e.id
            WHERE rr.user_id = %s
            ORDER BY rr.submitted_date DESC
        """, (user_id,))
        
        rent_requests = cursor.fetchall()
        print(f"✅ Found {len(rent_requests)} rent requests")
        
        conn.close()
        
        orders_list = []
        
        for booking in bookings:
            vendor_name = booking['vendor_name'] or booking['vendor_contact'] or 'Vendor'
            
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
                'total_amount': float(booking['total_amount']) if booking['total_amount'] else 0,
                'status': booking['status'] or 'pending',
                'created_date': booking['created_date'],
                'cancellation_requested_date': booking['cancellation_requested_date'],
                'cancellation_reason': booking['cancellation_reason'],
                'status_before_cancel': booking['status_before_cancel'],
                'cancelled_date': booking['cancelled_date'],
                'equipment_image': booking['equipment_image'],
                'equipment_id': booking['equipment_id'],
                'user_name': booking['user_name'],
                'user_email': booking['user_email'],
                'user_phone': booking['user_phone'],
                'can_cancel': booking['status'] in ['pending', 'confirmed'] and booking['status'] != 'cancellation_requested',
                'is_cancellation_requested': booking['status'] == 'cancellation_requested'
            })
        
        for rent in rent_requests:
            vendor_name = rent['vendor_name'] or rent['vendor_contact'] or 'Vendor'
            
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
                'total_amount': float(rent['total_amount']) if rent['total_amount'] else 0,
                'status': rent['status'] or 'pending',
                'created_date': rent['created_date'],
                'cancellation_requested_date': rent['cancellation_requested_date'],
                'cancellation_reason': rent['cancellation_reason'],
                'status_before_cancel': rent['status_before_cancel'],
                'cancelled_date': rent['cancelled_date'],
                'equipment_image': rent['equipment_image'],
                'equipment_id': rent['equipment_id'],
                'user_name': rent['user_name'],
                'user_email': rent['user_email'],
                'user_phone': rent['user_phone'],
                'can_cancel': rent['status'] in ['pending', 'approved'] and rent['status'] != 'cancellation_requested',
                'is_cancellation_requested': rent['status'] == 'cancellation_requested'
            })
        
        orders_list.sort(key=lambda x: x['created_date'], reverse=True)
        
        print(f"📦 Total orders to return: {len(orders_list)}")
        return jsonify(orders_list)
        
    except Exception as e:
        print(f"❌ Error fetching user orders: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/order/<int:order_id>')
def get_order_details(order_id):
    try:
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': 'Not authenticated'}), 401
        
        order_type = request.args.get('type', 'booking')
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        if order_type == 'booking':
            cursor.execute("""
                SELECT b.*, e.image_url as equipment_image, 
                       COALESCE(v.contact_name, b.vendor_name) as vendor_name,
                       v.email as vendor_email, 
                       v.business_name,
                       b.user_name, b.user_email, b.user_phone
                FROM bookings b
                LEFT JOIN equipment e ON b.equipment_id = e.id
                LEFT JOIN vendors v ON b.vendor_email = v.email
                WHERE b.id = %s AND b.user_id = %s
            """, (order_id, user_id))
        else:
            cursor.execute("""
                SELECT rr.*, e.image_url as equipment_image, 
                       COALESCE(v.contact_name, rr.vendor_name) as vendor_name,
                       v.email as vendor_email,
                       v.business_name,
                       rr.user_name, rr.user_email, rr.user_phone
                FROM rent_requests rr
                LEFT JOIN equipment e ON rr.equipment_id = e.id
                LEFT JOIN vendors v ON rr.vendor_email = v.email
                WHERE rr.id = %s AND rr.user_id = %s
            """, (order_id, user_id))
        
        order = cursor.fetchone()
        conn.close()
        
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        if order_type == 'booking':
            order_details = {
                'id': order['id'],
                'order_type': 'booking',
                'equipment_name': order['equipment_name'],
                'vendor_name': order['vendor_name'],
                'vendor_email': order['vendor_email'],
                'business_name': order['business_name'],
                'start_date': order['start_date'],
                'end_date': order['end_date'],
                'duration': order['duration'],
                'total_amount': order['total_amount'],
                'status': order['status'] or 'pending',
                'notes': order['notes'],
                'created_date': order['created_date'],
                'cancellation_requested_date': order['cancellation_requested_date'],
                'cancellation_reason': order['cancellation_reason'],
                'equipment_image': order['equipment_image'],
                'user_name': order['user_name'],
                'user_email': order['user_email'],
                'user_phone': order['user_phone']
            }
        else:
            order_details = {
                'id': order['id'],
                'order_type': 'rent',
                'equipment_name': order['equipment_name'],
                'vendor_name': order['vendor_name'],
                'vendor_email': order['vendor_email'],
                'business_name': order['business_name'],
                'start_date': order['start_date'],
                'end_date': order['end_date'],
                'duration': order['duration'],
                'total_amount': order['total_amount'],
                'status': order['status'] or 'pending',
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

@app.route('/api/user/order/request-cancel', methods=['POST'])
def request_order_cancellation():
    """Request cancellation for an order"""
    print("📥 Received cancellation request")
    
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        print("📦 Request data:", data)
        
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400
            
        order_id = data.get('order_id')
        order_type = data.get('order_type')
        cancellation_reason = data.get('cancellation_reason', '')
        
        print(f"🔍 Processing: {order_type} #{order_id}, reason: {cancellation_reason}")
        
        if not order_id or not order_type:
            return jsonify({'error': 'Order ID and type are required'}), 400
        
        user_id = session['user_id']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
                    COALESCE(v.contact_name, b.vendor_name) as vendor_name
                FROM bookings b
                LEFT JOIN vendors v ON b.vendor_email = v.email
                WHERE b.id = %s AND b.user_id = %s
            """, (order_id, user_id))
        else:
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
                    COALESCE(v.contact_name, rr.vendor_name) as vendor_name
                FROM rent_requests rr
                LEFT JOIN vendors v ON rr.vendor_email = v.email
                WHERE rr.id = %s AND rr.user_id = %s
            """, (order_id, user_id))
        
        order = cursor.fetchone()
        
        if not order:
            conn.close()
            return jsonify({'error': 'Order not found or access denied'}), 404
        
        equipment_id = order['equipment_id']
        equipment_name = order['equipment_name']
        status = order['status']
        total_amount = order['total_amount']
        created_date = order['created_date']
        start_date = order['start_date']
        end_date = order['end_date']
        duration = order['duration']
        vendor_email = order['vendor_email']
        user_name = order['user_name']
        user_email = order['user_email']
        user_phone = order['user_phone']
        vendor_name = order['vendor_name']
        
        if order_type == 'booking':
            cursor.execute("""
                UPDATE bookings 
                SET status = 'cancellation_requested',
                    cancellation_requested_date = CURRENT_TIMESTAMP,
                    cancellation_reason = %s,
                    status_before_cancel = %s
                WHERE id = %s AND user_id = %s
            """, (cancellation_reason, status, order_id, user_id))
        else:
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'cancellation_requested',
                    cancellation_requested_date = CURRENT_TIMESTAMP,
                    cancellation_reason = %s,
                    status_before_cancel = %s
                WHERE id = %s AND user_id = %s
            """, (cancellation_reason, status, order_id, user_id))
        
        cursor.execute("""
            INSERT INTO cancellation_requests 
            (order_id, order_type, user_id, user_name, user_email, user_phone,
             vendor_email, vendor_name, equipment_id, equipment_name, total_amount, 
             start_date, end_date, duration, order_status_before_cancel, 
             order_created_date, cancellation_reason, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
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
        
        conn.commit()
        conn.close()
        
        print(f"✅ Cancellation request stored successfully")
        
        return jsonify({
            'success': True,
            'message': 'Cancellation request submitted successfully! Waiting for vendor approval.'
        })
        
    except Exception as e:
        print(f"❌ Error in cancellation request: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/user/order/cancel', methods=['POST'])
def cancel_user_order():
    """Cancel a user order (booking or rent request) and restock equipment"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        order_type = data.get('order_type')
        order_id = data.get('order_id')
        cancellation_reason = data.get('cancellation_reason', '')
        
        if not order_type or not order_id:
            return jsonify({'error': 'Missing order type or ID'}), 400
        
        user_id = session['user_id']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        if order_type == 'booking':
            cursor.execute("""
                SELECT equipment_id, status FROM bookings 
                WHERE id = %s AND user_id = %s
            """, (order_id, user_id))
            
            booking = cursor.fetchone()
            
            if not booking:
                conn.close()
                return jsonify({'error': 'Booking not found'}), 404
            
            equipment_id = booking['equipment_id']
            current_status = booking['status']
            
            if current_status not in ['pending', 'confirmed']:
                conn.close()
                return jsonify({'error': 'Cannot cancel booking with current status'}), 400
            
            cursor.execute("""
                UPDATE bookings 
                SET status = 'cancelled', 
                    cancelled_date = CURRENT_TIMESTAMP,
                    cancellation_reason = %s
                WHERE id = %s AND user_id = %s
            """, (cancellation_reason, order_id, user_id))
            
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 > 0 THEN 'available' 
                        ELSE status 
                    END
                WHERE id = %s
            """, (equipment_id,))
            
        elif order_type == 'rent':
            cursor.execute("""
                SELECT equipment_id, status FROM rent_requests 
                WHERE id = %s AND user_id = %s
            """, (order_id, user_id))
            
            rent_request = cursor.fetchone()
            
            if not rent_request:
                conn.close()
                return jsonify({'error': 'Rent request not found'}), 404
            
            equipment_id = rent_request['equipment_id']
            current_status = rent_request['status']
            
            if current_status not in ['pending', 'approved']:
                conn.close()
                return jsonify({'error': 'Cannot cancel rent request with current status'}), 400
            
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'cancelled', 
                    cancelled_date = CURRENT_TIMESTAMP,
                    cancellation_reason = %s
                WHERE id = %s AND user_id = %s
            """, (cancellation_reason, order_id, user_id))
            
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 > 0 THEN 'available' 
                        ELSE status 
                    END
                WHERE id = %s
            """, (equipment_id,))
        
        else:
            conn.close()
            return jsonify({'error': 'Invalid order type'}), 400
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'{order_type.capitalize()} cancelled successfully'
        })
        
    except Exception as e:
        print(f"Error cancelling order: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/booking/<int:booking_id>/request-cancel', methods=['POST'])
def request_booking_cancellation(booking_id):
    """Request cancellation for a specific booking"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        cancellation_reason = data.get('cancellation_reason', 'No reason provided')
        
        user_id = session['user_id']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT status FROM bookings 
            WHERE id = %s AND user_id = %s
        """, (booking_id, user_id))
        
        booking = cursor.fetchone()
        
        if not booking:
            conn.close()
            return jsonify({'error': 'Booking not found'}), 404
        
        current_status = booking['status']
        
        if current_status not in ['pending', 'confirmed']:
            conn.close()
            return jsonify({'error': 'Cannot cancel booking with current status'}), 400
        
        cursor.execute("""
            UPDATE bookings 
            SET status = 'cancellation_requested', 
                cancellation_requested_date = CURRENT_TIMESTAMP,
                cancellation_reason = %s,
                status_before_cancel = %s
            WHERE id = %s AND user_id = %s
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
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT status FROM rent_requests 
            WHERE id = %s AND user_id = %s
        """, (request_id, user_id))
        
        rent_request = cursor.fetchone()
        
        if not rent_request:
            conn.close()
            return jsonify({'error': 'Rent request not found'}), 404
        
        current_status = rent_request['status']
        
        if current_status not in ['pending', 'approved']:
            conn.close()
            return jsonify({'error': 'Cannot cancel rent request with current status'}), 400
        
        cursor.execute("""
            UPDATE rent_requests 
            SET status = 'cancellation_requested', 
                cancellation_requested_date = CURRENT_TIMESTAMP,
                cancellation_reason = %s,
                status_before_cancel = %s
            WHERE id = %s AND user_id = %s
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

@app.route('/api/user/booking/<int:booking_id>')
def get_user_booking_detail(booking_id):
    """Get booking details for review"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT b.*, e.id as equipment_id, e.name as equipment_name, e.vendor_email
            FROM bookings b
            JOIN equipment e ON b.equipment_id = e.id
            WHERE b.id = %s AND b.user_id = %s
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

@app.route('/api/user/rent-requests')
def get_user_rent_requests():
    """Get rent requests for the logged-in user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT rr.*, v.business_name as vendor_name
            FROM rent_requests rr
            JOIN vendors v ON rr.vendor_email = v.email
            WHERE rr.user_id = %s
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
                'status': row['status'] or 'pending',
                'submitted_date': row['submitted_date']
            })
        
        return jsonify(requests_list)
        
    except Exception as e:
        print(f"Error fetching user rent requests: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/bookings')
def get_user_bookings():
    """Get all bookings for the logged-in user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT * FROM bookings 
            WHERE user_id = %s 
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
                'total_amount': float(booking['total_amount']) if booking['total_amount'] else 0,
                'status': booking['status'] or 'pending',
                'notes': booking['notes'],
                'created_date': booking['created_date']
            })
        
        return jsonify(bookings_list)
        
    except Exception as e:
        print(f"Error fetching user bookings: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/stats')
def get_user_stats():
    """Get statistics for user dashboard"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        user_id = session['user_id']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT COUNT(*) as count FROM bookings 
            WHERE user_id = %s AND status IN ('pending', 'confirmed', 'active')
        """, (user_id,))
        result = cursor.fetchone()
        active_bookings = result['count'] if result else 0
        
        cursor.execute("""
            SELECT COUNT(*) as count FROM bookings 
            WHERE user_id = %s AND status IN ('completed', 'cancelled')
        """, (user_id,))
        result = cursor.fetchone()
        past_bookings = result['count'] if result else 0
        
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status IN ('pending', 'approved') THEN 1 ELSE 0 END) as active_rents,
                SUM(CASE WHEN status IN ('completed', 'cancelled', 'rejected') THEN 1 ELSE 0 END) as past_rents
            FROM rent_requests 
            WHERE user_id = %s
        """, (user_id,))
        rent_counts = cursor.fetchone()
        
        active_rents = rent_counts['active_rents'] or 0 if rent_counts else 0
        past_rents = rent_counts['past_rents'] or 0 if rent_counts else 0
        
        cursor.execute("""
            SELECT COUNT(*) as count FROM reviews 
            WHERE user_id = %s
        """, (user_id,))
        result = cursor.fetchone()
        reviews_written = result['count'] if result else 0
        
        conn.close()
        
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

# ================= REVIEW SYSTEM ==================

@app.route('/api/user/completed-orders')
def get_user_completed_orders():
    """Get completed bookings AND rent requests for review writing"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        user_id = session['user_id']
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        print(f"🔍 Fetching completed orders for user {user_id}")
        
        cursor.execute("""
            SELECT 
                b.id as order_id,
                'booking' as order_type,
                b.equipment_id,
                b.equipment_name,
                b.vendor_email,
                COALESCE(v.contact_name, b.vendor_name) as vendor_name,
                b.created_date,
                b.total_amount,
                e.image_url as equipment_image
            FROM bookings b
            LEFT JOIN equipment e ON b.equipment_id = e.id
            LEFT JOIN vendors v ON b.vendor_email = v.email
            WHERE b.user_id = %s 
            AND b.status IN ('completed', 'confirmed')
            AND NOT EXISTS (
                SELECT 1 FROM reviews r 
                WHERE r.order_id = b.id 
                AND r.order_type = 'booking' 
                AND r.user_id = %s
            )
            
            UNION ALL
            
            SELECT 
                rr.id as order_id,
                'rent' as order_type,
                rr.equipment_id,
                rr.equipment_name,
                rr.vendor_email,
                COALESCE(v.contact_name, rr.vendor_name) as vendor_name,
                rr.submitted_date as created_date,
                rr.total_amount,
                e.image_url as equipment_image
            FROM rent_requests rr
            LEFT JOIN equipment e ON rr.equipment_id = e.id
            LEFT JOIN vendors v ON rr.vendor_email = v.email
            WHERE rr.user_id = %s 
            AND rr.status IN ('completed', 'approved')
            AND NOT EXISTS (
                SELECT 1 FROM reviews r 
                WHERE r.order_id = rr.id 
                AND r.order_type = 'rent' 
                AND r.user_id = %s
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
        
        print(f"✅ Found {len(orders_list)} completed orders")
        return jsonify(orders_list)
        
    except Exception as e:
        print(f"❌ Error fetching completed orders: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/reviews')
def get_user_reviews():
    """Get reviews written by the user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        user_id = session['user_id']
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT r.*, e.image_url as equipment_image
            FROM reviews r
            LEFT JOIN equipment e ON r.equipment_id = e.id
            WHERE r.user_id = %s
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
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT id FROM reviews 
            WHERE user_id = %s AND order_id = %s AND order_type = %s
        """, (user_id, data['order_id'], data['order_type']))
        
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'You have already reviewed this order'}), 400
        
        cursor.execute("""
            INSERT INTO reviews 
            (user_id, user_name, equipment_id, equipment_name, vendor_email, 
             vendor_name, order_type, order_id, rating, title, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        
        try:
            cursor.execute("""
                UPDATE equipment 
                SET avg_rating = (
                    SELECT COALESCE(AVG(rating), 0) FROM reviews 
                    WHERE equipment_id = %s
                )
                WHERE id = %s
            """, (data['equipment_id'], data['equipment_id']))
            print(f"✅ Updated avg_rating for equipment #{data['equipment_id']}")
        except Exception as e:
            print(f"⚠️ Could not update avg_rating: {e}")
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Review submitted successfully!'
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
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT equipment_id FROM reviews 
            WHERE id = %s AND user_id = %s
        """, (review_id, user_id))
        
        review = cursor.fetchone()
        
        if not review:
            conn.close()
            return jsonify({'error': 'Review not found or access denied'}), 404
        
        equipment_id = review['equipment_id']
        
        cursor.execute("DELETE FROM reviews WHERE id = %s", (review_id,))
        
        try:
            cursor.execute("""
                UPDATE equipment 
                SET avg_rating = (
                    SELECT COALESCE(AVG(rating), 0) FROM reviews 
                    WHERE equipment_id = %s
                )
                WHERE id = %s
            """, (equipment_id, equipment_id))
        except Exception as e:
            print(f"⚠️ Could not update avg_rating: {e}")
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Review deleted successfully'
        })
        
    except Exception as e:
        print(f"❌ Error deleting review: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/completed-bookings')
def get_user_completed_bookings():
    """Get completed bookings for review writing"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT b.*, e.name as equipment_name, v.business_name as vendor_name
            FROM bookings b
            JOIN equipment e ON b.equipment_id = e.id
            JOIN vendors v ON b.vendor_email = v.email
            WHERE b.user_id = %s AND b.status = 'completed'
            AND NOT EXISTS (
                SELECT 1 FROM reviews r 
                WHERE r.order_id = b.id AND r.order_type = 'booking' AND r.user_id = %s
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

@app.route('/api/equipment/<int:equipment_id>/reviews')
def get_equipment_reviews(equipment_id):
    """Get reviews for a specific equipment"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT r.*, f.full_name as user_name 
            FROM reviews r
            LEFT JOIN farmers f ON r.user_id = f.id
            WHERE r.equipment_id = %s
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

# ================= VENDOR API ENDPOINTS ==================

@app.route('/api/vendor/cancellation-requests')
def get_vendor_cancellation_requests():
    """Get all cancellation requests for vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
            WHERE cr.vendor_email = %s
            ORDER BY cr.requested_date DESC
        """, (vendor_email,))
        
        cancellation_requests = cursor.fetchall()
        conn.close()
        
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
        
        print(f"📊 Returning {len(requests_list)} cancellation requests")
        return jsonify(requests_list)
        
    except Exception as e:
        print(f"❌ Error fetching vendor cancellation requests: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vendor/cancellation-request/approve', methods=['POST'])
def approve_cancellation_request():
    """Vendor approves a cancellation request"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        cancellation_id = data.get('cancellation_id')
        
        if not cancellation_id:
            return jsonify({'error': 'Missing cancellation ID'}), 400
        
        vendor_email = session['vendor_email']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT order_id, order_type, equipment_id, user_phone, user_name, equipment_name
            FROM cancellation_requests 
            WHERE id = %s AND vendor_email = %s AND status = 'pending'
        """, (cancellation_id, vendor_email))
        
        cancellation_request = cursor.fetchone()
        
        if not cancellation_request:
            conn.close()
            return jsonify({'error': 'Cancellation request not found or access denied'}), 404
        
        order_id = cancellation_request['order_id']
        order_type = cancellation_request['order_type']
        equipment_id = cancellation_request['equipment_id']
        user_phone = cancellation_request['user_phone']
        user_name = cancellation_request['user_name']
        equipment_name = cancellation_request['equipment_name']
        
        if order_type == 'booking':
            cursor.execute("""
                UPDATE bookings 
                SET status = 'cancelled'
                WHERE id = %s AND vendor_email = %s
            """, (order_id, vendor_email))
            
        elif order_type == 'rent':
            cursor.execute("""
                UPDATE rent_requests 
                SET status = 'cancelled'
                WHERE id = %s AND vendor_email = %s
            """, (order_id, vendor_email))
        
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = stock_quantity + 1,
                status = CASE 
                    WHEN stock_quantity + 1 > 0 THEN 'available' 
                    ELSE status 
                END
            WHERE id = %s
        """, (equipment_id,))
        
        cursor.execute("""
            UPDATE cancellation_requests 
            SET status = 'approved', 
                processed_date = CURRENT_TIMESTAMP,
                processed_by = 'vendor'
            WHERE id = %s
        """, (cancellation_id,))
        
        conn.commit()
        conn.close()
        
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
    """Vendor rejects a cancellation request"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        cancellation_id = data.get('cancellation_id')
        
        if not cancellation_id:
            return jsonify({'error': 'Missing cancellation ID'}), 400
        
        vendor_email = session['vendor_email']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT order_id, order_type, user_phone, user_name, equipment_name, order_status_before_cancel
            FROM cancellation_requests 
            WHERE id = %s AND vendor_email = %s AND status = 'pending'
        """, (cancellation_id, vendor_email))
        
        cancellation_request = cursor.fetchone()
        
        if not cancellation_request:
            conn.close()
            return jsonify({'error': 'Cancellation request not found'}), 404
        
        order_id = cancellation_request['order_id']
        order_type = cancellation_request['order_type']
        user_phone = cancellation_request['user_phone']
        user_name = cancellation_request['user_name']
        equipment_name = cancellation_request['equipment_name']
        previous_status = cancellation_request['order_status_before_cancel']
        
        if order_type == 'booking':
            cursor.execute("""
                UPDATE bookings 
                SET status = %s 
                WHERE id = %s AND vendor_email = %s
            """, (previous_status, order_id, vendor_email))
            
        elif order_type == 'rent':
            cursor.execute("""
                UPDATE rent_requests 
                SET status = %s 
                WHERE id = %s AND vendor_email = %s
            """, (previous_status, order_id, vendor_email))
        
        cursor.execute("""
            UPDATE cancellation_requests 
            SET status = 'rejected', 
                processed_date = CURRENT_TIMESTAMP,
                processed_by = 'vendor'
            WHERE id = %s
        """, (cancellation_id,))
        
        conn.commit()
        conn.close()
        
        sms_message = f"Your cancellation request for {equipment_name} has been rejected. Order remains active. - Lend A Hand"
        send_sms(user_phone, sms_message)
        
        return jsonify({
            'success': True,
            'message': 'Cancellation rejected'
        })
        
    except Exception as e:
        print(f"❌ Error rejecting cancellation: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vendor/cancellation-requests/details')
def get_vendor_cancellation_requests_details():
    """Get complete cancellation request details for vendor dashboard"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        status_filter = request.args.get('status', 'pending')
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        query = """
            SELECT * FROM cancellation_requests 
            WHERE vendor_email = %s
        """
        params = [vendor_email]
        
        if status_filter != 'all':
            query += " AND status = %s"
            params.append(status_filter)
        
        query += " ORDER BY requested_date DESC"
        
        cursor.execute(query, params)
        cancellation_requests = cursor.fetchall()
        conn.close()
        
        requests_list = []
        for req in cancellation_requests:
            requests_list.append({
                'cancellation_id': req['id'],
                'order_type': req['order_type'],
                'order_id': req['order_id'],
                'requested_date': req['requested_date'],
                'cancellation_reason': req['cancellation_reason'],
                'status': req['status'],
                'user_name': req['user_name'],
                'user_email': req['user_email'],
                'user_phone': req['user_phone'],
                'user_location': req['user_location'],
                'user_id': req['user_id'],
                'vendor_name': req['vendor_name'],
                'vendor_business_name': req['vendor_business_name'],
                'vendor_contact_phone': req['vendor_contact_phone'],
                'equipment_name': req['equipment_name'],
                'equipment_category': req['equipment_category'],
                'equipment_description': req['equipment_description'],
                'equipment_price': req['equipment_price'],
                'equipment_price_unit': req['equipment_price_unit'],
                'equipment_location': req['equipment_location'],
                'equipment_image_url': req['equipment_image_url'],
                'total_amount': req['total_amount'],
                'start_date': req['start_date'],
                'end_date': req['end_date'],
                'duration': req['duration'],
                'order_notes': req['order_notes'],
                'purpose': req['purpose'],
                'order_status_before_cancel': req['order_status_before_cancel'],
                'order_created_date': req['order_created_date'],
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

@app.route('/api/vendor/rent-requests')
def get_vendor_rent_requests():
    """Get rent requests for vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        status_filter = request.args.get('status', 'all')
        vendor_email = session['vendor_email']
        
        print(f"🔄 Fetching rent requests for vendor: {vendor_email}, filter: {status_filter}")
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        if status_filter == 'all':
            cursor.execute("""
                SELECT rr.*, e.name as equipment_name
                FROM rent_requests rr
                JOIN equipment e ON rr.equipment_id = e.id
                WHERE rr.vendor_email = %s
                ORDER BY rr.submitted_date DESC
            """, (vendor_email,))
        else:
            cursor.execute("""
                SELECT rr.*, e.name as equipment_name
                FROM rent_requests rr
                JOIN equipment e ON rr.equipment_id = e.id
                WHERE rr.vendor_email = %s AND rr.status = %s
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
                'status': row['status'] or 'pending',
                'submitted_date': row['submitted_date'],
                'processed_date': row['processed_date']
            })
        
        print(f"✅ Found {len(requests_list)} rent requests for vendor")
        return jsonify(requests_list)
        
    except Exception as e:
        print(f"❌ Error fetching rent requests: {str(e)}")
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
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        print(f"🔄 Updating rent request #{request_id} to status: {new_status}")
        
        cursor.execute("""
            SELECT 
                rr.user_phone, rr.user_name, rr.equipment_name, rr.total_amount, rr.duration,
                rr.equipment_id, e.stock_quantity, e.min_stock_threshold,
                v.contact_name as vendor_name, v.phone as vendor_phone
            FROM rent_requests rr
            JOIN equipment e ON rr.equipment_id = e.id
            JOIN vendors v ON rr.vendor_email = v.email
            WHERE rr.id = %s AND rr.vendor_email = %s
        """, (request_id, vendor_email))
        
        request_data = cursor.fetchone()
        
        if not request_data:
            conn.close()
            return jsonify({'error': 'Rent request not found or access denied'}), 404
        
        user_phone = request_data['user_phone']
        user_name = request_data['user_name']
        equipment_name = request_data['equipment_name']
        total_amount = request_data['total_amount']
        duration = request_data['duration']
        equipment_id = request_data['equipment_id']
        current_stock = request_data['stock_quantity']
        min_stock_threshold = request_data['min_stock_threshold'] or 5
        vendor_name = request_data['vendor_name']
        vendor_phone = request_data['vendor_phone']
        
        print(f"📱 Notification details: User={user_name}, Phone={user_phone}, Equipment={equipment_name}")
        print(f"📦 Current stock: {current_stock}")
        
        cursor.execute("""
            UPDATE rent_requests 
            SET status = %s, processed_date = CURRENT_TIMESTAMP 
            WHERE id = %s AND vendor_email = %s
        """, (new_status, request_id, vendor_email))
        
        if new_status == 'rejected':
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 <= %s THEN 'low_stock'
                        WHEN stock_quantity + 1 > 0 THEN 'available'
                        ELSE status
                    END
                WHERE id = %s
            """, (min_stock_threshold, equipment_id))
            print(f"✅ Rejected rent: Restocked equipment (+1)")
            
        elif new_status == 'completed':
            cursor.execute("""
                UPDATE equipment 
                SET stock_quantity = stock_quantity + 1,
                    status = CASE 
                        WHEN stock_quantity + 1 <= %s THEN 'low_stock'
                        WHEN stock_quantity + 1 > 0 THEN 'available'
                        ELSE status
                    END
                WHERE id = %s
            """, (min_stock_threshold, equipment_id))
            print(f"✅ Completed rent: Restocked equipment (+1)")
        
        conn.commit()
        conn.close()
        
        if new_status == 'approved':
            message = f"🎉 Dear {user_name}, your rent request for {equipment_name} has been APPROVED by {vendor_name}! Total amount: ₹{total_amount} for {duration} days. Please contact vendor at {vendor_phone} for pickup details. - Lend A Hand"
        elif new_status == 'rejected':
            message = f"❌ Dear {user_name}, your rent request for {equipment_name} has been REJECTED by {vendor_name}. Please contact support if you have questions. - Lend A Hand"
        elif new_status == 'completed':
            message = f"✅ Dear {user_name}, your rental period for {equipment_name} has been COMPLETED. Equipment has been returned. Thank you for using Lend A Hand! - Lend A Hand"
        
        print(f"📤 Sending SMS to {user_phone}: {message[:50]}...")
        sms_result = send_sms(user_phone, message)
        
        if sms_result.get('success'):
            print(f"✅ SMS sent successfully to user {user_name}")
        else:
            print(f"⚠️ SMS sending failed: {sms_result.get('error', 'Unknown error')}")
        
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

@app.route('/api/vendor/rent-request/<int:request_id>/return', methods=['POST'])
def mark_equipment_returned(request_id):
    """Mark equipment as returned by farmer and await vendor approval"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            UPDATE rent_requests 
            SET status = 'returned', processed_date = CURRENT_TIMESTAMP 
            WHERE id = %s AND vendor_email = %s
        """, (request_id, session['vendor_email']))
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': 'Rent request not found or access denied'}), 404
        
        cursor.execute("""
            SELECT user_phone, user_name, equipment_name 
            FROM rent_requests WHERE id = %s
        """, (request_id,))
        
        request_data = cursor.fetchone()
        conn.commit()
        conn.close()
        
        if request_data:
            user_phone = request_data['user_phone']
            user_name = request_data['user_name']
            equipment_name = request_data['equipment_name']
            message = f"Dear {user_name}, your return request for {equipment_name} has been submitted. Waiting for vendor approval."
            send_sms(user_phone, message)
        
        return jsonify({
            'success': True, 
            'message': 'Equipment return submitted for vendor approval'
        })
        
    except Exception as e:
        print(f"Error marking equipment returned: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vendor/rent-request/<int:request_id>/complete', methods=['POST'])
def complete_rent_request(request_id):
    """Vendor approves the return and marks request as completed"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT equipment_id, user_phone, user_name, equipment_name 
            FROM rent_requests WHERE id = %s AND vendor_email = %s
        """, (request_id, session['vendor_email']))
        
        request_data = cursor.fetchone()
        
        if not request_data:
            conn.close()
            return jsonify({'error': 'Rent request not found or access denied'}), 404
        
        equipment_id = request_data['equipment_id']
        user_phone = request_data['user_phone']
        user_name = request_data['user_name']
        equipment_name = request_data['equipment_name']
        
        cursor.execute("""
            UPDATE rent_requests 
            SET status = 'completed', processed_date = CURRENT_TIMESTAMP 
            WHERE id = %s AND vendor_email = %s
        """, (request_id, session['vendor_email']))
        
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = stock_quantity + 1,
                status = CASE 
                    WHEN stock_quantity + 1 > 0 THEN 'available' 
                    ELSE status 
                END
            WHERE id = %s
        """, (equipment_id,))
        
        conn.commit()
        conn.close()
        
        completion_message = f"Thank you {user_name}! Your equipment {equipment_name} has been successfully returned and approved. We appreciate your business! - Lend A Hand"
        send_sms(user_phone, completion_message)
        
        return jsonify({
            'success': True, 
            'message': 'Rent request completed successfully'
        })
        
    except Exception as e:
        print(f"Error completing rent request: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vendor/reviews')
def get_vendor_reviews():
    """Get all reviews for the logged-in vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        vendor_email = session['vendor_email']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT 
                r.*,
                e.image_url as equipment_image,
                e.category as equipment_category
            FROM reviews r
            LEFT JOIN equipment e ON r.equipment_id = e.id
            WHERE r.vendor_email = %s
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

@app.route('/api/vendor/bookings')
def get_vendor_bookings():
    """Get all bookings for the logged-in vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        status_filter = request.args.get('status', 'all')
        vendor_email = session['vendor_email']
        
        print(f"🔄 Fetching bookings for vendor: {vendor_email}, filter: {status_filter}")
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        if status_filter == 'all':
            cursor.execute("""
                SELECT * FROM bookings 
                WHERE vendor_email = %s
                ORDER BY created_date DESC
            """, (vendor_email,))
        else:
            cursor.execute("""
                SELECT * FROM bookings 
                WHERE vendor_email = %s AND status = %s
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
                'status': booking['status'] or 'pending',
                'notes': booking['notes'],
                'created_date': booking['created_date']
            })
        
        print(f"✅ Found {len(bookings_list)} bookings for vendor")
        return jsonify(bookings_list)
        
    except Exception as e:
        print(f"❌ Error fetching vendor bookings: {str(e)}")
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
        
        vendor_email = session['vendor_email']
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            UPDATE bookings 
            SET status = %s, processed_date = CURRENT_TIMESTAMP 
            WHERE id = %s AND vendor_email = %s
        """, (new_status, booking_id, vendor_email))
        
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({'error': 'Booking not found or access denied'}), 404
        
        if new_status in ['rejected', 'completed']:
            cursor.execute("""
                UPDATE equipment 
                SET status = 'available' 
                WHERE id = (
                    SELECT equipment_id FROM bookings WHERE id = %s
                )
            """, (booking_id,))
        
        cursor.execute("""
            SELECT user_phone, user_name, equipment_name, total_amount 
            FROM bookings WHERE id = %s
        """, (booking_id,))
        
        booking_data = cursor.fetchone()
        
        conn.commit()
        conn.close()
        
        if booking_data:
            user_phone = booking_data['user_phone']
            user_name = booking_data['user_name']
            equipment_name = booking_data['equipment_name']
            total_amount = booking_data['total_amount']
            
            if new_status == 'confirmed':
                message = f"Dear {user_name}, your booking for {equipment_name} has been confirmed! Total amount: ₹{total_amount}."
            elif new_status == 'rejected':
                message = f"Dear {user_name}, your booking for {equipment_name} has been rejected."
            elif new_status == 'completed':
                message = f"Dear {user_name}, your booking period for {equipment_name} has been completed. Thank you for using Lend A Hand!"
            
            send_sms(user_phone, message)
        
        return jsonify({
            'success': True, 
            'message': f'Booking {new_status} successfully'
        })
        
    except Exception as e:
        print(f"Error updating booking: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/vendor/equipment')
def get_vendor_equipment():
    """Get all equipment for the logged-in vendor"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT * FROM equipment 
            WHERE vendor_email = %s 
            ORDER BY created_date DESC
        """, (session['vendor_email'],))
        
        equipment = cursor.fetchall()
        conn.close()
        
        equipment_list = []
        for item in equipment:
            stock_quantity = item['stock_quantity'] or 0
            min_stock_threshold = item['min_stock_threshold'] or 5
            current_status = item['status']
            
            stock_status = 'available'
            if stock_quantity <= 0:
                stock_status = 'out_of_stock'
            elif stock_quantity <= min_stock_threshold:
                stock_status = 'low_stock'
            
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
                'price': price,
                'price_unit': price_unit,
                'rental_price': item['rental_price'],
                'rental_price_unit': item['rental_price_unit'],
                'purchase_price': item['purchase_price'],
                'purchase_unit': item['purchase_unit'],
                'equipment_type': item['equipment_type'],
                'location': item['location'],
                'image_url': item['image_url'],
                'status': stock_status,
                'raw_status': current_status,
                'stock_quantity': stock_quantity,
                'min_stock_threshold': item['min_stock_threshold'],
                'created_date': item['created_date']
            }
            equipment_list.append(equipment_data)
        
        return jsonify(equipment_list)
        
    except Exception as e:
        print(f"❌ Error fetching vendor equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ================= EQUIPMENT API ==================

@app.route('/api/equipment')
def get_equipment_for_users():
    """Get all available equipment for users"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
            stock_quantity = item['stock_quantity'] or 1
            min_stock_threshold = item['min_stock_threshold'] or 5
            
            stock_status = 'available'
            if stock_quantity <= 0:
                stock_status = 'out_of_stock'
            elif stock_quantity <= min_stock_threshold:
                stock_status = 'low_stock'
            
            image_url = item['image_url']
            if image_url and not image_url.startswith('http') and not image_url.startswith('/static/uploads/equipment/'):
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
                'status': stock_status,
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
        print(f"❌ Error fetching equipment: {str(e)}")
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
        
        name = request.form.get('name')
        category = request.form.get('category')
        description = request.form.get('description', '')
        rental_price = request.form.get('rental_price')
        rental_price_unit = request.form.get('rental_price_unit', 'day')
        purchase_price = request.form.get('purchase_price')
        purchase_unit = request.form.get('purchase_unit', 'unit')
        equipment_type = request.form.get('equipment_type', 'both')
        location = request.form.get('location')
        status = request.form.get('status', 'available')
        stock_quantity = request.form.get('stock_quantity')
        min_stock_threshold = request.form.get('min_stock_threshold')
        
        print(f"📦 Received form data - rental_price: {rental_price}, purchase_price: {purchase_price}, equipment_type: {equipment_type}")
        
        if not all([name, category, location, stock_quantity, min_stock_threshold, equipment_type]):
            return jsonify({'error': 'Missing required fields'}), 400
        
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
        
        image_url = None
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename != '':
                image_url = save_uploaded_image(image_file)
                print(f"🖼️ Image saved: {image_url}")
        
        price = 0
        price_unit = ''
        if equipment_type in ['both', 'rental_only']:
            price = rental_price
            price_unit = rental_price_unit
        elif equipment_type == 'purchase_only':
            price = purchase_price
            price_unit = purchase_unit
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            INSERT INTO equipment 
            (vendor_email, name, category, description, price, price_unit,
             rental_price, rental_price_unit, purchase_price, purchase_unit, equipment_type,
             location, status, image_url, stock_quantity, min_stock_threshold)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        conn.close()
        
        print(f"✅ Equipment added successfully")
        print(f"💰 Pricing: Rental=₹{rental_price}/{rental_price_unit}, Purchase=₹{purchase_price}/{purchase_unit}")
        print(f"📦 Type: {equipment_type}")
        
        return jsonify({
            'success': True,
            'message': 'Equipment added successfully'
        })
        
    except Exception as e:
        print(f"❌ Error adding equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/equipment/update/<int:equipment_id>', methods=['POST'])
def update_equipment(equipment_id):
    """Update equipment details with separate pricing"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        print(f"🔄 Updating equipment ID: {equipment_id}")
        
        name = request.form.get('name')
        category = request.form.get('category')
        description = request.form.get('description', '')
        rental_price = request.form.get('rental_price')
        rental_price_unit = request.form.get('rental_price_unit', 'day')
        purchase_price = request.form.get('purchase_price')
        purchase_unit = request.form.get('purchase_unit', 'unit')
        equipment_type = request.form.get('equipment_type', 'both')
        location = request.form.get('location')
        status = request.form.get('status', 'available')
        stock_quantity = request.form.get('stock_quantity')
        min_stock_threshold = request.form.get('min_stock_threshold')
        
        print(f"📦 Received update data - equipment_type: {equipment_type}, rental_price: {rental_price}, purchase_price: {purchase_price}")
        
        if not all([name, category, location, stock_quantity, min_stock_threshold, equipment_type]):
            return jsonify({'error': 'Missing required fields'}), 400
        
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
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT image_url FROM equipment WHERE id = %s AND vendor_email = %s", 
                      (equipment_id, session['vendor_email']))
        current_equipment = cursor.fetchone()
        
        if not current_equipment:
            conn.close()
            return jsonify({'error': 'Equipment not found or access denied'}), 404
        
        current_image_url = current_equipment['image_url']
        
        image_url = current_image_url
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename:
                new_image_url = save_uploaded_image(image_file)
                if new_image_url:
                    image_url = new_image_url
                    print(f"🖼️ New image saved: {image_url}")
        
        price = 0
        price_unit = ''
        if equipment_type in ['both', 'rental_only']:
            price = rental_price
            price_unit = rental_price_unit
        elif equipment_type == 'purchase_only':
            price = purchase_price
            price_unit = purchase_unit
        
        cursor.execute("""
            UPDATE equipment 
            SET name = %s, category = %s, description = %s,
                rental_price = %s, rental_price_unit = %s,
                purchase_price = %s, purchase_unit = %s, equipment_type = %s,
                location = %s, status = %s, image_url = %s,
                stock_quantity = %s, min_stock_threshold = %s
            WHERE id = %s AND vendor_email = %s
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
        
        return jsonify({
            'success': True, 
            'message': 'Equipment updated successfully',
            'image_url': image_url
        })
        
    except Exception as e:
        print(f"❌ Error updating equipment: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/equipment/delete/<int:equipment_id>', methods=['POST'])
def delete_equipment(equipment_id):
    """Delete equipment"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            DELETE FROM equipment 
            WHERE id = %s AND vendor_email = %s
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

@app.route('/api/equipment/update-stock/<int:equipment_id>', methods=['POST'])
def update_equipment_stock(equipment_id):
    """Update equipment stock quantity"""
    if 'vendor_email' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        quantity_change = data.get('quantity_change', 0)
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT stock_quantity FROM equipment WHERE id = %s AND vendor_email = %s", 
                      (equipment_id, session['vendor_email']))
        current_stock = cursor.fetchone()
        
        if not current_stock:
            conn.close()
            return jsonify({'error': 'Equipment not found or access denied'}), 404
        
        new_stock = current_stock['stock_quantity'] + quantity_change
        if new_stock < 0:
            new_stock = 0
        
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = %s, 
                status = CASE WHEN %s <= 0 THEN 'unavailable' ELSE 'available' END
            WHERE id = %s AND vendor_email = %s
        """, (new_stock, new_stock, equipment_id, session['vendor_email']))
        
        conn.commit()
        conn.close()
        
        print(f"📦 Stock updated for equipment {equipment_id}: {current_stock['stock_quantity']} → {new_stock}")
        
        return jsonify({
            'success': True, 
            'message': 'Stock updated successfully',
            'new_stock': new_stock
        })
        
    except Exception as e:
        print(f"❌ Error updating stock: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ================= BOOKING API ==================

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
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT 
                e.id, e.vendor_email, e.name, e.category, e.description,
                e.purchase_price, e.purchase_unit, e.rental_price, e.rental_price_unit, e.equipment_type,
                e.location, e.status, e.stock_quantity, e.min_stock_threshold, e.image_url,
                v.contact_name, v.business_name
            FROM equipment e
            JOIN vendors v ON e.vendor_email = v.email
            WHERE e.id = %s
        """, (data['equipment_id'],))
        
        equipment = cursor.fetchone()
        if not equipment:
            conn.close()
            return jsonify({'error': 'Equipment not found'}), 404
        
        equipment_id = equipment['id']
        vendor_email = equipment['vendor_email']
        equipment_name = equipment['name']
        purchase_price = equipment['purchase_price']
        equipment_type = equipment['equipment_type']
        stock_quantity = equipment['stock_quantity']
        min_stock_threshold = equipment['min_stock_threshold'] or 5
        vendor_name = equipment['contact_name']
        
        if equipment_type == 'rental_only':
            conn.close()
            return jsonify({'error': 'This equipment is only available for rental, not purchase'}), 400
        
        try:
            if stock_quantity is None:
                stock_quantity_int = 1
            else:
                stock_quantity_int = int(stock_quantity)
        except (ValueError, TypeError):
            stock_quantity_int = 1
        
        print(f"📦 Stock quantity check: raw={stock_quantity}, converted={stock_quantity_int}")
        
        if stock_quantity_int <= 0:
            conn.close()
            return jsonify({'error': 'Equipment out of stock'}), 400
        
        duration = 1
        start_date = datetime.now().strftime('%Y-%m-%d')
        end_date = datetime.now().strftime('%Y-%m-%d')
        
        cursor.execute("""
            INSERT INTO bookings 
            (user_id, user_name, user_email, user_phone,
             equipment_id, equipment_name, vendor_email, vendor_name,
             start_date, end_date, duration, total_amount, status, notes,
             created_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
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
        
        new_stock = stock_quantity_int - 1
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = %s,
                status = CASE 
                    WHEN %s <= 0 THEN 'unavailable' 
                    WHEN %s <= %s THEN 'low_stock'  
                    ELSE 'available' 
                END
            WHERE id = %s
        """, (new_stock, new_stock, new_stock, min_stock_threshold, data['equipment_id']))
        
        conn.commit()
        conn.close()
        
        print(f"✅ Booking submitted. Stock updated: {stock_quantity_int} → {new_stock}")
        
        user_message = f"Your booking for {equipment_name} has been submitted successfully!"
        send_sms(session.get('user_phone', ''), user_message)
        
        return jsonify({
            'success': True,
            'message': 'Booking submitted successfully!',
            'equipment_name': equipment_name,
            'vendor_name': vendor_name
        })
        
    except Exception as e:
        print(f"❌ Error submitting booking: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ================= RENT REQUEST API ==================

@app.route('/api/rent/submit-request', methods=['POST'])
def submit_rent_request():
    """Submit a rent request using rental price"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        print("📩 Received rent request:", data)
        
        required_fields = ['equipment_id', 'start_date', 'end_date', 'purpose', 'total_amount']
        missing_fields = [field for field in required_fields if field not in data]
        
        if missing_fields:
            return jsonify({
                'success': False, 
                'error': f'Missing required fields: {", ".join(missing_fields)}'
            }), 400
        
        start_date = datetime.strptime(data['start_date'], '%Y-%m-%d')
        end_date = datetime.strptime(data['end_date'], '%Y-%m-%d')
        duration = (end_date - start_date).days + 1
        
        if duration <= 0:
            return jsonify({'error': 'End date must be after start date'}), 400
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT 
                e.id, e.vendor_email, e.name, e.category, e.description,
                e.rental_price, e.rental_price_unit, e.purchase_price, e.purchase_unit, e.equipment_type,
                e.location, e.status, e.stock_quantity, e.min_stock_threshold, e.image_url,
                v.contact_name, v.business_name, v.phone
            FROM equipment e
            JOIN vendors v ON e.vendor_email = v.email
            WHERE e.id = %s
        """, (data['equipment_id'],))
        
        equipment = cursor.fetchone()
        if not equipment:
            conn.close()
            return jsonify({'error': 'Equipment not found'}), 404
        
        equipment_id = equipment['id']
        vendor_email = equipment['vendor_email']
        equipment_name = equipment['name']
        rental_price = equipment['rental_price']
        equipment_type = equipment['equipment_type']
        stock_quantity = equipment['stock_quantity']
        min_stock_threshold = equipment['min_stock_threshold'] or 5
        vendor_name = equipment['contact_name']
        vendor_phone = equipment['phone']
        
        if equipment_type == 'purchase_only':
            conn.close()
            return jsonify({'error': 'This equipment is only available for purchase, not rental'}), 400
        
        try:
            stock_quantity_int = int(stock_quantity) if stock_quantity is not None else 1
        except (ValueError, TypeError):
            stock_quantity_int = 1
        
        print(f"📦 Stock quantity check: raw={stock_quantity}, converted={stock_quantity_int}")
        
        if stock_quantity_int <= 0:
            conn.close()
            return jsonify({'error': 'Equipment out of stock'}), 400
        
        try:
            daily_rate = float(rental_price) if rental_price is not None else 0
        except (ValueError, TypeError):
            daily_rate = 0
        
        base_amount = daily_rate * duration
        service_fee = base_amount * 0.1
        total_amount = data['total_amount']
        
        cursor.execute("""
            INSERT INTO rent_requests 
            (user_id, user_name, user_phone, user_email,
             equipment_id, equipment_name, vendor_email, vendor_name,
             start_date, end_date, duration, purpose, notes,
             daily_rate, base_amount, service_fee, total_amount, status,
             submitted_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
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
        
        new_stock = stock_quantity_int - 1
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = %s,
                status = CASE 
                    WHEN %s <= 0 THEN 'unavailable' 
                    WHEN %s <= %s THEN 'low_stock'
                    ELSE 'available' 
                END
            WHERE id = %s
        """, (new_stock, new_stock, new_stock, min_stock_threshold, data['equipment_id']))
        
        conn.commit()
        conn.close()
        
        print(f"✅ Rent request submitted. Stock updated: {stock_quantity_int} → {new_stock}")
        
        user_message = f"Your rent request for {equipment_name} has been submitted successfully!"
        send_sms(session.get('user_phone', ''), user_message)
        
        return jsonify({
            'success': True,
            'message': 'Rent request submitted successfully!',
            'equipment_name': equipment_name,
            'vendor_name': vendor_name
        })
        
    except Exception as e:
        print(f"❌ Error submitting rent request: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ================= LOAN PURCHASE API ENDPOINTS ==================

@app.route('/api/loan/purchase', methods=['POST'])
def submit_loan_purchase():
    """Submit a new equipment purchase with loan option"""
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        data = request.get_json()
        print("📩 Received loan purchase data:", data)
        
        # Validate required fields
        required_fields = ['equipment_id', 'purchase_amount', 'down_payment', 
                          'loan_amount', 'interest_rate', 'loan_term_years', 
                          'emi_amount', 'total_payable', 'total_interest']
        
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({'error': f'Missing required fields: {", ".join(missing_fields)}'}), 400
        
        # Get user details from session
        user_id = session['user_id']
        user_name = session['user_name']
        user_email = session.get('user_email', '')
        user_phone = session.get('user_phone', '')
        
        # Connect to database
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get equipment and vendor details
        cursor.execute("""
            SELECT e.id, e.name, e.vendor_email, e.stock_quantity, e.min_stock_threshold,
                   v.contact_name as vendor_name, v.phone as vendor_phone
            FROM equipment e
            JOIN vendors v ON e.vendor_email = v.email
            WHERE e.id = %s
        """, (data['equipment_id'],))
        
        equipment = cursor.fetchone()
        if not equipment:
            conn.close()
            return jsonify({'error': 'Equipment not found'}), 404
        
        # Extract equipment details
        equipment_id = equipment['id']
        equipment_name = equipment['name']
        vendor_email = equipment['vendor_email']
        vendor_name = equipment['vendor_name']
        vendor_phone = equipment.get('vendor_phone')
        stock_quantity = equipment['stock_quantity']
        min_stock_threshold = equipment['min_stock_threshold'] or 5
        
        # Check stock
        try:
            stock_quantity_int = int(stock_quantity) if stock_quantity is not None else 1
        except (ValueError, TypeError):
            stock_quantity_int = 1
        
        if stock_quantity_int <= 0:
            conn.close()
            return jsonify({'error': 'Equipment out of stock'}), 400
        
        # Calculate dates
        today = datetime.now().date()
        loan_term_months = data['loan_term_years'] * 12
        
        # Calculate first EMI date (next month)
        if today.month == 12:
            first_emi_date = date(today.year + 1, 1, today.day)
        else:
            first_emi_date = date(today.year, today.month + 1, today.day)
        
        # Calculate last EMI date
        last_emi_year = today.year + data['loan_term_years']
        last_emi_month = today.month
        if last_emi_month > 12:
            last_emi_year += last_emi_month // 12
            last_emi_month = last_emi_month % 12
        last_emi_date = date(last_emi_year, last_emi_month, today.day)
        
        # 1. Insert into loan_purchases
        cursor.execute("""
            INSERT INTO loan_purchases 
            (user_id, user_name, user_phone, user_email, equipment_id, equipment_name, 
             vendor_email, vendor_name, purchase_amount, down_payment, loan_amount, 
             interest_rate, loan_term_years, loan_term_months, emi_amount, 
             total_payable, total_interest, first_emi_date, last_emi_date, 
             payment_mode, next_due_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            user_id, user_name, user_phone, user_email,
            equipment_id, equipment_name,
            vendor_email, vendor_name,
            data['purchase_amount'],
            data['down_payment'],
            data['loan_amount'],
            data['interest_rate'],
            data['loan_term_years'],
            loan_term_months,
            data['emi_amount'],
            data['total_payable'],
            data['total_interest'],
            first_emi_date,
            last_emi_date,
            'loan',
            first_emi_date
        ))
        
        loan_result = cursor.fetchone()
        if not loan_result:
            conn.close()
            return jsonify({'error': 'Failed to create loan record'}), 500
        
        loan_id = loan_result['id']
        print(f"✅ Created loan record #{loan_id}")
        
        # 2. ALSO CREATE A BOOKING RECORD (for vendor to see)
        start_date = datetime.now().strftime('%Y-%m-%d')
        end_date = datetime.now().strftime('%Y-%m-%d')
        duration = 1
        
        cursor.execute("""
            INSERT INTO bookings 
            (user_id, user_name, user_email, user_phone,
             equipment_id, equipment_name, vendor_email, vendor_name,
             start_date, end_date, duration, total_amount, status, notes,
             created_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING id
        """, (
            user_id,
            user_name,
            user_email,
            user_phone,
            equipment_id,
            equipment_name,
            vendor_email,
            vendor_name,
            start_date,
            end_date,
            duration,
            data['purchase_amount'],  # Use purchase amount as total
            'confirmed',  # Auto-confirmed for loan purchases
            f"Loan purchase - Loan ID: {loan_id}, EMI: ₹{data['emi_amount']}/month for {data['loan_term_years']} years"
        ))
        
        booking_result = cursor.fetchone()
        if not booking_result:
            conn.close()
            return jsonify({'error': 'Failed to create booking record'}), 500
        
        booking_id = booking_result['id']
        print(f"✅ Created booking #{booking_id} for vendor visibility")
        
        # 3. Decrease stock
        new_stock = stock_quantity_int - 1
        cursor.execute("""
            UPDATE equipment 
            SET stock_quantity = %s,
                status = CASE 
                    WHEN %s <= 0 THEN 'unavailable' 
                    WHEN %s <= %s THEN 'low_stock'  
                    ELSE 'available' 
                END
            WHERE id = %s
        """, (new_stock, new_stock, new_stock, min_stock_threshold, equipment_id))
        
        print(f"📦 Stock updated: {stock_quantity_int} → {new_stock}")
        
        # Commit all changes
        conn.commit()
        conn.close()
        
        # Send SMS notification to farmer
        farmer_message = f"Your loan application for {equipment_name} has been submitted successfully! Booking ID: #{booking_id}. First EMI of ₹{data['emi_amount']} is due on {first_emi_date.strftime('%d-%b-%Y')}. - Lend A Hand"
        send_sms(user_phone, farmer_message)
        
        # Send SMS notification to vendor
        if vendor_phone:
            vendor_message = f"New loan purchase booking #{booking_id} for {equipment_name} by {user_name}. Amount: ₹{data['purchase_amount']} - Lend A Hand"
            send_sms(vendor_phone, vendor_message)
        
        return jsonify({
            'success': True,
            'message': 'Loan purchase submitted successfully!',
            'loan_id': loan_id,
            'booking_id': booking_id,
            'first_emi_date': first_emi_date.strftime('%Y-%m-%d')
        })
        
    except Exception as e:
        print(f"❌ Error submitting loan purchase: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ================= ADMIN API ENDPOINTS ==================

@app.route('/api/admin/loans')
def api_admin_loans():
    """Get all loans with filtering options - checks both tables"""
    print("="*60)
    print("LOAN API CALLED - COMBINED TABLES VERSION")
    print("="*60)
    
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        print("❌ Unauthorized")
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        status_filter = request.args.get('status', 'all')
        search_term = request.args.get('search', '')
        
        print(f"📊 Filters: status={status_filter}, search={search_term}")
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        all_loans = []
        
        # Get from loan_purchases (where farmer loans go)
        try:
            cursor.execute("""
                SELECT 
                    id,
                    user_id,
                    user_name,
                    user_phone,
                    user_email,
                    equipment_id,
                    equipment_name,
                    loan_amount,
                    down_payment,
                    interest_rate,
                    loan_term_months,
                    emi_amount,
                    total_payable,
                    total_interest,
                    status,
                    COALESCE(emi_paid, 0) as emi_paid,
                    COALESCE(emi_missed, 0) as emi_missed,
                    next_due_date,
                    created_at as created_date,
                    first_emi_date,
                    last_emi_date,
                    'purchases' as source_table
                FROM loan_purchases
                WHERE 1=1
            """)
            purchases_loans = cursor.fetchall()
            print(f"✅ Found {len(purchases_loans)} loans in loan_purchases")
            all_loans.extend(purchases_loans)
        except Exception as e:
            print(f"Note: Could not query loan_purchases: {e}")
        
        # Also get from loan_history (for any existing history data)
        try:
            cursor.execute("""
                SELECT 
                    id,
                    user_id,
                    user_name,
                    user_phone,
                    user_email,
                    equipment_id,
                    equipment_name,
                    loan_amount,
                    COALESCE(down_payment, 0) as down_payment,
                    interest_rate,
                    loan_term_months,
                    emi_amount,
                    total_payable,
                    total_interest,
                    status,
                    COALESCE(emi_paid, 0) as emi_paid,
                    COALESCE(emi_missed, 0) as emi_missed,
                    next_due_date,
                    created_at as created_date,
                    first_emi_date,
                    last_emi_date,
                    'history' as source_table
                FROM loan_history
                WHERE 1=1
            """)
            history_loans = cursor.fetchall()
            print(f"✅ Found {len(history_loans)} loans in loan_history")
            all_loans.extend(history_loans)
        except Exception as e:
            print(f"Note: Could not query loan_history: {e}")
        
        # Sort by created_date (newest first)
        all_loans.sort(key=lambda x: x.get('created_date', datetime.min), reverse=True)
        
        print(f"📊 Total loans found: {len(all_loans)}")
        
        # Calculate additional fields for each loan
        today = datetime.now().date()
        for loan in all_loans:
            if loan.get('next_due_date'):
                due_date = loan['next_due_date']
                if isinstance(due_date, str):
                    due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
                
                days_overdue = (today - due_date).days if today > due_date else 0
                loan['days_overdue'] = days_overdue
                
                emi_missed = loan.get('emi_missed', 0) or 0
                if emi_missed == 0:
                    loan['default_risk'] = 'low'
                elif emi_missed <= 2:
                    loan['default_risk'] = 'medium'
                elif emi_missed <= 4:
                    loan['default_risk'] = 'high'
                else:
                    loan['default_risk'] = 'critical'
            else:
                loan['days_overdue'] = 0
                loan['default_risk'] = 'low'
            
            # Get recent payments
            try:
                cursor.execute("""
                    SELECT * FROM loan_payments 
                    WHERE loan_id = %s 
                    ORDER BY payment_date DESC 
                    LIMIT 5
                """, (loan['id'],))
                loan['recent_payments'] = cursor.fetchall()
            except:
                loan['recent_payments'] = []
        
        conn.close()
        return jsonify(all_loans)
        
    except Exception as e:
        print(f"❌ Error fetching loans: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'loans': []}), 500

@app.route('/debug-check-loans')
def debug_check_loans():
    """Debug endpoint to check loan data"""
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        result = "<h2>Loan Data Debug</h2>"
        
        # Check loan_history
        cursor.execute("SELECT COUNT(*) as count FROM loan_history")
        history_count = cursor.fetchone()['count']
        result += f"<h3>loan_history: {history_count} records</h3>"
        
        if history_count > 0:
            cursor.execute("SELECT * FROM loan_history ORDER BY id DESC LIMIT 5")
            history_data = cursor.fetchall()
            result += "<table border='1'><tr>"
            if history_data:
                for key in history_data[0].keys():
                    result += f"<th>{key}</th>"
                result += "</tr>"
                for row in history_data:
                    result += "<tr>"
                    for value in row.values():
                        result += f"<td>{value}</td>"
                    result += "</tr>"
                result += "</table>"
        
        # Check loan_purchases
        cursor.execute("SELECT COUNT(*) as count FROM loan_purchases")
        purchases_count = cursor.fetchone()['count']
        result += f"<h3>loan_purchases: {purchases_count} records</h3>"
        
        if purchases_count > 0:
            cursor.execute("SELECT * FROM loan_purchases ORDER BY id DESC LIMIT 5")
            purchases_data = cursor.fetchall()
            result += "<table border='1'><tr>"
            if purchases_data:
                for key in purchases_data[0].keys():
                    result += f"<th>{key}</th>"
                result += "</tr>"
                for row in purchases_data:
                    result += "<tr>"
                    for value in row.values():
                        result += f"<td>{value}</td>"
                    result += "</tr>"
                result += "</table>"
        
        conn.close()
        return result
    except Exception as e:
        return f"Error: {str(e)}"

@app.route('/api/admin/loan/<int:loan_id>')
def api_admin_loan_detail(loan_id):
    """Get detailed loan information with payment history"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get loan details from loan_purchases (without joining farmers)
        cursor.execute("""
            SELECT 
                lp.*,
                e.name as equipment_name,
                e.category as equipment_category,
                e.image_url as equipment_image
            FROM loan_purchases lp
            LEFT JOIN equipment e ON lp.equipment_id = e.id
            WHERE lp.id = %s
        """, (loan_id,))
        
        loan = cursor.fetchone()
        
        if not loan:
            conn.close()
            return jsonify({'error': 'Loan not found'}), 404
        
        # Get payment history
        cursor.execute("""
            SELECT * FROM loan_payments 
            WHERE loan_id = %s 
            ORDER BY payment_date DESC
        """, (loan_id,))
        
        payments = cursor.fetchall()
        
        # Calculate days overdue
        today = datetime.now().date()
        days_overdue = 0
        if loan.get('next_due_date'):
            due_date = loan['next_due_date']
            if isinstance(due_date, str):
                due_date = datetime.strptime(due_date, '%Y-%m-%d').date()
            days_overdue = (today - due_date).days if today > due_date else 0
        
        conn.close()
        
        return jsonify({
            'loan': loan,
            'payments': payments,
            'days_overdue': days_overdue
        })
        
    except Exception as e:
        print(f"Error fetching loan details: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/loan/<int:loan_id>/payments', methods=['POST'])
def api_admin_add_loan_payment(loan_id):
    """Add a payment for a loan"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        
        amount_paid = data.get('amount_paid')
        payment_method = data.get('payment_method', 'cash')
        transaction_id = data.get('transaction_id')
        remarks = data.get('remarks')
        
        if not amount_paid or amount_paid <= 0:
            return jsonify({'error': 'Invalid payment amount'}), 400
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT * FROM loan_purchases 
            WHERE id = %s
        """, (loan_id,))
        
        loan = cursor.fetchone()
        
        if not loan:
            conn.close()
            return jsonify({'error': 'Loan not found'}), 404
        
        if loan['status'] not in ['active', 'defaulted']:
            return jsonify({'error': 'Cannot add payment to this loan'}), 400
        
        monthly_rate = loan['interest_rate'] / 100 / 12
        interest_paid = loan['emi_amount'] * 0.7
        principal_paid = amount_paid - interest_paid
        
        payment_month = loan['emi_paid'] + 1
        
        cursor.execute("""
            INSERT INTO loan_payments 
            (loan_id, user_id, due_date, amount_paid, principal_paid, 
             interest_paid, payment_method, transaction_id, payment_month, remarks)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            loan_id,
            loan['user_id'],
            loan['next_due_date'],
            amount_paid,
            principal_paid,
            interest_paid,
            payment_method,
            transaction_id,
            payment_month,
            remarks
        ))
        
        new_emi_paid = loan['emi_paid'] + 1
        
        next_due_date = None
        if loan['next_due_date']:
            next_due = loan['next_due_date']
            if isinstance(next_due, str):
                next_due = datetime.strptime(next_due, '%Y-%m-%d').date()
            
            if next_due.month == 12:
                next_due_date = date(next_due.year + 1, 1, next_due.day)
            else:
                next_due_date = date(next_due.year, next_due.month + 1, next_due.day)
        
        new_status = loan['status']
        if new_emi_paid >= loan['loan_term_months']:
            new_status = 'completed'
        elif loan['status'] == 'defaulted' and new_emi_paid > loan['emi_paid']:
            new_status = 'active'
        
        cursor.execute("""
            UPDATE loan_purchases 
            SET emi_paid = %s,
                last_payment_date = CURRENT_TIMESTAMP,
                next_due_date = %s,
                status = %s
            WHERE id = %s
        """, (new_emi_paid, next_due_date, new_status, loan_id))
        
        conn.commit()
        conn.close()
        
        send_sms(loan['user_phone'], f"Your payment of ₹{amount_paid} for {loan['equipment_name']} loan has been recorded. EMI {new_emi_paid}/{loan['loan_term_months']} paid. - Lend A Hand")
        
        return jsonify({
            'success': True,
            'message': 'Payment recorded successfully'
        })
        
    except Exception as e:
        print(f"Error recording payment: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/loan/<int:loan_id>/status', methods=['POST'])
def api_admin_update_loan_status(loan_id):
    """Update loan status (mark as defaulted, foreclosed, etc.)"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        new_status = data.get('status')
        reason = data.get('reason')
        
        if new_status not in ['defaulted', 'foreclosed', 'completed']:
            return jsonify({'error': 'Invalid status'}), 400
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            UPDATE loan_purchases 
            SET status = %s, notes = CONCAT(COALESCE(notes, ''), '\\n', %s)
            WHERE id = %s
        """, (new_status, f"Status changed to {new_status}: {reason}", loan_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'Loan status updated to {new_status}'
        })
        
    except Exception as e:
        print(f"Error updating loan status: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/loan/stats')
def api_admin_loan_stats():
    """Get loan statistics"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT COALESCE(SUM(loan_amount), 0) as total FROM loan_purchases WHERE status != 'foreclosed'")
        total_loan_amount = cursor.fetchone()['total']
        
        cursor.execute("SELECT COUNT(*) as count FROM loan_purchases WHERE status = 'active'")
        active_loans = cursor.fetchone()['count']
        
        cursor.execute("SELECT COUNT(*) as count FROM loan_purchases WHERE status = 'defaulted'")
        defaulted_loans = cursor.fetchone()['count']
        
        today = datetime.now().date()
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM loan_purchases 
            WHERE status = 'active' 
            AND next_due_date < %s
        """, (today,))
        high_risk_loans = cursor.fetchone()['count']
        
        conn.close()
        
        return jsonify({
            'total_loan_amount': total_loan_amount,
            'active_loans': active_loans,
            'defaulted_loans': defaulted_loans,
            'high_risk_loans': high_risk_loans
        })
        
    except Exception as e:
        print(f"Error fetching loan stats: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/farmers')
def api_admin_farmers():
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        status_filter = request.args.get('status', 'all')
        search_term = request.args.get('search', '')
        
        query = "SELECT * FROM farmers"
        params = []
        conditions = []
        
        if status_filter != 'all':
            conditions.append("status = %s")
            params.append(status_filter)
            
        if search_term:
            conditions.append("(full_name ILIKE %s OR last_name ILIKE %s OR email ILIKE %s OR phone ILIKE %s OR farm_location ILIKE %s)")
            search_pattern = f"%{search_term}%"
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern, search_pattern])
            
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY registration_date DESC"
        
        cursor.execute(query, params)
        farmers = cursor.fetchall()
        
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
        
    except Exception as e:
        print(f"Error fetching farmers: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/farmer/<int:farmer_id>')
def api_admin_farmer_detail(farmer_id):
    """Get detailed farmer information for admin dashboard"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT * FROM farmers WHERE id = %s", (farmer_id,))
        farmer = cursor.fetchone()
        conn.close()
        
        if not farmer:
            return jsonify({'error': 'Farmer not found'}), 404
        
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

@app.route('/api/admin/vendors')
def api_admin_vendors():
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        status_filter = request.args.get('status', 'all')
        search_term = request.args.get('search', '')
        
        query = "SELECT * FROM vendors"
        params = []
        conditions = []
        
        if status_filter != 'all':
            conditions.append("status = %s")
            params.append(status_filter)
            
        if search_term:
            conditions.append("(business_name ILIKE %s OR contact_name ILIKE %s OR email ILIKE %s OR phone ILIKE %s OR service_type ILIKE %s)")
            search_pattern = f"%{search_term}%"
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern, search_pattern])
            
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY registration_date DESC"
        
        cursor.execute(query, params)
        vendors = cursor.fetchall()
        
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
                'business_document': vendor['business_document'],
                'document_verified': vendor['document_verified'],
                'document_url': url_for('serve_vendor_document', filename=vendor['business_document']) if vendor['business_document'] else None,
                'registration_date': vendor['registration_date'],
                'status': vendor['status']
            }
            
            cursor.execute("SELECT COUNT(*) as count FROM equipment WHERE vendor_email = %s", (vendor['email'],))
            equipment_count = cursor.fetchone()['count']
            vendor_data['equipment_count'] = equipment_count
            
            cursor.execute("SELECT COUNT(*) as count FROM bookings WHERE vendor_email = %s", (vendor['email'],))
            booking_count = cursor.fetchone()['count']
            vendor_data['booking_count'] = booking_count
            
            vendors_list.append(vendor_data)
        
        conn.close()
        return jsonify(vendors_list)
        
    except Exception as e:
        print(f"Error fetching vendors: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/vendor/<int:vendor_id>')
def api_admin_vendor_detail(vendor_id):
    """Get detailed vendor information for admin dashboard"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT * FROM vendors WHERE id = %s", (vendor_id,))
        vendor = cursor.fetchone()
        
        if not vendor:
            conn.close()
            return jsonify({'error': 'Vendor not found'}), 404
        
        cursor.execute("SELECT COUNT(*) as count FROM equipment WHERE vendor_email = %s", (vendor['email'],))
        equipment_count = cursor.fetchone()['count'] or 0
        
        cursor.execute("SELECT COUNT(*) as count FROM bookings WHERE vendor_email = %s", (vendor['email'],))
        booking_count = cursor.fetchone()['count'] or 0
        
        conn.close()
        
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

@app.route('/api/admin/equipment')
def api_admin_equipment():
    """Get all equipment for admin dashboard"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
    """Get all bookings for admin dashboard"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        status_filter = request.args.get('status', 'all')
        search_term = request.args.get('search', '')
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        query = "SELECT * FROM bookings"
        params = []
        conditions = []
        
        if status_filter != 'all':
            conditions.append("status = %s")
            params.append(status_filter)
        
        if search_term:
            conditions.append("(user_name ILIKE %s OR equipment_name ILIKE %s OR vendor_name ILIKE %s)")
            search_pattern = f"%{search_term}%"
            params.extend([search_pattern, search_pattern, search_pattern])
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY created_date DESC"
        
        cursor.execute(query, params)
        bookings = cursor.fetchall()
        
        bookings_list = []
        for booking in bookings:
            # Get farmer location from farmers table
            cursor.execute("SELECT farm_location FROM farmers WHERE id = %s", (booking['user_id'],))
            farmer_data = cursor.fetchone()
            farmer_location = farmer_data['farm_location'] if farmer_data else "Unknown"
            
            bookings_list.append({
                'id': booking['id'],
                'farmer_name': booking['user_name'],
                'farmer_phone': booking['user_phone'],
                'farmer_location': farmer_location,
                'vendor_name': booking['vendor_name'],
                'equipment_name': booking['equipment_name'],
                'start_date': booking['start_date'],
                'end_date': booking['end_date'],
                'total_days': booking['duration'],
                'total_amount': booking['total_amount'],
                'status': booking['status'] or 'pending',
                'booking_date': booking['created_date']
            })
        
        conn.close()
        return jsonify(bookings_list)
        
    except Exception as e:
        print(f"Error fetching bookings: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/booking/<int:booking_id>')
def api_admin_booking_detail(booking_id):
    """Get detailed booking information"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT * FROM bookings WHERE id = %s", (booking_id,))
        booking = cursor.fetchone()
        
        if not booking:
            conn.close()
            return jsonify({'error': 'Booking not found'}), 404
        
        # Get farmer details
        cursor.execute("SELECT email, farm_location FROM farmers WHERE id = %s", (booking['user_id'],))
        farmer_data = cursor.fetchone()
        farmer_email = farmer_data['email'] if farmer_data else "Unknown"
        farmer_location = farmer_data['farm_location'] if farmer_data else "Unknown"
        
        # Get vendor details
        cursor.execute("SELECT contact_name, phone FROM vendors WHERE email = %s", (booking['vendor_email'],))
        vendor_data = cursor.fetchone()
        vendor_contact = vendor_data['contact_name'] if vendor_data else "Unknown"
        vendor_phone = vendor_data['phone'] if vendor_data else "Unknown"
        
        # Get equipment details
        cursor.execute("SELECT category, price FROM equipment WHERE id = %s", (booking['equipment_id'],))
        equipment_data = cursor.fetchone()
        equipment_category = equipment_data['category'] if equipment_data else "Unknown"
        equipment_price = equipment_data['price'] if equipment_data else 0
        
        conn.close()
        
        booking_data = {
            'id': booking['id'],
            'farmer_name': booking['user_name'],
            'farmer_phone': booking['user_phone'],
            'farmer_email': farmer_email,
            'farmer_location': farmer_location,
            'vendor_name': booking['vendor_name'],
            'vendor_contact': vendor_contact,
            'vendor_phone': vendor_phone,
            'vendor_email': booking['vendor_email'],
            'equipment_name': booking['equipment_name'],
            'equipment_category': equipment_category,
            'equipment_price': equipment_price,
            'start_date': booking['start_date'],
            'end_date': booking['end_date'],
            'total_days': booking['duration'],
            'total_amount': booking['total_amount'],
            'status': booking['status'],
            'notes': booking['notes'],
            'booking_date': booking['created_date'],
            'payment_status': 'paid'
        }
        
        return jsonify(booking_data)
        
    except Exception as e:
        print(f"Error fetching booking details: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/booking/delete/<int:booking_id>', methods=['POST'])
def api_admin_delete_booking(booking_id):
    """Delete a booking (admin only)"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM bookings WHERE id = %s", (booking_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Booking deleted successfully'})
        
    except Exception as e:
        print(f"Error deleting booking: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/stats')
def api_admin_stats():
    """Get admin dashboard statistics"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        stats = {}
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Farmers stats
        cursor.execute("SELECT COUNT(*) as count FROM farmers")
        result = cursor.fetchone()
        stats['total_farmers'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM farmers WHERE status = 'pending'")
        result = cursor.fetchone()
        stats['pending_farmers'] = result['count'] if result else 0
        
        # Vendors stats
        cursor.execute("SELECT COUNT(*) as count FROM vendors")
        result = cursor.fetchone()
        stats['total_vendors'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM vendors WHERE status = 'pending'")
        result = cursor.fetchone()
        stats['pending_vendors'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM vendors WHERE document_verified = 'pending'")
        result = cursor.fetchone()
        stats['pending_documents'] = result['count'] if result else 0
        
        # Equipment stats
        cursor.execute("SELECT COUNT(*) as count FROM equipment")
        result = cursor.fetchone()
        stats['total_equipment'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM equipment WHERE status = 'available'")
        result = cursor.fetchone()
        stats['available_equipment'] = result['count'] if result else 0
        
        # Bookings stats
        cursor.execute("SELECT COUNT(*) as count FROM bookings")
        result = cursor.fetchone()
        stats['total_bookings'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM bookings WHERE status = 'pending'")
        result = cursor.fetchone()
        stats['pending_bookings'] = result['count'] if result else 0
        
        # Rent requests stats
        cursor.execute("SELECT COUNT(*) as count FROM rent_requests")
        result = cursor.fetchone()
        stats['total_rent_requests'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM rent_requests WHERE status = 'pending'")
        result = cursor.fetchone()
        stats['pending_rent_requests'] = result['count'] if result else 0
        
        # Loans stats
        cursor.execute("SELECT COUNT(*) as count FROM loan_purchases")
        result = cursor.fetchone()
        stats['total_loans'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM loan_purchases WHERE status = 'active'")
        result = cursor.fetchone()
        stats['active_loans'] = result['count'] if result else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM loan_purchases WHERE status = 'defaulted'")
        result = cursor.fetchone()
        stats['defaulted_loans'] = result['count'] if result else 0
        
        conn.close()
        
        print(f"✅ Stats generated: {stats}")
        return jsonify(stats)
        
    except Exception as e:
        print(f"❌ Error generating stats: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/farmers-count')
def api_admin_farmers_count():
    """Get count of farmers for broadcast"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT COUNT(*) as count FROM farmers")
        result = cursor.fetchone()
        count = result['count'] if result else 0
        conn.close()
        
        return jsonify({
            'total_farmers': count,
            'success': True
        })
        
    except Exception as e:
        print(f"Error fetching farmers count: {str(e)}")
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
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT full_name, phone FROM farmers")
        farmers = cursor.fetchall()
        conn.close()
        
        if not farmers:
            return jsonify({'error': 'No farmers found in database'}), 400
        
        success_count = 0
        failed_count = 0
        
        full_message = f"{title}\n\n{content}\n\n- Lend A Hand"
        
        print(f"📢 Starting broadcast to {len(farmers)} farmers")
        
        for farmer in farmers:
            farmer_name = farmer['full_name']
            farmer_phone = farmer['phone']
            
            try:
                phone = ''.join(filter(str.isdigit, str(farmer_phone)))
                
                print(f"📱 Sending to {farmer_name}: {phone}")
                
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
        
        # Save to history
        try:
            vendors_conn = get_vendors_db()
            vendors_cursor = vendors_conn.cursor()
            vendors_cursor.execute("""
                INSERT INTO broadcast_history 
                (title, content, type, recipients_count, success_count, failed_count, sent_by, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (title, content, message_type, len(farmers), success_count, failed_count, 
                  session.get('admin_name', 'Admin'), 'sent' if success_count > 0 else 'failed'))
            vendors_conn.commit()
            vendors_conn.close()
        except Exception as e:
            print(f"Note: Could not save to history: {e}")
        
        response_data = {
            'success': True,
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

@app.route('/api/admin/broadcast-history')
def api_admin_broadcast_history():
    """Get broadcast message history"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT * FROM broadcast_history 
            ORDER BY sent_date DESC 
            LIMIT 20
        """)
        history = cursor.fetchall()
        conn.close()
        
        return jsonify(history)
        
    except Exception as e:
        print(f"Error fetching broadcast history: {str(e)}")
        return jsonify([])

@app.route('/api/admin/vendor/document/verify', methods=['POST'])
def verify_vendor_document():
    """Update vendor document verification status"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        print("📨 Received verification data:", data)
        
        vendor_id = data.get('vendor_id')
        status = data.get('status')
        
        if not vendor_id or not status:
            return jsonify({'error': 'Missing vendor_id or status'}), 400
        
        if status not in ['verified', 'rejected', 'pending']:
            return jsonify({'error': 'Invalid status. Use verified, rejected, or pending'}), 400
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT id, email, business_name, phone FROM vendors WHERE id = %s", (vendor_id,))
        vendor = cursor.fetchone()
        
        if not vendor:
            conn.close()
            return jsonify({'error': 'Vendor not found'}), 404
        
        vendor_phone = vendor['phone']
        
        cursor.execute("""
            UPDATE vendors 
            SET document_verified = %s 
            WHERE id = %s
        """, (status, vendor_id))
        
        affected_rows = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        print(f"✅ Updated vendor {vendor_id} document status to {status}. Affected rows: {affected_rows}")
        
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

@app.route('/api/admin/farmer/approve/<int:farmer_id>', methods=['POST'])
def api_approve_farmer(farmer_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT phone FROM farmers WHERE id = %s", (farmer_id,))
        farmer_phone = cursor.fetchone()
        
        if farmer_phone:
            sms_message = "Your farmer registration has been approved! You can now log in to the platform."
            send_sms(farmer_phone['phone'], sms_message)
        
        cursor.execute("UPDATE farmers SET status = 'approved' WHERE id = %s", (farmer_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/farmer/reject/<int:farmer_id>', methods=['POST'])
def api_reject_farmer(farmer_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT phone FROM farmers WHERE id = %s", (farmer_id,))
        farmer_phone = cursor.fetchone()
        
        if farmer_phone:
            sms_message = "Your farmer registration has been rejected. Please contact support for more information."
            send_sms(farmer_phone['phone'], sms_message)
        
        cursor.execute("UPDATE farmers SET status = 'rejected' WHERE id = %s", (farmer_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/vendor/approve/<int:vendor_id>', methods=['POST'])
def api_approve_vendor(vendor_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT phone FROM vendors WHERE id = %s", (vendor_id,))
        vendor_phone = cursor.fetchone()
        
        if vendor_phone:
            sms_message = "Your vendor registration has been approved! You can now access all features of the platform."
            send_sms(vendor_phone['phone'], sms_message)
        
        cursor.execute("UPDATE vendors SET status = 'approved' WHERE id = %s", (vendor_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/vendor/reject/<int:vendor_id>', methods=['POST'])
def api_reject_vendor(vendor_id):
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT phone FROM vendors WHERE id = %s", (vendor_id,))
        vendor_phone = cursor.fetchone()
        
        if vendor_phone:
            sms_message = "Your vendor registration has been rejected. Please contact support for more information."
            send_sms(vendor_phone['phone'], sms_message)
        
        cursor.execute("UPDATE vendors SET status = 'rejected' WHERE id = %s", (vendor_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/reports/real-data')
def api_admin_real_reports():
    """Get real data from database - NO DUMMY VALUES"""
    if 'admin_id' not in session or session.get('user_type') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        print("📊 Loading REAL reports data from database...")
        
        conn = get_vendors_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Summary stats
        cursor.execute("SELECT COUNT(*) as count FROM farmers")
        total_farmers = cursor.fetchone()['count'] or 0
        
        cursor.execute("SELECT COUNT(*) as count FROM vendors")
        total_vendors = cursor.fetchone()['count'] or 0
        
        cursor.execute("SELECT COUNT(*) as count FROM equipment")
        total_equipment = cursor.fetchone()['count'] or 0
        
        cursor.execute("SELECT COUNT(*) as count FROM equipment WHERE status = 'available'")
        available_equipment = cursor.fetchone()['count'] or 0
        
        cursor.execute("SELECT COUNT(*) as count FROM bookings")
        total_bookings = cursor.fetchone()['count'] or 0
        
        cursor.execute("SELECT COUNT(*) as count FROM rent_requests")
        total_rents = cursor.fetchone()['count'] or 0
        
        # Registration timeline (last 6 months)
        registration_data = []
        for i in range(5, -1, -1):
            date = datetime.now().date() - timedelta(days=30 * i)
            start_date = date.replace(day=1)
            end_date = (date.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            
            cursor.execute("""
                SELECT COUNT(*) as count FROM farmers 
                WHERE DATE(registration_date) BETWEEN %s AND %s
            """, (start_date, end_date))
            farmers_count = cursor.fetchone()['count'] or 0
            
            cursor.execute("""
                SELECT COUNT(*) as count FROM vendors 
                WHERE DATE(registration_date) BETWEEN %s AND %s
            """, (start_date, end_date))
            vendors_count = cursor.fetchone()['count'] or 0
            
            registration_data.append({
                'date': start_date.strftime('%b %Y'),
                'farmers': farmers_count,
                'vendors': vendors_count,
                'total': farmers_count + vendors_count
            })
        
        # Equipment categories
        cursor.execute("""
            SELECT category, COUNT(*) as count 
            FROM equipment 
            GROUP BY category 
            ORDER BY count DESC
        """)
        categories_result = cursor.fetchall()
        
        category_distribution = []
        for row in categories_result:
            if row['category']:
                category_distribution.append({
                    'category': row['category'],
                    'count': row['count']
                })
        
        # Booking status distribution
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM bookings 
            GROUP BY status
        """)
        booking_statuses = cursor.fetchall()
        
        booking_status_distribution = []
        for row in booking_statuses:
            if row['status']:
                booking_status_distribution.append({
                    'status': row['status'].replace('_', ' ').title(),
                    'count': row['count']
                })
        
        # Rent status distribution
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM rent_requests 
            GROUP BY status
        """)
        rent_statuses = cursor.fetchall()
        
        rent_status_distribution = []
        for row in rent_statuses:
            if row['status']:
                rent_status_distribution.append({
                    'status': row['status'].replace('_', ' ').title(),
                    'count': row['count']
                })
        
        # Recent bookings
        cursor.execute("""
            SELECT id, equipment_name, user_name, total_amount, status, created_date
            FROM bookings 
            ORDER BY created_date DESC 
            LIMIT 10
        """)
        recent_bookings = []
        for row in cursor.fetchall():
            recent_bookings.append({
                'id': row['id'],
                'equipment_name': row['equipment_name'],
                'user_name': row['user_name'],
                'total_amount': float(row['total_amount']) if row['total_amount'] else 0,
                'status': row['status'],
                'date': row['created_date']
            })
        
        # Recent rent requests
        cursor.execute("""
            SELECT id, equipment_name, user_name, total_amount, status, submitted_date
            FROM rent_requests 
            ORDER BY submitted_date DESC 
            LIMIT 10
        """)
        recent_rents = []
        for row in cursor.fetchall():
            recent_rents.append({
                'id': row['id'],
                'equipment_name': row['equipment_name'],
                'user_name': row['user_name'],
                'total_amount': float(row['total_amount']) if row['total_amount'] else 0,
                'status': row['status'],
                'date': row['submitted_date']
            })
        
        # Recent registrations
        cursor.execute("""
            SELECT id, full_name, last_name, 'farmer' as type, registration_date
            FROM farmers 
            ORDER BY registration_date DESC 
            LIMIT 5
        """)
        recent_farmers = cursor.fetchall()
        
        cursor.execute("""
            SELECT id, business_name, contact_name, 'vendor' as type, registration_date
            FROM vendors 
            ORDER BY registration_date DESC 
            LIMIT 5
        """)
        recent_vendors = cursor.fetchall()
        
        recent_registrations = []
        for farmer in recent_farmers:
            recent_registrations.append({
                'id': farmer['id'],
                'name': f"{farmer['full_name']} {farmer['last_name']}",
                'type': farmer['type'],
                'date': farmer['registration_date']
            })
        
        for vendor in recent_vendors:
            recent_registrations.append({
                'id': vendor['id'],
                'name': f"{vendor['business_name']} ({vendor['contact_name']})",
                'type': vendor['type'],
                'date': vendor['registration_date']
            })
        
        recent_registrations.sort(key=lambda x: x['date'], reverse=True)
        
        conn.close()
        
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
                'registrations': recent_registrations[:10]
            }
        }
        
        print(f"✅ REAL Reports data loaded: {total_farmers} farmers, {total_vendors} vendors, {total_equipment} equipment")
        
        return jsonify(reports_data)
        
    except Exception as e:
        print(f"❌ Error in REAL reports API: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'message': 'Failed to load real reports data'}), 500

# ================= TRANSLATE ROUTE ==================

@app.route("/translate")
def translate():
    response = requests.get("https://example.com")
    return response.text

if __name__ == '__main__':
    init_databases()
    start_reminder_scheduler()
    app.run(debug=True)
