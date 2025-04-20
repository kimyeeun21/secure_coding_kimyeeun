import sqlite3
import uuid
import bcrypt
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from flask_socketio import SocketIO, send

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
DATABASE = 'market.db'
socketio = SocketIO(app)

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
        db.commit()

# 관리자 여부 확인 함수
def is_admin():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT is_admin FROM user WHERE id = ?", (session['user_id'],))
    user = cursor.fetchone()
    return user and user['is_admin'] == 1

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
        username = request.form['username']
        password = request.form['password']
        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()) # 비밀번호 해시해서 저장장

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

# 로그인
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        user = cursor.fetchone()

        if user and bcrypt.checkpw(password.encode('utf-8'), user['password']): # 해시된 비밀번호 확인
            session['user_id'] = user['id']
            flash('로그인 성공!')
            return redirect(url_for('dashboard'))
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
        title = request.form['title']
        description = request.form['description']
        price = request.form['price']
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
        return redirect(url_for('login'))

    if request.method == 'POST':
        report_type = request.form['type']
        target_id = request.form['target_id']
        reason = request.form['reason']
        db = get_db()
        cursor = db.cursor()
        report_id = str(uuid.uuid4())

        cursor.execute(
            "INSERT INTO report (id, reporter_id, target_id, reason) VALUES (?, ?, ?, ?)",
            (report_id, session['user_id'], target_id, f"{report_type}:{reason}")
        )
        db.commit()
        flash('신고가 접수되었습니다.')
        return redirect(url_for('dashboard'))

    return render_template('report.html')


# 실시간 채팅: 클라이언트가 메시지를 보내면 전체 브로드캐스트
@socketio.on('send_message')
def handle_send_message_event(data):
    data['message_id'] = str(uuid.uuid4())
    send(data, broadcast=True)

if __name__ == '__main__':
    init_db()  # 앱 컨텍스트 내에서 테이블 생성
    socketio.run(app, debug=True)
