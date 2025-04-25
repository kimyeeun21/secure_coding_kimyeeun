## 환경 설정 및 실행 방법

이 프로젝트는 Flask 기반의 중고거래 플랫폼이며, Conda 가상환경을 사용하여 실행됩니다.

### 사전 요구사항

- Python 3.x
- [Anaconda 또는 Miniconda](https://www.anaconda.com/products/distribution) 설치
- Git (옵션)

### 가상환경 생성 및 설정

```bash
# 1. 프로젝트 디렉토리로 이동
cd secure-coding

# 2. Conda 환경 생성 (처음 한 번만)
conda create -n secure_coding python=3.10

# 3. 환경 활성화
conda activate secure_coding

# 4. 필요 패키지 설치
pip install flask flask_socketio bcrypt

# 5. 데이터베이스 자동 생성 및 서버 실행
python app.py