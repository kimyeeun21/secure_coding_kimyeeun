import sqlite3
import uuid
import bcrypt
import time
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from flask_socketio import SocketIO, send

app = Flask(__name__)
app.config['SESSION_COOKIE_HTTPONLY'] = True   # 자바스크립트로 세션쿠키 접근 불가 (XSS 방어)
app.config['SESSION_COOKIE_SECURE'] = True     # HTTPS 환경에서만 세션 쿠키 전송 (HTTPS 적용 시 필요)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # 교차사이트 요청 제한 (CSRF에 도움됨)
app.config['SECRET_KEY'] = 'secret!'
DATABASE = 'market.db'
socketio = SocketIO(app)

import logging

# 로그 파일 설정
logging.basicConfig(
    filename='app.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# 데이터베이스 연결 관리: 요청마다 연결 생성 후 사용, 종료 시 close
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row  # 결과를 dict처럼 사용하기 위함
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# 테이블 생성 (최초 실행 시에만)
def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        # 사용자 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                bio TEXT,
                is_admin INTEGER DEFAULT 0  -- 관리자 여부 (0: 일반, 1: 관리자)
            )
        """)
        # 상품 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS product (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price TEXT NOT NULL,
                seller_id TEXT NOT NULL
            )
        """)
        # 신고 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report (
                id TEXT PRIMARY KEY,
                reporter_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)
        # 사용자 테이블 생성
        cursor.execute("""
            SELECT r.id AS report_id, r.reason, r.target_id AS user_id, u.username, u.is_suspended
                FROM report r
                JOIN user u ON r.target_id = u.id
                WHERE r.reason LIKE 'user:%'
        """)

        # 1대1 채팅 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS private_message (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 송금 내역 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transfer (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()

# 거래내역 페이지
@app.route('/transactions')
def transaction_history():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT t.*, u.username AS receiver_name
        FROM transfer t
        JOIN user u ON t.receiver_id = u.id
        WHERE t.sender_id = ?
        ORDER BY timestamp DESC
    """, (session['user_id'],))
    sent_transactions = cursor.fetchall()

    cursor.execute("""
        SELECT t.*, u.username AS sender_name
        FROM transfer t
        JOIN user u ON t.sender_id = u.id
        WHERE t.receiver_id = ?
        ORDER BY timestamp DESC
    """, (session['user_id'],))
    received_transactions = cursor.fetchall()

    return render_template('transactions.html', sent_transactions=sent_transactions, received_transactions=received_transactions)

# 관리자 홈
@app.route('/admin')
def admin_panel():
    if 'user_id' not in session or not is_admin():
        flash("관리자 권한이 필요합니다.")
        return redirect(url_for('dashboard'))
    return render_template('admin_panel.html')

# 사용자 관리 페이지
@app.route('/admin/manage_users')
def manage_users():
    if 'user_id' not in session or not is_admin():
        flash("관리자 권한이 필요합니다.")
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user")
    users = cursor.fetchall()
    return render_template('admin_manage_users.html', users=users)

# 상품 신고 관리 페이지 (기존 admin_reports 재활용 가능)
@app.route('/admin/manage_reports')
def manage_reports():
    return redirect(url_for('admin_reports'))

# 유저 신고 관리 페이지 (기존 admin_user_reports 재활용 가능)
@app.route('/admin/manage_user_reports')
def manage_user_reports():
    return redirect(url_for('admin_user_reports'))

# 송금 관리 페이지
@app.route('/admin/manage_transfers')
def manage_transfers():
    if 'user_id' not in session or not is_admin():
        flash("관리자 권한이 필요합니다.")
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT t.*, su.username AS sender_name, ru.username AS receiver_name
        FROM transfer t
        JOIN user su ON t.sender_id = su.id
        JOIN user ru ON t.receiver_id = ru.id
        ORDER BY t.timestamp DESC
    """)
    transfers = cursor.fetchall()
    return render_template('admin_manage_transfers.html', transfers=transfers)


# 관리자 여부 확인 함수
def is_admin():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT is_admin FROM user WHERE id = ?", (session['user_id'],))
    user = cursor.fetchone()
    return user and user['is_admin'] == 1
    
# 유저 간 송금   
@app.route('/send_money/<receiver_id>', methods=['GET', 'POST'])
def send_money(receiver_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    sender_id = session['user_id']
    db = get_db()
    cursor = db.cursor()

    # 수신자 정보
    cursor.execute("SELECT * FROM user WHERE id = ?", (receiver_id,))
    receiver = cursor.fetchone()
    if not receiver:
        flash("존재하지 않는 사용자입니다.")
        return redirect(url_for('user_list'))

    if request.method == 'POST':
        amount = int(request.form['amount'])

        # 송금 전 잔액 확인
        cursor.execute("SELECT balance FROM user WHERE id = ?", (sender_id,))
        sender = cursor.fetchone()
        if sender['balance'] < amount:
            flash("잔액이 부족합니다.")
            return redirect(url_for('send_money', receiver_id=receiver_id))

        # 송금 처리
        transfer_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO transfer (id, sender_id, receiver_id, amount) VALUES (?, ?, ?, ?)",
            (transfer_id, sender_id, receiver_id, amount)
        )
        cursor.execute("UPDATE user SET balance = balance - ? WHERE id = ?", (amount, sender_id))
        cursor.execute("UPDATE user SET balance = balance + ? WHERE id = ?", (amount, receiver_id))
        db.commit()

        flash(f"{receiver['username']}님에게 {amount}원을 송금했습니다.")
        return redirect(url_for('user_list'))

    # GET 요청 시: 송금 페이지 렌더링
    return render_template('send_money.html', receiver=receiver)


# 기본 라우트
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

# 회원가입
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        # 서버측 입력 검증
        import re
        if not re.match(r'^[a-zA-Z0-9_]{3,20}$', username):
            flash('아이디는 3~20자의 영문자, 숫자, 밑줄(_)만 가능합니다.')
            return redirect(url_for('register'))

        if len(password) < 6:
            flash('비밀번호는 최소 6자 이상이어야 합니다.')
            return redirect(url_for('register'))

        # 비밀번호 해시
        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        if cursor.fetchone() is not None:
            flash('이미 존재하는 사용자명입니다.')
            return redirect(url_for('register'))

        user_id = str(uuid.uuid4())
        cursor.execute("INSERT INTO user (id, username, password) VALUES (?, ?, ?)",
                       (user_id, username, hashed_pw))
        db.commit()
        flash('회원가입이 완료되었습니다. 로그인 해주세요.')
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        # 로그인 실패 횟수 및 차단시간 관리
        if 'login_attempts' not in session:
            session['login_attempts'] = 0
            session['lockout_time'] = 0

        # 로그인 시도 제한 확인
        if session['login_attempts'] >= 5:
            remaining = session['lockout_time'] - time.time()
            if remaining > 0:
                flash(f"너무 많은 로그인 시도로 인해 잠시 차단되었습니다. {int(remaining)}초 후에 다시 시도해주세요.")
                return redirect(url_for('login'))
            else:
                session['login_attempts'] = 0
                session['lockout_time'] = 0

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        user = cursor.fetchone()

        if user and bcrypt.checkpw(password.encode('utf-8'), user['password']):
            if user['is_suspended']:  # 휴먼 처리 확인
                flash('이 계정은 관리자에 의해 휴먼 처리되었습니다.')
                return redirect(url_for('login'))

            session['user_id'] = user['id']
            session.pop('login_attempts', None)  # 성공 시 실패횟수 초기화
            session.pop('lockout_time', None)
            flash('로그인 성공!')
            return redirect(url_for('dashboard'))
        else:
            session['login_attempts'] += 1
            if session['login_attempts'] >= 5:
                session['lockout_time'] = time.time() + 60  # 60초 차단
                flash('로그인 5회 이상 실패. 1분간 로그인 시도할 수 없습니다.')
            else:
                flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))

    return render_template('login.html')


# 로그아웃
@app.route('/logout')
def logout():
    session.pop('user_id', None)
    flash('로그아웃되었습니다.')
    return redirect(url_for('index'))

# 대시보드: 사용자 정보와 전체 상품 리스트 표시
@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    # 현재 사용자 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()
    # 모든 상품 조회
    cursor.execute("SELECT * FROM product")
    all_products = cursor.fetchall()
    return render_template('dashboard.html', products=all_products, user=current_user)

# 검색기능
@app.route('/search')
def search():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    query = request.args.get('query', '')
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()

    cursor.execute(
        "SELECT * FROM product WHERE title LIKE ? OR description LIKE ?",
        (f'%{query}%', f'%{query}%')
    )
    results = cursor.fetchall()

    return render_template('search_results.html', products=results, user=current_user, query=query)

# 상품 수정 기능
@app.route('/product/<product_id>/edit', methods=['GET', 'POST'])
def edit_product(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()

    if not product or product['seller_id'] != session['user_id']:
        flash('수정할 수 없는 상품입니다.')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        price = request.form['price']

        cursor.execute(
            "UPDATE product SET title = ?, description = ?, price = ? WHERE id = ?",
            (title, description, price, product_id)
        )
        db.commit()
        flash('상품이 수정되었습니다.')
        return redirect(url_for('view_product', product_id=product_id))

    return render_template('edit_product.html', product=product)

# 신고된 상품 삭제 기능
@app.route('/admin/delete_product/<product_id>', methods=['POST'])
def delete_reported_product(product_id):
    if 'user_id' not in session or not is_admin():
        flash('관리자만 삭제할 수 있습니다.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()

    # 상품 삭제
    cursor.execute("DELETE FROM product WHERE id = ?", (product_id,))
    # 해당 상품 관련 신고도 삭제
    cursor.execute("DELETE FROM report WHERE target_id = ?", (product_id,))
    db.commit()

    flash('신고된 상품이 삭제되었습니다.')
    return redirect(url_for('admin_reports'))


# 상품 삭제 기능
@app.route('/product/<product_id>/delete', methods=['POST'])
def delete_product(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()

    # 삭제할 상품이 현재 로그인한 사용자의 것인지 확인
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()

    if not product:
        flash('존재하지 않는 상품입니다.')
        return redirect(url_for('dashboard'))

    if product['seller_id'] != session['user_id']:
        flash('해당 상품을 삭제할 권한이 없습니다.')
        return redirect(url_for('dashboard'))

    # 삭제 수행
    cursor.execute("DELETE FROM product WHERE id = ?", (product_id,))
    db.commit()
    flash('상품이 삭제되었습니다.')
    return redirect(url_for('dashboard'))


# 프로필 페이지: bio 업데이트 가능
@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    db = get_db()
    cursor = db.cursor()

    if request.method == 'POST':
        bio = request.form.get('bio', '')
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')

        if current_password and new_password:
            # 현재 비밀번호 확인
            cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
            user = cursor.fetchone()

            if bcrypt.checkpw(current_password.encode('utf-8'), user['password']):
                hashed_pw = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
                cursor.execute("UPDATE user SET password = ? WHERE id = ?", (hashed_pw, session['user_id']))
                flash('비밀번호가 변경되었습니다.')
            else:
                flash('현재 비밀번호가 올바르지 않습니다.')

        # 소개글 업데이트
        cursor.execute("UPDATE user SET bio = ? WHERE id = ?", (bio, session['user_id']))
        db.commit()
        return redirect(url_for('profile'))

    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    user = cursor.fetchone()
    return render_template('profile.html', user=user)

# 사용자 목록 페이지
@app.route('/users')
def user_list():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id != ?", (session['user_id'],))
    users = cursor.fetchall()

    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()

    return render_template('user_list.html', users=users, user=current_user)



# 상품 등록
@app.route('/product/new', methods=['GET', 'POST'])
def new_product():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        import html
        import re

        title = request.form['title'].strip()
        description = request.form['description'].strip()
        price = request.form['price'].strip()

        # 서버측 입력 검증

        # 1. 제목 길이
        if len(title) < 3 or len(title) > 100:
            flash('제목은 3~100자 사이여야 합니다.')
            return redirect(url_for('new_product'))

        # 2. 가격 숫자 여부
        if not price.isdigit() or int(price) <= 0:
            flash('가격은 양의 숫자여야 합니다.')
            return redirect(url_for('new_product'))

        # 3. XSS 방지 (description)
        def strip_tags(text):
            return re.sub(r'<[^>]+>', '', text)

        title = html.escape(strip_tags(title))
        description = html.escape(strip_tags(description))

        # 4. DB 저장
        db = get_db()
        cursor = db.cursor()
        product_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO product (id, title, description, price, seller_id) VALUES (?, ?, ?, ?, ?)",
            (product_id, title, description, price, session['user_id'])
        )
        db.commit()
        flash('상품이 등록되었습니다.')
        return redirect(url_for('dashboard'))

    return render_template('new_product.html')



# 상품 상세보기
@app.route('/product/<product_id>')
def view_product(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()

    # 상품 정보 가져오기
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))

    # 판매자 정보 가져오기
    cursor.execute("SELECT * FROM user WHERE id = ?", (product['seller_id'],))
    seller = cursor.fetchone()

    # 현재 로그인한 사용자 정보
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()

    return render_template(
        'view_product.html',
        product=product,
        seller=seller,
        user=current_user  # ← 템플릿에서 user.id 등 쓸 수 있게 넘겨줌
    )

# 관리자 페이지
@app.route('/admin')
def admin_home():
    if 'user_id' not in session or not is_admin():
        flash("관리자만 접근할 수 있습니다.")
        return redirect(url_for('dashboard'))
    return render_template('admin_home.html')


# 관리자용 신고 목록 페이지
@app.route('/admin/reports')
def admin_reports():
    if 'user_id' not in session or not is_admin():
        flash('관리자만 접근할 수 있습니다.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()

    # product 관련 신고만 조회 (reason에 'product:' 포함)
    cursor.execute("""
        SELECT r.id AS report_id, r.reason, r.target_id AS product_id, p.title
        FROM report r
        JOIN product p ON r.target_id = p.id
        WHERE r.reason LIKE 'product:%'
    """)
    reports = cursor.fetchall()

    return render_template('admin_reports.html', reports=reports)

# 유저 신고 필터링
@app.route('/admin/user_reports')
def admin_user_reports():
    if 'user_id' not in session or not is_admin():
        flash('관리자만 접근할 수 있습니다.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT r.id AS report_id, r.reason, r.target_id AS user_id,
               u.username, u.is_suspended
        FROM report r
        JOIN user u ON r.target_id = u.id
        WHERE r.reason LIKE 'user:%'
    """)
    reports = cursor.fetchall()

    return render_template('admin_user_reports.html', reports=reports)

# 휴먼 처리 라우트
@app.route('/admin/suspend_user/<user_id>', methods=['POST'])
def suspend_user(user_id):
    if 'user_id' not in session or not is_admin():
        flash('관리자 권한이 필요합니다.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE user SET is_suspended = 1 WHERE id = ?", (user_id,))
    db.commit()

    flash('해당 유저가 휴먼 처리되었습니다.')
    return redirect(url_for('admin_user_reports'))

# 휴먼 유저 목록 조회
@app.route('/admin/suspended_users')
def suspended_users():
    if 'user_id' not in session or not is_admin():
        flash('관리자 권한이 필요합니다.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, username FROM user WHERE is_suspended = 1")
    users = cursor.fetchall()

    return render_template('suspended_users.html', users=users)

# 휴먼 유저 복구
@app.route('/admin/unsuspend_user/<user_id>', methods=['POST'])
def unsuspend_user(user_id):
    if 'user_id' not in session or not is_admin():
        flash('관리자 권한이 필요합니다.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("UPDATE user SET is_suspended = 0 WHERE id = ?", (user_id,))
    db.commit()

    flash('유저가 복구되었습니다.')
    return redirect(url_for('suspended_users'))

# 불량 상품 삭제
@app.route('/admin/delete_product/<product_id>', methods=['POST'])
def admin_delete_product(product_id):
    if 'user_id' not in session or not is_admin():
        flash('관리자만 상품을 삭제할 수 있습니다.')
        return redirect(url_for('dashboard'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM product WHERE id = ?", (product_id,))
    cursor.execute("DELETE FROM report WHERE target_id = ?", (product_id,))
    db.commit()
    flash('상품이 삭제되었습니다.')
    return redirect(url_for('admin_reports'))


# 1대1 채팅 페이지
@app.route('/chat/<receiver_id>', methods=['GET', 'POST'])
def private_chat(receiver_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()

    sender_id = session['user_id']

    # 메시지 전송 처리
    if request.method == 'POST':
        message = request.form['message']
        message_id = str(uuid.uuid4())
        cursor.execute("""
            INSERT INTO private_message (id, sender_id, receiver_id, message)
            VALUES (?, ?, ?, ?)
        """, (message_id, sender_id, receiver_id, message))
        db.commit()
        return redirect(url_for('private_chat', receiver_id=receiver_id))

    # 채팅 내역 조회 (양방향 모두 포함)
    cursor.execute("""
        SELECT * FROM private_message
        WHERE (sender_id = ? AND receiver_id = ?)
           OR (sender_id = ? AND receiver_id = ?)
        ORDER BY timestamp ASC
    """, (sender_id, receiver_id, receiver_id, sender_id))
    messages = cursor.fetchall()

    # 상대방 이름 표시용
    cursor.execute("SELECT username FROM user WHERE id = ?", (receiver_id,))
    receiver = cursor.fetchone()

    return render_template('private_chat.html', messages=messages, receiver=receiver)

# 신고하기
@app.route('/report', methods=['GET', 'POST'])
def report():
    if 'user_id' not in session:
        flash('로그인이 필요한 기능입니다.')
        return redirect(url_for('login'))

    if request.method == 'POST':
        report_type = request.form.get('type', '').strip()
        target_id = request.form.get('target_id', '').strip()
        reason = request.form.get('reason', '').strip()

        # 서버 측 입력 유효성 검증
        if report_type not in ['user', 'product']:
            flash('올바르지 않은 신고 유형입니다.')
            return redirect(url_for('dashboard'))

        if not target_id or not re.match(r'^[a-f0-9-]{36}$', target_id):
            flash('올바르지 않은 대상 ID입니다.')
            return redirect(url_for('dashboard'))

        if len(reason) < 2 or len(reason) > 100:
            flash('신고 사유는 2~100자 사이여야 합니다.')
            return redirect(url_for('dashboard'))

        report_id = str(uuid.uuid4())
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO report (id, reporter_id, target_id, reason) VALUES (?, ?, ?, ?)",
            (report_id, session['user_id'], target_id, f"{report_type}:{reason}")
        )
        db.commit()

        # 로그 남기기
        logging.info(f"신고 접수: type={report_type}, reporter={session['user_id']}, target={target_id}, reason={reason}")

        flash('신고가 접수되었습니다.')
        return redirect(url_for('dashboard'))

    return render_template('report.html')

# 유저별 마지막 메시지 전송 시간을 저장하는 딕셔너리
last_message_time = {}

# 실시간 채팅: 클라이언트가 메시지를 보내면 전체 브로드캐스트
@socketio.on('send_message')
def handle_send_message_event(data):
    user_id = data.get('user_id')  # 클라이언트에서 함께 보내야 함
    now = time.time()
    RATE_LIMIT_INTERVAL = 3  # 제한 간격 (초)

    if user_id:
        last_time = last_message_time.get(user_id, 0)
        if now - last_time < RATE_LIMIT_INTERVAL:
            send({'error': '메시지를 너무 자주 보낼 수 없습니다.'}, to=request.sid)
            return
        last_message_time[user_id] = now

    data['message_id'] = str(uuid.uuid4())
    send(data, broadcast=True)

if __name__ == '__main__':
    init_db()
    socketio.run(app, debug=True)
