const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const scriptBlocks = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)];
scriptBlocks.forEach((match, index) => {
  new vm.Script(match[1], { filename: `index.html#script-${index + 1}` });
});
console.log(JSON.stringify({ scriptBlocks: scriptBlocks.length, syntax: 'ok' }));
const constants = html.slice(
  html.indexOf('const STORAGE_CODEC_PREFIX'),
  html.indexOf('const PUBLISHED_DATA_FILE')
);
const functions = html.slice(
  html.indexOf('function storageCodeString'),
  html.indexOf('function getStorableData')
);

eval(`${constants}\n${functions}`);

const samples = [
  'Příliš žluťoučký kůň 🟢 '.repeat(5000),
  fs.readFileSync(path.join(root, 'rosters-live.json'), 'utf8')
];

for (const sample of samples) {
  const encoded = encodeStorageSnapshot(sample);
  const decoded = decompressStorageText(encoded);
  if (decoded !== sample) throw new Error('Browser snapshot codec round-trip mismatch.');
  console.log(JSON.stringify({
    sourceChars: sample.length,
    storedChars: encoded.length,
    ratio: Number((encoded.length / sample.length).toFixed(4))
  }));
}
