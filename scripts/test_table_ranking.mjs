import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const here=path.dirname(fileURLToPath(import.meta.url));
const html=fs.readFileSync(path.join(here,'..','index.html'),'utf8');
const source=html.match(/function sortLeagueTableRows\([\s\S]*?\n}\n\nfunction computeStats\(/)?.[0];
assert.ok(source,'sortLeagueTableRows must be present in index.html');
const functionSource=source.replace(/\n\nfunction computeStats\($/,'');
const sandbox={};
vm.runInNewContext(`${functionSource}\nthis.sortLeagueTableRows=sortLeagueTableRows;`,sandbox);

const row=(t,{w=4,d=1,l=1,gf,ga})=>({t,s:{w,d,l,gf,ga}});

// Zbrojovka and Slovan have equal points and goal difference, have not
// completed both mutual fixtures, so overall goals scored decide the order.
{
  const rows=[
    row('FC Slovan Liberec',{gf:8,ga:3}),
    row('FC Zbrojovka Brno',{gf:10,ga:5})
  ];
  assert.equal(
    sandbox.sortLeagueTableRows(rows,[])[0].t,
    'FC Zbrojovka Brno'
  );
}

// Even one completed mutual match is not enough to activate head-to-head.
{
  const rows=[
    row('FC Slovan Liberec',{gf:8,ga:3}),
    row('FC Zbrojovka Brno',{gf:10,ga:5})
  ];
  const matches=[{h:'FC Slovan Liberec',a:'FC Zbrojovka Brno',score:'2:0'}];
  assert.equal(
    sandbox.sortLeagueTableRows(rows,matches)[0].t,
    'FC Zbrojovka Brno'
  );
}

// After both home-and-away fixtures, head-to-head takes precedence.
{
  const rows=[
    row('FC Slovan Liberec',{gf:8,ga:3}),
    row('FC Zbrojovka Brno',{gf:10,ga:5})
  ];
  const matches=[
    {h:'FC Slovan Liberec',a:'FC Zbrojovka Brno',score:'2:0'},
    {h:'FC Zbrojovka Brno',a:'FC Slovan Liberec',score:'1:1'}
  ];
  assert.equal(
    sandbox.sortLeagueTableRows(rows,matches)[0].t,
    'FC Slovan Liberec'
  );
}

// A durable snapshot is checked silently before the newer published roster is
// loaded; only the final merged state may trigger the automatic user alert.
const snapshotLoaderStart=html.indexOf('async function loadPublishedDataSnapshot()');
const snapshotLoaderEnd=html.indexOf('// Validate the final initialized state',snapshotLoaderStart);
const snapshotLoader=html.slice(snapshotLoaderStart,snapshotLoaderEnd);
assert.ok(snapshotLoaderStart>=0&&snapshotLoaderEnd>snapshotLoaderStart,'published snapshot loader must be present');
assert.match(snapshotLoader,/checkDataHealth\(false,false\)/);
assert.doesNotMatch(snapshotLoader,/runAutomaticDataHealthCheck\(\)/);

console.log('table ranking and startup validation tests: OK');
