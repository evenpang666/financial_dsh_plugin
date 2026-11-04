import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile, access} from 'node:fs/promises';
import {decodePortfolioImage, MAX_PORTFOLIO_IMAGE_BYTES} from '../lib/portfolio-image.mjs';
import {importPortfolio} from '../lib/portfolio.mjs';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG04AAAAASUVORK5CYII=', 'base64');
const image = `data:image/png;base64,${png.toString('base64')}`;

test('image uploads validate size, declared type, magic bytes and canonical base64', () => {
  assert.deepEqual(decodePortfolioImage(image), {bytes:png, extension:'png'});
  for (const invalid of [null, {}, '', 'https://example.com/image.png', image.replace('image/png', 'image/svg+xml'), image.replace('image/png','image/jpeg'), 'data:image/png;base64,abcd', image + '=', `data:image/png;base64,${Buffer.alloc(MAX_PORTFOLIO_IMAGE_BYTES + 1).toString('base64')}`]) assert.throws(() => decodePortfolioImage(invalid));
});

test('a screenshot without share counts returns editable text and never resolves or saves holdings', async () => {
  let imagePath;
  const result = await importPortfolio({image, preview:false}, {
    signal:new AbortController().signal, progress(){},
    data:async()=>{throw Error('Images must never write holdings');},
    research:async (prompt, options) => {
      assert.deepEqual(options.allowedTools, []); assert.equal(options.purpose, 'portfolio');
      assert.match(prompt, /不添加数量/);
      imagePath = options.images[0]; assert.deepEqual(await readFile(imagePath), png);
      return {text:JSON.stringify({text:'002297 博云新材\n000066 中国长城',issues:['底部一行裁切，无法完整读取。']}),model:'test-model'};
    }
  });
  assert.equal(result.preview, true); assert.equal(result.model, 'test-model');
  assert.equal(result.recognizedText,'002297 博云新材\n000066 中国长城');
  assert.deepEqual(result.holdings,[]); assert.equal(result.issues.length,1);
  await assert.rejects(access(imagePath), {code:'ENOENT'});
});

test('image temporary files are removed after recognition failure and cancellation', async () => {
  for (const mode of ['failure','cancel','invalid']) {
    let path; const controller = new AbortController();
    await assert.rejects(importPortfolio({image}, {signal:controller.signal,progress(){},research:async (_prompt, options) => {
      path=options.images[0];
      if (mode === 'failure') throw Error('Recognition failed');
      if (mode === 'cancel') controller.abort();
      return {text:mode === 'invalid' ? '{"text":"","issues":[]}' : '{"text":"002297 博云新材","issues":[]}'};
    }}));
    await assert.rejects(access(path), {code:'ENOENT'});
  }
});
