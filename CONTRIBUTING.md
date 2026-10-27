# 기여하기

Python 3.10 이상과 macOS 또는 Linux에서 작업합니다.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
python -m unittest discover -s tests -v
python scripts/release.py --check
```

테스트는 임시 폴더·DB·로컬 HTTP 서버와 가짜 세션만 사용합니다. 실제 로그인 정보, 원고, 게시글 URL, 화면 캡처를 테스트 픽스처로 추가하지 마세요. URL 예시는 `example.com`이나 로컬 서버를 사용하세요.

등록 버튼을 누른 뒤 결과가 불확실한 경우 `review`로 남기는 동작과 HTTP 401·403·429 중단 동작을 유지합니다. 로그인·세션·큐 상태 처리 변경에는 그 실패 상황을 재현하는 테스트를 함께 추가합니다.

새 공개 파일은 `.gitignore`와 `scripts/release.py`의 허용 목록에 함께 추가합니다. 수정한 파일만 배포되는지 ZIP 내용을 확인하세요. GitHub Actions는 실제 계정에 로그인하거나 외부 게시판에 글을 쓰지 않습니다.
