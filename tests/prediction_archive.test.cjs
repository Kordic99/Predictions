'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const A=require('../prediction-archive-core.js');
const {makeRecord,empty,digest}=require('../scripts/archive_predictions.cjs');
const {execFileSync}=require('node:child_process');
const {publishArchive}=require('../scripts/publish_prediction_archive.cjs');
const {openApp}=require('../scripts/archive_runtime.cjs');
const root=path.resolve(__dirname,'..');
const fixture={officialMatchId:'8327',round:1,home:'FC Viktoria Plzeň',away:'FC Slovan Liberec',date:'2026-07-25',time:'17:00'};
const lock=()=>({id:'a',home:'Viktoria Plzeň',away:'Slovan Liberec',season:A.SEASON,round:1,predSystem:'P2',hP:50,dP:30,aP:20,predOutcome:'1X',topScore:'1:0',savedAt:'2026-07-25T12:00:00Z'});
const record=()=>makeRecord(lock(),fixture,{kind:'test'},'2026-07-25T12:01:00Z');

test('Prague kickoff conversion uses summer/winter offsets independent of runner timezone',()=>{
  assert.equal(new Date(A.kickoffMs(fixture)).toISOString(),'2026-07-25T15:00:00.000Z');
  assert.equal(new Date(A.kickoffMs({...fixture,date:'2026-10-25'})).toISOString(),'2026-10-25T16:00:00.000Z');
  assert.equal(A.kickoffMs({...fixture,time:null}),null);
  assert.equal(A.kickoffMs({...fixture,date:'2026-03-29',time:'02:30'}),null);
});
test('late, exactly-at-kickoff, missing and backdated submissions are excluded',()=>{
  assert.equal(A.eligibility(lock(),fixture).eligible,true);
  for(const savedAt of ['2026-07-25T15:00:00Z','2026-07-26T10:00:00Z','invalid'])assert.equal(A.eligibility({...lock(),savedAt},fixture).eligible,false);
  assert.equal(A.eligibility(lock(),fixture,'2026-07-25T16:00:00Z').reason,'received_after_kickoff');
  assert.equal(A.eligibility(lock(),{...fixture,time:null}).eligible,false);
});
test('identities must agree with official fixtures, not just supplied IDs',()=>{
  assert.equal(A.fixtureFor(lock(),[fixture]),fixture);
  assert.throws(()=>A.fixtureFor({...lock(),officialMatchId:'8327',away:'Sparta Prague'},[fixture]));
  assert.throws(()=>A.fixtureFor({...lock(),round:2},[fixture]));
});
test('invalid probabilities and missing creation time fail closed',()=>{
  for(const bad of [{hP:NaN},{dP:-1},{hP:99},{savedAt:null},{predOutcome:'anything'}])assert.throws(()=>A.validateLock({...lock(),...bad}));
});
test('immutable append retains old records; duplicates are idempotent; mutation fails',()=>{
  const r=record(),a=A.appendRecords(empty(),[r]);
  assert.equal(A.appendRecords(a,[r]).records.length,1);
  assert.equal(A.appendRecords(a,[]).records.length,1);
  assert.throws(()=>A.appendRecords(a,[{...r,archivedAt:'2026-07-25T13:00:00Z'}]),/overwrite/);
  assert.equal(r.recordId,digest(A.signature(r.lock)));
  assert.throws(()=>A.assertAppendOnly(a,empty()),/removed or changed/);
  assert.throws(()=>A.assertAppendOnly(a,{...a,records:[{...r,source:{kind:'changed'}}]}),/removed or changed/);
  assert.doesNotThrow(()=>A.assertAppendOnly(a,a));
});
test('distinct local versions are retained, but exact copies do not multiply',()=>{
  const a=lock(),b={...a,id:'b'},c={...a,hP:49,aP:21};
  assert.equal(A.mergeLocks([a,b],[c]).length,2);
});
test('recovered archive contains 40 unchanged original predictions, two late',()=>{
  const archive=A.validateArchive(JSON.parse(fs.readFileSync(path.join(root,A.FILE),'utf8')));
  const recovered=archive.records.filter(r=>r.source.kind==='recovered-export');
  assert.equal(recovered.length,40);
  assert.equal(recovered.filter(r=>!r.evaluation.eligible).length,2);
  for(const row of archive.records)assert.equal(row.recordId,digest(A.signature(row.lock)));
});
test('CAS publisher retries a concurrent change, retains the other record, then is idempotent',async()=>{
  const first=record();
  const second=makeRecord({...lock(),hP:49,aP:21},fixture,{kind:'test'},'2026-07-25T12:02:00Z');
  let remote=empty(),put=0;
  const fetchBefore=global.fetch,tokenBefore=process.env.GITHUB_TOKEN;
  process.env.GITHUB_TOKEN='test-only-not-a-credential';
  global.fetch=async(url,options)=>{
    const route=url.replace('https://api.github.com/repos/Kordic99/Predictions','');
    let data;
    if(route==='/git/ref/heads/main')data={object:{sha:'head'}};
    else if(route==='/git/commits/head')data={tree:{sha:'tree'}};
    else if(route==='/git/trees/tree?recursive=1')data={tree:[{path:A.FILE,sha:'blob-'+put}]};
    else if(route.startsWith('/git/blobs/'))data={content:Buffer.from(JSON.stringify(remote)).toString('base64')};
    else if(route==='/contents/'+A.FILE&&options.method==='PUT'){
      put++;
      if(put===1){remote=A.appendRecords(remote,[second]);return {ok:false,status:409};}
      const body=JSON.parse(options.body);
      remote=JSON.parse(Buffer.from(body.content,'base64').toString());
      data={commit:{sha:'published'}};
    }else throw new Error('Unexpected GitHub mutation: '+route);
    return {ok:true,status:200,json:async()=>data};
  };
  try{
    const local=A.appendRecords(empty(),[first]);
    await publishArchive(local);
    assert.equal(put,2);assert.equal(remote.records.length,2);
    assert.deepEqual(remote.records[0],second);
    await publishArchive(local);assert.equal(put,2);
  }finally{global.fetch=fetchBefore;if(tokenBefore===undefined)delete process.env.GITHUB_TOKEN;else process.env.GITHUB_TOKEN=tokenBefore;}
});
test('default and legacy server commands never create or rewrite predictions',()=>{
  const before=fs.readFileSync(path.join(root,A.FILE));
  for(const args of [[],['generate'],['validate']]){
    execFileSync(process.execPath,['scripts/archive_predictions.cjs',...args],{cwd:root,encoding:'utf8'});
    assert.deepEqual(fs.readFileSync(path.join(root,A.FILE)),before);
  }
  const workflow=fs.readFileSync(path.join(root,'.github/workflows/archive-predictions.yml'),'utf8');
  assert.doesNotMatch(workflow,/\n\s+schedule:/);
  assert.doesNotMatch(workflow,/archive_predictions\.cjs generate/);
});
test('browser never auto-locks before kickoff; manual lock still captures and freezes current inputs',{timeout:120000},async()=>{
  const app=await openApp(root);
  try{
    await app.page.clock.install({time:new Date('2030-01-01T10:30:00Z')});
    await app.page.evaluate(()=>{
      const f=OFFICIAL_SCHEDULE_2026_27.find(f=>!findResultForPred({...f,season:CURRENT_SEASON}));
      f.date='2030-01-01';f.time='12:00';
      predMatches=[{...f,season:CURRENT_SEASON,kickoff:f.time,hDisadv:0,aDisadv:0,hXI:[],aXI:[]}];
      startPredictionKickoffLock();autoLockDuePredictions();
    });
    await app.page.clock.runFor(31000);
    assert.equal(await app.page.evaluate(()=>lockedPredictions.length),0);
    const result=await app.page.evaluate(()=>{
      const match=predMatches[0];
      const expected=getPred3MatchPrediction(match);
      const saved=lockPredictionSystem(match,'P2','saved_to_accuracy',false);
      const snapshot=JSON.stringify(lockedPredictions[0]);
      match.hDisadv=20;clearPred3Cache();
      const frozen=getPred3MatchPrediction(match);
      autoLockDuePredictions();
      return {saved,count:lockedPredictions.length,valid:lockedPredictionIntegrityOK(lockedPredictions[0]),reason:lockedPredictions[0].lockedReason,expected:expected.hP,frozen:frozen.hP,unchanged:snapshot===JSON.stringify(lockedPredictions[0]),inputSource:lockedPredictions[0].snapshot.inputs.homeXI.source};
    });
    assert.equal(result.saved,true);assert.equal(result.count,1);assert.equal(result.valid,true);
    assert.equal(result.reason,'saved_to_accuracy');assert.equal(result.expected,result.frozen);
    assert.equal(result.unchanged,true);assert.ok(result.inputSource);
    assert.deepEqual(app.errors,[]);
  }finally{await app.close();}
});
test('browser: recovery, incomplete import, immutable calculation, no late lock, quota and reload',{timeout:120000},async()=>{
  const app=await openApp(root);
  try{
    const full=JSON.parse(fs.readFileSync(path.join(root,A.FILE),'utf8'));
    const archive={...full,records:full.records.filter(r=>r.source.kind==='recovered-export')};
    const report=await app.page.evaluate(archive=>{
      mergePublishedPredictionArchive(archive);
      const count=lockedPredictions.length;
      const original=JSON.stringify(lockedPredictions.map(l=>[l.id,l.hP,l.dP,l.aP,l.integrity]));
      const partial=normalizeStoredData({app:'chance-liga-dashboard',lockedPredictions:lockedPredictions.filter(l=>Number(l.round)===12),predMatches:[]});
      if(!applyStoredData(partial))throw new Error('Partial import failed');
      const retained=lockedPredictions.length;
      const journalBefore=localStorage.getItem(PredictionArchive.JOURNAL);
      persistSnapshot();
      const afterSave=lockedPredictions.length;
      const eligible=lockedPredictions.filter(l=>predictionEvaluationStatus(l).eligible).length;
      const first=lockedPredictions[0];
      const before=first.hP;
      const m=predMatches.find(m=>m.home===first.home&&m.away===first.away&&Number(m.round)===Number(first.round));
      m.hDisadv=20;clearPred3Cache();
      const frozen=getPred3MatchPrediction(m).hP;
      // A fixture without a prior snapshot must not acquire one after kickoff.
      const f=OFFICIAL_SCHEDULE_2026_27.find(f=>!lockedPredictions.some(l=>predictionFixture(l)?.officialMatchId===f.officialMatchId));
      f.date='2000-01-01';f.time='12:00';
      const past={...f,season:CURRENT_SEASON,kickoff:f.time,hDisadv:0,aDisadv:0,hXI:[],aXI:[]};
      predMatches.push(past);
      const lateSave=lockPredictionSystem(past,'P2','test',false);
      const beforeAuto=lockedPredictions.length;
      autoLockDuePredictions();
      const autoLate=!!findLockedPrediction(past,'P2');
      // Quota failure preserves the compact history, never deletes it to fit players.
      const journalSaved=localStorage.getItem(PredictionArchive.JOURNAL);
      const set=Storage.prototype.setItem;
      Storage.prototype.setItem=function(key,value){if(key===STORAGE_KEY)throw new DOMException('Quota','QuotaExceededError');return set.call(this,key,value);};
      let quota=false;
      try{persistSnapshot();}catch{quota=true;}
      Storage.prototype.setItem=set;
      const journalAfter=readPredictionJournal().locks.length;
      // A stale server response cannot remove recovered records.
      let rejectedShorter=false;
      try{mergePublishedPredictionArchive({...archive,records:archive.records.slice(1)});}catch{rejectedShorter=true;}
      return {count,retained,afterSave,eligible,before,frozen,lateSave,autoLate,quota,journalAfter,rejectedShorter,journalExists:!!journalSaved,signatureUnchanged:original===JSON.stringify(lockedPredictions.slice(0,count).map(l=>[l.id,l.hP,l.dP,l.aP,l.integrity])),errors:window._lastDataValidationErrors||[]};
    },archive);
    assert.equal(report.count,40);
    assert.equal(report.retained,40);
    assert.equal(report.afterSave,40);
    assert.equal(report.eligible,38);
    assert.equal(report.before,report.frozen);
    assert.equal(report.lateSave,false);
    assert.equal(report.autoLate,false);
    assert.equal(report.quota,true);
    assert.equal(report.journalExists,true);
    assert.equal(report.journalAfter,40);
    assert.equal(report.rejectedShorter,true);
    assert.equal(report.signatureUnchanged,true);
    assert.deepEqual(report.errors,[]);
    await app.page.reload({waitUntil:'load'});
    assert.equal(await app.page.evaluate(()=>lockedPredictions.length),40);
    assert.deepEqual(app.errors,[]);
  }finally{await app.close();}
});
