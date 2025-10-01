"""
WhatsApp Complaint Manager (Full Prototype)
Filename: whatsapp_complaint_manager_full.py

Features included:
- Flask webhook endpoints for MSG91 WhatsApp messages
- Razorpay webhook handler (payment.captured signature verification)
- SQLAlchemy models: Users, Technicians, Tickets, TicketLogs, Payments, Expenses, Reconciliations
- Assignment logic for two technicians (load-balanced)
- Manual admin endpoints to record payments and expenses
- Reconciliation & report generation (Excel + PDF)
- APScheduler jobs to send daily/monthly/yearly and monthly reconciliation reports to MD via MSG91
- Designed to run on Ubuntu VM (Postgres recommended in prod)

Usage:
1. Install dependencies in a virtualenv:
   pip install flask sqlalchemy psycopg2-binary pandas requests apscheduler xlsxwriter reportlab python-dotenv

2. Environment variables (.env):
   DATABASE_URL=postgresql://user:pass@localhost:5432/complaintdb  # or sqlite:///complaints.db
   MSG91_AUTH_KEY=your_msg91_auth_key
   MSG91_SENDER=your_msg91_sender_id   # if applicable
   RAZORPAY_WEBHOOK_SECRET=your_razorpay_webhook_secret
   MD_PHONE=9198XXXXXXXX   # MD E.164 without leading + (MSG91 may expect country code without +)
   FLASK_ENV=development

3. Run:
   python whatsapp_complaint_manager_full.py

Note: Adapt MSG91 payload formats according to your MSG91 account docs. For file attachments, host files publicly (S3) before sending via MSG91 media.

"""

import os
import json
import hmac
import hashlib
import datetime
from flask import Flask, request, jsonify, abort
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///complaints.db')
MSG91_AUTH_KEY = os.environ.get('MSG91_AUTH_KEY')
MSG91_SENDER = os.environ.get('MSG91_SENDER')
RAZORPAY_WEBHOOK_SECRET = os.environ.get('RAZORPAY_WEBHOOK_SECRET')
MD_PHONE = os.environ.get('MD_PHONE')  # e.g. 9198XXXX (country code without + or with + depending on provider)
REPORT_DIR = os.path.join(os.getcwd(), 'reports')
os.makedirs(REPORT_DIR, exist_ok=True)

# -----------------------------------------------------------------------------
# DB Setup
# -----------------------------------------------------------------------------
Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith('sqlite') else {})
Session = sessionmaker(bind=engine)
session = Session()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String, unique=True)
    role = Column(String, default='user')
    tickets = relationship('Ticket', back_populates='user')

class Technician(Base):
    __tablename__ = 'technicians'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String, unique=True)
    tickets_assigned = Column(Integer, default=0)
    tickets = relationship('Ticket', back_populates='technician')

class Ticket(Base):
    __tablename__ = 'tickets'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    tech_id = Column(Integer, ForeignKey('technicians.id'), nullable=True)
    issue = Column(Text)
    priority = Column(String(20), default='Normal')
    status = Column(String(32), default='Open')
    billed_amount = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship('User', back_populates='tickets')
    technician = relationship('Technician', back_populates='tickets')

class TicketLog(Base):
    __tablename__ = 'ticket_logs'
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey('tickets.id'))
    status = Column(String(32))
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    updated_by = Column(String(128))

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey('tickets.id'), nullable=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default='INR')
    mode = Column(String(64))
    provider_txn_id = Column(String(128))
    status = Column(String(32), default='Pending')
    received_at = Column(DateTime, default=datetime.datetime.utcnow)
    reconciled_at = Column(DateTime, nullable=True)
    notes = Column(Text)

class Expense(Base):
    __tablename__ = 'expenses'
    id = Column(Integer, primary_key=True)
    amount = Column(Float, nullable=False)
    category = Column(String(128))
    paid_by = Column(String(128))
    paid_to = Column(String(256))
    voucher_no = Column(String(128))
    incurred_at = Column(DateTime, default=datetime.datetime.utcnow)
    notes = Column(Text)

class Reconciliation(Base):
    __tablename__ = 'reconciliations'
    id = Column(Integer, primary_key=True)
    period_start = Column(DateTime)
    period_end = Column(DateTime)
    total_collections = Column(Float)
    total_expenses = Column(Float)
    net_amount = Column(Float)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    report_file = Column(String(512))
    notes = Column(Text)

Base.metadata.create_all(engine)

# Ensure two technicians exist
if session.query(Technician).count() == 0:
    t1 = Technician(name='Tech1', phone='911234567890')
    t2 = Technician(name='Tech2', phone='919876543210')
    session.add_all([t1, t2])
    session.commit()

# -----------------------------------------------------------------------------
# Helpers: MSG91 send
# -----------------------------------------------------------------------------

def send_msg91_whatsapp(phone_e164, message, media_url=None):
    """Send WhatsApp message using MSG91 API. phone_e164: '9198...' (country code + number)
    Adjust payload to match your MSG91 account settings and template permissions."""
    if not MSG91_AUTH_KEY:
        print('[MSG91] Auth key not set — printing message instead')
        print('To:', phone_e164)
        print('Msg:', message)
        return None
    url = 'https://api.msg91.com/api/v5/whatsapp/send'
    headers = {
        'authkey': MSG91_AUTH_KEY,
        'Content-Type': 'application/json'
    }
    payload = {
        'to': phone_e164,
        'message': message
    }
    if media_url:
        payload['media'] = {'url': media_url}
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    try:
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print('MSG91 send error', e, resp.text if resp is not None else '')
        return None

# -----------------------------------------------------------------------------
# Assignment logic
# -----------------------------------------------------------------------------

def assign_ticket(ticket):
    techs = session.query(Technician).all()
    assigned = min(techs, key=lambda t: t.tickets_assigned)
    ticket.tech_id = assigned.id
    assigned.tickets_assigned += 1
    ticket.updated_at = datetime.datetime.utcnow()
    session.add(ticket)
    session.commit()
    log = TicketLog(ticket_id=ticket.id, status=ticket.status, updated_by='system_assign')
    session.add(log)
    session.commit()
    return assigned

# -----------------------------------------------------------------------------
# Reporting & reconciliation
# -----------------------------------------------------------------------------

def df_from_table(name):
    try:
        return pd.read_sql_table(name, con=engine)
    except Exception:
        # fallback: empty
        return pd.DataFrame()


def generate_period_bounds(period='daily', year=None, month=None):
    now = datetime.datetime.utcnow()
    if period == 'daily':
        start = datetime.datetime(now.year, now.month, now.day)
        end = start + datetime.timedelta(days=1)
    elif period == 'monthly':
        year = year or now.year
        month = month or now.month
        start = datetime.datetime(year, month, 1)
        if month == 12:
            end = datetime.datetime(year + 1, 1, 1)
        else:
            end = datetime.datetime(year, month + 1, 1)
    elif period == 'yearly':
        year = year or now.year
        start = datetime.datetime(year, 1, 1)
        end = datetime.datetime(year + 1, 1, 1)
    else:
        start = datetime.datetime(1970,1,1)
        end = datetime.datetime.utcnow()
    return start, end


def generate_report_text(period='daily', year=None, month=None):
    start, end = generate_period_bounds(period, year, month)
    payments_df = df_from_table('payments')
    if not payments_df.empty:
        payments_df['received_at'] = pd.to_datetime(payments_df['received_at'])
        p = payments_df[(payments_df['received_at'] >= start) & (payments_df['received_at'] < end)]
    else:
        p = pd.DataFrame()
    expenses_df = df_from_table('expenses')
    if not expenses_df.empty:
        expenses_df['incurred_at'] = pd.to_datetime(expenses_df['incurred_at'])
        e = expenses_df[(expenses_df['incurred_at'] >= start) & (expenses_df['incurred_at'] < end)]
    else:
        e = pd.DataFrame()

    total_collections = p['amount'].sum() if not p.empty else 0.0
    total_expenses = e['amount'].sum() if not e.empty else 0.0
    net = total_collections - total_expenses
    per_mode = p.groupby('mode')['amount'].sum().reset_index() if not p.empty else pd.DataFrame()

    techs = {t.id: t.name for t in session.query(Technician).all()}
    tickets_df = df_from_table('tickets')
    t_period = pd.DataFrame()
    if not tickets_df.empty:
        tickets_df['created_at'] = pd.to_datetime(tickets_df['created_at'])
        t_period = tickets_df[(tickets_df['created_at'] >= start) & (tickets_df['created_at'] < end)]
    total_tickets = len(t_period)
    resolved = len(t_period[t_period['status'] == 'Resolved'])
    pending = total_tickets - resolved

    per_tech = t_period.groupby('tech_id').size().reset_index(name='count') if not t_period.empty else pd.DataFrame()
    per_tech['tech_name'] = per_tech['tech_id'].map(techs)

    lines = [f"{period.capitalize()} Report: {start.date()} to { (end - datetime.timedelta(seconds=1)).date() } (UTC)",
             f"Total Collections: ₹{total_collections:.2f}",
             f"Total Expenses: ₹{total_expenses:.2f}",
             f"Net: ₹{net:.2f}",
             f"Total Tickets: {total_tickets} (Resolved: {resolved}, Pending: {pending})",
             '\nCollections by Mode:']
    if not per_mode.empty:
        for _, row in per_mode.iterrows():
            lines.append(f" - {row['mode']}: ₹{row['amount']:.2f}")
    else:
        lines.append(' - No collections')

    lines.append('\nTickets by Technician:')
    if not per_tech.empty:
        for _, r in per_tech.iterrows():
            lines.append(f" - {r['tech_name']}: {int(r['count'])} tickets")
    else:
        lines.append(' - No tickets')

    return '\n'.join(lines)


def generate_excel_recon(period='monthly', year=None, month=None):
    start, end = generate_period_bounds(period, year, month)
    payments_df = df_from_table('payments')
    expenses_df = df_from_table('expenses')

    if not payments_df.empty:
        payments_df['received_at'] = pd.to_datetime(payments_df['received_at'])
        p_month = payments_df[(payments_df['received_at'] >= start) & (payments_df['received_at'] < end)]
    else:
        p_month = pd.DataFrame()
    if not expenses_df.empty:
        expenses_df['incurred_at'] = pd.to_datetime(expenses_df['incurred_at'])
        e_month = expenses_df[(expenses_df['incurred_at'] >= start) & (expenses_df['incurred_at'] < end)]
    else:
        e_month = pd.DataFrame()

    total_collections = p_month['amount'].sum() if not p_month.empty else 0.0
    total_expenses = e_month['amount'].sum() if not e_month.empty else 0.0
    net = total_collections - total_expenses

    fname = os.path.join(REPORT_DIR, f'recon_{period}_{start.strftime("%Y%m%d")}_{end.strftime("%Y%m%d")}.xlsx')
    with pd.ExcelWriter(fname, engine='xlsxwriter') as writer:
        pd.DataFrame([{ 'period_start': start, 'period_end': end, 'total_collections': total_collections, 'total_expenses': total_expenses, 'net': net }]).to_excel(writer, sheet_name='Summary', index=False)
        if not p_month.empty:
            p_month.to_excel(writer, sheet_name='Payments', index=False)
            p_month.groupby('mode')['amount'].sum().reset_index().to_excel(writer, sheet_name='ByMode', index=False)
        else:
            pd.DataFrame([{'note': 'No payments'}]).to_excel(writer, sheet_name='Payments', index=False)
        if not e_month.empty:
            e_month.to_excel(writer, sheet_name='Expenses', index=False)
        else:
            pd.DataFrame([{'note': 'No expenses'}]).to_excel(writer, sheet_name='Expenses', index=False)
    # store reconciliation record
    rec = Reconciliation(period_start=start, period_end=end, total_collections=total_collections, total_expenses=total_expenses, net_amount=net, report_file=fname)
    session.add(rec)
    session.commit()
    return fname


def generate_pdf_summary(period='monthly', year=None, month=None):
    text = generate_report_text(period, year, month)
    start, end = generate_period_bounds(period, year, month)
    fname = os.path.join(REPORT_DIR, f'recon_{period}_{start.strftime("%Y%m%d")}_{end.strftime("%Y%m%d")}.pdf')
    c = canvas.Canvas(fname, pagesize=letter)
    width, height = letter
    y = height - 40
    for line in text.split('\n'):
        c.drawString(40, y, line)
        y -= 16
        if y < 40:
            c.showPage()
            y = height - 40
    c.save()
    return fname

# -----------------------------------------------------------------------------
# Flask App: endpoints for MSG91 webhook, Razorpay webhook, admin tasks
# -----------------------------------------------------------------------------
app = Flask(__name__)
STATUS_OPTIONS = ['Open', 'In Progress', 'Resolved']

@app.route('/webhook/msg91', methods=['POST'])
def msg91_webhook():
    # MSG91 will POST incoming WhatsApp messages here. Adapt parsing to MSG91 payload.
    data = request.json or {}
    # Example payload assumption: { 'message': { 'from': '9198xxxx', 'text': 'New Complaint: ...' } }
    try:
        # Attempt to be flexible with payload structures
        msg = None
        phone = None
        if 'message' in data:
            m = data['message']
            phone = m.get('from') or m.get('mobile') or data.get('from')
            # extract text
            if isinstance(m.get('text'), dict):
                msg = m['text'].get('body')
            else:
                msg = m.get('text') or data.get('text') or data.get('message_text')
        else:
            phone = data.get('from') or data.get('mobile')
            msg = data.get('text') or data.get('message')

        if not phone or not msg:
            return jsonify({'status': 'no_content'}), 400

        phone = phone.replace('+', '').strip()
        text = msg.strip()
        lower = text.lower()

        # New complaint
        if lower.startswith('new complaint:') or lower.startswith('new:'):
            issue = text.split(':',1)[1].strip() if ':' in text else 'No description'
            user = session.query(User).filter_by(phone=phone).first()
            if not user:
                user = User(name=phone, phone=phone, role='user')
                session.add(user)
                session.commit()
            ticket = Ticket(user_id=user.id, issue=issue, status='Open')
            session.add(ticket)
            session.commit()
            assigned = assign_ticket(ticket)
            log = TicketLog(ticket_id=ticket.id, status=ticket.status, updated_by=phone)
            session.add(log)
            session.commit()
            send_msg91_whatsapp(phone, f"✅ Ticket #{ticket.id} created and assigned to {assigned.name}. Reply 'Update #{ticket.id} Resolved' when fixed.")
            send_msg91_whatsapp(assigned.phone, f"🔔 New ticket #{ticket.id} assigned. Issue: {issue}")
            return jsonify({'status': 'created', 'ticket_id': ticket.id}), 200

        # Update ticket
        if lower.startswith('update') and '#' in text:
            import re
            m = re.search(r'#(\d+)', text)
            if not m:
                send_msg91_whatsapp(phone, "Couldn't parse ticket id. Use: Update #<id> <status>")
                return jsonify({'status': 'bad_request'}), 400
            ticket_id = int(m.group(1))
            words = text.strip().split()
            new_status = words[-1].capitalize()
            if new_status not in STATUS_OPTIONS:
                send_msg91_whatsapp(phone, f"Invalid status. Use: {', '.join(STATUS_OPTIONS)}")
                return jsonify({'status': 'invalid_status'}), 400
            ticket = session.query(Ticket).get(ticket_id)
            if not ticket:
                send_msg91_whatsapp(phone, f"Ticket #{ticket_id} not found.")
                return jsonify({'status': 'not_found'}), 404
            prev = ticket.status
            ticket.status = new_status
            ticket.updated_at = datetime.datetime.utcnow()
            session.commit()
            log = TicketLog(ticket_id=ticket.id, status=new_status, updated_by=phone)
            session.add(log)
            session.commit()
            if ticket.user and ticket.user.phone:
                send_msg91_whatsapp(ticket.user.phone, f"Your ticket #{ticket.id} status changed from {prev} to {new_status}.")
            if ticket.technician and ticket.technician.phone:
                send_msg91_whatsapp(ticket.technician.phone, f"Ticket #{ticket.id} status updated to {new_status} by {phone}.")
            send_msg91_whatsapp(phone, f"Ticket #{ticket.id} updated to {new_status}.")
            return jsonify({'status': 'updated'}), 200

        # Payment initiation shortcut (user can say 'pay <amount> <mode>' or similar)
        if lower.startswith('pay'):
            parts = text.split()
            if len(parts) >= 3:
                try:
                    amt = float(parts[1])
                    mode = parts[2]
                except:
                    send_msg91_whatsapp(phone, "Payment command format: Pay <amount> <mode>. e.g., Pay 500 Razorpay-IndianBank")
                    return jsonify({'status':'bad_format'}), 400
                # create pending payment record and reply with instructions / payment link (you must generate Razorpay link separately)
                user = session.query(User).filter_by(phone=phone).first()
                if not user:
                    user = User(name=phone, phone=phone)
                    session.add(user); session.commit()
                pay = Payment(user_id=user.id, amount=amt, mode=mode, status='Pending')
                session.add(pay); session.commit()
                send_msg91_whatsapp(phone, f"Payment record created (pending). Amount: ₹{amt:.2f}. Mode: {mode}. Please complete payment and share transaction reference.")
                return jsonify({'status':'payment_created','payment_id':pay.id}), 200
            else:
                send_msg91_whatsapp(phone, "Payment command: Pay <amount> <mode>. Example: Pay 500 Razorpay-IndianBank")
                return jsonify({'status':'bad_format'}), 400

        # My tickets
        if lower in ('my tickets', 'tickets'):
            user = session.query(User).filter_by(phone=phone).first()
            if not user:
                send_msg91_whatsapp(phone, "You have no tickets.")
                return jsonify({'status':'no_user'}), 200
            tickets = session.query(Ticket).filter_by(user_id=user.id).all()
            if not tickets:
                send_msg91_whatsapp(phone, "You have no tickets.")
                return jsonify({'status':'no_tickets'}), 200
            lines = []
            for t in tickets:
                tech_name = t.technician.name if t.technician else 'Unassigned'
                lines.append(f"#{t.id} - {t.status} - {tech_name} - { (t.issue[:60] + '...') if len(t.issue)>60 else t.issue }")
            send_msg91_whatsapp(phone, "Your Tickets:\n" + "\n".join(lines))
            return jsonify({'status':'sent_list'}), 200

        # default
        send_msg91_whatsapp(phone, "Unrecognized command. Use: New Complaint: <desc> | Update #<id> Resolved | Pay <amount> <mode> | My Tickets")
        return jsonify({'status':'unknown'}), 200

    except Exception as e:
        print('MSG91 webhook error', e)
        return jsonify({'status':'error','error':str(e)}), 500

# Razorpay webhook
@app.route('/webhook/razorpay', methods=['POST'])
def razorpay_webhook():
    payload = request.data
    signature = request.headers.get('X-Razorpay-Signature') or request.headers.get('x-razorpay-signature')
    if not RAZORPAY_WEBHOOK_SECRET:
        print('Warning: RAZORPAY_WEBHOOK_SECRET not set; skipping signature validation')
    else:
        # verify
        expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            print('Invalid Razorpay signature')
            abort(400, 'Invalid signature')
    data = request.json or {}
    event = data.get('event')
    if event == 'payment.captured' or event == 'payment.authorized':
        entity = data.get('payload', {}).get('payment', {}).get('entity', {})
        amount = float(entity.get('amount', 0)) / 100.0
        provider_txn_id = entity.get('id')
        notes = entity.get('notes') or {}
        mode = notes.get('mode') or 'Razorpay-Unknown'
        user_phone = notes.get('user_phone')
        ticket_id = notes.get('ticket_id')
        payment = Payment(amount=amount, currency='INR', mode=mode, provider_txn_id=provider_txn_id, status='Success', received_at=datetime.datetime.utcnow(), notes=json.dumps(notes))
        if user_phone:
            user = session.query(User).filter_by(phone=user_phone).first()
            if user:
                payment.user_id = user.id
        if ticket_id:
            try:
                payment.ticket_id = int(ticket_id)
            except:
                pass
        session.add(payment)
        session.commit()
        # notify MD or user
        if user_phone:
            send_msg91_whatsapp(user_phone, f"Payment of ₹{amount:.2f} received. Txn: {provider_txn_id}")
        return '', 200
    else:
        # handle other events if desired
        return '', 200

# Admin endpoints (secure these in production!)
@app.route('/admin/payments/manual', methods=['POST'])
def manual_payment():
    # simple token-based check (improve in prod)
    token = request.headers.get('X-ADMIN-TOKEN')
    if not token or token != os.environ.get('ADMIN_TOKEN'):
        return jsonify({'error':'unauthorized'}), 401
    data = request.json or {}
    mode = data.get('mode')
    amount = float(data.get('amount', 0))
    provider_txn_id = data.get('provider_txn_id')
    user_phone = data.get('user_phone')
    ticket_id = data.get('ticket_id')
    notes = data.get('notes')
    user_id = None
    if user_phone:
        user = session.query(User).filter_by(phone=user_phone).first()
        if not user:
            user = User(name=user_phone, phone=user_phone)
            session.add(user); session.commit()
        user_id = user.id
    pay = Payment(ticket_id=ticket_id, user_id=user_id, amount=amount, mode=mode, provider_txn_id=provider_txn_id, status='Success', received_at=datetime.datetime.utcnow(), notes=notes)
    session.add(pay); session.commit()
    if user_phone:
        send_msg91_whatsapp(user_phone, f"Payment of ₹{amount:.2f} recorded for your account. Ref: {provider_txn_id}")
    return jsonify({'status':'ok','payment_id':pay.id}), 200

@app.route('/admin/expenses', methods=['POST'])
def record_expense():
    token = request.headers.get('X-ADMIN-TOKEN')
    if not token or token != os.environ.get('ADMIN_TOKEN'):
        return jsonify({'error':'unauthorized'}), 401
    data = request.json or {}
    exp = Expense(amount=float(data['amount']), category=data.get('category'), paid_by=data.get('paid_by'), paid_to=data.get('paid_to'), voucher_no=data.get('voucher_no'), incurred_at=datetime.datetime.strptime(data['incurred_at'], '%Y-%m-%d %H:%M:%S') if data.get('incurred_at') else datetime.datetime.utcnow(), notes=data.get('notes'))
    session.add(exp); session.commit()
    return jsonify({'status':'ok','expense_id':exp.id}), 200

# Endpoint to trigger monthly recon (can be called by cron or manually)
@app.route('/admin/reconcile/monthly', methods=['POST'])
def trigger_monthly_recon():
    token = request.headers.get('X-ADMIN-TOKEN')
    if not token or token != os.environ.get('ADMIN_TOKEN'):
        return jsonify({'error':'unauthorized'}), 401
    data = request.json or {}
    year = data.get('year')
    month = data.get('month')
    fname = generate_excel_recon('monthly', year, month)
    pdf = generate_pdf_summary('monthly', year, month)
    # send summary to MD
    if MD_PHONE:
        text = generate_report_text('monthly', year, month)
        send_msg91_whatsapp(MD_PHONE, text)
        # to send files, upload to public URL (S3) and send link or media param
    return jsonify({'status':'ok','excel':fname,'pdf':pdf}), 200

# -----------------------------------------------------------------------------
# Scheduler jobs to send daily/monthly/yearly summaries
# -----------------------------------------------------------------------------
scheduler = BackgroundScheduler()

def send_periodic_report(period):
    try:
        text = generate_report_text(period)
        if MD_PHONE:
            send_msg91_whatsapp(MD_PHONE, text)
        print(f'Sent {period} report to MD (if configured).')
    except Exception as e:
        print('Error sending periodic report', e)

# Daily at 18:00 UTC
scheduler.add_job(lambda: send_periodic_report('daily'), 'cron', hour=18, minute=0)
# Monthly on 1st at 18:00 UTC
scheduler.add_job(lambda: send_periodic_report('monthly'), 'cron', day=1, hour=18, minute=0)
# Yearly on Jan 1 at 18:00 UTC
scheduler.add_job(lambda: send_periodic_report('yearly'), 'cron', month=1, day=1, hour=18, minute=0)

scheduler.start()

# -----------------------------------------------------------------------------
# Run app
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    print('Starting full WhatsApp Complaint Manager...')
    app.run(host='0.0.0.0', port=5000)
