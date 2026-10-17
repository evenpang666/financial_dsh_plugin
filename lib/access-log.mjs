export async function logWorkspaceAccess(url, {network, log = console.log, prefix = '[DSH 插件]', request = fetch} = {}) {
  let lanUrl, adapter;
  if (network !== undefined) {
    if (network) { lanUrl = `http://${network.address}:${new URL(url).port}/`; adapter = network.adapter; }
  } else {
    try {
      const response = await request(new URL('/api/network', url), {signal: AbortSignal.timeout(1500)});
      if (response.ok) ({lanUrl, adapter} = await response.json());
    } catch {}
  }
  log(`${prefix} 本机：${url}`);
  log(lanUrl ? `${prefix} 局域网：${lanUrl} · ${adapter || '以太网'}` : `${prefix} 局域网：未检测到可用的物理以太网地址`);
}
