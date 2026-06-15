from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify
from flask_mysqldb import PyMySQL
from functools import wraps
import hashlib, os, random, string
from datetime import datetime, date, timedelta
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'meditrack-secret-2025-change-in-production')

# ── DATABASE CONFIG ──
app.config['MYSQL_HOST']     = os.environ.get('DB_HOST', 'localhost')
app.config['MYSQL_USER']     = os.environ.get('DB_USER', 'root')
app.config['MYSQL_PASSWORD'] = os.environ.get('DB_PASS', '')
app.config['MYSQL_DB']       = os.environ.get('DB_NAME', 'meditrack_saas')
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

# ── UPLOAD CONFIG ──
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB

mysql = MySQL(app)

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def md5(s):
    return hashlib.md5(s.encode()).hexdigest()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_hospital(slug):
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM hospitals WHERE slug=%s AND is_active=1", (slug,))
    return cur.fetchone()

def plan_allows(hospital_id, feature):
    cur = mysql.connection.cursor()
    cur.execute(f"SELECT p.{feature} FROM hospitals h JOIN plans p ON h.plan_id=p.plan_id WHERE h.hospital_id=%s", (hospital_id,))
    row = cur.fetchone()
    return bool(row and row.get(feature))

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('loggedin'):
            slug = session.get('hospital_slug', '')
            return redirect(url_for('login', slug=slug))
        return f(*args, **kwargs)
    return decorated

def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get('loggedin'):
                return redirect(url_for('login'))
            if session.get('role') not in roles:
                return redirect(url_for('dashboard', slug=session.get('hospital_slug', '')))
            return f(*args, **kwargs)
        return decorated
    return decorator

def require_platform_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('platform_admin'):
            return redirect(url_for('platform_admin'))
        return f(*args, **kwargs)
    return decorated

def save_upload(file, subfolder):
    if file and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        fname = f"{''.join(random.choices(string.ascii_lowercase+string.digits,k=12))}.{ext}"
        folder = os.path.join(app.config['UPLOAD_FOLDER'], subfolder)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, fname)
        file.save(path)
        return f"uploads/{subfolder}/{fname}"
    return None

def get_doctor_id_for_user():
    role = session.get('role')
    if role == 'doctor':
        return session.get('linked_id', 0)
    if role == 'assistant':
        cur = mysql.connection.cursor()
        cur.execute("SELECT doctor_id FROM assistants WHERE assistant_id=%s AND hospital_id=%s",
                    (session.get('linked_id'), session.get('hospital_id')))
        row = cur.fetchone()
        return row['doctor_id'] if row else 0
    return 0

# ─────────────────────────────────────────────────────────────
# INDEX / LANDING PAGE
# ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    cur = mysql.connection.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM hospitals WHERE is_active=1")
    h_count = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) AS c FROM doctors WHERE is_active=1")
    d_count = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) AS c FROM patients")
    p_count = cur.fetchone()['c']
    cur.execute("SELECT h.name, h.slug, pl.name AS plan_name FROM hospitals h JOIN plans pl ON h.plan_id=pl.plan_id WHERE h.is_active=1 AND h.is_verified=1 ORDER BY h.created_at DESC LIMIT 4")
    featured = cur.fetchall()
    cur.execute("SELECT * FROM plans WHERE is_active=1 ORDER BY plan_id")
    plans = cur.fetchall()
    return render_template('index.html', h_count=h_count, d_count=d_count, p_count=p_count,
                           featured=featured, plans=plans)

# ─────────────────────────────────────────────────────────────
# FIND HOSPITAL
# ─────────────────────────────────────────────────────────────
@app.route('/find')
def find_hospital():
    q = request.args.get('q', '').strip()
    cur = mysql.connection.cursor()
    if q:
        cur.execute("""SELECT h.*, pl.name AS plan_name, COUNT(DISTINCT d.doctor_id) AS doctor_count
            FROM hospitals h JOIN plans pl ON h.plan_id=pl.plan_id
            LEFT JOIN doctors d ON d.hospital_id=h.hospital_id AND d.is_active=1
            WHERE h.is_active=1 AND (h.name LIKE %s OR h.address LIKE %s OR h.slug LIKE %s)
            GROUP BY h.hospital_id ORDER BY h.name""", (f'%{q}%',f'%{q}%',f'%{q}%'))
    else:
        cur.execute("""SELECT h.*, pl.name AS plan_name, COUNT(DISTINCT d.doctor_id) AS doctor_count
            FROM hospitals h JOIN plans pl ON h.plan_id=pl.plan_id
            LEFT JOIN doctors d ON d.hospital_id=h.hospital_id AND d.is_active=1
            WHERE h.is_active=1 GROUP BY h.hospital_id ORDER BY h.created_at DESC LIMIT 20""")
    hospitals = cur.fetchall()
    return render_template('find_hospital.html', hospitals=hospitals, q=q)

# ─────────────────────────────────────────────────────────────
# HOSPITAL PUBLIC PAGE
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>')
def hospital_page(slug):
    h = get_hospital(slug)
    if not h:
        return redirect(url_for('find_hospital'))
    hid = h['hospital_id']
    today = datetime.now().strftime('%A')
    cur = mysql.connection.cursor()
    cur.execute("""SELECT d.*, s.spec_name,
        ds.start_time, ds.end_time, ds.slot_minutes, ds.max_patients,
        (SELECT COUNT(*) FROM appointments a WHERE a.doctor_id=d.doctor_id AND a.appt_date=CURDATE() AND a.status NOT IN ('Cancelled')) AS booked,
        (SELECT COUNT(*) FROM doctor_leaves dl WHERE dl.doctor_id=d.doctor_id AND dl.leave_date=CURDATE()) AS on_leave
        FROM doctors d LEFT JOIN specializations s ON d.spec_id=s.spec_id
        LEFT JOIN doctor_schedules ds ON ds.doctor_id=d.doctor_id AND ds.day_of_week=%s AND ds.is_active=1
        WHERE d.hospital_id=%s AND d.is_active=1 ORDER BY d.name""", (today, hid))
    doctors = cur.fetchall()
    return render_template('hospital.html', h=h, doctors=doctors, today=today)

# ─────────────────────────────────────────────────────────────
# BOOK APPOINTMENT (Public)
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/book', methods=['GET','POST'])
def book_appt(slug):
    h = get_hospital(slug)
    if not h:
        return redirect(url_for('find_hospital'))
    hid = h['hospital_id']
    cur = mysql.connection.cursor()
    cur.execute("SELECT d.doctor_id,d.name,d.chamber_no,d.visit_fee,s.spec_name FROM doctors d LEFT JOIN specializations s ON d.spec_id=s.spec_id WHERE d.hospital_id=%s AND d.is_active=1 ORDER BY d.name", (hid,))
    doctors = cur.fetchall()

    selected_doctor = int(request.args.get('doctor_id', 0))
    selected_date   = request.args.get('date', date.today().isoformat())
    if selected_date < date.today().isoformat():
        selected_date = date.today().isoformat()

    schedule = None; available_slots = []; booked_slots = []
    if selected_doctor:
        day = datetime.strptime(selected_date, '%Y-%m-%d').strftime('%A')
        cur.execute("SELECT * FROM doctor_schedules WHERE doctor_id=%s AND hospital_id=%s AND day_of_week=%s AND is_active=1", (selected_doctor, hid, day))
        sch = cur.fetchone()
        if sch:
            cur.execute("SELECT leave_id FROM doctor_leaves WHERE doctor_id=%s AND hospital_id=%s AND leave_date=%s", (selected_doctor, hid, selected_date))
            if not cur.fetchone():
                schedule = sch
                from datetime import time as dtime
                start = datetime.combine(date.today(), sch['start_time']) if hasattr(sch['start_time'], 'hour') else datetime.strptime(str(sch['start_time']), '%H:%M:%S')
                end   = datetime.combine(date.today(), sch['end_time'])   if hasattr(sch['end_time'], 'hour')   else datetime.strptime(str(sch['end_time']),   '%H:%M:%S')
                mins  = int(sch['slot_minutes'])
                cur2  = start
                while cur2 < end:
                    available_slots.append(cur2.strftime('%H:%M'))
                    cur2 += timedelta(minutes=mins)
                cur.execute("SELECT TIME_FORMAT(appt_time,'%%H:%%i') AS t FROM appointments WHERE doctor_id=%s AND hospital_id=%s AND appt_date=%s AND status NOT IN ('Cancelled')", (selected_doctor, hid, selected_date))
                booked_slots = [r['t'] for r in cur.fetchall()]

    success = False; msg = ''
    if request.method == 'POST':
        did   = int(request.form.get('doctor_id', 0))
        dt    = request.form.get('appt_date', '')
        tm    = request.form.get('appt_time', '')
        pname = request.form.get('patient_name', '').strip()
        phone = request.form.get('patient_phone', '').strip()
        reason= request.form.get('reason', '').strip()
        if not phone.isdigit() or len(phone) != 11:
            msg = 'Phone must be 11 digits.'
        else:
            cur.execute("SELECT appt_id FROM appointments WHERE doctor_id=%s AND hospital_id=%s AND appt_date=%s AND appt_time=%s AND status NOT IN ('Cancelled')", (did, hid, dt, tm))
            if cur.fetchone():
                msg = 'This slot is already taken. Please choose another.'
            else:
                cur.execute("SELECT patient_id FROM patients WHERE phone=%s AND hospital_id=%s", (phone, hid))
                prow = cur.fetchone()
                pid = prow['patient_id'] if prow else None
                cur.execute("INSERT INTO appointments (hospital_id,patient_id,doctor_id,appt_date,appt_time,patient_name,patient_phone,reason,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'Pending')", (hid, pid, did, dt, tm, pname, phone, reason))
                mysql.connection.commit()
                success = True

    return render_template('book_appt.html', h=h, doctors=doctors, selected_doctor=selected_doctor,
                           selected_date=selected_date, schedule=schedule, available_slots=available_slots,
                           booked_slots=booked_slots, success=success, msg=msg)

# ─────────────────────────────────────────────────────────────
# REGISTER HOSPITAL
# ─────────────────────────────────────────────────────────────
@app.route('/register', methods=['GET','POST'])
def register_hospital():
    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM plans WHERE is_active=1 ORDER BY plan_id")
    plans = cur.fetchall()
    plan_id = int(request.args.get('plan', 1))
    msg = ''
    if request.method == 'POST':
        name   = request.form.get('name','').strip()
        email  = request.form.get('email','').strip()
        phone  = request.form.get('phone','').strip()
        addr   = request.form.get('address','').strip()
        pid    = int(request.form.get('plan_id', 1))
        uname  = request.form.get('admin_username','').strip()
        passw  = request.form.get('admin_password','')
        import re
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        if not all([name, email, phone, uname, passw]):
            msg = 'All fields are required.'
        elif len(passw) < 6:
            msg = 'Password must be at least 6 characters.'
        else:
            cur.execute("SELECT hospital_id FROM hospitals WHERE email=%s OR slug=%s", (email, slug))
            if cur.fetchone():
                msg = 'Email already registered or hospital name taken.'
            else:
                expires = None if pid == 1 else (date.today() + timedelta(days=30)).isoformat()
                cur.execute("INSERT INTO hospitals (name,slug,email,phone,address,plan_id,plan_expires,is_active,is_verified,primary_color) VALUES (%s,%s,%s,%s,%s,%s,%s,1,0,'#0d9488')",
                            (name, slug, email, phone, addr, pid, expires))
                hid = cur.lastrowid
                cur.execute("INSERT INTO users (hospital_id,username,phone,email,password,role,full_name) VALUES (%s,%s,%s,%s,%s,'hospital_admin',%s)",
                            (hid, uname, phone, email, md5(passw), f'{name} Admin'))
                mysql.connection.commit()
                session['reg_hospital_slug'] = slug
                session['reg_hospital_name'] = name
                return redirect(url_for('register_success', slug=slug))
    return render_template('register_hospital.html', plans=plans, plan_id=plan_id, msg=msg)

@app.route('/register/success')
def register_success():
    slug = request.args.get('slug') or session.get('reg_hospital_slug','')
    name = session.get('reg_hospital_name', 'Your Hospital')
    return render_template('register_success.html', slug=slug, name=name)

# ─────────────────────────────────────────────────────────────
# LOGIN / LOGOUT
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/login', methods=['GET','POST'])
def login(slug):
    h = get_hospital(slug)
    msg = ''
    if request.args.get('reset'):
        msg = 'success:Password reset! Login with your new password.'
    if request.method == 'POST' and h:
        hid   = h['hospital_id']
        uname = request.form.get('username','').strip()
        passw = md5(request.form.get('password',''))
        cur = mysql.connection.cursor()
        cur.execute("""SELECT u.*, d.doctor_id, a.assistant_id, p2.patient_id AS pat_id
            FROM users u
            LEFT JOIN doctors d ON d.user_id=u.id AND d.hospital_id=u.hospital_id
            LEFT JOIN assistants a ON a.user_id=u.id AND a.hospital_id=u.hospital_id
            LEFT JOIN patients p2 ON p2.user_id=u.id AND p2.hospital_id=u.hospital_id
            WHERE u.hospital_id=%s AND u.username=%s AND u.password=%s AND u.is_active=1""",
                    (hid, uname, passw))
        row = cur.fetchone()
        if row:
            session['loggedin']      = True
            session['user_id']       = row['id']
            session['username']      = row['username']
            session['full_name']     = row['full_name']
            session['role']          = row['role']
            session['hospital_id']   = hid
            session['hospital_slug'] = slug
            role = row['role']
            if role == 'doctor':      session['linked_id'] = row['doctor_id']
            elif role == 'assistant': session['linked_id'] = row['assistant_id']
            elif role == 'patient':   session['linked_id'] = row['pat_id']
            else:                     session['linked_id'] = None
            if role == 'patient':
                return redirect(url_for('my_profile', slug=slug))
            return redirect(url_for('dashboard', slug=slug))
        else:
            msg = 'error:Incorrect username or password.'
    return render_template('login.html', h=h, slug=slug, msg=msg)

@app.route('/h/<slug>/logout')
def logout(slug):
    session.clear()
    return redirect(url_for('login', slug=slug))

# ─────────────────────────────────────────────────────────────
# PATIENT REGISTER
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/register', methods=['GET','POST'])
def patient_register(slug):
    h = get_hospital(slug)
    if not h: return redirect(url_for('find_hospital'))
    hid = h['hospital_id']
    msg = ''; success = ''
    if request.method == 'POST':
        uname = request.form.get('username','').strip()
        phone = request.form.get('phone','').strip()
        passw = request.form.get('password','')
        if not phone.isdigit() or len(phone) != 11:
            msg = 'Phone must be 11 digits.'
        elif len(passw) < 6:
            msg = 'Password must be at least 6 characters.'
        else:
            cur = mysql.connection.cursor()
            cur.execute("SELECT patient_id FROM patients WHERE phone=%s AND hospital_id=%s", (phone, hid))
            prow = cur.fetchone()
            if not prow:
                msg = 'Phone not found in our patient records. Please contact the clinic first.'
            else:
                pid = prow['patient_id']
                cur.execute("SELECT id FROM users WHERE (username=%s OR phone=%s) AND hospital_id=%s", (uname, phone, hid))
                if cur.fetchone():
                    msg = 'Username or phone already registered.'
                else:
                    cur.execute("INSERT INTO users (hospital_id,username,phone,password,role) VALUES (%s,%s,%s,%s,'patient')",
                                (hid, uname, phone, md5(passw)))
                    uid = cur.lastrowid
                    cur.execute("UPDATE patients SET user_id=%s WHERE patient_id=%s AND hospital_id=%s", (uid, pid, hid))
                    mysql.connection.commit()
                    success = 'Account created!'
    return render_template('patient_register.html', h=h, slug=slug, msg=msg, success=success)

# ─────────────────────────────────────────────────────────────
# FORGOT PASSWORD (OTP Flow)
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/forgot', methods=['GET','POST'])
def forgot_password(slug):
    h = get_hospital(slug)
    hid = h['hospital_id'] if h else 0
    step = session.get('otp_step', 1)
    msg = ''
    if request.args.get('restart'):
        session.pop('otp_step', None); session.pop('otp_phone', None)
        session.pop('otp_verified', None); session.pop('otp_demo', None)
        return redirect(url_for('forgot_password', slug=slug))

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'send_otp':
            phone = request.form.get('phone','').strip()
            if not phone.isdigit() or len(phone) != 11:
                msg = 'Enter a valid 11-digit phone number.'
            else:
                cur = mysql.connection.cursor()
                cur.execute("SELECT id FROM users WHERE phone=%s AND is_active=1 AND hospital_id=%s", (phone, hid))
                if not cur.fetchone():
                    msg = 'No account found with this phone number.'
                else:
                    otp = str(random.randint(100000, 999999))
                    expires = (datetime.now() + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
                    cur.execute("UPDATE otp_codes SET used=1 WHERE phone=%s AND used=0", (phone,))
                    cur.execute("INSERT INTO otp_codes (phone,otp,purpose,hospital_id,expires_at) VALUES (%s,%s,'reset_password',%s,%s)",
                                (phone, otp, hid, expires))
                    mysql.connection.commit()
                    session['otp_phone'] = phone; session['otp_step'] = 2; session['otp_demo'] = otp
                    return redirect(url_for('forgot_password', slug=slug))
        elif action == 'verify_otp':
            phone = session.get('otp_phone','')
            otp   = request.form.get('otp','').strip()
            cur   = mysql.connection.cursor()
            now   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cur.execute("SELECT id FROM otp_codes WHERE phone=%s AND otp=%s AND used=0 AND expires_at>%s ORDER BY id DESC LIMIT 1", (phone, otp, now))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE otp_codes SET used=1 WHERE id=%s", (row['id'],))
                mysql.connection.commit()
                session['otp_step'] = 3; session['otp_verified'] = True
                return redirect(url_for('forgot_password', slug=slug))
            else:
                msg = 'Invalid or expired OTP. Try again.'; step = 2
        elif action == 'reset_password':
            if not session.get('otp_verified'):
                return redirect(url_for('forgot_password', slug=slug))
            p1 = request.form.get('password','')
            p2 = request.form.get('password2','')
            if len(p1) < 6: msg = 'Password must be at least 6 characters.'; step = 3
            elif p1 != p2:  msg = 'Passwords do not match.'; step = 3
            else:
                phone = session.get('otp_phone','')
                cur = mysql.connection.cursor()
                cur.execute("UPDATE users SET password=%s WHERE phone=%s AND hospital_id=%s", (md5(p1), phone, hid))
                mysql.connection.commit()
                session.pop('otp_step', None); session.pop('otp_phone', None)
                session.pop('otp_verified', None); session.pop('otp_demo', None)
                return redirect(url_for('login', slug=slug, reset=1))
    step = session.get('otp_step', 1)
    return render_template('forgot_password.html', h=h, slug=slug, step=step, msg=msg,
                           otp_demo=session.get('otp_demo'), otp_phone=session.get('otp_phone',''))

# ─────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/dashboard')
@require_login
def dashboard(slug):
    hid = session['hospital_id']
    role = session['role']
    doctor_id = get_doctor_id_for_user()
    cur = mysql.connection.cursor()
    h = get_hospital(slug)
    doc_scope = f"AND doctor_id={doctor_id}" if doctor_id else ""
    today = date.today().isoformat()

    def q(sql): cur.execute(sql); return cur.fetchone()
    stats = {
        'patients':       q(f"SELECT COUNT(*) AS c FROM patients WHERE hospital_id={hid} {doc_scope}")['c'],
        'doctors':        q(f"SELECT COUNT(*) AS c FROM doctors WHERE hospital_id={hid} AND is_active=1")['c'],
        'prescriptions':  q(f"SELECT COUNT(*) AS c FROM prescriptions WHERE hospital_id={hid} {doc_scope}")['c'],
        'today_appts':    q(f"SELECT COUNT(*) AS c FROM appointments WHERE hospital_id={hid} {doc_scope} AND appt_date=CURDATE() AND status NOT IN ('Cancelled')")['c'],
        'pending_appts':  q(f"SELECT COUNT(*) AS c FROM appointments WHERE hospital_id={hid} {doc_scope} AND status='Pending'")['c'],
        'monthly_revenue':q(f"SELECT COALESCE(SUM(paid_amount),0) AS s FROM invoices WHERE hospital_id={hid} AND MONTH(visit_date)=MONTH(CURDATE()) AND YEAR(visit_date)=YEAR(CURDATE())")['s'],
    }
    doc_filter = f"AND p.doctor_id={doctor_id}" if doctor_id else ""
    cur.execute(f"SELECT p.*, d.name AS doctor_name FROM patients p LEFT JOIN doctors d ON p.doctor_id=d.doctor_id WHERE p.hospital_id={hid} {doc_filter} ORDER BY p.created_at DESC LIMIT 6")
    recent_patients = cur.fetchall()
    cur.execute(f"SELECT a.*, d.name AS doctor_name FROM appointments a JOIN doctors d ON a.doctor_id=d.doctor_id WHERE a.hospital_id={hid} AND a.appt_date=CURDATE() {doc_scope} ORDER BY a.appt_time ASC LIMIT 8")
    today_appts = cur.fetchall()
    has_lab = plan_allows(hid, 'has_lab')
    has_inv = plan_allows(hid, 'has_invoice')
    return render_template('dashboard.html', h=h, slug=slug, stats=stats, role=role,
                           recent_patients=recent_patients, today_appts=today_appts,
                           has_lab=has_lab, has_inv=has_inv)

# ─────────────────────────────────────────────────────────────
# PATIENTS
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/patients', methods=['GET','POST'])
@require_login
def patients(slug):
    hid = session['hospital_id']; role = session['role']
    doctor_id = get_doctor_id_for_user()
    assistant_id = session.get('linked_id') if role == 'assistant' else 0
    h = get_hospital(slug); cur = mysql.connection.cursor()
    msg = ''

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add' and role in ('hospital_admin','assistant'):
            fn   = request.form.get('full_name','').strip()
            age  = int(request.form.get('age', 0))
            gen  = request.form.get('gender','')
            ph   = request.form.get('phone','').strip()
            zilla= request.form.get('zilla','').strip()
            upoz = request.form.get('upozila','').strip()
            bg   = request.form.get('blood_group','')
            did  = int(request.form.get('doctor_id', doctor_id or 0))
            notes= request.form.get('notes','').strip()
            ab   = assistant_id or None
            if not ph.isdigit() or len(ph) != 11:
                msg = 'Phone must be 11 digits.'
            else:
                cur.execute("SELECT patient_id FROM patients WHERE phone=%s AND hospital_id=%s", (ph, hid))
                if cur.fetchone():
                    msg = 'A patient with this phone already exists.'
                else:
                    cur.execute("INSERT INTO patients (hospital_id,doctor_id,added_by,full_name,age,gender,phone,zilla,upozila,blood_group,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                (hid, did, ab, fn, age, gen, ph, zilla, upoz, bg, notes))
                    mysql.connection.commit()
                    return redirect(url_for('patients', slug=slug, msg='added'))
        elif action == 'update' and role in ('hospital_admin','assistant'):
            pid  = int(request.form.get('patient_id',0))
            fn   = request.form.get('full_name','').strip()
            age  = int(request.form.get('age',0))
            gen  = request.form.get('gender','')
            ph   = request.form.get('phone','').strip()
            zilla= request.form.get('zilla','').strip()
            upoz = request.form.get('upozila','').strip()
            bg   = request.form.get('blood_group','')
            did  = int(request.form.get('doctor_id', doctor_id or 0))
            notes= request.form.get('notes','').strip()
            cur.execute("UPDATE patients SET full_name=%s,age=%s,gender=%s,phone=%s,zilla=%s,upozila=%s,blood_group=%s,doctor_id=%s,notes=%s WHERE patient_id=%s AND hospital_id=%s",
                        (fn, age, gen, ph, zilla, upoz, bg, did, notes, pid, hid))
            mysql.connection.commit()
            return redirect(url_for('patients', slug=slug, msg='updated'))

    if request.args.get('delete') and role == 'hospital_admin':
        pid = int(request.args.get('delete',0))
        cur.execute("DELETE FROM patients WHERE patient_id=%s AND hospital_id=%s", (pid, hid))
        mysql.connection.commit()
        return redirect(url_for('patients', slug=slug, msg='deleted'))

    msgs = {'added':'Patient added!','updated':'Patient updated.','deleted':'Patient deleted.'}
    if request.args.get('msg'):
        msg = msgs.get(request.args.get('msg'), '')

    edit_id = int(request.args.get('edit', 0)); edit_data = None
    if edit_id:
        scope = f"AND doctor_id={doctor_id}" if doctor_id else ""
        cur.execute(f"SELECT * FROM patients WHERE patient_id=%s AND hospital_id=%s {scope}", (edit_id, hid))
        edit_data = cur.fetchone()

    search = request.args.get('search','').strip()
    scope_w = f"WHERE p.hospital_id={hid}"
    if doctor_id: scope_w += f" AND p.doctor_id={doctor_id}"
    if search:    scope_w += f" AND (p.full_name LIKE '%{search}%' OR p.phone LIKE '%{search}%')"
    cur.execute(f"SELECT p.*, d.name AS doctor_name FROM patients p LEFT JOIN doctors d ON p.doctor_id=d.doctor_id {scope_w} ORDER BY p.created_at DESC")
    patient_list = cur.fetchall()
    cur.execute("SELECT doctor_id,name FROM doctors WHERE hospital_id=%s AND is_active=1 ORDER BY name", (hid,))
    doctors = cur.fetchall()

    # Bangladesh locations JSON
    import json
    bd_loc = get_bd_locations()
    return render_template('patients.html', h=h, slug=slug, role=role, msg=msg,
                           patients=patient_list, doctors=doctors, edit_id=edit_id, edit_data=edit_data,
                           search=search, doctor_id=doctor_id, bd_locations=json.dumps(bd_loc))

# ─────────────────────────────────────────────────────────────
# PATIENT PROFILE
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/patient/<int:pid>')
@require_login
def patient_profile(slug, pid):
    hid = session['hospital_id']; role = session['role']
    doctor_id = get_doctor_id_for_user()
    h = get_hospital(slug); cur = mysql.connection.cursor()
    if role == 'patient':
        pid = session.get('linked_id', 0)
    else:
        scope = f"AND doctor_id={doctor_id}" if doctor_id else ""
        cur.execute(f"SELECT patient_id FROM patients WHERE patient_id=%s AND hospital_id=%s {scope}", (pid, hid))
        if not cur.fetchone():
            return "Access denied", 403
    cur.execute("SELECT p.*,d.name AS doctor_name,s.spec_name FROM patients p LEFT JOIN doctors d ON p.doctor_id=d.doctor_id LEFT JOIN specializations s ON d.spec_id=s.spec_id WHERE p.patient_id=%s AND p.hospital_id=%s", (pid, hid))
    pt = cur.fetchone()
    if not pt: return "Patient not found", 404
    cur.execute("SELECT pr.*,COUNT(pm.med_id) AS med_count FROM prescriptions pr LEFT JOIN prescription_medicines pm ON pm.prescription_id=pr.prescription_id WHERE pr.patient_id=%s AND pr.hospital_id=%s GROUP BY pr.prescription_id ORDER BY pr.visit_date DESC", (pid, hid))
    prescriptions = cur.fetchall()
    cur.execute("SELECT * FROM lab_reports WHERE patient_id=%s AND hospital_id=%s ORDER BY test_date DESC", (pid, hid))
    labs = cur.fetchall()
    cur.execute("SELECT * FROM invoices WHERE patient_id=%s AND hospital_id=%s ORDER BY visit_date DESC", (pid, hid))
    invoices = cur.fetchall()
    cur.execute("SELECT a.*,d.name AS doctor_name FROM appointments a JOIN doctors d ON a.doctor_id=d.doctor_id WHERE a.patient_id=%s AND a.hospital_id=%s ORDER BY a.appt_date DESC LIMIT 5", (pid, hid))
    appointments = cur.fetchall()
    for rx in prescriptions:
        cur.execute("SELECT * FROM prescription_medicines WHERE prescription_id=%s ORDER BY med_id", (rx['prescription_id'],))
        rx['medicines'] = cur.fetchall()
    has_inv = plan_allows(hid, 'has_invoice')
    has_lab = plan_allows(hid, 'has_lab')
    return render_template('patient_profile.html', h=h, slug=slug, pt=pt, role=role,
                           prescriptions=prescriptions, labs=labs, invoices=invoices,
                           appointments=appointments, has_inv=has_inv, has_lab=has_lab)

@app.route('/h/<slug>/profile')
@require_login
def my_profile(slug):
    pid = session.get('linked_id', 0)
    return redirect(url_for('patient_profile', slug=slug, pid=pid))

# ─────────────────────────────────────────────────────────────
# DOCTORS
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/doctors', methods=['GET','POST'])
@require_login
def doctors(slug):
    hid = session['hospital_id']; role = session['role']
    if role != 'hospital_admin':
        return redirect(url_for('dashboard', slug=slug))
    h = get_hospital(slug); cur = mysql.connection.cursor()
    cur.execute("SELECT p.max_doctors FROM hospitals h JOIN plans p ON h.plan_id=p.plan_id WHERE h.hospital_id=%s", (hid,))
    plan = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS c FROM doctors WHERE hospital_id=%s AND is_active=1", (hid,))
    current_count = cur.fetchone()['c']
    can_add = current_count < plan['max_doctors']
    msg = ''
    if request.method == 'POST':
        if not can_add:
            msg = f"Plan limit reached ({plan['max_doctors']} doctors). Please upgrade."
        else:
            name  = request.form.get('name','').strip()
            sid   = int(request.form.get('spec_id',0))
            cham  = request.form.get('chamber_no','').strip()
            qual  = request.form.get('qualification','').strip()
            cont  = request.form.get('contact','').strip()
            email = request.form.get('email','').strip()
            fee   = int(request.form.get('visit_fee',0))
            bio   = request.form.get('bio','').strip()
            uname = request.form.get('username','').strip()
            passw = request.form.get('password','')
            cur.execute("SELECT id FROM users WHERE hospital_id=%s AND username=%s", (hid, uname))
            if cur.fetchone():
                msg = 'Username already taken.'
            else:
                cur.execute("INSERT INTO doctors (hospital_id,name,spec_id,chamber_no,qualification,contact,email,visit_fee,bio,is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)",
                            (hid, name, sid, cham, qual, cont, email, fee, bio))
                did = cur.lastrowid
                cur.execute("INSERT INTO users (hospital_id,username,phone,email,password,role,full_name) VALUES (%s,%s,%s,%s,%s,'doctor',%s)",
                            (hid, uname, cont, email, md5(passw), f'Dr. {name}'))
                uid = cur.lastrowid
                cur.execute("UPDATE doctors SET user_id=%s WHERE doctor_id=%s", (uid, did))
                mysql.connection.commit()
                return redirect(url_for('doctors', slug=slug, msg='added'))
    if request.args.get('toggle'):
        did = int(request.args.get('toggle'))
        cur.execute("SELECT is_active FROM doctors WHERE doctor_id=%s AND hospital_id=%s", (did, hid))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE doctors SET is_active=%s WHERE doctor_id=%s AND hospital_id=%s", (0 if row['is_active'] else 1, did, hid))
            mysql.connection.commit()
        return redirect(url_for('doctors', slug=slug))
    if request.args.get('msg') == 'added':
        msg = 'Doctor added and login account created!'
    cur.execute("SELECT * FROM specializations ORDER BY spec_name")
    specs = cur.fetchall()
    cur.execute("""SELECT d.*,s.spec_name,u.username,COUNT(DISTINCT p.patient_id) AS patient_count
        FROM doctors d LEFT JOIN specializations s ON d.spec_id=s.spec_id
        LEFT JOIN users u ON d.user_id=u.id LEFT JOIN patients p ON p.doctor_id=d.doctor_id
        WHERE d.hospital_id=%s GROUP BY d.doctor_id ORDER BY d.created_at DESC""", (hid,))
    doctor_list = cur.fetchall()
    return render_template('doctors.html', h=h, slug=slug, specs=specs, doctors=doctor_list,
                           can_add=can_add, plan=plan, current_count=current_count, msg=msg)

# ─────────────────────────────────────────────────────────────
# ASSISTANTS
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/assistants', methods=['GET','POST'])
@require_login
def assistants(slug):
    hid = session['hospital_id']; role = session['role']
    if role != 'hospital_admin': return redirect(url_for('dashboard', slug=slug))
    h = get_hospital(slug); cur = mysql.connection.cursor()
    msg = ''
    if request.method == 'POST':
        name  = request.form.get('name','').strip()
        did   = int(request.form.get('doctor_id',0))
        cont  = request.form.get('contact','').strip()
        email = request.form.get('email','').strip()
        uname = request.form.get('username','').strip()
        passw = request.form.get('password','')
        cur.execute("SELECT id FROM users WHERE hospital_id=%s AND username=%s", (hid, uname))
        if cur.fetchone():
            msg = 'Username already taken.'
        else:
            cur.execute("INSERT INTO assistants (hospital_id,doctor_id,name,contact,email) VALUES (%s,%s,%s,%s,%s)", (hid, did, name, cont, email))
            aid = cur.lastrowid
            cur.execute("INSERT INTO users (hospital_id,username,phone,email,password,role,full_name) VALUES (%s,%s,%s,%s,%s,'assistant',%s)",
                        (hid, uname, cont, email, md5(passw), name))
            uid = cur.lastrowid
            cur.execute("UPDATE assistants SET user_id=%s WHERE assistant_id=%s", (uid, aid))
            mysql.connection.commit()
            return redirect(url_for('assistants', slug=slug, msg='added'))
    if request.args.get('msg'): msg = 'Assistant added and login created!'
    cur.execute("SELECT doctor_id,name FROM doctors WHERE hospital_id=%s AND is_active=1 ORDER BY name", (hid,))
    doctor_list = cur.fetchall()
    cur.execute("""SELECT a.*,d.name AS doctor_name,u.username FROM assistants a
        LEFT JOIN doctors d ON a.doctor_id=d.doctor_id LEFT JOIN users u ON a.user_id=u.id
        WHERE a.hospital_id=%s ORDER BY a.created_at DESC""", (hid,))
    assistant_list = cur.fetchall()
    return render_template('assistants.html', h=h, slug=slug, doctors=doctor_list, assistants=assistant_list, msg=msg)

# ─────────────────────────────────────────────────────────────
# PRESCRIPTIONS
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/prescriptions', methods=['GET','POST'])
@require_login
def prescriptions(slug):
    hid = session['hospital_id']; role = session['role']
    if role not in ('hospital_admin','doctor'): return redirect(url_for('dashboard', slug=slug))
    doctor_id = get_doctor_id_for_user()
    h = get_hospital(slug); cur = mysql.connection.cursor()
    if request.method == 'POST':
        pid       = int(request.form.get('patient_id',0))
        did       = int(request.form.get('doctor_id', doctor_id or 0))
        vdate     = request.form.get('visit_date','')
        complaint = request.form.get('chief_complaint','').strip()
        diagnosis = request.form.get('diagnosis','').strip()
        notes     = request.form.get('notes','').strip()
        followup  = request.form.get('follow_up_date') or None
        scope     = f"AND doctor_id={did}" if did else ""
        cur.execute(f"SELECT patient_id FROM patients WHERE patient_id=%s AND hospital_id=%s {scope}", (pid, hid))
        if not cur.fetchone():
            flash('Patient not found or access denied.', 'error')
        else:
            cur.execute("INSERT INTO prescriptions (hospital_id,patient_id,doctor_id,visit_date,chief_complaint,diagnosis,notes,follow_up_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (hid, pid, did, vdate, complaint, diagnosis, notes, followup))
            rx_id = cur.lastrowid
            meds  = request.form.getlist('medicine_name[]')
            dosages=request.form.getlist('dosage[]')
            freqs  =request.form.getlist('frequency[]')
            whens  =request.form.getlist('when_to_take[]')
            durs   =request.form.getlist('duration[]')
            instrs =request.form.getlist('instructions[]')
            for i, med in enumerate(meds):
                if med.strip():
                    cur.execute("INSERT INTO prescription_medicines (prescription_id,medicine_name,dosage,frequency,when_to_take,duration,instructions) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                                (rx_id, med, dosages[i] if i<len(dosages) else '', freqs[i] if i<len(freqs) else '',
                                 whens[i] if i<len(whens) else '', durs[i] if i<len(durs) else '', instrs[i] if i<len(instrs) else ''))
            mysql.connection.commit()
            return redirect(url_for('view_prescription', slug=slug, rx_id=rx_id, msg='saved'))

    scope_pt = f"AND doctor_id={doctor_id}" if doctor_id else ""
    cur.execute(f"SELECT patient_id,full_name,phone FROM patients WHERE hospital_id={hid} {scope_pt} ORDER BY full_name")
    patient_list = cur.fetchall()
    cur.execute("SELECT doctor_id,name FROM doctors WHERE hospital_id=%s AND is_active=1 ORDER BY name", (hid,)) if role == 'hospital_admin' else None
    doctors_dd = cur.fetchall() if role == 'hospital_admin' else []
    scope_rx = f"AND pr.doctor_id={doctor_id}" if doctor_id else ""
    cur.execute(f"""SELECT pr.*,p.full_name AS patient_name,d.name AS doctor_name,COUNT(pm.med_id) AS med_count
        FROM prescriptions pr JOIN patients p ON pr.patient_id=p.patient_id JOIN doctors d ON pr.doctor_id=d.doctor_id
        LEFT JOIN prescription_medicines pm ON pm.prescription_id=pr.prescription_id
        WHERE pr.hospital_id={hid} {scope_rx} GROUP BY pr.prescription_id ORDER BY pr.visit_date DESC,pr.prescription_id DESC""")
    rx_list = cur.fetchall()
    pre_pid = int(request.args.get('patient_id', 0))
    return render_template('prescriptions.html', h=h, slug=slug, role=role, patients=patient_list,
                           doctors_dd=doctors_dd, rx_list=rx_list, doctor_id=doctor_id, pre_pid=pre_pid)

@app.route('/h/<slug>/prescription/<int:rx_id>')
@require_login
def view_prescription(slug, rx_id):
    hid = session['hospital_id']; role = session['role']
    doctor_id = get_doctor_id_for_user()
    h = get_hospital(slug); cur = mysql.connection.cursor()
    if role == 'patient':
        cur.execute("SELECT * FROM prescriptions WHERE prescription_id=%s AND hospital_id=%s AND patient_id=%s", (rx_id, hid, session.get('linked_id')))
    elif doctor_id:
        cur.execute("SELECT * FROM prescriptions WHERE prescription_id=%s AND hospital_id=%s AND doctor_id=%s", (rx_id, hid, doctor_id))
    else:
        cur.execute("SELECT * FROM prescriptions WHERE prescription_id=%s AND hospital_id=%s", (rx_id, hid))
    presc = cur.fetchone()
    if not presc: return "Prescription not found", 404
    cur.execute("SELECT * FROM patients WHERE patient_id=%s AND hospital_id=%s", (presc['patient_id'], hid))
    pt = cur.fetchone()
    cur.execute("SELECT d.*,s.spec_name FROM doctors d LEFT JOIN specializations s ON d.spec_id=s.spec_id WHERE d.doctor_id=%s", (presc['doctor_id'],))
    dr = cur.fetchone()
    cur.execute("SELECT * FROM prescription_medicines WHERE prescription_id=%s ORDER BY med_id", (rx_id,))
    meds = cur.fetchall()
    return render_template('view_prescription.html', h=h, slug=slug, presc=presc, pt=pt, dr=dr, meds=meds, rx_id=rx_id)

# ─────────────────────────────────────────────────────────────
# LAB REPORTS
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/lab-reports', methods=['GET','POST'])
@require_login
def lab_reports(slug):
    hid = session['hospital_id']; role = session['role']
    if not plan_allows(hid, 'has_lab'):
        return render_template('plan_gate.html', h=get_hospital(slug), slug=slug, feature='Lab Reports', plan_name='Basic')
    doctor_id = get_doctor_id_for_user()
    h = get_hospital(slug); cur = mysql.connection.cursor()
    msg = ''
    if request.method == 'POST' and request.form.get('action') == 'add':
        pid       = int(request.form.get('patient_id',0))
        did       = int(request.form.get('doctor_id', doctor_id or 0))
        test_name = request.form.get('test_name','').strip()
        condition = request.form.get('condition_notes','').strip()
        test_date = request.form.get('test_date','')
        status    = request.form.get('status','Pending')
        image_path= None
        if 'report_image' in request.files:
            f = request.files['report_image']
            if f and f.filename:
                image_path = save_upload(f, f'reports/{hid}')
        cur.execute("INSERT INTO lab_reports (hospital_id,patient_id,doctor_id,test_name,condition_notes,report_image,test_date,status,uploaded_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (hid, pid, did, test_name, condition, image_path, test_date, status, session['user_id']))
        mysql.connection.commit()
        return redirect(url_for('lab_reports', slug=slug, msg='added'))
    if request.args.get('delete') and role in ('hospital_admin', 'doctor'):
        rid = int(request.args.get('delete'))
        cur.execute("SELECT report_image FROM lab_reports WHERE report_id=%s AND hospital_id=%s", (rid, hid))
        row = cur.fetchone()
        if row and row['report_image']:
            fp = os.path.join(app.root_path, 'static', row['report_image'])
            if os.path.exists(fp): os.remove(fp)
        cur.execute("DELETE FROM lab_reports WHERE report_id=%s AND hospital_id=%s", (rid, hid))
        mysql.connection.commit()
        return redirect(url_for('lab_reports', slug=slug, msg='deleted'))
    msgs = {'added':'Lab report added!','deleted':'Report deleted.'}
    if request.args.get('msg'): msg = msgs.get(request.args.get('msg'),'')
    scope_pt = f"AND doctor_id={doctor_id}" if doctor_id else ""
    cur.execute(f"SELECT patient_id,full_name,phone FROM patients WHERE hospital_id={hid} {scope_pt} ORDER BY full_name")
    patient_list = cur.fetchall()
    if role == 'hospital_admin':
        cur.execute("SELECT doctor_id,name FROM doctors WHERE hospital_id=%s AND is_active=1 ORDER BY name", (hid,))
        doctors_dd = cur.fetchall()
    else: doctors_dd = []
    search = request.args.get('search','').strip()
    scope_r = f"WHERE lr.hospital_id={hid}"
    if doctor_id: scope_r += f" AND lr.doctor_id={doctor_id}"
    if search: scope_r += f" AND (p.full_name LIKE '%{search}%' OR lr.test_name LIKE '%{search}%')"
    cur.execute(f"SELECT lr.*,p.full_name AS patient_name,p.phone,d.name AS doctor_name FROM lab_reports lr JOIN patients p ON lr.patient_id=p.patient_id JOIN doctors d ON lr.doctor_id=d.doctor_id {scope_r} ORDER BY lr.test_date DESC,lr.report_id DESC")
    reports = cur.fetchall()
    return render_template('lab_reports.html', h=h, slug=slug, role=role, msg=msg,
                           patients=patient_list, doctors_dd=doctors_dd, reports=reports, search=search)

@app.route('/h/<slug>/report/<int:rid>')
@require_login
def view_report(slug, rid):
    hid = session['hospital_id']; role = session['role']
    doctor_id = get_doctor_id_for_user()
    h = get_hospital(slug); cur = mysql.connection.cursor()
    if role == 'patient':
        cur.execute("SELECT * FROM lab_reports WHERE report_id=%s AND hospital_id=%s AND patient_id=%s", (rid, hid, session.get('linked_id')))
    elif doctor_id:
        cur.execute("SELECT * FROM lab_reports WHERE report_id=%s AND hospital_id=%s AND doctor_id=%s", (rid, hid, doctor_id))
    else:
        cur.execute("SELECT * FROM lab_reports WHERE report_id=%s AND hospital_id=%s", (rid, hid))
    rpt = cur.fetchone()
    if not rpt: return "Report not found", 404
    cur.execute("SELECT * FROM patients WHERE patient_id=%s", (rpt['patient_id'],))
    pt = cur.fetchone()
    cur.execute("SELECT d.*,s.spec_name FROM doctors d LEFT JOIN specializations s ON d.spec_id=s.spec_id WHERE d.doctor_id=%s", (rpt['doctor_id'],))
    dr = cur.fetchone()
    return render_template('view_report.html', h=h, slug=slug, rpt=rpt, pt=pt, dr=dr)

# ─────────────────────────────────────────────────────────────
# APPOINTMENTS
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/appointments', methods=['GET','POST'])
@require_login
def appointments(slug):
    hid = session['hospital_id']; role = session['role']
    doctor_id = get_doctor_id_for_user()
    h = get_hospital(slug); cur = mysql.connection.cursor()
    msg = ''
    if request.method == 'POST':
        aid    = int(request.form.get('appt_id',0))
        status = request.form.get('status','')
        scope  = f"AND doctor_id={doctor_id}" if doctor_id else ""
        cur.execute(f"UPDATE appointments SET status=%s WHERE appt_id=%s AND hospital_id=%s {scope}", (status, aid, hid))
        mysql.connection.commit()
        return redirect(url_for('appointments', slug=slug, msg='updated'))
    msgs = {'updated':'Appointment updated!'}
    if request.args.get('msg'): msg = msgs.get(request.args.get('msg'),'')
    filter_date   = request.args.get('date','')
    filter_status = request.args.get('status','')
    search        = request.args.get('search','').strip()
    where = f"WHERE a.hospital_id={hid}"
    if doctor_id:     where += f" AND a.doctor_id={doctor_id}"
    if filter_date:   where += f" AND a.appt_date='{filter_date}'"
    if filter_status: where += f" AND a.status='{filter_status}'"
    if search:        where += f" AND (a.patient_name LIKE '%{search}%' OR a.patient_phone LIKE '%{search}%')"
    cur.execute(f"SELECT a.*,d.name AS doctor_name FROM appointments a JOIN doctors d ON a.doctor_id=d.doctor_id {where} ORDER BY a.appt_date DESC,a.appt_time ASC")
    appt_list = cur.fetchall()
    doc_scope = f"AND doctor_id={doctor_id}" if doctor_id else ""
    cur.execute(f"SELECT COUNT(*) AS c FROM appointments WHERE hospital_id={hid} {doc_scope} AND appt_date=CURDATE() AND status NOT IN ('Cancelled')")
    today_count = cur.fetchone()['c']
    cur.execute(f"SELECT COUNT(*) AS c FROM appointments WHERE hospital_id={hid} {doc_scope} AND status='Pending'")
    pending_count = cur.fetchone()['c']
    return render_template('appointments.html', h=h, slug=slug, role=role, msg=msg,
                           appointments=appt_list, today_count=today_count, pending_count=pending_count,
                           filter_date=filter_date, filter_status=filter_status, search=search,
                           today=date.today().isoformat())

# ─────────────────────────────────────────────────────────────
# INVOICES
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/invoices', methods=['GET','POST'])
@require_login
def invoices(slug):
    hid = session['hospital_id']; role = session['role']
    if not plan_allows(hid, 'has_invoice'):
        return render_template('plan_gate.html', h=get_hospital(slug), slug=slug, feature='Invoicing', plan_name='Pro')
    doctor_id = get_doctor_id_for_user()
    h = get_hospital(slug); cur = mysql.connection.cursor()
    msg = ''
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            pid    = int(request.form.get('patient_id',0))
            did    = int(request.form.get('doctor_id', doctor_id or 0))
            vdate  = request.form.get('visit_date','')
            vfee   = int(request.form.get('visit_fee',0))
            lfee   = int(request.form.get('lab_fee',0))
            mfee   = int(request.form.get('medicine_fee',0))
            disc   = int(request.form.get('discount',0))
            paid   = int(request.form.get('paid_amount',0))
            method = request.form.get('payment_method','')
            notes  = request.form.get('notes','').strip()
            total  = vfee + lfee + mfee - disc
            status = 'paid' if paid >= total else ('partial' if paid > 0 else 'unpaid')
            cur.execute("INSERT INTO invoices (hospital_id,patient_id,doctor_id,visit_date,visit_fee,lab_fee,medicine_fee,discount,total,paid_amount,payment_method,status,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (hid, pid, did, vdate, vfee, lfee, mfee, disc, total, paid, method, status, notes))
            mysql.connection.commit()
            return redirect(url_for('invoices', slug=slug, msg='added'))
        elif action == 'pay':
            iid    = int(request.form.get('invoice_id',0))
            pay_now= int(request.form.get('paid_now',0))
            method = request.form.get('pay_method','')
            cur.execute("SELECT total,paid_amount FROM invoices WHERE invoice_id=%s AND hospital_id=%s", (iid, hid))
            inv = cur.fetchone()
            if inv:
                new_paid = inv['paid_amount'] + pay_now
                new_status = 'paid' if new_paid >= inv['total'] else ('partial' if new_paid > 0 else 'unpaid')
                cur.execute("UPDATE invoices SET paid_amount=%s,payment_method=%s,status=%s WHERE invoice_id=%s AND hospital_id=%s",
                            (new_paid, method, new_status, iid, hid))
                mysql.connection.commit()
            return redirect(url_for('invoices', slug=slug, msg='updated'))
    msgs = {'added':'Invoice created!','updated':'Payment recorded!'}
    if request.args.get('msg'): msg = msgs.get(request.args.get('msg'),'')
    doc_scope = f"AND doctor_id={doctor_id}" if doctor_id else ""
    cur.execute(f"SELECT patient_id,full_name,phone FROM patients WHERE hospital_id={hid} {doc_scope} ORDER BY full_name")
    patient_list = cur.fetchall()
    if role == 'hospital_admin':
        cur.execute("SELECT doctor_id,name FROM doctors WHERE hospital_id=%s AND is_active=1 ORDER BY name", (hid,))
        doctors_dd = cur.fetchall()
    else: doctors_dd = []
    filter_status = request.args.get('status','')
    scope_inv = f"AND i.doctor_id={doctor_id}" if doctor_id else ""
    status_cl = f"AND i.status='{filter_status}'" if filter_status else ""
    cur.execute(f"SELECT i.*,p.full_name AS patient_name,p.phone,d.name AS doctor_name FROM invoices i JOIN patients p ON i.patient_id=p.patient_id JOIN doctors d ON i.doctor_id=d.doctor_id WHERE i.hospital_id={hid} {scope_inv} {status_cl} ORDER BY i.created_at DESC")
    inv_list = cur.fetchall()
    cur.execute(f"SELECT COALESCE(SUM(paid_amount),0) AS s FROM invoices WHERE hospital_id={hid} {scope_inv}")
    total_revenue = cur.fetchone()['s']
    cur.execute(f"SELECT COALESCE(SUM(total-paid_amount),0) AS s FROM invoices WHERE hospital_id={hid} {scope_inv} AND status!='paid'")
    total_due = cur.fetchone()['s']
    return render_template('invoices.html', h=h, slug=slug, role=role, msg=msg,
                           patients=patient_list, doctors_dd=doctors_dd, invoices=inv_list,
                           total_revenue=total_revenue, total_due=total_due, filter_status=filter_status,
                           today=date.today().isoformat())

@app.route('/h/<slug>/invoice/<int:iid>')
@require_login
def view_invoice(slug, iid):
    hid = session['hospital_id']
    h = get_hospital(slug); cur = mysql.connection.cursor()
    cur.execute("SELECT i.*,p.full_name,p.phone,p.age,p.gender,p.zilla,d.name AS doctor_name,s.spec_name FROM invoices i JOIN patients p ON i.patient_id=p.patient_id JOIN doctors d ON i.doctor_id=d.doctor_id LEFT JOIN specializations s ON d.spec_id=s.spec_id WHERE i.invoice_id=%s AND i.hospital_id=%s", (iid, hid))
    inv = cur.fetchone()
    if not inv: return "Invoice not found", 404
    return render_template('view_invoice.html', h=h, slug=slug, inv=inv, due=inv['total']-inv['paid_amount'])

# ─────────────────────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/settings', methods=['GET','POST'])
@require_login
def settings(slug):
    hid = session['hospital_id']; role = session['role']
    if role != 'hospital_admin': return redirect(url_for('dashboard', slug=slug))
    h = get_hospital(slug); cur = mysql.connection.cursor()
    msg = ''
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'save_settings':
            fields = ['name','tagline','address','phone','email','about','emergency_phone','working_hours','facebook_url','primary_color','accent_color']
            updates = {f: request.form.get(f,'').strip() for f in fields}
            logo_path = None
            if 'logo' in request.files:
                f = request.files['logo']
                if f and f.filename:
                    logo_path = save_upload(f, 'logos')
            if logo_path: updates['logo'] = logo_path
            set_clause = ','.join([f"{k}=%s" for k in updates])
            cur.execute(f"UPDATE hospitals SET {set_clause} WHERE hospital_id=%s", list(updates.values()) + [hid])
            mysql.connection.commit()
            h = get_hospital(slug)
            msg = 'Settings saved!'
        elif action == 'save_schedule':
            did   = int(request.form.get('doctor_id',0))
            day   = request.form.get('day_of_week','')
            start = request.form.get('start_time','')
            end   = request.form.get('end_time','')
            slot  = int(request.form.get('slot_minutes',15))
            maxp  = int(request.form.get('max_patients',16))
            active= 1 if request.form.get('is_active') else 0
            cur.execute("INSERT INTO doctor_schedules (hospital_id,doctor_id,day_of_week,start_time,end_time,slot_minutes,max_patients,is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE start_time=%s,end_time=%s,slot_minutes=%s,max_patients=%s,is_active=%s",
                        (hid, did, day, start, end, slot, maxp, active, start, end, slot, maxp, active))
            mysql.connection.commit()
            msg = 'Schedule saved!'
        elif action == 'add_leave':
            did    = int(request.form.get('leave_doctor_id',0))
            ldate  = request.form.get('leave_date','')
            reason = request.form.get('leave_reason','').strip()
            cur.execute("INSERT IGNORE INTO doctor_leaves (hospital_id,doctor_id,leave_date,reason) VALUES (%s,%s,%s,%s)", (hid, did, ldate, reason))
            mysql.connection.commit()
            msg = 'Leave day added!'
    if request.args.get('del_sch'):
        sid = int(request.args.get('del_sch'))
        cur.execute("DELETE FROM doctor_schedules WHERE schedule_id=%s AND hospital_id=%s", (sid, hid))
        mysql.connection.commit()
        return redirect(url_for('settings', slug=slug))
    if request.args.get('del_leave'):
        lid = int(request.args.get('del_leave'))
        cur.execute("DELETE FROM doctor_leaves WHERE leave_id=%s AND hospital_id=%s", (lid, hid))
        mysql.connection.commit()
        return redirect(url_for('settings', slug=slug))
    if request.args.get('msg'): msg = 'Done!'
    cur.execute("SELECT doctor_id,name FROM doctors WHERE hospital_id=%s AND is_active=1 ORDER BY name", (hid,))
    doctors = cur.fetchall()
    cur.execute("SELECT ds.*,d.name AS doctor_name FROM doctor_schedules ds JOIN doctors d ON ds.doctor_id=d.doctor_id WHERE ds.hospital_id=%s ORDER BY d.name", (hid,))
    schedules = cur.fetchall()
    cur.execute("SELECT dl.*,d.name AS doctor_name FROM doctor_leaves dl JOIN doctors d ON dl.doctor_id=d.doctor_id WHERE dl.hospital_id=%s AND dl.leave_date>=CURDATE() ORDER BY dl.leave_date", (hid,))
    leaves = cur.fetchall()
    cur.execute("SELECT * FROM plans WHERE plan_id=%s", (h['plan_id'],))
    plan = cur.fetchone()
    days = ['Saturday','Sunday','Monday','Tuesday','Wednesday','Thursday','Friday']
    return render_template('settings.html', h=h, slug=slug, msg=msg, doctors=doctors,
                           schedules=schedules, leaves=leaves, plan=plan, days=days)

# ─────────────────────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/audit')
@require_login
def audit(slug):
    hid = session['hospital_id']; role = session['role']
    if role != 'hospital_admin': return redirect(url_for('dashboard', slug=slug))
    h = get_hospital(slug); cur = mysql.connection.cursor()
    search = request.args.get('search','').strip()
    filter_table = request.args.get('tbl','')
    where = f"WHERE hospital_id={hid}"
    if search:       where += f" AND (details LIKE '%{search}%' OR done_by LIKE '%{search}%')"
    if filter_table: where += f" AND table_name='{filter_table}'"
    cur.execute(f"SELECT * FROM audit_log {where} ORDER BY created_at DESC LIMIT 300")
    logs = cur.fetchall()
    cur.execute(f"SELECT DISTINCT table_name FROM audit_log WHERE hospital_id={hid} ORDER BY table_name")
    tables = cur.fetchall()
    return render_template('audit.html', h=h, slug=slug, logs=logs, tables=tables, search=search, filter_table=filter_table)

# ─────────────────────────────────────────────────────────────
# UPGRADE PLAN
# ─────────────────────────────────────────────────────────────
@app.route('/h/<slug>/upgrade', methods=['GET','POST'])
@require_login
def upgrade(slug):
    hid = session['hospital_id']
    h = get_hospital(slug); cur = mysql.connection.cursor()
    msg = ''
    if request.method == 'POST':
        new_plan = int(request.form.get('plan_id',0))
        trx_id   = request.form.get('trx_id','').strip()
        method   = request.form.get('method','')
        cur.execute("SELECT * FROM plans WHERE plan_id=%s", (new_plan,))
        plan_info = cur.fetchone()
        if plan_info:
            cur.execute("INSERT INTO payments (hospital_id,plan_id,amount_bdt,method,trx_id,status) VALUES (%s,%s,%s,%s,%s,'pending')",
                        (hid, new_plan, plan_info['price_bdt'], method, trx_id))
            mysql.connection.commit()
            msg = f'Payment submitted! Transaction ID: {trx_id}. Will be upgraded within 24 hours.'
    cur.execute("SELECT * FROM plans WHERE is_active=1 ORDER BY plan_id")
    plans = cur.fetchall()
    return render_template('upgrade.html', h=h, slug=slug, plans=plans, msg=msg)

# ─────────────────────────────────────────────────────────────
# PLATFORM ADMIN
# ─────────────────────────────────────────────────────────────
@app.route('/admin', methods=['GET','POST'])
def platform_admin():
    if not session.get('platform_admin'):
        msg = ''
        if request.method == 'POST' and request.form.get('action') == 'login':
            uname = request.form.get('username','').strip()
            passw = md5(request.form.get('password',''))
            cur = mysql.connection.cursor()
            cur.execute("SELECT * FROM platform_admins WHERE username=%s AND password=%s", (uname, passw))
            row = cur.fetchone()
            if row:
                session['platform_admin'] = {'username': row['username']}
                return redirect(url_for('platform_admin'))
            msg = 'Invalid credentials.'
        return render_template('platform_login.html', msg=msg)
    if request.args.get('logout'):
        session.pop('platform_admin', None)
        return redirect(url_for('platform_admin'))
    cur = mysql.connection.cursor()
    msg = ''
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'toggle':
            hid = int(request.form.get('hospital_id',0))
            cur.execute("SELECT is_active FROM hospitals WHERE hospital_id=%s", (hid,))
            row = cur.fetchone()
            if row: cur.execute("UPDATE hospitals SET is_active=%s WHERE hospital_id=%s", (0 if row['is_active'] else 1, hid))
            mysql.connection.commit(); msg = 'Hospital status updated.'
        elif action == 'verify':
            hid = int(request.form.get('hospital_id',0))
            cur.execute("UPDATE hospitals SET is_verified=1 WHERE hospital_id=%s", (hid,))
            mysql.connection.commit(); msg = 'Hospital verified!'
        elif action == 'upgrade_plan':
            hid = int(request.form.get('hospital_id',0))
            pid = int(request.form.get('plan_id',0))
            exp = (date.today() + timedelta(days=30)).isoformat()
            cur.execute("UPDATE hospitals SET plan_id=%s,plan_expires=%s WHERE hospital_id=%s", (pid, exp, hid))
            mysql.connection.commit(); msg = 'Plan upgraded!'
        elif action == 'approve_payment':
            pay_id = int(request.form.get('payment_id',0))
            hid    = int(request.form.get('hospital_id',0))
            pid    = int(request.form.get('plan_id',0))
            exp    = (date.today() + timedelta(days=30)).isoformat()
            cur.execute("UPDATE payments SET status='paid',paid_at=NOW() WHERE payment_id=%s", (pay_id,))
            cur.execute("UPDATE hospitals SET plan_id=%s,plan_expires=%s WHERE hospital_id=%s", (pid, exp, hid))
            mysql.connection.commit(); msg = 'Payment approved and plan upgraded!'
    cur.execute("""SELECT h.*,pl.name AS plan_name,COUNT(DISTINCT d.doctor_id) AS doctors,COUNT(DISTINCT p.patient_id) AS patients
        FROM hospitals h LEFT JOIN plans pl ON h.plan_id=pl.plan_id
        LEFT JOIN doctors d ON d.hospital_id=h.hospital_id LEFT JOIN patients p ON p.hospital_id=h.hospital_id
        GROUP BY h.hospital_id ORDER BY h.created_at DESC""")
    hospitals = cur.fetchall()
    cur.execute("SELECT * FROM plans ORDER BY plan_id")
    plans = cur.fetchall()
    cur.execute("""SELECT py.*,h.name AS hospital_name,pl.name AS plan_name FROM payments py
        JOIN hospitals h ON py.hospital_id=h.hospital_id JOIN plans pl ON py.plan_id=pl.plan_id
        WHERE py.status='pending' ORDER BY py.created_at DESC""")
    payments = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS c FROM hospitals"); total_h = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) AS c FROM patients");  total_p = cur.fetchone()['c']
    cur.execute("SELECT COALESCE(SUM(amount_bdt),0) AS s FROM payments WHERE status='paid'"); total_rev = cur.fetchone()['s']
    return render_template('platform_admin.html', hospitals=hospitals, plans=plans, payments=payments,
                           total_h=total_h, total_p=total_p, total_rev=total_rev, msg=msg)

# ─────────────────────────────────────────────────────────────
# STATIC UPLOADS
# ─────────────────────────────────────────────────────────────
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ─────────────────────────────────────────────────────────────
# BANGLADESH LOCATIONS (for autocomplete)
# ─────────────────────────────────────────────────────────────
def get_bd_locations():
    return {
        "Dhaka":["Dhanmondi","Mirpur","Gulshan","Uttara","Mohammadpur","Tejgaon","Motijheel","Rampura","Banani","Badda"],
        "Chittagong":["Pahartali","Panchlaish","Kotwali","Bayazid","Chandgaon","Halishahar","Khulshi","Agrabad"],
        "Sylhet":["Sylhet Sadar","Jalalabad","Shahporan","Osmani Nagar","Beanibazar"],
        "Rajshahi":["Rajshahi Sadar","Boalia","Motihar","Shah Makhdum","Poba"],
        "Khulna":["Khulna Sadar","Sonadanga","Khalishpur","Daulatpur","Rupsa"],
        "Barisal":["Barisal Sadar","Wazirpur","Babuganj","Agailjhara","Bakerganj"],
        "Rangpur":["Rangpur Sadar","Badarganj","Mithapukur","Gangachara","Pirganj"],
        "Mymensingh":["Mymensingh Sadar","Muktagachha","Trishal","Bhaluka","Gaffargaon"],
        "Comilla":["Comilla Sadar","Debidwar","Barura","Brahmanpara","Chandina"],
        "Narayanganj":["Narayanganj Sadar","Araihazar","Bandar","Rupganj","Sonargaon"],
        "Gazipur":["Gazipur Sadar","Kaliakair","Kapasia","Kaliganj","Sreepur"],
        "Tangail":["Tangail Sadar","Basail","Bhuapur","Delduar","Ghatail"],
        "Bogra":["Bogra Sadar","Adamdighi","Dhunat","Dhupchanchia","Gabtali"],
        "Jessore":["Jessore Sadar","Chougachha","Jhikargachha","Monirampur","Sharsha"],
        "Dinajpur":["Dinajpur Sadar","Birganj","Bochaganj","Chirirbandar","Fulbari"],
    }

@app.route('/api/bd-locations')
def bd_locations():
    return jsonify(get_bd_locations())

# ─────────────────────────────────────────────────────────────
# TEMPLATE FILTERS
# ─────────────────────────────────────────────────────────────
@app.template_filter('initials')
def initials_filter(name):
    words = (name or '').split()
    return ''.join(w[0].upper() for w in words[:2])

@app.template_filter('format_time')
def format_time_filter(t):
    try:
        if hasattr(t, 'strftime'): return t.strftime('%I:%M %p')
        return datetime.strptime(str(t), '%H:%M:%S').strftime('%I:%M %p')
    except: return str(t)

@app.template_filter('format_date')
def format_date_filter(d):
    try:
        if hasattr(d, 'strftime'): return d.strftime('%d %b %Y')
        return datetime.strptime(str(d), '%Y-%m-%d').strftime('%d %b %Y')
    except: return str(d)

@app.template_filter('currency')
def currency_filter(v):
    try: return f"৳{int(v):,}"
    except: return f"৳{v}"

@app.context_processor
def inject_globals():
    return {
        'session': session,
        'today': date.today().isoformat(),
        'now': datetime.now(),
    }

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
