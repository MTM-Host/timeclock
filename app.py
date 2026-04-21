from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_file
from datetime import datetime, date, timedelta
import os
import json
import csv
import io
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# PostgreSQL support
import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production-abc123')

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

EMAIL_SENDER = os.environ.get('EMAIL_SENDER', '')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '')
EMAIL_RECIPIENT = os.environ.get('EMAIL_RECIPIENT', '')
EMAIL_SMTP = os.environ.get('EMAIL_SMTP', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))

DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ─── Database Setup ───────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS employees (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL UNIQUE,
                    active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS punches (
                    id SERIAL PRIMARY KEY,
                    employee_id INTEGER NOT NULL,
                    punch_type TEXT NOT NULL,
                    punch_time TEXT NOT NULL,
                    date TEXT NOT NULL,
                    FOREIGN KEY (employee_id) REFERENCES employees(id)
                )
            ''')
        conn.commit()

init_db()

# ─── Helpers ─────────────────────────────────────────────────────────────────

def calc_hours(punches_for_day):
    punch_map = {}
    for p in punches_for_day:
        punch_map[p['punch_type']] = p['punch_time']

    total_minutes = 0

    if 'clock_in' in punch_map and 'lunch_out' in punch_map:
        t1 = datetime.fromisoformat(punch_map['clock_in'])
        t2 = datetime.fromisoformat(punch_map['lunch_out'])
        total_minutes += (t2 - t1).total_seconds() / 60
    elif 'clock_in' in punch_map and 'clock_out' in punch_map and 'lunch_out' not in punch_map:
        t1 = datetime.fromisoformat(punch_map['clock_in'])
        t2 = datetime.fromisoformat(punch_map['clock_out'])
        total_minutes += (t2 - t1).total_seconds() / 60

    if 'lunch_in' in punch_map and 'clock_out' in punch_map:
        t1 = datetime.fromisoformat(punch_map['lunch_in'])
        t2 = datetime.fromisoformat(punch_map['clock_out'])
        total_minutes += (t2 - t1).total_seconds() / 60

    hours = int(total_minutes // 60)
    minutes = int(total_minutes % 60)
    return hours, minutes, total_minutes

def get_employee_status(employee_id):
    today = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM punches WHERE employee_id=%s AND date=%s ORDER BY punch_time', (employee_id, today))
            punches = cur.fetchall()

    punch_types = [p['punch_type'] for p in punches]

    if not punch_types: return 'not_in'
    if 'clock_out' in punch_types: return 'clocked_out'
    if 'lunch_in' in punch_types: return 'working_afternoon'
    if 'lunch_out' in punch_types: return 'at_lunch'
    if 'clock_in' in punch_types: return 'working_morning'
    return 'not_in'

def next_punch_type(status):
    return {'not_in':'clock_in','working_morning':'lunch_out','at_lunch':'lunch_in','working_afternoon':'clock_out','clocked_out':None}.get(status)

def next_punch_label(status):
    return {'not_in':'Clock In','working_morning':'Lunch Out','at_lunch':'Back from Lunch','working_afternoon':'Clock Out','clocked_out':None}.get(status)

# ─── Employee Routes ──────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/lookup', methods=['POST'])
def lookup():
    data = request.json
    code = data.get('code', '').strip().upper()

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM employees WHERE code=%s AND active=1', (code,))
            emp = cur.fetchone()

    if not emp:
        return jsonify({'success': False, 'message': 'Code not found. Please try again.'})

    status = get_employee_status(emp['id'])
    next_type = next_punch_type(status)
    next_label = next_punch_label(status)

    if next_type is None:
        return jsonify({'success':True,'employee_id':emp['id'],'name':emp['name'],'status':status,'next_punch':None,'next_label':'Done for today! See you tomorrow 👋','done':True})

    return jsonify({'success':True,'employee_id':emp['id'],'name':emp['name'],'status':status,'next_punch':next_type,'next_label':next_label,'done':False})

@app.route('/api/punch', methods=['POST'])
def punch():
    data = request.json
    employee_id = data.get('employee_id')
    punch_type = data.get('punch_type')

    if punch_type not in ['clock_in','lunch_out','lunch_in','clock_out']:
        return jsonify({'success': False, 'message': 'Invalid punch type.'})

    now = datetime.now(pytz.timezone('America/Los_Angeles'))

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM employees WHERE id=%s AND active=1', (employee_id,))
            emp = cur.fetchone()
            if not emp:
                return jsonify({'success': False, 'message': 'Employee not found.'})

            status = get_employee_status(employee_id)
            expected = next_punch_type(status)
            if punch_type != expected:
                return jsonify({'success': False, 'message': 'Unexpected punch type.'})

            cur.execute(
                'INSERT INTO punches (employee_id, punch_type, punch_time, date) VALUES (%s,%s,%s,%s)',
                (employee_id, punch_type, now.isoformat(), date.today().isoformat())
            )
        conn.commit()

    labels = {'clock_in':'Clocked in','lunch_out':'Out for lunch','lunch_in':'Back from lunch','clock_out':'Clocked out'}
    return jsonify({'success':True,'message':f"{labels[punch_type]} at {now.strftime('%I:%M %p')}","time":now.strftime('%I:%M %p'),'punch_type':punch_type})

# ─── Admin Routes ─────────────────────────────────────────────────────────────

@app.route('/admin')
def admin():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    return render_template('admin.html')

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        data = request.json
        if data.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'Wrong password.'})
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('admin_login'))

@app.route('/api/admin/employees', methods=['GET'])
def get_employees():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM employees WHERE active=1 ORDER BY name')
            emps = cur.fetchall()
    return jsonify([dict(e) for e in emps])

@app.route('/api/admin/employees', methods=['POST'])
def add_employee():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    data = request.json
    name = data.get('name','').strip()
    code = data.get('code','').strip().upper()
    if not name or not code:
        return jsonify({'success':False,'message':'Name and code are required.'})
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO employees (name, code) VALUES (%s,%s)', (name, code))
            conn.commit()
        return jsonify({'success': True})
    except psycopg2.errors.UniqueViolation:
        return jsonify({'success':False,'message':'That code is already in use.'})

@app.route('/api/admin/employees/<int:emp_id>', methods=['DELETE'])
def deactivate_employee(emp_id):
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE employees SET active=0 WHERE id=%s', (emp_id,))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/admin/today')
def today_summary():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    today = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM employees WHERE active=1 ORDER BY name')
            employees = cur.fetchall()
            cur.execute('SELECT * FROM punches WHERE date=%s', (today,))
            punches = cur.fetchall()

    punch_by_emp = {}
    for p in punches:
        eid = p['employee_id']
        if eid not in punch_by_emp: punch_by_emp[eid] = []
        punch_by_emp[eid].append(dict(p))

    result = []
    for emp in employees:
        eid = emp['id']
        emp_punches = punch_by_emp.get(eid, [])
        status = get_employee_status(eid)
        h, m, total_min = calc_hours(emp_punches)
        punch_map = {p['punch_type']: datetime.fromisoformat(p['punch_time']).strftime('%I:%M %p') for p in emp_punches}
        result.append({'id':eid,'name':emp['name'],'status':status,'clock_in':punch_map.get('clock_in','—'),'lunch_out':punch_map.get('lunch_out','—'),'lunch_in':punch_map.get('lunch_in','—'),'clock_out':punch_map.get('clock_out','—'),'hours_today':f"{h}h {m}m" if total_min > 0 else '—'})
    return jsonify(result)

@app.route('/api/admin/report')
def payroll_report():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except:
        return jsonify({'error':'Invalid dates'}), 400

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM employees WHERE active=1 ORDER BY name')
            employees = cur.fetchall()
            cur.execute('SELECT * FROM punches WHERE date >= %s AND date <= %s ORDER BY date, punch_time', (start_str, end_str))
            punches = cur.fetchall()

    data = {}
    for emp in employees:
        data[emp['id']] = {'name':emp['name'],'code':emp['code'],'days':{}}
    for p in punches:
        eid = p['employee_id']
        if eid not in data: continue
        d = p['date']
        if d not in data[eid]['days']: data[eid]['days'][d] = []
        data[eid]['days'][d].append(dict(p))

    result = []
    for eid, emp_data in data.items():
        total_minutes = 0
        days_worked = 0
        daily = []
        current = start_date
        while current <= end_date:
            d = current.isoformat()
            day_punches = emp_data['days'].get(d, [])
            h, m, mins = calc_hours(day_punches)
            if mins > 0:
                days_worked += 1
                total_minutes += mins
                punch_map = {p['punch_type']: datetime.fromisoformat(p['punch_time']).strftime('%I:%M %p') for p in day_punches}
                daily.append({'date':current.strftime('%a %b %d'),'clock_in':punch_map.get('clock_in','—'),'lunch_out':punch_map.get('lunch_out','—'),'lunch_in':punch_map.get('lunch_in','—'),'clock_out':punch_map.get('clock_out','—'),'hours':f"{h}h {m}m"})
            current += timedelta(days=1)
        th = int(total_minutes // 60)
        tm = int(total_minutes % 60)
        result.append({'name':emp_data['name'],'days_worked':days_worked,'total_hours':f"{th}h {tm}m",'total_minutes':total_minutes,'daily':daily})
    return jsonify({'start':start_date.strftime('%B %d, %Y'),'end':end_date.strftime('%B %d, %Y'),'employees':result})

@app.route('/api/admin/trends')
def trends():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    end = date.today()
    start = end - timedelta(days=29)

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT p.*, e.name FROM punches p JOIN employees e ON p.employee_id=e.id WHERE p.date >= %s AND p.date <= %s ORDER BY p.date, p.punch_time', (start.isoformat(), end.isoformat()))
            punches = cur.fetchall()

    by_emp_day = {}
    for p in punches:
        key = (p['employee_id'], p['date'])
        if key not in by_emp_day: by_emp_day[key] = []
        by_emp_day[key].append(dict(p))

    daily_totals = {}
    for (eid, d), day_punches in by_emp_day.items():
        h, m, mins = calc_hours(day_punches)
        if d not in daily_totals: daily_totals[d] = 0
        daily_totals[d] += mins

    chart_data = []
    current = start
    while current <= end:
        d = current.isoformat()
        chart_data.append({'date':current.strftime('%b %d'),'hours':round(daily_totals.get(d,0)/60,1)})
        current += timedelta(days=1)

    arrivals = {}
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT p.employee_id, e.name, p.punch_time FROM punches p JOIN employees e ON p.employee_id=e.id WHERE p.punch_type='clock_in' AND p.date >= %s", (start.isoformat(),))
            clock_ins = cur.fetchall()

    for ci in clock_ins:
        eid = ci['employee_id']
        if eid not in arrivals: arrivals[eid] = {'name':ci['name'],'times':[]}
        t = datetime.fromisoformat(ci['punch_time'])
        arrivals[eid]['times'].append(t.hour*60+t.minute)

    avg_arrivals = []
    for eid, info in arrivals.items():
        avg_min = sum(info['times'])/len(info['times'])
        h = int(avg_min//60); m = int(avg_min%60)
        suffix = 'AM' if h < 12 else 'PM'
        h12 = h if h <= 12 else h-12
        if h12 == 0: h12 = 12
        avg_arrivals.append({'name':info['name'],'avg_arrival':f"{h12}:{m:02d} {suffix}"})
    avg_arrivals.sort(key=lambda x: x['name'])
    return jsonify({'chart':chart_data,'avg_arrivals':avg_arrivals})

def generate_csv_report(start_str, end_str):
    start_date = date.fromisoformat(start_str)
    end_date = date.fromisoformat(end_str)

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM employees WHERE active=1 ORDER BY name')
            employees = cur.fetchall()
            cur.execute('SELECT * FROM punches WHERE date >= %s AND date <= %s ORDER BY date, punch_time', (start_str, end_str))
            punches = cur.fetchall()

    data = {}
    for emp in employees:
        data[emp['id']] = {'name':emp['name'],'days':{}}
    for p in punches:
        eid = p['employee_id']
        if eid not in data: continue
        d = p['date']
        if d not in data[eid]['days']: data[eid]['days'][d] = []
        data[eid]['days'][d].append(dict(p))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Employee','Date','Clock In','Lunch Out','Lunch In','Clock Out','Hours Worked'])

    for eid, emp_data in data.items():
        total_min = 0
        current = start_date
        while current <= end_date:
            d = current.isoformat()
            day_punches = emp_data['days'].get(d, [])
            h, m, mins = calc_hours(day_punches)
            if mins > 0:
                total_min += mins
                punch_map = {p['punch_type']: datetime.fromisoformat(p['punch_time']).strftime('%I:%M %p') for p in day_punches}
                writer.writerow([emp_data['name'],current.strftime('%m/%d/%Y'),punch_map.get('clock_in',''),punch_map.get('lunch_out',''),punch_map.get('lunch_in',''),punch_map.get('clock_out',''),f"{h}h {m}m"])
            current += timedelta(days=1)
        th = int(total_min//60); tm = int(total_min%60)
        writer.writerow([emp_data['name'],'TOTAL','','','',f"{th}h {tm}m"])
        writer.writerow([])
    return output.getvalue()

def send_payroll_email():
    if not EMAIL_SENDER or not EMAIL_RECIPIENT:
        print("Email not configured — skipping.")
        return
    today = date.today()
    if today.day == 1:
        last_month_end = today - timedelta(days=1)
        start = last_month_end.replace(day=16)
        end = last_month_end
    elif today.day == 15:
        start = today.replace(day=1)
        end = today
    else:
        return

    csv_data = generate_csv_report(start.isoformat(), end.isoformat())
    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECIPIENT
    msg['Subject'] = f"Payroll Report: {start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    msg.attach(MIMEText(f"Please find attached the payroll report for {start.strftime('%B %d')} – {end.strftime('%B %d, %Y')}.", 'plain'))
    attachment = MIMEBase('application', 'octet-stream')
    attachment.set_payload(csv_data.encode())
    encoders.encode_base64(attachment)
    attachment.add_header('Content-Disposition', f'attachment; filename="payroll_{start.isoformat()}_{end.isoformat()}.csv"')
    msg.attach(attachment)
    try:
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"Payroll email sent to {EMAIL_RECIPIENT}")
    except Exception as e:
        print(f"Failed to send email: {e}")

@app.route('/api/admin/test-email')
def test_email():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    if not EMAIL_SENDER or not EMAIL_RECIPIENT:
        return jsonify({'success': False, 'message': 'Email not configured. Check your Railway variables.'})
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECIPIENT
        msg['Subject'] = 'Time Clock — Test Email ✅'
        msg.attach(MIMEText('Your Time Clock email reports are working correctly! You will receive payroll reports automatically on the 1st and 15th of each month.', 'plain'))
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        return jsonify({'success': True, 'message': f'Test email sent to {EMAIL_RECIPIENT}!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/admin/report/csv')
def download_csv():
    if not session.get('admin'): return jsonify({'error':'Unauthorized'}), 401
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    csv_data = generate_csv_report(start_str, end_str)
    return send_file(io.BytesIO(csv_data.encode()), mimetype='text/csv', as_attachment=True, download_name=f'payroll_{start_str}_{end_str}.csv')

scheduler = BackgroundScheduler()
scheduler.add_job(send_payroll_email, 'cron', hour=8, minute=0)
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
