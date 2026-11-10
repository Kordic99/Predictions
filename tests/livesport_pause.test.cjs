'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {openApp}=require('../scripts/archive_runtime.cjs');
const root=path.resolve(__dirname,'..');

test('Livesport workflows cannot run on cron or dispatch; archive and official schedule remain independent',()=>{
  for(const name of ['update-player-stats','update-match-context','update-rosters']){
    const text=fs.readFileSync(path.join(root,`.github/workflows/${name}.yml`),'utf8');
    assert.doesNotMatch(text,/\n\s+schedule:/);
    assert.match(text,/if:\s*\$\{\{ false \}\}/);
  }
  const archive=fs.readFileSync(path.join(root,'.github/workflows/archive-predictions.yml'),'utf8');
  assert.doesNotMatch(archive,/if:\s*\$\{\{ false \}\}/);
  const schedule=fs.readFileSync(path.join(root,'.github/workflows/update-schedule.yml'),'utf8');
  assert.match(schedule,/python scripts\/update_schedule\.py/);
  assert.doesNotMatch(schedule,/update_livesport|update_rosters|update_match_context/);
});

test('paused browser performs zero imports and preserves predictions, Results, Players and archive',{timeout:120000},async()=>{
  const archivePath=path.join(root,'data/predictions/2026-27.json');
  const savedArchive=fs.readFileSync(archivePath);
  const savedRecordCount=JSON.parse(savedArchive).records.length;
  const app=await openApp(root);
  try{
    await app.page.evaluate(payload=>mergePublishedPredictionArchive(payload),JSON.parse(savedArchive));
    await app.page.clock.install();
    const checked=await app.page.evaluate(async()=>{
      const state=()=>JSON.stringify({predMatches,lockedPredictions,results,players});
      const before=state(),fetchBefore=window.fetch,requests=[];
      window.fetch=async url=>{requests.push(String(url));throw new Error('Unexpected request');};
      try{
        startAutomaticResultsCheck();lsScheduleNextAutomaticCheck(1);lsUpdateAutoResultsCountdown();
        const automatic=await runAutomaticResultsCheck();
        const manual=await checkAwaitingResults({automatic:false});
        for(const call of [()=>lsFetchJson('https://1.ds.lsapp.eu/pq_graphql'),
                          ()=>lsCheckOneFixture({home:'Teplice',away:'Pardubice'})]){
          let blocked=false;
          try{await call();}catch(error){blocked=/pozastaven/.test(error.message);}
          if(!blocked)throw new Error('Direct fetch did not respect pause');
        }
        if(exportUpdateRequest().status!=='paused')throw new Error('Career update not paused');
        let requestBlocked=false;
        try{buildUpdateRequest();}catch(error){requestBlocked=/pozastaven/.test(error.message);}
        return {automatic,manual,requests,requestBlocked,unchanged:state()===before,
          locks:lockedPredictions.length,timers:[livesportAutoCheckTimer,livesportAutoCountdownTimer],
          badge:document.getElementById('auto-results-badge').textContent,
          archiveRecords:predictionArchiveState.data.records.length,
          exportAvailable:typeof exportJSON==='function',importAvailable:typeof importJSON==='function'};
      }finally{window.fetch=fetchBefore;}
    });
    assert.equal(checked.automatic.status,'paused');assert.equal(checked.manual.status,'paused');
    assert.deepEqual(checked.requests,[]);assert.deepEqual(checked.timers,[null,null]);
    assert.equal(checked.requestBlocked,true);assert.equal(checked.unchanged,true);
    assert.equal(checked.locks,savedRecordCount);assert.equal(checked.archiveRecords,savedRecordCount);
    assert.equal(checked.exportAvailable,true);assert.equal(checked.importAvailable,true);
    assert.match(checked.badge,/pozastaveno/);
    await app.page.clock.runFor(6*60*1000);
    assert.deepEqual(await app.page.evaluate(()=>[livesportAutoCheckTimer,livesportAutoCountdownTimer]),[null,null]);
    assert.deepEqual(app.errors,[]);
  }finally{await app.close();}
  assert.deepEqual(fs.readFileSync(archivePath),savedArchive);
});
