import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { networkInterfaces } from 'node:os';
import { readFile, realpath, access } from 'node:fs/promises';

const execute = promisify(execFile);
const virtual = /virtual|vmware|vbox|hyper-v|vethernet|loopback|\bvpn\b|\btap\b|\btun\b|tunnel|docker|container|wsl/i;
const usable = ip => {
  if (typeof ip !== 'string' || !/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) return false;
  const parts = ip.split('.').map(Number);
  return parts.every(n => n <= 255) && parts[0] > 0 && parts[0] < 224 && parts[0] !== 127 && !(parts[0] === 169 && parts[1] === 254);
};

export function selectEthernet(adapters) {
  return adapters.filter(adapter => adapter.physical === true && adapter.virtual !== true && adapter.up === true && adapter.type === 6 && !virtual.test(`${adapter.name} ${adapter.description || ''}`))
    .flatMap(adapter => (adapter.addresses || []).filter(usable).map(address => ({ adapter: adapter.name, address, metric: Number(adapter.metric) || 0, index: Number(adapter.index) || 0 })))
    .sort((a, b) => a.metric - b.metric || a.index - b.index || a.address.localeCompare(b.address))
    .map(({adapter, address}) => ({adapter, address}))[0] || null;
}

export async function detectEthernet() {
  try {
    if (process.platform === 'win32') {
      const command = `
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$addresses=@(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred)
$routes=@(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
$interfaces=@(Get-NetIPInterface -AddressFamily IPv4)
$result=@(Get-NetAdapter -Physical | ForEach-Object {
  $adapter=$_
  $route=$routes | Where-Object InterfaceIndex -eq $adapter.ifIndex | Sort-Object RouteMetric | Select-Object -First 1
  $interface=$interfaces | Where-Object InterfaceIndex -eq $adapter.ifIndex | Select-Object -First 1
  [pscustomobject]@{name=$adapter.Name;description=$adapter.InterfaceDescription;physical=[bool]$adapter.HardwareInterface;virtual=[bool]$adapter.Virtual;up=($adapter.Status -eq 'Up');type=[int]$adapter.InterfaceType;index=[int]$adapter.ifIndex;metric=$(if($route){[int]$route.RouteMetric+[int]$interface.InterfaceMetric}else{2147483647});addresses=@($addresses | Where-Object { $_.InterfaceIndex -eq $adapter.ifIndex -and -not $_.SkipAsSource } | Select-Object -ExpandProperty IPAddress)}
})
ConvertTo-Json -InputObject $result -Depth 4 -Compress`;
      const { stdout } = await execute('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command], { windowsHide: true, timeout: 10000, maxBuffer: 1024 * 1024, encoding: 'utf8' });
      return selectEthernet(JSON.parse(stdout.replace(/^\uFEFF/, '').trim()));
    }
    const adapters = [];
    let macEthernet = new Set();
    if (process.platform === 'darwin') {
      const { stdout } = await execute('networksetup', ['-listallhardwareports'], { timeout: 5000 });
      macEthernet = new Set([...stdout.matchAll(/Hardware Port: ([^\n]+)\nDevice: ([^\n]+)/g)].filter(match => /ethernet|\blan\b/i.test(match[1])).map(match => match[2].trim()));
    }
    for (const [name, entries] of Object.entries(networkInterfaces())) {
      let physical = macEthernet.has(name);
      if (process.platform === 'linux') {
        try {
          const device = await realpath(`/sys/class/net/${name}/device`);
          physical = !device.includes('/virtual/') && (await readFile(`/sys/class/net/${name}/type`, 'utf8')).trim() === '1' && (await readFile(`/sys/class/net/${name}/operstate`, 'utf8')).trim() === 'up';
          try { await access(`/sys/class/net/${name}/wireless`); physical = false; } catch {}
        } catch { physical = false; }
      }
      adapters.push({name, physical, type: 6, up: true, addresses: entries.filter(entry => !entry.internal && entry.family === 'IPv4').map(entry => entry.address)});
    }
    return selectEthernet(adapters);
  } catch (error) {
    console.warn('[知衡网络] 无法识别物理以太网：' + error.message.split('\n')[0]);
    return null;
  }
}
