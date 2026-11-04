"""Exercise the real settings markup and script together without changing user settings."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHROME = shutil.which('google-chrome') or shutil.which('chromium') or next((str(path) for path in (
    Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
    Path('C:/Program Files/Microsoft/Edge/Application/msedge.exe'),
) if path.exists()), None)


@unittest.skipUnless(CHROME and os.getenv('FINANCE_BROWSER_TESTS') == '1', 'Set FINANCE_BROWSER_TESTS=1 for browser checks')
class SettingsBrowserTests(unittest.TestCase):
    def test_load_save_host_defaults_and_timeout_recovery(self):
        (ROOT / '.test-tmp').mkdir(exist_ok=True)
        setup = r'''
window.scriptErrors=[];window.addEventListener('error',event=>scriptErrors.push(event.message));
window.setInterval=()=>0;
const originalTimeout=AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout=ms=>originalTimeout(Math.min(ms,50));
window.saved=[];window.selection=null;
window.fetch=async(url,options={})=>{
 if(url==='/api/status')return {ok:true,json:async()=>({managedBy:'dsh'})};
 if(!url.startsWith('/api/settings'))throw Error('unexpected request '+url);
 if(window.failNext){window.failNext=false;return new Promise((_,reject)=>{
   if(!options.signal)throw Error('request has no timeout');
   options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true});
 });}
 if(options.method==='PUT'){
   const value=JSON.parse(options.body);saved.push(value);selection=value.inheritHost?null:value;
   return {ok:true,json:async()=>value};
 }
 const query=new URL(url,'http://fixture').searchParams;
 const selected=query.get('model')||selection?.model||'default';
 return {ok:true,json:async()=>({provider:'fixture',model:selection?.model||'default',hostModel:'default',
   inheritHost:!selection,reasoningEffort:selection?.reasoningEffort,
   models:[{provider:'fixture',id:'default',label:'默认模型'},{provider:'fixture',id:'vision',label:'图片模型'}],
   reasoningEfforts:[{id:selected==='vision'?'low':'high',label:selected==='vision'?'低':'高'}]})};
};
'''
        checks = r'''
;(async()=>{
 const check=(ok,message)=>{if(!ok)throw Error(message)};
 const pause=()=>new Promise(resolve=>setTimeout(resolve,150));
 try{
  const form=document.querySelector('#agentSettingsForm'),model=document.querySelector('#agentModel');
  const effort=document.querySelector('#reasoningEffort'),save=document.querySelector('#saveAgentSettings');
  const reload=document.querySelector('#reloadSettings'),status=document.querySelector('#agentSettingsStatus');
  await pause();
  if(window.testTimeout){
    check(status.textContent.includes('超时'),'timeout not explained');
    check(!reload.disabled,'timeout left retry disabled');reload.click();await pause();
  }
  check(scriptErrors.length===0,scriptErrors.join(';'));
  check(status.textContent.includes('已加载'),'settings did not finish loading');
  check(model.options.length===3&&!model.disabled&&!save.disabled,'models or save button unavailable');
  check(model.value===''&&effort.disabled,'host inheritance not restored');
  model.value=JSON.stringify(['fixture','vision']);model.dispatchEvent(new Event('change'));await pause();
  check(!effort.disabled&&effort.options[1].value==='low','selected model efforts not loaded');
  effort.value='low';save.click();await pause();
  check(saved.length===1&&saved[0].model==='vision'&&saved[0].reasoningEffort==='low','explicit model not saved');
  check(!save.disabled,'save stayed disabled');
  model.value='';model.dispatchEvent(new Event('change'));save.click();await pause();
  check(saved.length===2&&saved[1].inheritHost===true,'host default blocked by form validation');
  const light=document.querySelector('input[name=theme][value=light]');light.click();
  check(document.documentElement.dataset.theme==='light','theme control did not initialize');
  check(scriptErrors.length===0,scriptErrors.join(';'));
  check(document.documentElement.scrollWidth<=innerWidth,'settings overflow');
  document.body.dataset.settingsCheck='passed';
 }catch(error){document.body.dataset.settingsCheck=error.message;}
})();
'''
        for fail_first in (False, True):
            for width, height in ((1440, 900), (390, 844)):
                with self.subTest(timeout=fail_first, width=width), tempfile.TemporaryDirectory(dir=ROOT / '.test-tmp', prefix='settings-browser-') as directory:
                    html = (ROOT / 'web/settings.html').read_text(encoding='utf-8')
                    html = html.replace('<head>', '<head><script>'+setup+f'window.failNext=window.testTimeout={str(fail_first).lower()};</script>')
                    html = re.sub(r'<link rel="stylesheet" href="/([^"]+)">', lambda match: '<style>'+(ROOT/'web'/match.group(1)).read_text(encoding='utf-8')+'</style>', html)
                    html = re.sub(r'<script src="/([^"]+)"[^>]*></script>', lambda match: '<script>'+(ROOT/'web'/match.group(1)).read_text(encoding='utf-8')+'</script>', html)
                    html = html.replace('</body>', '<script>'+checks+'</script></body>')
                    fixture = Path(directory)/'settings.html'; fixture.write_text(html, encoding='utf-8')
                    result = subprocess.run([CHROME, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                        f'--user-data-dir={directory}/profile', '--virtual-time-budget=5000', f'--window-size={width},{height}',
                        '--dump-dom', fixture.as_uri()], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
                    marker = re.search(r'data-settings-check="([^"]+)"', result.stdout)
                    self.assertIsNotNone(marker, result.stderr[-1000:])
                    self.assertEqual(marker.group(1), 'passed')


if __name__ == '__main__':
    unittest.main()
