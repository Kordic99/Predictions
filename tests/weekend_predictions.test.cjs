'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const A=require('../prediction-archive-core.js');
const {weekendWindow,selectFixtures,verifyReceipt,calculate,run,INPUTS}=require('../scripts/weekend_predictions.cjs');
const {empty,makeRecord,digest}=require('../scripts/archive_predictions.cjs');
const {publishArchive}=require('../scripts/publish_prediction_archive.cjs');
const {openApp}=require('../scripts/archive_runtime.cjs');
const root=path.resolve(__dirname,'..'),read=file=>JSON.parse(fs.readFileSync(path.join(root,file),'utf8'));
const at=s=>Date.parse(s);

test('Prague Friday 08:00, DST, Saturday/Sunday catchup and next-week preview',()=>{
  for(const [now,friday,active] of [
    ['2026-11-09T10:00:00Z','2026-11-13',false],
    ['2026-11-13T06:59:00Z','2026-11-13',false],
    ['2026-11-13T07:00:00Z','2026-11-13',true],
    ['2026-07-31T05:59:00Z','2026-07-31',false],
    ['2026-07-31T06:00:00Z','2026-07-31',true],
    ['2026-11-14T22:00:00Z','2026-11-13',true],
    ['2026-11-15T22:59:00Z','2026-11-13',true],
    ['2026-11-15T23:00:00Z','2026-11-20',false],
  ])assert.deepEqual([weekendWindow(at(now)).friday,weekendWindow(at(now)).active],[friday,active]);
});

test('selection respects weekend dates, results, TBA, postponements, known locks and 240/30 validation',()=>{
  const schedule=read('schedule-live.json');
  schedule.matches.forEach(f=>{f.date='2026-11-23';f.score=null;f.time='15:00';delete f.status;});
  const f=schedule.matches.slice(0,8);
  f.forEach(x=>x.date='2026-11-21');
  f[1].date='2026-11-20';f[2].date='2026-11-22';
  f[3].time=null;f[4].status='postponed';f[5].score='0:0';f[6].date='2026-11-19';
  const lock={predSystem:'P2',season:A.SEASON,home:A.team(f[7].home),away:A.team(f[7].away),round:f[7].round,hP:50,dP:30,aP:20,topScore:'1:0',predOutcome:'1X',savedAt:'2026-11-20T07:01:00Z'};
  const archive=A.appendRecords(empty(),[makeRecord(lock,f[7],{kind:'test'},'2026-11-20T07:02:00Z')]);
  const s=selectFixtures(schedule,archive,at('2026-11-20T07:05:00Z'));
  assert.deepEqual(s.selected.map(x=>x.officialMatchId),[f[1],f[0],f[2]].map(x=>x.officialMatchId));
  assert.deepEqual(new Set(s.skipped.map(x=>x.reason)),new Set(['unknown_kickoff','postponed','already_played','already_archived']));
  assert.ok(selectFixtures(schedule,archive,at('2026-11-21T15:00:00Z')).skipped.some(x=>x.reason==='kickoff_passed'));
  assert.throws(()=>selectFixtures({...schedule,matches:schedule.matches.slice(1)},archive,Date.now()),/240/);
  schedule.matches[0].round=30;assert.throws(()=>selectFixtures(schedule,archive,Date.now()),/round/);
});

test('receipts bind successful fresh checks to exact model and data bytes',()=>{
  const now=Date.now(),receipt={schemaVersion:1,checkedAt:new Date(now).toISOString(),runId:process.env.GITHUB_RUN_ID,
    checks:['official-schedule','official-rosters-and-values','livesport-stats','strict-context'],
    hashes:Object.fromEntries(INPUTS.map(f=>[f,digest(fs.readFileSync(path.join(root,f)))]))};
  assert.doesNotThrow(()=>verifyReceipt(receipt,root,now));
  assert.throws(()=>verifyReceipt({...receipt,checkedAt:new Date(now-7200001).toISOString()},root,now),/expired/);
  assert.throws(()=>verifyReceipt({...receipt,checks:[]},root,now),/check/);
  assert.throws(()=>verifyReceipt({...receipt,hashes:{...receipt.hashes,'index.html':'wrong'}},root,now),/changed/);
});

test('production invocation on Monday cannot create forecasts or alter history',async()=>{
  const before=fs.readFileSync(path.join(root,A.FILE)),clock=Date.now;
  Date.now=()=>at('2026-11-09T10:00:00Z');
  try{
    const result=await run({root,dryRun:false,receiptPath:'must-not-read-an-unverified-receipt'});
    assert.equal(result.status,'outside_friday_weekend_window');
    assert.equal(result.added,0);assert.deepEqual(fs.readFileSync(path.join(root,A.FILE)),before);
  }finally{Date.now=clock;}
});

test('Friday publisher skips a concurrently archived fixture without PUT',async()=>{
  const f={officialMatchId:'8327',home:'Viktoria Plzeň',away:'Slovan Liberec',round:1,date:'2030-01-04',time:'17:00'};
  const lock={home:f.home,away:f.away,round:1,season:A.SEASON,predSystem:'P2',hP:50,dP:30,aP:20,predOutcome:'1X',topScore:'1:0',savedAt:'2030-01-04T08:00:00Z'};
  const prior=makeRecord(lock,f,{kind:'authenticated-browser'},'2030-01-04T08:01:00Z');
  const generated=makeRecord({...lock,hP:49,aP:21},f,{kind:'friday-automatic'},'2030-01-04T08:02:00Z');
  const remote=A.appendRecords(empty(),[prior]),local=A.appendRecords(empty(),[generated]);
  const oldFetch=global.fetch,token=process.env.GITHUB_TOKEN;
  process.env.GITHUB_TOKEN='test';let put=0;
  global.fetch=async(url,options)=>{
    if(options.method==='PUT'){put++;throw new Error('Unwanted duplicate PUT');}
    const data=url.includes('/git/ref/')?{object:{sha:'head'}}:url.includes('/git/commits/')?{tree:{sha:'tree'}}:
      url.includes('/git/trees/')?{tree:[{path:A.FILE,sha:'blob'}]}:{content:Buffer.from(JSON.stringify(remote)).toString('base64')};
    return {ok:true,status:200,json:async()=>data};
  };
  try{await publishArchive(local);assert.equal(put,0);}finally{global.fetch=oldFetch;if(token===undefined)delete process.env.GITHUB_TOKEN;else process.env.GITHUB_TOKEN=token;}
});

test('real model consumes refreshed feeds; frozen server records restore both tabs without changing old history',{timeout:120000},async()=>{
  const app=await openApp(root),schedule=read('schedule-live.json'),rosters=read('rosters-live.json');
  const archive=read(A.FILE),context=read('match-context-live.json');
  const fixture=schedule.matches.find(f=>!f.score&&!archive.records.some(r=>r.fixtureId===String(f.officialMatchId)));
  fixture.date='2030-01-05';fixture.time='15:00';delete fixture.status;
  const side={source:'proxy',players:[],missing:[],uncertain:[]};
  context.fixtures=[{...fixture,home:A.team(fixture.home),away:A.team(fixture.away),livesportMatchId:'testfuture',sourceUrl:'https://www.livesport.cz/',homeContext:side,awayContext:side}];
  context.updatedAt='2030-01-04T07:00:00Z';
  try{
    await app.page.clock.install({time:new Date('2030-01-04T07:05:00Z')});
    const locks=await calculate(app,{schedule,rosters,context,archive,fixtures:[fixture]});
    assert.equal(locks.length,1);const lock=locks[0];
    assert.equal(lock.lockedReason,'friday_automatic');
    assert.equal(lock.snapshot.inputs.contextVersion,context.updatedAt);
    assert.equal(lock.snapshot.inputs.livesportMatchId,'testfuture');
    assert.equal(lock.snapshot.inputs.homeXI.names.length,11);
    const record=makeRecord(lock,fixture,{kind:'friday-automatic'},'2030-01-04T07:06:00Z');
    const next=A.appendRecords(archive,[record]);A.assertAppendOnly(archive,next);
    assert.equal(selectFixtures(schedule,next,at('2030-01-04T07:06:00Z')).selected.some(f=>f.officialMatchId===fixture.officialMatchId),false);
    // Simulate a fresh browser without the large server-only player cache.
    await app.page.evaluate(()=>localStorage.clear());
    await app.page.reload({waitUntil:'load',timeout:60000});
    const restored=await app.page.evaluate(next=>{
      mergePublishedPredictionArchive(next);
      const row=next.records.at(-1),lock=findLockedPrediction(row.lock,'P2');
      const match=predMatches.find(m=>String(m.officialMatchId)===row.fixtureId);
      return {valid:lockedPredictionIntegrityOK(lock),inPredictions:!!match,hP:getPred3MatchPrediction(match).hP,reason:lock.lockedReason};
    },next);
    assert.equal(restored.valid,true);assert.equal(restored.inPredictions,true);assert.equal(restored.hP,lock.hP);
    assert.equal(restored.reason,'friday_automatic');assert.deepEqual(app.errors,[]);
  }finally{await app.close();}
});
