# daangn-post-automation

직접 로그인한 브라우저 세션을 재사용해 JSON·CSV 원고를 게시하는 Python CLI입니다. 글쓰기 화면의 선택자를 설정할 수 있으며, 당근 카페용 설정 예제와 일반 웹 폼 예제를 제공합니다.

- 수동 로그인 후 쿠키·localStorage·IndexedDB를 로컬에 저장
- SQLite 큐, 중단 후 재개, 원고 ID 및 내용 기반 중복 검사
- 무작위 순서·대기 시간과 병렬 작업자 수 설정
- 제목·본문 대조, 선택적 이미지 첨부와 하단 문구
- 등록 결과가 불확실하면 `review`로 보류하고 자동 재등록 방지
- 401·403·429 및 로그인 화면 감지 시 새 등록 중단

이 프로젝트는 특정 플랫폼의 공식 SDK가 아닙니다. 로그인과 글 작성 권한이 있는 게시판에서 사용하며, 화면이 바뀌면 선택자를 조정해야 합니다. 원고 생성 기능은 포함하지 않습니다.

## 실행 환경

Python 3.10 이상, macOS 또는 Linux. Windows에서는 WSL2를 사용합니다. 네이티브 Windows는 파일 잠금과 신호 처리 때문에 지원하지 않습니다. Playwright Chromium과 로그인할 수 있는 브라우저 환경이 필요합니다.

## 설치

저장소를 내려받아 프로젝트 폴더에서 실행합니다.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m playwright install chromium
cp config.example.json config.json
```

Linux에서 브라우저 시스템 라이브러리가 필요하면 `python -m playwright install --with-deps chromium`을 사용합니다.

설치 후 `cafe-publisher` 또는 `python -m uploader`로 실행할 수 있습니다. 아래 예제는 후자를 사용합니다.

## 설정

`config.example.json`은 당근 카페 글쓰기 화면을 위한 템플릿입니다. `YOUR_CAFE_SLUG`를 자신의 카페 경로로 바꾸고, `target_id`와 게시판 선택자를 설정합니다. `success_url_pattern`의 경로도 같은 값으로 바꿉니다. 실제 계정이나 카페 주소는 포함하지 않습니다.

다른 웹사이트에서는 `examples/config.generic.json`을 복사해 URL과 화면 선택자를 바꿉니다. 예제의 `example.com`과 선택자는 설명용이며 실제 연결 대상이 아닙니다.

| 설정 | 역할 |
|---|---|
| `target_id` | 계정·사이트·게시판을 구분하는 로컬 식별자 |
| `login_url`, `write_url` | 로그인 페이지와 새 글 작성 페이지 |
| `authenticated` | 로그인 성공 시에만 표시되는 요소 |
| `title`, `body`, `submit` | 제목·본문·등록 버튼 선택자 |
| `success`, `success_url_pattern` | 등록 완료 화면의 요소와 URL 조건 |
| `board_picker`, `board_option` | 게시판 선택이 필요할 때 함께 설정 |
| `post_footer` | 본문 마지막에 한 번 붙일 문구; 기본값은 빈 문자열 |
| `min_body_characters`, `max_title_characters` | 본문 최소·제목 최대 길이 |
| `strip_sources_and_links` | 출처 구간·본문 링크 제거 여부; 공개 예제는 `false` |
| `include_images`, `image_input`, `images_ready` | 이미지 사용 여부와 업로드 선택자 |
| `editor_settle_ms` | 에디터 변경 내용이 폼에 반영될 때까지 기다릴 시간 |

선택자는 Playwright locator 문법을 사용합니다. iframe 안의 필드는 다음 형식으로 지정할 수 있습니다.

```json
{"selector": "body[contenteditable=true]", "frames": ["iframe.editor"]}
```

사이트별 공개 범위·검색 허용 설정은 웹 폼의 기본값을 따릅니다. 이 도구가 공개 범위를 별도로 강제하지는 않습니다.

## 처음 실행하기

```sh
# 브라우저에서 직접 로그인한 후 터미널에서 Enter
python -m uploader login
python -m uploader check

# 원고를 큐에 가져와 확인
python -m uploader import examples/posts.json
python -m uploader preview

# 브라우저와 등록 버튼을 사용하지 않는 사전 검사
python -m uploader run --only-from examples/posts.json --limit 1 --dry-run

# 지정한 원고 중 1개 실제 등록
python -m uploader run --only-from examples/posts.json --limit 1
python -m uploader status
```

`login`과 `check`는 글을 등록하지 않습니다. `run`에서 `--dry-run`을 빼면 실제 등록 버튼을 누릅니다. 입력 결과가 원고와 다르면 등록 전에 중단합니다. 등록 완료는 URL·완료 표시와 제목 일치 여부로 판정합니다.

## 원고 형식

`examples/posts.json`에 개인 정보가 없는 예제 원고 두 개를 제공합니다.

```json
[
  {"id": "post-0001", "title": "글 제목", "body": "게시할 본문", "images": []}
]
```

- `id`는 재실행할 때도 동일하게 유지합니다.
- `body`는 일반 텍스트이며 JSON의 `\n`으로 줄바꿈을 표현합니다.
- 이미지 경로는 원고 파일이 있는 폴더를 기준으로 해석합니다.
- CSV는 UTF-8의 `id,title,body,images` 열을 사용합니다. `images`는 JSON 배열 문자열 또는 빈 값입니다.
- 동일 ID의 내용이 달라지면 import 전체를 취소합니다. 같은 내용의 새 ID도 중복으로 생략합니다.
- 이미지가 없는 모드라도 원고의 `images`에 경로를 적었다면 import 시 해당 파일이 있어야 합니다.

본문 길이는 `post_footer`를 제외하고 검사합니다. 길이 단위는 Python 문자열의 문자 수입니다. 이미지·동영상·HTML 전체 문서와 플랫폼의 모든 특수 에디터를 자동으로 변환하는 기능은 없습니다.

## 병렬 등록과 재개

```sh
python -m uploader run --headless --no-images --workers 3 --limit 100 \
  --only-from examples/posts.json --interval-min 10 --interval-max 20
```

`--only-from`은 먼저 가져온 파일의 ID와 내용이 큐에 일치하는지 확인하고, 해당 파일의 대기 글만 처리합니다. 게시 완료 글은 다시 등록하지 않습니다.

일반 모드는 기본 작업자 3개·작업자별 30~60초 대기입니다. `--bulk`는 예제 설정 기준 작업자 10개·1~3초 대기·등록 10건마다 세션 저장을 사용합니다. 설정 파일과 CLI로 변경할 수 있고 CLI 값이 우선합니다. 작업자는 1~20개를 지원하며, 무작위 순서는 `--seed`로 재현할 수 있습니다. 완료 순서는 응답 시간에 따라 달라집니다.

SQLite의 게시 상태는 매번 즉시 저장합니다. 세션 저장 간격은 `--checkpoint-every`로 정하며 정상 종료와 오류 중단 때도 마지막 변경분을 저장합니다. Ctrl+C는 새 작업을 멈추고 진행 중인 등록의 결과를 기록한 뒤 종료합니다.

| 상태 | 의미 |
|---|---|
| `pending` | 등록 대기 |
| `preparing` | 입력 중; 재개 시 pending으로 복구 |
| `submitting` | 등록 결과 확인 중; 중단 후에는 review로 전환 |
| `posted` | 등록 완료 조건 확인 |
| `failed` | 등록 전 준비 단계의 재시도 횟수 초과 |
| `review` | 실제 게시판에서 등록 여부를 확인해야 함 |

```sh
# 게시판에서 실제 등록을 확인한 경우
python -m uploader resolve post-0001 posted --url 'https://example.com/posts/actual-id'

# 등록되지 않았음을 확인한 경우
python -m uploader resolve post-0001 pending
```

웹사이트와 로컬 DB 사이에서 완전한 1회 등록을 보장하지는 않습니다. 결과가 불확실한 글을 자동 재시도하지 않고 `review`로 남기는 이유입니다.

다른 계정·게시판은 별도의 큐와 세션을 사용합니다. `target_id`는 사용자가 정하는 값이며 실제 계정 전환을 자동 감지하지 않습니다.

```sh
python -m uploader --config other-config.json --db .state/other.sqlite3 \
  --session .auth/other.json run --limit 1
```

## 게시 후 검증

```sh
python -m uploader.verification --source examples/posts.json --out .state/verification.json
```

실제 게시글을 읽어 원고·제목·본문 길이·설정한 하단 문구와 이미지 수를 비교합니다. 링크 제거를 설정했을 때만 링크 제외도 검사합니다. 새 글은 등록하지 않습니다.

당근 템플릿의 기본 검증은 해당 서비스의 본문·목록 구조에 맞춰져 있습니다. 일반 사이트는 `verification.body`에 게시된 본문 선택자를 지정합니다. 목록 검사가 필요 없으면 `verification.verify_feed`를 `false`로 설정합니다. 목록 URL·카드·제목·더보기 선택자는 각각 `feed_url`, `items`, `title`, `more`로 바꿀 수 있습니다. 이 검증은 원고와 화면의 일치를 확인하며 내용의 사실 여부를 판정하지 않습니다.

## 개인정보와 공개 배포

로그인 세션은 `.auth/`, 큐·실행 로그·화면 검사 결과는 `.state/`에 저장합니다. 이 파일에는 계정 정보, 게시글 주소 또는 원고가 포함될 수 있습니다. 공개 예제와 소스에는 로그인 정보·실제 계정·게시 이력·개인 원고·수집 이미지가 없습니다.

`.gitignore`는 공개 파일만 허용하는 방식입니다. 배포 ZIP도 별도의 허용 목록으로 만듭니다.

```sh
python scripts/release.py --check
python scripts/release.py --output dist/cafe-publisher-source.zip
```

배포 도구는 로컬 설정·브라우저·세션·DB·콘텐츠·미디어·원격 주소와 Git 작성자 기록을 ZIP에 넣지 않습니다. 사용자 홈의 절대 경로와 알려진 자격 증명 형식도 검사합니다. 추가로 제외할 문자열은 비공개 JSON 배열을 만들고 `--deny-file`로 지정할 수 있습니다. 자동 검사가 모든 개인정보를 알아내는 것은 아니므로 배포 목록도 함께 확인합니다. 상세 내용은 [SECURITY.md](SECURITY.md)를 참고하세요.

## 개발

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/release.py --check
```

테스트는 임시 DB와 로컬 웹 서버를 사용합니다. 실제 계정이나 공개 게시판에 연결하지 않습니다. GitHub Actions 구성은 Linux·macOS에서 테스트하고 배포 ZIP을 검사합니다. 이 저장소를 GitHub에 올리면 워크플로가 실행됩니다.

수정·기여 방법은 [CONTRIBUTING.md](CONTRIBUTING.md), 라이선스는 [MIT](LICENSE)입니다. 포함된 MIT 라이선스는 소스 코드와 예제에 적용되며 사용자가 가져온 원고·이미지의 이용 권한을 대신 제공하지 않습니다.
