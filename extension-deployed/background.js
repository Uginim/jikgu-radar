// 서비스 워커: 툴바 아이콘을 누르면 사이드 패널을 연다. 현재 탭 캡처(captureVisibleTab)는 여기서만 부를 수 있다.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "capture") {
    chrome.tabs.captureVisibleTab(msg.windowId, { format: "png" })
      .then((dataUrl) => reply({ ok: true, dataUrl }))
      .catch((e) => reply({ ok: false, error: String(e) }));
    return true; // 비동기 응답
  }
});
