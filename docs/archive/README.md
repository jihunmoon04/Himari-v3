# 보관 문서

2026-11-24 문서 정리 때 `docs/`에서 옮긴 문서다. 기능이 빠졌거나 다른 문서로 대체되어, 지금 코드와 맞지 않는 내용이 많다. 지금 상태는 [status.md](../status.md), 지금 유효한 결정은 [decisions.md](../decisions.md)를 본다.

- 옛 결정 번호(`V3-001`~`V3-P109`)는 [decisions-v3.md](decisions-v3.md)에 그대로 있다. 코드 주석과 커밋 메시지는 이 번호를 계속 쓴다.
- 문서 안의 링크는 옮긴 뒤에도 열리도록 고쳤다. 본문은 고치지 않았다.

| 문서 | 무엇이었나 | 왜 보관했나 |
| --- | --- | --- |
| [decisions-v3.md](decisions-v3.md) | 2026-10-13~11-24의 결정 기록(`V3-001`~`V3-P109`, Confirmed·Proposed 구분) | 지금 유효한 것만 모은 [새 결정 기록](../decisions.md)(H 번호)으로 바꿈 |
| [open-questions.md](open-questions.md) | 2026-10-13의 미결정 항목(Q 번호) | `run_command`·Workspace·Pause 중심이라 대부분 풀렸거나 뺀 기능. 지금의 열린 문제는 [decisions.md](../decisions.md) 끝 |
| [control-pause-v0.1.md](control-pause-v0.1.md) | Pause/Resume 초안(V3-P55) | V3-P106에서 Pause를 뺌. 취소(`/cancel`)는 남음 |
| [control-run-budget-v0.1.md](control-run-budget-v0.1.md) | 한 턴의 토큰 예산·진척 없음·자리 비움(V3-P62) | V3-P106에서 Run 상한 `HIMARI_MAX_RUNS` 하나로 바뀜. 사용량 표시와 대화별 캐시 키(`session_id`)는 남음 |
| [current-state-contract-v0.1.md](current-state-contract-v0.1.md) | 활성 Environment·Workspace Binding·Artifact의 읽기 투영 | Workspace·Binding을 V3-P106에서 뺌. Current State 블록은 [prompt-composition.md](../prompt-composition.md) |
| [default-workspace-files-v0.1.md](default-workspace-files-v0.1.md) | 기본 Workspace 파일 계약 첫 초안 | v0.2로 대체(그 v0.2도 보관) |
| [default-workspace-files-v0.2.md](default-workspace-files-v0.2.md) | 사용자별 Root, 대화별 폴더와 교차 접근, sandbox 안의 `run_command` | Inside 제거(V3-P101), `run_command` 제거(V3-P106). 대화 폴더는 [server-slim-v0.1.md](../server-slim-v0.1.md)(V3-P109) |
| [sandbox-execution-v0.1.md](sandbox-execution-v0.1.md) | `run_command`용 Bubblewrap/Landlock sandbox 재설계 | `run_command` 제거(V3-P101·V3-P106). 문서·이미지 worker의 server sandbox는 `src/himari/sandbox`에 남음 |
| [sandbox-implementation-v0.2.md](sandbox-implementation-v0.2.md) | v2 코드 이식 지도, Environment 계약, 명령 실행 한도 | 위와 같음 |
| [machine-workspace-v0.1.md](machine-workspace-v0.1.md) | `machine` Environment와 `himari-runner`, 붙인 Workspace, `git_push`(V3-P74) | V3-P100(쓰기·`git_push`), V3-P106(`run_command`·Workspace), V3-P109(runner). 지금 `machine`은 [server-slim-v0.1.md](../server-slim-v0.1.md) |
| [web-files-v0.1.md](web-files-v0.1.md) | Web 파일 탐색기·편집기(V3-P75) | V3-P99(보기 전용), V3-P106(탐색기 제거) |
| [editor-agent-v0.1.md](editor-agent-v0.1.md) | 편집기 선택을 대화에 붙이기, 바뀐 파일 검토(V3-P76) | V3-P99, V3-P106 |
| [pdf-ink-v0.1.md](pdf-ink-v0.1.md) | PDF 필기와 에이전트에게 넘기기(V3-P77) | V3-P96 |
| [pdf-notebook-v0.1.md](pdf-notebook-v0.1.md) | PDF 필기 자동 저장(V3-P82) | V3-P96 |
| [mcp-apps-v0.1.md](mcp-apps-v0.1.md) | MCP 서버가 준 Tool 화면(V3-P58·V3-P63) | V3-P97 |
| [mcp-stdio-v0.1.md](mcp-stdio-v0.1.md) | stdio MCP 실행과 Sandbox 모듈 분리(V3-P59) | V3-P109에서 stdio 실행을 뺌. §3의 Sandbox 모듈 분리는 지금 `src/himari/sandbox`의 근거로 남음 |
| [hub-projects-ui-v0.1.md](hub-projects-ui-v0.1.md) | Web에서 hub 프로젝트·기기 폴더 다루기(V3-P103) | V3-P106 |
| [workbench-v0.1.md](workbench-v0.1.md) | 작업 앱과 hub 실행기 관리(V3-P104) | V3-P106 |
| [unified-workspace-v0.1.md](unified-workspace-v0.1.md) | 옛 Web의 대화·파일 함께 보기 배치 | 옛 Web(V3-P102 W6d), 파일 탐색기(V3-P106), PDF 필기(V3-P96)가 없어짐 |
| [responsive-workspace-review.md](responsive-workspace-review.md) | 위 배치의 휴대폰·태블릿 점검 기록 | 위와 같음 |
| [web-drafts-v0.1.md](web-drafts-v0.1.md) | 옛 Web의 작성 중인 것 지키기(V3-P78) | 옛 Web이 없어짐(V3-P102 W6d), §4는 V3-P99로 철회. 자리별 초안은 React Web(`web/src/lib/drafts.ts`)에 옮겨짐 |
| [web-chat-basics-v0.1.md](web-chat-basics-v0.1.md) | 옛 Web의 화면 주소·입력·코드 복사 등 기본기(V3-P79) | 옛 Web이 없어짐. 화면 주소(`/c/<id>` 등)와 코드 복사는 React Web에 있음 |
| [web-activity-paste-v0.1.md](web-activity-paste-v0.1.md) | 다른 대화의 상태 표시와 붙여넣기 첨부(V3-P80) | 옛 Web이 없어짐. 사이드바의 작업 중 표시와 붙여넣기 첨부는 React Web에 있음 |
