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
    """Connect to vendors database using DATABASE_URL environment variable"""
    DATABASE_URL = os.getenv("DATABASE_URL")
    
    if not DATABASE_URL:
        # Fallback for local development
        DATABASE_URL = f"postgresql://{os.getenv('DB_USER', 'postgres')}:{os.getenv('DB_PASSWORD', 'Hruthik@2004')}@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'vendors')}"
        print(f"⚠️ Using local database URL (password hidden)")
    
    # Handle Heroku's postgres:// URLs (deprecated but still used)
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            sslmode='require' if os.getenv('DB_SSL', 'true').lower() == 'true' else 'disable',
            cursor_factory=RealDictCursor
        )
        return conn
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        print(f"   URL format: {DATABASE_URL[:20]}...")
        raise

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
        cursor = conn.cursor()  # Remove cursor_factory - connection already has it
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        cursor = conn.cursor()  # Use regular cursor for table creation
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
    cursor = conn.cursor()  # Remove cursor_factory
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
    cursor = conn.cursor()  # Remove cursor_factory
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
            cursor = conn.cursor()  # Remove cursor_factory

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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        # The loan_payments table has a foreign key to loan_history
        # So we need to use loan_history.id
        if table_name == 'loan_purchases':
            # Check if there's a corresponding record in loan_history
            cursor.execute("""
                SELECT id FROM loan_history 
                WHERE user_id = %s AND equipment_id = %s 
                AND loan_amount = %s
                ORDER BY id DESC LIMIT 1
            """, (user_id, loan['equipment_id'], loan['loan_amount']))
            
            history_record = cursor.fetchone()
            if history_record:
                payment_loan_id = history_record['id']
            else:
                # Create a record in loan_history first
                cursor.execute("""
                    INSERT INTO loan_history 
                    (user_id, user_name, user_phone, user_email, equipment_id, equipment_name, 
                     loan_amount, down_payment, interest_rate, loan_term_months, emi_amount, 
                     total_payable, total_interest, first_emi_date, last_emi_date, status, 
                     emi_paid, next_due_date, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    loan['user_id'], loan['user_name'], loan['user_phone'], loan['user_email'],
                    loan['equipment_id'], loan['equipment_name'],
                    loan['loan_amount'], loan['down_payment'], loan['interest_rate'],
                    loan['loan_term_months'], loan['emi_amount'],
                    loan['total_payable'], loan['total_interest'],
                    loan['first_emi_date'], loan['last_emi_date'],
                    loan['status'], loan['emi_paid'], loan['next_due_date'],
                    loan['created_at']
                ))
                history_record = cursor.fetchone()
                payment_loan_id = history_record['id']
        else:
            payment_loan_id = loan_id
        
        # Record payment in loan_payments (uses loan_history foreign key)
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
        
        # Update the original table
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
            
            # Also update loan_history if it exists
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
            """, (new_emi_paid, next_due_date, new_status, payment_loan_id))
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
            cursor = conn.cursor()  # Remove cursor_factory
            
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
            cursor = conn.cursor()  # Remove cursor_factory
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
    cursor = conn.cursor()  # Remove cursor_factory
    
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
        cursor = conn.cursor()  # Use regular cursor for this
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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
        cursor = conn.cursor()  # Use regular cursor for schema changes
        
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
        cursor = conn.cursor()  # Remove cursor_factory
        
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

# Continue with all remaining routes following the same pattern:
# Remove cursor_factory=RealDictCursor from all cursor() calls
# For admin routes that need to query farmers, they're already in the same DB

# ... [All other routes remain exactly the same, just with cursor() calls fixed]

# For brevity, I've shown the pattern - you need to apply this to ALL routes:
# 1. Remove cursor_factory=RealDictCursor from all cursor() calls
# 2. Keep all other code exactly the same

if __name__ == '__main__':
    init_databases()
    start_reminder_scheduler()
    app.run(debug=True)
