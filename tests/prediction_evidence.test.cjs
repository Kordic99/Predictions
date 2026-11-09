'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const E=require('../prediction-evidence.js'),A=require('../prediction-archive-core.js');
const {openApp}=require('../scripts/archive_runtime.cjs');
const {calculate}=require('../scripts/weekend_predictions.cjs');
const {makeRecord}=require('../scripts/archive_predictions.cjs');
const root=path.resolve(__dirname,'..'),read=f=>JSON.parse(fs.readFileSync(path.join(root,f),'utf8'));
const cutoff='2026-11-13T07:00:00Z',target='2026-11-15T14:00:00Z';
const game=(id,time,extra={})=>({id,kickoffAt:time,completedAt:new Date(Date.parse(time)+2*3600000).toISOString(),fetchedAt:'2026-11-13T06:59:00Z',sourceUrl:'https://www.livesport.cz/zapas/'+id+'/',status:'completed',competition:'Evropská liga',score:'9:0',...extra});
function feed(matches){return {schemaVersion:1,season:'2026/27',fetchedAt:'2026-11-13T06:59:00Z',teams:{H:{matches,news:[],coverage:{results:'ok',fixtures:'ok'}}}};}

test('last five are per-team actual league dates, including higher-round midweek and lower-round postponement',()=>{
  const rows=Array.from({length:7},(_,i)=>({h:'H',a:'A',r:[1,2,7,4,5,6,3][i],season:'2026/27',date:`2026-11-${String(i+1).padStart(2,'0')}T16:00:00Z`,score:'1:0'}));
  rows.push({...rows[0],r:99,competition:'MOL Cup',date:'2026-11-08T17:00:00Z'},
    {...rows[1],r:100,date:'2026-11-14T17:00:00Z'},
    {...rows[2],r:101,date:'2026-11-08T17:00:00Z',knownAt:'2026-11-14T12:00:00Z'},
    {...rows[2],r:102,date:'2026-11-08T17:00:00Z',updatedAt:'invalid'},
    {...rows[3],r:103,date:null},
    {...rows[4],r:104,date:'2026-11-09T17:00:00Z',status:'live'},
    {...rows[4],r:105,date:'2026-11-09T17:00:00Z',completedAt:'2026-11-14T17:00:00Z'},
    {...rows[0]});
  const selected=E.chronological(rows,{cutoff,observedAt:'2026-11-13T06:00:00Z',kickoff:r=>Date.parse(r.date),season:'2026/27'});
  assert.equal(selected.length,7);assert.deepEqual(E.lastFive(selected,'H').map(r=>r.r),[3,6,5,4,7]);
  assert.deepEqual(E.lastFive(selected,'B'),[]);
  assert.equal(E.chronological(rows,{cutoff,observedAt:'2026-11-14T06:00:00Z',kickoff:r=>Date.parse(r.date)}).length,0);
});
test('load includes European/cup matches and verified planned midweek fixtures, caps effect at five percent',()=>{
  const f=feed([game('a','2026-11-09T18:00:00Z'),game('b','2026-11-11T18:00:00Z'),game('c','2026-11-12T18:00:00Z'),
    game('d','2026-11-14T18:00:00Z',{status:'scheduled',score:null,completedAt:null})]);
  assert.doesNotThrow(()=>E.validateFeed(f));
  const c=E.context(f,'H','A',target,cutoff);
  assert.equal(c.home.multiplier,.95);assert.equal(c.home.projected,true);assert.equal(c.home.count7,4);
  assert.equal(c.away.multiplier,1);assert.equal(c.away.coverage,'partial-or-stale');
  f.teams.H.matches.forEach(m=>m.score='0:9');assert.equal(E.context(f,'H','A',target,cutoff).home.multiplier,.95);
});
test('an ongoing unmarked score does not enter form; an explicit completed event does',()=>{
  const row={h:'H',a:'A',r:3,season:'2026/27',score:'1:0',date:'2026-11-13T06:00:00Z'};
  const opts={cutoff,observedAt:'2026-11-13T06:50:00Z',kickoff:r=>Date.parse(r.date)};
  assert.equal(E.chronological([row],opts).length,0);
  assert.equal(E.chronological([{...row,completedAt:'2026-11-13T06:55:00Z'}],opts).length,1);
  assert.equal(E.chronological([{...row,completedAt:'2026-11-13T07:01:00Z'}],opts).length,0);
});
test('future observations, live scores, duplicate and stale feeds cannot cause context correction',()=>{
  const m=game('a','2026-11-12T18:00:00Z');
  const f=feed([m,m,game('b','2026-11-12T20:00:00Z',{fetchedAt:'2026-11-14T06:00:00Z'}),game('c','2026-11-13T18:00:00Z')]);
  assert.equal(E.context(f,'H','A',target,cutoff).home.matches.length,1);
  f.teams.H.coverage.fixtures='unavailable';assert.equal(E.context(f,'H','A',target,cutoff).home.multiplier,1);
  f.fetchedAt='2026-11-10T07:00:00Z';assert.equal(E.context(f,'H','A',target,cutoff).home.matches.length,0);
  f.fetchedAt='2026-11-14T07:00:00Z';const c=E.context(f,'H','A',target,cutoff);assert.equal(c.feedCheckedAt,null);assert.equal(c.home.multiplier,1);
});
test('ambiguous news remains informational and cannot be assigned an automatic effect',()=>{
  const f=feed([]),news={title:'Možná rotace; hráč není zraněný',sourceUrl:'https://club.example/news/1',publishedAt:'2026-11-12T12:00:00Z',fetchedAt:'2026-11-13T06:59:00Z',effect:0};
  f.teams.H.news=[news,{...news,publishedAt:'2026-11-14T00:00:00Z'}];
  const c=E.context(f,'H','A',target,cutoff);assert.equal(c.home.news.length,1);assert.equal(c.home.multiplier,1);
  news.effect=.03;assert.throws(()=>E.validateFeed(f),/zprávy/);
});
test('metrics use the same 1X2 outcome and never treat double chance as a 1X2 probability',()=>{
  const m=E.metrics({hP:50,dP:30,aP:20,predOutcome:'1X',hardTip:'1',topScore:'1:0'},'0:0');
  assert.ok(Math.abs(m.brier-.78)<1e-10);assert.equal(m.logLoss,-Math.log(.3));assert.equal(m.correct,true);assert.equal(m.hardCorrect,false);assert.equal(m.exact,false);
});
test('published free feed validates without paid services',()=>assert.doesNotThrow(()=>E.validateFeed(read('prediction-evidence-live.json'))));

test('real model freezes paired snapshots, rejects future evidence, restores new page and leaves all old records unchanged',{timeout:120000},async()=>{
  const app=await openApp(root),schedule=read('schedule-live.json'),archive=read(A.FILE),before=JSON.stringify(archive);
  const context=read('match-context-live.json'),rosters=read('rosters-live.json'),evidence=read('prediction-evidence-live.json');
  const f=schedule.matches.find(f=>!f.score&&!archive.records.some(r=>r.fixtureId===String(f.officialMatchId)));
  f.date='2030-01-06';f.time='15:00';delete f.status;
  const side={source:'proxy',players:[],missing:[],uncertain:[]};
  context.fixtures=[{...f,home:A.team(f.home),away:A.team(f.away),livesportMatchId:'pairedtest',homeContext:side,awayContext:side}];context.updatedAt='2030-01-04T07:00:00Z';
  evidence.fetchedAt='2030-01-04T07:00:00Z';
  evidence.teams[A.team(f.home)]={fetchedAt:evidence.fetchedAt,matches:[game('cup','2030-01-03T18:00:00Z',{fetchedAt:evidence.fetchedAt})],news:[],coverage:{results:'ok',fixtures:'ok'}};
  try{
    await app.page.clock.install({time:new Date('2030-01-04T07:05:00Z')});
    const [lock]=await calculate(app,{schedule,rosters,context,evidence,archive,fixtures:[f]});
    const v=lock.snapshot.calculation.contextVariant;
    assert.equal(v.cutoff,lock.lockedAt);assert.equal(v.evidence.home.multiplier,.97);assert.notEqual(v.hP,lock.hP);
    assert.ok(Buffer.byteLength(JSON.stringify(lock))<55000,'single-snapshot submission must fit transport limit');
    assert.doesNotThrow(()=>A.validateLock(lock));
    const bad=structuredClone(lock);bad.snapshot.calculation.contextVariant.evidence.home.matches[0].fetchedAt='2030-01-05T00:00:00Z';assert.throws(()=>A.validateLock(bad),/Future/);
    const next=A.appendRecords(archive,[makeRecord(lock,f,{kind:'friday-automatic'},'2030-01-04T07:06:00Z')]);A.assertAppendOnly(archive,next);
    const state=await app.page.evaluate(({lock,next})=>{
      mergePublishedPredictionArchive(next);
      const saved=findLockedPrediction(lock,'P2');const frozen=JSON.stringify(saved.snapshot);
      predictionEvidenceLive={schemaVersion:1,season:'2026/27',fetchedAt:'2031-01-01T00:00:00Z',teams:{}};
      const m=predMatches.find(m=>m.home===lock.home&&m.away===lock.away&&Number(m.round)===Number(lock.round));
      clearPred3Cache();const p=getPred3MatchPrediction(m);
      sw('accuracy-new');renderAccuracyNew();
      const before=saved.snapshot.calculation.modelMeta.lastFive;
      // An officially completed event supplies its actual time even if the Results row has no kickoff.
      results.push({season:CURRENT_SEASON,r:999,h:lock.home,a:lock.away,date:'2030-01-04',score:'2:2',livesportMatchId:'early-test'});
      predictionEvidenceLive={fetchedAt:'2030-01-04T03:00:00Z',teams:{test:{matches:[{id:'early-test',home:lock.home,away:lock.away,score:'2:2',status:'completed',kickoffAt:'2030-01-04T00:00:00Z',completedAt:'2030-01-04T02:00:00Z',fetchedAt:'2030-01-04T03:00:00Z'}]}}};
      const actualTimeUsed=p3ChronologicalMatches(CURRENT_SEASON,null).some(m=>m.r===999&&new Date(m._playedAt).toISOString()==='2030-01-04T00:00:00.000Z');
      return {frozen:frozen===JSON.stringify(saved.snapshot),variant:p.contextVariant.hP,text:document.getElementById('an-list').textContent,active:document.getElementById('sec-accuracy-new').classList.contains('on'),cutoffs:before,actualTimeUsed};
    },{lock,next});
    assert.equal(state.frozen,true);assert.equal(state.variant,v.hP);assert.equal(state.active,true);assert.match(state.text,/zátěž|Zátěž/);
    assert.equal(state.actualTimeUsed,true);
    for(const games of Object.values(state.cutoffs))assert.ok(games.length<=5&&games.every(m=>Date.parse(m.playedAt)<Date.parse(lock.lockedAt)));
    assert.equal(JSON.stringify(archive),before);assert.deepEqual(app.errors,[]);
    if(process.env.EVIDENCE_SCREENSHOT){
      await app.page.evaluate(lock=>{results.push({season:lock.season,r:lock.round,h:lock.home,a:lock.away,score:'2:1'});renderAccuracyNew();},lock);
      await app.page.setViewportSize({width:1280,height:950});
      await app.page.screenshot({path:process.env.EVIDENCE_SCREENSHOT});
      await app.page.setViewportSize({width:390,height:844});
      await app.page.screenshot({path:process.env.EVIDENCE_SCREENSHOT.replace('.png','-mobile.png'),fullPage:true});
    }
  }finally{await app.close();}
});
