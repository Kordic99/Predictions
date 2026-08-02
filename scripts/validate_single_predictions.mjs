import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(scriptDir, '..', 'index.html'), 'utf8');

const inlineScripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1])
  .join('\n');

assert.ok(inlineScripts, 'index.html must contain an inline script');
new Function(inlineScripts);

assert.equal((html.match(/data-tab="pred2"/g) || []).length, 1, 'one Predictions tab is required');
assert.equal((html.match(/data-tab="pred"/g) || []).length, 0, 'legacy Prediction 1 tab must be removed');
assert.equal((html.match(/id="sec-pred2"/g) || []).length, 1, 'the retained Predictions page is missing');
assert.equal((html.match(/id="sec-pred"/g) || []).length, 0, 'legacy Prediction 1 page must be removed');
assert.match(html, /data-tab="pred2"[^>]*>Predictions<\/button>/, 'retained tab must be named Predictions');

const activeSystemMatch = inlineScripts.match(/const\s+ACTIVE_PREDICTION_SYSTEM\s*=\s*'([^']+)'\s*;/);
const activeFilterMatch = inlineScripts.match(/function\s+isActivePredictionLock\s*\([^)]*\)\s*\{[\s\S]*?\n\}/);
assert.ok(activeSystemMatch, 'active prediction system constant is missing');
assert.ok(activeFilterMatch, 'active Accuracy migration filter is missing');

const context = {};
vm.createContext(context);
vm.runInContext(
  `const ACTIVE_PREDICTION_SYSTEM=${JSON.stringify(activeSystemMatch[1])};\n${activeFilterMatch[0]}\n` +
  'globalThis.testActivePredictionLock=isActivePredictionLock;',
  context
);

const legacy = { id: 'old', predSystem: 'P1', home: 'A', away: 'B' };
const retained = { id: 'new', predSystem: 'P2', home: 'A', away: 'B' };
const unlabeledLegacy = { id: 'legacy', home: 'A', away: 'B' };
const migrated = [legacy, retained, unlabeledLegacy].filter(context.testActivePredictionLock);

assert.deepEqual(migrated.map(row => row.id), ['new'], 'migration must retain only former Prediction 2 data');
assert.match(inlineScripts, /lockedPredictions\.filter\(isActivePredictionLock\)\.map\(normalizeLockedPrediction\)/, 'exports must exclude legacy Accuracy rows');
assert.match(inlineScripts, /\[ACTIVE_PREDICTION_SYSTEM\]\.forEach\(system=>/, 'kickoff lock must run only for the retained model');
assert.doesNotMatch(inlineScripts, /\['P1','P2'\]\.forEach\(system=>/, 'kickoff lock must not recreate Prediction 1 rows');

console.log('Single Predictions migration validation OK');
