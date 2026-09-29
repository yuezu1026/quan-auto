async function fetchJson(url) {
  let response;
  try {
    response = await fetch(url);
  } catch (cause) {
    // 网络层就断了（后端没起、端口不对）。单独说这一句，
    // 否则浏览器控制台里的 "Failed to fetch" 会被当成「报告有问题」。
    throw new Error("连不上后端：" + url + "（后端起了吗？mvn -pl api spring-boot:run）");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    // 后端所有错误都是 {"error": "..."} 形状（见 ApiExceptionHandler）。
    throw new Error((body && body.error) || "HTTP " + response.status);
  }
  return body;
}

export function listReports() {
  return fetchJson("/api/reports");
}

export function loadReport(id) {
  return fetchJson("/api/reports/" + encodeURIComponent(id));
}
