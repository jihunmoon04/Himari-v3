# HIMARI Windows 앱

PR 120에서 병합된 설계 rev. 5를 기준으로 만든 개발 중인 Windows 앱이다. .NET 10 Windows Desktop SDK가 필요하다. 서버 측 기기 API도 배포되어야 실제 연결할 수 있다.

```powershell
dotnet run --project clients/windows/Himari.Companion/Himari.Companion.csproj
```

Web의 Devices 화면에서 기기를 만들고, 앱에서 HTTPS 서버 주소와 짝짓기 코드를 입력한다. 기기 토큰은 Windows 자격 증명 관리자에 보관한다. 캐릭터나 말풍선을 드래그하여 이동한다. 표정 선택과 가짜 대화, 트레이의 보이기/숨기기·다시 연결·Web 열기·종료를 사용할 수 있다. 이미지들은 `animation-preview/pixel`에 있는 실험용 생성 시안이다.

구현한 범위: 짝짓기, 기기 토큰 저장, 대화 목록과 생성, 텍스트 전송·취소, 접속 신호, 사건 흐름과 자기 기기 작업의 실시간 응답·표정 수신. 네트워크 중단 시 입력은 유지하고 재접속한다. 짝짓기 응답이 유실되면 같은 코드와 토큰으로 재시도한다.

현재 제한: 말풍선은 긴 답의 펼쳐 보기와 자동 접기를 지원하지 않으며 대화 기록 전체를 탐색하는 UI는 없다. 단축키, 자산별 자연스러운 말하기·졸림 동작, 설정 저장은 아직 필요하다. 기기 API 구현 및 실제 두 PC 테스트는 아직 확인하지 못했다.

가짜 HTTP 전송으로 요청 형식과 SSE를 확인하는 독립 테스트:

```powershell
dotnet run --project clients/windows/Himari.Companion.ContractTests/Himari.Companion.ContractTests.csproj
```
