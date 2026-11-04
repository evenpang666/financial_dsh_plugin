import {replaceFile as rename} from './atomic-file.mjs';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {join} from 'node:path';

// Only non-secret workspace preferences live here. DSH owns provider credentials.
export async function createWebSettings(root, {currentSelection = () => ({}), llm} = {}) {
  const directory = join(root, 'data/web'), file = join(directory, 'settings.json');
  const shape = value => {
    if (value?.inheritHost === true && Object.keys(value).length === 1) return {};
    if (!value || Array.isArray(value) || Object.keys(value).some(key => !['provider', 'model', 'reasoningEffort'].includes(key)) ||
        !['provider', 'model'].every(key => typeof value[key] === 'string' && value[key].trim() && value[key].length <= 300) ||
        (value.reasoningEffort !== undefined && (typeof value.reasoningEffort !== 'string' || !value.reasoningEffort || value.reasoningEffort.length > 100))) throw Error('请选择有效的 DSH 模型与推理强度。');
    return {provider: value.provider, model: value.model, ...(value.reasoningEffort ? {reasoningEffort: value.reasoningEffort} : {})};
  };
  let current = {}, saving = Promise.resolve();
  try { const saved = JSON.parse(await readFile(file, 'utf8')); current = Object.keys(saved).length ? shape(saved) : {}; }
  catch (error) { if (error.code !== 'ENOENT') throw Error(`无法读取网页设置：${error.message}`); }
  async function catalog() {
    if (!llm) return [];
    const providers = await llm.listProviders();
    return (await Promise.all(providers.map(async provider => (await llm.listModels(provider.id)).map(row => ({
      provider: provider.id, id: row.id, label: `${provider.name || provider.id} · ${row.name || row.id}`,
    }))))).flat();
  }
  async function efforts(selection) {
    if (!selection.provider || !selection.model || !llm?.resolveModelInfo) return [];
    const info = await llm.resolveModelInfo(selection.provider, selection.model, AbortSignal.timeout(10000));
    return (info.reasoning?.efforts || []).map(row => ({id: row.id, label: row.name || row.id}));
  }
  return {
    get: () => ({...current}),
    async describe(query = {}) {
      const selection = {...(Object.keys(current).length ? current : currentSelection())};
      const models = await catalog();
      if (selection.provider && selection.model && !models.some(row => row.provider === selection.provider && row.id === selection.model)) models.push({provider: selection.provider, id: selection.model, label: `${selection.provider} · ${selection.model}`});
      const preview = query.provider || query.model ? shape(query) : selection;
      if ((query.provider || query.model) && !models.some(row => row.provider === preview.provider && row.id === preview.model)) throw Error('所选模型不在 DSH 模型目录中。');
      const host = currentSelection();
      return {...selection, hostModel: host.model, inheritHost: !Object.keys(current).length, models, reasoningEfforts: await efforts(preview)};
    },
    async update(value) {
      const next = shape(value);
      const operation = saving.catch(() => {}).then(async () => {
        if (Object.keys(next).length) {
          if (!(await catalog()).some(row => row.provider === next.provider && row.id === next.model)) throw Error('所选模型不在 DSH 模型目录中。');
          if (next.reasoningEffort && !(await efforts(next)).some(row => row.id === next.reasoningEffort)) throw Error('所选模型不支持该推理强度。');
        }
        await mkdir(directory, {recursive: true});
        await writeFile(file + '.tmp', JSON.stringify(next, null, 2) + '\n');
        await rename(file + '.tmp', file); current = next;
        return {...current, inheritHost: !Object.keys(current).length};
      });
      saving = operation; return operation;
    },
  };
}
