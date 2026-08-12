import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(HERE, '../..');

const source = JSON.parse(
  await readFile(path.join(HERE, 'consumer-contract.json'), 'utf8'),
);

function normalizedProjectPath(value) {
  const leading = value.startsWith('/') ? value : `/${value}`;
  return leading.endsWith('/') ? leading : `${leading}/`;
}

function stringEnvironment(name, fallback) {
  const value = process.env[name];
  return value === undefined || value === '' ? fallback : value;
}

export const contract = Object.freeze({
  ...source,
  pagesRoot: path.resolve(
    ROOT,
    stringEnvironment('ORINOCO_PAGES_ROOT', source.pages_root),
  ),
  projectPath: normalizedProjectPath(
    stringEnvironment('ORINOCO_PROJECT_PATH', source.project_path),
  ),
});

export function projectURL(origin, relative = '') {
  return new URL(`${contract.projectPath}${relative}`, origin).href;
}

export function reviewBundleFilename(pid) {
  const safe = pid
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return `orinoco-review-${safe}.json`;
}

export function editorApplyCommand(bundle) {
  const program = stringEnvironment(
    'ORINOCO_EDITOR_APPLY_PROGRAM',
    contract.editor.apply_program,
  );
  const arguments_ = contract.editor.apply_arguments.map((value) =>
    value === '{bundle}' ? bundle : value,
  );
  return { arguments: arguments_, program };
}
