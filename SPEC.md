Dani - 간단한 Github 기반 omx 자동화

## Stack
- Python
- uv
- FastAPI
- subprocess
- OMX & tmux
- Simplest stack as possible

## 워크플로우
Webhook Server가 돌면서 Github Event를 수신.
수신하면
1차 : 레포 별 분류 - registry.json에 등록된 레포만 고려
2차 : 새 이슈 생성, 이슈 코멘트 (agent comment 제외), 새 PR or PR comment 등록, main으로 보내는 PR인지 확인

새 이슈일 경우
omx session을 열어 "$ralplan을 이용한 구현 계획 작성 + 이슈가 필요한지 보고서 작성"
보고서는 깃허브 이슈 코멘트로 남김.

### 이슈 보고서 양식은 다음을 포함
1. 이 이슈가 왜 필요한가?
2. 이 이슈가 왜 필요하지 않은가?
3. 구현 계획
4. Agent Signature (실제로 깃허브 이슈에서 안보여도 ㄱㅊ)

Agent Signature로 Webhook event 분류 시에 signature 있는 것은 무시.

이 때 사용한 omx session의 session 번호 저장. json 파일에 각 레포 - 이슈 - 세션 - PR 번호를 저장할 것임.


이후, 인간이 '/approve'로 승인하면 그것을 인지하고 다음 '구현 단계로 넘어감'
여기까지가 issue request 단계

### 2. 구현

이슈 내용과 구현 계획을 담은 github comment들을 omx session에 시작 프롬프트와 함께 전달. instruction은 $ralph를 포함하며, 반드시 TDD (test code 우선 작성), 테스트 코드 모두 통과, e2e test 및 실제 실행하여 검증을 포함. omx --madmax 명령어를 사용하여 실행. 또한, 이슈 번호로 'Feature/#32'와 같은 브랜치 생성과 해당 브랜치에서 작업을 강제. 모든 작업이 완료되었다고 생각하면 dev branch로의 PR 생성. dev 없을 경우 main에서 만들며 dev는 항상 main으로부터 최신 유지

초기 PR이 만들어진 이벤트 수신하면 (PR.created) 자동으로 새로운 omx --madmax 세션 시작; 이슈 내용과 함께 코드 리뷰 및 '직접 사용하면서' 실제로 작동하는지를 확인하도록 함. 확인 후에는 PR comment로 확인 결과 남김.

이렇게 세 번의 확인 과정을 거침. (3번 이후에는 최종 verdict 단계로 넘어감, 이는 PR comment 개수로 정함)

### 3. 최종 Verdict

새로운 omx session으로, PR comment들 (3번의 확인) 과정을 종합하여 최종 verdict 'APPROVE or REJECT' 내림. Verdict Signature 삽입

Verdict Signature에 따라 APPROVE면 자동으로 dev branch로 머지, REJECT면 아무것도 하지 않고 인간의 손길을 기다리기.

### 4. Release loop

인간이 dev에서 release branch를 만든 후 release -> main 브랜치 (즉 main branch로 향하는 PR이 생성된 경우임) 경우 트리거
그 동안의 commit message와 PR diff + issue 내용을 분석하여 어떤 것이 바뀌었기에 release 전 체크해야 할 사항들을 체크리스트로 뽑아줌.
그리고 끝; 인간이 수동머지함.

## 규칙
- 최대한 심플하게 유지함.
- 한 레포당 하나의 일만 동시에 함. 레포 내에서 동시 병렬 진행 없음.
- omx --madmax 명령어에 특정 프롬프트 넣을 수 있는 방식을 반드시 알아보고 진행.
- retry logic 딱히 없음. 중간에 rate limit error 등에 대한 대응은 추후에.
- event가 들어오면 repo 별 queue에 쌓아놓고 한 번에 처리
- 새 레포에 세팅했을 경우에 bootstrap 시행 가능. 기존 이슈들을 queue에 넣어두고 순차 처리.
- typer로 간단한 cli 운영으로 서버 시작, bootstrap, 레포 등록 등 사용.


## Non-goals

- Any GUI, TUI
- 복잡한 서브 에이전트, 에이전트 위계 (에이전트는 그냥 omx session 쓰면됨)
- Over-engineering, 인간이 쉽게 이해할 정도의 minimal Python code 설계
